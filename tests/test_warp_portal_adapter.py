from __future__ import annotations

import types
import unittest

from korelab.adapters.rathena import RathenaAdapter


class WarpPortalAdapterTests(unittest.TestCase):
    def test_memo_fixture_is_offline_character_scoped_and_verified(self):
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        adapter.char_db = "test_char_db"
        character = {"name": "Fixture", "char_id": 12, "account_id": 8}
        adapter.character = lambda name: {**character, "online": 0}
        adapter.wait_offline = lambda name: {**character, "online": 0}
        statements = []
        original = [{"memo_id": 4, "char_id": 12, "map": "gef_fild01", "x": 90, "y": 91}]
        def mysql(sql):
            statements.append(sql)
            state[:] = [{"memo_id": 5, "char_id": 12, "map": "prontera", "x": 100, "y": 120}]
            return types.SimpleNamespace(returncode=0, stdout="")

        adapter._mysql = mysql
        expected = [{"memo_id": 5, "char_id": 12, "map": "prontera", "x": 100, "y": 120}]
        state = list(original)
        adapter.wait_offline = lambda name: {**character, "online": 0}
        adapter.memo_rows = lambda selected: list(state)
        fixture = [{"map": "prontera", "x": 100, "y": 120}]
        actual = adapter.prepare_memo_rows(character, fixture)
        self.assertEqual(actual, expected)
        self.assertIn("WHERE char_id=12 AND EXISTS", statements[0])
        self.assertIn("account_id=8 AND online=0", statements[0])
        self.assertIn("INSERT INTO test_char_db.memo (char_id,map,x,y)", statements[0])
        self.assertEqual(adapter.prepare_memo_rows(character, fixture), actual)
        self.assertEqual(len(statements), 1, "repeating a prepared fixture should not rewrite it")

    def test_memo_fixture_rejects_online_or_malformed_scope(self):
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        adapter.char_db = "test_char_db"
        character = {"name": "Fixture", "char_id": 12, "account_id": 8}
        adapter.wait_offline = lambda name: {**character, "online": 1}
        adapter.character = lambda name: {**character, "online": 1}
        with self.assertRaisesRegex(RuntimeError, "offline"):
            adapter.prepare_memo_rows(character, [{"map": "prontera", "x": 1, "y": 2}])
        adapter.character = lambda name: {**character, "online": 0}
        adapter.memo_rows = lambda selected: []
        adapter._mysql = lambda sql: types.SimpleNamespace(returncode=0, stdout="")
        with self.assertRaisesRegex(ValueError, "valid map name"):
            adapter.prepare_memo_rows(character, [{"map": "prontera'; DROP TABLE memo", "x": 1, "y": 2}])
        with self.assertRaisesRegex(ValueError, "at most three"):
            adapter.prepare_memo_rows(character, [
                {"map": "prontera", "x": i, "y": i} for i in range(4)
            ])

    def test_memo_restoration_requires_matching_character_and_exact_rows(self):
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        adapter.char_db = "test_char_db"
        character = {"name": "Fixture", "char_id": 12, "account_id": 8}
        adapter.wait_offline = lambda name: {**character, "online": 0}
        adapter.character = lambda name: {**character, "online": 0}
        statements = []
        original = [{"memo_id": 4, "char_id": 12, "map": "gef_fild01", "x": 90, "y": 91}]
        state = [{"memo_id": 5, "char_id": 12, "map": "prontera", "x": 1, "y": 2}]

        def mysql(sql):
            statements.append(sql)
            state[:] = original
            return types.SimpleNamespace(returncode=0, stdout="")

        adapter._mysql = mysql
        adapter.memo_rows = lambda selected: list(state)
        restored = adapter.restore_memo_rows(character, original)
        self.assertEqual(restored, original)
        self.assertIn("memo_id,char_id,map,x,y", statements[0])
        self.assertIn("account_id=8 AND online=0", statements[0])
        with self.assertRaisesRegex(ValueError, "snapshot"):
            adapter.restore_memo_rows(character, [{**original[0], "char_id": 13}])

    def test_partial_memo_preparation_restores_the_exact_original_snapshot(self):
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        adapter.char_db = "test_char_db"
        character = {"name": "Fixture", "char_id": 12, "account_id": 8}
        original = [{"memo_id": 4, "char_id": 12, "map": "gef_fild01", "x": 90, "y": 91}]
        state = list(original)
        statements = []
        adapter.wait_offline = lambda name: {**character, "online": 0}
        adapter.character = lambda name: {**character, "online": 0}
        adapter.memo_rows = lambda selected: list(state)

        def mysql(sql):
            statements.append(sql)
            if len(statements) == 1:
                state.clear()  # DELETE succeeded; a later statement failed.
                return types.SimpleNamespace(returncode=1, stdout="partial SQL failure")
            state[:] = original
            return types.SimpleNamespace(returncode=0, stdout="")

        adapter._mysql = mysql
        with self.assertRaisesRegex(RuntimeError, "original memo snapshot was restored"):
            adapter.prepare_memo_rows(character, [{"map": "prontera", "x": 100, "y": 120}])
        self.assertEqual(state, original)
        self.assertIn("memo_id,char_id,map,x,y", statements[1])

    def test_partial_memo_restoration_retries_until_exact_snapshot_is_observed(self):
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        adapter.char_db = "test_char_db"
        character = {"name": "Fixture", "char_id": 12, "account_id": 8}
        original = [{"memo_id": 4, "char_id": 12, "map": "gef_fild01", "x": 90, "y": 91}]
        state = [{"memo_id": 5, "char_id": 12, "map": "prontera", "x": 1, "y": 2}]
        statements = []
        adapter.wait_offline = lambda name: {**character, "online": 0}
        adapter.character = lambda name: {**character, "online": 0}
        adapter.memo_rows = lambda selected: list(state)

        def mysql(sql):
            statements.append(sql)
            if len(statements) == 1:
                state.clear()
                return types.SimpleNamespace(returncode=1, stdout="partial SQL failure")
            state[:] = original
            return types.SimpleNamespace(returncode=0, stdout="")

        adapter._mysql = mysql
        self.assertEqual(adapter.restore_memo_rows(character, original), original)
        self.assertEqual(state, original)
        self.assertEqual(len(statements), 2)

    def test_failed_memo_preparation_reports_unconfirmed_restoration(self):
        adapter = RathenaAdapter.__new__(RathenaAdapter)
        adapter.char_db = "test_char_db"
        character = {"name": "Fixture", "char_id": 12, "account_id": 8}
        original = [{"memo_id": 4, "char_id": 12, "map": "gef_fild01", "x": 90, "y": 91}]
        state = list(original)
        adapter.wait_offline = lambda name: {**character, "online": 0}
        adapter.character = lambda name: {**character, "online": 0}
        adapter.memo_rows = lambda selected: list(state)

        def mysql(sql):
            state.clear()
            return types.SimpleNamespace(returncode=1, stdout="database unavailable")

        adapter._mysql = mysql
        with self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
            adapter.prepare_memo_rows(character, [{"map": "prontera", "x": 100, "y": 120}])
        self.assertEqual(state, [])


if __name__ == "__main__":
    unittest.main()
