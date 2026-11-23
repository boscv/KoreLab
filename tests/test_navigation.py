from __future__ import annotations

import gzip
import struct
import zlib
from pathlib import Path

import pytest

from korelab.navigation import select_verified_destination, verify_walkable_cell


def map_fixtures(tmp_path: Path, *, open_cell: int = 1, server_cell: int = 0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fields = tmp_path / "fields"
    fields.mkdir()
    cells = bytes([open_cell] * 25)
    (fields / "arena.fld2.gz").write_bytes(gzip.compress(struct.pack("<HH", 5, 5) + cells))
    db = tmp_path / "rathena/db"
    db.mkdir(parents=True)
    payload = zlib.compress(bytes([server_cell] * 25))
    info = struct.pack("<12s hh I", b"arena", 5, 5, len(payload))
    (db / "map_cache.dat").write_bytes(struct.pack("<IH2x", 28 + len(payload), 1) + info + payload)
    return fields, tmp_path / "rathena"


def test_selects_nearest_cell_confirmed_by_both_native_map_sources(tmp_path: Path) -> None:
    fields, rathena = map_fixtures(tmp_path)

    target = select_verified_destination(map_name="arena", origin={"x": 2, "y": 2},
        openkore_fields=fields, rathena=rathena, max_distance=2)

    assert (target["x"], target["y"]) == (2, 1)
    assert target["rathena_cell_type"] == 0
    assert target["openkore_field_sha256"]
    assert target["rathena_map_cache_sha256"]


def test_refuses_destination_when_native_sources_disagree(tmp_path: Path) -> None:
    fields, rathena = map_fixtures(tmp_path, open_cell=1, server_cell=1)

    with pytest.raises(ValueError, match="no mutually verified walkable destination"):
        select_verified_destination(map_name="arena", origin={"x": 2, "y": 2},
            openkore_fields=fields, rathena=rathena, max_distance=2)


def test_refuses_when_map_dimensions_disagree(tmp_path: Path) -> None:
    fields, rathena = map_fixtures(tmp_path)
    (fields / "arena.fld2.gz").write_bytes(gzip.compress(struct.pack("<HH", 4, 5) + bytes([1] * 20)))

    with pytest.raises(ValueError, match="dimensions disagree"):
        select_verified_destination(map_name="arena", origin={"x": 2, "y": 2},
            openkore_fields=fields, rathena=rathena)


def test_initial_spawn_must_be_walkable_in_both_sources(tmp_path: Path) -> None:
    fields, rathena = map_fixtures(tmp_path)

    verified = verify_walkable_cell(map_name="arena", x=2, y=2,
                                    openkore_fields=fields, rathena=rathena)

    assert verified["evidence"].startswith("initial cell is walkable")
    fields2, rathena2 = map_fixtures(tmp_path / "blocked", open_cell=1, server_cell=1)
    with pytest.raises(ValueError, match="not walkable in both"):
        verify_walkable_cell(map_name="arena", x=2, y=2,
                             openkore_fields=fields2, rathena=rathena2)


def test_runner_resolves_declared_auto_target_and_pins_field_directory(tmp_path: Path) -> None:
    from korelab.runner import _resolve_verified_move_target
    from korelab.adapters.openkore import render_private_server_profile

    fields, rathena = map_fixtures(tmp_path)
    openkore = tmp_path / "openkore"
    openkore.mkdir()
    (openkore / "fields").symlink_to(fields, target_is_directory=True)
    scenario = {"initial_state": {"map": "arena", "position": {"x": 2, "y": 2}},
        "expected": {}, "action": {"type": "move_to_position",
            "target": {"selection": "nearest_verified_walkable", "max_distance": 2}},
        "openkore": {"server_profile": {"name": "KoreLab_test"}}}

    evidence = _resolve_verified_move_target(scenario, openkore, rathena)

    assert evidence and evidence["evidence"].startswith("destination cell")
    assert scenario["action"]["target"] == {"x": 2, "y": 1}
    assert scenario["expected"] == {"map": "arena", "position": {"x": 2, "y": 1}}
    assert scenario["openkore"]["server_profile"]["fields_folder"] == str(fields.resolve())
    assert scenario["openkore"]["server_profile"]["field_overrides"] == {"arena": "arena"}
    rendered = render_private_server_profile({"name": "KoreLab_test", "host": "127.0.0.1",
        "port": 6900, "server_type": "Local", "table_folders": ["iRO"],
        "fields_folder": str(fields), "field_overrides": {"arena": "arena"}})
    assert "field_arena arena\n" in rendered


def test_move_reports_native_field_rejection_without_waiting_for_scenario_timeout() -> None:
    from korelab.adapters.openkore import OpenKoreAdapter

    adapter = object.__new__(OpenKoreAdapter)
    adapter.state = lambda _predicate, timeout: {"connected_in_game": True, "map": "arena"}
    adapter._observer_rows = lambda: [{}]
    adapter.output = ""
    adapter._capture_raw_command = lambda _command: "Coordinates 2 2 are not walkable on the map arena\n"
    adapter.state_after = lambda *_args, **_kwargs: pytest.fail("rejected movement must not wait for observer timeout")

    with pytest.raises(RuntimeError, match="verify the selected server fields_folder"):
        adapter.move("arena", {"x": 2, "y": 2}, 60)


def test_state_wait_classifies_native_route_stuck_before_objective_timeout(tmp_path: Path) -> None:
    import time
    from korelab.adapters.openkore import OpenKoreAdapter

    adapter = object.__new__(OpenKoreAdapter)
    adapter.deadline = time.monotonic() + 10
    adapter.observer_file = tmp_path / "observer.jsonl"
    adapter.observer_file.write_text("")
    adapter.output = ""
    adapter._read = lambda _timeout=0.1: setattr(adapter, "output", adapter.output + "Stuck during route.\n")

    with pytest.raises(RuntimeError, match="native route failure"):
        adapter.state_after(0, lambda _row: False, timeout=9,
                            failure_patterns=(r"Stuck during route",), failure_cursor=0)
