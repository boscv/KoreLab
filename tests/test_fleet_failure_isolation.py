from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

import korelab.fleet as fleet_module
from korelab.fleet import ConcurrentFleet


class FakeProcess:
    next_pid = 1000

    def __init__(self):
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.exit_code = None

    def poll(self):
        return self.exit_code


class FakeLab:
    def __init__(self, root: Path, control: dict):
        self.root = root
        self.control = control
        self.online: dict[int, int] = {}
        self.destroyed = False
        self.stop_calls = 0
        self.destroy_calls = 0
        self.healthy = True
        self.control["lab"] = self

    def create(self, *, openkore, rathena_runtime, xstools_overlay,
               name, start_point, agent_count, combat_observer):
        lab_id = "fake-fleet"
        home = self.root / lab_id
        home.mkdir(parents=True)
        agents = []
        secrets = {"fleet_agents": {}}
        for index in range(1, agent_count + 1):
            agent_id = f"agent-{index:03d}"
            account = f"account-{index:03d}"
            character = f"character-{index:03d}"
            profile_file = f"profiles/{agent_id}.json"
            profile_config = home / "profiles" / agent_id / "config.txt"
            profile_config.parent.mkdir(parents=True)
            profile_config.write_text(
                f"username {account}\npassword placeholder\nloginPinCode placeholder\n",
                encoding="utf-8")
            (home / profile_file).parent.mkdir(parents=True, exist_ok=True)
            (home / profile_file).write_text(json.dumps({
                "profile_config": str(profile_config), "character": character,
                "openkore": str(openkore), "fields_folder": str(openkore / "fields"),
                "server_profile": "fake-local", "server_port": 6900,
                "server_type": "kRO_RagexeRE_2021_11_03",
                "xstools_overlay": str(xstools_overlay),
            }), encoding="utf-8")
            agents.append({"id": agent_id, "account": account,
                "character": character, "profile_file": profile_file})
            secrets["fleet_agents"][agent_id] = {
                "account": account, "password": "private-test-secret", "login_pin": "000000"}
        (home / "owner.json").write_text(json.dumps({
            "agents": agents, "openkore_source": str(openkore), "services": {},
        }), encoding="utf-8")
        (home / "secrets.json").write_text(json.dumps(secrets), encoding="utf-8")
        self.control["home"] = home
        self.control["agents"] = agents
        return {"id": lab_id, "path": str(home)}

    def start(self, _lab_id, timeout=90):
        return {"healthy": self.healthy}

    def bootstrap(self, _lab_id, *, agent_id, timeout=120):
        index = int(agent_id[-3:])
        char_id = 1000 + index
        self.online[char_id] = 0
        return {"char_id": char_id, "class_id": 0, "base_level": 1,
                "position": {"map": "prontera", "x": 10 + index, "y": 20}}

    def inspect(self, _lab_id):
        return {"healthy": self.healthy}

    def stop(self, _lab_id):
        self.stop_calls += 1
        self.control.setdefault("events", []).append(("stop", time.monotonic()))
        return {"stopped": ["map", "char", "login", "database"], "unconfirmed": []}

    def destroy(self, _lab_id):
        self.destroy_calls += 1
        self.destroyed = True
        self.control.setdefault("events", []).append(("destroy", time.monotonic()))
        shutil.rmtree(self.control["home"])
        return {"destroyed": True}


class FakeAdapter:
    def __init__(self, _checkout, scenario, _run_dir, _deadline, _perl):
        self.scenario = scenario
        self.agent_id = scenario["identity"]["username"].replace("account-", "agent-")
        self.control = scenario["_test_control"]
        self.lab = self.control["lab"]
        self.char_id = 1000 + int(self.agent_id[-3:])
        self.generation = self.control.setdefault("generations", {}).get(self.agent_id, 0)
        self.control["generations"][self.agent_id] = self.generation + 1
        self.process = FakeProcess()
        self.output = ""
        self.sensitive_values = ("private-test-secret", "000000")
        self.closed = False
        self.disconnect_reported = False
        self.observer_reads = 0
        self.observer_rows = []
        self.control.setdefault("adapters", []).append(self)

    def start(self):
        if (self.control.get("fail_recovery") and self.agent_id == "agent-003"
                and self.generation > 0):
            self.control["failed_recovery_attempts"] = (
                self.control.get("failed_recovery_attempts", 0) + 1)
            if self.control["failed_recovery_attempts"] == 3:
                self.control["terminal_failure_at"] = time.monotonic()
            raise RuntimeError("fixture session unavailable")
        self.lab.online[self.char_id] = 1

    def state(self, _predicate, timeout=120):
        return self._state(connected=True)

    def _state(self, *, connected):
        return {"type": "state", "connected_in_game": connected,
            "map": "prt_fild08", "server_packet_position_from": {"x": 40, "y": 50},
            "character_attributes": {"is_dead": False},
            "native_ai_config": {"attackAuto": 2}, "native_ai_queue": ["attack"],
            "native_attack_target": {"target_present": True, "name": "Poring"},
            "native_auto_attack_candidate_count": 1,
            "native_best_target_candidate_count": 1,
            "nearby_monsters": [{"id": f"mob-{self.agent_id}", "name": "Poring",
                "damage_from_you": 10 * max(self.observer_reads, 1),
                "attacks_from_you": max(self.observer_reads, 1)}],
            "inventory": [], "character_skills": {}}

    def _observer_rows(self):
        self.observer_reads += 1
        if (self.control.get("disconnect_agent") == self.agent_id
                and self.generation == 0 and not self.disconnect_reported):
            self.disconnect_reported = True
            self.observer_rows.append(self._state(connected=False))
        else:
            self.observer_rows.append(self._state(connected=True))
        return list(self.observer_rows)

    def close(self):
        self.closed = True
        if self.process:
            self.process.exit_code = 0
        self.lab.online[self.char_id] = 0


@pytest.fixture
def fleet_harness(tmp_path, monkeypatch):
    control: dict = {"disconnect_agent": "agent-003", "fail_recovery": False}
    root = tmp_path / "managed-data"
    manager = FakeLab(root, control)
    openkore = tmp_path / "openkore"
    (openkore / "fields").mkdir(parents=True)
    (openkore / "tables/kRO/Ragexe_2021_11_03").mkdir(parents=True)
    (openkore / "tables/iRO").mkdir(parents=True)
    xstools = tmp_path / "XSTools"
    xstools.mkdir()
    monkeypatch.setattr(fleet_module, "ManagedLaboratory", lambda _root: manager)
    monkeypatch.setattr(fleet_module, "_drain_openkore_output", lambda _adapter: None)
    monkeypatch.setattr(fleet_module, "_resource_snapshot", lambda pids: {
        "processes": len(pids), "rss_mb": float(len(pids)), "cpu_percent": 0.0})
    monkeypatch.setattr(fleet_module, "_sql_rows", _fake_sql_rows)
    from korelab import runner
    from korelab.adapters import openkore as openkore_adapter
    monkeypatch.setattr(runner, "preflight_perl", lambda *_args: Path("/usr/bin/perl"))
    monkeypatch.setattr(openkore_adapter, "OpenKoreAdapter", FakeAdapter)
    fleet = ConcurrentFleet(root)
    return fleet, manager, control, openkore, tmp_path / "rathena", xstools


def _fake_sql_rows(manager: FakeLab, _home: Path, sql: str) -> list[list[str]]:
    if manager.control.get("global_failure") and "last_map" in sql:
        raise RuntimeError("shared database observation failed")
    if "MAX(event_id)" in sql:
        return [["0"]]
    if "COUNT(*),MAX(occurred_at)" in sql:
        return []
    if "COUNT(*)" in sql and "online<>0" in sql:
        return [[str(sum(value != 0 for value in manager.online.values()))]]
    if "WHERE char_id=" in sql:
        char_id = int(sql.split("WHERE char_id=")[1].split()[0])
        return [[str(manager.online.get(char_id, 0))]]
    if "WHERE char_id IN" in sql:
        ids_text = sql.split("WHERE char_id IN (")[1].split(")")[0]
        char_ids = [int(value) for value in ids_text.split(",")]
        rows = []
        for char_id in char_ids:
            online = manager.online.get(char_id, 0)
            if "last_map" in sql and "base_level" in sql:
                rows.append([str(char_id), str(online), "prt_fild08", "1", "0"])
            elif "last_map" in sql:
                rows.append([str(char_id), "prt_fild08", "40", "50", str(online)])
            else:
                rows.append([str(char_id), str(online)])
        return rows
    raise AssertionError(f"unexpected fixture SQL: {sql}")


def _run(harness):
    fleet, _manager, control, openkore, rathena, xstools = harness
    control["test_control"] = control
    # The adapter receives this only through its scenario, never through a real profile.
    original = FakeAdapter.__init__

    def init_with_test_control(self, checkout, scenario, run_dir, deadline, perl):
        scenario["_test_control"] = control
        original(self, checkout, scenario, run_dir, deadline, perl)

    FakeAdapter.__init__ = init_with_test_control
    try:
        return fleet.run(openkore=openkore, rathena_runtime=rathena,
            xstools_overlay=xstools, count=5, lock_map="prt_fild08",
            start_point={"map": "prontera", "x": 156, "y": 191},
            window_seconds=1, sample_interval=1, startup_batch=5)
    finally:
        FakeAdapter.__init__ = original


def test_run_recovers_one_disconnected_agent_and_completes_cleanup(fleet_harness) -> None:
    report = _run(fleet_harness)
    _fleet, manager, control, *_ = fleet_harness

    assert report["acceptance"] == "PASS"
    assert report["session_recoveries"] == [{
        "agents": ["agent-003"], "recovered": ["agent-003"],
        "terminal_failures": [], "restarted_only_failed_profiles": True,
        "acceptance_window_restarted": True, "next_window": 2}]
    assert control["generations"]["agent-003"] == 2
    assert all(control["generations"][f"agent-{i:03d}"] == 1 for i in (1, 2, 4, 5))
    assert manager.stop_calls == manager.destroy_calls == 1
    assert manager.destroyed and not (control["home"]).exists()


def test_run_explicit_completion_stops_all_agents_and_removes_lab(fleet_harness) -> None:
    _fleet, manager, control, *_ = fleet_harness
    control["disconnect_agent"] = None

    report = _run(fleet_harness)

    assert report["acceptance"] == "PASS"
    assert report["minimum_concurrent_online"] == 5
    assert manager.stop_calls == manager.destroy_calls == 1
    assert manager.destroyed and not control["home"].exists()
    assert all(adapter.process.poll() is not None for adapter in control["adapters"])


def test_run_keeps_four_healthy_agents_through_terminal_failure_window(fleet_harness, monkeypatch) -> None:
    _fleet, manager, control, *_ = fleet_harness
    control["fail_recovery"] = True
    original_sleep = time.sleep
    healthy_during_wait = []
    teardown_calls_during_wait = []

    def observed_sleep(seconds):
        if control.get("terminal_failure_at") and not manager.destroyed:
            healthy_during_wait.append(sum(
                adapter.process.poll() is None and adapter.agent_id != "agent-003"
                for adapter in control.get("adapters", [])))
            teardown_calls_during_wait.append((manager.stop_calls, manager.destroy_calls))
        original_sleep(seconds)

    monkeypatch.setattr(fleet_module.time, "sleep", observed_sleep)
    report = _run(fleet_harness)

    assert report["acceptance"] == "FAILED"
    assert report["failure"] == "terminal individual agent failure(s): agent-003"
    assert report["terminal_agent_failures"] == [{
        "agent_id": "agent-003", "phase": "session_recovery", "error_type": "_AgentRecoveryError"}]
    assert report["session_recoveries"][0]["acceptance_window_restarted"] is True
    assert report["session_recoveries"][0]["next_window"] == 2
    assert control["failed_recovery_attempts"] == 3
    assert report["acceptance_window_actual_seconds"] >= 1
    assert report["minimum_concurrent_online"] == 4
    assert report["peak_concurrent_online"] == 4
    assert len(report["samples"]) >= 1
    assert {sample["window"] for sample in report["samples"]} == {2}
    assert report["samples"][-1]["server_confirmed_online"] == 4
    assert healthy_during_wait and max(healthy_during_wait) >= 4
    assert teardown_calls_during_wait and all(calls == (0, 0)
        for calls in teardown_calls_during_wait)
    assert manager.destroyed and manager.stop_calls == manager.destroy_calls == 1
    assert all(adapter.process.poll() is not None for adapter in control["adapters"])


def test_run_global_observation_failure_stops_agents_and_destroys_lab(fleet_harness) -> None:
    _fleet, manager, control, *_ = fleet_harness
    control["global_failure"] = True

    report = _run(fleet_harness)

    assert report["acceptance"] == "FAILED"
    assert "shared database observation failed" in report["failure"]
    assert manager.stop_calls == manager.destroy_calls == 1
    assert manager.destroyed and not control["home"].exists()
    assert all(adapter.process.poll() is not None for adapter in control["adapters"])
