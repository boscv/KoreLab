from __future__ import annotations

import os
import re
import socket
import time
from pathlib import Path

from ..database import choose_mysql_provider
from ..processes import local_listener_processes, process_is_under


class RathenaAdapter:
    CHARACTER_FIELDS = ("class", "base_level", "job_level", "base_exp", "job_exp",
                        "status_point", "skill_point", "str", "agi", "vit", "int", "dex",
                        "luk", "max_hp", "hp", "max_sp", "sp", "zeny")
    PREPARABLE_FIELDS = {"str", "agi", "vit", "int", "dex", "luk", "max_hp", "hp",
                         "max_sp", "sp", "zeny"}
    COMBAT_SETUP_FIELDS = {"class", "skill_point"}
    CHARACTER_SETUP_FIELDS = {"class", "base_level", "job_level", "status_point", "skill_point",
                             "str", "agi", "vit", "int", "dex", "luk", "max_hp", "hp",
                             "max_sp", "sp", "zeny"}
    INVENTORY_FIELDS = ("id", "char_id", "nameid", "amount", "equip", "identify", "refine",
        "attribute", "card0", "card1", "card2", "card3", "option_id0", "option_val0",
        "option_parm0", "option_id1", "option_val1", "option_parm1", "option_id2",
        "option_val2", "option_parm2", "option_id3", "option_val3", "option_parm3",
        "option_id4", "option_val4", "option_parm4", "expire_time", "favorite", "bound",
        "unique_id", "equip_switch", "enchantgrade")

    def __init__(self, checkout: Path):
        self.checkout = checkout.expanduser().resolve()
        self.db_host = os.environ.get("KORELAB_DB_HOST", "127.0.0.1")
        self.db_port = int(os.environ.get("KORELAB_DB_PORT", "3306"))
        self.db_user = os.environ.get("KORELAB_DB_USER", "ragnarok")
        self.char_db = self._identifier(os.environ.get("KORELAB_CHAR_DB", "ragnarok"))
        self.login_db = self._identifier(os.environ.get("KORELAB_LOGIN_DB", "ragnarok"))
        self.db_provider = choose_mysql_provider()

    @staticmethod
    def _identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise ValueError("database schema must be an alphanumeric SQL identifier")
        return value

    def _mysql(self, sql: str, *, timeout: int = 10):
        if self.db_provider is None:
            raise RuntimeError("no MySQL/MariaDB protocol provider is installed; install a compatible Python driver or mysql/mariadb client, then run `korelab doctor`")
        return self.db_provider.execute(sql, timeout=timeout)

    def require_services(self) -> dict[str, bool]:
        host = os.environ.get("KORELAB_SERVICE_HOST", "127.0.0.1")
        ports = (int(os.environ.get("KORELAB_LOGIN_PORT", "6900")),
                 int(os.environ.get("KORELAB_CHAR_PORT", "6121")),
                 int(os.environ.get("KORELAB_MAP_PORT", "5121")))
        checks = {}
        for port in ports:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    owners = local_listener_processes(port)
                    checks[str(port)] = any(process_is_under(owner["path"], self.checkout)
                                            for owner in owners)
            except OSError:
                checks[str(port)] = False
        if not all(checks.values()):
            raise RuntimeError(f"local rAthena listeners are missing or their process identity does not match the configured checkout: {checks}")
        return checks

    def character(self, name: str) -> dict[str, object]:
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise ValueError("character name contains unsupported SQL characters")
        result = self._mysql(
            f"SELECT char_id,account_id,name,last_map,last_x,last_y,online,"
            f"{','.join(f'`{field}`' for field in self.CHARACTER_FIELDS)} "
            f"FROM {self.char_db}.char WHERE name='{name}'")
        if result.returncode:
            raise RuntimeError("could not read the local rAthena character row")
        fields = result.stdout.strip().split("\t")
        if len(fields) != 7 + len(self.CHARACTER_FIELDS):
            raise RuntimeError(f"expected one character row, received {len(fields)}")
        row = {"char_id": int(fields[0]), "account_id": int(fields[1]), "name": fields[2],
               "map": fields[3], "x": int(fields[4]), "y": int(fields[5]), "online": int(fields[6])}
        row.update({key: int(value) for key, value in zip(self.CHARACTER_FIELDS, fields[7:])})
        return row

    def account_group(self, account_id: int) -> int:
        result = self._mysql(f"SELECT group_id FROM {self.login_db}.login WHERE account_id={account_id}")
        if result.returncode:
            raise RuntimeError("could not read the local account privilege group")
        try:
            return int(result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError("expected exactly one local account privilege row") from exc

    def character_skills(self, character: dict[str, object]) -> dict[str, int]:
        return {skill_id: int(row["lv"]) for skill_id, row in
                self.character_skill_rows(character).items() if int(row["lv"]) > 0}

    def inventory_rows(self, character: dict[str, object], item_ids: tuple[int, ...]) -> list[dict[str, int]]:
        if any(type(item_id) is not int or item_id <= 0 for item_id in item_ids):
            raise ValueError("inventory snapshot requires positive numeric item IDs")
        if not item_ids:
            return []
        ids = ",".join(map(str, sorted(set(item_ids))))
        return self._read_inventory_rows(character, f"nameid IN ({ids})",
                                         "could not read selected character inventory from rAthena")

    def _read_inventory_rows(self, character: dict[str, object], condition: str | None,
                             failure: str) -> list[dict[str, int]]:
        fields = ",".join(f"`{field}`" for field in self.INVENTORY_FIELDS)
        selected = f" AND {condition}" if condition else ""
        result = self._mysql(f"SELECT {fields} FROM {self.char_db}.inventory "
                             f"WHERE char_id={int(character['char_id'])}{selected} ORDER BY id")
        if result.returncode:
            raise RuntimeError(failure)
        rows = []
        for line in result.stdout.splitlines():
            values = line.split("\t")
            if len(values) != len(self.INVENTORY_FIELDS):
                raise RuntimeError("rAthena inventory row did not match the active schema")
            rows.append(dict(zip(self.INVENTORY_FIELDS, map(int, values))))
        return rows

    def all_inventory_rows(self, character: dict[str, object]) -> list[dict[str, int]]:
        """Read the complete inventory snapshot so unplanned drops can be cleaned up too."""
        return self._read_inventory_rows(character, None,
                                         "could not read the complete character inventory from rAthena")

    def storage_rows(self, account_id: int, item_ids: tuple[int, ...]) -> list[dict[str, int]]:
        """Read selected account-storage stacks for independent verification only."""
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("storage snapshot requires a positive account ID")
        if not item_ids or any(type(item_id) is not int or item_id <= 0 for item_id in item_ids):
            raise ValueError("storage snapshot requires positive numeric item IDs")
        ids = ",".join(map(str, sorted(set(item_ids))))
        result = self._mysql(f"SELECT id,account_id,nameid,amount FROM {self.char_db}.storage "
                             f"WHERE account_id={account_id} AND nameid IN ({ids}) ORDER BY id")
        if result.returncode:
            raise RuntimeError("could not read local rAthena account-storage rows")
        rows = []
        for line in result.stdout.splitlines():
            values = line.split("\t")
            if len(values) != 4:
                raise RuntimeError("rAthena storage row did not match the active schema")
            rows.append(dict(zip(("id", "account_id", "nameid", "amount"), map(int, values))))
        return rows

    def restore_storage_rows(self, account_id: int, item_ids: tuple[int, ...],
                             original_rows: list[dict[str, int]]) -> list[dict[str, int]]:
        """Restore only selected account-storage rows after an offline test fixture."""
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("storage restoration requires a positive account ID")
        if not item_ids or any(type(item_id) is not int or item_id <= 0 for item_id in item_ids):
            raise ValueError("storage restoration requires positive numeric item IDs")
        ids_set = set(item_ids)
        if any(int(row.get("account_id", -1)) != account_id
               or int(row.get("nameid", -1)) not in ids_set for row in original_rows):
            raise ValueError("storage restoration rows must match the selected account and item IDs")
        if self.online_characters_for_account(account_id):
            raise RuntimeError("storage restoration requires every account character to be offline")
        ids = ",".join(map(str, sorted(ids_set)))
        delete = (f"DELETE FROM {self.char_db}.storage WHERE account_id={account_id} AND nameid IN ({ids}) "
                  f"AND NOT EXISTS (SELECT 1 FROM {self.char_db}.char WHERE account_id={account_id} AND online<>0)")
        statements = [delete]
        for row in original_rows:
            statements.append(f"INSERT INTO {self.char_db}.storage (id,account_id,nameid,amount) "
                              f"SELECT {int(row['id'])},{account_id},{int(row['nameid'])},{int(row['amount'])} "
                              f"FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM {self.char_db}.char "
                              f"WHERE account_id={account_id} AND online<>0)")
        result = self._mysql("; ".join(statements))
        if result.returncode:
            raise RuntimeError("rAthena rejected guarded account-storage restoration")
        restored = self.storage_rows(account_id, tuple(sorted(ids_set)))
        if restored != sorted(original_rows, key=lambda row: row["id"]):
            raise RuntimeError("restored account-storage rows did not exactly match the original snapshot")
        return restored

    def equipped_inventory_rows(self, character: dict[str, object]) -> list[dict[str, int]]:
        return self._read_inventory_rows(character, "equip<>0",
                                         "could not read currently equipped rAthena inventory rows")

    @staticmethod
    def inventory_amount(rows: list[dict[str, int]], item_id: int) -> int:
        return sum(int(row["amount"]) for row in rows if int(row["nameid"]) == item_id)

    def quest_rows(self, character: dict[str, object], quest_ids: tuple[int, ...]) -> list[dict[str, int]]:
        if not quest_ids or any(type(quest_id) is not int or quest_id <= 0 for quest_id in quest_ids):
            raise ValueError("quest snapshot requires positive numeric IDs")
        ids = ",".join(map(str, sorted(set(quest_ids))))
        result = self._mysql(f"SELECT char_id,quest_id,state,time,count1,count2,count3 FROM {self.char_db}.quest "
            f"WHERE char_id={int(character['char_id'])} AND quest_id IN ({ids}) ORDER BY quest_id")
        if result.returncode:
            raise RuntimeError("could not read selected rAthena quest states")
        rows = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 7:
                raise RuntimeError("rAthena quest row did not match the active schema")
            rows.append(dict(zip(("char_id", "quest_id", "state", "time", "count1", "count2", "count3"),
                                 (int(fields[0]), int(fields[1]), int(fields[2]), *map(int, fields[3:])))))
        return rows

    def memo_rows(self, character: dict[str, object]) -> list[dict[str, object]]:
        """Read the ordered Warp Portal memo snapshot for one character."""
        result = self._mysql(
            f"SELECT memo_id,char_id,`map`,x,y FROM {self.char_db}.memo "
            f"WHERE char_id={int(character['char_id'])} ORDER BY memo_id")
        if result.returncode:
            raise RuntimeError("could not read selected character Warp Portal memos")
        rows = []
        for line in result.stdout.splitlines():
            values = line.split("\t")
            if len(values) != 5:
                raise RuntimeError("rAthena memo row did not match the active schema")
            rows.append({"memo_id": int(values[0]), "char_id": int(values[1]), "map": values[2],
                         "x": int(values[3]), "y": int(values[4])})
        return rows

    @staticmethod
    def _validate_memo_rows(rows: list[dict[str, object]], *, snapshot: bool = False) -> None:
        label = "memo snapshot" if snapshot else "memo fixture"
        if not isinstance(rows, list) or len(rows) > 3:
            raise ValueError(f"{label} supports at most three memo rows")
        expected = {"memo_id", "char_id", "map", "x", "y"} if snapshot else {"map", "x", "y"}
        for row in rows:
            if not isinstance(row, dict) or set(row) != expected:
                raise ValueError(f"{label} row has an invalid shape")
            if (not isinstance(row["map"], str) or not re.fullmatch(r"[a-z0-9_]{1,11}", row["map"])):
                raise ValueError(f"{label} requires a valid map name")
            if any(type(row[key]) is not int or not 0 <= row[key] <= 999 for key in ("x", "y")):
                raise ValueError(f"{label} coordinates must be bounded non-negative integers")
            if snapshot and any(type(row[key]) is not int or row[key] <= 0
                                for key in ("memo_id", "char_id")):
                raise ValueError("memo snapshot contains an invalid row identity")
        if snapshot and len({row["memo_id"] for row in rows}) != len(rows):
            raise ValueError("memo snapshot contains duplicate row identities")

    def prepare_memo_rows(self, character: dict[str, object],
                          rows: list[dict[str, object]]) -> list[dict[str, object]]:
        """Replace a character's memo fixture while the verified owner is offline."""
        self._validate_memo_rows(rows)
        current = self.wait_offline(str(character["name"]))
        if (current["online"] != 0 or current["char_id"] != character["char_id"]
                or current["account_id"] != character["account_id"]):
            raise RuntimeError("memo preparation requires the same character offline")
        original = self.memo_rows(character)
        self._validate_memo_rows(original, snapshot=True)
        if any(row["char_id"] != character["char_id"] for row in original):
            raise RuntimeError("selected character memo snapshot contains a different character ID")
        if self._memo_fixture_matches(original, rows):
            return original
        try:
            return self._write_memo_rows(character, rows, exact=False)
        except Exception as preparation_error:
            restore_error = None
            restored = False
            for _ in range(2):
                try:
                    self._write_memo_rows(character, original, exact=True)
                    restored = True
                    break
                except Exception as exc:
                    restore_error = exc
            if not restored:
                raise RuntimeError(
                    "memo preparation failed and exact restoration of the original snapshot could not be confirmed"
                ) from restore_error or preparation_error
            raise RuntimeError("memo preparation failed; original memo snapshot was restored") from preparation_error

    def restore_memo_rows(self, character: dict[str, object],
                          original_rows: list[dict[str, object]]) -> list[dict[str, object]]:
        """Restore exact memo row identities and values after an offline scenario."""
        self._validate_memo_rows(original_rows, snapshot=True)
        if any(row["char_id"] != character["char_id"] for row in original_rows):
            raise ValueError("memo snapshot rows must belong to the selected character")
        self.wait_offline(str(character["name"]))
        current = self.character(str(character["name"]))
        if (current["char_id"] != character["char_id"]
                or current["account_id"] != character["account_id"]):
            raise RuntimeError("character/account identity changed; refusing memo restoration")
        if current["online"] != 0:
            raise RuntimeError("memo restoration requires the same character offline")
        if self.memo_rows(character) == original_rows:
            return original_rows
        first_error = None
        for _ in range(2):
            try:
                return self._write_memo_rows(character, original_rows, exact=True)
            except Exception as exc:
                first_error = first_error or exc
        raise RuntimeError(
            "memo restoration failed; exact original memo rows could not be confirmed after retry"
        ) from first_error

    @staticmethod
    def _memo_fixture_matches(actual: list[dict[str, object]],
                              expected: list[dict[str, object]]) -> bool:
        return len(actual) == len(expected) and all(
            (current["map"], current["x"], current["y"])
            == (requested["map"], requested["x"], requested["y"])
            for current, requested in zip(actual, expected))

    def _write_memo_rows(self, character: dict[str, object], rows: list[dict[str, object]],
                         *, exact: bool) -> list[dict[str, object]]:
        """Replace memo rows with guarded statements and verify the observed result."""
        char_id, account_id = int(character["char_id"]), int(character["account_id"])
        guard = (f"EXISTS (SELECT 1 FROM {self.char_db}.char WHERE char_id={char_id} "
                 f"AND account_id={account_id} AND online=0)")
        statements = [f"DELETE FROM {self.char_db}.memo WHERE char_id={char_id} AND {guard}"]
        for row in rows:
            if exact:
                statements.append(
                    f"INSERT INTO {self.char_db}.memo (memo_id,char_id,map,x,y) "
                    f"SELECT {row['memo_id']},{char_id},'{row['map']}',{row['x']},{row['y']} "
                    f"FROM DUAL WHERE {guard}")
            else:
                statements.append(
                    f"INSERT INTO {self.char_db}.memo (char_id,map,x,y) "
                    f"SELECT {char_id},'{row['map']}',{row['x']},{row['y']} FROM DUAL WHERE {guard}")
        result = self._mysql("; ".join(statements))
        if result.returncode:
            operation = "restoration" if exact else "preparation"
            raise RuntimeError(f"rAthena rejected guarded Warp Portal memo {operation}")
        current = self.character(str(character["name"]))
        if (current["online"] != 0 or current["char_id"] != char_id
                or current["account_id"] != account_id):
            raise RuntimeError("character/account identity changed or became online during memo replacement")
        observed = self.memo_rows(character)
        if exact and observed != rows:
            raise RuntimeError("restored Warp Portal memo rows did not exactly match the original snapshot")
        if not exact and not self._memo_fixture_matches(observed, rows):
            raise RuntimeError("prepared Warp Portal memos did not verify in rAthena")
        return observed

    def restore_quest_rows(self, character: dict[str, object], quest_ids: tuple[int, ...],
                           original_rows: list[dict[str, int]]) -> list[dict[str, int]]:
        self.wait_offline(str(character["name"]))
        current_character = self.character(str(character["name"]))
        if current_character["char_id"] != character["char_id"] or current_character["account_id"] != character["account_id"]:
            raise RuntimeError("character/account identity changed; refusing quest restoration")
        ids = ",".join(map(str, sorted(set(quest_ids))))
        delete = (f"DELETE FROM {self.char_db}.quest WHERE char_id={int(character['char_id'])} "
                  f"AND quest_id IN ({ids}) AND EXISTS (SELECT 1 FROM {self.char_db}.char "
                  f"WHERE char_id={int(character['char_id'])} AND account_id={int(character['account_id'])} AND online=0)")
        statements = [delete]
        for row in original_rows:
            statements.append(f"INSERT INTO {self.char_db}.quest (char_id,quest_id,state,time,count1,count2,count3) "
                f"SELECT {int(row['char_id'])},{int(row['quest_id'])},{int(row['state'])},{int(row['time'])},"
                f"{int(row['count1'])},{int(row['count2'])},{int(row['count3'])} FROM DUAL WHERE EXISTS "
                f"(SELECT 1 FROM {self.char_db}.char WHERE char_id={int(character['char_id'])} "
                f"AND account_id={int(character['account_id'])} AND online=0)")
        result = self._mysql("; ".join(statements))
        if result.returncode:
            raise RuntimeError("rAthena rejected guarded quest-state restoration")
        restored = self.quest_rows(character, quest_ids)
        if restored != original_rows:
            raise RuntimeError("restored quest rows did not exactly match the original snapshot")
        return restored

    def prepare_inventory_item(self, character: dict[str, object], item_id: int, amount: int) -> None:
        if type(item_id) is not int or item_id <= 0 or type(amount) is not int or not 1 <= amount <= 100:
            raise ValueError("inventory fixture requires a positive item ID and amount from 1 to 100")
        row = self.character(str(character["name"]))
        if row["online"] != 0 or row["char_id"] != character["char_id"] or row["account_id"] != character["account_id"]:
            raise RuntimeError("inventory preparation requires the same character offline")
        current = self.inventory_rows(character, (item_id,))
        if any(int(item["equip"]) for item in current):
            raise RuntimeError("inventory fixture item is equipped; refusing to alter equipment")
        matching = next((item for item in current if item["card0"] == item["card1"] == item["card2"] == item["card3"] == 0
                         and item["unique_id"] == 0 and item["expire_time"] == 0), None)
        if matching:
            sql = (f"UPDATE {self.char_db}.inventory i JOIN {self.char_db}.char c ON c.char_id=i.char_id "
                   f"SET i.amount=i.amount+{amount} WHERE i.id={matching['id']} AND i.amount={matching['amount']} "
                   f"AND i.char_id={int(character['char_id'])} AND i.nameid={item_id} AND i.equip=0 "
                   f"AND c.account_id={int(character['account_id'])} AND c.online=0; SELECT ROW_COUNT()")
        else:
            sql = (f"INSERT INTO {self.char_db}.inventory (char_id,nameid,amount,identify) "
                   f"SELECT char_id,{item_id},{amount},1 FROM {self.char_db}.char WHERE char_id={int(character['char_id'])} "
                   f"AND account_id={int(character['account_id'])} AND online=0; SELECT ROW_COUNT()")
        result = self._mysql(sql)
        if result.returncode or result.stdout.strip().splitlines()[-1:] != ["1"]:
            raise RuntimeError("guarded offline inventory fixture update failed")
        observed = self.inventory_rows(character, (item_id,))
        if self.inventory_amount(observed, item_id) != self.inventory_amount(current, item_id) + amount:
            raise RuntimeError("prepared inventory amount did not verify in rAthena")

    def restore_inventory_items(self, character: dict[str, object], item_ids: tuple[int, ...],
                                original_rows: list[dict[str, int]]) -> list[dict[str, int]]:
        self.wait_offline(str(character["name"]))
        current_character = self.character(str(character["name"]))
        if current_character["char_id"] != character["char_id"] or current_character["account_id"] != character["account_id"]:
            raise RuntimeError("character/account identity changed; refusing inventory restoration")
        ids = ",".join(map(str, sorted(set(item_ids))))
        columns = ",".join(f"`{field}`" for field in self.INVENTORY_FIELDS)
        delete = (f"DELETE FROM {self.char_db}.inventory WHERE char_id={int(character['char_id'])} "
                  f"AND nameid IN ({ids}) AND EXISTS (SELECT 1 FROM {self.char_db}.char "
                  f"WHERE char_id={int(character['char_id'])} AND account_id={int(character['account_id'])} "
                  f"AND online=0)")
        statements = [delete]
        for original in original_rows:
            values = ",".join(str(int(original[field])) for field in self.INVENTORY_FIELDS)
            statements.append(f"INSERT INTO {self.char_db}.inventory ({columns}) SELECT {values} FROM DUAL WHERE "
                              f"EXISTS (SELECT 1 FROM {self.char_db}.char WHERE char_id={int(character['char_id'])} "
                              f"AND account_id={int(character['account_id'])} AND online=0)")
        result = self._mysql("; ".join(statements))
        if result.returncode:
            raise RuntimeError("rAthena rejected guarded inventory restoration")
        restored = self.inventory_rows(character, item_ids)
        if restored != original_rows:
            raise RuntimeError("restored inventory rows did not exactly match the original snapshot")
        return restored

    def character_skill_rows(self, character: dict[str, object]) -> dict[str, dict[str, int]]:
        result = self._mysql(f"SELECT id,lv,flag FROM {self.char_db}.skill WHERE char_id={int(character['char_id'])}")
        if result.returncode:
            raise RuntimeError("could not read the character skill list")
        skills: dict[str, dict[str, int]] = {}
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) == 3:
                skills[fields[0]] = {"lv": int(fields[1]), "flag": int(fields[2])}
        return skills

    def set_character_skill(self, character: dict[str, object], skill_id: str, level: int,
                            flag: int = 0) -> None:
        if not skill_id.isdigit() or not 1 <= int(level) <= 10:
            raise ValueError("prepared skill requires a numeric ID and level from 1 to 10")
        if flag not in (0, 3):
            raise ValueError("prepared skill flag is unsupported")
        current_character = self.character(str(character["name"]))
        if (current_character["char_id"] != character["char_id"]
                or current_character["account_id"] != character["account_id"]
                or current_character["online"] != 0):
            raise RuntimeError("skill fixture requires the same character offline")
        previous = self.character_skill_rows(character).get(skill_id)
        if previous:
            sql = (f"UPDATE {self.char_db}.skill s JOIN {self.char_db}.char c ON c.char_id=s.char_id "
                   f"SET s.lv={int(level)},s.flag={flag} WHERE s.char_id={int(character['char_id'])} "
                   f"AND s.id={int(skill_id)} AND s.lv={previous['lv']} AND s.flag={previous['flag']} "
                   f"AND c.account_id={int(character['account_id'])} AND c.online=0; SELECT ROW_COUNT()")
        else:
            sql = (f"INSERT INTO {self.char_db}.skill (char_id,id,lv,flag) "
                   f"SELECT char_id,{int(skill_id)},{int(level)},{flag} FROM {self.char_db}.char "
                   f"WHERE char_id={int(character['char_id'])} AND account_id={int(character['account_id'])} "
                   f"AND online=0 AND NOT EXISTS (SELECT 1 FROM {self.char_db}.skill "
                   f"WHERE char_id={int(character['char_id'])} AND id={int(skill_id)}); SELECT ROW_COUNT()")
        result = self._mysql(sql)
        if result.returncode or result.stdout.strip().splitlines()[-1:] != ["1"]:
            raise RuntimeError("guarded character skill fixture update did not affect exactly one row")
        observed = self.character_skill_rows(character).get(skill_id)
        if observed != {"lv": int(level), "flag": flag}:
            raise RuntimeError("prepared character skill did not verify in rAthena")

    def restore_character_skills(self, character: dict[str, object],
                                 original: dict[str, dict[str, int]], changed_ids: dict[str, int]) -> None:
        self.wait_offline(str(character["name"]))
        current_character = self.character(str(character["name"]))
        if (current_character["char_id"] != character["char_id"]
                or current_character["account_id"] != character["account_id"]):
            raise RuntimeError("character/account identity changed; refusing skill restoration")
        current = self.character_skill_rows(character)
        for skill_id in changed_ids:
            before, now = original.get(skill_id), current.get(skill_id)
            if before == now:
                continue
            if before is None and now is not None:
                sql = (f"DELETE FROM {self.char_db}.skill "
                       f"WHERE char_id={int(character['char_id'])} AND id={int(skill_id)} "
                       f"AND lv={now['lv']} AND flag={now['flag']} AND EXISTS "
                       f"(SELECT 1 FROM {self.char_db}.char WHERE char_id={int(character['char_id'])} "
                       f"AND account_id={int(character['account_id'])} AND online=0); SELECT ROW_COUNT()")
            elif before is not None and now is None:
                sql = (f"INSERT INTO {self.char_db}.skill (char_id,id,lv,flag) "
                       f"SELECT char_id,{int(skill_id)},{before['lv']},{before['flag']} "
                       f"FROM {self.char_db}.char WHERE char_id={int(character['char_id'])} "
                       f"AND account_id={int(character['account_id'])} AND online=0 "
                       f"AND NOT EXISTS (SELECT 1 FROM {self.char_db}.skill WHERE char_id={int(character['char_id'])} "
                       f"AND id={int(skill_id)}); SELECT ROW_COUNT()")
            else:
                sql = (f"UPDATE {self.char_db}.skill s JOIN {self.char_db}.char c ON c.char_id=s.char_id "
                       f"SET s.lv={before['lv']},s.flag={before['flag']} "
                       f"WHERE s.char_id={int(character['char_id'])} AND s.id={int(skill_id)} "
                       f"AND s.lv={now['lv']} AND s.flag={now['flag']} "
                       f"AND c.account_id={int(character['account_id'])} AND c.online=0; SELECT ROW_COUNT()")
            result = self._mysql(sql)
            if result.returncode or result.stdout.strip().splitlines()[-1:] != ["1"]:
                raise RuntimeError("guarded character skill restoration did not affect exactly one row")
        restored = self.character_skill_rows(character)
        if any(restored.get(skill_id) != original.get(skill_id) for skill_id in changed_ids):
            raise RuntimeError("character skill restoration did not verify")

    def group_allows(self, group_id: int, commands: tuple[str, ...]) -> bool:
        path = self.checkout / "conf/groups.yml"
        text = path.read_text(encoding="utf-8")
        match = re.search(rf"(?ms)^  - Id: {group_id}\s*$([\s\S]*?)(?=^  - Id:|\Z)", text)
        if not match:
            return False
        block = match.group(1)
        permissions = re.search(r"(?ms)^    Permissions:\s*$(.*?)(?=^    [A-Za-z]|\Z)", block)
        if permissions and re.search(r"(?m)^      all_commands:\s*true\s*$", permissions.group(1)):
            return True
        commands_block = re.search(r"(?ms)^    Commands:\s*$(.*?)(?=^    [A-Za-z]|\Z)", block)
        if not commands_block:
            return False
        body = commands_block.group(1)
        return all(re.search(rf"(?m)^      {re.escape(command)}:\s*true\s*$", body)
                   for command in commands)

    def set_account_group(self, account_id: int, expected_group: int, new_group: int) -> None:
        result = self._mysql(
            f"UPDATE {self.login_db}.login SET group_id={new_group} WHERE account_id={account_id} "
            f"AND group_id={expected_group}; SELECT ROW_COUNT()")
        if result.returncode or result.stdout.strip().splitlines()[-1:] != ["1"]:
            raise RuntimeError("guarded temporary account-group update did not affect exactly one row")
        if self.account_group(account_id) != new_group:
            raise RuntimeError("temporary account-group update failed verification")

    def online_characters_for_account(self, account_id: int) -> int:
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("invalid account identifier")
        result = self._mysql(
            f"SELECT COUNT(*) FROM {self.char_db}.char WHERE account_id={account_id} AND online<>0")
        if result.returncode:
            raise RuntimeError("could not verify account exclusivity in the local character database")
        try:
            return int(result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError("invalid account online-character count") from exc

    def update_character_fields(self, original: dict[str, object], values: dict[str, int],
                                expected: dict[str, int]) -> None:
        if set(values) - self.PREPARABLE_FIELDS:
            raise ValueError("scenario preparation cannot change progression or identity fields")
        self._update_character_fields(original, values, expected)

    def prepare_combat_character(self, original: dict[str, object], job_class_id: int,
                                 skill_points: int) -> dict[str, object]:
        """Apply a minimal offline, guarded class/skill-point fixture."""
        values = {"class": job_class_id, "skill_point": skill_points}
        if not 0 <= job_class_id <= 1000 or not 0 <= skill_points <= 100:
            raise ValueError("unsupported temporary job class or skill-point value")
        if set(values) - self.COMBAT_SETUP_FIELDS:
            raise ValueError("unsupported combat setup field")
        self._update_character_fields(original, values,
                                      {key: int(original[key]) for key in values})
        return self.character(str(original["name"]))

    def prepare_character_state(self, original: dict[str, object], values: dict[str, int]) -> dict[str, object]:
        """Apply a small, offline, identity-guarded character fixture for purpose-driven tests."""
        if not values or set(values) - self.CHARACTER_SETUP_FIELDS:
            raise ValueError("character fixture contains no fields or unsupported identity fields")
        for field, value in values.items():
            if type(value) is not int or not 0 <= value <= 2_000_000_000:
                raise ValueError("character fixture values must be bounded non-negative integers")
            if field == "base_level" and not 1 <= value <= 250:
                raise ValueError("prepared base level must be between 1 and 250")
            if field == "job_level" and not 1 <= value <= 70:
                raise ValueError("prepared job level must be between 1 and 70")
            if field == "class" and value > 1000:
                raise ValueError("prepared class ID is outside the supported range")
        self._update_character_fields(original, values,
            {field: int(original[field]) for field in values})
        return self.character(str(original["name"]))

    def _update_character_fields(self, original: dict[str, object], values: dict[str, int],
                                 expected: dict[str, int]) -> None:
        if not values:
            return
        if set(values) - set(self.CHARACTER_FIELDS):
            raise ValueError("unsupported character preparation field")
        if any(type(value) is not int or not 0 <= value <= 2_000_000_000 for value in values.values()):
            raise ValueError("character preparation values must be integers in the supported range")
        row = self.character(str(original["name"]))
        if row["online"] != 0 or row["char_id"] != original["char_id"] or row["account_id"] != original["account_id"]:
            raise RuntimeError("character/account identity changed or character is online; refusing fixture update")
        assignments = ",".join(f"`{field}`={value}" for field, value in values.items())
        guards = " AND ".join(f"`{field}`={int(expected[field])}" for field in values)
        sql = (f"UPDATE {self.char_db}.char SET {assignments} WHERE char_id={row['char_id']} "
               f"AND account_id={row['account_id']} AND online=0 AND {guards}; SELECT ROW_COUNT()")
        result = self._mysql(sql)
        if result.returncode or result.stdout.strip().splitlines()[-1:] != ["1"]:
            raise RuntimeError("guarded character fixture update did not affect exactly one offline row")
        updated = self.character(str(original["name"]))
        if updated["online"] != 0 or any(updated[key] != value for key, value in values.items()):
            raise RuntimeError("character fixture update failed independent database verification")

    def wait_offline(self, name: str, timeout: float = 20) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        stable = 0
        row = self.character(name)
        while time.monotonic() < deadline:
            row = self.character(name)
            stable = stable + 1 if row["online"] == 0 else 0
            if stable >= 3:
                return row
            time.sleep(0.5)
        raise TimeoutError(f"character did not remain offline; online={row['online']}")

    def restore_position(self, name: str, original: dict[str, object]) -> dict[str, object]:
        row = self.wait_offline(name)
        map_name, x, y = str(original["map"]), int(original["x"]), int(original["y"])
        if not re.fullmatch(r"[a-z0-9_]+", map_name) or x < 0 or y < 0:
            raise ValueError("saved original position is invalid")
        if (row["char_id"] != original["char_id"]
                or row["account_id"] != original["account_id"]):
            raise RuntimeError("character/account identity changed; refusing position restoration")
        current_position = (str(row["map"]), int(row["x"]), int(row["y"]))
        target_position = (map_name, x, y)
        if current_position != target_position:
            if not re.fullmatch(r"[a-z0-9_]+", current_position[0]) or min(current_position[1:]) < 0:
                raise RuntimeError("observed current position is invalid; refusing position restoration")
            sql = (f"UPDATE {self.char_db}.char SET last_map='{map_name}',last_x={x},last_y={y} "
                   f"WHERE char_id={int(original['char_id'])} AND account_id={int(original['account_id'])} "
                   f"AND online=0 AND last_map='{current_position[0]}' AND last_x={current_position[1]} "
                   f"AND last_y={current_position[2]}; SELECT ROW_COUNT()")
            result = self._mysql(sql)
            if result.returncode or result.stdout.strip().splitlines()[-1:] != ["1"]:
                raise RuntimeError("guarded offline position restoration detected a concurrent state change")
        restored = self.character(name)
        if restored["online"] != 0 or any(restored[key] != value for key, value in
                                          (("map", map_name), ("x", x), ("y", y))):
            raise RuntimeError("rAthena database position restoration did not verify")
        return restored

    def prepare_position(self, original: dict[str, object], map_name: str, x: int, y: int) -> dict[str, object]:
        """Move an offline test character to a declared starting location, with identity/state guards."""
        name = str(original.get("name", ""))
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise ValueError("test character name is invalid")
        if (not re.fullmatch(r"[a-z0-9_]+", map_name) or type(x) is not int or type(y) is not int
                or not (0 <= x <= 999 and 0 <= y <= 999)):
            raise ValueError("prepared test location requires a valid map and bounded integer coordinates")
        map_index = self.checkout / "db/map_index.txt"
        if not map_index.is_file() or not re.search(rf"(?m)^\s*{re.escape(map_name)}\s*$", map_index.read_text()):
            raise ValueError("prepared location map is not present in this rAthena map index")
        row = self.wait_offline(name)
        identity = ("char_id", "account_id")
        location = ("map", "x", "y")
        if any(row[key] != original[key] for key in identity + location):
            raise RuntimeError("offline character identity or starting location changed; refusing fixture preparation")
        sql = (f"UPDATE {self.char_db}.char SET last_map='{map_name}',last_x={x},last_y={y} "
               f"WHERE char_id={row['char_id']} AND account_id={row['account_id']} AND online=0 "
               f"AND last_map='{row['map']}' AND last_x={int(row['x'])} AND last_y={int(row['y'])}; "
               "SELECT ROW_COUNT()")
        result = self._mysql(sql)
        if result.returncode or result.stdout.strip().splitlines()[-1:] != ["1"]:
            raise RuntimeError("guarded offline test-position preparation did not affect exactly one character")
        prepared = self.character(name)
        if prepared["online"] != 0 or any(prepared[key] != value for key, value in
                (("char_id", row["char_id"]), ("account_id", row["account_id"]),
                 ("map", map_name), ("x", x), ("y", y))):
            raise RuntimeError("prepared offline test position failed independent database verification")
        return prepared

    def restore_character_fields(self, original: dict[str, object], changed: dict[str, int],
                                 expected: dict[str, int]) -> dict[str, object]:
        self.wait_offline(str(original["name"]))
        current = self.character(str(original["name"]))
        if current["char_id"] != original["char_id"] or current["account_id"] != original["account_id"]:
            raise RuntimeError("character/account identity changed; refusing attribute restoration")
        # Restore only values KoreLab set; preserve any intervening external edits.
        restorable = {key: int(original[key]) for key, value in changed.items()
                      if current[key] == value and int(original[key]) != value}
        if restorable:
            self._update_character_fields(original, restorable,
                                          {key: int(expected[key]) for key in restorable})
        if any(self.character(str(original["name"]))[key] != int(original[key])
               for key in restorable):
            raise RuntimeError("character attribute restoration did not verify")
        return self.character(str(original["name"]))

    def restore_character_snapshot(self, original: dict[str, object]) -> dict[str, object]:
        self.wait_offline(str(original["name"]))
        current = self.character(str(original["name"]))
        if current["online"] != 0:
            raise RuntimeError("character became online; refusing offline character-state restoration")
        if (current["char_id"] != original["char_id"]
                or current["account_id"] != original["account_id"]):
            raise RuntimeError("character/account identity changed; refusing state restoration")
        restore = {field: int(original[field]) for field in self.CHARACTER_FIELDS
                   if current[field] != original[field]}
        if restore:
            self._update_character_fields(original, restore,
                                          {field: int(current[field]) for field in restore})
        restored = self.character(str(original["name"]))
        if restored["online"] != 0 or restored["char_id"] != original["char_id"] \
                or restored["account_id"] != original["account_id"] \
                or any(restored[field] != int(original[field]) for field in self.CHARACTER_FIELDS):
            raise RuntimeError("character snapshot restoration did not verify")
        return restored
