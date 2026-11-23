from __future__ import annotations

import json
import hashlib
import pathlib
import subprocess
import sys
import tempfile
import unittest

from korelab.agent import (OpenKoreAgentSession, UnsupportedOperation, build_move_eventmacro,
                           execute_interactive_request, export_eventmacro_source,
                           validate_eventmacro_source)
from korelab.adapters.openkore import OpenKoreAdapter
from korelab.knowledge import KnowledgeError, KnowledgeStore
from korelab.openkore_profile import ProfileError, find_server_profile, validate_selection


class FakeAdapter:
    def __init__(self):
        self.output = ""
        self.sensitive_values = ()
        self.calls = []
        self.reads = 0
        self.report_running_macro = False
        self.running_macro_name = None
        self.macro_markers = "KORELAB_EVENTMACRO_STARTED\nKORELAB_EVENTMACRO_PROGRESS\nKORELAB_EVENTMACRO_COMPLETED\n"
        self.current_state = {"type": "state", "connected_in_game": True, "map": "iz_int01",
                              "inventory_observed": True, "inventory": [],
                              "client_estimated_position": {"x": 18, "y": 26}, "movement_state": "stationary"}

    def command(self, value):
        self.calls.append(value)
        self.output += f"\n{value}\nSyntheticCharacter is at iz_int01\n"
        if value.startswith("eventMacro ") and value not in {"eventMacro status", "eventMacro stop"}:
            self.report_running_macro = True
            self.running_macro_name = value.split(maxsplit=1)[1]
        if value.startswith("eventMacro "):
            self.output += self.macro_markers

    def set_config_value(self, key, value, timeout=10):
        self.calls.append(f"conf {key} {value}")
        return {"key": key, "native_response": f"Config '{key}' set to {value} (was 8)",
                "plugin_events": ["[lockMapSweep] Invalidated pending route"]}

    def wait_position_change(self, map_name, x, y, timeout=30):
        return {"status": "OBSERVED", "from": {"map": map_name, "x": x, "y": y},
                "observation": {**self.current_state, "client_estimated_position": {"x": x, "y": y + 1}},
                "completion": "observed position changed"}

    def _read(self, timeout=0.1):
        self.reads += 1

    def capture_command(self, value, *, timeout=5, quiet_period=0.35):
        self.command(value)
        if value == "eventMacro status":
            if self.report_running_macro:
                return f"There's a macro currently running\nMacro name: {self.running_macro_name or 'unrelated_macro'}\n"
            return "There's no macro currently running.\n"
        if value == "eventMacro stop":
            self.report_running_macro = False
            self.running_macro_name = None
            return "Stopping macro\n"
        return f"{value}\nSyntheticCharacter is at iz_int01\n"

    def move(self, map_name, target, timeout):
        self.current_state.update({"map": map_name, "client_estimated_position": target, "movement_state": "stationary"})
        return self.current_state

    def observe_npc_dialogue(self, name, timeout):
        return {"status": "OBSERVED", "npc": name, "position": {"x": 19, "y": 26},
                "responses": ["Ask about the quest", "Cancel Chat"],
                "completion": "live NPC dialogue responses captured; no choice selected"}

    def collect_monster_drop(self, item_id, item_name, timeout=20, client_index=None):
        self.calls.append(("take", item_id, item_name, client_index))
        return {"status": "PASS", "item_id": item_id, "item_name": item_name,
                "client_index": client_index, "inventory_after": 1}

    def spawn_monster(self, aegis_name, test_name, event_label, timeout=15,
                      require_clear=False, count=1):
        self.calls.append(("spawn", aegis_name, test_name, event_label, count))
        return {"connected_in_game": True,
                "nearby_monsters": [{"name": test_name, "client_index": index}
                                     for index in range(count)]}

    def load_npc(self, name, relative_file, timeout=30):
        self.calls.append(("load_npc", name, relative_file))
        return {"state": {**self.current_state, "nearby_npcs": [{"name": name}]},
                "npc_observed": True, "refreshed_after_load": True}

    def unload_npc(self, name, timeout=15):
        self.calls.append(("unload_npc", name))

    def unload_npc_file(self, relative_file, name, timeout=20):
        self.calls.append(("unload_npc_file", relative_file, name))
        return {"state": {**self.current_state, "nearby_npcs": []},
                "native_response": "File unloaded.", "evidence": "fixture source unloaded"}

    def _observer_rows(self):
        return [self.current_state]

    def state_after(self, sample_count, predicate, timeout=15):
        row = dict(self.current_state)
        assert predicate(row)
        return row

    def replace_eventmacro_source(self, source, expected_names, timeout=10):
        return {"status": "LOADED", "revision": 1, "macro_names_verified": expected_names}

    def state(self, predicate, timeout=15):
        row = dict(self.current_state)
        assert predicate(row)
        return row

    def replace_eventmacro_source(self, source, expected_names, timeout=10):
        return {"status": "LOADED", "revision": 2, "macro_names_verified": expected_names,
                "source": str(source), "evidence": "native eventMacro_file reload followed by eventMacro list"}


class AgentFoundationTests(unittest.TestCase):
    def test_agent_cli_import_does_not_load_rathena_adapter(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-c",
            "import sys, korelab.cli, korelab.runner; "
            "assert 'korelab.adapters.rathena' not in sys.modules"],
            cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        help_result = subprocess.run([sys.executable, "-m", "korelab", "agent-session", "--help"],
            cwd=root, capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--interactive", help_result.stdout)
        self.assertIn("--session-duration", help_result.stdout)

    def test_capabilities_report_only_available_session_operations(self):
        catalog = OpenKoreAgentSession.capabilities()
        self.assertIn("no rAthena", catalog["runtime_dependency"])
        self.assertIn("move", {row["name"] for row in catalog["capabilities"]})
        profile = next(row for row in catalog["capabilities"] if row["name"] == "runtime_profile")
        self.assertIn("openkore-profile", profile["authorization"])
        self.assertIn("required_inputs", next(row for row in catalog["capabilities"] if row["name"] == "npc_dialogue"))
        self.assertIn("wait-reconnect", {row["name"] for row in catalog["capabilities"]})
        self.assertIn("eventmacro-start", {row["name"] for row in catalog["capabilities"]})
        self.assertIn("eventmacro-stop", {row["name"] for row in catalog["capabilities"]})
        self.assertIn("eventmacro_status", __import__("korelab.agent", fromlist=["READ_COMMANDS"]).READ_COMMANDS)
        self.assertIn("eventmacro_inspect_automacro", {row["name"] for row in catalog["capabilities"]})
        self.assertIn("lockmapsweep_status", {row["name"] for row in catalog["capabilities"]})
        self.assertIn("ai-mode", {row["name"] for row in catalog["capabilities"]})
        self.assertIn("pickup-item", {row["name"] for row in catalog["capabilities"]})

    def test_read_command_returns_openkore_response_and_unsupported_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            session = OpenKoreAgentSession(FakeAdapter(), KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = session.execute_read_only("where")
            self.assertEqual(result["status"], "OBSERVED")
            self.assertTrue(result["response_contract_matched"])
            self.assertIn("SyntheticCharacter is at iz_int01", result["response"])
            with self.assertRaises(UnsupportedOperation):
                session.execute_read_only("drop-all-items")

    def test_read_command_does_not_accept_unrelated_console_output_as_its_response(self):
        class InterleavedAdapter(FakeAdapter):
            def capture_command(self, value, *, timeout=5, quiet_period=0.35):
                self.command(value)
                return "Connecting to Character Server... connected\\nReceived characters from Character Server\\n"

        with tempfile.TemporaryDirectory() as directory:
            session = OpenKoreAgentSession(InterleavedAdapter(), KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = session.execute_read_only("help")
            self.assertEqual(result["status"], "NO_RESPONSE")
            self.assertFalse(result["response_contract_matched"])
            self.assertEqual(result["response_classification"], "unclassified_console_output")

    def test_eventmacro_auto_inspection_is_read_only_and_name_bounded(self):
        class AutomacroAdapter(FakeAdapter):
            def capture_command(self, value, *, timeout=5, quiet_period=0.35):
                self.calls.append(value)
                return ("[eventMacro] Printing information about automacro 'loot_gate'.\\n"
                        "Number of false conditions: '1'\\n"
                        "1 - InInventory: 'false'\\n")

        with tempfile.TemporaryDirectory() as directory:
            adapter = AutomacroAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = session.execute_read_only("eventmacro_auto:loot_gate")
            self.assertEqual(result["status"], "OBSERVED")
            self.assertIn("eventMacro auto loot_gate", adapter.calls)
            self.assertIn("InInventory: 'false'", result["response"])
            with self.assertRaises(UnsupportedOperation):
                session.execute_read_only("eventmacro_auto:loot_gate; quit")

    def test_reference_plugin_status_is_an_allowlisted_read_with_source_specific_response(self):
        class PluginAdapter(FakeAdapter):
            def capture_command(self, value, *, timeout=5, quiet_period=0.35):
                self.calls.append(value)
                return "[lockMapSweep] ready=1 map=iz_int01 block=2/4 visit=1/1 visitedBlocks=1\\n[lockMapSweep] pending=20,26\\n"

        with tempfile.TemporaryDirectory() as directory:
            adapter = PluginAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = session.execute_read_only("lockmapsweep_status")
            self.assertEqual(result["status"], "OBSERVED")
            self.assertEqual(adapter.calls, ["lockMapSweep status"])

    def test_ai_mode_transition_requires_explicit_authorization_and_native_confirmation(self):
        class ModeAdapter(FakeAdapter):
            def set_ai_mode(self, mode, timeout=10):
                self.calls.append(f"ai {mode}")
                return {"mode": mode, "native_response": "AI set to manual mode"}

        with tempfile.TemporaryDirectory() as directory:
            session = OpenKoreAgentSession(ModeAdapter(), KnowledgeStore(pathlib.Path(directory), "test-server"))
            request = {"action": "ai-mode", "mode": "manual"}
            with self.assertRaises(PermissionError):
                execute_interactive_request(session, request, authorized=set(), loaded_macros=set())
            result = execute_interactive_request(session, request, authorized={"ai-mode"}, loaded_macros=set())
            self.assertEqual(result["result"]["status"], "OBSERVED")
            self.assertEqual(result["result"]["mode"], "manual")

    def test_config_set_is_authorized_declared_bounded_and_reports_only_native_application(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"),
                mutable_config_keys={"lockMapSweep_randY"})
            request = {"action": "config-set", "key": "lockMapSweep_randY", "value": 0}
            with self.assertRaises(PermissionError):
                execute_interactive_request(session, request, authorized=set(), loaded_macros=set())
            with self.assertRaises(UnsupportedOperation):
                session.perform_action("config-set", inputs={"key": "lockMapSweep_enabled", "value": 0},
                    authorized={"openkore-config"})
            session.mutable_config_keys.add("server_password")
            with self.assertRaises(UnsupportedOperation):
                session.perform_action("config-set", inputs={"key": "server_password", "value": "secret"},
                    authorized={"openkore-config"})
            with self.assertRaises(ValueError):
                session.perform_action("config-set", inputs={"key": "lockMapSweep_randY", "value": "8; quit"},
                    authorized={"openkore-config"})
            result = execute_interactive_request(session, request,
                authorized={"openkore-config"}, loaded_macros=set())
            self.assertEqual(result["result"]["status"], "APPLIED")
            self.assertIn("Config 'lockMapSweep_randY' set to 0", result["result"]["native_response"])
            self.assertIn("post_action_observation", result)
            self.assertEqual(adapter.calls, ["conf lockMapSweep_randY 0"])
            self.assertEqual(result["result"]["plugin_events"], ["[lockMapSweep] Invalidated pending route"])

    def test_interactive_spawn_requires_authorization_and_observed_named_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            request = {"request_id": "spawn-1", "action": "spawn-monsters",
                       "aegis_name": "R_PORING", "count": 3}
            with self.assertRaises(PermissionError):
                execute_interactive_request(session, request, authorized=set(), loaded_macros=set())
            with self.assertRaisesRegex(ValueError, "from 1 to 10"):
                execute_interactive_request(session, {**request, "count": 11},
                    authorized={"spawn-monsters"}, loaded_macros=set())
            result = execute_interactive_request(session, request,
                authorized={"spawn-monsters"}, loaded_macros=set())
            spawn_call = next(call for call in adapter.calls if isinstance(call, tuple) and call[0] == "spawn")
            self.assertEqual(spawn_call[1], "R_PORING")
            self.assertEqual(spawn_call[4], 3)
            self.assertEqual(result["result"]["observed_count"], 3)
            self.assertRegex(result["result"]["test_name"], r"^KoreLabA[0-9]{6}$")
            self.assertIn("post_action_observation", result)

    def test_wait_position_change_uses_structured_observation_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            session = OpenKoreAgentSession(FakeAdapter(), KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = execute_interactive_request(session,
                {"action": "wait-position-change", "map": "iz_int01", "x": 18, "y": 26, "timeout": 3},
                authorized=set(), loaded_macros=set())
            self.assertEqual(result["result"]["status"], "OBSERVED")
            self.assertEqual(result["result"]["observation"]["client_estimated_position"], {"x": 18, "y": 27})
            self.assertEqual(session.adapter.calls, [])

    def test_observation_is_structured_and_stored_with_openkore_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            session = OpenKoreAgentSession(FakeAdapter(), KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = session.observe_and_store()
            self.assertTrue(result["observation"]["connected_in_game"])
            fact = result["knowledge_fact"]
            self.assertEqual(fact["provenance"], "openkore_observed")
            self.assertEqual(fact["status"], "active")
            self.assertEqual(session.knowledge.list_facts()[0]["value"]["map"], "iz_int01")

    def test_observe_waits_for_a_new_structured_state_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            calls = []
            def state_after(sample_count, predicate, timeout=15):
                calls.append(sample_count)
                row = {**adapter.current_state, "time": "new-sample"}
                assert predicate(row)
                return row
            adapter.state_after = state_after
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = session.observe()
            self.assertEqual(calls, [1])
            self.assertEqual(result["time"], "new-sample")

    def test_gameplay_action_permissions_and_observed_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            with self.assertRaises(PermissionError):
                session.perform_action("move", inputs={"map": "iz_int01", "x": 20, "y": 26})
            moved = session.perform_action("move", inputs={"map": "iz_int01", "x": 20, "y": 26},
                                           authorized={"move"})
            self.assertEqual(moved["status"], "VERIFIED")
            self.assertEqual(moved["observations"]["client_estimated_position"], {"x": 20, "y": 26})
            with self.assertRaises(PermissionError):
                session.perform_action("npc-dialogue", inputs={"npc": "Guide"})
            dialogue = session.perform_action("npc-dialogue", inputs={"npc": "Guide"}, authorized={"npc"})
            self.assertEqual(dialogue["responses"][0], "Ask about the quest")
            self.assertEqual(dialogue["completion"], "live NPC dialogue responses captured; no choice selected")

    def test_cross_map_move_requires_declared_transition_and_confirms_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            adapter.scenario = {"openkore": {"map_transitions": [
                {"from": "iz_int01", "to": "prt_fild08"}]}}
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            moved = session.perform_action("move", inputs={"map": "prt_fild08", "x": 369, "y": 210},
                                           authorized={"move"})
            self.assertEqual(moved["status"], "VERIFIED")
            self.assertTrue(moved["map_transition"])
            self.assertEqual(moved["previous_map"], "iz_int01")
            adapter.scenario = {"openkore": {"map_transitions": []}}
            with self.assertRaisesRegex(ValueError, "scenario-declared"):
                session.perform_action("move", inputs={"map": "izlude", "x": 24, "y": 98},
                                       authorized={"move"})

    def test_interactive_requests_reuse_session_and_enforce_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            observed = execute_interactive_request(session, {"request_id": "r1", "action": "observe"},
                authorized=set(), loaded_macros=set())
            self.assertEqual(observed["request_id"], "r1")
            self.assertTrue(session.knowledge.list_facts())
            with self.assertRaises(PermissionError):
                execute_interactive_request(session, {"action": "move", "map": "iz_int01", "x": 21, "y": 26},
                    authorized=set(), loaded_macros=set())
            moved = execute_interactive_request(session, {"action": "move", "map": "iz_int01", "x": 21, "y": 26},
                authorized={"move"}, loaded_macros=set())
            self.assertEqual(moved["result"]["status"], "VERIFIED")
            self.assertEqual(moved["post_action_observation"]["observation"]["client_estimated_position"],
                             {"x": 21, "y": 26})
            self.assertEqual(moved["post_action_observation"]["knowledge_fact"]["provenance"],
                             "openkore_observed")
            with self.assertRaises(UnsupportedOperation):
                execute_interactive_request(session, {"action": "eventmacro", "name": "unknown"},
                    authorized={"eventmacro"}, loaded_macros={"korelab_probe"})
            with self.assertRaisesRegex(ValueError, "unsupported interactive request fields"):
                execute_interactive_request(session, {"action": "observe", "command": "raw"},
                    authorized=set(), loaded_macros=set())

    def test_interactive_pickup_requires_authorization_and_observed_item_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            request = {"request_id": "pick-1", "action": "pickup-item", "item_id": 7049,
                       "item_name": "Stone", "client_index": 2}
            with self.assertRaises(PermissionError):
                execute_interactive_request(session, request, authorized=set(), loaded_macros=set())
            with self.assertRaisesRegex(ValueError, "ground-item index"):
                execute_interactive_request(session, {**request, "client_index": -1},
                    authorized={"pickup-item"}, loaded_macros=set())
            result = execute_interactive_request(session, request,
                authorized={"pickup-item"}, loaded_macros=set())
            self.assertEqual(result["result"]["status"], "PASS")
            self.assertIn(("take", 7049, "Stone", 2), adapter.calls)
            self.assertIn("post_action_observation", result)

    def test_native_pickup_fails_fast_when_observed_ground_item_index_is_stale(self):
        adapter = object.__new__(OpenKoreAdapter)
        adapter.deadline = __import__("time").monotonic() + 5
        adapter.state = lambda predicate, timeout=15: {
            "connected_in_game": True, "inventory": [],
            "nearby_ground_items": [{"item_id": 7049, "amount": 1, "client_index": 1}]}
        adapter.command = lambda command: self.fail(f"stale pickup must not issue {command}")
        with self.assertRaisesRegex(RuntimeError, "no longer visible"):
            adapter.collect_monster_drop(7049, "Stone", client_index=2)

    def test_interactive_test_npc_fixture_load_and_unload_are_bounded_and_authorized(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            request = {"request_id": "load-npc", "action": "load-test-npc",
                       "npc_name": "KoreLabTestNpc", "relative_file": "npc/custom/korelab_test_npc.txt"}
            with self.assertRaises(PermissionError):
                execute_interactive_request(session, request, authorized=set(), loaded_macros=set())
            with self.assertRaisesRegex(ValueError, "relative npc/custom"):
                execute_interactive_request(session, {**request, "relative_file": "../../secrets.txt"},
                    authorized={"npc-fixture"}, loaded_macros=set())
            loaded = execute_interactive_request(session, request,
                authorized={"npc-fixture"}, loaded_macros=set())
            self.assertEqual(loaded["result"]["status"], "OBSERVED")
            self.assertIn(("load_npc", "KoreLabTestNpc", "npc/custom/korelab_test_npc.txt"), adapter.calls)
            unloaded = execute_interactive_request(session,
                {"request_id": "unload-npc", "action": "unload-test-npc", "npc_name": "KoreLabTestNpc",
                 "relative_file": "npc/custom/korelab_test_npc.txt"},
                authorized={"npc-fixture"}, loaded_macros=set())
            self.assertEqual(unloaded["result"]["status"], "OBSERVED")
            self.assertIn(("unload_npc_file", "npc/custom/korelab_test_npc.txt", "KoreLabTestNpc"), adapter.calls)

    def test_native_npc_fixture_rejection_fails_fast_on_command_evidence(self):
        adapter = object.__new__(OpenKoreAdapter)
        adapter.output = ""
        adapter.state = lambda predicate, timeout=15: {"connected_in_game": True, "nearby_npcs": []}
        adapter.command = lambda command: None
        class Match:
            def group(self, _index): return "File not found."
        adapter._wait_after = lambda pattern, cursor, timeout=20: Match()
        with self.assertRaisesRegex(RuntimeError, "rejected controlled NPC source unload: File not found"):
            adapter.unload_npc_file("npc/custom/korelab_missing.txt", "KoreLabMissing")

        class LoadMatch:
            def group(self, _index): return "Script could not be loaded."
        adapter._wait_after = lambda pattern, cursor, timeout=15: LoadMatch()
        with self.assertRaisesRegex(RuntimeError, "rejected controlled NPC fixture load"):
            adapter.load_npc("KoreLabMissing", "npc/custom/korelab_missing.txt")

    def test_interactive_wait_reconnect_requires_observed_disconnect_then_native_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            history = [dict(adapter.current_state)]
            def read(_timeout=0.1):
                if len(history) == 1:
                    history.append({"type": "state", "connected_in_game": False,
                                    "time": "2026-11-15T00:00:01Z"})
                elif len(history) == 2:
                    history.append({**adapter.current_state, "time": "2026-11-15T00:00:03Z"})
            adapter._observer_rows = lambda: history
            adapter._read = read
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = execute_interactive_request(session, {"request_id": "reconnect-1",
                "action": "wait-reconnect", "timeout": 5}, authorized=set(), loaded_macros=set())
            self.assertEqual(result["result"]["status"], "VERIFIED")
            self.assertEqual(result["result"]["observation"]["map"], "iz_int01")
            self.assertEqual(result["result"]["knowledge_fact"]["provenance"], "openkore_observed")

    def test_interactive_wait_reconnect_timeout_does_not_report_success_without_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = execute_interactive_request(session, {"action": "wait-reconnect", "timeout": 1},
                authorized=set(), loaded_macros=set())
            self.assertEqual(result["result"]["status"], "TIMEOUT")
            self.assertEqual(result["result"]["phase"], "disconnect_not_observed")
            self.assertFalse(session.knowledge.list_facts())

    def test_interactive_macro_start_is_not_success_and_stop_is_session_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            with self.assertRaises(PermissionError):
                execute_interactive_request(session, {"action": "eventmacro-start", "name": "korelab_probe"},
                    authorized=set(), loaded_macros={"korelab_probe"})
            started = execute_interactive_request(session,
                {"action": "eventmacro-start", "name": "korelab_probe"},
                authorized={"eventmacro"}, loaded_macros={"korelab_probe"})
            self.assertEqual(started["result"]["status"], "START_REQUESTED")
            self.assertNotEqual(started["result"]["status"], "COMPLETED")
            stopped = execute_interactive_request(session, {"action": "eventmacro-stop"},
                authorized={"eventmacro"}, loaded_macros={"korelab_probe"})
            self.assertEqual(stopped["result"]["status"], "VERIFIED")
            self.assertEqual(stopped["result"]["lifecycle_status"], "INTERRUPTED")
            self.assertFalse(session.session_owned_macro_active)

    def test_interactive_macro_stop_does_not_stop_unowned_macro(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = execute_interactive_request(session, {"action": "eventmacro-stop"},
                authorized={"eventmacro"}, loaded_macros=set())
            self.assertEqual(result["result"]["status"], "NO_SESSION_MACRO")
            self.assertNotIn("eventMacro stop", adapter.calls)

    def test_interactive_macro_stop_does_not_stop_a_different_macro(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            adapter.report_running_macro = True
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            session.session_owned_macro_active = True
            session.session_owned_macro_name = "korelab_probe"
            result = execute_interactive_request(session, {"action": "eventmacro-stop"},
                authorized={"eventmacro"}, loaded_macros=set())
            self.assertEqual(result["result"]["status"], "OWNERSHIP_MISMATCH")
            self.assertNotIn("eventMacro stop", adapter.calls)

    def test_interactive_macro_start_refuses_to_take_over_an_existing_native_macro(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            adapter.report_running_macro = True
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = execute_interactive_request(session,
                {"action": "eventmacro-start", "name": "korelab_probe"},
                authorized={"eventmacro"}, loaded_macros={"korelab_probe"})
            self.assertEqual(result["result"]["status"], "REJECTED_ACTIVE_MACRO")
            self.assertFalse(session.session_owned_macro_active)
            self.assertNotIn("eventMacro korelab_probe", adapter.calls)

    def test_interactive_macro_reload_uses_native_plugin_and_updates_loaded_names(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "replacement.txt"
            source.write_text("macro korelab_corrected {\n    do where\n}\n", encoding="utf-8")
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory) / "knowledge", "test-server"))
            loaded = {"korelab_old"}
            result = execute_interactive_request(session,
                {"action": "eventmacro-reload", "source": str(source)},
                authorized={"eventmacro"}, loaded_macros=loaded)
            self.assertEqual(result["result"]["status"], "LOADED")
            self.assertEqual(loaded, {"korelab_corrected"})
            self.assertIn("source_sha256", result["result"])

    def test_interactive_macro_reload_fails_closed_while_any_macro_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "replacement.txt"
            source.write_text("macro korelab_corrected {\n    do where\n}\n", encoding="utf-8")
            adapter = FakeAdapter()
            adapter.report_running_macro = True
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory) / "knowledge", "test-server"))
            result = session.reload_eventmacro(source, authorized=True)
            self.assertEqual(result["status"], "REJECTED_ACTIVE_MACRO")
            self.assertNotIn("eventMacro stop", adapter.calls)

    def test_eventmacro_completion_is_separate_from_structured_objective_result(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            adapter.current_state.update({"map": "iz_int01", "client_estimated_position": {"x": 20, "y": 26}})
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            result = session.run_eventmacro("korelab_probe", authorized=True, timeout=0.1,
                expected_map="iz_int01", expected_position={"x": 20, "y": 26})
            self.assertEqual(result["lifecycle_status"], "COMPLETED")
            self.assertEqual(result["objective_status"], "VERIFIED")

    def test_eventmacro_requires_explicit_authorization_and_native_completion_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter()
            session = OpenKoreAgentSession(adapter, KnowledgeStore(pathlib.Path(directory), "test-server"))
            with self.assertRaises(PermissionError):
                session.run_eventmacro("korelab_probe")
            adapter.output = "eventMacro korelab_probe\nKORELAB_EVENTMACRO_STARTED\nKORELAB_EVENTMACRO_PROGRESS\nKORELAB_EVENTMACRO_COMPLETED\n"
            result = session.run_eventmacro("korelab_probe", authorized=True, timeout=0.1)
            self.assertEqual(result["lifecycle_status"], "COMPLETED")
            self.assertEqual(result["objective_status"], "NOT_CHECKED")
            self.assertEqual([r["state"] for r in result["lifecycle"]], ["started", "progress", "completed"])
            adapter.macro_markers = "KORELAB_EVENTMACRO_STARTED\nKORELAB_EVENTMACRO_FAILED\n"
            timed_out = session.run_eventmacro("korelab_probe", authorized=True, timeout=0.01)
            self.assertEqual(timed_out["lifecycle_status"], "FAILED")
            self.assertIn("failed", [row["state"] for row in timed_out["lifecycle"]])
            adapter.macro_markers = ""
            timed_out = session.run_eventmacro("korelab_probe", authorized=True, timeout=0.01)
            self.assertEqual(timed_out["lifecycle_status"], "TIMEOUT")
            self.assertIn("timeout", [row["state"] for row in timed_out["lifecycle"]])

    def test_knowledge_keeps_provenance_conflicts_and_stale_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(pathlib.Path(directory), "test-server")
            first = store.add(entity_type="npc", entity_id="Healer", attribute="map", value="prontera",
                              provenance="user_provided", evidence="task brief")
            second = store.add(entity_type="npc", entity_id="Healer", attribute="map", value="izlude",
                               provenance="openkore_observed", evidence="observer sample 12")
            self.assertEqual(first["status"], "active")
            rows = store.list_facts()
            self.assertEqual([row["status"] for row in rows], ["conflicted", "conflicted"])
            self.assertEqual(rows[1]["provenance"], "openkore_observed")
            stale = store.mark_stale(second["fact_id"], reason="server changed after observation")
            self.assertEqual(stale["status"], "stale")
            self.assertEqual(json.loads(store.path.read_text().splitlines()[0])["provenance"], "user_provided")

    def test_knowledge_export_import_is_server_scoped_append_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = KnowledgeStore(root / "source", "test-server")
            source_fact = source.add(entity_type="npc", entity_id="Quest Clerk", attribute="map",
                value="prontera", provenance="openkore_observed", evidence="runs/observation.json")
            exported_path = root / "shared/knowledge.json"
            exported = source.export_document(exported_path)
            self.assertEqual(exported["fact_count"], 1)
            self.assertEqual(exported_path.stat().st_mode & 0o777, 0o600)

            destination = KnowledgeStore(root / "destination", "test-server")
            local_fact = destination.add(entity_type="npc", entity_id="Quest Clerk", attribute="map",
                value="izlude", provenance="user_provided", evidence="server notes")
            imported = destination.import_document(exported_path)
            self.assertEqual(imported["fact_count"], 1)
            rows = destination.list_facts()
            imported_fact = next(row for row in rows if row.get("imported_from"))
            self.assertEqual(imported_fact["provenance"], "openkore_observed")
            self.assertEqual(imported_fact["imported_from"]["fact_id"], source_fact["fact_id"])
            self.assertEqual(imported_fact["status"], "conflicted")
            self.assertEqual(next(row for row in rows if row["fact_id"] == local_fact["fact_id"])["status"],
                             "conflicted")
            self.assertEqual(destination.import_document(exported_path)["fact_count"], 0)
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                source.export_document(exported_path)

    def test_knowledge_import_rejects_cross_server_documents_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = KnowledgeStore(root / "source", "server-a")
            source.add(entity_type="map", entity_id="prontera", attribute="walkable",
                       value=True, provenance="agent_inferred", evidence="incomplete route sample")
            export_path = root / "source.json"
            source.export_document(export_path)
            destination = KnowledgeStore(root / "destination", "server-b")
            with self.assertRaisesRegex(KnowledgeError, "server_id"):
                destination.import_document(export_path)
            self.assertEqual(destination.list_facts(), [])

    def test_invalid_provenance_and_path_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(KnowledgeError):
                KnowledgeStore(pathlib.Path(directory), "../elsewhere")
            store = KnowledgeStore(pathlib.Path(directory), "server")
            with self.assertRaises(KnowledgeError):
                store.add(entity_type="item", entity_id="Jellopy", attribute="weight", value=1,
                          provenance="verified", evidence="unknown source")

    def test_caller_eventmacro_source_validation_does_not_mutate_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "caller-macro.txt"
            original = 'macro caller_probe {\n    log CALLER_PROBE\n}\n'
            source.write_text(original, encoding="utf-8")
            self.assertEqual(validate_eventmacro_source(source), ["caller_probe"])
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_profile_selection_reads_existing_server_tables_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "tables").mkdir()
            (root / "tables/servers.txt").write_text("[Remote Test]\nip game.example.org\nport 6900\nserverType kRO\n", encoding="utf-8")
            profile = find_server_profile(root, "Remote Test")
            self.assertEqual(profile, {"name": "Remote Test", "host": "game.example.org", "port": "6900", "server_type": "kRO"})
            self.assertEqual(validate_selection("Remote Test", 0, "Agent Test", 3)["character_slot"], 3)
            with self.assertRaises(ProfileError):
                find_server_profile(root, "Unknown Server")
            with self.assertRaises(ProfileError):
                validate_selection("Remote Test", -1, "Agent Test", 3)

    def test_profile_credentials_are_loaded_for_redaction_but_not_returned(self):
        from korelab.openkore_profile import read_openkore_credentials
        with tempfile.TemporaryDirectory() as directory:
            config = pathlib.Path(directory) / "config.txt"
            config.write_text("master Remote Test\nusername local-user\npassword private-value\nloginPinCode 4321\n", encoding="utf-8")
            settings, secrets = read_openkore_credentials(config)
            self.assertEqual(settings["master"], "Remote Test")
            self.assertEqual(secrets, ("private-value", "4321"))
            self.assertNotIn("private-value", repr({"master": settings["master"], "username": settings["username"]}))

    def test_eventmacro_source_validation_generation_replacement_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first = root / "bad-target.txt"
            first.write_text(build_move_eventmacro("move_probe", 20, 27), encoding="utf-8")
            self.assertEqual(validate_eventmacro_source(first), ["move_probe"])
            replacement = root / "corrected.txt"
            replacement.write_text(build_move_eventmacro("move_probe", 20, 26), encoding="utf-8")
            exported = export_eventmacro_source(replacement, root / "portable")
            manifest = json.loads(pathlib.Path(exported["manifest_path"]).read_text())
            self.assertEqual(manifest["macro_names"], ["move_probe"])
            self.assertEqual(manifest["sha256"], exported["sha256"])
            portable = root / "portable-profile"
            export_eventmacro_source(replacement, portable, runtime_profile={
                "ai_mode": "on",
                "config_overrides": {"lockMap": "prt_fild01", "username": "private-user",
                                     "server": "private-server"},
                "control_file_overrides": {"mon_control.txt": {
                    "mode": "replace", "content": "all 0 0 0\n1002 1 0 0\n"}},
            })
            portable_manifest = json.loads((portable / "manifest.json").read_text())
            requirements = portable_manifest["required_configuration"]
            self.assertEqual(requirements["ai_mode"], "on")
            self.assertEqual(requirements["config_overrides"], {"lockMap": "prt_fild01"})
            self.assertEqual(requirements["omitted_sensitive_config_keys"], ["server", "username"])
            control = portable / "control/mon_control.txt"
            self.assertEqual(control.read_text(), "all 0 0 0\n1002 1 0 0\n")
            self.assertEqual(requirements["control_file_overlays"][0]["sha256"],
                             hashlib.sha256(control.read_bytes()).hexdigest())
            self.assertNotIn("private-user", (portable / "manifest.json").read_text())
            self.assertNotIn("private-server", (portable / "manifest.json").read_text())
            invalid = root / "invalid.txt"
            invalid.write_text("macro broken {\n  log start\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_eventmacro_source(invalid)


if __name__ == "__main__":
    unittest.main()
