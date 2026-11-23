"""Helpers for isolated native OpenKore table overlays."""
from __future__ import annotations

import os
from pathlib import Path


def native_table_search_path(overlay: Path, upstream: Path,
                            server_table_folders: tuple[str, ...], *,
                            server_list_overlay: bool = False) -> str:
    """Build OpenKore's table search path for a private run overlay.

    Without a server-list override, profile folders and the upstream table
    root remain first. An effective ``servers.txt`` must be first so OpenKore
    selects that block, while the overlay also stays last for native
    ``addTableFolders`` resolution.
    """
    overlay = overlay.expanduser().resolve()
    upstream = upstream.expanduser().resolve()
    folders = [str((upstream / folder).resolve()) for folder in server_table_folders]
    if server_list_overlay:
        return os.pathsep.join([str(overlay), *folders, str(upstream), str(overlay)])
    return os.pathsep.join([*folders, str(upstream), str(overlay)])


def stage_portal_los_cache(upstream_tables: Path, output_dir: Path,
                           native_table_folder: str, *, content: bytes | None = None) -> Path:
    """Seed OpenKore's generated LOS cache inside a disposable table overlay."""
    upstream_los = upstream_tables / "portalsLOS.txt"
    effective_los = output_dir / native_table_folder / "portalsLOS.txt"
    effective_los.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if content is None:
        content = upstream_los.read_bytes() if upstream_los.is_file() else b""
    effective_los.write_bytes(content)
    return effective_los
