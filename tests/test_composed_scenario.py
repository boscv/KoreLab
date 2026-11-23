from copy import deepcopy

import pytest

from korelab.scenario import validate_scenario_structure
from korelab.runner import _verify_persisted_inventory_deltas
from korelab.adapters.openkore import OpenKoreAdapter


def scenario_fixture() -> dict:
    """Build a small synthetic scenario without a bundled lab instance."""
    return {
        "name": "synthetic_equipment_exchange", "identity": {},
        "initial_state": {"map": "test_map", "position": {"x": 4, "y": 7}},
        "expected": {}, "cleanup": {}, "maximum_duration_seconds": 30,
        "openkore": {"ai_mode": "manual"},
        "actions": [
            {"id": "exchange", "type": "npc_exchange",
             "npc": {"name": "KoreLabM3TEST01"}, "sequence": "x c"},
            {"id": "equip", "type": "equip_item", "item": "Test_Shield",
             "location": "Left_Hand"},
            {"id": "unload", "type": "unload_npc"},
            {"id": "verify", "type": "verify_equipment", "expected": {
                "inventory_delta": {"Test_Material": -1, "Test_Reward": 1},
                "equipment": [{"item": "Test_Shield", "equipped": True,
                               "location": "Left_Hand"}]}}],
    }


def test_composed_equipment_and_npc_exchange_scenario_is_declarative_and_valid() -> None:
    scenario = scenario_fixture()
    _, steps = validate_scenario_structure(scenario)
    assert [step["type"] for step in steps] == [
        "npc_exchange", "equip_item", "unload_npc", "verify_equipment"]
    assert steps[0]["npc"]["name"] == "KoreLabM3TEST01"
    assert scenario["openkore"]["ai_mode"] == "manual"
    assert steps[0]["sequence"] == "x c"
    assert steps[-1]["expected"]["inventory_delta"] == {"Test_Material": -1, "Test_Reward": 1}
    assert steps[-1]["expected"]["equipment"][0] == {
        "item": "Test_Shield", "equipped": True, "location": "Left_Hand"}


def test_equipment_verification_rejects_invalid_persisted_location() -> None:
    scenario = scenario_fixture()
    invalid = deepcopy(scenario)
    invalid["actions"][-1]["expected"]["location"] = {"map": "iz_int01", "x": 20, "y": 26}
    invalid["actions"][-1]["expected"]["location"]["x"] = 1000
    with pytest.raises(ValueError, match="verify_equipment expected.location"):
        validate_scenario_structure(invalid)


def test_equipment_verification_rejects_non_integer_inventory_delta() -> None:
    scenario = scenario_fixture()
    invalid = deepcopy(scenario)
    invalid["actions"][-1]["expected"]["inventory_delta"]["Test_Material"] = "-1"
    with pytest.raises(ValueError, match="verify_equipment expected.inventory_delta"):
        validate_scenario_structure(invalid)


def test_equipment_postcondition_checks_independent_deltas_after_prepared_items() -> None:
    catalog = {"Test_Material": {"id": 909, "aegisname": "Test_Material"},
               "Test_Reward": {"id": 501, "aegisname": "Test_Reward"}}

    def amount(rows: list[dict], item_id: int) -> int:
        return sum(int(row["amount"]) for row in rows if int(row["nameid"]) == item_id)

    observed = _verify_persisted_inventory_deltas(
        {"Test_Material": -1, "Test_Reward": 1}, catalog,
        [{"nameid": 909, "amount": 2}],
        [{"nameid": 909, "amount": 2}, {"nameid": 501, "amount": 1}],
        [{"item": "Test_Material", "amount": 1}], amount)
    assert observed == {"Test_Material": -1, "Test_Reward": 1}

    with pytest.raises(RuntimeError, match="Test_Material was 0, expected -1"):
        _verify_persisted_inventory_deltas(
            {"Test_Material": -1}, catalog, [{"nameid": 909, "amount": 2}],
            [{"nameid": 909, "amount": 3}], [{"item": "Test_Material", "amount": 1}], amount)


def test_preloaded_native_npc_exchange_does_not_reload_or_relog() -> None:
    adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
    npc = {"name": "KoreLabM3TEST01", "name_id": 12345,
           "position": {"x": 19, "y": 26}}
    state = {"connected_in_game": True, "nearby_npcs": [npc],
             "inventory": [{"item_id": 909, "amount": 1}]}
    calls: list[str] = []
    observed_sequences: list[str] = []
    adapter.state = lambda *_args, **_kwargs: state
    adapter.command = calls.append
    adapter._talknpc_by_observed_id = lambda _name, sequence, _timeout: observed_sequences.append(sequence) or npc
    adapter._wait = lambda *_args, **_kwargs: object()
    adapter.relog = lambda *_args, **_kwargs: pytest.fail("preloaded NPC must not force relog")

    result = adapter.interact_with_npc("KoreLabM3TEST01", "npc/custom/korelab_m3_test01.txt",
        909, 501, "Test_Material", "Test_Reward", timeout=1)

    assert result["npc_preloaded_before_session"] is True
    assert result["sequence"] == "x c"
    assert observed_sequences == ["x c"]
    assert not any("@loadnpc" in command for command in calls)


def test_preloaded_npc_waits_for_fresh_visibility_before_fallback() -> None:
    adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
    npc = {"name": "KoreLabM3TEST01", "name_id": 12345,
           "position": {"x": 19, "y": 26}}
    empty = {"connected_in_game": True, "nearby_npcs": [],
             "inventory": [{"item_id": 909, "amount": 1}]}
    visible = {**empty, "nearby_npcs": [npc]}
    calls: list[str] = []
    adapter.state = lambda *_args, **_kwargs: empty
    adapter._observer_rows = lambda: []
    adapter.state_after = lambda *_args, **_kwargs: visible
    adapter.command = calls.append
    adapter._talknpc_by_observed_id = lambda *args, **_kwargs: calls.append("talknpc") or npc
    adapter._wait = lambda *_args, **_kwargs: object()
    adapter.relog = lambda *_args, **_kwargs: pytest.fail("visible preloaded NPC must not force relog")

    result = adapter.interact_with_npc("KoreLabM3TEST01", "npc/custom/korelab_m3_test01.txt",
        909, 501, "Test_Material", "Test_Reward", timeout=1)

    assert result["npc_preloaded_before_session"] is True
    assert "talknpc" in calls
    assert not any("@loadnpc" in command for command in calls)


def test_scenario_rejects_invalid_declared_npc_fixture_name() -> None:
    scenario = scenario_fixture()
    invalid = deepcopy(scenario)
    invalid["actions"][0]["npc"]["name"] = "not-a-managed-npc"
    with pytest.raises(ValueError, match="npc_exchange npc.name"):
        validate_scenario_structure(invalid)


def test_npc_exchange_accepts_native_steps_and_rejects_console_injection() -> None:
    scenario = scenario_fixture()
    valid, invalid = deepcopy(scenario), deepcopy(scenario)
    valid["actions"][0]["sequence"] = "x c"
    validate_scenario_structure(valid)
    invalid["actions"][0]["sequence"] = "x; quit"
    with pytest.raises(ValueError, match="npc_exchange sequence"):
        validate_scenario_structure(invalid)


def test_queued_npc_task_rejects_ai_off_with_actionable_configuration_error() -> None:
    scenario = scenario_fixture()
    scenario["openkore"]["ai_mode"] = "off"
    with pytest.raises(ValueError, match="native NPC interaction.*AI is off.*ai_mode.*manual.*on"):
        validate_scenario_structure(scenario)


def test_ai_off_remains_valid_for_scenarios_without_queued_npc_tasks() -> None:
    scenario = scenario_fixture()
    scenario["openkore"]["ai_mode"] = "off"
    scenario["actions"] = [scenario["actions"][1], scenario["actions"][3]]
    scenario["actions"][0]["preconditions"] = []
    scenario["actions"][1]["preconditions"] = []
    validate_scenario_structure(scenario)


def test_native_npc_exchange_rejects_unsafe_sequence_before_interaction() -> None:
    adapter = OpenKoreAdapter.__new__(OpenKoreAdapter)
    with pytest.raises(ValueError, match="NPC conversation sequence"):
        adapter.interact_with_npc("KoreLabM3TEST01", "npc/custom/korelab_m3_test01.txt",
            909, 501, "Test_Material", "Test_Reward", sequence="x; quit")
