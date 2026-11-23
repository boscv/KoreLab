from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from korelab.adapters.openkore import OpenKoreAdapter, UnexpectedCharacterDeath
from korelab.adapters.rathena import RathenaAdapter
from korelab.runner import _failure_classification


class UnexpectedDeathDetectionTests(unittest.TestCase):
    def make_adapter(self, rows: list[dict]) -> OpenKoreAdapter:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        observer = Path(temp.name) / "observer.jsonl"
        observer.write_text("".join(json.dumps(row) + "\n" for row in rows))
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        adapter.observer_file = observer
        adapter.deadline = time.monotonic() + 2
        adapter.output = "You have died\n"
        adapter.process = None
        adapter._read = lambda _timeout: None
        return adapter

    @staticmethod
    def state(dead: bool) -> dict:
        return {"type": "state", "connected_in_game": True,
                "character_attributes": {"is_dead": dead}}

    def test_waits_fail_promptly_on_unexpected_death(self):
        adapter = self.make_adapter([self.state(False), self.state(True)])
        with self.assertRaises(UnexpectedCharacterDeath):
            adapter.state(lambda row: row.get("connected_in_game") is True)
        with self.assertRaises(UnexpectedCharacterDeath):
            adapter.state_after(1, lambda row: row.get("connected_in_game") is True)
        with self.assertRaises(UnexpectedCharacterDeath):
            adapter._wait_after("equipment acknowledgement", 0, timeout=1)
        self.assertEqual(_failure_classification(UnexpectedCharacterDeath("dead")),
                         "unexpected_character_death")

    def test_explicit_negative_combat_can_observe_expected_death(self):
        adapter = self.make_adapter([self.state(False), self.state(True)])
        observed = adapter.state_after(1, lambda row: row["character_attributes"]["is_dead"],
                                       allow_dead=True)
        self.assertTrue(observed["character_attributes"]["is_dead"])

    def test_state_wait_does_not_return_a_stale_match_from_an_older_connection(self):
        adapter = self.make_adapter([
            {"type": "state", "connected_in_game": True, "session_generation": 1},
            {"type": "state", "connected_in_game": False, "session_generation": 1},
        ])
        with self.assertRaises(TimeoutError):
            adapter.state(lambda row: row.get("connected_in_game") is True, timeout=0.01)


class InitialInventoryReadinessTests(unittest.TestCase):
    def make_adapter(self, current: dict) -> OpenKoreAdapter:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        observer = Path(temp.name) / "observer.jsonl"
        observer.write_text(json.dumps(current) + "\n")
        adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
        adapter.observer_file = observer
        adapter.deadline = time.monotonic() + 1
        adapter._read = lambda _timeout: None
        return adapter

    def state(self, *, generation: int = 3, connected: bool = True,
              observed: bool = True, inventory: list[dict] | None = None) -> dict:
        return {"type": "state", "connected_in_game": connected,
                "session_generation": generation, "inventory_observed": observed,
                "inventory": inventory or []}

    def test_empty_inventory_is_ready_only_after_current_session_list_end(self):
        observed_empty = self.make_adapter(self.state(inventory=[]))
        result = observed_empty.wait_for_initial_inventory(3, lambda row: not row["inventory"], timeout=0.01)
        self.assertEqual(result["inventory"], [])

        not_loaded = self.make_adapter(self.state(observed=False, inventory=[]))
        with self.assertRaisesRegex(TimeoutError, "initial inventory observation"):
            not_loaded.wait_for_initial_inventory(3, lambda row: not row["inventory"], timeout=0.01)

    def test_stale_generation_and_disconnect_fail_explicitly(self):
        stale = self.make_adapter(self.state(generation=2))
        with self.assertRaisesRegex(RuntimeError, "connection changed"):
            stale.wait_for_initial_inventory(3, lambda _row: True, timeout=0.01)
        disconnected = self.make_adapter(self.state(connected=False, observed=False))
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            disconnected.wait_for_initial_inventory(3, lambda _row: True, timeout=0.01)

    def test_fixture_counts_must_be_present_after_inventory_list_end(self):
        adapter = self.make_adapter(self.state(inventory=[]))
        with self.assertRaises(TimeoutError):
            adapter.wait_for_initial_inventory(3, lambda row: bool(row["inventory"]), timeout=0.01)


class CharacterSnapshotRestorationTests(unittest.TestCase):
    def make_adapter(self, snapshot: dict, current: dict) -> RathenaAdapter:
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        state = dict(current)
        adapter.wait_offline = lambda _name: dict(state)
        adapter.character = lambda _name: dict(state)

        def guarded_update(original, values, expected):
            if (state["online"] != 0 or state["char_id"] != original["char_id"]
                    or state["account_id"] != original["account_id"]
                    or any(state[key] != expected[key] for key in values)):
                raise RuntimeError("guarded restoration precondition changed")
            state.update(values)

        adapter._update_character_fields = guarded_update
        adapter.test_state = state
        return adapter

    def snapshot(self) -> dict:
        return {
            "name": "Fixture", "char_id": 12, "account_id": 8,
            "map": "iz_int01", "x": 18, "y": 26, "online": 0,
            "class": 0, "base_level": 1, "job_level": 1, "base_exp": 0,
            "job_exp": 0, "status_point": 48, "skill_point": 0,
            "str": 1, "agi": 1, "vit": 1, "int": 1, "dex": 1, "luk": 1,
            "max_hp": 40, "hp": 40, "max_sp": 11, "sp": 11, "zeny": 0,
        }

    def test_death_induced_hp_sp_and_zeny_changes_restore_exactly(self):
        original = self.snapshot()
        current = {**original, "hp": 0, "sp": 0, "zeny": 125,
                   "account_group": 7, "unrelated_state": "keep"}
        adapter = self.make_adapter(original, current)
        restored = adapter.restore_character_snapshot(original)
        self.assertTrue(all(adapter.test_state[key] == original[key]
                            for key in RathenaAdapter.CHARACTER_FIELDS))
        self.assertEqual(adapter.test_state["account_group"], 7)
        self.assertEqual(adapter.test_state["unrelated_state"], "keep")
        self.assertEqual({key: restored[key] for key in original}, original)

    def test_online_or_reassigned_character_fails_closed(self):
        original = self.snapshot()
        online = {**original, "online": 1}
        adapter = self.make_adapter(original, online)
        with self.assertRaisesRegex(RuntimeError, "became online"):
            adapter.restore_character_snapshot(original)
        reassigned = {**original, "account_id": 9}
        adapter = self.make_adapter(original, reassigned)
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            adapter.restore_character_snapshot(original)

    def test_unconfirmed_write_fails_instead_of_claiming_restoration(self):
        original = self.snapshot()
        current = {**original, "hp": 0, "sp": 0}
        adapter = self.make_adapter(original, current)
        adapter._update_character_fields = lambda *_args: None
        with self.assertRaisesRegex(RuntimeError, "restoration did not verify"):
            adapter.restore_character_snapshot(original)


class PositionRestorationTests(unittest.TestCase):
    def original(self) -> dict:
        return {"name": "Fixture", "char_id": 12, "account_id": 8,
                "map": "iz_int01", "x": 18, "y": 26, "online": 0}

    def adapter(self, row: dict, row_count: str = "1") -> RathenaAdapter:
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        state = dict(row)
        adapter.wait_offline = lambda _name: dict(state)
        adapter.character = lambda _name: dict(state)

        def execute(sql: str):
            adapter.last_sql = sql
            if row_count == "1":
                state.update(map="iz_int01", x=18, y=26)
            return type("Result", (), {"returncode": 0, "stdout": row_count + "\n"})()

        adapter._mysql = execute
        adapter.char_db = "ragnarok"
        return adapter

    def test_position_restore_checks_identity_and_compares_observed_state(self):
        original = self.original()
        adapter = self.adapter({**original, "map": "prt_fild08", "x": 10, "y": 20})
        restored = adapter.restore_position("Fixture", original)
        self.assertEqual((restored["map"], restored["x"], restored["y"]),
                         (original["map"], original["x"], original["y"]))
        self.assertIn("AND last_map='prt_fild08' AND last_x=10 AND last_y=20", adapter.last_sql)

    def test_position_restore_fails_on_concurrent_change_or_reassignment(self):
        original = self.original()
        concurrent = self.adapter({**original, "map": "prt_fild08", "x": 10, "y": 20}, row_count="0")
        with self.assertRaisesRegex(RuntimeError, "concurrent state change"):
            concurrent.restore_position("Fixture", original)
        reassigned = self.adapter({**original, "account_id": 9})
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            reassigned.restore_position("Fixture", original)

if __name__ == "__main__":
    unittest.main()
