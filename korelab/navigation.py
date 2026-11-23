"""Cross-check native OpenKore field cells against the active rAthena map cache."""
from __future__ import annotations

import gzip
import hashlib
import struct
import zlib
from pathlib import Path


def _openkore_field(fields: Path, map_name: str) -> tuple[int, int, bytes, Path]:
    for path in (fields / f"{map_name}.fld2", fields / f"{map_name}.fld2.gz"):
        if not path.is_file():
            continue
        raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
        if len(raw) < 4:
            raise ValueError(f"OpenKore field file is truncated: {path}")
        width, height = struct.unpack_from("<HH", raw)
        cells = raw[4:]
        if not width or not height or len(cells) != width * height:
            raise ValueError(f"OpenKore field dimensions do not match cell data: {path}")
        return width, height, cells, path
    raise FileNotFoundError(f"OpenKore has no .fld2 field for {map_name} under {fields}")


def _rathena_map(rathena: Path, map_name: str) -> tuple[int, int, bytes, Path]:
    candidates = (rathena / "db/import/map_cache.dat", rathena / "db/re/map_cache.dat",
                  rathena / "db/map_cache.dat")
    for path in candidates:
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if len(raw) < 8:
            raise ValueError(f"rAthena map cache is truncated: {path}")
        _file_size, count = struct.unpack_from("<IH", raw)
        offset = 8  # rAthena's native struct includes alignment padding here.
        for _ in range(count):
            if offset + 20 > len(raw):
                raise ValueError(f"rAthena map cache has a truncated map entry: {path}")
            name = raw[offset:offset + 12].split(b"\0", 1)[0].decode("ascii", "strict")
            width, height, compressed_size = struct.unpack_from("<hhI", raw, offset + 12)
            offset += 20
            end = offset + compressed_size
            if width <= 0 or height <= 0 or end > len(raw):
                raise ValueError(f"rAthena map cache has invalid dimensions or length: {path}")
            if name == map_name:
                cells = zlib.decompress(raw[offset:end])
                if len(cells) != width * height:
                    raise ValueError(f"rAthena map dimensions do not match cell data: {path}")
                return width, height, cells, path
            offset = end
    raise FileNotFoundError(f"active rAthena map cache does not contain {map_name}")


def select_verified_destination(*, map_name: str, origin: dict[str, int],
                                openkore_fields: Path, rathena: Path,
                                max_distance: int = 8) -> dict:
    """Pick the closest non-origin walkable cell agreed on by both native data sets."""
    if not map_name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_" for ch in map_name):
        raise ValueError("movement map must be a lowercase native map name")
    if type(max_distance) is not int or not 1 <= max_distance <= 100:
        raise ValueError("verified movement max_distance must be an integer from 1 to 100")
    x0, y0 = origin.get("x"), origin.get("y")
    if type(x0) is not int or type(y0) is not int or min(x0, y0) < 0:
        raise ValueError("movement origin requires nonnegative integer x and y")
    fields = openkore_fields.expanduser().resolve()
    width, height, open_cells, field_path = _openkore_field(fields, map_name)
    server_width, server_height, server_cells, cache_path = _rathena_map(rathena.expanduser().resolve(), map_name)
    if (width, height) != (server_width, server_height):
        raise ValueError(f"OpenKore/rAthena dimensions disagree for {map_name}: "
                         f"{width}x{height} vs {server_width}x{server_height}")
    candidates = []
    for y in range(max(0, y0 - max_distance), min(height, y0 + max_distance + 1)):
        for x in range(max(0, x0 - max_distance), min(width, x0 + max_distance + 1)):
            distance = max(abs(x - x0), abs(y - y0))
            if distance == 0 or distance > max_distance:
                continue
            offset = y * width + x
            # OpenKore FLD2 uses the walk bit; rAthena GAT cells 0, 2, 3, 4 and 6 are walkable.
            if open_cells[offset] & 1 and server_cells[offset] in (0, 2, 3, 4, 6):
                candidates.append((distance, abs(x - x0) + abs(y - y0), y, x,
                                   server_cells[offset]))
    if not candidates:
        raise ValueError(f"no mutually verified walkable destination near {map_name} ({x0},{y0})")
    distance, _manhattan, y, x, cell_type = min(candidates)
    return {"map": map_name, "x": x, "y": y, "distance": distance,
            "openkore_field": str(field_path), "openkore_field_sha256": hashlib.sha256(field_path.read_bytes()).hexdigest(),
            "rathena_map_cache": str(cache_path), "rathena_map_cache_sha256": hashlib.sha256(cache_path.read_bytes()).hexdigest(),
            "rathena_cell_type": cell_type,
            "evidence": "destination cell is walkable in both OpenKore FLD2 and active rAthena map cache"}


def verify_walkable_cell(*, map_name: str, x: int, y: int,
                         openkore_fields: Path, rathena: Path) -> dict:
    """Require one initial map cell to be walkable in both installed data sources."""
    fields = openkore_fields.expanduser().resolve()
    width, height, open_cells, field_path = _openkore_field(fields, map_name)
    server_width, server_height, server_cells, cache_path = _rathena_map(rathena.expanduser().resolve(), map_name)
    if (width, height) != (server_width, server_height):
        raise ValueError(f"OpenKore/rAthena dimensions disagree for {map_name}: "
                         f"{width}x{height} vs {server_width}x{server_height}")
    if type(x) is not int or type(y) is not int or not (0 <= x < width and 0 <= y < height):
        raise ValueError(f"start point {map_name} ({x},{y}) is outside the verified map dimensions")
    offset = y * width + x
    cell_type = server_cells[offset]
    if not (open_cells[offset] & 1) or cell_type not in (0, 2, 3, 4, 6):
        raise ValueError(f"start point {map_name} ({x},{y}) is not walkable in both OpenKore and rAthena data")
    return {"map": map_name, "x": x, "y": y, "rathena_cell_type": cell_type,
            "openkore_field_sha256": hashlib.sha256(field_path.read_bytes()).hexdigest(),
            "rathena_map_cache_sha256": hashlib.sha256(cache_path.read_bytes()).hexdigest(),
            "evidence": "initial cell is walkable in both OpenKore FLD2 and active rAthena map cache"}
