import tempfile
import unittest
from pathlib import Path

from korelab.items import (assess_equipment, equipment_location_masks, load_item_catalog,
                           resolve_item)
from korelab.runner import (_macro_source, _npc_branch_source, _npc_source,
                            _quest_fixture_source, _refresh_openkore_precondition_inventory,
                            _step_preconditions)
from korelab.quests import analyze_script_source, load_quest_catalog


class ItemCatalogTests(unittest.TestCase):
    def test_reads_active_item_imports_and_applies_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "db/re").mkdir(parents=True)
            (root / "db/import").mkdir(parents=True)
            (root / "db/re/item_db.yml").write_text(
                "Footer:\n  Imports:\n  - Path: db/re/items.yml\n", encoding="utf-8")
            (root / "db/re/items.yml").write_text(
                "  - Id: 10\n    AegisName: Sample_Item\n    Name: Sample Item\n"
                "    Type: Etc\n    Weight: 15\n", encoding="utf-8")
            (root / "db/import/item_db.yml").write_text(
                "  - Id: 11\n    AegisName: Sample_Item\n    Name: Local Sample\n"
                "    Type: Healing\n    Weight: 25\n", encoding="utf-8")
            catalog = load_item_catalog(root)
            self.assertEqual(resolve_item(catalog, "Sample_Item"), {
                "id": 11, "source": "db/import/item_db.yml", "aegisname": "Sample_Item",
                "name": "Local Sample", "type": "Healing", "weight": 25})

    def test_equipment_assessment_uses_rathena_job_and_slot_constants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src/common").mkdir(parents=True)
            (root / "src/common/mmo.hpp").write_text(
                "enum e_job { JOB_NOVICE, JOB_SWORDMAN, JOB_MAX };\n"
                "enum equip_pos : uint32 { EQP_HAND_L = 0x20, EQP_HAND_R = 0x2 };\n",
                encoding="utf-8")
            novice_guard = {"type": "Armor", "jobs": {"Novice": True},
                            "locations": {"Left_Hand": True}}
            princess_knife = {"type": "Weapon", "jobs": {"Archer": True},
                              "locations": {"Right_Hand": True}, "equiplevelmin": 1}
            self.assertTrue(assess_equipment(novice_guard, 0, 1, "Left_Hand", root)["can_equip"])
            assessment = assess_equipment(princess_knife, 0, 1, "Right_Hand", root)
            self.assertFalse(assessment["can_equip"])
            self.assertIn("rAthena Jobs restriction excludes NOVICE", assessment["reasons"])
            self.assertEqual(equipment_location_masks(root)["Left_Hand"], 32)


class OrderedInteractionTests(unittest.TestCase):
    def test_native_action_observation_refreshes_inventory_for_later_preconditions(self):
        inventory = {"openkore": {"Stone": 0}, "rathena": {"Stone": 0}}
        _refresh_openkore_precondition_inventory(inventory,
            {"inventory": [{"item_id": 7049, "amount": 1}]},
            {"Stone": {"id": 7049}})
        _step_preconditions({"preconditions": [
            {"inventory": {"item": "Stone", "at_least": 1, "source": "openkore"}}
        ]}, {}, inventory)

    def test_action_can_depend_on_prior_result_and_inventory_precondition(self):
        results = {"exchange": {"status": "PASS", "changed": True}}
        _step_preconditions({"preconditions": [
            {"step": "exchange", "path": "changed", "equals": True},
            {"inventory": {"item": "Jellopy", "at_least": 1, "source": "rathena"}},
        ]}, results, {"rathena": {"Jellopy": 1}})
        with self.assertRaises(RuntimeError):
            _step_preconditions({"preconditions": [
                {"step": "exchange", "path": "status", "equals": "FAIL"}
            ]}, results, {})

    def test_temporary_npc_script_uses_declared_server_item_ids_and_quantity(self):
        script = _npc_source("iz_int01", 19, 26, "KoreLabM3ABC123", 909, 501, 2)
        self.assertIn("countitem(909) < 2", script)
        self.assertIn("delitem 909,2", script)
        self.assertIn("getitem 501,2", script)
        with self.assertRaises(ValueError):
            _npc_source("../bad", 19, 26, "KoreLabM3ABC123", 909, 501, 1)

    def test_branch_script_generates_dynamic_choices_with_item_and_quest_conditions(self):
        catalog = {
            "Jellopy": {"id": 909, "aegisname": "Jellopy"},
            "Red_Potion": {"id": 501, "aegisname": "Red_Potion"},
            "Blue_Gemstone": {"id": 717, "aegisname": "Blue_Gemstone"},
        }
        choices = [
            {"id": "accept", "label": "Accept task", "message": "Started.",
             "requires": [{"item": "Jellopy"}], "consume": {"item": "Jellopy"},
             "grant": {"item": "Red_Potion"}, "quest": {"id": 1015, "action": "start"}},
            {"id": "complete", "label": "Submit sample", "message": "Complete.",
             "requires": [{"item": "Red_Potion"}], "quest_requires": [{"id": 1015, "state": "active"}],
             "consume": {"item": "Red_Potion"}, "grant": {"item": "Blue_Gemstone"},
             "quest": {"id": 1015, "action": "complete"}},
        ]
        script = _npc_branch_source("iz_int01", 19, 26, "KoreLabM3ABC123", choices, catalog)
        self.assertIn("select(.@menu$)", script)
        self.assertIn("checkquest(1015) == -1", script)
        self.assertIn("checkquest(1015) == 1", script)
        hunt = dict(choices[1], quest_requires=[{"id": 1015, "state": "hunt_complete"}])
        script = _npc_branch_source("iz_int01", 19, 26, "KoreLabM3ABC123",
                                    [choices[0], hunt], catalog)
        self.assertIn("checkquest(1015,HUNTING) == 2", script)
        self.assertIn("setquest 1015", script)
        self.assertIn("completequest 1015", script)
        self.assertIn('Submit sample', script)

    def test_native_macro_source_is_bounded_and_keeps_order(self):
        macro = _macro_source("quest_walk", ["do move 18 27", "pause 2"], "KoreLabMacro_AB12CD34")
        self.assertLess(macro.index("do move 18 27"), macro.index("pause 2"))
        self.assertIn("log KoreLabMacro_AB12CD34", macro)
        with self.assertRaises(ValueError):
            _macro_source("unsafe", ["do quit"], "KoreLabMacro_AB12CD34")

    def test_macro_source_marks_progress_before_completion(self):
        macro = _macro_source("quest_walk", ["do move 18 27", "pause 8"],
                              "KoreLabMacro_AB12CD34", "KoreLabProgress_1234ABCD")
        self.assertLess(macro.index("KoreLabProgress_1234ABCD"), macro.index("pause 8"))
        self.assertLess(macro.index("pause 8"), macro.index("KoreLabMacro_AB12CD34"))


class QuestSemanticsTests(unittest.TestCase):
    def test_temporary_quest_fixture_is_safe_and_declares_native_target(self):
        source = _quest_fixture_source(65001, "KoreLab controlled hunt", "PORING", 1)
        self.assertIn("Type: QUEST_DB", source)
        self.assertIn("Id: 65001", source)
        self.assertIn("Mob: PORING", source)
        self.assertIn("Count: 1", source)
        with self.assertRaises(ValueError):
            _quest_fixture_source(65001, 'bad"title', "PORING", 1)

    def test_reads_active_quest_targets_and_local_import_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src/config").mkdir(parents=True)
            (root / "src/config/renewal.hpp").write_text("#define RENEWAL\n", encoding="utf-8")
            (root / "db/re").mkdir(parents=True)
            (root / "db/import").mkdir(parents=True)
            (root / "db/re/quest_db.yml").write_text(
                "Body:\n  - Id: 1100\n    Title: Hunt Zerom\n    Targets:\n"
                "      - Mob: ZEROM\n        Count: 20\n", encoding="utf-8")
            (root / "db/import/quest_db.yml").write_text(
                "Body:\n  - Id: 1100\n    Title: Local Hunt\n    Targets:\n"
                "      - Mob: PORING\n        Count: 1\n", encoding="utf-8")
            catalog = load_quest_catalog(root)
            self.assertEqual(catalog["mode"], "renewal")
            self.assertEqual(catalog["quests"][0]["title"], "Local Hunt")
            self.assertEqual(catalog["quests"][0]["targets"], [{"mob": "PORING", "count": 1}])

    def test_lightweight_script_analysis_marks_supported_and_ambiguous_patterns(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "quest.txt"
            script.write_text(
                'prontera,1,1,4\tscript\tQuest::NPC\t4_F_TELEPORTER,{\n'
                ' mes "Hunt"; if (countitem(909) > 0 && checkquest(1015) == 1) {\n'
                ' delitem 909,1; getitem 501,1; completequest 1015; }\n'
                ' select("Accept:Finish"); rand(1,2); close; }\n', encoding="utf-8")
            analysis = analyze_script_source(script)
            self.assertTrue(analysis["supported_patterns"]["dialogue_choices"])
            self.assertTrue(analysis["supported_patterns"]["quest_prerequisites"])
            self.assertTrue(analysis["supported_patterns"]["inventory_consumption"])
            self.assertTrue(analysis["supported_patterns"]["quest_completion"])
            self.assertTrue(analysis["unsupported_or_ambiguous"])


if __name__ == "__main__":
    unittest.main()
