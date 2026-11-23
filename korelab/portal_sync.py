"""Targeted, server-backed corrections for OpenKore's permanent portal table."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .locking import try_lock
from .platform_support import set_private_permissions


class PortalEvidenceError(RuntimeError):
    """The active server evidence is missing, stale, or ambiguous."""


_MAP = r"[A-Za-z0-9_|@-]+"
_PORTAL_ROW = re.compile(
    rf"^(?P<indent>\s*)(?P<source>{_MAP})\s+(?P<sx>\d{{1,3}})\s+(?P<sy>\d{{1,3}})\s+"
    rf"(?P<dest>{_MAP})\s+(?P<dx>\d{{1,3}})\s+(?P<dy>\d{{1,3}})"
    rf"(?P<tail>[^#]*?)(?P<comment>\s+#.*)?\s*$"
)
_WARP_DECL = re.compile(
    r"^\s*([A-Za-z0-9_-]+),(\d{1,3}),(\d{1,3}),-?\d+\s+warp\s+"
    r"([A-Za-z0-9_#.-]+)\s+(\d{1,3}),(\d{1,3}),([A-Za-z0-9_-]+),(\d{1,3}),(\d{1,3})\s*$",
    re.I,
)


def _safe_server_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError("portal server ID must use 1–80 letters, digits, dots, underscores, or hyphens")
    return value


def resolve_rathena_source_root(path: Path) -> Path:
    """Resolve a managed runtime's recorded, read-only source checkout."""
    root = path.expanduser().resolve()
    if (root / "npc").is_dir() and (root / "src/config/renewal.hpp").is_file():
        return root
    owner_path = root / "owner.json"
    if owner_path.is_file():
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        source = Path(owner.get("rathena_runtime_source", "")).expanduser().resolve()
        if (source / "npc").is_dir() and (source / "src/config/renewal.hpp").is_file():
            return source
    raise PortalEvidenceError("selected rAthena path does not expose its active NPC source tree")


def _edition(root: Path, requested: str) -> str:
    if requested not in {"auto", "renewal", "pre-re"}:
        raise ValueError("rAthena edition must be auto, renewal, or pre-re")
    if requested != "auto":
        return requested
    header = root / "src/config/renewal.hpp"
    if not header.is_file():
        raise PortalEvidenceError("cannot determine the rAthena NPC script edition; specify --edition")
    text = header.read_text(encoding="utf-8", errors="replace")
    if re.search(r"(?m)^\s*#\s*define\s+RENEWAL\b", text):
        return "renewal"
    if re.search(r"(?m)^\s*#\s*undef\s+RENEWAL\b", text) or "RENEWAL" not in text:
        return "pre-re"
    raise PortalEvidenceError("rAthena renewal build mode is ambiguous; specify --edition")


def _active_npc_files(root: Path, edition: str) -> list[Path]:
    script_root = root / "npc" / ("re" if edition == "renewal" else "pre-re") / "scripts_main.conf"
    if not script_root.is_file():
        raise PortalEvidenceError(f"active rAthena NPC script entrypoint is missing: {script_root}")
    root = root.resolve()
    loaded: list[Path] = []
    visited: set[Path] = set()

    def visit(conf: Path) -> None:
        conf = conf.resolve()
        if not conf.is_relative_to(root):
            raise PortalEvidenceError("rAthena NPC import escapes the selected source checkout")
        if conf in visited:
            return
        visited.add(conf)
        if not conf.is_file():
            raise PortalEvidenceError(f"active rAthena NPC configuration references missing file: {conf}")
        for raw in conf.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.split("//", 1)[0].strip()
            match = re.match(r"^(import|npc)\s*:\s*(\S+)", line, re.I)
            if not match:
                continue
            kind, relative = match.groups()
            target = (root / relative).resolve()
            if kind.casefold() == "import":
                visit(target)
            else:
                if not target.is_relative_to(root):
                    raise PortalEvidenceError("rAthena NPC file reference escapes the selected source checkout")
                if not target.is_file():
                    raise PortalEvidenceError(f"active rAthena NPC configuration references missing file: {target}")
                loaded.append(target)

    visit(script_root)
    # Preserve load order while avoiding repeated imports.
    return list(dict.fromkeys(loaded))


def _active_warps(root: Path, edition: str, source_map: str) -> list[dict]:
    files = _active_npc_files(root, edition)
    candidates = []
    active_text = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        active_text.append(text)
        for line_number, raw in enumerate(text.splitlines(), 1):
            line = raw.split("//", 1)[0].strip()
            match = _WARP_DECL.fullmatch(line)
            if not match:
                continue
            src, x, y, name, width, height, dst, dx, dy = match.groups()
            if src.casefold() != source_map.casefold():
                continue
            candidates.append({
                "source_map": src, "source_x": int(x), "source_y": int(y),
                "trigger_width": int(width), "trigger_height": int(height),
                "destination_map": dst, "destination_x": int(dx), "destination_y": int(dy),
                "npc_name": name, "source_file": path.relative_to(root).as_posix(),
                "source_line": line_number, "source_text": line,
                "source_sha256": hashlib.sha256(line.encode()).hexdigest(),
                "kind": "standard_static_warp", "runtime_status": "not_independently_observed",
            })
    # Disabled/hidden controls make a static declaration insufficient evidence
    # that the portal is currently available.
    joined = "\n".join(active_text)
    for candidate in candidates:
        escaped = re.escape(candidate["npc_name"])
        candidate["visibility_control_found"] = bool(re.search(
            rf"(?is)\b(?:disablenpc|hidenpc|enablenpc|shownpc)\s+['\"]{escaped}['\"]", joined))
    return candidates


def resolve_static_portal(rathena_root: Path, source_map: str, x: int, y: int,
                          *, edition: str = "auto") -> dict:
    """Resolve a unique standard warp from the active source script hierarchy.

    The result explicitly distinguishes active-source evidence from runtime
    confirmation. Scripted, conditional and temporary teleports are excluded.
    """
    root = resolve_rathena_source_root(rathena_root)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", source_map) or any(
            type(value) is not int or not 0 <= value <= 999 for value in (x, y)):
        raise ValueError("portal source must be a valid rAthena map name and integer cell coordinates")
    selected_edition = _edition(root, edition)
    matches = _matching_warps(root, selected_edition, source_map, x, y)
    if len(matches) != 1:
        raise PortalEvidenceError(
            f"expected one active standard warp near {source_map} {x} {y}; found {len(matches)}; "
            "no table change was made")
    result = dict(matches[0])
    result.update({"edition": selected_edition, "queried_source": {"map": source_map, "x": x, "y": y},
                   "query_matches_warp_center": result["source_x"] == x and result["source_y"] == y,
                   "evidence_type": "active NPC script import hierarchy; static declaration",
                   "runtime_confirmation": "not available from source inspection"})
    if result["visibility_control_found"]:
        raise PortalEvidenceError(
            f"warp {result['npc_name']!r} has active script visibility controls; current availability is ambiguous")
    return result


def _matching_warps(root: Path, edition: str, source_map: str, x: int, y: int) -> list[dict]:
    warps = _active_warps(root, edition, source_map)
    exact = [warp for warp in warps if warp["source_x"] == x and warp["source_y"] == y]
    return exact or [warp for warp in warps
        if abs(warp["source_x"] - x) <= warp["trigger_width"]
        and abs(warp["source_y"] - y) <= warp["trigger_height"]]


def _active_source_digest(root: Path, edition: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    files = _active_npc_files(root, edition)
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest(), len(files)


def parse_portal_row(line: str) -> dict | None:
    match = _PORTAL_ROW.fullmatch(line.rstrip("\r\n"))
    if not match:
        return None
    values = match.groupdict()
    try:
        return {"source_map": values["source"], "source_x": int(values["sx"]),
                "source_y": int(values["sy"]), "destination_map": values["dest"],
                "destination_x": int(values["dx"]), "destination_y": int(values["dy"]),
                "metadata": values["tail"].strip(), "comment": values["comment"] or "",
                "line": line.rstrip("\r\n")}
    except (TypeError, ValueError):
        return None


def select_openkore_portals(upstream_tables: Path, table_folders: tuple[str, ...]) -> tuple[Path, str]:
    upstream_tables = upstream_tables.expanduser().resolve()
    candidates = [(upstream_tables / folder / "portals.txt", folder) for folder in table_folders]
    candidates.append((upstream_tables / "portals.txt", ""))
    for path, folder in candidates:
        if path.is_file():
            return path, folder
    raise PortalEvidenceError("the selected OpenKore server table folders contain no portals.txt")


def _normalize(line: str) -> str:
    return " ".join(line.split())


def _apply_records(lines: list[str], records: list[dict]) -> list[str]:
    result = list(lines)
    for record in records:
        operation = record["operation"]
        previous = record.get("previous_row")
        desired = record.get("portal_row")
        exact_desired = [i for i, line in enumerate(result)
                         if desired and _normalize(line) == _normalize(desired)]
        if operation == "add":
            if exact_desired:
                continue
            if any((row := parse_portal_row(line)) and (desired_row := parse_portal_row(desired))
                   and (row["source_map"], row["source_x"], row["source_y"])
                   == (desired_row["source_map"], desired_row["source_x"], desired_row["source_y"])
                   for line in result):
                raise PortalEvidenceError("stored portal addition now conflicts with an existing source entry")
            result.append(desired)
        elif operation == "replace":
            if exact_desired and not any(_normalize(line) == _normalize(previous) for line in result):
                continue
            matches = [i for i, line in enumerate(result) if _normalize(line) == _normalize(previous)]
            if len(matches) != 1:
                raise PortalEvidenceError("stored portal replacement no longer matches exactly one prior row")
            old = parse_portal_row(result[matches[0]])
            if old and old["metadata"]:
                raise PortalEvidenceError("stored portal replacement would overwrite native cost or dialogue metadata")
            result[matches[0]] = desired
        elif operation == "remove":
            matches = [i for i, line in enumerate(result) if _normalize(line) == _normalize(previous)]
            if len(matches) > 1:
                raise PortalEvidenceError("stored portal removal now matches duplicate rows")
            if matches:
                result.pop(matches[0])
        else:
            raise PortalEvidenceError(f"unsupported persisted portal operation {operation!r}")
    return result


@contextmanager
def _profile_lock(path: Path, timeout: float = 30):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    handle = lock_path.open("a+b")
    set_private_permissions(lock_path, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                try_lock(handle)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for the shared server portal profile lock")
                time.sleep(0.05)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".portals-", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        if os.name == "nt":
            os.close(fd)
            fd = -1
            set_private_permissions(temp_path, 0o600)
            temp_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        else:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                json.dump(value, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temp_path, path)
        set_private_permissions(path, 0o600)
    except Exception:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)
        raise


def _load_store(path: Path, server_id: str) -> dict:
    if not path.is_file():
        return {"schema_version": 1, "server_id": server_id, "corrections": []}
    store = json.loads(path.read_text(encoding="utf-8"))
    if (store.get("schema_version") != 1 or store.get("server_id") != server_id
            or not isinstance(store.get("corrections"), list)):
        raise PortalEvidenceError("server portal profile has an unsupported schema or identity")
    return store


def _remove_obsolete_portal(*, server_id: str, data_home: Path, openkore_tables: Path,
                            table_folders: tuple[str, ...], rathena_root: Path,
                            source_map: str, source_x: int, source_y: int,
                            destination: tuple[str, int, int], profile_name: str,
                            edition: str) -> dict:
    """Retire one explicitly named route only when active scripts lack its standard warp."""
    root = resolve_rathena_source_root(rathena_root)
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", source_map)
            or any(type(value) is not int or not 0 <= value <= 999 for value in (source_x, source_y))):
        raise ValueError("portal source must be a valid map and bounded integer cell coordinates")
    dst_map, dst_x, dst_y = destination
    if (not re.fullmatch(r"[A-Za-z0-9_|@-]+", dst_map)
            or any(type(value) is not int or not 0 <= value <= 999 for value in (dst_x, dst_y))):
        raise ValueError("obsolete portal destination must be a map and bounded integer cell coordinates")
    selected_edition = _edition(root, edition)
    if _matching_warps(root, selected_edition, source_map, source_x, source_y):
        raise PortalEvidenceError("an active standard warp still covers this source; obsolete route was preserved")
    source_digest, source_file_count = _active_source_digest(root, selected_edition)
    baseline, native_folder = select_openkore_portals(openkore_tables, table_folders)
    desired = f"{source_map} {source_x} {source_y} {dst_map} {dst_x} {dst_y}"
    store_path = Path(data_home).expanduser().resolve() / "portal-profiles" / server_id / "corrections.json"
    with _profile_lock(store_path):
        store = _load_store(store_path, server_id)
        if store.get("profile_name") not in (None, profile_name):
            raise PortalEvidenceError("server ID is already bound to a different OpenKore profile")
        store["profile_name"] = profile_name
        current = _apply_records(baseline.read_text(encoding="utf-8", errors="replace").splitlines(),
                                 store["corrections"])
        matches = [line for line in current if _normalize(line) == _normalize(desired)]
        if not matches:
            return {"status": "ALREADY_ABSENT", "server_id": server_id,
                    "portal_row": desired,
                    "evidence_type": "active NPC hierarchy has no standard warp at source",
                    "persistent_profile": str(store_path), "native_table_folder": native_folder}
        if len(matches) != 1:
            raise PortalEvidenceError("duplicate obsolete OpenKore rows are ambiguous; preserving the table")
        parsed = parse_portal_row(matches[0])
        if parsed and parsed["metadata"]:
            raise PortalEvidenceError("obsolete OpenKore entry includes cost or dialogue metadata; preserving it")
        operation_index = len(store["corrections"])
        for record in store["corrections"]:
            old = parse_portal_row(record.get("portal_row") or record.get("previous_row") or "")
            if old and (old["source_map"], old["source_x"], old["source_y"]) == (source_map, source_x, source_y):
                record["superseded_by"] = operation_index
        correction = {
            "operation": "remove", "previous_row": matches[0], "portal_row": None,
            "server_id": server_id, "profile_name": profile_name,
            "reason": "explicit retirement after scanning the active rAthena standard-warp hierarchy",
            "evidence": {"source_map": source_map, "source_x": source_x, "source_y": source_y,
                "destination_map": dst_map, "destination_x": dst_x, "destination_y": dst_y,
                "edition": selected_edition, "source_files_sha256": source_digest,
                "active_source_file_count": source_file_count,
                "evidence_type": "active NPC import hierarchy; no standard fixed warp covers source",
                "runtime_confirmation": "not available from source inspection"},
            "openkore_table": str(baseline),
            "openkore_table_sha256_before": hashlib.sha256(baseline.read_bytes()).hexdigest(),
            "native_table_folder": native_folder,
        }
        if len(_apply_records(current, [correction])) != len(current) - 1:
            raise PortalEvidenceError("obsolete portal removal failed exact-row validation")
        store["corrections"].append(correction)
        _atomic_json(store_path, store)
        return {"status": "CORRECTED", "operation": "remove", "portal_row": desired,
                "evidence": correction["evidence"], "persistent_profile": str(store_path),
                "native_table_folder": native_folder}


def reconcile_portal(*, server_id: str, data_home: Path, openkore_tables: Path,
                     table_folders: tuple[str, ...], rathena_root: Path,
                     source_map: str, source_x: int, source_y: int,
                     profile_name: str, edition: str = "auto",
                     obsolete_destination: tuple[str, int, int] | None = None) -> dict:
    """Persist one unambiguous correction against active standard warp evidence."""
    server_id = _safe_server_id(server_id)
    if obsolete_destination is not None:
        return _remove_obsolete_portal(server_id=server_id, data_home=data_home,
            openkore_tables=openkore_tables, table_folders=table_folders,
            rathena_root=rathena_root, source_map=source_map, source_x=source_x,
            source_y=source_y, destination=obsolete_destination,
            profile_name=profile_name, edition=edition)
    warp = resolve_static_portal(rathena_root, source_map, source_x, source_y, edition=edition)
    baseline, native_folder = select_openkore_portals(openkore_tables, table_folders)
    profile = Path(data_home).expanduser().resolve() / "portal-profiles" / server_id
    store_path = profile / "corrections.json"
    wanted = (f"{warp['source_map']} {warp['source_x']} {warp['source_y']} "
              f"{warp['destination_map']} {warp['destination_x']} {warp['destination_y']}")
    with _profile_lock(store_path):
        store = _load_store(store_path, server_id)
        if store.get("profile_name") not in (None, profile_name):
            raise PortalEvidenceError("server ID is already bound to a different OpenKore profile")
        store["profile_name"] = profile_name
        current_lines = _apply_records(baseline.read_text(encoding="utf-8", errors="replace").splitlines(),
                                       store["corrections"])
        exact = [line for line in current_lines if _normalize(line) == _normalize(wanted)]
        if exact:
            _atomic_json(store_path, store)
            return {"status": "ALREADY_CORRECT", "server_id": server_id,
                    "previous_rows": [exact[0]], "portal_row": wanted, "evidence": warp,
                    "persistent_profile": str(store_path), "native_table_folder": native_folder}
        region = lambda row: (row["source_map"].casefold() == warp["source_map"].casefold()
            and abs(row["source_x"] - warp["source_x"]) <= warp["trigger_width"]
            and abs(row["source_y"] - warp["source_y"]) <= warp["trigger_height"])
        exact_source = [line for line in current_lines if (parsed := parse_portal_row(line))
                        and parsed["source_map"].casefold() == warp["source_map"].casefold()
                        and parsed["source_x"] == warp["source_x"]
                        and parsed["source_y"] == warp["source_y"]]
        # Prefer a canonical table key; only use the NPC trigger rectangle to
        # find a stale source coordinate when no exact-key route exists. A
        # source can have multiple OpenKore destinations; select only the row
        # whose destination map matches the authoritative NPC definition.
        source_rows = exact_source or [line for line in current_lines
            if (parsed := parse_portal_row(line)) and region(parsed)]
        server_map_rows = [line for line in source_rows
            if (parsed := parse_portal_row(line))
            and parsed["destination_map"].casefold() == warp["destination_map"].casefold()]
        if len(server_map_rows) == 1:
            nearby = server_map_rows
        elif len(server_map_rows) > 1:
            nearby = server_map_rows
        elif len(source_rows) == 1:
            nearby = source_rows
        else:
            nearby = source_rows
        if len(nearby) > 1:
            raise PortalEvidenceError("multiple OpenKore routes overlap this server warp; preserving them unchanged")
        operation = "replace" if nearby else "add"
        previous = nearby[0] if nearby else None
        parsed_previous = parse_portal_row(previous) if previous else None
        if parsed_previous and parsed_previous["metadata"]:
            raise PortalEvidenceError("OpenKore entry includes cost or dialogue metadata; refusing to replace it")
        correction = {
            "operation": operation, "previous_row": previous, "portal_row": wanted,
            "server_id": server_id, "profile_name": profile_name,
            "reason": "targeted comparison with one active standard rAthena warp",
            "evidence": warp, "openkore_table": str(baseline),
            "openkore_table_sha256_before": hashlib.sha256(baseline.read_bytes()).hexdigest(),
            "native_table_folder": native_folder,
        }
        # Check the exact edit against a private in-memory copy before making it durable.
        trial = _apply_records(current_lines, [correction])
        if sum(_normalize(line) == _normalize(wanted) for line in trial) != 1:
            raise PortalEvidenceError("the proposed portal table edit failed its exact-row validation")
        next_index = len(store["corrections"])
        for record in store["corrections"]:
            if record.get("operation") != "remove":
                continue
            evidence = record.get("evidence", {})
            if (evidence.get("source_map", "").casefold() == warp["source_map"].casefold()
                    and evidence.get("source_x") == warp["source_x"]
                    and evidence.get("source_y") == warp["source_y"]):
                record["superseded_by"] = next_index
        store["corrections"].append(correction)
        _atomic_json(store_path, store)
        return {"status": "CORRECTED", "server_id": server_id,
                "operation": operation, "previous_rows": [previous] if previous else [],
                "portal_row": wanted, "evidence": warp,
                "persistent_profile": str(store_path), "native_table_folder": native_folder,
                "openkore_table_sha256_before": correction["openkore_table_sha256_before"]}


def materialize_portal_profile(*, server_id: str, data_home: Path,
                               openkore_tables: Path, table_folders: tuple[str, ...],
                               rathena_root: Path, destination: Path, profile_name: str | None = None,
                               edition: str = "auto") -> dict:
    """Build the effective run-local table; shared OpenKore tables stay read-only."""
    server_id = _safe_server_id(server_id)
    baseline, native_folder = select_openkore_portals(openkore_tables, table_folders)
    store_path = Path(data_home).expanduser().resolve() / "portal-profiles" / server_id / "corrections.json"
    with _profile_lock(store_path):
        store = _load_store(store_path, server_id)
        if profile_name and store.get("profile_name") not in (None, profile_name):
            raise PortalEvidenceError("server portal corrections belong to a different OpenKore profile")
        for correction in store["corrections"]:
            evidence = correction["evidence"]
            if correction.get("superseded_by") is not None:
                continue
            if correction.get("operation") == "remove":
                root = resolve_rathena_source_root(rathena_root)
                if _matching_warps(root, evidence["edition"], evidence["source_map"],
                                   evidence["source_x"], evidence["source_y"]):
                    raise PortalEvidenceError(
                        "an active standard warp now covers a retired portal source; review required")
                source_digest, _count = _active_source_digest(root, evidence["edition"])
                if source_digest != evidence.get("source_files_sha256"):
                    raise PortalEvidenceError("active rAthena NPC sources changed after portal retirement; review required")
                continue
            current = resolve_static_portal(rathena_root, evidence["source_map"],
                evidence["source_x"], evidence["source_y"], edition=evidence["edition"])
            for key in ("npc_name", "source_file", "source_text", "source_sha256",
                        "destination_map", "destination_x", "destination_y"):
                if current.get(key) != evidence.get(key):
                    raise PortalEvidenceError(
                        f"server portal evidence changed since reconciliation ({key}); review required")
        rows = _apply_records(baseline.read_text(encoding="utf-8", errors="replace").splitlines(),
                              store["corrections"])
        target = destination.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write(target, "\n".join(rows) + "\n")
        return {"server_id": server_id, "source_table": str(baseline),
                "source_table_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
                "effective_table": str(target),
                "effective_table_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "correction_count": len(store["corrections"]),
                "native_table_folder": native_folder,
                "runtime_status": "table_materialized; OpenKore load must be observed by the caller"}


def portal_los_fingerprint(*, portals_file: Path, openkore_tables: Path,
                           table_folders: tuple[str, ...], fields_folder: Path | None = None,
                           field_overrides: dict[str, str] | None = None,
                           openkore_source: Path | None = None) -> dict:
    """Fingerprint the effective graph and the field/source inputs used by OpenKore."""
    components: dict[str, str | None] = {}
    canonical_rows = []
    maps: set[str] = set()
    for raw in portals_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        canonical_rows.append(" ".join(line.split()))
        row = parse_portal_row(raw)
        if row:
            maps.update((row["source_map"], row["destination_map"]))
    graph = "\n".join(sorted(canonical_rows)).encode()
    components["portals.txt"] = hashlib.sha256(graph).hexdigest()

    tables_root = openkore_tables.expanduser().resolve()
    for filename in ("portals_commands.txt", "portals_spawns.txt", "portals_airship.txt",
                     "teleport_items.txt", "resnametable.txt"):
        candidate = next((tables_root / folder / filename for folder in table_folders
                          if (tables_root / folder / filename).is_file()), None)
        candidate = candidate or (tables_root / filename)
        components[f"table:{filename}"] = hashlib.sha256(candidate.read_bytes()).hexdigest() \
            if candidate.is_file() else None

    root = fields_folder.expanduser().resolve() if fields_folder else tables_root.parent / "fields"
    overrides = field_overrides or {}
    for map_name in sorted(maps):
        field_name = overrides.get(map_name, map_name)
        candidates = (root / f"{field_name}.fld2", root / f"{field_name}.fld2.gz")
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        components[f"field:{map_name}"] = hashlib.sha256(path.read_bytes()).hexdigest() if path else None

    if openkore_source:
        for relative in ("src/Misc.pm", "src/Field.pm", "src/PathFinding.pm"):
            path = openkore_source / relative
            components[f"source:{relative}"] = hashlib.sha256(path.read_bytes()).hexdigest() \
                if path.is_file() else None
    payload = json.dumps(components, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(payload).hexdigest(), "components": components}


def load_portal_los_cache(*, server_id: str, data_home: Path,
                          fingerprint: str) -> str | None:
    """Return only a complete native cache matching the current routing inputs."""
    server_id = _safe_server_id(server_id)
    profile = Path(data_home).expanduser().resolve() / "portal-profiles" / server_id
    lock_path = profile / "corrections.json"
    with _profile_lock(lock_path):
        _load_store(lock_path, server_id)
        cache_path = profile / "portalsLOS-cache.json"
        if not cache_path.is_file():
            return None
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        content = cache.get("content")
        if (cache.get("schema_version") != 1 or cache.get("fingerprint") != fingerprint
                or not isinstance(content, str)
                or hashlib.sha256(content.encode()).hexdigest() != cache.get("content_sha256")
                or not _valid_portals_los(content)):
            return None
        return content


def store_portal_los_cache(*, server_id: str, data_home: Path,
                           fingerprint: str, content: str) -> dict:
    """Atomically retain a compiler-confirmed cache; invalid output leaves the old cache intact."""
    server_id = _safe_server_id(server_id)
    if not _valid_portals_los(content):
        raise PortalEvidenceError("OpenKore generated an invalid portalsLOS table; previous cache was preserved")
    profile = Path(data_home).expanduser().resolve() / "portal-profiles" / server_id
    lock_path = profile / "corrections.json"
    with _profile_lock(lock_path):
        _load_store(lock_path, server_id)
        cache_path = profile / "portalsLOS-cache.json"
        cache = {"schema_version": 1, "server_id": server_id,
                 "fingerprint": fingerprint, "content": content,
                 "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                 "updated_at": int(time.time())}
        _atomic_json(cache_path, cache)
    return {"status": "rebuilt", "fingerprint": fingerprint,
            "content_sha256": cache["content_sha256"], "rows": len(content.splitlines())}


def _valid_portals_los(content: str) -> bool:
    if not isinstance(content, str):
        return False
    for line in content.splitlines():
        fields = line.split()
        if len(fields) < 7 or (len(fields) - 3) % 4:
            return False
        if (not re.fullmatch(_MAP, fields[0]) or not fields[1].isdigit()
                or not fields[2].isdigit() or int(fields[1]) > 999 or int(fields[2]) > 999):
            return False
        for index in range(3, len(fields), 4):
            if (not re.fullmatch(_MAP, fields[index]) or not fields[index + 1].isdigit()
                    or not fields[index + 2].isdigit() or not fields[index + 3].isdigit()
                    or int(fields[index + 1]) > 999 or int(fields[index + 2]) > 999):
                return False
    return True


def _atomic_write(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".portals-", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        if os.name == "nt":
            os.close(fd)
            fd = -1
            set_private_permissions(temp_path, 0o600)
            temp_path.write_text(content, encoding="utf-8")
        else:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temp_path, path)
        set_private_permissions(path, 0o600)
    except Exception:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)
        raise
