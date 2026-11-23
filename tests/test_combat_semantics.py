import tempfile
import unittest
from pathlib import Path

from korelab.monsters import assess_match, find_monster, load_monster_catalog


def fixture_catalog():
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    for folder in ("src/config", "src/map", "src/common", "db/re"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "src/config/renewal.hpp").write_text("#define RENEWAL\n")
    (root / "src/map/mob.hpp").write_text("MONSTER_TYPE_01 = 0x0\n")
    (root / "src/common/mmo.hpp").write_text("MD_CANMOVE = 0x1\nMD_CANATTACK = 0x2\n")
    (root / "db/re/mob_db.yml").write_text("""Header:
  Type: MOB_DB
  Version: 3
Body:
  - Id: 1002
    AegisName: TEST_MOB
    Name: Fixture Mob
    Level: 1
    Hp: 55
    Attack: 1
    Attack2: 1
    AttackDelay: 1000
    Defense: 2
    MagicDefense: 0
    Size: Medium
    Race: Plant
    Element: Water
    ElementLevel: 1
    Ai: '01'
    Modes:
      CanMove: true
      CanAttack: true
    Drops:
      - Item: Jellopy
        Rate: 7000
      - Item: Stone
        Rate: 10000
""")
    return temp, load_monster_catalog(root)


class MonsterCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp, cls.catalog = fixture_catalog()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_catalog_reads_active_database_and_decodes_local_mode_flags(self):
        mob = find_monster(self.catalog, "TEST_MOB")
        self.assertEqual(self.catalog["mode"], "renewal")
        self.assertEqual((mob["level"], mob["hp"], mob["attack"], mob["defense"]),
                         (1, 55, 1, 2))
        self.assertEqual((mob["race"], mob["size"], mob["element"], mob["element_level"]),
                         ("Plant", "Medium", "Water", 1))
        self.assertEqual(mob["mode_bits"], 0x3)
        self.assertTrue(mob["behavior"]["can_move"])
        self.assertTrue(mob["behavior"]["can_attack"])
        self.assertFalse(mob["behavior"]["aggressive"])
        self.assertEqual(mob["drops"], [{"item": "Jellopy", "rate": 7000},
                                         {"item": "Stone", "rate": 10000}])


class CombatSemanticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp, cls.catalog = fixture_catalog()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.character = {"base_level": 1, "str": 30, "attack": 30, "max_hp": 40,
                          "skills": {"5": 1}}
        self.mob = find_monster(self.catalog, "TEST_MOB")

    def test_victory_fit_and_extreme_defeat_override(self):
        result = assess_match(self.character, self.mob, "victory")
        self.assertEqual(result["assessment"], "estimated_win")
        with self.assertRaisesRegex(ValueError, "semantic guard rejected"):
            assess_match(self.character, self.mob, "defeat")
        extreme = assess_match(self.character, self.mob, "defeat", allow_extreme=True,
                               reason="Test an intentionally impossible defeat expectation")
        self.assertTrue(extreme["allow_extreme"])

    def test_escape_resistance_and_skill_intents_are_distinct(self):
        self.assertEqual(assess_match(self.character, self.mob, "escape")["intent"], "escape")
        resistance = assess_match(self.character, self.mob, "resistance", duration_seconds=20)
        self.assertEqual(resistance["intent"], "resistance")
        self.assertLess(resistance["estimated_incoming_damage"], self.character["max_hp"])
        self.assertEqual(assess_match(self.character, self.mob, "skill", skill_id="5")["assessment"],
                         "requested_skill_available")
        with self.assertRaisesRegex(ValueError, "requested_skill_not_learned_at_level"):
            assess_match(self.character, self.mob, "skill", skill_id="6")


if __name__ == "__main__":
    unittest.main()
