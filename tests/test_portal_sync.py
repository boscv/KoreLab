from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from korelab.portal_sync import (PortalEvidenceError, materialize_portal_profile,
                                 reconcile_portal, resolve_static_portal)


def source_tree(root: Path, *, include_candidate: bool = True, hidden: bool = False) -> Path:
    (root / "src/config").mkdir(parents=True, exist_ok=True)
    (root / "src/config/renewal.hpp").write_text("#define RENEWAL\n")
    scripts = root / "npc/re"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "scripts_main.conf").write_text("import: npc/re/warps.conf\n")
    (scripts / "warps.conf").write_text("npc: npc/re/active.txt\n")
    lines = []
    if include_candidate:
        lines.append("prt_fild07,383,239,0\twarp\tprtf14\t3,20,prt_fild08,20,239")
    if hidden:
        lines.append('disablenpc "prtf14";')
    (scripts / "active.txt").write_text("\n".join(lines) + "\n")
    (scripts / "not-imported.txt").write_text(
        "prt_fild07,100,100,0\twarp\tinactive\t1,1,prt_fild08,1,1\n")
    return root


def openkore_tables(root: Path, content: str) -> Path:
    tables = root / "tables"
    (tables / "iRO").mkdir(parents=True, exist_ok=True)
    (tables / "iRO/portals.txt").write_text(content)
    return tables


def reconcile(tmp_path: Path, *, source: Path | None = None, table_text: str | None = None,
              server_id: str = "lab-test", source_map: str = "prt_fild07",
              source_x: int = 383, source_y: int = 239):
    source = source or source_tree(tmp_path / "rathena")
    tables = openkore_tables(tmp_path / "openkore", table_text if table_text is not None else
        "# maintained comment\nprt_fild07 383 239 prt_fild08 16 239\n"
        "alberta 30 30 izlude 100 100\n")
    return reconcile_portal(server_id=server_id, profile_name="Disposable",
        data_home=tmp_path / "data", openkore_tables=tables, table_folders=("iRO",),
        rathena_root=source, source_map=source_map, source_x=source_x, source_y=source_y)


def test_replaces_wrong_destination_and_materializes_run_local_table(tmp_path: Path):
    result = reconcile(tmp_path)
    assert result["status"] == "CORRECTED"
    assert result["operation"] == "replace"
    assert result["previous_rows"] == ["prt_fild07 383 239 prt_fild08 16 239"]
    assert result["portal_row"] == "prt_fild07 383 239 prt_fild08 20 239"
    assert result["evidence"]["source_file"] == "npc/re/active.txt"
    assert result["evidence"]["runtime_status"] == "not_independently_observed"

    output = tmp_path / "runs/managed-server-tables/iRO/portals.txt"
    materialized = materialize_portal_profile(server_id="lab-test", data_home=tmp_path / "data",
        openkore_tables=tmp_path / "openkore/tables", table_folders=("iRO",),
        rathena_root=tmp_path / "rathena", destination=output)
    rows = output.read_text().splitlines()
    assert "# maintained comment" in rows
    assert "prt_fild07 383 239 prt_fild08 20 239" in rows
    assert "prt_fild07 383 239 prt_fild08 16 239" not in rows
    assert "alberta 30 30 izlude 100 100" in rows
    assert materialized["correction_count"] == 1
    assert materialized["native_table_folder"] == "iRO"
    assert not (tmp_path / "openkore/tables/iRO/portals.txt").read_text().endswith("20 239\n")


def test_missing_portal_is_added_without_inventing_reverse_route(tmp_path: Path):
    result = reconcile(tmp_path, table_text="# baseline\nalberta 30 30 izlude 100 100\n")
    assert result["operation"] == "add"
    assert result["previous_rows"] == []
    table = tmp_path / "data/portal-profiles/lab-test/corrections.json"
    stored = json.loads(table.read_text())
    assert stored["corrections"][0]["portal_row"] == "prt_fild07 383 239 prt_fild08 20 239"
    assert all(not row.get("portal_row", "").startswith("prt_fild08 20 239")
               for row in stored["corrections"])


def test_wrong_destination_map_is_corrected_without_touching_other_routes(tmp_path: Path):
    text = ("prt_fild07 383 239 old_server_map 1 2\n"
            "alberta 30 30 izlude 100 100\n")
    result = reconcile(tmp_path, table_text=text)
    assert result["status"] == "CORRECTED"
    assert result["previous_rows"] == ["prt_fild07 383 239 old_server_map 1 2"]
    output = tmp_path / "runs/managed-server-tables/iRO/portals.txt"
    materialize_portal_profile(server_id="lab-test", data_home=tmp_path / "data",
        openkore_tables=tmp_path / "openkore/tables", table_folders=("iRO",),
        rathena_root=tmp_path / "rathena", destination=output)
    assert "prt_fild07 383 239 prt_fild08 20 239" in output.read_text().splitlines()
    assert "alberta 30 30 izlude 100 100" in output.read_text().splitlines()


def test_reconciliation_is_idempotent(tmp_path: Path):
    first = reconcile(tmp_path)
    second = reconcile(tmp_path)
    store = json.loads((tmp_path / "data/portal-profiles/lab-test/corrections.json").read_text())
    assert first["status"] == "CORRECTED"
    assert second["status"] == "ALREADY_CORRECT"
    assert len(store["corrections"]) == 1


def test_ambiguous_routes_are_preserved_without_a_store_update(tmp_path: Path):
    text = ("prt_fild07 383 239 prt_fild08 16 239\n"
            "prt_fild07 383 239 prt_fild08 18 239\n")
    with pytest.raises(PortalEvidenceError, match="multiple OpenKore routes overlap"):
        reconcile(tmp_path, table_text=text)
    assert not (tmp_path / "data/portal-profiles/lab-test/corrections.json").exists()


def test_portal_metadata_is_not_overwritten(tmp_path: Path):
    with pytest.raises(PortalEvidenceError, match="cost or dialogue metadata"):
        reconcile(tmp_path, table_text="prt_fild07 383 239 prt_fild08 16 239 500 0 c c\n")
    assert not (tmp_path / "data/portal-profiles/lab-test/corrections.json").exists()


def test_obsolete_standard_portal_is_removed_only_from_targeted_route(tmp_path: Path):
    source = source_tree(tmp_path / "rathena", include_candidate=False)
    tables = openkore_tables(tmp_path / "openkore",
        "# keep comment\nprt_fild07 383 239 prt_fild08 20 239\n"
        "prt_fild07 383 239 prt_fild08a 16 239\nalberta 30 30 izlude 100 100\n")
    kwargs = dict(server_id="lab-test", profile_name="Disposable", data_home=tmp_path / "data",
        openkore_tables=tables, table_folders=("iRO",), rathena_root=source,
        source_map="prt_fild07", source_x=383, source_y=239,
        obsolete_destination=("prt_fild08", 20, 239))
    result = reconcile_portal(**kwargs)
    assert result["operation"] == "remove"
    assert reconcile_portal(**kwargs)["status"] == "ALREADY_ABSENT"
    output = tmp_path / "runs/iRO/portals.txt"
    materialize_portal_profile(server_id="lab-test", data_home=tmp_path / "data",
        openkore_tables=tables, table_folders=("iRO",), rathena_root=source,
        destination=output, profile_name="Disposable")
    rows = output.read_text().splitlines()
    assert "# keep comment" in rows
    assert "prt_fild07 383 239 prt_fild08 20 239" not in rows
    assert "prt_fild07 383 239 prt_fild08a 16 239" in rows
    assert "alberta 30 30 izlude 100 100" in rows


def test_obsolete_portal_removal_refuses_a_still_active_server_warp(tmp_path: Path):
    source = source_tree(tmp_path / "rathena")
    tables = openkore_tables(tmp_path / "openkore", "prt_fild07 383 239 prt_fild08 20 239\n")
    with pytest.raises(PortalEvidenceError, match="active standard warp still covers"):
        reconcile_portal(server_id="lab-test", profile_name="Disposable", data_home=tmp_path / "data",
            openkore_tables=tables, table_folders=("iRO",), rathena_root=source,
            source_map="prt_fild07", source_x=383, source_y=239,
            obsolete_destination=("prt_fild08", 20, 239))


def test_obsolete_portal_removal_requires_review_after_active_sources_change(tmp_path: Path):
    source = source_tree(tmp_path / "rathena", include_candidate=False)
    tables = openkore_tables(tmp_path / "openkore", "prt_fild07 383 239 prt_fild08 20 239\n")
    reconcile_portal(server_id="lab-test", profile_name="Disposable", data_home=tmp_path / "data",
        openkore_tables=tables, table_folders=("iRO",), rathena_root=source,
        source_map="prt_fild07", source_x=383, source_y=239,
        obsolete_destination=("prt_fild08", 20, 239))
    (source / "npc/re/active.txt").write_text("// reviewed source changed\n")
    with pytest.raises(PortalEvidenceError, match="sources changed"):
        materialize_portal_profile(server_id="lab-test", data_home=tmp_path / "data",
            openkore_tables=tables, table_folders=("iRO",), rathena_root=source,
            destination=tmp_path / "runs/iRO/portals.txt", profile_name="Disposable")


def test_active_import_hierarchy_excludes_unloaded_warp_files(tmp_path: Path):
    source = source_tree(tmp_path / "rathena", include_candidate=False)
    with pytest.raises(PortalEvidenceError, match="found 0"):
        resolve_static_portal(source, "prt_fild07", 100, 100)


def test_script_visibility_control_makes_warp_ambiguous(tmp_path: Path):
    source = source_tree(tmp_path / "rathena", hidden=True)
    with pytest.raises(PortalEvidenceError, match="visibility controls"):
        resolve_static_portal(source, "prt_fild07", 383, 239)


def test_map_alias_is_not_used_to_guess_server_map_name(tmp_path: Path):
    source = source_tree(tmp_path / "rathena")
    with pytest.raises(PortalEvidenceError, match="found 0"):
        resolve_static_portal(source, "prt_fild07_alias", 383, 239)


def test_concurrent_reconciliation_serializes_and_adds_once(tmp_path: Path):
    source = source_tree(tmp_path / "rathena")
    tables = openkore_tables(tmp_path / "openkore", "# empty\n")
    def run_one(_index: int):
        return reconcile_portal(server_id="lab-test", profile_name="Disposable",
            data_home=tmp_path / "data", openkore_tables=tables, table_folders=("iRO",),
            rathena_root=source, source_map="prt_fild07", source_x=383, source_y=239)["status"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(run_one, range(6)))
    assert statuses.count("CORRECTED") == 1
    assert statuses.count("ALREADY_CORRECT") == 5
    saved = json.loads((tmp_path / "data/portal-profiles/lab-test/corrections.json").read_text())
    assert len(saved["corrections"]) == 1


def test_source_change_invalidates_persistent_correction(tmp_path: Path):
    source = source_tree(tmp_path / "rathena")
    reconcile(tmp_path, source=source)
    active = source / "npc/re/active.txt"
    active.write_text("prt_fild07,383,239,0\twarp\tprtf14\t3,20,prt_fild08,21,239\n")
    with pytest.raises(PortalEvidenceError, match="evidence changed"):
        materialize_portal_profile(server_id="lab-test", data_home=tmp_path / "data",
            openkore_tables=tmp_path / "openkore/tables", table_folders=("iRO",),
            rathena_root=source, destination=tmp_path / "runs/iRO/portals.txt")


@pytest.mark.source_compatibility
def test_installed_rathena_and_openkore_have_a_targetable_warp_discrepancy():
    rathena_value = os.environ.get("KORELAB_RATHENA")
    openkore_value = os.environ.get("KORELAB_OPENKORE")
    if not rathena_value or not openkore_value:
        pytest.skip("set KORELAB_RATHENA and KORELAB_OPENKORE for installed-source compatibility")
    rathena = Path(rathena_value).expanduser()
    tables = Path(openkore_value).expanduser() / "tables"
    if not (rathena / "npc/re/scripts_main.conf").is_file() or not (tables / "iRO/portals.txt").is_file():
        pytest.skip("local OpenKore/rAthena source compatibility fixture is unavailable")
    warp = resolve_static_portal(rathena, "prt_fild07", 383, 239)
    rows = (tables / "iRO/portals.txt").read_text().splitlines()
    assert warp["destination_map"] == "prt_fild08"
    assert (warp["destination_x"], warp["destination_y"]) == (20, 239)
    assert "prt_fild07 383 239 prt_fild08 16 239" in rows


def test_native_los_cache_is_staged_privately_for_openkore_refresh(tmp_path: Path):
    from korelab.server_tables import stage_portal_los_cache

    tables = tmp_path / "openkore/tables"
    tables.mkdir(parents=True)
    upstream = tables / "portalsLOS.txt"
    upstream.write_text("# existing compiled routes\nprt_fild07 383 239 prt_fild08 9\n")
    overlay = tmp_path / "run/managed-server-tables"
    effective = stage_portal_los_cache(tables, overlay, "iRO")

    assert effective == overlay / "iRO/portalsLOS.txt"
    assert effective.read_bytes() == upstream.read_bytes()
    effective.write_text(effective.read_text() + "prt_fild07 383 239 prt_fild08 10\n")
    assert upstream.read_text() == "# existing compiled routes\nprt_fild07 383 239 prt_fild08 9\n"


def test_empty_inventory_selection_is_valid_for_location_only_verification():
    from korelab.adapters.rathena import RathenaAdapter

    adapter = RathenaAdapter.__new__(RathenaAdapter)
    adapter._read_inventory_rows = lambda *args: (_ for _ in ()).throw(AssertionError("unexpected SQL read"))
    assert adapter.inventory_rows({"char_id": 123}, ()) == []


def _routing_fingerprint(tmp_path: Path, portal_text: str):
    from korelab.portal_sync import portal_los_fingerprint

    tables = tmp_path / "openkore/tables"
    (tables / "iRO").mkdir(parents=True, exist_ok=True)
    portal_file = tables / "iRO/portals.txt"
    portal_file.write_text(portal_text)
    fields = tmp_path / "openkore/fields"
    fields.mkdir(parents=True, exist_ok=True)
    for map_name in ("prt_fild07", "prt_fild08", "alberta", "izlude"):
        (fields / f"{map_name}.fld2.gz").write_bytes(map_name.encode())
    return portal_los_fingerprint(portals_file=portal_file, openkore_tables=tables,
        table_folders=("iRO",), fields_folder=fields)


def test_portal_coordinate_addition_and_removal_invalidate_cached_los(tmp_path: Path):
    from korelab.portal_sync import load_portal_los_cache, store_portal_los_cache

    baseline = "prt_fild07 383 239 prt_fild08 16 239\n"
    first = _routing_fingerprint(tmp_path, baseline)["sha256"]
    content = "prt_fild08 16 239 prt_fild07 383 239 12\n"
    store_portal_los_cache(server_id="lab-test", data_home=tmp_path / "data",
                           fingerprint=first, content=content)
    assert load_portal_los_cache(server_id="lab-test", data_home=tmp_path / "data",
                                 fingerprint=first) == content

    for changed in (
        "prt_fild07 384 239 prt_fild08 16 239\n",  # changed source coordinate
        "prt_fild07 383 239 prt_fild08 20 239\n",  # changed destination coordinate
        baseline + "alberta 30 30 izlude 100 100\n",  # newly discovered route
        "",  # removed obsolete route
    ):
        changed_fingerprint = _routing_fingerprint(tmp_path, changed)["sha256"]
        assert changed_fingerprint != first
        assert load_portal_los_cache(server_id="lab-test", data_home=tmp_path / "data",
                                     fingerprint=changed_fingerprint) is None


def test_stale_los_values_are_not_reused_when_graph_changes_even_if_pairs_exist(tmp_path: Path):
    from korelab.portal_sync import (load_portal_los_cache, portal_los_fingerprint,
                                     store_portal_los_cache)
    from korelab.server_tables import stage_portal_los_cache

    old_table = "prt_fild07 383 239 prt_fild08 16 239\n"
    old_fingerprint = _routing_fingerprint(tmp_path, old_table)["sha256"]
    stale = "prt_fild08 16 239 prt_fild07 383 239 1\n"
    store_portal_los_cache(server_id="lab-test", data_home=tmp_path / "data",
                           fingerprint=old_fingerprint, content=stale)
    tables = tmp_path / "openkore/tables"
    effective = tables / "iRO/portals.txt"
    effective.write_text("prt_fild07 383 239 prt_fild08 20 239\n")
    new_fingerprint = portal_los_fingerprint(portals_file=effective, openkore_tables=tables,
        table_folders=("iRO",), fields_folder=tmp_path / "openkore/fields")["sha256"]
    assert load_portal_los_cache(server_id="lab-test", data_home=tmp_path / "data",
                                 fingerprint=new_fingerprint) is None
    staged = stage_portal_los_cache(tables, tmp_path / "run", "iRO", content=b"")
    assert staged.read_bytes() == b""


def test_matching_los_cache_is_reused_without_rebuild_and_bad_build_preserves_it(tmp_path: Path):
    from korelab.portal_sync import (PortalEvidenceError, load_portal_los_cache,
                                     store_portal_los_cache)

    fingerprint = _routing_fingerprint(tmp_path, "prt_fild07 383 239 prt_fild08 20 239\n")["sha256"]
    good = "prt_fild08 20 239 prt_fild07 383 239 11\n"
    first = store_portal_los_cache(server_id="lab-test", data_home=tmp_path / "data",
                                   fingerprint=fingerprint, content=good)
    assert first["status"] == "rebuilt"
    assert load_portal_los_cache(server_id="lab-test", data_home=tmp_path / "data",
                                 fingerprint=fingerprint) == good
    with pytest.raises(PortalEvidenceError, match="previous cache was preserved"):
        store_portal_los_cache(server_id="lab-test", data_home=tmp_path / "data",
                               fingerprint=fingerprint, content="broken stale row\n")
    assert load_portal_los_cache(server_id="lab-test", data_home=tmp_path / "data",
                                 fingerprint=fingerprint) == good
