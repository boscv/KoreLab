import re
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from korelab.adapters.openkore import OpenKoreAdapter, position
from korelab.adapters.rathena import RathenaAdapter
from korelab.agent import OpenKoreAgentSession
from korelab.runner import (_acquire_run_lock, _apply_declared_inventory_delta,
                            _random_drop_delta_bounds)
from korelab.scenario import validate_scenario_structure


def scenario_fixture(actions, *, openkore=None, **fields):
    """Return a minimal synthetic scenario for schema-level regressions."""
    return {"name": "synthetic_test", "identity": {},
            "initial_state": {"map": "test_map", "position": {"x": 1, "y": 1}},
            "expected": {}, "cleanup": {}, "maximum_duration_seconds": 30,
            "openkore": openkore or {}, "actions": actions, **fields}


def native_ai_fixture(*, quest_count=None):
    actions = []
    if quest_count is not None:
        actions.append({"id": "reload", "type": "reload_questdb"})
    actions.extend([
        {"id": "spawn", "type": "spawn_monster", "count": 1},
        {"id": "observe", "type": "await_native_ai", "spawn_step": "spawn",
         "eventmacro_marker": "SYNTHETIC_DONE", "inventory_delta": {"Test_Item": 1}},
        {"id": "verify", "type": "verify_inventory", "expected": {"inventory_delta": {}}},
    ])
    fields = {"quest_fixture": {"count": quest_count}} if quest_count is not None else {}
    return scenario_fixture(actions, **fields)


class NativeAIAutomationTests(unittest.TestCase):
    def test_position_parser_rejects_boolean_coordinates_and_uses_native_fallback(self):
        state = {"client_estimated_position": {"x": True, "y": 12},
                 "server_packet_position_to": {"x": 8, "y": 9}}
        self.assertEqual(position(state), {"x": 8, "y": 9})

    def test_run_lock_is_exclusive_and_released_by_close(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                "korelab.runner.tempfile.gettempdir", return_value=directory):
            first = _acquire_run_lock("same-resource", "resource is already locked")
            try:
                with self.assertRaisesRegex(RuntimeError, "already locked"):
                    _acquire_run_lock("same-resource", "resource is already locked")
            finally:
                first.close()
            second = _acquire_run_lock("same-resource", "resource is already locked")
            second.close()

    def test_native_equipment_ack_accepts_display_names_ending_in_punctuation(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        before = {"connected_in_game": True,
                  "inventory": [{"item_id": 1701, "client_index": 5, "equipped": 0}]}
        after = {"connected_in_game": True,
                 "inventory": [{"item_id": 1701, "client_index": 5, "equipped": 34}]}
        adapter.output = ""
        adapter.deadline = 9999999999
        adapter._observer_rows = lambda: [before]
        adapter.state = lambda predicate, timeout=15: before
        adapter.command = lambda value: setattr(
            adapter, "output", "You equip Bow [3] (5) - Two-Handed Weapon (type 34)\n")
        adapter._wait_after = lambda pattern, cursor, timeout=15: re.search(
            pattern, adapter.output[cursor:], re.I | re.S) or self.fail(pattern)
        adapter.state_after = lambda sample_count, predicate, timeout=15: after
        result = adapter.equip_item(1701, "Bow [3]", expect_success=True)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["openkore_equipped_mask"], 34)

    def test_native_arrow_equipment_uses_arrow_bullet_server_acknowledgement(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        before = {"connected_in_game": True,
                  "inventory": [{"item_id": 1750, "client_index": 1, "equipped": 0}]}
        after = {"connected_in_game": True,
                 "inventory": [{"item_id": 1750, "client_index": 1, "equipped": 32768}]}
        adapter.output = ""
        adapter.deadline = 9999999999
        adapter._observer_rows = lambda: [before]
        adapter.state = lambda predicate, timeout=15: before
        adapter.command = lambda value: setattr(
            adapter, "output", "Arrow/Bullet equipped: Arrow (1) x 30\n")
        adapter._wait_after = lambda pattern, cursor, timeout=15: re.search(
            pattern, adapter.output[cursor:], re.I | re.S) or self.fail(pattern)
        adapter.state_after = lambda sample_count, predicate, timeout=15: after
        result = adapter.equip_item(1750, "Arrow", expect_success=True)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["openkore_equipped_mask"], 32768)

    def test_rathena_storage_observation_is_account_and_item_scoped_read_only(self):
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        adapter.char_db = "test_char_db"
        queries = []
        adapter._mysql = lambda sql: (queries.append(sql) or
            types.SimpleNamespace(returncode=0, stdout="21\t8\t909\t3\n"))
        rows = adapter.storage_rows(8, (909,))
        self.assertEqual(rows, [{"id": 21, "account_id": 8, "nameid": 909, "amount": 3}])
        self.assertIn("WHERE account_id=8 AND nameid IN (909)", queries[0])
        self.assertTrue(queries[0].startswith("SELECT "))
        with self.assertRaisesRegex(ValueError, "positive account ID"):
            adapter.storage_rows(0, (909,))
        with self.assertRaisesRegex(ValueError, "positive numeric item IDs"):
            adapter.storage_rows(8, (0,))

    def test_character_fixture_allows_bounded_progression_and_rejects_identity_changes(self):
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        calls = []
        adapter._update_character_fields = lambda original, values, expected: calls.append(
            (dict(values), dict(expected)))
        adapter.character = lambda name: {"name": name, "base_level": 25, "class": 4}
        original = {"name": "Fixture", "base_level": 1, "class": 0, "dex": 1}
        prepared = adapter.prepare_character_state(original, {"base_level": 25, "class": 4, "dex": 30})
        self.assertEqual(prepared["base_level"], 25)
        self.assertEqual(calls[0][0], {"base_level": 25, "class": 4, "dex": 30})
        self.assertEqual(calls[0][1], {"base_level": 1, "class": 0, "dex": 1})
        with self.assertRaisesRegex(ValueError, "unsupported identity fields"):
            adapter.prepare_character_state(original, {"char_id": 999})
        with self.assertRaisesRegex(ValueError, "between 1 and 250"):
            adapter.prepare_character_state(original, {"base_level": 251})

    def test_rathena_storage_restoration_is_account_scoped_and_requires_offline_account(self):
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        adapter.char_db = "test_char_db"
        original = [{"id": 21, "account_id": 8, "nameid": 7049, "amount": 2}]
        statements = []
        adapter.online_characters_for_account = lambda account_id: 0
        adapter._mysql = lambda sql: (statements.append(sql) or
            types.SimpleNamespace(returncode=0, stdout=""))
        adapter.storage_rows = lambda account_id, item_ids: list(original)
        restored = adapter.restore_storage_rows(8, (7049,), original)
        self.assertEqual(restored, original)
        self.assertIn("NOT EXISTS (SELECT 1 FROM test_char_db.char WHERE account_id=8 AND online<>0)", statements[0])
        self.assertIn("DELETE FROM test_char_db.storage WHERE account_id=8 AND nameid IN (7049)", statements[0])
        with self.assertRaisesRegex(ValueError, "selected account"):
            adapter.restore_storage_rows(8, (7049,), [{**original[0], "account_id": 9}])
        adapter.online_characters_for_account = lambda account_id: 1
        with self.assertRaisesRegex(RuntimeError, "every account character to be offline"):
            adapter.restore_storage_rows(8, (7049,), original)

    def test_native_cross_map_move_accepts_scenario_target_with_map_metadata(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        adapter.output = ""
        before = {"connected_in_game": True, "map": "prontera",
                  "client_estimated_position": {"x": 156, "y": 26}}
        after = {"connected_in_game": True, "map": "prt_fild08",
                 "client_estimated_position": {"x": 190, "y": 360},
                 "movement_state": "stationary"}
        rows = [before]
        adapter.state = lambda predicate, timeout=15: next(row for row in rows if predicate(row))
        adapter._observer_rows = lambda: rows
        commands = []
        def capture_command(value):
            commands.append(value)
            rows.append(after)
            return ""
        adapter._capture_raw_command = capture_command
        adapter.state_after = lambda count, predicate, timeout=15, **_kwargs: next(
            row for row in rows[count:] if predicate(row))
        observed = adapter.move("prt_fild08", {"map": "prt_fild08", "x": 190, "y": 360}, 30)
        self.assertEqual(commands, ["move prt_fild08 190 360"])
        self.assertEqual(observed["map"], "prt_fild08")

    def test_native_move_rejects_stale_matching_state_before_command(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        adapter.output = ""
        before = {"connected_in_game": True, "map": "prt_fild08",
                  "client_estimated_position": {"x": 190, "y": 360},
                  "movement_state": "stationary"}
        rows = [before]
        adapter.state = lambda predicate, timeout=15: before
        adapter._observer_rows = lambda: rows
        adapter._capture_raw_command = lambda _value: ""
        def state_after(count, predicate, timeout=15, **_kwargs):
            if len(rows) <= count:
                raise TimeoutError("no fresh observer sample")
            return next(row for row in rows[count:] if predicate(row))
        adapter.state_after = state_after
        with self.assertRaisesRegex(TimeoutError, "fresh observer"):
            adapter.move("prt_fild08", {"x": 190, "y": 360}, 1)

    def test_npc_actions_wait_for_fresh_presence_and_inventory_observation(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        stale = {"connected_in_game": True, "map": "prontera", "inventory": [],
                 "nearby_npcs": [{"name": "KoreLabNPC", "name_id": 123}]}
        fresh = {"connected_in_game": True, "map": "prontera",
                 "inventory": [{"item_id": 7049, "amount": 1}],
                 "nearby_npcs": [{"name": "KoreLabNPC", "name_id": 456}]}
        rows = [stale]
        adapter._observer_rows = lambda: rows
        def state_after(count, predicate, timeout=15):
            rows.append(fresh)
            return next(row for row in rows[count:] if predicate(row))
        adapter.state_after = state_after
        observed = adapter._observe_nearby_npc("KoreLabNPC", require_name_id=True)
        self.assertEqual(observed["inventory"][0]["item_id"], 7049)
        self.assertEqual(observed["nearby_npcs"][0]["name_id"], 456)

    def test_offline_start_position_preparation_is_identity_and_state_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "db").mkdir()
            (root / "db/map_index.txt").write_text("izlude\nprt_fild08\n")
            adapter = RathenaAdapter.__new__(RathenaAdapter)
            adapter.checkout = root
            adapter.char_db = "test_char_db"
            row = {"name": "SyntheticCharacter", "char_id": 7, "account_id": 8, "online": 0,
                   "map": "iz_int01", "x": 18, "y": 26}
            adapter.wait_offline = lambda name: dict(row)
            adapter.character = lambda name: dict(row)
            sql = []
            def mysql(statement):
                sql.append(statement)
                row.update({"map": "izlude", "x": 24, "y": 98})
                return types.SimpleNamespace(returncode=0, stdout="1\n")
            adapter._mysql = mysql
            prepared = adapter.prepare_position(dict(row, map="iz_int01", x=18, y=26), "izlude", 24, 98)
            self.assertEqual((prepared["map"], prepared["x"], prepared["y"]), ("izlude", 24, 98))
            self.assertIn("online=0", sql[0])
            self.assertIn("AND last_map='iz_int01' AND last_x=18 AND last_y=26", sql[0])
            with self.assertRaisesRegex(RuntimeError, "starting location changed"):
                adapter.prepare_position(dict(row, map="iz_int01", x=18, y=26), "izlude", 24, 98)

    def test_ordered_cross_map_moves_require_declared_forward_and_return_edges(self):
        scenario = {
            "name": "cross_map_contract", "identity": {"character": "SyntheticCharacter"},
            "initial_state": {"map": "izlude", "position": {"x": 24, "y": 98}},
            "expected": {}, "cleanup": {}, "maximum_duration_seconds": 180,
            "openkore": {"map_transitions": [
                {"from": "izlude", "to": "prt_fild08"},
                {"from": "prt_fild08", "to": "izlude"}]},
            "actions": [
                {"id": "outbound", "type": "move",
                 "target": {"map": "prt_fild08", "x": 369, "y": 210}},
                {"id": "return", "type": "move",
                 "target": {"map": "izlude", "x": 24, "y": 98}},
                {"id": "verify", "type": "verify_inventory", "expected": {}}
            ]}
        _, steps = validate_scenario_structure(scenario)
        self.assertEqual([step["id"] for step in steps], ["outbound", "return", "verify"])
        scenario["openkore"]["map_transitions"].pop()
        with self.assertRaisesRegex(ValueError, "explicit openkore.map_transitions"):
            validate_scenario_structure(scenario)

    def test_openkore_adapter_issues_native_map_qualified_move(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        adapter.output = ""
        adapter.current = {"connected_in_game": True, "map": "izlude",
                           "client_estimated_position": {"x": 24, "y": 98},
                           "movement_state": "stationary"}
        calls = []
        rows = [adapter.current]
        def capture_command(text):
            calls.append(text)
            adapter.current.update({"map": "prt_fild08", "client_estimated_position": {"x": 369, "y": 210}})
            rows.append({**adapter.current, "movement_state": "stationary"})
            return ""
        adapter._capture_raw_command = capture_command
        adapter.state = lambda predicate, timeout=15: adapter.current if predicate(adapter.current) else None
        adapter._observer_rows = lambda: rows[:1]
        adapter.state_after = lambda count, predicate, timeout=15, **_kwargs: next(
            row for row in rows[count:] if predicate(row))
        observed = adapter.move("prt_fild08", {"x": 369, "y": 210}, 5)
        self.assertEqual(calls, ["move prt_fild08 369 210"])
        self.assertEqual(observed["map"], "prt_fild08")

    def test_composed_scenario_uses_native_policy_and_eventmacro_without_per_target_attack(self):
        scenario = native_ai_fixture()
        scenario["openkore"] = {"ai_mode": "on", "config_overrides": {"itemsTakeAuto": 1}}
        _, steps = validate_scenario_structure(scenario)
        self.assertEqual(scenario["openkore"]["ai_mode"], "on")
        self.assertEqual(scenario["openkore"]["config_overrides"]["itemsTakeAuto"], 1)
        self.assertEqual(sum(step["type"] == "attack_monster" for step in steps), 0)
        self.assertEqual(sum(step["type"] == "await_native_ai" for step in steps), 1)
        self.assertEqual(steps[-1]["type"], "verify_inventory")

    def test_eventmacro_owned_dialogue_requires_markers_and_live_choice_declaration(self):
        scenario = scenario_fixture([
            {"id": "talk", "type": "npc_dialogue", "driver": "eventmacro",
             "eventmacro_trigger": "accept_test", "eventmacro_marker": "ACCEPTED",
             "expected_available_choices": ["Continue"]},
            {"id": "verify", "type": "verify_inventory", "expected": {}}],
            openkore={"eventmacro_source": "/tmp/caller-provided-macro.txt"})
        scenario["actions"][0].pop("eventmacro_marker")
        with self.assertRaisesRegex(ValueError, "eventmacro_marker"):
            validate_scenario_structure(scenario)

    def test_inventory_delta_ranges_require_nonoverlapping_ordered_integer_bounds(self):
        scenario = scenario_fixture([{"id": "verify", "type": "verify_inventory",
                                      "expected": {"inventory_delta": {"Test_Item": -1},
                                                   "inventory_delta_ranges": {"Test_Item": {"min": 0, "max": 1}}}}])
        verify = scenario["actions"][0]
        with self.assertRaisesRegex(ValueError, "both an exact inventory delta"):
            validate_scenario_structure(scenario)
        del verify["expected"]["inventory_delta"]["Test_Item"]
        verify["expected"]["inventory_delta_ranges"]["Test_Item"] = {"min": True, "max": 1}
        with self.assertRaisesRegex(ValueError, "ordered integer min/max"):
            validate_scenario_structure(scenario)

    def test_native_eventmacro_dialogue_requires_expected_live_menu_and_inventory_effect(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        adapter.output = ("[eventMacro] Conditions met for automacro 'accept_branch' during packet, "
                          "calling macro 'accept_branch_macro'\n"
                          "[eventmacro message] QUEST_ACCEPTED\n")
        rows = [
            {"type": "dialogue", "name": "KoreLabM3ABC123",
             "responses": ["Accept study", "Cancel Chat"]},
            {"type": "state", "connected_in_game": True,
             "inventory": [{"item_id": 909, "amount": 0}, {"item_id": 501, "amount": 1}]},
        ]
        adapter._observer_rows = lambda: rows
        adapter._wait_after = lambda pattern, cursor, timeout: re.search(
            pattern, adapter.output[cursor:], re.I | re.S) or (_ for _ in ()).throw(TimeoutError())
        adapter.state = lambda predicate, timeout: next(row for row in reversed(rows) if predicate(row))
        result = adapter.wait_eventmacro_dialogue(npc_name="KoreLabM3ABC123",
            trigger="accept_branch", marker="QUEST_ACCEPTED", expected_choices=["Accept study"],
            inventory_before={909: 1, 501: 0}, inventory_delta={909: -1, 501: 1},
            observer_sample_count=0, branch_message="Accepted", timeout=1)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["driver"], "native_eventMacro")
        self.assertTrue(result["branch_message_observed"])

    def test_native_eventmacro_dialogue_rejects_unexpected_live_menu(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        adapter.output = ("Conditions met for automacro 'accept_branch' during packet\n"
                          "QUEST_ACCEPTED\nAccepted")
        rows = [{"type": "dialogue", "name": "KoreLabM3ABC123", "responses": ["Wrong choice"]}]
        adapter._observer_rows = lambda: rows
        adapter._wait_after = lambda pattern, cursor, timeout: re.search(
            pattern, adapter.output[cursor:], re.I | re.S) or (_ for _ in ()).throw(TimeoutError())
        with self.assertRaisesRegex(RuntimeError, "unexpected NPC dialogue choices"):
            adapter.wait_eventmacro_dialogue(npc_name="KoreLabM3ABC123",
                trigger="accept_branch", marker="QUEST_ACCEPTED", expected_choices=["Accept study"],
                inventory_before={909: 1}, inventory_delta={909: -1}, observer_sample_count=0,
                branch_message="Accepted", timeout=1)

    def test_native_ai_outcome_requires_marker_actor_disappearance_and_inventory_delta(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        adapter.output = "[eventMacro] Conditions met for automacro 'quest', calling macro\nKORELAB_DONE\n"
        adapter.process = types.SimpleNamespace(poll=lambda: None)
        present = {"type": "state", "connected_in_game": True,
                   "nearby_monsters": [{"name": "KoreLabQ1"}],
                   "inventory": [{"item_id": 501, "amount": 1}]}
        completed = {"type": "state", "connected_in_game": True,
                     "nearby_monsters": [],
                     "inventory": [{"item_id": 501, "amount": 0}, {"item_id": 506, "amount": 1}]}
        rows = [present]
        adapter._observer_rows = lambda: rows
        adapter._read = lambda _timeout: rows.append(completed) if len(rows) == 1 else None
        result = adapter.wait_native_ai_outcome("KoreLabQ1", "KORELAB_DONE",
                                               {501: 1, 506: 0}, {501: -1, 506: 1}, 1)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["marker_observed"])
        self.assertTrue(result["automacro_trigger_observed"])
        self.assertIn("structured actor/inventory", result["evidence"])
        self.assertEqual(result["lifecycle"][-1]["state"], "completed")

    def test_native_ai_mode_switch_uses_openkore_command_and_requires_response(self):
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        adapter.output = ""
        commands = []
        adapter.command = commands.append
        adapter._wait_after = lambda pattern, cursor, timeout: types.SimpleNamespace(group=lambda _i: "AI set to auto mode")
        result = adapter.set_ai_mode("on")
        self.assertEqual(commands, ["ai on"])
        self.assertEqual(result["mode"], "on")
        with self.assertRaisesRegex(ValueError, "mode must be"):
            adapter.set_ai_mode("automatic")

    def test_native_ai_action_requires_marker_and_inventory_outcomes(self):
        scenario = native_ai_fixture()
        action = next(step for step in scenario["actions"] if step["type"] == "await_native_ai")
        action.pop("eventmacro_marker")
        with self.assertRaisesRegex(ValueError, "eventMacro outcome marker"):
            validate_scenario_structure(scenario)

    def test_random_drop_bounds_use_active_mob_rates_and_controlled_target_count(self):
        bounds, rates = _random_drop_delta_bounds({"drops": [
            {"item": "Jellopy", "rate": 7000},
            {"item": "Jellopy", "rate": 10000},
            {"item": "Apple", "rate": 1000}]}, "Jellopy", 20, -1)
        self.assertEqual(bounds, {"min": 19, "max": 39})
        self.assertEqual(rates, [7000, 10000])
        with self.assertRaisesRegex(ValueError, "does not declare a random drop"):
            _random_drop_delta_bounds({"drops": [{"item": "Jellopy", "rate": 10000}]},
                                      "Jellopy", 20, -1)
        with self.assertRaisesRegex(ValueError, "positive controlled target count"):
            _random_drop_delta_bounds({"drops": []}, "Jellopy", 0, -1)

    def test_native_ai_rejects_unknown_quest_handoff_state(self):
        scenario = native_ai_fixture()
        action = next(step for step in scenario["actions"] if step["type"] == "await_native_ai")
        action["quest_state_after_action"] = "maybe"
        with self.assertRaisesRegex(ValueError, "quest_state_after_action"):
            validate_scenario_structure(scenario)

    def test_runtime_restore_requires_explicit_declared_eventmacro_handoff(self):
        scenario = scenario_fixture([
            {"id": "restore", "type": "restore_openkore_profile", "restore_ai_mode": "on",
             "handoff_macro": "resume_native", "handoff_marker": "RESUMED"},
            {"id": "verify", "type": "verify_inventory", "expected": {}}])
        restore = scenario["actions"][0]
        restore["handoff_macro"] = "not valid"
        with self.assertRaisesRegex(ValueError, "handoff_macro"):
            validate_scenario_structure(scenario)

    def test_quest_hunt_requires_spawn_count_to_match_quest_count(self):
        scenario = native_ai_fixture(quest_count=2)
        spawn = next(step for step in scenario["actions"] if step["type"] == "spawn_monster")
        spawn["count"] = 19
        with self.assertRaisesRegex(ValueError, "spawn must match"):
            validate_scenario_structure(scenario)

    def test_native_ai_inventory_milestones_feed_later_dialogue_preflight(self):
        planned = {501: 1}
        catalog = {"Stone": {"id": 7049, "aegisname": "Stone"}}
        _apply_declared_inventory_delta(planned, catalog, {"Stone": 1})
        self.assertEqual(planned, {501: 1, 7049: 1})
        with self.assertRaisesRegex(RuntimeError, "negative"):
            _apply_declared_inventory_delta(planned, catalog, {"Stone": -2})
        with self.assertRaisesRegex(ValueError, "bounded integers"):
            _apply_declared_inventory_delta(planned, catalog, {"Stone": True})

    def test_rathena_all_commands_permission_satisfies_native_group_preflight(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "conf").mkdir()
            (root / "conf/groups.yml").write_text("""Body:
  - Id: 3
    Name: Script Manager
    Commands:
      loadnpc: true
      unloadnpc: true
  - Id: 99
    Name: Admin
    Permissions:
      all_commands: true
""")
            adapter = RathenaAdapter(root)
            self.assertTrue(adapter.group_allows(3, ("loadnpc", "unloadnpc")))
            self.assertFalse(adapter.group_allows(3, ("loadnpc", "monster")))
            self.assertTrue(adapter.group_allows(99, ("loadnpc", "monster", "reloadquestdb")))

    def test_complete_inventory_snapshot_includes_items_outside_the_declared_fixture(self):
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        adapter.char_db = "char"
        fields = adapter.INVENTORY_FIELDS
        values = {field: index + 1 for index, field in enumerate(fields)}
        values.update({"nameid": 512, "amount": 1, "equip": 0})
        query = {}
        adapter._mysql = lambda sql: (query.update(sql=sql) or types.SimpleNamespace(
            returncode=0, stdout="\t".join(str(values[field]) for field in fields) + "\n"))
        rows = adapter.all_inventory_rows({"char_id": 42})
        self.assertEqual(rows, [values])
        self.assertIn("WHERE char_id=42 ORDER BY id", query["sql"])
        self.assertNotIn("nameid IN", query["sql"])


if __name__ == "__main__":
    unittest.main()
