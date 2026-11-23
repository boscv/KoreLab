import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from korelab.capabilities import CAPABILITIES, ORDERED_ACTION_TYPES, capability_catalog
from korelab.planner import (PlanningError, _input_provenance, plan_objective,
                             plan_objective_file, write_planned_scenario)
from korelab.scenario import validate_scenario_structure


class FakeRathena:
    PREPARABLE_FIELDS = {"str", "agi", "vit", "int", "dex", "luk", "max_hp", "hp", "max_sp", "sp", "zeny"}

    def __init__(self, inventory=None, quests=None, allow_group=True):
        self.items = inventory or {}
        self.quests = quests or []
        self.allow_group = allow_group

    def character(self, name):
        return {"name": name, "char_id": 123, "account_id": 456, "online": 0,
            "class": 0, "base_level": 1, "job_level": 1, "base_exp": 0, "job_exp": 0,
            "str": 1, "agi": 1, "vit": 1, "int": 1, "dex": 1, "luk": 1,
            "hp": 40, "max_hp": 40, "sp": 11, "max_sp": 11,
            "map": "iz_int01", "x": 18, "y": 26}

    def quest_rows(self, character, quest_ids):
        return [row for row in self.quests if row["quest_id"] in quest_ids]

    def inventory_rows(self, character, item_ids):
        return [{"nameid": item_id, "amount": amount, "equip": 0}
                for item_id, amount in self.items.items() if item_id in item_ids]

    @staticmethod
    def inventory_amount(rows, item_id):
        return sum(row["amount"] for row in rows if row["nameid"] == item_id)

    def equipped_inventory_rows(self, character):
        return []

    def group_allows(self, group_id, commands):
        return self.allow_group


class PlannerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "db").mkdir()
        (self.root / "db/map_index.txt").write_text("iz_int01\nprontera 1\n", encoding="utf-8")
        (self.root / "src/common").mkdir(parents=True)
        (self.root / "src/common/mmo.hpp").write_text(
            "enum e_job { JOB_NOVICE, JOB_ARCHER, JOB_MAX };\n"
            "enum equip_pos : uint32 { EQP_HAND_L = 0x20, EQP_HAND_R = 0x2 };\n", encoding="utf-8")
        self.openkore_root = self.root / "openkore"
        (self.openkore_root / "src").mkdir(parents=True)
        (self.openkore_root / "src/Fixture.pm").write_text(
            ''.join(f'Settings::addControlFile(getControlFilename("{name}"));\n' for name in
                    ("storage.txt", "mon_control.txt", "pickupitems.txt", "items_control.txt")),
            encoding="utf-8")
        (self.openkore_root / "control").mkdir()
        (self.openkore_root / "control/config.txt").write_text(
            "itemsMaxWeight 89\nattackAuto 2\nattackAuto_notInTown 0\n"
            "itemsTakeAuto 1\nitemsGatherAuto 2\nitemsGatherAuto_notInTown 0\n"
            "route_randomWalk 0\nlockMap iz_int01\n", encoding="utf-8")
        self.objective = {
            "version": 1, "id": "test_hunt", "identity": {"master": "profile", "username": "user",
                "character": "Test", "character_slot": "0"},
            "start": {"map": "iz_int01", "position": {"x": 18, "y": 26}},
            "requirements": {"character": {"job": "NOVICE", "minimum_base_level": 1},
                "equipment": [{"item": "Novice_Guard", "location": "Left_Hand", "condition": "compatible"}]},
            "preparation": {"attributes": {"str": 30}, "inventory": [{"item": "Jellopy", "amount": 1}]},
            "goal": {"type": "complete_monster_hunt", "quest": {"id": 65002, "title": "Planner hunt"},
                "target": {"monster": "PORING", "count": 1},
                "environment": {"npc": {"map": "iz_int01", "position": {"x": 19, "y": 26}},
                    "hunt_map": "iz_int01", "hunt_position": {"x": 18, "y": 27},
                    "return_position": {"x": 18, "y": 26}},
                "accept": {"id": "accept", "label": "Accept hunt", "message": "Accepted",
                    "requires": [{"item": "Jellopy", "amount": 1}],
                    "consume": {"item": "Jellopy", "amount": 1},
                    "grant": {"item": "Red_Potion", "amount": 1}},
                "turn_in": {"id": "turn_in", "label": "Turn in", "message": "Done",
                    "requires": [{"item": "Red_Potion", "amount": 1}],
                    "consume": {"item": "Red_Potion", "amount": 1},
                    "grant": {"item": "Blue_Gemstone", "amount": 1}}},
            "runtime": {"temporary_group_id": 3, "maximum_duration_seconds": 240}}
        self.exchange_objective = {
            "version": 1, "id": "test_item_exchange", "identity": self.objective["identity"],
            "start": self.objective["start"],
            "preparation": {"inventory": [{"item": "Jellopy", "amount": 1}]},
            "goal": {"type": "complete_npc_exchange", "npc": {"map": "iz_int01",
                "position": {"x": 19, "y": 26}},
                "exchange": {"material": {"item": "Jellopy", "amount": 1},
                    "reward": {"item": "Red_Potion", "amount": 1}}},
            "runtime": {"temporary_group_id": 3, "maximum_duration_seconds": 150}}
        self.branch_objective = {
            "version": 1, "id": "test_dialogue_branch", "identity": self.objective["identity"],
            "start": self.objective["start"],
            "preparation": {"inventory": [{"item": "Jellopy", "amount": 1},
                                           {"item": "Red_Potion", "amount": 1}]},
            "goal": {"type": "complete_npc_dialogue_branch",
                "npc": {"map": "iz_int01", "position": {"x": 19, "y": 26}},
                "desired_reward": {"item": "Blue_Gemstone", "amount": 1},
                "choices": [
                    {"id": "jellopy_gemstone", "label": "Jellopy for gemstone",
                     "message": "Gemstone supplied.", "requires": [{"item": "Jellopy", "amount": 1}],
                     "consume": {"item": "Jellopy", "amount": 1},
                     "grant": {"item": "Blue_Gemstone", "amount": 1}},
                    {"id": "potion_stone", "label": "Potion for stone",
                     "message": "Stone supplied.", "requires": [{"item": "Red_Potion", "amount": 1}],
                     "consume": {"item": "Red_Potion", "amount": 1},
                     "grant": {"item": "Stone", "amount": 1}}]},
            "runtime": {"temporary_group_id": 3, "maximum_duration_seconds": 150}}
        self.items = {
            "Jellopy": {"id": 909, "aegisname": "Jellopy", "name": "Jellopy", "type": "Etc"},
            "Red_Potion": {"id": 501, "aegisname": "Red_Potion", "name": "Red Potion", "type": "Healing"},
            "Blue_Gemstone": {"id": 717, "aegisname": "Blue_Gemstone", "name": "Blue Gemstone", "type": "Etc"},
            "Stone": {"id": 990, "aegisname": "Stone", "name": "Stone", "type": "Etc"},
            "Novice_Guard": {"id": 2112, "aegisname": "Novice_Guard", "name": "Novice Guard", "type": "Armor",
                "jobs": {"Novice": True}, "locations": {"Left_Hand": True}},
            "Princess_Knife": {"id": 1240, "aegisname": "Princess_Knife", "name": "Princess Knife", "type": "Weapon",
                "jobs": {"Archer": True}, "locations": {"Right_Hand": True}},
        }
        for item in self.items.values():
            item.setdefault("source", "db/re/item_db.yml")
        self.mobs = {"mode": "renewal", "sources": ["db/re/mob_db.yml"], "count": 1,
            "monsters": [{"id": 1002, "aegis_name": "PORING", "name": "Poring", "hp": 55,
                "attack": 1, "attack_delay": 1800, "defense": 2, "level": 1,
                "race": "Plant", "size": "Medium", "element": "Water", "element_level": 1,
                "behavior": {"can_move": True, "can_attack": True, "aggressive": False},
                "drops": [{"item": "Stone", "rate": 10000}]}]}

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, objective=None, adapter=None, openkore=None):
        data = objective or self.objective
        with patch("korelab.planner.load_item_catalog", return_value=self.items), \
             patch("korelab.planner.load_monster_catalog", return_value=self.mobs), \
             patch("korelab.planner.load_quest_catalog", return_value={"quests": []}):
            return plan_objective(data, self.root, adapter or FakeRathena(), openkore)

    def test_catalog_is_the_runner_ordered_action_source_of_truth(self):
        self.assertEqual(set(CAPABILITIES), set(ORDERED_ACTION_TYPES))
        self.assertEqual({row["name"] for row in capability_catalog()["capabilities"]} -
                         {"combat", "move_to_position"}, set(ORDERED_ACTION_TYPES))
        self.assertTrue(all({"required_inputs", "outputs", "preconditions", "observations", "limitations"}
                            <= set(row) for row in capability_catalog()["capabilities"]))
        operators = {row["name"]: row for row in capability_catalog()["planning_operators"]}
        self.assertEqual(set(operators), {"complete_monster_hunt", "complete_npc_exchange",
                                          "complete_npc_dialogue_branch"})
        self.assertTrue(all({"required_inputs", "outputs", "preconditions", "observations", "limitations"}
                        <= set(operator) for operator in operators.values()))

    def test_planned_file_keeps_identity_environment_references(self):
        objective = dict(self.objective)
        objective["identity"] = {"master": "${KORELAB_OPENKORE_MASTER}",
            "username": "${KORELAB_OPENKORE_USERNAME}", "character": "${KORELAB_CHARACTER}",
            "character_slot": "${KORELAB_CHARACTER_SLOT}"}
        path = self.root / "objective.json"
        path.write_text(json.dumps(objective), encoding="utf-8")
        generated = {"status": "READY", "scenario": {"name": "x", "identity": objective["identity"],
            "initial_state": {}, "expected": {}, "cleanup": {}, "maximum_duration_seconds": 60,
            "action": {"type": "move_to_position"}}}
        environment = {"KORELAB_OPENKORE_MASTER": "local-profile", "KORELAB_OPENKORE_USERNAME": "local-user",
                       "KORELAB_CHARACTER": "local-char", "KORELAB_CHARACTER_SLOT": "0"}
        with patch.dict("os.environ", environment), patch("korelab.planner.plan_objective", return_value=generated) as plan:
            result = plan_objective_file(path, self.root)
        self.assertEqual(plan.call_args.args[0]["identity"]["character"], "local-char")
        self.assertEqual(result["scenario"]["identity"], objective["identity"])

    def test_supplied_information_provenance_is_field_scoped_and_preserved(self):
        objective = {**self.objective, "input_provenance": [
            {"path": "goal.accept", "source_id": "quest-brief-17", "provenance": "user_provided"},
            {"path": "goal.environment.npc", "source_id": "observed-run-42", "provenance": "openkore_observed"},
        ]}
        facts = _input_provenance(objective)
        self.assertEqual([row["provenance"] for row in facts], ["user_provided", "openkore_observed"])
        plan = self.plan(objective)
        self.assertEqual(plan["scenario"]["planning"]["input_provenance"], facts)
        self.assertIn("does not establish", plan["scenario"]["planning"]["fixture_scope"])
        with self.assertRaisesRegex(PlanningError, "does not exist"):
            _input_provenance({**objective, "input_provenance": [
                {"path": "goal.accept.unknown", "source_id": "brief", "provenance": "user_provided"}]})
        with self.assertRaisesRegex(PlanningError, "duplicated"):
            _input_provenance({**objective, "input_provenance": [facts[0], facts[0]]})

    def test_plan_resolves_dependencies_and_emits_runner_compatible_scenario(self):
        plan = self.plan()
        scenario = plan["scenario"]
        self.assertEqual(plan["status"], "READY")
        self.assertEqual([step["type"] for step in scenario["actions"]], [
            "reload_questdb", "npc_dialogue", "move", "spawn_monster", "attack_monster",
            "move", "npc_dialogue", "unload_npc", "verify_inventory"])
        validate_scenario_structure(scenario)
        self.assertEqual(scenario["actions"][4]["spawn_step"], "spawn_objective_target")
        self.assertEqual(scenario["actions"][-1]["expected"]["quest"], {"id": 65002, "state": "complete"})
        self.assertEqual(scenario["actions"][-1]["expected"]["inventory_delta"], {
            "Blue_Gemstone": 1, "Jellopy": -1, "Red_Potion": 0})
        self.assertIn("combat_fit", [entry["check"] for entry in plan["semantic_checks"]])
        self.assertTrue(scenario["planning"]["dependencies"])

    def test_hunt_drop_composes_collection_before_return_and_turn_in(self):
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "collect": {"item": "Stone", "amount": 1},
            "turn_in": {"id": "turn_in", "label": "Turn in", "message": "Done",
                "requires": [{"item": "Red_Potion", "amount": 1}, {"item": "Stone", "amount": 1}],
                "consume": {"item": "Stone", "amount": 1},
                "grant": {"item": "Blue_Gemstone", "amount": 1}}}}
        plan = self.plan(objective)
        scenario = plan["scenario"]
        action_types = [step["type"] for step in scenario["actions"]]
        self.assertEqual(action_types, ["reload_questdb", "npc_dialogue", "move", "spawn_monster",
            "attack_monster", "collect_monster_drop", "move", "npc_dialogue", "unload_npc", "verify_inventory"])
        self.assertEqual(scenario["actions"][5]["attack_step"], "defeat_objective_target")
        self.assertEqual(scenario["actions"][-1]["expected"]["inventory_delta"],
                         {"Blue_Gemstone": 1, "Jellopy": -1, "Red_Potion": 1, "Stone": 0})
        self.assertIn("monster_drop", [entry["check"] for entry in plan["semantic_checks"]])
        validate_scenario_structure(scenario)

    def test_hunt_drop_rejects_random_or_undeclared_drop(self):
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "collect": {"item": "Stone", "amount": 1}}}
        self.mobs["monsters"][0]["drops"] = [{"item": "Stone", "rate": 9999}]
        with self.assertRaisesRegex(PlanningError, "does not guarantee") as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "unsupported_action")

    def test_native_ai_hunt_composes_discovered_policy_macro_and_handoff(self):
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "execution": {"combat": "openkore_ai"},
            "collect": {"item": "Stone", "amount": 1},
            "turn_in": {"id": "turn_in", "label": "Turn in", "message": "Done",
                "requires": [{"item": "Red_Potion", "amount": 1}, {"item": "Stone", "amount": 1}],
                "consume": {"item": "Stone", "amount": 1},
                "grant": {"item": "Blue_Gemstone", "amount": 1}}}}
        plan = self.plan(objective)
        scenario = plan["scenario"]
        steps = scenario["actions"]
        self.assertEqual([step["type"] for step in steps], ["reload_questdb", "npc_dialogue", "move",
            "spawn_monster", "await_native_ai", "move", "npc_dialogue", "unload_npc",
            "restore_openkore_profile", "verify_inventory"])
        self.assertEqual(sum(step["type"] == "attack_monster" for step in steps), 0)
        self.assertEqual(sum(step["type"] == "collect_monster_drop" for step in steps), 0)
        profile = scenario["openkore"]
        self.assertEqual(profile["ai_mode"], "on")
        self.assertEqual(profile["config_overrides"]["itemsTakeAuto"], 1)
        self.assertEqual(profile["config_overrides"]["itemsGatherAuto"], 2)
        self.assertIn("1002 1 0 0", profile["control_file_overrides"]["mon_control.txt"]["content"])
        self.assertIn("Stone 1", profile["control_file_overrides"]["pickupitems.txt"]["content"])
        macro = plan["artifacts"]["eventmacro_source"]
        self.assertIn("QuestHuntCompleted 65002 1002", macro)
        self.assertIn('InInventory "Stone" >= 1', macro)
        self.assertIn("macro korelab_resume_ai", macro)
        self.assertEqual(steps[4]["inventory_delta"], {"Stone": 1})
        rationale_ids = [entry["action"] for entry in plan["action_plan"]]
        self.assertLess(rationale_ids.index("restore_native_ai_policy"),
                        rationale_ids.index("verify_objective_reward"))
        validate_scenario_structure(scenario)

    def test_native_ai_planner_writes_companion_eventmacro_artifact(self):
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "execution": {"combat": "openkore_ai"}}}
        plan = self.plan(objective)
        output = self.root / "runs/generated.json"
        written = write_planned_scenario(plan, output)
        generated = json.loads(written.read_text())
        macro_path = Path(generated["openkore"]["eventmacro_source"])
        self.assertEqual(macro_path.resolve(), output.with_suffix(".eventmacro.txt").resolve())
        self.assertEqual(macro_path.read_text(), plan["artifacts"]["eventmacro_source"])
        self.assertEqual(plan["artifact_paths"]["eventmacro_source"], str(macro_path))

    def test_native_ai_planner_rejects_unknown_driver(self):
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "execution": {"combat": "quest_specific_executor"}}}
        with self.assertRaisesRegex(PlanningError, "unsupported") as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "unsupported_action")

    def test_native_ai_planner_validates_and_composes_declared_native_policy(self):
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "execution": {"combat": "openkore_ai", "openkore_profile": {
                "config_overrides": {"itemsMaxWeight": 70},
                "control_file_overrides": {"storage.txt": {"mode": "append", "content": "Jellopy 1\\n"}}}}}}
        plan = self.plan(objective, openkore=self.openkore_root)
        profile = plan["scenario"]["openkore"]
        self.assertEqual(profile["config_overrides"]["itemsMaxWeight"], 70)
        self.assertEqual(profile["control_file_overrides"]["storage.txt"], {
            "mode": "append", "content": "Jellopy 1\\n"})

    def test_native_ai_planner_rejects_unknown_or_conflicting_declared_policy(self):
        unknown = {**self.objective, "goal": {**self.objective["goal"],
            "execution": {"combat": "openkore_ai", "openkore_profile": {
                "config_overrides": {"imaginaryOpenKoreSetting": 1}}}}}
        with self.assertRaisesRegex(PlanningError, "not found") as caught:
            self.plan(unknown, openkore=self.openkore_root)
        self.assertEqual(caught.exception.code, "unsupported_action")
        conflict = {**self.objective, "goal": {**self.objective["goal"],
            "execution": {"combat": "openkore_ai", "openkore_profile": {
                "control_file_overrides": {"mon_control.txt": {"mode": "replace", "content": "all 0\\n"}}}}}}
        with self.assertRaisesRegex(PlanningError, "conflicts with generated hunt filters"):
            self.plan(conflict, openkore=self.openkore_root)
        incompatible = {**self.objective, "goal": {**self.objective["goal"],
            "execution": {"combat": "openkore_ai", "openkore_profile": {
                "config_overrides": {"attackAuto": 0}}}}}
        with self.assertRaisesRegex(PlanningError, "disables combat") as caught:
            self.plan(incompatible, openkore=self.openkore_root)
        self.assertEqual(caught.exception.code, "incompatible_policy")

    def test_npc_exchange_objective_composes_existing_runner_and_semantic_checks(self):
        plan = self.plan(self.exchange_objective)
        scenario = plan["scenario"]
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(scenario["planning"]["operator"], "complete_npc_exchange")
        self.assertEqual([step["type"] for step in scenario["actions"]],
                         ["npc_exchange", "unload_npc", "verify_inventory"])
        self.assertEqual(scenario["actions"][0]["expected"], {"material_delta": -1, "reward_delta": 1})
        self.assertEqual(scenario["actions"][-1]["expected"]["inventory_delta"],
                         {"Jellopy": -1, "Red_Potion": 1})
        self.assertIn("items", [entry["check"] for entry in plan["semantic_checks"]])
        validate_scenario_structure(scenario)

    def test_npc_exchange_rejects_missing_material_quantity_and_unavailable_map(self):
        missing = {**self.exchange_objective, "preparation": {"inventory": []}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(missing)
        self.assertEqual(caught.exception.code, "missing_prerequisite")
        mismatched = {**self.exchange_objective, "goal": {**self.exchange_objective["goal"],
            "exchange": {"material": {"item": "Jellopy", "amount": 2},
                         "reward": {"item": "Red_Potion", "amount": 1}}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(mismatched)
        self.assertEqual(caught.exception.code, "unsupported_action")
        unavailable = {**self.exchange_objective, "goal": {**self.exchange_objective["goal"],
            "npc": {"map": "not_a_map", "position": {"x": 1, "y": 1}}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(unavailable)
        self.assertEqual(caught.exception.code, "unavailable_map")
        with self.assertRaises(PlanningError) as caught:
            self.plan(self.exchange_objective, FakeRathena(allow_group=False))
        self.assertEqual(caught.exception.code, "missing_prerequisite")

    def test_planner_selects_unique_supplied_dialogue_branch_and_composes_runner_actions(self):
        objective = {**self.branch_objective, "input_provenance": [
            {"path": "goal.choices", "source_id": "npc-brief-4", "provenance": "user_provided"},
            {"path": "goal.desired_reward", "source_id": "quest-request-8", "provenance": "user_provided"}]}
        plan = self.plan(objective)
        scenario = plan["scenario"]
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(scenario["planning"]["operator"], "complete_npc_dialogue_branch")
        self.assertEqual(scenario["actions"][0]["choice"], "jellopy_gemstone")
        self.assertEqual(scenario["actions"][0]["expected_available_choices"],
                         ["Jellopy for gemstone", "Potion for stone"])
        self.assertEqual([step["type"] for step in scenario["actions"]],
                         ["npc_dialogue", "unload_npc", "verify_inventory"])
        self.assertEqual(scenario["actions"][-1]["expected"]["inventory_delta"],
                         {"Blue_Gemstone": 1, "Jellopy": -1})
        self.assertEqual(scenario["planning"]["input_provenance"], objective["input_provenance"])
        self.assertIn("does not establish", scenario["planning"]["fixture_scope"])
        validate_scenario_structure(scenario)

    def test_planner_composes_a_quest_gated_supplied_dialogue_path(self):
        choices = [
            {"id": "accept_study", "label": "Accept study", "message": "Study accepted.",
             "requires": [{"item": "Jellopy", "amount": 1}],
             "consume": {"item": "Jellopy", "amount": 1},
             "grant": {"item": "Red_Potion", "amount": 1},
             "quest": {"id": 65006, "action": "start"}},
            {"id": "claim_gem", "label": "Claim gemstone", "message": "Research complete.",
             "requires": [{"item": "Red_Potion", "amount": 1}],
             "consume": {"item": "Red_Potion", "amount": 1},
             "grant": {"item": "Blue_Gemstone", "amount": 1},
             "quest_requires": [{"id": 65006, "state": "active"}],
             "quest": {"id": 65006, "action": "complete"}},
        ]
        objective = {**self.branch_objective, "goal": {**self.branch_objective["goal"],
            "choices": choices, "quest_fixture": {"id": 65006, "title": "Supplied branch study",
                "monster": "PORING", "count": 1}},
            "preparation": {"inventory": [{"item": "Jellopy", "amount": 1}]}}
        plan = self.plan(objective)
        scenario = plan["scenario"]
        self.assertEqual(scenario["quest_fixture"]["id"], 65006)
        self.assertEqual([step["type"] for step in scenario["actions"]],
            ["reload_questdb", "npc_dialogue", "npc_dialogue", "unload_npc", "verify_inventory"])
        self.assertEqual([step["choice"] for step in scenario["actions"] if step["type"] == "npc_dialogue"],
                         ["accept_study", "claim_gem"])
        self.assertEqual(scenario["actions"][1]["expected_available_choices"], ["Accept study"])
        self.assertEqual(scenario["actions"][2]["expected_available_choices"], ["Claim gemstone"])
        self.assertEqual(scenario["actions"][-1]["expected"]["quest"],
                         {"id": 65006, "state": "complete"})
        self.assertEqual(scenario["actions"][-1]["expected"]["inventory_delta"],
                         {"Blue_Gemstone": 1, "Jellopy": -1})
        validate_scenario_structure(scenario)

    def test_quest_gated_branch_rejects_missing_fixture_and_unsupported_hunt_state(self):
        gated = {**self.branch_objective["goal"]["choices"][0],
            "quest_requires": [{"id": 65006, "state": "active"}]}
        objective = {**self.branch_objective, "goal": {**self.branch_objective["goal"],
            "choices": [gated, self.branch_objective["goal"]["choices"][1]]}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "missing_prerequisite")
        unsupported = {**objective, "goal": {**objective["goal"],
            "quest_fixture": {"id": 65006, "title": "Branch study", "monster": "PORING", "count": 1},
            "choices": [{**gated, "quest_requires": [{"id": 65006, "state": "hunt_complete"}]},
                        objective["goal"]["choices"][1]]}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(unsupported)
        self.assertEqual(caught.exception.code, "unsupported_action")

    def test_unused_quest_fixture_is_rejected(self):
        objective = {**self.branch_objective, "goal": {**self.branch_objective["goal"],
            "quest_fixture": {"id": 65006, "title": "Unused fixture", "monster": "PORING"}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "unsupported_action")
        self.assertIn("quest_fixture is unused", str(caught.exception))

    def test_quest_fixture_id_collision_and_infeasible_branch_path_are_rejected(self):
        choices = [
            {"id": "accept_study", "label": "Accept study", "message": "Study accepted.",
             "requires": [{"item": "Jellopy", "amount": 1}],
             "consume": {"item": "Jellopy", "amount": 1},
             "grant": {"item": "Red_Potion", "amount": 1},
             "quest": {"id": 65006, "action": "start"}},
            {"id": "claim_gem", "label": "Claim gemstone", "message": "Research complete.",
             "requires": [{"item": "Red_Potion", "amount": 2}],
             "consume": {"item": "Red_Potion", "amount": 2},
             "grant": {"item": "Blue_Gemstone", "amount": 1},
             "quest_requires": [{"id": 65006, "state": "active"}],
             "quest": {"id": 65006, "action": "complete"}},
        ]
        objective = {**self.branch_objective, "goal": {**self.branch_objective["goal"],
            "choices": choices, "quest_fixture": {"id": 65006, "title": "Supplied branch study",
                "monster": "PORING", "count": 1}},
            "preparation": {"inventory": [{"item": "Jellopy", "amount": 1}]}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "missing_prerequisite")
        with patch("korelab.planner.load_quest_catalog", return_value={"quests": [{"id": 65006}]}):
            with self.assertRaises(PlanningError) as caught:
                with patch("korelab.planner.load_item_catalog", return_value=self.items), \
                     patch("korelab.planner.load_monster_catalog", return_value=self.mobs):
                    plan_objective({**self.branch_objective, "goal": {**self.branch_objective["goal"],
                        "quest_fixture": objective["goal"]["quest_fixture"], "choices": choices}},
                        self.root, FakeRathena())
        self.assertEqual(caught.exception.code, "ambiguous_objective")
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective, FakeRathena(allow_group=False))
        self.assertEqual(caught.exception.code, "missing_prerequisite")
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective, FakeRathena(quests=[{"quest_id": 65006, "state": 0}]))
        self.assertEqual(caught.exception.code, "missing_prerequisite")

    def test_dialogue_branch_planner_rejects_ambiguous_outcome_and_missing_alternative_fixture(self):
        ambiguous = {**self.branch_objective, "goal": {**self.branch_objective["goal"],
            "choices": [self.branch_objective["goal"]["choices"][0], {
                **self.branch_objective["goal"]["choices"][1],
                "grant": {"item": "Blue_Gemstone", "amount": 1}}]}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(ambiguous)
        self.assertEqual(caught.exception.code, "ambiguous_objective")
        missing = {**self.branch_objective, "preparation": {"inventory": [
            {"item": "Jellopy", "amount": 1}]}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(missing)
        self.assertEqual(caught.exception.code, "missing_prerequisite")

    def test_dialogue_branch_planner_rejects_unavailable_map_and_unsafe_menu_text(self):
        unavailable = {**self.branch_objective, "goal": {**self.branch_objective["goal"],
            "npc": {"map": "missing_map", "position": {"x": 1, "y": 1}}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(unavailable)
        self.assertEqual(caught.exception.code, "unavailable_map")
        unsafe_choice = {**self.branch_objective["goal"]["choices"][0], "label": "Injected: branch"}
        unsafe = {**self.branch_objective, "goal": {**self.branch_objective["goal"],
            "choices": [unsafe_choice, self.branch_objective["goal"]["choices"][1]]}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(unsafe)
        self.assertEqual(caught.exception.code, "unsupported_action")
        duplicate_labels = {**self.branch_objective, "goal": {**self.branch_objective["goal"],
            "choices": [self.branch_objective["goal"]["choices"][0], {
                **self.branch_objective["goal"]["choices"][1],
                "label": self.branch_objective["goal"]["choices"][0]["label"]}]}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(duplicate_labels)
        self.assertEqual(caught.exception.code, "ambiguous_objective")

    def test_rejects_unsupported_action_and_ambiguous_objective(self):
        unsupported = {**self.objective, "goal": {"type": "teleport"}}
        with self.assertRaisesRegex(PlanningError, "no deterministic planner operator") as caught:
            self.plan(unsupported)
        self.assertEqual(caught.exception.code, "unsupported_action")
        ambiguous = {**self.objective, "goal": {"quest": {"id": 65002}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(ambiguous)
        self.assertEqual(caught.exception.code, "ambiguous_objective")

    def test_rejects_unimplemented_requirement_instead_of_ignoring_it(self):
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "map_transition": {"to": "prontera"}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "unsupported_action")

    def test_rejects_missing_inventory_prerequisite_without_declared_fixture(self):
        objective = {**self.objective, "preparation": {"attributes": {"str": 30}, "inventory": []}}
        with self.assertRaisesRegex(PlanningError, "requires 1 Jellopy") as caught:
            self.plan(objective, FakeRathena())
        self.assertEqual(caught.exception.code, "missing_prerequisite")

    def test_rejects_incompatible_equipment_for_selected_character(self):
        objective = {**self.objective, "requirements": {"character": {"job": "NOVICE"}, "equipment": [
            {"item": "Princess_Knife", "location": "Right_Hand", "condition": "compatible"}]}}
        with self.assertRaisesRegex(PlanningError, "incompatible with NOVICE") as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "incompatible_equipment")

    def test_rejects_incompatible_character_job_requirement(self):
        objective = {**self.objective, "requirements": {"character": {
            "job": "WIZARD", "minimum_base_level": 1}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "incompatible_character")

    def test_rejects_existing_temporary_quest_and_missing_item(self):
        with patch("korelab.planner.load_item_catalog", return_value=self.items), \
             patch("korelab.planner.load_monster_catalog", return_value=self.mobs), \
             patch("korelab.planner.load_quest_catalog", return_value={"quests": [{"id": 65002}]}):
            with self.assertRaises(PlanningError) as caught:
                plan_objective(self.objective, self.root, FakeRathena())
            self.assertEqual(caught.exception.code, "missing_prerequisite")
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "accept": {**self.objective["goal"]["accept"],
                       "requires": [{"item": "Unknown_Item", "amount": 1}]}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "unavailable_item")

    def test_rejects_unavailable_monster_and_map(self):
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "target": {"monster": "NONEXISTENT_MOB", "count": 1}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "unavailable_monster")
        objective = {**self.objective, "goal": {**self.objective["goal"], "environment": {
            **self.objective["goal"]["environment"], "hunt_map": "not_a_real_map"}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "unavailable_map")

    def test_rejects_existing_or_ambiguous_monster_selector(self):
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "target": {"monster": "POR", "count": 1}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "unavailable_monster")
        self.mobs["monsters"].append({**self.mobs["monsters"][0], "id": 1003, "name": "Another Poring"})
        objective = {**self.objective, "goal": {**self.objective["goal"],
            "target": {"monster": "Poring", "count": 1}}}
        with self.assertRaises(PlanningError) as caught:
            self.plan(objective)
        self.assertEqual(caught.exception.code, "ambiguous_objective")


if __name__ == "__main__":
    unittest.main()
