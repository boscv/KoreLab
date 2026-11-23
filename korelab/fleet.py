"""Bounded orchestration and evidence for real concurrent OpenKore sessions."""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .laboratory import ManagedLaboratory, _atomic_json
from .platform_support import set_private_permissions
from .fleet_profiles import (expand_fleet_roster, fleet_archetype, load_fleet_roster,
                             skill_config_block,
                             validate_fleet_archetypes, validate_fleet_maps)


class _AgentRecoveryError(RuntimeError):
    """A single profile exhausted its own recovery attempts."""


def _latest_state(adapter) -> dict | None:
    rows = adapter._observer_rows()
    return next((row for row in reversed(rows) if row.get("type") == "state"), None)


def _unconfirmed_combat_agents(agents: list[dict], outcomes: dict[str, dict]) -> list[dict]:
    return [agent for agent in agents if not outcomes[agent["id"]]["combat_confirmed"]]


def _unconfirmed_skill_agents(agents: list[dict], outcomes: dict[str, dict]) -> list[dict]:
    """Require a server-confirmed skill-use response, not only a client request."""
    return [agent for agent in agents
            if int(outcomes[agent["id"]].get("offensive_skill_use_confirmations", 0)) < 1]


def _recovery_targets(agents: list[dict], failed_ids) -> list[dict]:
    """Return only known failed profiles, in stable fleet order."""
    requested = set(failed_ids)
    by_id = {agent["id"]: agent for agent in agents}
    unknown = requested - by_id.keys()
    if unknown:
        raise RuntimeError("recovery requested for unknown agent identity")
    return [agent for agent in agents if agent["id"] in requested]


def _launch_agent_with_retries(agent_id: str, launch, on_failure, *, attempts: int = 3,
                               wait=time.sleep):
    """Bound startup retries for one profile without affecting other sessions."""
    if type(attempts) is not int or attempts < 1:
        raise ValueError("agent launch attempts must be a positive integer")
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return launch()
        except Exception as exc:
            last_error = exc
            on_failure()
            if attempt < attempts:
                wait(attempt)
    raise RuntimeError(
        f"OpenKore session for {agent_id} failed after {attempts} bounded attempts: "
        f"{type(last_error).__name__}") from last_error


def _drain_openkore_output(adapter) -> None:
    """Keep native OpenKore's PTY flowing while its AI runs unattended."""
    while adapter.process and adapter.process.poll() is None:
        try:
            adapter._read(0.2)
            if len(adapter.output) > 65536:
                adapter.output = adapter.output[-32768:]
        except TimeoutError:
            return


def _native_fleet_openkore_settings(lock_map: str, profile_name: str,
                                    target_monsters=None,
                                    config_overrides: dict | None = None) -> dict:
    """Configure OpenKore's own lock-map routing and auto-combat routines."""
    targets = tuple(target_monsters or ("Poring", "Lunatic", "Fabre", "Little Poring"))
    settings = {
        "native_profile": profile_name,
        "ai_mode": "on",
        "control_file_overrides": {"mon_control.txt": {"mode": "append",
            # These passive mobs must be eligible for OpenKore's native
            # auto-attack candidate list; mode 1 is its normal always-attack
            # setting. This only changes client target selection.
            "content": "".join(f"{name} 1 0 0 0 0 0 0 0 1\n" for name in targets)}},
        "config_overrides": {
            "lockMap": lock_map,
            "attackAuto": 2,
            # Match OpenKore's supported native lock-map profile: allow the
            # configured auto-attack mode while routing to the lock map, and
            # leave unrelated maps alone if a session is displaced.
            "attackAuto_routeToLock": 1,
            "attackAuto_outOfLock": 0,
            "attackAuto_notInTown": 1,
            "attackAuto_party": 0,
            "attackDistance": 1,
            "attackDistanceAuto": 1,
            "attackMaxDistance": 1,
            "attackUseWeapon": 1,
            "attackCheckLOS": 1,
            "attackRouteMaxPathDistance": 20,
            "attackMinPortalDistance": 4,
            "attackMinPlayerDistance": 2,
            "itemsTakeAuto": 0,
            "itemsGatherAuto": 0,
            "route_randomWalk": 1,
            "route_randomWalk_inLockOnly": 1,
            "route_step": 10,
            "portalCompile": 1,
            "sitAuto_idle": 0,
        },
    }
    if config_overrides:
        settings["config_overrides"].update(config_overrides)
    settings["config_overrides"]["lockMap"] = lock_map
    return settings


def _sql_rows(manager: ManagedLaboratory, home: Path, sql: str) -> list[list[str]]:
    result = manager._query_owned_db(home, sql)
    if result.returncode:
        raise RuntimeError("independent managed rAthena observation failed")
    return [line.split("\t") for line in result.stdout.splitlines() if line]


def _resource_snapshot(pids: list[int]) -> dict:
    if not pids:
        return {"processes": 0, "rss_mb": 0.0, "cpu_percent": 0.0}
    result = subprocess.run(["ps", "-p", ",".join(map(str, pids)), "-o", "pid=,rss=,%cpu="],
        capture_output=True, text=True, timeout=5, check=False)
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3:
            try:
                rows.append((int(fields[0]), int(fields[1]), float(fields[2])))
            except ValueError:
                continue
    return {"processes": len(rows), "rss_mb": round(sum(row[1] for row in rows) / 1024, 1),
            "cpu_percent": round(sum(row[2] for row in rows), 1)}


class ConcurrentFleet:
    """Create a disposable native rAthena/OpenKore fleet and fail closed on loss."""

    def __init__(self, data_home: Path | None = None):
        self.lab = ManagedLaboratory(data_home)

    def run(self, *, openkore: Path, rathena_runtime: Path, xstools_overlay: Path,
            count: int | None, lock_map: str | None, start_point: dict[str, int],
            window_seconds: int = 300, sample_interval: int = 5,
            startup_batch: int = 5, diverse_roster: bool = False,
            roster_path: Path | None = None) -> dict:
        roster_started = time.monotonic()
        if roster_path is not None and diverse_roster:
            raise ValueError("a saved roster cannot be combined with --diverse-roster")
        saved_roster = load_fleet_roster(roster_path) if roster_path is not None else None
        if count is None:
            count = saved_roster["population"] if saved_roster else 50
        if lock_map is None:
            lock_map = next(iter(saved_roster["builds"].values()))["lock_map"] if saved_roster else None
        if lock_map is None:
            raise ValueError("--lock-map is required without a saved roster")
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("fleet count must be between 1 and 100")
        if not re.fullmatch(r"[a-z0-9_]+", lock_map):
            raise ValueError("lock map must be a native lowercase map name")
        if type(window_seconds) is not int or not 1 <= window_seconds <= 3600:
            raise ValueError("observation window must be between 1 and 3600 seconds")
        if type(sample_interval) is not int or not 1 <= sample_interval <= 30:
            raise ValueError("sample interval must be between 1 and 30 seconds")
        if type(startup_batch) is not int or not 1 <= startup_batch <= 10:
            raise ValueError("startup batch must be between 1 and 10 sessions")
        if type(diverse_roster) is not bool:
            raise ValueError("diverse_roster must be a boolean")
        custom_profiles = list(saved_roster["builds"].values()) if saved_roster else None
        roster_definitions = expand_fleet_roster(saved_roster, count) if saved_roster else None
        if saved_roster or diverse_roster:
            validate_fleet_archetypes(rathena_runtime, profiles=custom_profiles)
            validate_fleet_maps(openkore, rathena_runtime, profiles=custom_profiles)

        roster_preflight_elapsed = time.monotonic() - roster_started
        create_started = time.monotonic()
        created = self.lab.create(openkore=openkore, rathena_runtime=rathena_runtime,
            xstools_overlay=xstools_overlay, name="combat-fleet", start_point=start_point,
            agent_count=count, combat_observer=True)
        lab_id = created["id"]
        home = Path(created["path"])
        stage_timings = {"roster_preflight_seconds": round(roster_preflight_elapsed, 3),
            "laboratory_create_seconds": round(time.monotonic() - create_started, 3),
            "profile_definition_seconds": 0.0, "server_start_seconds": 0.0,
            "native_account_character_provision_seconds": 0.0,
            "openkore_profile_materialization_agent_seconds": 0.0,
            "openkore_launch_and_map_login_agent_seconds": 0.0,
            "openkore_startup_wall_seconds": 0.0,
            "native_lock_map_arrival_seconds": None, "acceptance_window_seconds": 0.0,
            "laboratory_teardown_seconds": 0.0}
        owner = json.loads((home / "owner.json").read_text(encoding="utf-8"))
        agents = owner["agents"]
        if saved_roster:
            archetypes = {agent["id"]: definition for agent, definition in zip(agents, roster_definitions)}
        elif diverse_roster:
            archetypes = {agent["id"]: fleet_archetype(index) for index, agent in enumerate(agents)}
        else:
            archetypes = {}
        profile_definition_started = time.monotonic()
        if saved_roster or diverse_roster:
            for agent in agents:
                archetype = archetypes[agent["id"]]
                profile_path = (home / agent["profile_file"]).resolve()
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                config_path = Path(profile["profile_config"]).resolve()
                if not config_path.is_relative_to(home.resolve()):
                    raise RuntimeError("managed OpenKore config escaped its laboratory")
                config_text = config_path.read_text(encoding="utf-8")
                block = skill_config_block(archetype)
                if archetype["skill"] not in config_text:
                    config_path.write_text(config_text + block, encoding="utf-8")
                    set_private_permissions(config_path, 0o600)
                profile["archetype"] = {key: value for key, value in archetype.items()}
                _atomic_json(profile_path, profile)
            _atomic_json(home / "owner.json", owner)
        stage_timings["profile_definition_seconds"] = round(
            time.monotonic() - profile_definition_started, 3)
        stage_timing_lock = threading.Lock()

        def add_stage_time(key: str, elapsed: float) -> None:
            with stage_timing_lock:
                stage_timings[key] += elapsed

        adapters: dict[str, object] = {}
        output_readers: dict[str, threading.Thread] = {}
        outcomes = {agent["id"]: {"account": agent["account"], "character": agent["character"],
            "class_id": None, "level": None,
            "lock_map": archetypes.get(agent["id"], {}).get("lock_map",
                archetypes.get(agent["id"], {}).get("map", lock_map)), "login": "pending",
            "movement": "pending", "combat_confirmed": False, "combat_events": 0,
            "server_observed_attack_damage": 0, "server_observed_attack_count": 0,
            "offensive_skill_use_requests": 0, "offensive_skill_use_confirmations": 0,
            "offensive_skill_use_observed": False,
            "maps_observed": [], "death_observations": 0,
            "disconnections": 0, "errors": []} for agent in agents}
        observed_mob_damage: dict[str, dict[str, int]] = {agent["id"]: {} for agent in agents}
        observed_mob_attacks: dict[str, dict[str, int]] = {agent["id"]: {} for agent in agents}
        observer_cursors: dict[str, int] = {}
        samples: list[dict] = []
        terminal_failures: dict[str, dict] = {}
        report: dict = {"lab_id": lab_id, "requested_agents": count,
            "lock_map": lock_map, "diverse_roster": diverse_roster,
            "roster_id": saved_roster["id"] if saved_roster else None,
            "roster_source": saved_roster["source"] if saved_roster else None,
            "observation_window_seconds": window_seconds,
            "observation_interval_seconds": sample_interval, "agents": outcomes,
            "startup_checkpoints": [], "samples": samples, "cleanup": {},
            "preparation_timings_seconds": stage_timings}
        acceptance_started = None
        failure = None

        def mark_terminal_agent_failure(agent: dict, phase: str, exc: Exception) -> None:
            agent_id = agent["id"]
            failure_record = {"agent_id": agent_id, "phase": phase,
                              "error_type": type(exc).__name__}
            terminal_failures[agent_id] = failure_record
            report.setdefault("terminal_agent_failures", []).append(failure_record)
            outcomes[agent_id]["login"] = "terminal_agent_failure"
            outcomes[agent_id]["errors"].append(
                f"{phase} recovery exhausted ({type(exc).__name__})")

        try:
            started_clock = time.monotonic()
            started = self.lab.start(lab_id, timeout=90)
            stage_timings["server_start_seconds"] = round(time.monotonic() - started_clock, 3)
            if not started.get("healthy"):
                raise RuntimeError("managed rAthena services did not reach gameplay readiness")
            owner = json.loads((home / "owner.json").read_text(encoding="utf-8"))
            agents = owner["agents"]
            report["agents"] = outcomes
            identities = []
            for agent in agents:
                provision_started = time.monotonic()
                result = self.lab.bootstrap(lab_id, agent_id=agent["id"], timeout=120)
                if saved_roster or diverse_roster:
                    result.update(self.lab.prepare_fleet_character(
                        lab_id, agent_id=agent["id"], archetype=archetypes[agent["id"]]))
                provision_elapsed = time.monotonic() - provision_started
                stage_timings["native_account_character_provision_seconds"] += provision_elapsed
                outcomes[agent["id"]]["provisioning_seconds"] = round(provision_elapsed, 3)
                agent.update({"char_id": result["char_id"], "state": "provisioned",
                              "baseline": {"map": result["position"]["map"],
                                  "x": result["position"]["x"], "y": result["position"]["y"]}})
                outcomes[agent["id"]].update({"login": "native_created_and_verified",
                    "char_id": result["char_id"], "class_id": result["class_id"],
                    "level": result["base_level"], "baseline_position": result["position"]})
                if saved_roster or diverse_roster:
                    outcomes[agent["id"]].update({
                        "job": archetypes[agent["id"]]["job"],
                        "job_level": archetypes[agent["id"]]["job_level"],
                        "attributes": result["attributes"],
                        "status_points_remaining": result["status_points_remaining"],
                        "weapon": archetypes[agent["id"]]["weapon"],
                        "offensive_skill": archetypes[agent["id"]]["skill"],
                        "lock_map": archetypes[agent["id"]]["map"],
                        "target_monsters": list(archetypes[agent["id"]]["targets"]),
                        "archetype_fixture_verified": result["server_verified"]})
                identities.append(result["char_id"])
            if len(set(identities)) != count or len({a["account"] for a in agents}) != count \
                    or len({a["character"] for a in agents}) != count:
                raise RuntimeError("native provisioning did not produce unique account/character identities")
            report["provisioned_characters"] = count
            if saved_roster or diverse_roster:
                report["roster"] = [{"account": agent["account"],
                    "character": agent["character"], **outcomes[agent["id"]]}
                    for agent in agents]

            from .adapters.openkore import OpenKoreAdapter
            from .adapters.openkore import redact as redact_openkore_output
            from .runner import preflight_perl
            perl = preflight_perl(Path(owner["openkore_source"]), Path(xstools_overlay))
            profile = json.loads((home / agents[0]["profile_file"]).read_text(encoding="utf-8"))
            if not profile.get("fields_folder"):
                raise RuntimeError("managed OpenKore profile has no compatible fields folder")
            tables = Path(profile["openkore"]) / "tables"
            table_folders = ["kRO/Ragexe_2021_11_03", "iRO"]
            if not all((tables / folder).is_dir() for folder in table_folders):
                raise RuntimeError("OpenKore is missing the managed profile's packet tables")
            server = {"name": profile["server_profile"], "host": "127.0.0.1",
                "port": int(profile["server_port"]), "server_type": profile["server_type"],
                "table_folders": table_folders, "fields_folder": profile["fields_folder"]}
            def launch(agent: dict) -> tuple[str, object]:
                profile_started = time.monotonic()
                current = json.loads((home / agent["profile_file"]).read_text(encoding="utf-8"))
                creds = json.loads((home / "secrets.json").read_text(encoding="utf-8"))
                secret = creds.get("fleet_agents", {}).get(agent["id"], creds)
                config_source = Path(current["profile_config"])
                config_text = config_source.read_text(encoding="utf-8")
                for key, value in (("username", secret["account"]),
                                   ("password", secret["password"]),
                                   ("loginPinCode", secret["login_pin"])):
                    config_text, changed = re.subn(
                        rf"(?m)^{re.escape(key)}\s+.*$", f"{key} {value}",
                        config_text, count=1)
                    if changed != 1:
                        raise RuntimeError(f"private OpenKore profile has no unique {key} setting")
                session_root = home / "fleet" / "sessions" / agent["id"]
                session_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                runtime_config = session_root / "config.txt"
                runtime_config.write_text(config_text, encoding="utf-8")
                set_private_permissions(runtime_config, 0o600)
                add_stage_time("openkore_profile_materialization_agent_seconds",
                               time.monotonic() - profile_started)
                profile_name = "bot" + agent["id"].split("-")[-1]
                archetype = archetypes.get(agent["id"], {})
                agent_lock_map = archetype.get("lock_map", archetype.get("map", lock_map))
                native_openkore = _native_fleet_openkore_settings(agent_lock_map, profile_name,
                    archetype.get("targets"),
                    saved_roster.get("openkore_config") if saved_roster else None)
                scenario = {"identity": {"master": current["server_profile"],
                    "username": secret["account"], "character": current["character"],
                    "character_slot": 0}, "openkore": {"profile_config_path": str(runtime_config),
                    "profile_selection": {"profile": current["server_profile"],
                        "server_index": 0, "character_slot": 0}, "server_profile": server,
                    "xstools_overlay": current["xstools_overlay"],
                    "connection_stage_timeout": 120,
                    **native_openkore}}
                outcomes[agent["id"]]["openkore_profile"] = profile_name
                outcomes[agent["id"]]["lock_map_config"] = agent_lock_map
                run_dir = session_root / ("session-" + uuid.uuid4().hex[:8])
                run_dir.mkdir(mode=0o700)
                adapter = OpenKoreAdapter(Path(current["openkore"]), scenario, run_dir,
                    time.monotonic() + window_seconds + max(300, count * 20), perl)
                try:
                    launch_started = time.monotonic()
                    adapter.start()
                    state = adapter.state(lambda row: row.get("connected_in_game") is True
                        and bool(row.get("map")), timeout=120)
                    launch_elapsed = time.monotonic() - launch_started
                    add_stage_time("openkore_launch_and_map_login_agent_seconds", launch_elapsed)
                    outcomes[agent["id"]]["initial_login_seconds"] = round(launch_elapsed, 3)
                    outcomes[agent["id"]]["initial_map"] = state.get("map")
                    reader = threading.Thread(target=_drain_openkore_output,
                        args=(adapter,), name=f"korelab-output-{agent['id']}", daemon=True)
                    reader.start()
                    output_readers[agent["id"]] = reader
                    outcomes[agent["id"]]["native_movement_observer"] = (
                        "OpenKore native lock-map and attack routing; persistence is checked after logout")
                except BaseException as exc:
                    safe_output = redact_openkore_output(adapter.output, adapter.sensitive_values)
                    diagnostics = [line.strip()[:400] for line in safe_output.splitlines()
                        if re.search(r"(?i)attack|monster|mob|route|path|stuck|target|disconnect|connection|error|observer", line)]
                    outcomes[agent["id"]]["errors"].append(f"{type(exc).__name__}: {exc}")
                    outcomes[agent["id"]]["openkore_process_exit_code"] = (
                        adapter.process.poll() if adapter.process else None)
                    outcomes[agent["id"]]["openkore_failure_output_tail"] = [
                        line.strip()[:400] for line in safe_output.splitlines()[-40:]]
                    if diagnostics:
                        outcomes[agent["id"]]["openkore_diagnostics"] = diagnostics[-30:]
                    adapter.close()
                    raise
                adapters[agent["id"]] = adapter
                return agent["id"], adapter

            def launch_with_retry(agent: dict, attempts: int = 3) -> tuple[str, object]:
                agent_id = agent["id"]
                def record_failure() -> None:
                    outcomes[agent_id]["recovery_attempts"] = int(
                        outcomes[agent_id].get("recovery_attempts", 0)) + 1
                return _launch_agent_with_retries(agent_id,
                    lambda: launch(agent), record_failure, attempts=attempts)

            def recover_agent(agent: dict, home: Path) -> None:
                agent_id = agent["id"]
                result = outcomes[agent_id]
                attempts = int(result.get("recovery_cycles", 0))
                if attempts >= 3:
                    raise _AgentRecoveryError(f"session recovery retry limit reached for {agent_id}")
                old = adapters.get(agent_id)
                if old:
                    old.close()
                reader = output_readers.get(agent_id)
                if reader:
                    reader.join(timeout=2)
                # Wait for rAthena to clear the old map-server session before
                # reconnecting this one account. Other bots remain untouched.
                offline_deadline = time.monotonic() + 30
                while time.monotonic() < offline_deadline:
                    rows = _sql_rows(self.lab, home,
                        f"SELECT online FROM `char` WHERE char_id={int(agent['char_id'])}")
                    if rows and rows[0][0] == "0":
                        break
                    time.sleep(1)
                else:
                    raise _AgentRecoveryError(
                        f"rAthena did not clear the disconnected session for {agent_id}")
                try:
                    agent_id, replacement = launch_with_retry(agent, attempts=3 - attempts)
                except RuntimeError as exc:
                    raise _AgentRecoveryError(
                        f"OpenKore profile could not reconnect for {agent_id}") from exc
                adapters[agent_id] = replacement
                online_deadline = time.monotonic() + 30
                while time.monotonic() < online_deadline:
                    rows = _sql_rows(self.lab, home,
                        f"SELECT online FROM `char` WHERE char_id={int(agent['char_id'])}")
                    if rows and rows[0][0] == "1":
                        result["login"] = "map_connected_recovered"
                        result["recovery_cycles"] = attempts + 1
                        return
                    time.sleep(1)
                raise _AgentRecoveryError(
                    f"rAthena did not confirm the recovered session for {agent_id}")

            def recover_one_or_mark_terminal(agent: dict, phase: str) -> bool:
                try:
                    recover_agent(agent, home)
                    return True
                except _AgentRecoveryError as exc:
                    mark_terminal_agent_failure(agent, phase, exc)
                    return False

            startup_started = time.monotonic()
            checkpoint_targets = sorted(set([min(count, n) for n in (5, 10, 20, 35, 50)] + [count]))
            cursor = 0
            for target in checkpoint_targets:
                while cursor < target:
                    batch = agents[cursor:min(target, cursor + startup_batch)]
                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as pool:
                        futures = {pool.submit(launch_with_retry, agent): agent for agent in batch}
                        for future in concurrent.futures.as_completed(futures):
                            agent = futures[future]
                            try:
                                agent_id, adapter = future.result()
                                adapters[agent_id] = adapter
                                outcomes[agent_id]["login"] = "map_connected"
                            except Exception as exc:
                                mark_terminal_agent_failure(agent, "startup", exc)
                    cursor += len(batch)
                ids = ",".join(str(agent["char_id"]) for agent in agents[:target])
                online_rows = _sql_rows(self.lab, home,
                    f"SELECT char_id,online FROM `char` WHERE char_id IN ({ids})")
                online = {int(row[0]): row[1] == "1" for row in online_rows}
                missing = [agent for agent in agents[:target]
                    if agent["id"] not in terminal_failures
                    and not online.get(int(agent["char_id"]), False)]
                for agent in _recovery_targets(agents, (agent["id"] for agent in missing)):
                    try:
                        recover_agent(agent, home)
                    except _AgentRecoveryError as exc:
                        mark_terminal_agent_failure(agent, "startup_recovery", exc)
                if missing:
                    online_rows = _sql_rows(self.lab, home,
                        f"SELECT char_id,online FROM `char` WHERE char_id IN ({ids})")
                    online = {int(row[0]): row[1] == "1" for row in online_rows}
                not_terminal = [agent for agent in agents[:target]
                    if agent["id"] not in terminal_failures]
                offline = [agent for agent in not_terminal
                    if not online.get(int(agent["char_id"]), False)]
                if offline:
                    health = self.lab.inspect(lab_id)
                    if not health.get("healthy"):
                        raise RuntimeError("shared rAthena services became unhealthy during startup")
                    for agent in offline:
                        mark_terminal_agent_failure(agent, "startup_verification",
                            RuntimeError("rAthena did not confirm the individual session"))
                report["startup_checkpoints"].append({"agents": target,
                    "server_confirmed_online": sum(online.values()),
                    "recovered_profiles": [agent["id"] for agent in missing
                        if agent["id"] not in terminal_failures],
                    "terminal_failures": [agent["id"] for agent in agents[:target]
                        if agent["id"] in terminal_failures],
                    "status": "PARTIAL_AGENT_FAILURE" if any(
                        agent["id"] in terminal_failures for agent in agents[:target]) else "PASS"})

            minimum_online = peak_online = sum(online.values())
            stage_timings["openkore_startup_wall_seconds"] = round(
                time.monotonic() - startup_started, 3)
            prior_events = _sql_rows(self.lab, home,
                "SELECT COALESCE(MAX(event_id),0) FROM korelab_combat_events")
            prior_event_id = int(prior_events[0][0]) if prior_events else 0
            start_wall = datetime.now(timezone.utc)
            acceptance_started = time.monotonic()
            end = acceptance_started + window_seconds
            window_index = 1
            minimum_online = peak_online = sum(online.values())
            combat_retry_cycles = 0
            while True:
                pids = []
                latest_states = {}
                recovery_required: set[str] = set()
                for agent in agents:
                    agent_id = agent["id"]
                    if agent_id in terminal_failures:
                        continue
                    adapter = adapters[agent_id]
                    if not adapter.process or adapter.process.poll() is not None:
                        outcomes[agent_id]["disconnections"] += 1
                        recovery_required.add(agent_id)
                        continue
                    pids.append(adapter.process.pid)
                    observer_rows = adapter._observer_rows()
                    new_observer_rows = observer_rows[observer_cursors.get(agent_id, 0):]
                    states = [row for row in new_observer_rows if row.get("type") == "state"]
                    observer_cursors[agent_id] = len(observer_rows)
                    latest = next((row for row in reversed(observer_rows)
                                   if row.get("type") == "state"), None)
                    state = latest
                    latest_states[agent_id] = state
                    skill_id = archetypes.get(agent_id, {}).get("skill_id")
                    confirmed_skills = [row for row in new_observer_rows
                                        if row.get("type") == "skill_use_confirmed"
                                        and (skill_id is None
                                             or int(row.get("skill_id", -1)) == skill_id)]
                    if confirmed_skills:
                        outcome = outcomes[agent_id]
                        outcome["offensive_skill_use_confirmations"] = int(
                            outcome.get("offensive_skill_use_confirmations", 0)) + len(confirmed_skills)
                        outcome["offensive_skill_use_observed"] = True
                    if state:
                        outcome = outcomes[agent_id]
                        skill_requests = [row for row in new_observer_rows
                                          if row.get("type") == "skill_use_request"
                                          and (skill_id is None or int(row.get("skill_id", -1)) == skill_id)]
                        if skill_requests:
                            outcome["offensive_skill_use_requests"] = int(
                                outcome.get("offensive_skill_use_requests", 0)) + len(skill_requests)
                        observed_map = state.get("map")
                        if observed_map and observed_map not in outcome["maps_observed"]:
                            outcome["maps_observed"].append(observed_map)
                        if state.get("character_attributes", {}).get("is_dead") is True:
                            outcome["death_observations"] += 1
                        # OpenKore derives these counters from received rAthena
                        # attack result packets. Keep per-mob maxima so repeated
                        # observer snapshots do not count the same hit twice.
                        for observed in states:
                            for monster in observed.get("nearby_monsters", []):
                                monster_id = monster.get("id")
                                if not monster_id:
                                    continue
                                damage = int(monster.get("damage_from_you", 0) or 0)
                                attacks = int(monster.get("attacks_from_you", 0) or 0)
                                observed_mob_damage[agent_id][monster_id] = max(
                                    observed_mob_damage[agent_id].get(monster_id, 0), damage)
                                observed_mob_attacks[agent_id][monster_id] = max(
                                    observed_mob_attacks[agent_id].get(monster_id, 0), attacks)
                        outcome["server_observed_attack_damage"] = sum(
                            observed_mob_damage[agent_id].values())
                        outcome["server_observed_attack_count"] = sum(
                            observed_mob_attacks[agent_id].values())
                        outcome["combat_confirmed"] = bool(
                            outcome["combat_events"] or outcome["server_observed_attack_damage"] > 0)
                        outcome["native_ai_observed"] = {
                            "map": state.get("map"),
                            "position": state.get("server_packet_position_from"),
                            "movement_state": state.get("movement_state"),
                            "ai_idle": state.get("native_ai_idle"),
                            "field_is_city": state.get("field_is_city"),
                            "field_raw_map_loaded": state.get("field_raw_map_loaded"),
                            "ai_config": state.get("native_ai_config"),
                            "queue": state.get("native_ai_queue", []),
                            "attack_target": state.get("native_attack_target"),
                            "auto_attack_candidates": state.get("native_auto_attack_candidate_count"),
                            "best_target_candidates": state.get("native_best_target_candidate_count"),
                            "nearby_monsters": [
                                {"name": monster.get("name"),
                                 "mon_control_attack_auto": monster.get("mon_control_attack_auto"),
                                 "position": monster.get("position")}
                                for monster in state.get("nearby_monsters", [])[:8]
                            ],
                        }
                        if any("attack" in str(action).casefold()
                               for action in state.get("native_ai_queue", [])):
                            outcome["native_ai_attack_queue_samples"] = int(
                                outcome.get("native_ai_attack_queue_samples", 0)) + 1
                        native_target = state.get("native_attack_target")
                        if native_target and native_target.get("target_present"):
                            outcome["native_attack_target_observed"] = {
                                "name": native_target.get("name"),
                                "position": native_target.get("position")}
                        outcome["native_auto_attack_candidate_samples"] = int(
                            outcome.get("native_auto_attack_candidate_samples", 0)) + int(
                                state.get("native_auto_attack_candidate_count", 0) > 0)
                    if (not state or state.get("connected_in_game") is not True
                            or not state.get("map")):
                        recovery_required.add(agent_id)
                        continue
                    if state.get("server_packet_position_from"):
                        pos = state["server_packet_position_from"]
                        agent_lock_map = outcomes[agent_id]["lock_map"]
                        if state.get("map") == agent_lock_map:
                            outcomes[agent_id]["movement"] = "native_movement_observed"
                            outcomes[agent_id].setdefault("native_lock_map_arrival", {
                                "map": agent_lock_map, "x": pos.get("x"), "y": pos.get("y")})
                            if stage_timings["native_lock_map_arrival_seconds"] is None:
                                stage_timings["native_lock_map_arrival_seconds"] = round(
                                    time.monotonic() - acceptance_started, 3)
                            outcomes[agent_id]["native_movement_observed_position"] = {
                                "map": agent_lock_map, "x": pos.get("x"), "y": pos.get("y")}
                if recovery_required:
                    recovered_ids = []
                    for agent in _recovery_targets(agents, recovery_required):
                        if recover_one_or_mark_terminal(agent, "session_recovery"):
                            recovered_ids.append(agent["id"])
                    # A recovered session invalidates the uninterrupted window.
                    # Keep the other 49 processes alive and restart only the
                    # failed profile's session before beginning a fresh window.
                    ids = ",".join(str(agent["char_id"]) for agent in agents)
                    online_rows = _sql_rows(self.lab, home,
                        f"SELECT char_id,online FROM `char` WHERE char_id IN ({ids})")
                    online = {int(row[0]): row[1] == "1" for row in online_rows}
                    offline = [agent for agent in agents
                        if agent["id"] not in terminal_failures
                        and not online.get(int(agent["char_id"]), False)]
                    if offline:
                        health = self.lab.inspect(lab_id)
                        if not health.get("healthy"):
                            raise RuntimeError("shared rAthena services became unhealthy during session recovery")
                        for agent in offline:
                            mark_terminal_agent_failure(agent, "session_verification",
                                RuntimeError("rAthena did not confirm the individual session"))
                    for agent in agents:
                        aid = agent["id"]
                        outcomes[aid]["server_observed_attack_damage"] = 0
                        outcomes[aid]["server_observed_attack_count"] = 0
                        outcomes[aid]["combat_events"] = 0
                        outcomes[aid]["combat_confirmed"] = False
                        observed_mob_damage[aid].clear()
                        observed_mob_attacks[aid].clear()
                        if aid in adapters:
                            observer_cursors[aid] = len(adapters[aid]._observer_rows())
                    prior_events = _sql_rows(self.lab, home,
                        "SELECT COALESCE(MAX(event_id),0) FROM korelab_combat_events")
                    prior_event_id = int(prior_events[0][0]) if prior_events else 0
                    report.setdefault("session_recoveries", []).append({
                        "agents": sorted(recovery_required),
                        "recovered": recovered_ids,
                        "terminal_failures": [agent_id for agent_id in sorted(recovery_required)
                            if agent_id in terminal_failures],
                        "restarted_only_failed_profiles": True,
                        "acceptance_window_restarted": True,
                        "next_window": window_index + 1})
                    start_wall = datetime.now(timezone.utc)
                    acceptance_started = time.monotonic()
                    end = acceptance_started + window_seconds
                    window_index += 1
                    online_count = sum(online.values())
                    minimum_online, peak_online = online_count, online_count
                    continue
                ids = ",".join(str(agent["char_id"]) for agent in agents)
                server_rows = _sql_rows(self.lab, home,
                    f"SELECT char_id,online,last_map,base_level,`class` FROM `char` WHERE char_id IN ({ids})")
                server_state = {int(row[0]): row for row in server_rows}
                for agent in agents:
                    status_row = server_state.get(int(agent["char_id"]))
                    outcomes[agent["id"]]["last_rathena_character_row"] = (
                        {"online": status_row[1], "saved_map": status_row[2]}
                        if status_row else None)
                online_count = sum(1 for agent in agents
                    if server_state.get(int(agent["char_id"]), [None, "0"])[1] == "1")
                minimum_online = min(minimum_online, online_count)
                peak_online = max(peak_online, online_count)
                expected_online = count - len(terminal_failures)
                if online_count < expected_online:
                    offline_agents = [agent for agent in agents
                        if agent["id"] not in terminal_failures
                        and server_state.get(int(agent["char_id"]), [None, "0"])[1] != "1"]
                    for agent in offline_agents:
                        outcomes[agent["id"]]["disconnections"] += 1
                        recovery_required.add(agent["id"])
                    recovered_ids = []
                    for agent in _recovery_targets(agents, recovery_required):
                        if recover_one_or_mark_terminal(agent, "session_recovery"):
                            recovered_ids.append(agent["id"])
                    # Restart the continuous five-minute acceptance period
                    # after any server-confirmed reconnect.
                    ids = ",".join(str(agent["char_id"]) for agent in agents)
                    online_rows = _sql_rows(self.lab, home,
                        f"SELECT char_id,online FROM `char` WHERE char_id IN ({ids})")
                    online = {int(row[0]): row[1] == "1" for row in online_rows}
                    offline = [agent for agent in agents
                        if agent["id"] not in terminal_failures
                        and not online.get(int(agent["char_id"]), False)]
                    if offline:
                        health = self.lab.inspect(lab_id)
                        if not health.get("healthy"):
                            raise RuntimeError("shared rAthena services became unhealthy during session recovery")
                        for agent in offline:
                            mark_terminal_agent_failure(agent, "session_verification",
                                RuntimeError("rAthena did not confirm the individual session"))
                    for agent in agents:
                        aid = agent["id"]
                        outcomes[aid]["server_observed_attack_damage"] = 0
                        outcomes[aid]["server_observed_attack_count"] = 0
                        outcomes[aid]["combat_events"] = 0
                        outcomes[aid]["combat_confirmed"] = False
                        observed_mob_damage[aid].clear()
                        observed_mob_attacks[aid].clear()
                        observer_cursors[aid] = len(adapters[aid]._observer_rows())
                    prior_events = _sql_rows(self.lab, home,
                        "SELECT COALESCE(MAX(event_id),0) FROM korelab_combat_events")
                    prior_event_id = int(prior_events[0][0]) if prior_events else 0
                    report.setdefault("session_recoveries", []).append({
                        "agents": sorted(recovery_required),
                        "recovered": recovered_ids,
                        "terminal_failures": [agent_id for agent_id in sorted(recovery_required)
                            if agent_id in terminal_failures],
                        "restarted_only_failed_profiles": True,
                        "acceptance_window_restarted": True,
                        "next_window": window_index + 1})
                    start_wall = datetime.now(timezone.utc)
                    acceptance_started = time.monotonic()
                    end = acceptance_started + window_seconds
                    window_index += 1
                    online_count = sum(online.values())
                    minimum_online, peak_online = online_count, online_count
                    continue
                # rAthena persists `last_map` on character save/logout; while
                # connected it is not a live map-server location. `online=1`
                # confirms map-server presence, while the current OpenKore
                # observer supplies the live map. Final SQL state is checked
                # after logout.
                combat_rows = _sql_rows(self.lab, home,
                    f"SELECT char_id,COUNT(*),MAX(occurred_at) FROM korelab_combat_events "
                    f"WHERE event_id > {prior_event_id} "
                    f"AND char_id IN ({ids}) GROUP BY char_id")
                kills = {int(row[0]): int(row[1]) for row in combat_rows}
                for agent in agents:
                    aid = agent["id"]
                    result = outcomes[aid]
                    result["combat_events"] = kills.get(int(agent["char_id"]), 0)
                    result["combat_confirmed"] = bool(result["combat_events"]
                        or result["server_observed_attack_damage"] > 0)
                    result["class_id"] = int(server_state[int(agent["char_id"])][4])
                    result["level"] = int(server_state[int(agent["char_id"])][3])
                    state = latest_states.get(aid)
                    result["actual_map"] = state.get("map") if state else None
                    adapter = adapters.get(aid)
                    result["native_ai_mode"] = (
                        "on" if adapter and adapter.scenario["openkore"]["ai_mode"] == "on"
                        else "unavailable" if not adapter else "unknown")
                    result["equipment"] = [{"item_id": item.get("item_id"),
                        "equipped": item.get("equipped", 0)} for item in (state or {}).get("inventory", [])]
                    result["skills"] = (state or {}).get("character_skills", {})
                    result["offensive_skill_use_evidence"] = (
                        "OpenKore emitted the configured native skill-use packet"
                        if result.get("offensive_skill_use_observed") else "not observed")
                    result["nearby_monster_count"] = len((state or {}).get("nearby_monsters", []))
                    result["nearby_monster_names"] = sorted({
                        str(monster.get("name")) for monster in (state or {}).get("nearby_monsters", [])
                        if monster.get("name")})
                    result["native_ai_queue"] = (state or {}).get("native_ai_queue", [])
                    result["native_ai_config"] = (state or {}).get("native_ai_config", {})
                    result["native_attack_target"] = (state or {}).get("native_attack_target")
                    result["native_auto_attack_candidate_count"] = int(
                        (state or {}).get("native_auto_attack_candidate_count", 0))
                    result["native_best_target_candidate_count"] = int(
                        (state or {}).get("native_best_target_candidate_count", 0))
                    result["nearby_monsters"] = [{"name": monster.get("name"),
                        "client_index": monster.get("client_index"),
                        "position": monster.get("position"),
                        "mon_control_attack_auto": monster.get("mon_control_attack_auto"),
                        "damage_from_you": int(monster.get("damage_from_you", 0) or 0),
                        "attacks_from_you": int(monster.get("attacks_from_you", 0) or 0)}
                        for monster in (state or {}).get("nearby_monsters", [])]
                    result["nearby_monster_damage_from_you"] = sum(
                        int(monster.get("damage_from_you", 0) or 0)
                        for monster in (state or {}).get("nearby_monsters", []))
                    result["nearby_monster_attacks_from_you"] = sum(
                        int(monster.get("attacks_from_you", 0) or 0)
                        for monster in (state or {}).get("nearby_monsters", []))
                    result["observed_attack_queue_samples"] = int(
                        result.get("observed_attack_queue_samples", 0)) + int(
                            any("attack" in str(action).casefold()
                                for action in (state or {}).get("native_ai_queue", [])))
                    result["max_observed_damage_to_monsters"] = max(
                        int(result.get("max_observed_damage_to_monsters", 0)),
                        result["nearby_monster_damage_from_you"])
                    result["max_observed_monster_attacks"] = max(
                        int(result.get("max_observed_monster_attacks", 0)),
                        result["nearby_monster_attacks_from_you"])
                    result["combat_observer_events"] = result["combat_events"]
                elapsed = time.monotonic() - acceptance_started
                rss = _resource_snapshot(pids)
                samples.append({"window": window_index, "elapsed_seconds": round(elapsed, 1),
                    "server_confirmed_online": online_count,
                    "combat_confirmed_agents": sum(1 for value in outcomes.values()
                        if value["combat_confirmed"]),
                    "agents_with_server_observed_attack_damage": sum(1 for value in outcomes.values()
                        if value["server_observed_attack_damage"] > 0),
                    "openkore_agents_with_nearby_monsters": sum(
                        1 for state in latest_states.values()
                        if state and state.get("nearby_monsters")),
                    "openkore_agents_with_attack_ai": sum(
                        1 for state in latest_states.values()
                        if state and any("attack" in str(action).casefold()
                                         for action in state.get("native_ai_queue", []))),
                    "openkore": rss})
                self._sample_server_resources(report, home, owner, elapsed)
                time.sleep(min(sample_interval, max(0.0, end - time.monotonic())))
                if time.monotonic() < end:
                    continue
                duration = time.monotonic() - acceptance_started
                if duration < window_seconds:
                    raise RuntimeError("acceptance window ended before its monotonic duration requirement")
                if terminal_failures:
                    failed_ids = sorted(terminal_failures)
                    failure = "terminal individual agent failure(s): " + ",".join(failed_ids)
                    report.update({"acceptance": "FAILED", "failure": failure,
                        "acceptance_window_actual_seconds": round(duration, 2),
                        "acceptance_started_utc": start_wall.isoformat(),
                        "minimum_concurrent_online": minimum_online,
                        "peak_concurrent_online": peak_online,
                        "terminal_agent_failures": list(terminal_failures.values()),
                        "characters_with_server_confirmed_combat": sum(
                            1 for value in outcomes.values() if value["combat_confirmed"])})
                    break
                if any(value["movement"] != "native_movement_observed" for value in outcomes.values()):
                    raise RuntimeError("one or more agents did not demonstrate native movement")
                missing = _unconfirmed_combat_agents(agents, outcomes)
                if missing:
                    if combat_retry_cycles >= 2:
                        raise RuntimeError("server did not confirm combat after retrying only: "
                            + ",".join(agent["id"] for agent in missing))
                    failed_ids = [agent["id"] for agent in missing]
                    report.setdefault("combat_profile_retries", []).append({
                        "agents": failed_ids,
                        "restarted_only_failed_profiles": True,
                        "successful_sessions_preserved": count - len(missing),
                        "acceptance_window_restarted": True,
                        "next_window": window_index + 1})
                    for agent in _recovery_targets(agents, failed_ids):
                        result = outcomes[agent["id"]]
                        result["server_observed_attack_damage"] = 0
                        result["server_observed_attack_count"] = 0
                        result["combat_events"] = 0
                        result["combat_confirmed"] = False
                        observed_mob_damage[agent["id"]].clear()
                        observed_mob_attacks[agent["id"]].clear()
                        recover_agent(agent, home)
                        observer_cursors[agent["id"]] = len(
                            adapters[agent["id"]]._observer_rows())
                    online_rows = _sql_rows(self.lab, home,
                        f"SELECT char_id,online FROM `char` WHERE char_id IN ({ids})")
                    online = {int(row[0]): row[1] == "1" for row in online_rows}
                    if sum(online.values()) != count:
                        raise RuntimeError(
                            f"rAthena confirmed only {sum(online.values())}/{count} sessions after combat retry")
                    for agent in agents:
                        aid = agent["id"]
                        outcomes[aid]["server_observed_attack_damage"] = 0
                        outcomes[aid]["server_observed_attack_count"] = 0
                        outcomes[aid]["combat_events"] = 0
                        outcomes[aid]["combat_confirmed"] = False
                        observed_mob_damage[aid].clear()
                        observed_mob_attacks[aid].clear()
                        observer_cursors[aid] = len(adapters[aid]._observer_rows())
                    combat_retry_cycles += 1
                    prior_events = _sql_rows(self.lab, home,
                        "SELECT COALESCE(MAX(event_id),0) FROM korelab_combat_events")
                    prior_event_id = int(prior_events[0][0]) if prior_events else 0
                    start_wall = datetime.now(timezone.utc)
                    acceptance_started = time.monotonic()
                    end = acceptance_started + window_seconds
                    window_index += 1
                    minimum_online, peak_online = count, count
                    continue
                if saved_roster or diverse_roster:
                    missing_skills = [agent["id"] for agent in
                                      _unconfirmed_skill_agents(agents, outcomes)]
                    report["characters_with_server_confirmed_offensive_skill_use"] = (
                        count - len(missing_skills))
                    report["characters_without_server_confirmed_offensive_skill_use"] = missing_skills
                    if missing_skills:
                        failure = "server-confirmed offensive skill use was not observed for: " + ",".join(missing_skills)
                        report.update({"acceptance": "FAILED", "failure": failure})
                        break
                report.update({"acceptance": "PASS", "acceptance_window_actual_seconds": round(duration, 2),
                    "acceptance_started_utc": start_wall.isoformat(), "minimum_concurrent_online": minimum_online,
                    "peak_concurrent_online": peak_online,
                    "characters_with_server_confirmed_combat": sum(1 for value in outcomes.values()
                        if value["combat_confirmed"]), "characters_with_native_movement": sum(1 for value in outcomes.values()
                        if value["movement"] != "pending")})
                break
        except BaseException as exc:
            failure = f"{type(exc).__name__}: {exc}"
            report["acceptance"] = "FAILED"
            report["failure"] = failure
        finally:
            teardown_started = time.monotonic()
            cleanup_errors = []
            for agent_id, adapter in adapters.items():
                try:
                    from .adapters.openkore import redact as redact_openkore_output
                    safe_output = redact_openkore_output(adapter.output, adapter.sensitive_values)
                    outcomes[agent_id]["native_openkore_diagnostics"] = [
                        line.strip()[:300] for line in safe_output.splitlines()
                        if re.search(r"(?i)random route|lockmap route|attack|target|monster|path|stuck|disconnect|error", line)
                    ][-40:]
                    final_state = _latest_state(adapter)
                    if final_state:
                        outcomes[agent_id]["final_openkore_state"] = {
                            "connected_in_game": final_state.get("connected_in_game"),
                            "map": final_state.get("map"),
                            "position": final_state.get("server_packet_position_from"),
                            "is_dead": final_state.get("character_attributes", {}).get("is_dead"),
                            "native_ai_config": final_state.get("native_ai_config", {}),
                            "ai_queue": final_state.get("native_ai_queue", []),
                            "native_attack_target": final_state.get("native_attack_target"),
                            "auto_attack_candidate_count": int(
                                final_state.get("native_auto_attack_candidate_count", 0)),
                            "best_target_candidate_count": int(
                                final_state.get("native_best_target_candidate_count", 0)),
                            "nearby_monsters": [{"name": mob.get("name"),
                                "client_index": mob.get("client_index"),
                                "damage_from_you": int(mob.get("damage_from_you", 0) or 0),
                                "attacks_from_you": int(mob.get("attacks_from_you", 0) or 0)}
                                for mob in final_state.get("nearby_monsters", [])]}
                    safe_output = redact_openkore_output(adapter.output, adapter.sensitive_values)
                    diagnostics = [line.strip()[:400] for line in safe_output.splitlines()
                        if re.search(r"(?i)attack|monster|mob|route|path|stuck|target|disconnect|connection|error", line)]
                    if diagnostics:
                        outcomes[agent_id]["openkore_diagnostics"] = diagnostics[-30:]
                    adapter.close()
                    reader = output_readers.get(agent_id)
                    if reader:
                        reader.join(timeout=2)
                    if adapter.process and adapter.process.poll() is None:
                        cleanup_errors.append(f"OpenKore process remains: {agent_id}")
                    else:
                        outcomes[agent_id]["process_terminated"] = True
                except Exception:
                    cleanup_errors.append(f"OpenKore cleanup failed: {agent_id}")
            if home.exists():
                ids = [str(agent.get("char_id")) for agent in owner.get("agents", [])
                    if agent.get("char_id")]
                try:
                    if ids:
                        offline = []
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            offline = _sql_rows(self.lab, home,
                                f"SELECT COUNT(*) FROM `char` WHERE char_id IN ({','.join(ids)}) AND online<>0")
                            if offline and offline[0][0] == "0":
                                break
                            time.sleep(.5)
                        report["cleanup"]["server_online_after_logout"] = 0 if offline and offline[0][0] == "0" else None
                        if report["cleanup"]["server_online_after_logout"] != 0:
                            cleanup_errors.append("rAthena did not confirm all characters offline after logout")
                        final_rows = _sql_rows(self.lab, home,
                            f"SELECT char_id,last_map,last_x,last_y,online FROM `char` WHERE char_id IN ({','.join(ids)})")
                        final_state = {int(row[0]): row for row in final_rows}
                        for agent in owner.get("agents", []):
                            if agent.get("char_id") and int(agent["char_id"]) in final_state:
                                row = final_state[int(agent["char_id"])]
                                outcomes[agent["id"]]["saved_server_position_after_logout"] = {
                                    "map": row[1], "x": int(row[2]), "y": int(row[3]),
                                    "online": int(row[4])}
                                outcomes[agent["id"]]["server_persisted_lock_map"] = (
                                    row[1] == outcomes[agent["id"]]["lock_map"])
                except Exception as exc:
                    report["cleanup"]["server_online_after_logout"] = None
                    report["cleanup"]["verification_error"] = type(exc).__name__
                try:
                    stopped = self.lab.stop(lab_id)
                    report["cleanup"]["server_stop"] = stopped
                    if stopped.get("unconfirmed"):
                        cleanup_errors.append("managed rAthena process cleanup unconfirmed")
                        report["cleanup"]["laboratory_destroyed"] = False
                    else:
                        destroyed = self.lab.destroy(lab_id)
                        report["cleanup"]["laboratory_destroyed"] = destroyed.get("destroyed") is True
                except Exception as exc:
                    cleanup_errors.append(f"managed lab cleanup failed: {type(exc).__name__}")
                    report["cleanup"]["laboratory_destroyed"] = False
            report["cleanup"]["errors"] = cleanup_errors
            stage_timings["acceptance_window_seconds"] = round(
                float(report.get("acceptance_window_actual_seconds", 0.0)), 3)
            stage_timings["laboratory_teardown_seconds"] = round(
                time.monotonic() - teardown_started, 3)
            if cleanup_errors:
                report["acceptance"] = "FAILED_CLEANUP_UNCONFIRMED"
            report["total_agent_process_samples"] = len(samples)
            self._write_report(report)
        if failure and report["acceptance"] == "FAILED":
            return report
        return report

    def _sample_server_resources(self, report: dict, home: Path, owner: dict,
                                 elapsed: float) -> None:
        service_pids = [int(record["pid"]) for record in owner.get("services", {}).values()
                        if record.get("pid")]
        sample = _resource_snapshot(service_pids)
        report.setdefault("server_resource_samples", []).append({
            "elapsed_seconds": round(elapsed, 1), **sample})

    def _write_report(self, report: dict) -> None:
        root = self.lab.root / "reports" / "fleets"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        set_private_permissions(root, 0o700)
        target = root / f"{report['lab_id']}.json"
        report["report_path"] = str(target)
        _atomic_json(target, report)

