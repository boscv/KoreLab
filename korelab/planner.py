from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .capabilities import capability_for
from .items import (assess_equipment, class_job_name, equipment_location_masks,
                    job_candidates, load_item_catalog, resolve_item)
from .monsters import assess_match, load_monster_catalog
from .openkore_runtime import discover_control_files, validate_runtime_profile
from .quests import load_quest_catalog
from .scenario import load_scenario, validate_scenario_structure
from .platform_support import set_private_permissions


class PlanningError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _require(condition: bool, code: str, message: str, details: dict | None = None) -> None:
    if not condition:
        raise PlanningError(code, message, details)


def _reject_unknown_fields(value: dict, allowed: set[str], scope: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlanningError("unsupported_action", f"unsupported {scope} fields: {unknown}")


def _resolve_item(catalog: dict, selector: str) -> dict:
    try:
        return resolve_item(catalog, selector)
    except ValueError as exc:
        raise PlanningError("unavailable_item", str(exc)) from exc


def _positive_int(value: Any, name: str) -> int:
    _require(type(value) is int and value > 0, "invalid_objective", f"{name} must be a positive integer")
    return value


def _input_provenance(objective: dict[str, Any]) -> list[dict[str, str]]:
    declarations = objective.get("input_provenance", [])
    _require(isinstance(declarations, list) and len(declarations) <= 100, "ambiguous_objective",
             "input_provenance must be a list of at most 100 field references")
    records = []
    seen_paths = set()
    allowed_provenance = {"user_provided", "openkore_observed", "local_server_data", "agent_inferred"}
    for declaration in declarations:
        _require(isinstance(declaration, dict), "ambiguous_objective",
                 "input_provenance entries must be objects")
        _reject_unknown_fields(declaration, {"path", "source_id", "provenance"}, "input_provenance entry")
        path = declaration.get("path")
        source_id = declaration.get("source_id")
        provenance = declaration.get("provenance")
        _require(isinstance(path, str) and re.fullmatch(r"(?:start|requirements|preparation|goal)(?:\.[A-Za-z_][A-Za-z0-9_]*)+", path),
                 "ambiguous_objective", "input_provenance.path must point into start, requirements, preparation, or goal")
        _require(path not in seen_paths, "ambiguous_objective", f"input provenance is duplicated for {path}")
        current: Any = objective
        for part in path.split("."):
            current = current.get(part) if isinstance(current, dict) else None
        _require(current is not None, "ambiguous_objective", f"input_provenance path does not exist: {path}")
        _require(isinstance(source_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", source_id),
                 "ambiguous_objective", "input_provenance.source_id must be a short opaque reference")
        _require(isinstance(provenance, str) and provenance in allowed_provenance, "ambiguous_objective",
                 f"input_provenance.provenance must be one of {sorted(allowed_provenance)}")
        records.append({"path": path, "source_id": source_id, "provenance": provenance})
        seen_paths.add(path)
    return records


def _attach_input_provenance(plan: dict[str, Any], declarations: list[dict[str, str]]) -> dict[str, Any]:
    if declarations:
        plan["input_provenance"] = declarations
        plan["scenario"].setdefault("planning", {})["input_provenance"] = declarations
        plan["scenario"]["planning"]["fixture_scope"] = (
            "The temporary local NPC/quest reproduces only the declared objective fields. "
            "It does not establish that a remote server has identical hidden rules or behavior.")
    return plan


def _position(value: Any, name: str) -> dict[str, int]:
    _require(isinstance(value, dict) and all(type(value.get(axis)) is int and value[axis] >= 0
                                               for axis in ("x", "y")),
             "ambiguous_objective", f"{name} must declare non-negative integer x/y coordinates")
    return {"x": value["x"], "y": value["y"]}


def _maps(checkout: Path) -> set[str]:
    path = checkout / "db/map_index.txt"
    if not path.is_file():
        raise PlanningError("unavailable_map_catalog", "active rAthena db/map_index.txt is unavailable")
    result = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        body = line.split("//", 1)[0].strip()
        if body:
            result.add(body.split()[0])
    return result


def _resolve_exact_monster(catalog: dict, selector: Any) -> dict:
    _require(isinstance(selector, (str, int)) and str(selector).strip(), "ambiguous_objective",
             "target.monster must be one exact active rAthena mob ID, AegisName, or name")
    needle = str(selector).strip().casefold()
    matches = [mob for mob in catalog["monsters"] if needle in {
        str(mob.get("id", "")).casefold(), str(mob.get("aegis_name", "")).casefold(),
        str(mob.get("name", "")).casefold()}]
    _require(bool(matches), "unavailable_monster",
             f"monster {selector!r} is not present in the active rAthena mob database")
    _require(len(matches) == 1, "ambiguous_objective",
             f"monster selector {selector!r} matches {len(matches)} active records; use an exact ID or AegisName")
    return matches[0]


def _resolve_choice(raw: Any, label: str, catalog: dict) -> dict:
    _require(isinstance(raw, dict), "ambiguous_objective", f"goal.{label} must declare an NPC choice")
    _reject_unknown_fields(raw, {"id", "label", "message", "requires", "consume", "grant",
                                 "quest", "quest_requires"}, f"goal.{label}")
    choice_id = raw.get("id")
    _require(isinstance(choice_id, str) and re.fullmatch(r"[a-z][a-z0-9_]*", choice_id),
             "ambiguous_objective", f"goal.{label}.id must be a lowercase identifier")
    _require(isinstance(raw.get("label"), str) and raw["label"].strip(),
             "ambiguous_objective", f"goal.{label}.label is required")
    _require(isinstance(raw.get("message"), str) and raw["message"].strip(),
             "ambiguous_objective", f"goal.{label}.message is required")
    requirements = raw.get("requires")
    _require(isinstance(requirements, list) and bool(requirements),
             "ambiguous_objective", f"goal.{label}.requires must be a non-empty item list")
    normalized_requirements = []
    requirement_amounts: dict[str, int] = defaultdict(int)
    for requirement in requirements:
        _require(isinstance(requirement, dict) and isinstance(requirement.get("item"), str),
                 "ambiguous_objective", f"goal.{label}.requires entries need an item name")
        _reject_unknown_fields(requirement, {"item", "amount"}, f"goal.{label}.requires entry")
        item = _resolve_item(catalog, requirement["item"])
        amount = requirement.get("amount", 1)
        _positive_int(amount, f"goal.{label}.requires amount")
        requirement_amounts[item["aegisname"]] += amount
    transaction = {}
    for field in ("consume", "grant"):
        raw_item = raw.get(field)
        _require(isinstance(raw_item, dict) and isinstance(raw_item.get("item"), str),
                 "ambiguous_objective", f"goal.{label}.{field} must declare item and amount")
        _reject_unknown_fields(raw_item, {"item", "amount"}, f"goal.{label}.{field}")
        item = _resolve_item(catalog, raw_item["item"])
        amount = _positive_int(raw_item.get("amount", 1), f"goal.{label}.{field}.amount")
        transaction[field] = {"item": item["aegisname"], "amount": amount}
        if field == "consume":
            _require(requirement_amounts[item["aegisname"]] >= amount, "ambiguous_objective",
                     f"goal.{label}.requires must cover the full consumed amount of {item['aegisname']}")
        _require(item.get("type") in ("Etc", "Healing", "Usable"), "unsupported_action",
                 f"NPC transaction item {item['aegisname']} is not a supported stackable inventory item")
    normalized_requirements = [{"item": item, "amount": amount}
                               for item, amount in sorted(requirement_amounts.items())]
    result = {"id": choice_id, "label": raw["label"].strip(), "message": raw["message"].strip(),
             "requires": normalized_requirements, **transaction}
    quest_action = raw.get("quest")
    if quest_action is not None:
        _require(isinstance(quest_action, dict), "ambiguous_objective",
                 f"goal.{label}.quest must declare id and action")
        _reject_unknown_fields(quest_action, {"id", "action"}, f"goal.{label}.quest")
        quest_id = _positive_int(quest_action.get("id"), f"goal.{label}.quest.id")
        _require(quest_action.get("action") in ("start", "complete"), "unsupported_action",
                 f"goal.{label}.quest.action must be start or complete")
        result["quest"] = {"id": quest_id, "action": quest_action["action"]}
    raw_quest_requirements = raw.get("quest_requires", [])
    _require(isinstance(raw_quest_requirements, list), "ambiguous_objective",
             f"goal.{label}.quest_requires must be a list")
    quest_requirements = []
    for index, requirement in enumerate(raw_quest_requirements):
        _require(isinstance(requirement, dict), "ambiguous_objective",
                 f"goal.{label}.quest_requires[{index}] must be an object")
        _reject_unknown_fields(requirement, {"id", "state"}, f"goal.{label}.quest_requires[{index}]")
        quest_id = _positive_int(requirement.get("id"), f"goal.{label}.quest_requires[{index}].id")
        state = requirement.get("state")
        _require(state in ("active", "hunt_complete"), "unsupported_action",
                 f"goal.{label}.quest_requires state must be active or hunt_complete")
        quest_requirements.append({"id": quest_id, "state": state})
    result["quest_requires"] = quest_requirements
    return result


def _native_hunt_eventmacro(quest_id: int, monster_id: int,
                            collect_item: dict | None, collect_amount: int = 1) -> str:
    conditions = [f"QuestHuntCompleted {quest_id} {monster_id}"]
    if collect_item:
        item_name = str(collect_item["aegisname"])
        if not re.fullmatch(r"[A-Za-z0-9_]+", item_name):
            raise PlanningError("unsupported_action", "native AI loot item has an unsafe eventMacro selector")
        conditions.append(f'InInventory "{item_name}" >= {collect_amount}')
    condition_lines = "\n".join(f"\t{condition}" for condition in conditions)
    return ("automacro korelab_native_hunt_ready {\n\trun-once 1\n"
            f"{condition_lines}\n\tcall korelab_native_hunt_complete\n}}\n\n"
            "macro korelab_native_hunt_complete {\n"
            f"\tlog KORELAB_NATIVE_HUNT_{quest_id}_READY\n}}\n\n"
            "macro korelab_resume_ai {\n\tdo ai on\n\tlog KORELAB_AI_HANDOFF_COMPLETE\n}\n")


def _plan_npc_exchange(objective_id: str, objective: dict[str, Any], goal: dict[str, Any],
                       identity: dict[str, str], actual: dict[str, Any], start: dict[str, Any],
                       start_position: dict[str, int],
                       checkout: Path, adapter: Any, available_maps: set[str],
                       group_id: int, duration: int) -> dict[str, Any]:
    """Compose a single declared item exchange using the existing M3 sequence runner."""
    _reject_unknown_fields(goal, {"type", "npc", "exchange"}, "goal")
    _require(not objective.get("requirements"), "unsupported_action",
             "complete_npc_exchange does not currently apply character or equipment requirements")
    npc = goal.get("npc")
    _require(isinstance(npc, dict) and isinstance(npc.get("map"), str), "ambiguous_objective",
             "goal.npc must declare map and position")
    _reject_unknown_fields(npc, {"map", "position"}, "goal.npc")
    npc_position = _position(npc.get("position"), "goal.npc.position")
    _require(npc_position["x"] <= 500 and npc_position["y"] <= 500, "invalid_objective",
             "goal.npc.position coordinates must be from 0 to 500")
    missing_maps = sorted({start["map"], npc["map"]} - available_maps)
    _require(not missing_maps, "unavailable_map", f"map(s) absent from active rAthena map_index: {missing_maps}",
             {"maps": missing_maps})
    _require(npc["map"] == start["map"], "unsupported_action",
             "complete_npc_exchange currently supports an NPC on the character's starting map")

    exchange = goal.get("exchange")
    _require(isinstance(exchange, dict), "ambiguous_objective", "goal.exchange must declare material and reward")
    _reject_unknown_fields(exchange, {"material", "reward"}, "goal.exchange")
    item_catalog = load_item_catalog(checkout)
    resolved = {}
    for role in ("material", "reward"):
        declaration = exchange.get(role)
        _require(isinstance(declaration, dict) and isinstance(declaration.get("item"), str),
                 "ambiguous_objective", f"goal.exchange.{role} must declare an item and amount")
        _reject_unknown_fields(declaration, {"item", "amount"}, f"goal.exchange.{role}")
        item = _resolve_item(item_catalog, declaration["item"])
        amount = _positive_int(declaration.get("amount", 1), f"goal.exchange.{role}.amount")
        _require(amount <= 100, "invalid_objective", f"goal.exchange.{role}.amount must not exceed 100")
        _require(item.get("type") in ("Etc", "Healing", "Usable"), "unsupported_action",
                 f"NPC exchange {role} {item['aegisname']} is not a supported stackable inventory item")
        resolved[role] = {"item": item, "amount": amount}
    material, reward = resolved["material"], resolved["reward"]
    _require(material["amount"] == reward["amount"], "unsupported_action",
             "the existing npc_exchange action supports equal material and reward quantities")
    _require(int(material["item"]["id"]) != int(reward["item"]["id"]), "ambiguous_objective",
             "NPC exchange material and reward must be different items")

    preparation = objective.get("preparation", {})
    _require(isinstance(preparation, dict), "ambiguous_objective", "preparation must be an object")
    _reject_unknown_fields(preparation, {"inventory"}, "preparation")
    raw_inventory = preparation.get("inventory", [])
    _require(isinstance(raw_inventory, list), "ambiguous_objective", "preparation.inventory must be a list")
    fixtures = []
    fixture_amounts: dict[int, int] = defaultdict(int)
    for row in raw_inventory:
        _require(isinstance(row, dict) and isinstance(row.get("item"), str), "ambiguous_objective",
                 "preparation.inventory entries require an item name")
        _reject_unknown_fields(row, {"item", "amount"}, "preparation.inventory entry")
        item = _resolve_item(item_catalog, row["item"])
        amount = _positive_int(row.get("amount"), "preparation.inventory.amount")
        fixtures.append({"item": item["aegisname"], "amount": amount})
        fixture_amounts[int(item["id"])] += amount

    material_item = material["item"]
    material_id = int(material_item["id"])
    current_rows = adapter.inventory_rows(actual, (material_id,))
    current_amount = adapter.inventory_amount(current_rows, material_id)
    available_amount = current_amount + fixture_amounts[material_id]
    _require(available_amount >= material["amount"], "missing_prerequisite",
             f"NPC exchange requires {material['amount']} {material_item['aegisname']}; current inventory plus declared preparation provides {available_amount}",
             {"item": material_item["aegisname"], "required": material["amount"],
              "current": current_amount, "prepared": fixture_amounts[material_id]})

    runtime_commands = ("loadnpc", "unloadnpc")
    _require(adapter.group_allows(group_id, runtime_commands), "missing_prerequisite",
             f"temporary rAthena group {group_id} must allow {', '.join(runtime_commands)}",
             {"group_id": group_id, "commands": list(runtime_commands)})

    material_name, reward_name = str(material_item["aegisname"]), str(reward["item"]["aegisname"])
    amount = int(material["amount"])
    reward_amount = int(reward["amount"])
    action_steps = [
        {"id": "exchange_declared_items", "type": "npc_exchange", "material": material_name,
         "reward": reward_name, "amount": amount,
         "npc": {"map": npc["map"], "position": npc_position},
         "expected": {"material_delta": -amount, "reward_delta": reward_amount},
         "preconditions": [{"inventory": {"item": material_name, "at_least": amount, "source": "openkore"}}]},
        {"id": "remove_temporary_exchange_npc", "type": "unload_npc",
         "preconditions": [{"step": "exchange_declared_items", "path": "status", "equals": "PASS"}]},
        {"id": "verify_exchange_inventory", "type": "verify_inventory",
         "preconditions": [{"step": "remove_temporary_exchange_npc",
             "path": "npc_removed_from_client_view", "equals": True}],
         "expected": {"inventory_delta": {material_name: -amount, reward_name: reward_amount}}},
    ]
    rationale = [
        {"action": "exchange_declared_items", "capability": "npc_exchange",
         "reason": f"use the existing OpenKore NPC transaction to exchange {amount} {material_name} for {reward_amount} {reward_name}"},
        {"action": "remove_temporary_exchange_npc", "capability": "unload_npc",
         "reason": "remove only the temporary NPC created by this generated scenario"},
        {"action": "verify_exchange_inventory", "capability": "verify_inventory",
         "reason": "compare persisted rAthena inventory deltas after OpenKore logs out"},
    ]
    scenario = {"name": objective_id, "identity": identity,
        "initial_state": {"map": start["map"], "position": start_position},
        "server_setup": {"temporary_group_id": group_id},
        "prepare": {"inventory": {"items": fixtures}}, "actions": action_steps,
        "expected": {"objective_completed": objective_id},
        "openkore": {"config_overrides": {"attackAuto": "0", "itemsTakeAuto": "0",
            "itemsGatherAuto": "0", "route_randomWalk": "0", f"field_{start['map']}": start["map"]}},
        "maximum_duration_seconds": duration,
        "cleanup": {"restore_character_state": True, "restore_inventory": True,
            "restore_equipment": True, "restore_account_group": True,
            "remove_temporary_npc": True, "require_character_offline": True}}
    semantic_checks = [
        {"check": "character", "status": "PASS", "class_id": int(actual["class"]),
         "base_level": int(actual["base_level"]),
         "map": start["map"], "position": start_position, "source": "rAthena character row"},
        {"check": "items", "status": "PASS", "material": {"id": material_id, "aegisname": material_name,
             "type": material_item["type"], "available": available_amount, "required": amount},
         "reward": {"id": int(reward["item"]["id"]), "aegisname": reward_name,
             "type": reward["item"]["type"], "amount": reward_amount},
         "source": sorted({str(material_item["source"]), str(reward["item"]["source"])})},
        {"check": "map", "status": "PASS", "map": start["map"],
         "source": "active rAthena db/map_index.txt"},
        {"check": "temporary_group", "status": "PASS", "group_id": group_id,
         "commands": list(runtime_commands)},
    ]
    scenario["planning"] = {"planner": "deterministic-v1", "objective_id": objective_id,
        "operator": "complete_npc_exchange", "selected_capabilities": [step["type"] for step in action_steps],
        "dependencies": rationale, "semantic_checks": semantic_checks,
        "limitations": ["one material-to-reward NPC exchange on the starting map",
            "rAthena item data validates item type; server script behavior remains authoritative"]}
    try:
        validate_scenario_structure(scenario)
    except ValueError as exc:
        raise PlanningError("planner_output_invalid", f"planner generated an invalid runner scenario: {exc}") from exc
    return {"status": "READY", "objective_id": objective_id,
        "selected_capabilities": [step["type"] for step in action_steps],
        "action_plan": rationale, "semantic_checks": semantic_checks, "scenario": scenario}


def _plan_npc_dialogue_branch(objective_id: str, objective: dict[str, Any], goal: dict[str, Any],
                              identity: dict[str, str], actual: dict[str, Any], start: dict[str, Any],
                              start_position: dict[str, int], checkout: Path, adapter: Any,
                              available_maps: set[str], group_id: int, duration: int) -> dict[str, Any]:
    """Choose a declared item-gated NPC branch and compile it through the shared M3 runner."""
    _reject_unknown_fields(goal, {"type", "npc", "choices", "desired_reward", "quest_fixture"}, "goal")
    _require(not objective.get("requirements"), "unsupported_action",
             "complete_npc_dialogue_branch does not currently apply character or equipment requirements")
    npc = goal.get("npc")
    _require(isinstance(npc, dict) and isinstance(npc.get("map"), str), "ambiguous_objective",
             "goal.npc must declare map and position")
    _reject_unknown_fields(npc, {"map", "position"}, "goal.npc")
    npc_position = _position(npc.get("position"), "goal.npc.position")
    _require(npc_position["x"] <= 500 and npc_position["y"] <= 500, "invalid_objective",
             "goal.npc.position coordinates must be from 0 to 500")
    missing_maps = sorted({start["map"], npc["map"]} - available_maps)
    _require(not missing_maps, "unavailable_map", f"map(s) absent from active rAthena map_index: {missing_maps}",
             {"maps": missing_maps})
    _require(npc["map"] == start["map"], "unsupported_action",
             "complete_npc_dialogue_branch currently supports an NPC on the character's starting map")

    item_catalog = load_item_catalog(checkout)
    desired = goal.get("desired_reward")
    _require(isinstance(desired, dict) and isinstance(desired.get("item"), str), "ambiguous_objective",
             "goal.desired_reward must declare item and amount")
    _reject_unknown_fields(desired, {"item", "amount"}, "goal.desired_reward")
    desired_item = _resolve_item(item_catalog, desired["item"])
    desired_amount = _positive_int(desired.get("amount", 1), "goal.desired_reward.amount")
    _require(desired_amount <= 100, "invalid_objective", "goal.desired_reward.amount must not exceed 100")
    _require(desired_item.get("type") in ("Etc", "Healing", "Usable"), "unsupported_action",
             "desired NPC outcome must be a supported stackable inventory item")

    raw_choices = goal.get("choices")
    _require(isinstance(raw_choices, list) and 2 <= len(raw_choices) <= 8, "ambiguous_objective",
             "goal.choices must declare between two and eight branches")
    choices = [_resolve_choice(row, f"choices[{index}]", item_catalog)
               for index, row in enumerate(raw_choices)]
    choice_ids = [choice["id"] for choice in choices]
    _require(len(choice_ids) == len(set(choice_ids)), "ambiguous_objective",
             "goal.choices IDs must be unique")
    labels = [choice["label"] for choice in choices]
    _require(len(labels) == len(set(labels)), "ambiguous_objective",
             "goal.choices labels must be unique so OpenKore can identify the intended response")
    for raw, choice in zip(raw_choices, choices):
        for requirement in choice["requires"]:
            required_item = _resolve_item(item_catalog, requirement["item"])
            _require(required_item.get("type") in ("Etc", "Healing", "Usable"), "unsupported_action",
                     f"dialogue prerequisite {required_item['aegisname']} is not a supported stackable inventory item")
            _require(int(requirement["amount"]) <= 100, "invalid_objective",
                     f"NPC branch {choice['id']} prerequisite quantities must not exceed 100")
        consume_item = _resolve_item(item_catalog, choice["consume"]["item"])
        grant_item = _resolve_item(item_catalog, choice["grant"]["item"])
        _require(choice["consume"]["amount"] <= 100 and choice["grant"]["amount"] <= 100,
                 "invalid_objective", f"NPC branch {choice['id']} quantities must not exceed 100")
        _require(int(consume_item["id"]) != int(grant_item["id"]), "unsupported_action",
                 f"NPC branch {choice['id']} cannot consume and grant the same item")
        for text_field in ("label", "message"):
            value = str(raw[text_field]).strip()
            _require(len(value) <= 100 and not any(char in value for char in ':"\\\r\n'),
                     "unsupported_action", f"NPC branch {choice['id']} has unsafe {text_field} text")

    quest_refs = [ref for choice in choices
                  for ref in ([choice["quest"]] if choice.get("quest") else [])
                  + choice.get("quest_requires", [])]
    raw_quest_fixture = goal.get("quest_fixture")
    quest_fixture = None
    quest_id = None
    if quest_refs or raw_quest_fixture is not None:
        _require(bool(quest_refs), "unsupported_action",
                 "quest_fixture is unused: at least one declared choice must reference its quest ID")
        _require(isinstance(raw_quest_fixture, dict), "missing_prerequisite",
                 "quest-gated dialogue requires a declared temporary quest_fixture")
        _reject_unknown_fields(raw_quest_fixture, {"id", "title", "monster", "count"},
                               "goal.quest_fixture")
        quest_id = _positive_int(raw_quest_fixture.get("id"), "goal.quest_fixture.id")
        title = raw_quest_fixture.get("title")
        _require(isinstance(title, str) and 1 <= len(title) <= 80
                 and not any(char in title for char in '\\"\n\r'), "ambiguous_objective",
                 "goal.quest_fixture.title must be safe text of 1 to 80 characters")
        monster = _resolve_exact_monster(load_monster_catalog(checkout), raw_quest_fixture.get("monster"))
        quest_count = _positive_int(raw_quest_fixture.get("count", 1), "goal.quest_fixture.count")
        _require(quest_count <= 100, "invalid_objective", "goal.quest_fixture.count must not exceed 100")
        _require(all(int(ref["id"]) == quest_id for ref in quest_refs), "unsupported_action",
                 "quest-gated branches must refer only to the declared temporary quest_fixture ID")
        _require(all(ref.get("state") == "active" for choice in choices
                     for ref in choice.get("quest_requires", [])), "unsupported_action",
                 "quest-gated branch planning supports active prerequisites; hunt_complete requires a composed combat objective")
        active_quests = {int(row["id"]) for row in load_quest_catalog(checkout)["quests"]}
        _require(quest_id not in active_quests, "ambiguous_objective",
                 f"temporary quest ID {quest_id} already exists in active rAthena quest data")
        _require(not adapter.quest_rows(actual, (quest_id,)), "missing_prerequisite",
                 f"character already has saved state for temporary quest ID {quest_id}")
        quest_fixture = {"id": quest_id, "title": title, "monster": monster["aegis_name"],
                         "count": quest_count}

    matches = [choice for choice in choices
               if choice["grant"] == {"item": desired_item["aegisname"], "amount": desired_amount}]
    _require(len(matches) == 1, "ambiguous_objective",
             f"desired outcome {desired_amount} {desired_item['aegisname']} matches {len(matches)} branches; exactly one is required",
             {"matching_choices": [choice["id"] for choice in matches]})
    selected = matches[0]

    preparation = objective.get("preparation", {})
    _require(isinstance(preparation, dict), "ambiguous_objective", "preparation must be an object")
    _reject_unknown_fields(preparation, {"inventory"}, "preparation")
    raw_inventory = preparation.get("inventory", [])
    _require(isinstance(raw_inventory, list), "ambiguous_objective", "preparation.inventory must be a list")
    fixtures = []
    fixture_amounts: dict[int, int] = defaultdict(int)
    for row in raw_inventory:
        _require(isinstance(row, dict) and isinstance(row.get("item"), str), "ambiguous_objective",
                 "preparation.inventory entries require an item name")
        _reject_unknown_fields(row, {"item", "amount"}, "preparation.inventory entry")
        item = _resolve_item(item_catalog, row["item"])
        amount = _positive_int(row.get("amount"), "preparation.inventory.amount")
        _require(amount <= 100, "invalid_objective", "preparation.inventory.amount must not exceed 100")
        _require(item.get("type") in ("Etc", "Healing", "Usable"), "unsupported_action",
                 f"dialogue fixture {item['aegisname']} is not a supported stackable item")
        fixtures.append({"item": item["aegisname"], "amount": amount})
        fixture_amounts[int(item["id"])] += amount

    all_requirements = {requirement["item"]: int(requirement["amount"])
                        for choice in choices for requirement in choice["requires"]}
    current: dict[str, int] = {}
    available: dict[str, int] = {}
    for name in all_requirements:
        item = _resolve_item(item_catalog, name)
        item_id = int(item["id"])
        current[name] = adapter.inventory_amount(adapter.inventory_rows(actual, (item_id,)), item_id)
        available[name] = current[name] + fixture_amounts[item_id]
    if not quest_fixture:
        for choice in choices:
            for requirement in choice["requires"]:
                name, needed = requirement["item"], int(requirement["amount"])
                _require(available[name] >= needed, "missing_prerequisite",
                         f"NPC branch {choice['id']} requires {needed} {name}; current inventory plus declared preparation provides {available[name]}",
                         {"choice": choice["id"], "item": name, "required": needed,
                          "current": current[name], "prepared": fixture_amounts[int(_resolve_item(item_catalog, name)["id"])]})

    runtime_commands = (("loadnpc", "unloadnpc", "reloadquestdb") if quest_fixture
                        else ("loadnpc", "unloadnpc"))
    _require(adapter.group_allows(group_id, runtime_commands), "missing_prerequisite",
             f"temporary rAthena group {group_id} must allow {', '.join(runtime_commands)}",
             {"group_id": group_id, "commands": list(runtime_commands)})
    deltas: dict[str, int] = defaultdict(int)
    action_steps = []
    if quest_fixture:
        action_steps.append({"id": "load_temporary_quest_definition", "type": "reload_questdb"})
    simulated_inventory = dict(available)
    simulated_quest = None
    selected_path: list[tuple[dict, list[str]]] = []
    path: list[tuple[dict, list[str]]] = []
    candidates: list[list[tuple[dict, list[str]]]] = []

    def is_eligible(choice: dict, inventory: dict[str, int], quest_state: str | None) -> bool:
        if any(inventory.get(req["item"], 0) < int(req["amount"])
               for req in choice["requires"]):
            return False
        if not all(req["state"] == "active" and quest_state == "active"
                   for req in choice.get("quest_requires", [])):
            return False
        effect = choice.get("quest")
        return not effect or (effect["action"] == "start" and quest_state is None) or (
            effect["action"] == "complete" and quest_state == "active")

    def search_dialogue(inventory: dict[str, int], quest_state: str | None,
                        used: frozenset[str]) -> None:
        if len(path) >= len(choices):
            return
        offered = [choice for choice in choices if choice["id"] not in used
                   and is_eligible(choice, inventory, quest_state)]
        offered_labels = [choice["label"] for choice in offered]
        for choice in offered:
            if choice["id"] == selected["id"]:
                candidates.append(path + [(choice, offered_labels)])
                continue
            next_quest = quest_state
            effect = choice.get("quest")
            if effect:
                if effect["action"] == "start":
                    if quest_state is not None:
                        continue
                    next_quest = "active"
                else:
                    if quest_state != "active":
                        continue
                    next_quest = "complete"
            next_inventory = dict(inventory)
            next_inventory[choice["consume"]["item"]] = (
                next_inventory.get(choice["consume"]["item"], 0)
                - int(choice["consume"]["amount"]))
            next_inventory[choice["grant"]["item"]] = (
                next_inventory.get(choice["grant"]["item"], 0)
                + int(choice["grant"]["amount"]))
            path.append((choice, offered_labels))
            search_dialogue(next_inventory, next_quest, used | {choice["id"]})
            path.pop()

    if quest_fixture:
        search_dialogue(simulated_inventory, None, frozenset())
        _require(bool(candidates), "missing_prerequisite",
                 f"no declared dialogue path satisfies prerequisites for {selected['id']} and the requested reward",
                 {"choice": selected["id"], "quest_id": quest_id})
        shortest = min(len(candidate) for candidate in candidates)
        candidates = [candidate for candidate in candidates if len(candidate) == shortest]
        _require(len(candidates) == 1, "ambiguous_objective",
                 "multiple shortest quest/dialogue paths produce the requested outcome",
                 {"paths": [[row[0]["id"] for row in candidate] for candidate in candidates]})
        selected_path = candidates[0]
    else:
        selected_path = [(selected, labels)]

    for index, (choice, offered_labels) in enumerate(selected_path, start=1):
        consume, grant = choice["consume"], choice["grant"]
        deltas[consume["item"]] -= int(consume["amount"])
        deltas[grant["item"]] += int(grant["amount"])
        step = {"id": f"select_dialogue_branch_{index}", "type": "npc_dialogue",
                "choice": choice["id"], "expected_available_choices": offered_labels,
                "preconditions": [{"inventory": {"item": consume["item"],
                    "at_least": int(consume["amount"]), "source": "openkore"}}]}
        if action_steps:
            step["preconditions"].append({"step": action_steps[-1]["id"],
                                           "path": "status", "equals": "PASS"})
        action_steps.append(step)
        simulated_inventory[consume["item"]] = simulated_inventory.get(consume["item"], 0) - int(consume["amount"])
        simulated_inventory[grant["item"]] = simulated_inventory.get(grant["item"], 0) + int(grant["amount"])
        if choice.get("quest"):
            simulated_quest = "active" if choice["quest"]["action"] == "start" else "complete"
    inventory_delta = {name: delta for name, delta in sorted(deltas.items()) if delta}
    final_quest_state = simulated_quest
    final_action = action_steps[-1]["id"]
    action_steps.extend([
        {"id": "remove_temporary_branch_npc", "type": "unload_npc",
         "preconditions": [{"step": final_action, "path": "status", "equals": "PASS"}]},
        {"id": "verify_selected_branch_result", "type": "verify_inventory",
         "preconditions": [{"step": "remove_temporary_branch_npc",
             "path": "npc_removed_from_client_view", "equals": True}],
         "expected": {"inventory_delta": inventory_delta}},
    ])
    if quest_fixture:
        action_steps[-1]["expected"]["quest"] = {"id": quest_id, "state": final_quest_state}
    rationale = ([{"action": "load_temporary_quest_definition", "capability": "reload_questdb",
        "reason": f"load the supplied quest definition {quest_id} before the NPC can assign it"}]
        if quest_fixture else []) + [
        {"action": step["id"], "capability": "npc_dialogue",
         "reason": f"OpenKore observes the current eligible menu and chooses declared branch {step['choice']} as the next prerequisite in the unique shortest path to {desired_amount} {desired_item['aegisname']}"}
        for step in action_steps if step["type"] == "npc_dialogue"
    ] + [
        {"action": "remove_temporary_branch_npc", "capability": "unload_npc",
         "reason": "remove the laboratory-created NPC after the interaction"},
        {"action": "verify_selected_branch_result", "capability": "verify_inventory",
         "reason": "compare the selected consume/grant deltas from rAthena after OpenKore logs out"},
    ]
    scenario_choices = [{"id": choice["id"], "label": choice["label"], "message": choice["message"],
                        "requires": choice["requires"], "consume": choice["consume"], "grant": choice["grant"],
                        **({"quest": choice["quest"]} if choice.get("quest") else {}),
                        "quest_requires": choice.get("quest_requires", [])}
                       for choice in choices]
    scenario = {"name": objective_id, "identity": identity,
        "initial_state": {"map": start["map"], "position": start_position},
        "server_setup": {"temporary_group_id": group_id},
        "prepare": {"inventory": {"items": fixtures}},
        "npc_dialogue": {"npc": {"map": npc["map"], "position": npc_position}, "choices": scenario_choices},
        "actions": action_steps,
        "expected": {"objective_completed": objective_id, "selected_choice": selected["id"]},
        "openkore": {"config_overrides": {"attackAuto": "0", "itemsTakeAuto": "0",
            "itemsGatherAuto": "0", "route_randomWalk": "0", f"field_{start['map']}": start["map"]}},
        "maximum_duration_seconds": duration,
        "cleanup": {"restore_character_state": True, "restore_inventory": True,
            "restore_equipment": True, "restore_account_group": True,
            "remove_temporary_npc": True, "require_character_offline": True}}
    if quest_fixture:
        scenario["quest_fixture"] = quest_fixture
    semantic_checks = [
        {"check": "character", "status": "PASS", "class_id": int(actual["class"]),
         "base_level": int(actual["base_level"]), "map": start["map"],
         "position": start_position, "source": "rAthena character row"},
        {"check": "dialogue_branches", "status": "PASS", "selected": selected["id"],
         "offered": labels, "desired_reward": {"id": int(desired_item["id"]),
             "aegisname": desired_item["aegisname"], "amount": desired_amount},
         "source": sorted({str(_resolve_item(item_catalog, choice[field]["item"])["source"])
             for choice in choices for field in ("consume", "grant")})},
        {"check": "branch_prerequisites", "status": "PASS", "available": available,
         "source": "rAthena inventory plus declared preparation",
         **({"selected_path": [choice["id"] for choice, _ in selected_path],
             "offered_per_step": [offered for _, offered in selected_path],
             "quest_fixture": quest_fixture} if quest_fixture else {})},
        {"check": "map", "status": "PASS", "map": start["map"],
         "source": "active rAthena db/map_index.txt"},
        {"check": "temporary_group", "status": "PASS", "group_id": group_id,
         "commands": list(runtime_commands)},
    ]
    scenario["planning"] = {"planner": "deterministic-v1", "objective_id": objective_id,
        "operator": "complete_npc_dialogue_branch",
        "selected_capabilities": [step["type"] for step in action_steps],
        "dependencies": rationale, "semantic_checks": semantic_checks,
        "limitations": ["declared item-gated branch selection is limited to the starting map",
            "optional temporary quest fixtures support declared start/complete effects and active prerequisites; hunt_complete requires a combat composition",
            "fixture models only supplied labels, prerequisites, consumption, reward, and supported quest effects",
            "rAthena script behavior is authoritative; remote equivalence is not established"]}
    try:
        validate_scenario_structure(scenario)
    except ValueError as exc:
        raise PlanningError("planner_output_invalid", f"planner generated an invalid runner scenario: {exc}") from exc
    return {"status": "READY", "objective_id": objective_id,
        "selected_capabilities": [step["type"] for step in action_steps],
        "action_plan": rationale, "semantic_checks": semantic_checks, "scenario": scenario}


def plan_objective(objective: dict[str, Any], rathena_path: Path,
                   rathena: Any = None, openkore_path: Path | None = None) -> dict[str, Any]:
    """Compile the supported high-level hunt quest objective to an M3 scenario."""
    _require(isinstance(objective, dict), "ambiguous_objective", "objective root must be an object")
    _require(objective.get("version") == 1, "unsupported_objective_version", "only objective version 1 is supported")
    objective_id = objective.get("id")
    _require(isinstance(objective_id, str) and re.fullmatch(r"[a-z][a-z0-9_]*", objective_id),
             "ambiguous_objective", "objective.id must be a lowercase identifier")
    allowed_fields = {"version", "id", "identity", "start", "goal", "preparation", "requirements", "runtime",
                      "input_provenance"}
    unknown = sorted(set(objective) - allowed_fields)
    if unknown:
        raise PlanningError("unsupported_action", f"unsupported objective fields: {unknown}")
    input_provenance = _input_provenance(objective)
    identity = objective.get("identity")
    _require(isinstance(identity, dict) and all(isinstance(identity.get(k), str) and identity[k]
            for k in ("master", "username", "character", "character_slot")),
             "ambiguous_objective", "identity must declare master, username, character and character_slot")
    start = objective.get("start")
    _require(isinstance(start, dict) and isinstance(start.get("map"), str),
             "ambiguous_objective", "start must declare a map and position")
    _reject_unknown_fields(start, {"map", "position"}, "start")
    start_position = _position(start.get("position"), "start.position")
    goal = objective.get("goal")
    _require(isinstance(goal, dict) and isinstance(goal.get("type"), str),
             "ambiguous_objective", "goal.type is required; the objective is ambiguous")
    if goal["type"] not in {"complete_monster_hunt", "complete_npc_exchange",
                             "complete_npc_dialogue_branch"}:
        capability = capability_for(goal["type"])
        details = {"known_capability": capability} if capability else {}
        raise PlanningError("unsupported_action", f"goal type {goal['type']!r} has no deterministic planner operator", details)

    checkout = rathena_path.expanduser().resolve()
    if rathena is None:
        from .adapters.rathena import RathenaAdapter
        rathena = RathenaAdapter(checkout)
    adapter = rathena
    runtime = objective.get("runtime", {})
    _require(isinstance(runtime, dict), "ambiguous_objective", "runtime must be an object")
    _reject_unknown_fields(runtime, {"temporary_group_id", "maximum_duration_seconds"}, "runtime")
    group_id = runtime.get("temporary_group_id", 3)
    _require(type(group_id) is int and group_id > 0, "invalid_objective", "runtime.temporary_group_id must be positive")
    duration = runtime.get("maximum_duration_seconds", 240)
    _require(type(duration) is int and 30 <= duration <= 900, "invalid_objective",
             "runtime.maximum_duration_seconds must be between 30 and 900")

    actual = adapter.character(identity["character"])
    _require(actual.get("online") == 0, "missing_prerequisite", "selected character must be offline while planning")
    _require((actual.get("map"), actual.get("x"), actual.get("y")) ==
             (start["map"], start_position["x"], start_position["y"]),
             "missing_prerequisite", "actual character map/position does not match objective.start",
             {"expected": {"map": start["map"], **start_position},
              "actual": {"map": actual.get("map"), "x": actual.get("x"), "y": actual.get("y")}})

    available_maps = _maps(checkout)
    if goal["type"] == "complete_npc_exchange":
        plan = _plan_npc_exchange(objective_id, objective, goal, identity, actual, start,
                                  start_position, checkout, adapter, available_maps, group_id, duration)
        return _attach_input_provenance(plan, input_provenance)
    if goal["type"] == "complete_npc_dialogue_branch":
        plan = _plan_npc_dialogue_branch(objective_id, objective, goal, identity, actual, start,
                                         start_position, checkout, adapter, available_maps, group_id, duration)
        return _attach_input_provenance(plan, input_provenance)
    environment = goal.get("environment")
    _require(isinstance(environment, dict), "ambiguous_objective", "goal.environment is required")
    _reject_unknown_fields(goal, {"type", "quest", "target", "environment", "accept", "turn_in",
                                  "collect", "execution"}, "goal")
    execution = goal.get("execution", {})
    _require(isinstance(execution, dict), "ambiguous_objective", "goal.execution must be an object")
    _reject_unknown_fields(execution, {"combat", "openkore_profile"}, "goal.execution")
    combat_driver = execution.get("combat", "openkore_command")
    _require(combat_driver in ("openkore_command", "openkore_ai"), "unsupported_action",
             f"combat driver {combat_driver!r} is unsupported")
    declared_openkore_profile = execution.get("openkore_profile", {})
    _require(isinstance(declared_openkore_profile, dict), "ambiguous_objective",
             "goal.execution.openkore_profile must be an object")
    _require(combat_driver == "openkore_ai" or not declared_openkore_profile, "unsupported_action",
             "goal.execution.openkore_profile currently requires combat=openkore_ai")
    _reject_unknown_fields(declared_openkore_profile,
        {"config_overrides", "control_file_overrides"}, "goal.execution.openkore_profile")
    _reject_unknown_fields(environment, {"npc", "hunt_map", "hunt_position", "return_position"}, "goal.environment")
    npc = environment.get("npc")
    _require(isinstance(npc, dict) and isinstance(npc.get("map"), str), "ambiguous_objective",
             "goal.environment.npc must declare map and position")
    _reject_unknown_fields(npc, {"map", "position"}, "goal.environment.npc")
    npc_position = _position(npc.get("position"), "goal.environment.npc.position")
    hunt_map = environment.get("hunt_map", start["map"])
    hunt_position = _position(environment.get("hunt_position"), "goal.environment.hunt_position")
    return_position = _position(environment.get("return_position", start_position),
                                "goal.environment.return_position")
    requested_maps = (start["map"], npc["map"], hunt_map)
    missing_maps = sorted(set(requested_maps) - available_maps)
    _require(not missing_maps, "unavailable_map", f"map(s) absent from active rAthena map_index: {missing_maps}",
             {"maps": missing_maps})
    _require(len(set(requested_maps)) == 1, "unsupported_action",
             "this planner composition has no map-transition capability; all locations must be on the start map")

    quest = goal.get("quest")
    target = goal.get("target")
    _require(isinstance(quest, dict) and isinstance(target, dict), "ambiguous_objective",
             "complete_monster_hunt requires quest and target declarations")
    _reject_unknown_fields(quest, {"id", "title"}, "goal.quest")
    _reject_unknown_fields(target, {"monster", "count"}, "goal.target")
    quest_id = _positive_int(quest.get("id"), "goal.quest.id")
    title = quest.get("title")
    _require(isinstance(title, str) and 1 <= len(title) <= 60 and not re.search(r"[\r\n\"\\]", title),
             "ambiguous_objective", "goal.quest.title must be a single safe string up to 60 characters")
    count = _positive_int(target.get("count"), "goal.target.count")
    _require(count == 1, "unsupported_action", "the current shared combat quest runner supports exactly one target")
    mob_catalog = load_monster_catalog(checkout)
    monster = _resolve_exact_monster(mob_catalog, target.get("monster"))

    quest_catalog = load_quest_catalog(checkout)
    active_quests = {int(row["id"]): row for row in quest_catalog["quests"]}
    _require(quest_id not in active_quests, "missing_prerequisite",
             f"temporary quest ID {quest_id} already exists in the active rAthena quest DB")
    existing_rows = adapter.quest_rows(actual, (quest_id,))
    _require(not existing_rows, "missing_prerequisite",
             f"temporary quest ID {quest_id} already has saved character progress")

    item_catalog = load_item_catalog(checkout)
    collect_drop = None
    raw_collect = goal.get("collect")
    if raw_collect is not None:
        _require(isinstance(raw_collect, dict) and isinstance(raw_collect.get("item"), str),
                 "ambiguous_objective", "goal.collect must declare a drop item and amount")
        _reject_unknown_fields(raw_collect, {"item", "amount"}, "goal.collect")
        collect_item = _resolve_item(item_catalog, raw_collect["item"])
        collect_amount = _positive_int(raw_collect.get("amount", 1), "goal.collect.amount")
        _require(collect_amount == 1, "unsupported_action",
                 "the current collector supports one guaranteed ground item per hunt")
        _require(collect_item.get("type") in ("Etc", "Healing", "Usable"), "unsupported_action",
                 f"monster drop {collect_item['aegisname']} is not a supported stackable inventory item")
        guaranteed_drops = sum(1 for drop in monster.get("drops", [])
            if str(drop.get("item", "")).casefold() == str(collect_item["aegisname"]).casefold()
            and int(drop.get("rate", 0)) >= 10000)
        _require(guaranteed_drops >= collect_amount, "unsupported_action",
                 f"active mob data does not guarantee {collect_amount} {collect_item['aegisname']} drop(s); deterministic collection requires Rate 10000",
                 {"monster": monster["aegis_name"], "item": collect_item["aegisname"],
                  "declared_drops": monster.get("drops", [])})
        collect_drop = {"item": collect_item, "amount": collect_amount, "rate": 10000}
    accept = _resolve_choice(goal.get("accept"), "accept", item_catalog)
    turn_in = _resolve_choice(goal.get("turn_in"), "turn_in", item_catalog)
    _require(accept["id"] != turn_in["id"], "ambiguous_objective", "accept and turn_in choice IDs must differ")
    accept["quest"] = {"id": quest_id, "action": "start"}
    turn_in["quest_requires"] = [{"id": quest_id, "state": "hunt_complete"}]
    turn_in["quest"] = {"id": quest_id, "action": "complete"}

    preparation = objective.get("preparation", {})
    _require(isinstance(preparation, dict), "ambiguous_objective", "preparation must be an object")
    _reject_unknown_fields(preparation, {"attributes", "inventory"}, "preparation")
    attributes = preparation.get("attributes", {})
    _require(isinstance(attributes, dict), "ambiguous_objective", "preparation.attributes must be an object")
    unsupported_attributes = sorted(set(attributes) - adapter.PREPARABLE_FIELDS)
    _require(not unsupported_attributes, "unsupported_action",
             f"unsupported character preparation fields: {unsupported_attributes}")
    _require(all(type(value) is int and 0 <= value <= 2_000_000_000 for value in attributes.values()),
             "invalid_objective", "prepared character attributes must be bounded non-negative integers")
    raw_inventory = preparation.get("inventory", [])
    _require(isinstance(raw_inventory, list), "ambiguous_objective", "preparation.inventory must be a list")
    fixtures = []
    fixture_amounts: dict[int, int] = defaultdict(int)
    referenced_items: dict[int, dict] = {}
    for row in raw_inventory:
        _require(isinstance(row, dict) and isinstance(row.get("item"), str), "ambiguous_objective",
                 "preparation.inventory entries require an item name")
        _reject_unknown_fields(row, {"item", "amount"}, "preparation.inventory entry")
        item = _resolve_item(item_catalog, row["item"])
        amount = _positive_int(row.get("amount"), "preparation.inventory.amount")
        fixtures.append({"item": item["aegisname"], "amount": amount})
        fixture_amounts[int(item["id"])] += amount
        referenced_items[int(item["id"])] = item

    transactions = (accept, turn_in)
    expected_deltas: dict[str, int] = defaultdict(int)
    for choice in transactions:
        for requirement in choice["requires"]:
            item = _resolve_item(item_catalog, requirement["item"])
            item_id = int(item["id"])
            referenced_items[item_id] = item
        for field in ("consume", "grant"):
            item = _resolve_item(item_catalog, choice[field]["item"])
            item_id, amount = int(item["id"]), int(choice[field]["amount"])
            referenced_items[item_id] = item
            delta = -amount if field == "consume" else amount
            expected_deltas[str(item["aegisname"])] += delta
    if collect_drop:
        drop_item = collect_drop["item"]
        referenced_items[int(drop_item["id"])] = drop_item
        expected_deltas[str(drop_item["aegisname"])] += int(collect_drop["amount"])
    for requirement in accept["requires"]:
        item = _resolve_item(item_catalog, requirement["item"])
        item_id, amount = int(item["id"]), int(requirement["amount"])
        current_rows = adapter.inventory_rows(actual, (item_id,))
        current = adapter.inventory_amount(current_rows, item_id)
        if current + fixture_amounts[item_id] < amount:
            item = referenced_items[item_id]
            raise PlanningError("missing_prerequisite",
                f"NPC choice requires {amount} {item['aegisname']}; current inventory plus declared preparation provides "
                f"{current + fixture_amounts[item_id]}",
                {"item": item["aegisname"], "required": amount,
                 "current": current, "prepared": fixture_amounts[item_id]})

    # Check turn-in stock after the acceptance transaction using real current
    # inventory plus declarative preparation and the declared server effects.
    for requirement in turn_in["requires"]:
        item = _resolve_item(item_catalog, requirement["item"])
        item_id = int(item["id"])
        current_rows = adapter.inventory_rows(actual, (item_id,))
        current = adapter.inventory_amount(current_rows, item_id) + fixture_amounts[item_id]
        current += sum((-int(choice["consume"]["amount"]) if choice["consume"]["item"] == item["aegisname"] else 0) +
                       (int(choice["grant"]["amount"]) if choice["grant"]["item"] == item["aegisname"] else 0)
                       for choice in (accept,))
        if collect_drop and collect_drop["item"]["aegisname"] == item["aegisname"]:
            current += int(collect_drop["amount"])
        _require(current >= int(requirement["amount"]), "missing_prerequisite",
                 f"turn-in choice requires {requirement['amount']} {item['aegisname']} after acceptance; planned flow provides {current}",
                 {"item": item["aegisname"], "required": requirement["amount"], "available_after_accept": current})
    requirements = objective.get("requirements", {})
    _require(isinstance(requirements, dict), "ambiguous_objective", "requirements must be an object")
    _reject_unknown_fields(requirements, {"character", "equipment"}, "requirements")
    character_req = requirements.get("character", {})
    _require(isinstance(character_req, dict), "ambiguous_objective", "requirements.character must be an object")
    _reject_unknown_fields(character_req, {"job", "minimum_base_level"}, "requirements.character")
    actual_job = class_job_name(checkout, int(actual["class"]))
    requested_job = character_req.get("job")
    if requested_job:
        candidates = job_candidates(str(requested_job).upper())
        candidates.add(str(requested_job))
        actual_candidates = job_candidates(actual_job)
        _require(bool(candidates & actual_candidates), "incompatible_character",
                 f"character job {actual_job} does not satisfy required job {requested_job}")
    min_level = character_req.get("minimum_base_level", 1)
    _require(type(min_level) is int and min_level >= 1, "invalid_objective",
             "requirements.character.minimum_base_level must be positive")
    _require(int(actual["base_level"]) >= min_level, "incompatible_character",
             f"character base level {actual['base_level']} is below required {min_level}")
    semantic_character = {**actual, **attributes,
                          "attack": int(attributes.get("str", actual.get("str", 1)))}
    combat = assess_match(semantic_character, monster, "victory", duration_seconds=duration)

    equipment_checks = []
    current_equipment = adapter.equipped_inventory_rows(actual)
    slot_masks = equipment_location_masks(checkout)
    raw_equipment_checks = requirements.get("equipment", [])
    _require(isinstance(raw_equipment_checks, list), "ambiguous_objective",
             "requirements.equipment must be a list")
    for check in raw_equipment_checks:
        _require(isinstance(check, dict) and isinstance(check.get("item"), str),
                 "ambiguous_objective", "requirements.equipment entries need an item")
        _reject_unknown_fields(check, {"item", "location", "condition"}, "requirements.equipment entry")
        item = _resolve_item(item_catalog, check["item"])
        location = check.get("location")
        requested = check.get("condition", "compatible")
        _require(requested in ("compatible", "equipped"), "unsupported_action",
                 f"equipment condition {requested!r} is unsupported")
        assessment = assess_equipment(item, int(actual["class"]), int(actual["base_level"]),
                                     location, checkout)
        _require(assessment["can_equip"], "incompatible_equipment",
                 f"{item['aegisname']} is incompatible with {actual_job}: {assessment['reasons']}",
                 {"item": item["aegisname"], "assessment": assessment})
        equipped = [row for row in current_equipment if int(row["nameid"]) == int(item["id"])]
        if requested == "equipped":
            mask = slot_masks.get(str(location)) if location else None
            _require(bool(equipped) and (mask is None or any(int(row["equip"]) & mask for row in equipped)),
                     "missing_prerequisite", f"required equipment {item['aegisname']} is not equipped in {location}")
        equipment_checks.append({"item": item["aegisname"], "condition": requested,
                                 "assessment": assessment, "currently_equipped": bool(equipped)})

    runtime_commands = ("loadnpc", "unloadnpc", "reloadquestdb", "monster", "killmonster2")
    _require(adapter.group_allows(group_id, runtime_commands), "missing_prerequisite",
             f"temporary rAthena group {group_id} must allow {', '.join(runtime_commands)}",
             {"group_id": group_id, "commands": list(runtime_commands)})

    choice_ids = {accept["id"], turn_in["id"]}
    _require(len(choice_ids) == 2, "ambiguous_objective", "NPC choice IDs must be unique")
    dialogue_choices = [accept, turn_in]
    preparation_items = fixtures
    action_steps = [
        {"id": "reload_objective_quest", "type": "reload_questdb"},
        {"id": "accept_objective_quest", "type": "npc_dialogue", "choice": accept["id"],
         "preconditions": [{"step": "reload_objective_quest", "path": "status", "equals": "PASS"}] +
                         [{"inventory": {"item": req["item"], "at_least": req["amount"], "source": "openkore"}}
                          for req in accept["requires"]]},
        {"id": "move_to_hunt", "type": "move", "target": hunt_position,
         "preconditions": [{"step": "accept_objective_quest", "path": "status", "equals": "PASS"}]},
        {"id": "spawn_objective_target", "type": "spawn_monster", "monster": monster["aegis_name"],
         "preconditions": [{"step": "move_to_hunt", "path": "status", "equals": "PASS"}]},
    ]
    if combat_driver == "openkore_ai":
        native_marker = f"KORELAB_NATIVE_HUNT_{quest_id}_READY"
        native_inventory_delta = ({collect_drop["item"]["aegisname"]: collect_drop["amount"]}
                                  if collect_drop else {})
        action_steps.append({"id": "await_native_objective_target", "type": "await_native_ai",
            "spawn_step": "spawn_objective_target", "eventmacro_marker": native_marker,
            "quest_state_after_action": "hunt_complete", "inventory_delta": native_inventory_delta,
            "timeout_seconds": min(120, duration // 2),
            "preconditions": [{"step": "spawn_objective_target", "path": "status", "equals": "PASS"}]})
        completion_step = "await_native_objective_target"
    else:
        action_steps.append({"id": "defeat_objective_target", "type": "attack_monster", "spawn_step": "spawn_objective_target",
            "timeout_seconds": min(90, duration // 2),
            "preconditions": [{"step": "spawn_objective_target", "path": "status", "equals": "PASS"}]})
        completion_step = "defeat_objective_target"
    action_steps.extend([
        {"id": "return_to_objective_npc", "type": "move", "target": return_position,
         "preconditions": [{"step": completion_step, "path": "status", "equals": "PASS"}]},
        {"id": "turn_in_objective_quest", "type": "npc_dialogue", "choice": turn_in["id"],
         "preconditions": [{"step": "return_to_objective_npc", "path": "status", "equals": "PASS"}] +
                         [{"inventory": {"item": req["item"], "at_least": req["amount"], "source": "openkore"}}
                          for req in turn_in["requires"]]},
        {"id": "unload_objective_npc", "type": "unload_npc",
         "preconditions": [{"step": "turn_in_objective_quest", "path": "status", "equals": "PASS"}]},
        {"id": "verify_objective_reward", "type": "verify_inventory",
         "preconditions": [{"step": "unload_objective_npc", "path": "npc_removed_from_client_view", "equals": True}],
         "expected": {"inventory_delta": dict(sorted(expected_deltas.items())),
                      "quest": {"id": quest_id, "state": "complete"}}},
    ])
    if collect_drop and combat_driver == "openkore_command":
        action_steps.insert(5, {"id": "collect_objective_drop", "type": "collect_monster_drop",
            "attack_step": "defeat_objective_target", "item": collect_drop["item"]["aegisname"],
            "amount": collect_drop["amount"], "timeout_seconds": min(30, duration // 4),
            "preconditions": [{"step": "defeat_objective_target", "path": "status", "equals": "PASS"}]})
    generated_eventmacro = None
    openkore_profile = {"config_overrides": {"attackAuto": "0", "itemsTakeAuto": "0",
        "itemsGatherAuto": "0", "route_randomWalk": "0", f"field_{start['map']}": start["map"]}}
    if combat_driver == "openkore_ai":
        openkore_config = {"attackAuto": 2, "attackAuto_notInTown": 0,
            "itemsTakeAuto": 1 if collect_drop else 0,
            "itemsGatherAuto": 2 if collect_drop else 0,
            "itemsGatherAuto_notInTown": 0, "route_randomWalk": 0,
            "itemsMaxWeight": 90, "lockMap": hunt_map}
        controls = {"mon_control.txt": {"mode": "replace",
            "content": f"all 0 0 0\n{int(monster['id'])} 1 0 0\n"}}
        if collect_drop:
            item_name = collect_drop["item"]["aegisname"]
            controls.update({
                "pickupitems.txt": {"mode": "replace", "content": f"all 0\n{item_name} 1\n"},
                "items_control.txt": {"mode": "replace",
                    "content": f"all 0 0 0\n{item_name} 1 0 0\n"},
            })
        user_config = declared_openkore_profile.get("config_overrides", {})
        user_controls = declared_openkore_profile.get("control_file_overrides", {})
        _require(isinstance(user_config, dict), "ambiguous_objective",
                 "goal.execution.openkore_profile.config_overrides must be an object")
        _require(isinstance(user_controls, dict), "ambiguous_objective",
                 "goal.execution.openkore_profile.control_file_overrides must be an object")
        collisions = sorted(set(controls) & set(user_controls))
        _require(not collisions, "ambiguous_objective",
                 f"declared control-file policy conflicts with generated hunt filters: {collisions}; merge these rules in the objective's own control file after reviewing server semantics")
        if user_config or user_controls:
            _require(openkore_path is not None, "missing_prerequisite",
                     "declared OpenKore runtime policy requires --openkore for source-backed validation")
        if user_controls:
            registered_controls = discover_control_files(openkore_path.expanduser().resolve())
            unsupported_controls = sorted(set(user_controls) - set(registered_controls))
            _require(not unsupported_controls, "unsupported_action",
                     f"declared OpenKore control files are not registered in the selected installation: {unsupported_controls}")
        openkore_profile = {"ai_mode": "on",
            "config_overrides": {**openkore_config, **user_config},
            "control_file_overrides": {**controls, **user_controls}}
        effective_config = openkore_profile["config_overrides"]
        try:
            attack_enabled = int(effective_config.get("attackAuto", 0)) > 0
            pickup_enabled = (not collect_drop or
                (int(effective_config.get("itemsTakeAuto", 0)) > 0
                 and int(effective_config.get("itemsGatherAuto", 0)) > 0))
        except (TypeError, ValueError):
            attack_enabled = pickup_enabled = False
        _require(attack_enabled and pickup_enabled, "incompatible_policy",
                 "declared OpenKore policy disables combat or required native item pickup")
        _require(str(effective_config.get("lockMap", "")) == str(hunt_map), "incompatible_policy",
                 "declared OpenKore lockMap conflicts with the objective hunt map")
        if user_config or user_controls:
            try:
                validate_runtime_profile(openkore_path.expanduser().resolve(), openkore_profile)
            except ValueError as exc:
                raise PlanningError("unsupported_action",
                    f"invalid declared OpenKore runtime policy: {exc}") from exc
        action_steps.insert(-1, {"id": "restore_native_ai_policy", "type": "restore_openkore_profile",
            "restore_ai_mode": "manual", "handoff_macro": "korelab_resume_ai",
            "handoff_marker": "KORELAB_AI_HANDOFF_COMPLETE", "timeout_seconds": 20,
            "preconditions": [{"step": "unload_objective_npc", "path": "status", "equals": "PASS"}]})
        action_steps[-1]["preconditions"] = [{"step": "restore_native_ai_policy", "path": "status", "equals": "PASS"}]
        generated_eventmacro = _native_hunt_eventmacro(int(quest_id), int(monster["id"]),
            collect_drop["item"] if collect_drop else None,
            int(collect_drop["amount"]) if collect_drop else 1)
    combat_action_name = ("await_native_objective_target" if combat_driver == "openkore_ai"
                          else "defeat_objective_target")
    rationale = [
        {"action": "reload_objective_quest", "capability": "reload_questdb",
         "reason": "activate the collision-checked temporary QUEST_DB target before NPC dialogue"},
        {"action": "accept_objective_quest", "capability": "npc_dialogue",
         "reason": "perform the declared inventory transaction and set the quest active"},
        {"action": "move_to_hunt", "capability": "move",
         "reason": "place the character at the requested hunt staging position after acceptance"},
        {"action": "spawn_objective_target", "capability": "spawn_monster",
         "reason": f"spawn the exact active-database target {monster['aegis_name']}"},
        {"action": combat_action_name,
         "capability": "await_native_ai" if combat_driver == "openkore_ai" else "attack_monster",
         "reason": ("let the configured OpenKore AI select and defeat the allowed target; eventMacro waits for native quest progress"
                    if combat_driver == "openkore_ai" else
                    "use OpenKore native combat, gated by active mob data and combat semantics")},
        {"action": "return_to_objective_npc", "capability": "move",
         "reason": "return to the declared turn-in position after the server hunt counter advances"},
        {"action": "turn_in_objective_quest", "capability": "npc_dialogue",
         "reason": "select the branch whose quest prerequisite requires the hunt to be complete"},
        {"action": "unload_objective_npc", "capability": "unload_npc",
         "reason": "remove the NPC created and tracked by this scenario"},
        {"action": "verify_objective_reward", "capability": "verify_inventory",
         "reason": "verify reward deltas and quest completion from rAthena after OpenKore logout"},
    ]
    if collect_drop and combat_driver == "openkore_command":
        rationale.insert(5, {"action": "collect_objective_drop", "capability": "collect_monster_drop",
            "reason": f"use OpenKore's native take command to collect the active mob DB's guaranteed {collect_drop['item']['aegisname']} drop"})
    if combat_driver == "openkore_ai":
        rationale.insert(-1, {"action": "restore_native_ai_policy", "capability": "restore_openkore_profile",
            "reason": "restore the user's original runtime policy and hand off control to OpenKore AI"})
    scenario = {
        "name": objective_id,
        "identity": identity,
        "initial_state": {"map": start["map"], "position": start_position},
        "server_setup": {"temporary_group_id": group_id},
        "prepare": {"attributes": attributes, "inventory": {"items": preparation_items}},
        "quest_fixture": {"id": quest_id, "title": title, "monster": monster["aegis_name"], "count": count},
        "npc_dialogue": {"npc": {"map": npc["map"], "position": npc_position}, "choices": dialogue_choices},
        "actions": action_steps,
        "expected": {"objective_completed": objective_id},
        "openkore": openkore_profile,
        "maximum_duration_seconds": duration,
        "cleanup": {"restore_inventory": True, "restore_equipment": True,
            "restore_quest_progress": True, "restore_account_group": True,
            "require_character_offline": True},
    }
    semantic_checks = [
        {"check": "character", "status": "PASS", "class": actual_job,
         "base_level": int(actual["base_level"]), "map": actual["map"], "position": start_position,
         "source": "rAthena character row"},
        {"check": "monster", "status": "PASS", "id": int(monster["id"]),
         "aegis_name": monster["aegis_name"], "hp": int(monster["hp"]),
         "element": monster["element"], "source": mob_catalog["sources"]},
        {"check": "quest_id", "status": "PASS", "id": quest_id,
         "reason": "ID is absent from active quest DB and saved character quest rows"},
        {"check": "maps", "status": "PASS", "maps": list(requested_maps),
         "source": "active rAthena db/map_index.txt"},
        {"check": "combat_fit", "status": "PASS", "assessment": combat},
        {"check": "equipment", "status": "PASS", "checks": equipment_checks},
        {"check": "npc_items", "status": "PASS", "source": "active item DB and current inventory plus declared preparation"},
    ]
    if collect_drop:
        semantic_checks.append({"check": "monster_drop", "status": "PASS",
            "monster": monster["aegis_name"], "item": collect_drop["item"]["aegisname"],
            "rate": collect_drop["rate"], "amount": collect_drop["amount"],
            "source": mob_catalog["sources"]})
    scenario["planning"] = {"planner": "deterministic-v1", "objective_id": objective_id,
        "operator": goal["type"], "selected_capabilities": [step["type"] for step in action_steps],
        "dependencies": rationale, "semantic_checks": semantic_checks,
        "limitations": ["map-index membership does not prove cell walkability",
            "combat fit is heuristic; rAthena executes the authoritative damage rules",
            "collection supports one declared 100% mob drop per hunt and does not plan random drops"]}
    try:
        validate_scenario_structure(scenario)
    except ValueError as exc:
        raise PlanningError("planner_output_invalid", f"planner generated an invalid runner scenario: {exc}") from exc
    plan = {"status": "READY", "objective_id": objective_id,
            "selected_capabilities": [step["type"] for step in action_steps],
            "action_plan": rationale, "semantic_checks": semantic_checks,
            "scenario": scenario,
            **({"artifacts": {"eventmacro_source": generated_eventmacro}} if generated_eventmacro else {})}
    return _attach_input_provenance(plan, input_provenance)


def plan_objective_file(path: Path, rathena_path: Path,
                        openkore_path: Path | None = None) -> dict[str, Any]:
    source = path.expanduser().resolve()
    raw_objective = json.loads(source.read_text(encoding="utf-8"))
    objective = load_scenario(source)
    plan = plan_objective(objective, rathena_path, openkore_path=openkore_path)
    # Keep local identity as environment references in the generated scenario;
    # planning uses the expanded values, but generated evidence must not copy them.
    plan["scenario"]["identity"] = raw_objective["identity"]
    validate_scenario_structure(plan["scenario"])
    return plan


def write_planned_scenario(plan: dict[str, Any], output: Path) -> Path:
    if plan.get("status") != "READY" or not isinstance(plan.get("scenario"), dict):
        raise PlanningError("invalid_plan", "only a READY plan can be written as a scenario")
    target = output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    eventmacro_source = plan.get("artifacts", {}).get("eventmacro_source")
    eventmacro_path = None
    if eventmacro_source:
        eventmacro_path = target.with_suffix(".eventmacro.txt")
        if target.exists() or eventmacro_path.exists():
            raise FileExistsError("planned scenario or generated eventMacro artifact already exists")
        plan["scenario"].setdefault("openkore", {})["eventmacro_source"] = str(eventmacro_path)
        validate_scenario_structure(plan["scenario"])
    elif target.exists():
        raise FileExistsError("planned scenario output already exists")
    if eventmacro_source:
        eventmacro_path.write_text(str(eventmacro_source), encoding="utf-8")
        set_private_permissions(eventmacro_path, 0o600)
    target.write_text(json.dumps(plan["scenario"], indent=2) + "\n", encoding="utf-8")
    if eventmacro_path:
        plan["artifact_paths"] = {"eventmacro_source": str(eventmacro_path)}
    return target
