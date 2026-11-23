from __future__ import annotations

from copy import deepcopy


# This registry is shared with runner validation so documentation and executable
# support cannot drift into separate lists.
CAPABILITIES: dict[str, dict] = {
    "npc_exchange": {
        "category": "interaction",
        "required_inputs": ["npc_exchange.material", "npc_exchange.reward", "npc_exchange.npc"],
        "outputs": ["selected exchange", "client inventory delta", "NPC response"],
        "preconditions": ["declared items exist in active rAthena item DB", "material is prepared", "NPC is reachable"],
        "observations": ["OpenKore dialogue and inventory packets", "rAthena inventory after logout"],
        "limitations": ["one declared material-to-reward transaction", "no arbitrary NPC script interpretation"],
    },
    "npc_dialogue": {
        "category": "interaction",
        "required_inputs": ["npc_dialogue.npc", "npc_dialogue.choices", "actions[].choice"],
        "optional_inputs": ["actions[].driver=eventmacro", "actions[].eventmacro_trigger", "actions[].eventmacro_marker", "actions[].expected_available_choices"],
        "outputs": ["selected_choice", "available_choices", "inventory_before", "inventory_after", "branch_message_observed"],
        "preconditions": ["choice ID exists", "declared item and quest conditions are satisfiable", "NPC response matches planned choice", "eventMacro driver requires a declared automacro trigger and observed marker"],
        "observations": ["live OpenKore response menu", "server item packets", "rAthena inventory and quest rows after logout", "native eventMacro trigger and marker when delegated"],
        "limitations": ["temporary NPC supports declared item/quest branches", "arbitrary script expressions and other prompt types are unsupported"],
    },
    "move": {
        "category": "navigation",
        "required_inputs": ["initial_state.map", "actions[].target.x", "actions[].target.y"],
        "outputs": ["OpenKore observed map and position"],
        "preconditions": ["target coordinates are integer map cells", "target is reachable by OpenKore"],
        "observations": ["structured OpenKore movement state"],
        "limitations": ["single map only", "planner checks map-index membership, not cell walkability"],
    },
    "macro": {
        "category": "automation",
        "required_inputs": ["actions[].name", "actions[].commands", "actions[].target"],
        "outputs": ["started/progress/completed/interrupted/failed lifecycle", "final structured position"],
        "preconditions": ["commands pass the bounded native macro allowlist"],
        "observations": ["native macro markers", "structured OpenKore state", "logout/reconnect transition"],
        "limitations": ["not a general macro language interpreter", "only bounded declared commands"],
    },
    "equip_item": {
        "category": "equipment",
        "required_inputs": ["actions[].item", "actions[].location"],
        "outputs": ["OpenKore equip result", "equipment semantics"],
        "preconditions": ["active item DB permits the character job, level and slot", "item is available", "slot is unoccupied"],
        "observations": ["OpenKore server response", "rAthena inventory equip mask after logout"],
        "limitations": ["does not model every refine, gender, weight, map, status or script restriction"],
    },
    "unequip_item": {
        "category": "equipment",
        "required_inputs": ["actions[].item"],
        "outputs": ["OpenKore unequip result"],
        "preconditions": ["item exists in the active item DB and is equipped"],
        "observations": ["OpenKore server response", "rAthena inventory equip mask after logout"],
        "limitations": ["does not interpret item-specific scripts"],
    },
    "verify_equipment": {
        "category": "verification",
        "required_inputs": ["actions[].expected.equipment"],
        "outputs": ["persisted equipment and slot masks"],
        "preconditions": ["must be the final action"],
        "observations": ["rAthena inventory rows after OpenKore logout"],
        "limitations": ["verifies declared items only"],
    },
    "verify_inventory": {
        "category": "verification",
        "required_inputs": ["actions[].expected.inventory_delta"],
        "outputs": ["persisted inventory deltas", "optional persisted quest state"],
        "preconditions": ["must be the final action", "all checked items and quests are declared"],
        "observations": ["rAthena inventory and quest rows after OpenKore logout"],
        "limitations": ["compares declared rows and outcomes only"],
    },
    "reload_questdb": {
        "category": "quest",
        "required_inputs": ["quest_fixture"],
        "outputs": ["active temporary quest definition"],
        "preconditions": ["temporary quest ID is unused", "fixture group permits reloadquestdb"],
        "observations": ["rAthena command response"],
        "limitations": ["supports the declared QUEST_DB target subset"],
    },
    "spawn_monster": {
        "category": "combat",
        "required_inputs": ["actions[].monster", "quest_fixture.monster"],
        "outputs": ["uniquely named tagged monster", "local monster data"],
        "preconditions": ["monster exists in active mob DB", "fixture group permits monster command"],
        "observations": ["rAthena spawn acknowledgement", "OpenKore nearby actor list"],
        "limitations": ["one temporary target in the current combat quest composition"],
    },
    "set_ai_mode": {
        "category": "native_openkore_control",
        "required_inputs": ["actions[].mode"],
        "outputs": ["native OpenKore AI mode response", "inventory snapshot before mode transition"],
        "preconditions": ["active OpenKore session", "mode is on, manual, or off"],
        "observations": ["OpenKore native ai command response", "structured state available to subsequent actions"],
        "limitations": ["mode confirmation is not proof that a later gameplay objective succeeded"],
    },
    "await_inventory_delta": {
        "category": "inventory_observation",
        "required_inputs": ["actions[].source_step", "actions[].inventory_delta", "actions[].timeout_seconds"],
        "outputs": ["declared item deltas from a prior action snapshot"],
        "preconditions": ["referenced action passed and captured OpenKore inventory", "items resolve from active rAthena item data"],
        "observations": ["OpenKore stateObserver inventory before and after the referenced action"],
        "limitations": ["observes inventory only; it does not attribute a delta to a specific native subsystem without correlated logs or timing"],
    },
    "attack_monster": {
        "category": "combat",
        "required_inputs": ["actions[].spawn_step", "actions[].timeout_seconds"],
        "outputs": ["target disappearance", "post-combat character state"],
        "preconditions": ["earlier spawn_monster result", "combat semantic preflight accepts the character/target"],
        "observations": ["OpenKore native attack and structured actor list", "rAthena persisted progression after logout"],
        "limitations": ["damage prediction remains heuristic; rAthena is authoritative"],
    },
    "await_native_ai": {
        "category": "automation",
        "required_inputs": ["actions[].spawn_step", "actions[].eventmacro_marker"],
        "optional_inputs": ["actions[].inventory_delta"],
        "outputs": ["spawned actor disappearance", "native eventMacro outcome marker", "declared OpenKore inventory deltas"],
        "preconditions": ["native OpenKore AI profile is active", "an earlier controlled monster spawn was observed", "eventMacro is loaded"],
        "observations": ["OpenKore structured actor/inventory state", "native eventMacro output", "rAthena quest state after logout"],
        "limitations": ["actor disappearance alone does not establish a kill; the declared native quest trigger and independent rAthena verification are also required"],
    },
    "restore_openkore_profile": {
        "category": "automation_handoff",
        "required_inputs": ["actions[].restore_ai_mode", "actions[].handoff_macro", "actions[].handoff_marker"],
        "outputs": ["startup config values restored in the private session", "original control-file content reloaded", "native AI resumed by the next-routine eventMacro"],
        "preconditions": ["this session applied a temporary runtime profile", "eventMacro is loaded", "caller authorizes the declared handoff"],
        "observations": ["OpenKore config responses", "native control-file reload responses", "eventMacro lifecycle and marker"],
        "limitations": ["restoration is scoped to this KoreLab session; it does not edit the user's base configuration", "handoff macro is executable source and must be explicitly declared"],
    },
    "collect_monster_drop": {
        "category": "inventory",
        "required_inputs": ["actions[].attack_step", "actions[].item", "actions[].amount"],
        "outputs": ["ground drop selected by item ID", "OpenKore inventory delta"],
        "preconditions": ["referenced monster was defeated", "active mob DB declares the requested guaranteed drop",
            "character can reach the observed ground item"],
        "observations": ["OpenKore structured ground-item list", "native take command", "structured inventory packet/state"],
        "limitations": ["one item pickup per action", "only deterministic 100% drop declarations are planner-supported",
            "server pickup restrictions and inventory capacity remain authoritative"],
    },
    "unload_npc": {
        "category": "cleanup",
        "required_inputs": ["temporary NPC created by this scenario"],
        "outputs": ["NPC removed from client view"],
        "preconditions": ["this scenario owns the temporary NPC"],
        "observations": ["rAthena unload response", "OpenKore NPC list"],
        "limitations": ["only laboratory-created NPCs are tracked"],
    },
}

LEGACY_CAPABILITIES: dict[str, dict] = {
    "combat": {
        "category": "combat",
        "required_inputs": ["monster", "action.intent"],
        "outputs": ["combat result", "optional persisted EXP/skill state"],
        "preconditions": ["monster exists in active mob DB", "semantic intent is feasible or explicitly overridden"],
        "observations": ["OpenKore combat/target state", "rAthena persisted character state"],
        "limitations": ["damage and survival checks are heuristic"],
    },
    "move_to_position": {
        "category": "navigation",
        "required_inputs": ["action.target.x", "action.target.y"],
        "outputs": ["OpenKore observed position", "rAthena persisted position"],
        "preconditions": ["target is reachable on the declared map"],
        "observations": ["structured OpenKore state", "rAthena character row after logout"],
        "limitations": ["one map only"],
    },
}

PLANNING_OPERATORS: dict[str, dict] = {
    "complete_monster_hunt": {
        "category": "planning_operator",
        "required_inputs": ["identity", "start", "quest", "target", "NPC accept/turn-in transactions", "hunt map and positions"],
        "optional_inputs": ["goal.execution.combat=openkore_ai", "goal.execution.openkore_profile config keys and registered control-file overlays"],
        "outputs": ["M3-compatible ordered scenario", "dependency rationale", "semantic checks"],
        "preconditions": ["character is offline at declared start", "one exact mob record and unused quest ID exist",
            "maps/items/equipment requirements resolve in active rAthena data", "fixture group has required commands"],
        "observations": ["rAthena character/inventory/quest reads", "active item/mob/quest DBs", "map_index and job/equipment constants"],
        "limitations": ["one target, one map, one accept and one turn-in dialogue", "no map transition", "custom policies overlapping generated hunt control files are rejected",
            "combat fit is heuristic", "rAthena remains authoritative"],
    },
    "complete_npc_exchange": {
        "category": "planning_operator",
        "required_inputs": ["identity", "offline start position", "NPC map/position", "material/reward item IDs and quantities"],
        "outputs": ["M3-compatible ordered scenario", "dependency rationale", "active item/map/group checks"],
        "preconditions": ["character is offline at declared start", "items exist and are supported stackable types",
            "material is present or prepared", "NPC is on the starting map", "fixture group can load/unload its NPC"],
        "observations": ["active rAthena item DB, inventory and map index", "OpenKore NPC transaction", "rAthena persisted inventory after logout"],
        "limitations": ["one equal-quantity material-to-reward exchange", "one temporary NPC on one map",
            "does not interpret arbitrary NPC scripts or prove server policy beyond the local fixture"],
    },
    "complete_npc_dialogue_branch": {
        "category": "planning_operator",
        "required_inputs": ["identity", "offline start position", "NPC map/position", "at least two declared choices", "desired reward item and quantity", "preparation for each selected path prerequisite", "optional temporary quest fixture for declared active quest gates"],
        "outputs": ["M3-compatible ordered NPC/quest dialogue scenario", "deterministic shortest prerequisite path", "selected branch rationale", "item/map/group/quest semantic checks"],
        "preconditions": ["character is offline at declared start", "all referenced items are supported stackable records in active rAthena data", "selected path prerequisites can be prepared or granted by earlier choices", "exactly one choice grants the desired reward", "NPC is on the starting map", "fixture group permits required NPC and optional quest reload commands"],
        "observations": ["active rAthena item DB, character inventory and map index", "OpenKore observed dialogue choice and inventory", "rAthena persisted inventory after logout"],
        "limitations": ["declared branch paths support at most eight choices on one map", "temporary quest fixtures support declared start/complete effects and active prerequisites; hunt_complete requires a composed combat objective", "does not model arbitrary server script conditions or prove equivalence with remote behavior"],
    },
}

ORDERED_ACTION_TYPES = frozenset(CAPABILITIES)


def capability_catalog() -> dict:
    entries = []
    for category in (CAPABILITIES, LEGACY_CAPABILITIES):
        entries.extend({"name": name, **deepcopy(spec)} for name, spec in category.items())
    operators = [{"name": name, **deepcopy(spec)} for name, spec in PLANNING_OPERATORS.items()]
    return {"schema_version": 1, "capabilities": sorted(entries, key=lambda item: item["name"]),
            "planning_operators": operators}


def capability_for(action_type: str) -> dict | None:
    spec = (CAPABILITIES.get(action_type) or LEGACY_CAPABILITIES.get(action_type)
            or PLANNING_OPERATORS.get(action_type))
    return {"name": action_type, **deepcopy(spec)} if spec else None
