from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .capabilities import ORDERED_ACTION_TYPES


_VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def load_scenario(path: Path) -> dict[str, Any]:
    """Load JSON and expand explicit environment references in declarative fields."""
    scenario = json.loads(path.read_text(encoding="utf-8"))
    missing: set[str] = set()

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            def replace(match: re.Match) -> str:
                name = match.group(1)
                result = os.environ.get(name)
                if result is None:
                    missing.add(name)
                    return match.group(0)
                return result
            return _VARIABLE.sub(replace, value)
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        return value

    scenario = expand(scenario)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"scenario requires environment settings: {names}")
    return scenario


def validate_scenario_structure(scenario: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate structural contracts shared by planned and hand-authored scenarios."""
    required = {"name", "identity", "initial_state", "expected", "cleanup",
                "maximum_duration_seconds"}
    missing = sorted(required - scenario.keys())
    if missing:
        raise ValueError(f"scenario is missing fields: {missing}")
    openkore = scenario.get("openkore", {})
    if isinstance(openkore, dict) and openkore.get("portal_profile_id") is not None:
        if (not isinstance(openkore["portal_profile_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", openkore["portal_profile_id"])):
            raise ValueError("openkore.portal_profile_id must be a stable server ID using letters, digits, dot, underscore, or hyphen")
    transitions = openkore.get("map_transitions", []) if isinstance(openkore, dict) else []
    if not isinstance(transitions, list):
        raise ValueError("openkore.map_transitions must be an array of declared from/to map pairs")
    declared_transitions = set()
    for edge in transitions:
        if (not isinstance(edge, dict) or set(edge) != {"from", "to"}
                or not all(isinstance(edge.get(key), str)
                           and re.fullmatch(r"[a-z0-9_]+", edge[key]) for key in ("from", "to"))
                or edge["from"] == edge["to"]):
            raise ValueError("each map transition must declare distinct lowercase from and to map names")
        declared_transitions.add((edge["from"], edge["to"]))
    steps = scenario.get("actions")
    if steps is not None:
        if not isinstance(steps, list) or not steps:
            raise ValueError("actions must be a non-empty ordered array")
        ids = [step.get("id") for step in steps if isinstance(step, dict)]
        if len(ids) != len(steps) or any(not isinstance(step_id, str) or not step_id for step_id in ids):
            raise ValueError("every ordered action requires a non-empty string id")
        if len(set(ids)) != len(ids):
            raise ValueError("ordered action IDs must be unique")
        available = set()
        current_map = scenario.get("initial_state", {}).get("map")
        ai_mode = openkore.get("ai_mode", "manual") if isinstance(openkore, dict) else "manual"
        for step in steps:
            if step.get("type") not in ORDERED_ACTION_TYPES:
                raise ValueError(f"unsupported ordered action type: {step.get('type')}")
            step_type = step.get("type")
            needs_queued_npc_task = (step_type == "npc_exchange"
                or step_type == "npc_dialogue" and step.get("driver", "runner") == "runner")
            if needs_queued_npc_task and ai_mode == "off":
                raise ValueError("native NPC interaction cannot run while OpenKore AI is off; "
                    "set openkore.ai_mode to 'manual' or 'on' before this action")
            if step_type == "set_ai_mode":
                if step.get("mode") not in ("on", "manual", "off"):
                    raise ValueError("set_ai_mode requires mode on, manual, or off")
                ai_mode = step["mode"]
            if step.get("type") == "npc_dialogue":
                driver = step.get("driver", "runner")
                if driver not in ("runner", "eventmacro"):
                    raise ValueError("npc_dialogue driver must be runner or eventmacro")
                if driver == "eventmacro":
                    if not scenario.get("openkore", {}).get("eventmacro_source"):
                        raise ValueError("eventmacro-driven npc_dialogue requires a loaded eventmacro_source")
                    if not isinstance(step.get("eventmacro_trigger"), str) or not re.fullmatch(
                            r"[a-z][a-z0-9_]*", step["eventmacro_trigger"]):
                        raise ValueError("eventmacro-driven npc_dialogue requires an eventmacro_trigger name")
                    if not isinstance(step.get("eventmacro_marker"), str) or not re.fullmatch(
                            r"[A-Za-z0-9_.:-]{1,120}", step["eventmacro_marker"]):
                        raise ValueError("eventmacro-driven npc_dialogue requires a literal eventmacro_marker")
                    if not isinstance(step.get("expected_available_choices"), list) or not step["expected_available_choices"]:
                        raise ValueError("eventmacro-driven npc_dialogue requires expected_available_choices")
            if step.get("type") == "verify_inventory":
                expected = step.get("expected", {})
                if not isinstance(expected, dict):
                    raise ValueError("verify_inventory expected must be an object")
                exact = expected.get("inventory_delta", {})
                ranges = expected.get("inventory_delta_ranges", {})
                if not isinstance(exact, dict) or not isinstance(ranges, dict):
                    raise ValueError("inventory_delta and inventory_delta_ranges must be objects")
                if set(exact) & set(ranges):
                    raise ValueError("an item cannot have both an exact inventory delta and a ranged delta")
                for item_name, bounds in ranges.items():
                    if (not isinstance(item_name, str) or not item_name
                            or not isinstance(bounds, dict) or set(bounds) != {"min", "max"}
                            or type(bounds.get("min")) is not int or type(bounds.get("max")) is not int
                            or bounds["min"] > bounds["max"]):
                        raise ValueError("inventory_delta_ranges entries require ordered integer min/max bounds")
                location = expected.get("location")
                if location is not None and (not isinstance(location, dict)
                        or set(location) != {"map", "x", "y"}
                        or not isinstance(location.get("map"), str)
                        or not re.fullmatch(r"[a-z0-9_]+", location["map"])
                        or type(location.get("x")) is not int or type(location.get("y")) is not int
                        or not (0 <= location["x"] <= 999 and 0 <= location["y"] <= 999)):
                    raise ValueError("verify_inventory expected.location requires map and bounded integer x/y")
            if step.get("type") == "verify_equipment":
                expected = step.get("expected", {})
                if not isinstance(expected, dict):
                    raise ValueError("verify_equipment expected must be an object")
                inventory_delta = expected.get("inventory_delta", {})
                if (not isinstance(inventory_delta, dict)
                        or any(not isinstance(name, str) or not name
                               or type(delta) is not int
                               for name, delta in inventory_delta.items())):
                    raise ValueError("verify_equipment expected.inventory_delta must map item names to integer deltas")
                location = expected.get("location")
                if location is not None and (not isinstance(location, dict)
                        or set(location) != {"map", "x", "y"}
                        or not isinstance(location.get("map"), str)
                        or not re.fullmatch(r"[a-z0-9_]+", location["map"])
                        or type(location.get("x")) is not int or type(location.get("y")) is not int
                        or not (0 <= location["x"] <= 999 and 0 <= location["y"] <= 999)):
                    raise ValueError("verify_equipment expected.location requires map and bounded integer x/y")
            if step.get("type") == "npc_exchange":
                npc = step.get("npc", {})
                name = npc.get("name") if isinstance(npc, dict) else None
                if name is not None and (not isinstance(name, str)
                        or not re.fullmatch(r"KoreLabM3[A-Z0-9]{6}", name)):
                    raise ValueError("npc_exchange npc.name must use KoreLabM3 plus six uppercase letters or digits")
                native_sequence = step.get("sequence", "x c")
                if (not isinstance(native_sequence, str) or not re.fullmatch(
                        r"(?:x|k|c|n|b|r\d+|w\d+)(?: (?:x|k|c|n|b|r\d+|w\d+))*",
                        native_sequence)):
                    raise ValueError("npc_exchange sequence must contain supported native OpenKore talk steps")
            if step.get("type") == "move":
                target = step.get("target")
                if (not isinstance(target, dict) or type(target.get("x")) is not int
                        or type(target.get("y")) is not int
                        or not (0 <= target["x"] <= 999 and 0 <= target["y"] <= 999)):
                    raise ValueError("move actions require bounded integer target coordinates")
                destination_map = target.get("map", current_map)
                if not isinstance(destination_map, str) or not re.fullmatch(r"[a-z0-9_]+", destination_map):
                    raise ValueError("move target map must be a lowercase map name")
                if destination_map != current_map and (current_map, destination_map) not in declared_transitions:
                    raise ValueError("cross-map move requires an explicit openkore.map_transitions edge from the current map")
                current_map = destination_map
            if step.get("type") == "restore_openkore_profile":
                if step.get("restore_ai_mode") not in ("on", "manual", "off"):
                    raise ValueError("restore_openkore_profile requires an explicit restore_ai_mode")
                if not isinstance(step.get("handoff_macro"), str) or not re.fullmatch(
                        r"[a-z][a-z0-9_]*", step["handoff_macro"]):
                    raise ValueError("restore_openkore_profile requires a declared native eventMacro handoff_macro")
                if not isinstance(step.get("handoff_marker"), str) or not re.fullmatch(
                        r"[A-Za-z0-9_.:-]{1,120}", step["handoff_marker"]):
                    raise ValueError("restore_openkore_profile requires a literal handoff_marker")
            for condition in step.get("preconditions", []):
                if isinstance(condition, dict) and "step" in condition and condition["step"] not in available:
                    raise ValueError("ordered action preconditions must reference an earlier action")
            available.add(step["id"])
        if steps[-1].get("type") not in ("verify_inventory", "verify_equipment"):
            raise ValueError("ordered actions must finish with an independent inventory/equipment verification")
        if any(step.get("type") == "spawn_monster" for step in steps):
            spawn_index = next(i for i, step in enumerate(steps) if step.get("type") == "spawn_monster")
            consumers = [step for step in steps if step.get("type") in ("attack_monster", "await_native_ai")]
            matching = [step for step in consumers if step.get("spawn_step") == steps[spawn_index]["id"]]
            if len(matching) != 1 or len(consumers) != 1:
                raise ValueError("one attack_monster or await_native_ai action must reference the spawned monster step")
            if steps.index(matching[0]) <= spawn_index:
                raise ValueError("monster combat/AI observation must follow the earlier spawn_monster action")
            if matching[0].get("type") == "await_native_ai":
                if (not isinstance(matching[0].get("eventmacro_marker"), str)
                        or not matching[0].get("eventmacro_marker")
                        or not isinstance(matching[0].get("inventory_delta", {}), dict)):
                    raise ValueError("await_native_ai requires an eventMacro outcome marker and inventory_delta")
                quest_state = matching[0].get("quest_state_after_action", "hunt_complete")
                if quest_state not in ("active", "hunt_complete", "complete"):
                    raise ValueError("await_native_ai quest_state_after_action must be active, hunt_complete, or complete")
        drop_steps = [step for step in steps if step.get("type") == "collect_monster_drop"]
        if len(drop_steps) > 1:
            raise ValueError("only one collect_monster_drop action is supported in this increment")
        if drop_steps:
            drop = drop_steps[0]
            attack = next((step for step in steps if step.get("id") == drop.get("attack_step")), None)
            if (not attack or attack.get("type") != "attack_monster"
                    or steps.index(drop) <= steps.index(attack)):
                raise ValueError("collect_monster_drop must follow and reference an earlier attack_monster action")
        if scenario.get("quest_fixture"):
            quest_count = scenario["quest_fixture"].get("count", 1)
            spawns = [step for step in steps if step.get("type") == "spawn_monster"]
            if spawns and (type(quest_count) is not int or not 1 <= quest_count <= 100
                           or len(spawns) != 1
                           or type(spawns[0].get("count", 1)) is not int
                           or spawns[0].get("count", 1) != quest_count):
                raise ValueError("controlled spawn must match the quest target count (1 to 100)")
            reload_index = next((i for i, step in enumerate(steps) if step.get("type") == "reload_questdb"), None)
            first_dialogue = next((i for i, step in enumerate(steps) if step.get("type") == "npc_dialogue"), None)
            if reload_index is None or (first_dialogue is not None and reload_index > first_dialogue):
                raise ValueError("temporary quest fixtures must be reloaded before the first NPC dialogue")
        action = {"type": "sequence"}
    else:
        if "action" not in scenario:
            raise ValueError("scenario requires either action or ordered actions")
        action = scenario["action"]
        steps = [action]
    macro_steps = [step for step in steps if step.get("type") == "macro"]
    if len(macro_steps) > 1:
        raise ValueError("only one native macro action per scenario is supported in this increment")
    duration = scenario["maximum_duration_seconds"]
    if type(duration) is not int or not 1 <= duration <= 900:
        raise ValueError("maximum_duration_seconds must be between 1 and 900")
    return action, steps
