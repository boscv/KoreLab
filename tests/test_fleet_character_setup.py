import json
import subprocess

import pytest

from korelab.fleet_profiles import fleet_archetype
from korelab.laboratory import ManagedLaboratory
from korelab.platform_support import set_private_permissions


def test_character_setup_reuses_and_equips_native_starter_weapon(tmp_path, monkeypatch):
    root = tmp_path / "labs"
    home = root / "combat-fleet-test"
    home.mkdir(parents=True)
    rathena = tmp_path / "rathena"
    (rathena / "src/config").mkdir(parents=True)
    (rathena / "src/config/renewal.hpp").write_text("#define RENEWAL_STAT\n")
    agent_id = "agent-006"
    character = "KLABCDEF"
    owner = {"format": ManagedLaboratory.VERSION, "id": home.name, "home": str(home),
        "rathena_runtime_source": str(rathena),
        "agents": [{"id": agent_id, "char_id": 55,
                    "profile_file": "openkore-agents/agent-006/profile.json"}]}
    (home / "owner.json").write_text(json.dumps(owner))
    secrets_file = home / "secrets.json"
    secrets_file.write_text(json.dumps({"fleet_agents": {agent_id: {
        "account": "synthetic_account", "password": "private", "login_pin": "1234"}}}))
    set_private_permissions(secrets_file, 0o600)
    profile_dir = home / "openkore-agents" / agent_id
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.json").write_text(json.dumps({"character": character}))
    manager = ManagedLaboratory(root)
    calls = []

    def query(_home, sql):
        calls.append(sql)
        if sql.startswith("SELECT c.char_id"):
            return subprocess.CompletedProcess([], 0, "55\t77\t0\t0\t1\t1\t0\t48\t1\t1\t1\t1\t1\t1\n", "")
        if sql.startswith("UPDATE `char` SET `class`"):
            return subprocess.CompletedProcess([], 0, "1\n", "")
        if sql.startswith("INSERT INTO skill"):
            return subprocess.CompletedProcess([], 0, "1\n", "")
        if sql.startswith("SELECT i.id,i.equip"):
            return subprocess.CompletedProcess([], 0, "1234\t2\n", "")
        if sql.startswith("UPDATE inventory i JOIN `char` c ON c.char_id=i.char_id SET i.equip=0"):
            return subprocess.CompletedProcess([], 0, "1\n", "")
        if sql.startswith("SELECT i.id,i.amount,i.equip"):
            return subprocess.CompletedProcess([], 0, "900\t1\t0\n", "")
        if sql.startswith("UPDATE inventory i JOIN"):
            return subprocess.CompletedProcess([], 0, "1\n", "")
        if sql.startswith("SELECT i.amount,i.equip"):
            return subprocess.CompletedProcess([], 0, "1\t2\n", "")
        if sql.startswith("SELECT c.`class`"):
            return subprocess.CompletedProcess([], 0,
                "6\t10\t1\t0\t0\t48\t1\t1\t1\t1\t1\t1\t1\t3\t1\n", "")
        if sql.startswith("SELECT s.lv,s.flag"):
            assert "s.id=1" in sql and "c.account_id=77" in sql and "c.online=0" in sql
            return subprocess.CompletedProcess([], 0, "7\t3\n", "")
        raise AssertionError("unexpected managed database operation")

    monkeypatch.setattr(ManagedLaboratory, "_query_owned_db", staticmethod(query))
    build = dict(fleet_archetype(5), additional_skills=[
        {"skill": "NV_BASIC", "skill_id": 1, "skill_level": 7}])
    result = manager.prepare_fleet_character(home.name, agent_id=agent_id,
                                             archetype=build)
    assert result["server_verified"] is True
    assert result["weapon_id"] == 1201
    assert result["attributes"] == {"str": 1, "agi": 1, "vit": 1,
                                     "int": 1, "dex": 1, "luk": 1}
    assert result["status_points_remaining"] == 48
    update = next(sql for sql in calls if sql.startswith("UPDATE inventory i JOIN"))
    assert "i.char_id=55" in update and "c.account_id=77" in update and "c.online=0" in update
    unequip = next(sql for sql in calls if "SET i.equip=0" in sql)
    assert "i.equip=2" in unequip and "c.`class`=6" in unequip
    skill = next(sql for sql in calls if sql.startswith("INSERT INTO skill"))
    assert "SELECT c.char_id,52,1,3" in skill
    basic = next(sql for sql in calls if sql.startswith("INSERT INTO skill") and "c.char_id,1,7,3" in sql)
    assert "c.account_id=77" in basic and "c.online=0" in basic and "c.`class`=6" in basic
    assert result["additional_skills"] == [{"skill": "NV_BASIC", "skill_id": 1,
                                             "skill_level": 7}]


def test_character_setup_applies_stats_only_from_the_verified_offline_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "labs"
    home = root / "fleet-stat-test"
    home.mkdir(parents=True)
    rathena = tmp_path / "rathena"
    (rathena / "src/config").mkdir(parents=True)
    (rathena / "src/config/renewal.hpp").write_text("#define RENEWAL_STAT\n")
    agent_id = "agent-001"
    owner = {"format": ManagedLaboratory.VERSION, "id": home.name, "home": str(home),
        "rathena_runtime_source": str(rathena),
        "agents": [{"id": agent_id, "char_id": 71,
                    "profile_file": "openkore-agents/agent-001/profile.json"}]}
    (home / "owner.json").write_text(json.dumps(owner))
    secret_file = home / "secrets.json"
    secret_file.write_text(json.dumps({"fleet_agents": {agent_id: {
        "account": "synthetic_account", "password": "private", "login_pin": "1234"}}}))
    set_private_permissions(secret_file, 0o600)
    set_private_permissions(secret_file, 0o600)
    profile_dir = home / "openkore-agents" / agent_id
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.json").write_text(json.dumps({"character": "KLABABCD"}))
    manager = ManagedLaboratory(root)
    calls = []

    def query(_home, sql):
        calls.append(sql)
        if sql.startswith("SELECT c.char_id"):
            return subprocess.CompletedProcess([], 0,
                "71\t91\t0\t0\t1\t1\t0\t48\t1\t1\t1\t1\t1\t1\n", "")
        if sql.startswith("UPDATE `char` SET `class`"):
            return subprocess.CompletedProcess([], 0, "1\n", "")
        if sql.startswith("INSERT INTO skill"):
            return subprocess.CompletedProcess([], 0, "1\n", "")
        if sql.startswith("SELECT i.id,i.equip"):
            return subprocess.CompletedProcess([], 0, "1234\t2\n", "")
        if sql.startswith("UPDATE inventory i JOIN `char` c ON c.char_id=i.char_id SET i.equip=0"):
            return subprocess.CompletedProcess([], 0, "1\n", "")
        if sql.startswith("SELECT i.id,i.amount,i.equip"):
            return subprocess.CompletedProcess([], 0, "900\t1\t0\n", "")
        if sql.startswith("UPDATE inventory i JOIN"):
            return subprocess.CompletedProcess([], 0, "1\n", "")
        if sql.startswith("SELECT i.amount,i.equip"):
            return subprocess.CompletedProcess([], 0, "1\t2\n", "")
        if sql.startswith("SELECT c.`class`"):
            return subprocess.CompletedProcess([], 0,
                "6\t10\t1\t0\t0\t0\t9\t15\t1\t1\t1\t1\t1\t3\t1\n", "")
        raise AssertionError("unexpected managed database operation")

    monkeypatch.setattr(ManagedLaboratory, "_query_owned_db", staticmethod(query))
    excessive = dict(fleet_archetype(5), attributes={"str": 99, "agi": 1, "vit": 1,
        "int": 1, "dex": 1, "luk": 1})
    with pytest.raises(ValueError, match="available status points"):
        manager.prepare_fleet_character(home.name, agent_id=agent_id, archetype=excessive)
    assert not any(sql.startswith("UPDATE `char` SET `class`") for sql in calls)
    build = dict(fleet_archetype(5), attributes={"str": 9, "agi": 15, "vit": 1,
        "int": 1, "dex": 1, "luk": 1})
    result = manager.prepare_fleet_character(home.name, agent_id=agent_id, archetype=build)
    update = next(sql for sql in calls if sql.startswith("UPDATE `char` SET `class`"))
    assert "status_point=0" in update and "str=9" in update and "agi=15" in update
    assert "AND online=0 AND `class`=0" in update
    assert "AND str=1 AND agi=1" in update
    assert result["attributes"] == build["attributes"]
    assert result["status_points_remaining"] == 0
