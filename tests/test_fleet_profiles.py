import json

import pytest

from korelab.fleet_profiles import (ARCHETYPES, expand_fleet_roster, fleet_archetype,
                                    load_fleet_roster, skill_config_block,
                                    status_point_cost, validate_fleet_archetypes)
from korelab.fleet import _native_fleet_openkore_settings, _unconfirmed_skill_agents
from korelab.adapters.openkore import OBSERVER


def synthetic_roster():
    return {"id": "test-roster", "version": 1,
        "composition": [{"build": "mage", "count": 1}, {"build": "archer", "count": 1}],
        "builds": {
            "mage": {"job": "Mage", "class_id": 2, "base_level": 10,
                "job_level": 1, "attributes": {"str": 1, "agi": 1, "vit": 1,
                    "int": 1, "dex": 1, "luk": 1}, "skill": "MG_LIGHTNINGBOLT",
                "skill_id": 20, "skill_level": 1, "weapon": "Rod", "weapon_id": 1601,
                "lock_map": "test_field", "targets": ["Test_Monster"]},
            "archer": {"job": "Archer", "class_id": 3, "base_level": 10,
                "job_level": 1, "attributes": {"str": 1, "agi": 1, "vit": 1,
                    "int": 1, "dex": 1, "luk": 1}, "skill": "AC_DOUBLE",
                "skill_id": 46, "skill_level": 1, "weapon": "Bow", "weapon_id": 1701,
                "ammo_id": 1750, "ammo_amount": 5,
                "lock_map": "test_field", "targets": ["Test_Monster"]},
        }}


def test_fleet_roster_cycles_all_first_job_archetypes_and_maps():
    roster = [fleet_archetype(index) for index in range(50)]
    assert len({entry["class_id"] for entry in roster}) == 6
    assert {entry["job"] for entry in roster} == {
        "Swordman", "Mage", "Archer", "Acolyte", "Merchant", "Thief"}
    assert {entry["map"] for entry in roster} == {"prt_fild08", "prt_fild07"}
    assert sum(entry["map"] == "prt_fild08" for entry in roster) == 25
    assert sum(entry["map"] == "prt_fild07" for entry in roster) == 25
    assert roster[0] == fleet_archetype(len(ARCHETYPES))
    assert sum(entry["job"] == "Archer" for entry in roster) == 8


def test_class_profiles_generate_supported_native_skill_blocks():
    mage = skill_config_block(fleet_archetype(1))
    archer = skill_config_block(fleet_archetype(2))
    merchant = skill_config_block(fleet_archetype(4))
    assert "attackSkillSlot MG_LIGHTNINGBOLT {" in mage
    assert "maxDist 9" in mage and "sp > 0" in mage
    assert "attackSkillSlot AC_DOUBLE {" in archer
    assert "maxDist 9" in archer
    assert "attackSkillSlot MC_MAMMONITE {" in merchant
    assert "zeny > 1000" in merchant


def test_native_party_prerequisite_skill_is_source_checked_and_inherited(tmp_path):
    root = tmp_path / "rathena"
    (root / "src/common").mkdir(parents=True)
    (root / "db/re").mkdir(parents=True)
    (root / "src/common/mmo.hpp").write_text(
        "enum e_job { JOB_NOVICE, JOB_SWORDMAN, JOB_MAGE, JOB_ARCHER, "
        "JOB_ACOLYTE, JOB_MERCHANT, JOB_THIEF };")
    (root / "db/re/skill_db.yml").write_text(
        "Header:\n  Type: SKILL_DB\n  Version: 4\nBody:\n"
        "  - Id: 1\n    Name: NV_BASIC\n    MaxLevel: 9\n"
        "  - Id: 28\n    Name: AL_HEAL\n    MaxLevel: 10\n")
    (root / "db/re/skill_tree.yml").write_text(
        "Body:\n  - Job: Novice\n    Tree:\n      - Name: NV_BASIC\n        MaxLevel: 9\n"
        "  - Job: Acolyte\n    Inherit:\n      Novice: true\n    Tree:\n"
        "      - Name: AL_HEAL\n        MaxLevel: 10\n")
    (root / "db/re/item_db_equip.yml").write_text(
        "  - Id: 1501\n    Jobs:\n      Acolyte: true\n")
    (root / "db/re/item_db_etc.yml").write_text("")
    build = {"job": "Acolyte", "class_id": 4, "base_level": 10, "job_level": 1,
        "skill": "AL_HEAL", "skill_id": 28, "skill_level": 1, "weapon_id": 1501,
        "map": "prt_fild08", "targets": ["Poring"], "additional_skills": [
            {"skill": "NV_BASIC", "skill_id": 1, "skill_level": 7}]}
    validate_fleet_archetypes(root, profiles=[build])
    invalid = {**build, "additional_skills": [
        {"skill": "NV_BASIC", "skill_id": 28, "skill_level": 7}]}
    with pytest.raises(RuntimeError, match="skill ID changed"):
        validate_fleet_archetypes(root, profiles=[invalid])


def test_only_archer_archetypes_receive_ammunition():
    for index in range(50):
        profile = fleet_archetype(index)
        if profile["job"] == "Archer":
            assert profile["ammo_id"] == 1750
            assert profile["arrow_amount"] == 500
        else:
            assert "ammo_id" not in profile
            assert profile["arrow_amount"] == 0


def test_observer_records_native_outgoing_skill_use_packets():
    assert "packet_pre/sendSkillUse" in OBSERVER
    assert "type=>'skill_use_request'" in OBSERVER
    assert "skill_id=>0+$args->{ID}" in OBSERVER


def test_observer_records_server_confirmed_skill_use_independently_of_send_hook():
    assert "type=>'skill_use_confirmed'" in OBSERVER
    assert "$char->{last_skill_time}" in OBSERVER


def test_skill_request_without_server_confirmation_does_not_satisfy_acceptance():
    agents = [{"id": f"agent-{index}"} for index in range(1, 6)]
    outcomes = {agent["id"]: {"offensive_skill_use_requests": 1,
                              "offensive_skill_use_confirmations": 0}
                for agent in agents}
    assert {agent["id"] for agent in _unconfirmed_skill_agents(agents, outcomes)} == {
        agent["id"] for agent in agents}
    outcomes["agent-3"]["offensive_skill_use_confirmations"] = 1
    assert [agent["id"] for agent in _unconfirmed_skill_agents(agents, outcomes)] == [
        "agent-1", "agent-2", "agent-4", "agent-5"]


def test_caller_roster_is_editable_credential_free_and_reusable(tmp_path):
    source = tmp_path / "roster.json"
    source.write_text(json.dumps(synthetic_roster()))
    roster = load_fleet_roster(source)
    builds = expand_fleet_roster(roster, 4)
    assert roster["id"] == "test-roster"
    assert len(builds) == 4
    assert [build["job"] for build in expand_fleet_roster(roster, 2)] == ["Mage", "Archer"]
    assert all("password" not in build and "account" not in build for build in builds)


def test_caller_roster_inherits_valid_native_stat_overrides(tmp_path):
    base = tmp_path / "base.json"
    override = tmp_path / "override.json"
    base.write_text(json.dumps(synthetic_roster()))
    override.write_text(json.dumps({"id": "stat-override", "version": 1,
        "extends": "base.json", "attribute_overrides": {"mage": {"str": 1, "agi": 1,
            "vit": 1, "int": 15, "dex": 9, "luk": 1}}}))
    roster = load_fleet_roster(override)
    mage = roster["builds"]["mage"]
    assert mage["attributes"]["int"] == 15
    assert sum(status_point_cost(1, stat) for stat in mage["attributes"].values()) == 48


def test_roster_rejects_credential_fields_and_invalid_stat_budgets(tmp_path):
    raw = synthetic_roster()
    raw["builds"]["mage"]["password"] = "never-allowed"
    source = tmp_path / "private.json"
    source.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="credentials"):
        load_fleet_roster(source)
    with pytest.raises(ValueError, match="cannot be reduced"):
        status_point_cost(2, 1)
    del raw["builds"]["mage"]["password"]
    raw["builds"]["mage"]["class_id"] = 6
    source.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="mismatched first-job class"):
        load_fleet_roster(source)


def test_native_profile_uses_roster_overrides_but_keeps_per_character_lock_map():
    settings = _native_fleet_openkore_settings("prt_fild08", "bot001",
        config_overrides={"route_step": 7, "lockMap": "should_not_override"})
    assert settings["native_profile"] == "bot001"
    assert settings["config_overrides"]["route_step"] == 7
    assert settings["config_overrides"]["lockMap"] == "prt_fild08"
    assert settings["config_overrides"]["attackAuto"] == 2
