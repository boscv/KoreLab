from __future__ import annotations

import json
from pathlib import Path

import pytest

from korelab.fleet import (ConcurrentFleet, _latest_state, _unconfirmed_combat_agents,
                           _launch_agent_with_retries, _native_fleet_openkore_settings,
                           _recovery_targets)


class ObserverFixture:
    def __init__(self, path: Path):
        self.observer_file = path

    def _observer_rows(self):
        return [json.loads(line) for line in self.observer_file.read_text().splitlines()]


def test_fleet_uses_latest_structured_state_not_old_connected_event(tmp_path: Path) -> None:
    observer = tmp_path / "observer.jsonl"
    observer.write_text(
        '{"type":"state","connected_in_game":true,"map":"prt_fild08"}\n'
        '{"type":"state","connected_in_game":false,"map":"prt_fild08"}\n',
        encoding="utf-8")

    assert _latest_state(ObserverFixture(observer))["connected_in_game"] is False


def test_combat_retry_selects_only_agents_without_observed_combat() -> None:
    agents = [{"id": "agent-001"}, {"id": "agent-002"}, {"id": "agent-003"}]
    outcomes = {
        "agent-001": {"combat_confirmed": True},
        "agent-002": {"combat_confirmed": False},
        "agent-003": {"combat_confirmed": True},
    }

    assert [agent["id"] for agent in _unconfirmed_combat_agents(agents, outcomes)] == [
        "agent-002"]


def test_single_disconnect_recovery_targets_only_the_failed_profile() -> None:
    agents = [{"id": f"agent-{number:03d}"} for number in range(1, 6)]
    running = {agent["id"]: True for agent in agents}
    restarted = []

    def recover(agent: dict) -> None:
        restarted.append(agent["id"])
        running[agent["id"]] = True

    failed = _recovery_targets(agents, {"agent-003"})
    for agent in failed:
        running[agent["id"]] = False
        recover(agent)

    assert restarted == ["agent-003"]
    assert all(running.values())
    assert [agent["id"] for agent in _recovery_targets(agents, {"agent-003"})] == [
        "agent-003"]


def test_recovery_target_selection_rejects_an_unknown_identity() -> None:
    with pytest.raises(RuntimeError, match="unknown agent identity"):
        _recovery_targets([{"id": "agent-001"}], {"agent-missing"})


def test_failed_profile_launch_stops_at_the_configured_retry_limit() -> None:
    attempts = []
    failures = []
    waits = []

    def launch_failed_profile():
        attempts.append("agent-003")
        raise ConnectionError("unavailable")

    with pytest.raises(RuntimeError, match="agent-003 failed after 3 bounded attempts"):
        _launch_agent_with_retries("agent-003", launch_failed_profile,
            lambda: failures.append("agent-003"), attempts=3, wait=waits.append)

    assert attempts == ["agent-003"] * 3
    assert failures == ["agent-003"] * 3
    assert waits == [1, 2]


def test_fleet_rejects_invalid_population_before_creating_a_lab(tmp_path: Path) -> None:
    fleet = ConcurrentFleet(tmp_path / "isolated")
    with pytest.raises(ValueError, match="between 1 and 100"):
        fleet.run(openkore=tmp_path, rathena_runtime=tmp_path,
            xstools_overlay=tmp_path, count=101, lock_map="prt_fild08",
            start_point={"map": "prt_fild08", "x": 20, "y": 239})
    assert not fleet.lab.root.exists()


def test_native_fleet_profile_delegates_lock_and_combat_to_openkore_ai() -> None:
    profile = _native_fleet_openkore_settings("prt_fild08", "bot001")
    settings = profile["config_overrides"]
    assert profile["native_profile"] == "bot001"
    assert profile["ai_mode"] == "on"
    assert settings["lockMap"] == "prt_fild08"
    assert settings["attackAuto"] == 2
    assert settings["attackAuto_routeToLock"] == 1
    assert settings["attackAuto_outOfLock"] == 0
    assert settings["attackDistance"] == settings["attackDistanceAuto"] == 1
    assert settings["attackMaxDistance"] == settings["attackUseWeapon"] == 1
    assert settings["attackCheckLOS"] == 1
    assert settings["attackRouteMaxPathDistance"] == 20
    assert settings["route_randomWalk"] == settings["route_randomWalk_inLockOnly"] == 1
    assert settings["route_step"] == 10
    assert settings["portalCompile"] == 1
    assert "action" not in profile
    assert "_capture_raw_command" not in ConcurrentFleet.run.__code__.co_names
    contents = profile["control_file_overrides"]["mon_control.txt"]["content"]
    assert contents.splitlines() == [
        "Poring 1 0 0 0 0 0 0 0 1", "Lunatic 1 0 0 0 0 0 0 0 1",
        "Fabre 1 0 0 0 0 0 0 0 1", "Little Poring 1 0 0 0 0 0 0 0 1"]
