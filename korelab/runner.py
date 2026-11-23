from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
import pathlib
import re
import tempfile
import time
from datetime import datetime, timezone
import shutil
import subprocess
import signal
import secrets
import sys

from .adapters.openkore import (OpenKoreAdapter, UnexpectedCharacterDeath,
                                inventory_counts, position)
from .monsters import assess_match, find_monster, load_monster_catalog
from .items import assess_equipment, equipment_location_masks, load_item_catalog, resolve_item
from .quests import find_quest_scripts, load_quest_catalog
from .scenario import load_scenario, validate_scenario_structure
from .paths import RUNS_DIR
from .locking import try_lock
from .platform_support import set_private_permissions

COMBAT_EVENT_LABEL = "KoreLabCombat::OnMobDeath"


PERL_BUILD_KEYS = ("version", "archname", "useithreads", "ptrsize", "byteorder", "cc", "ccflags", "lddlflags")


def _acquire_run_lock(resource: str, conflict: str):
    lock_name = hashlib.sha256(resource.encode()).hexdigest()[:20]
    path = pathlib.Path(tempfile.gettempdir()) / f"korelab-{lock_name}.lock"
    lock = path.open("a+b")
    try:
        set_private_permissions(path, 0o600)
        try_lock(lock)
    except BlockingIOError as exc:
        lock.close()
        raise RuntimeError(conflict) from exc
    return lock


def _failure_classification(error: Exception) -> str:
    if isinstance(error, UnexpectedCharacterDeath):
        return "unexpected_character_death"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ValueError):
        return "precondition_or_contract_failure"
    return "execution_failure"


def preflight_perl(openkore_path: pathlib.Path,
                   xstools_overlay: pathlib.Path | None = None) -> pathlib.Path:
    """Fail closed before OpenKore can auto-rebuild its native XSTools bundle."""
    override = os.environ.get("KORELAB_PERL")
    perl = pathlib.Path(override) if override else pathlib.Path(shutil.which("perl") or "perl")
    try:
        output = subprocess.run(
            [str(perl), "-MConfig", "-e",
             'print join("\\n", map { "$_=$Config{$_}" } qw(version archname useithreads ptrsize byteorder cc ccflags lddlflags dlext))'],
            text=True, capture_output=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Perl preflight failed; OpenKore was not started") from exc
    current = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    extension_root = (xstools_overlay.expanduser().resolve() if xstools_overlay
                      else openkore_path / "src/auto/XSTools")
    marker = extension_root / ".perl-config"
    extension = current.get("dlext") or ("dll" if os.name == "nt" else
                                          "bundle" if sys.platform == "darwin" else "so")
    bundle = extension_root / f"XSTools.{extension}"
    if not bundle.is_file() and extension == "bundle":
        bundle = extension_root / "XSTools.bundle"
    if not marker.is_file() or not bundle.is_file():
        raise RuntimeError("Perl/XSTools preflight failed: built bundle or compatibility marker is missing")
    built = dict(line.split("=", 1) for line in marker.read_text().splitlines() if "=" in line)
    if any(current.get(key) != built.get(key) for key in PERL_BUILD_KEYS):
        raise RuntimeError("Perl/XSTools preflight failed: current Perl does not match the selected bundle; refusing automatic rebuild")
    if os.name == "nt" and (current.get("ptrsize") != "8"
                              or not any(token in current.get("archname", "").lower()
                                         for token in ("x64", "x86_64", "amd64"))):
        raise RuntimeError("Perl/XSTools preflight failed: native Windows support requires a 64-bit Perl build")
    return perl.resolve()


def _step_preconditions(step: dict, results: dict[str, dict],
                        inventory: dict[str, int] | None = None) -> None:
    for requirement in step.get("preconditions", []):
        if isinstance(requirement, dict) and "inventory" in requirement:
            condition = requirement["inventory"]
            name, minimum = str(condition["item"]), int(condition.get("at_least", 1))
            source = str(condition.get("source", "openkore"))
            observed = inventory.get(source, {}) if inventory else {}
            if int(observed.get(name, 0)) < minimum:
                raise RuntimeError(f"inventory precondition failed for {name}")
            continue
        if not isinstance(requirement, dict) or "step" not in requirement or "path" not in requirement:
            raise ValueError("preconditions must check inventory or reference a prior step result")
        prior = results.get(str(requirement["step"]))
        if prior is None:
            raise RuntimeError(f"precondition references an unavailable step result: {requirement['step']}")
        value: object = prior
        for part in str(requirement["path"]).split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if "equals" in requirement and value != requirement["equals"]:
            raise RuntimeError(f"step precondition failed: {requirement['step']}.{requirement['path']}")


def _verify_persisted_inventory_deltas(expected: dict[str, int], item_catalog: dict,
                                      before_rows: list[dict], after_rows: list[dict],
                                      fixtures: list[dict], inventory_amount) -> dict[str, int]:
    observed = {}
    for item_name, expected_delta in expected.items():
        item_id = int(resolve_item(item_catalog, str(item_name))["id"])
        before = inventory_amount(before_rows, item_id)
        for fixture in fixtures:
            if int(resolve_item(item_catalog, str(fixture["item"]))["id"]) == item_id:
                before += int(fixture["amount"])
        delta = inventory_amount(after_rows, item_id) - before
        if delta != expected_delta:
            item = resolve_item(item_catalog, str(item_name))
            raise RuntimeError(f"rAthena inventory delta for {item['aegisname']} was "
                               f"{delta}, expected {expected_delta}")
        observed[str(item_name)] = delta
    return observed


def _refresh_openkore_precondition_inventory(preconditions: dict[str, dict], state: dict,
                                             items: dict[str, dict]) -> None:
    counts = inventory_counts(state.get("inventory", []))
    preconditions.setdefault("openkore", {}).update({
        name: counts.get(int(item["id"]), 0) for name, item in items.items()
    })


def _resolve_verified_move_target(scenario: dict, openkore: pathlib.Path,
                                 rathena: pathlib.Path) -> dict | None:
    action = scenario.get("action")
    if not isinstance(action, dict) or action.get("type") != "move_to_position":
        return None
    target = action.get("target")
    if not isinstance(target, dict) or target.get("selection") != "nearest_verified_walkable":
        return None
    fields = pathlib.Path(scenario.get("openkore", {}).get("fields_folder") or openkore / "fields")
    origin = scenario.get("initial_state", {}).get("position", {})
    map_name = scenario.get("initial_state", {}).get("map")
    from .navigation import select_verified_destination
    evidence = select_verified_destination(map_name=map_name, origin=origin,
        openkore_fields=fields, rathena=rathena,
        max_distance=target.get("max_distance", 8))
    action["target"] = {"x": evidence["x"], "y": evidence["y"]}
    scenario.setdefault("expected", {}).update({"map": map_name,
        "position": {"x": evidence["x"], "y": evidence["y"]}})
    open_settings = scenario.setdefault("openkore", {})
    private_server = open_settings.get("server_profile")
    if isinstance(private_server, dict):
        configured_fields = private_server.get("fields_folder")
        if configured_fields and pathlib.Path(configured_fields).expanduser().resolve() != fields.expanduser().resolve():
            raise ValueError("verified movement field data differs from the OpenKore server profile fields_folder")
        private_server["fields_folder"] = str(fields.expanduser().resolve())
        # OpenKore's resnametable maps some maps (for example iz_int02) to
        # shared client assets. That alias can point at a different-sized
        # FLD2 than the actual rAthena map. A verified same-name field
        # override keeps native pathfinding on the field we cross-checked.
        overrides = private_server.setdefault("field_overrides", {})
        if not isinstance(overrides, dict):
            raise ValueError("server_profile.field_overrides must be a map-name mapping")
        overrides[map_name] = map_name
    evidence["origin"] = {"map": map_name, **origin}
    return evidence


def _apply_declared_inventory_delta(planned: dict[int, int], item_catalog: dict,
                                    inventory_delta: dict[str, int]) -> None:
    if not isinstance(inventory_delta, dict) or not inventory_delta:
        raise ValueError("native AI inventory_delta must be a non-empty item-to-integer mapping")
    for selector, delta in inventory_delta.items():
        if type(delta) is not int or not -100_000 <= delta <= 100_000:
            raise ValueError("declared inventory deltas must be bounded integers")
        item = resolve_item(item_catalog, str(selector))
        item_id = int(item["id"])
        after = planned.get(item_id, 0) + delta
        if after < 0:
            raise RuntimeError(f"declared inventory delta would make {item['aegisname']} count negative")
        planned[item_id] = after


def _random_drop_delta_bounds(monster: dict, aegis_name: str, target_count: int,
                              deterministic_delta: int) -> tuple[dict[str, int], list[int]]:
    """Bound one item's net delta from declared transactions plus active mob drops."""
    if type(target_count) is not int or target_count <= 0:
        raise ValueError("random-drop bounds require a positive controlled target count")
    matching = [int(drop.get("rate", 0)) for drop in monster.get("drops", [])
        if str(drop.get("item", "")).casefold() == aegis_name.casefold()
        and 0 < int(drop.get("rate", 0)) <= 10000]
    if not any(rate < 10000 for rate in matching):
        raise ValueError(f"rAthena mob data does not declare a random drop for ranged item {aegis_name}")
    guaranteed_per_target = sum(rate == 10000 for rate in matching)
    return ({"min": deterministic_delta + target_count * guaranteed_per_target,
             "max": deterministic_delta + target_count * len(matching)}, matching)


def _npc_source(map_name: str, x: int, y: int, npc_name: str,
                material_id: int, reward_id: int, amount: int) -> str:
    if not re.fullmatch(r"[a-z0-9_]+", map_name) or not (0 <= x <= 500 and 0 <= y <= 500):
        raise ValueError("temporary NPC map coordinates are invalid")
    if not re.fullmatch(r"KoreLabM3[A-Z0-9]{6}", npc_name):
        raise ValueError("temporary NPC name is invalid")
    if any(type(item_id) is not int or item_id <= 0 for item_id in (material_id, reward_id)):
        raise ValueError("temporary NPC item IDs are invalid")
    if type(amount) is not int or not 1 <= amount <= 100:
        raise ValueError("temporary NPC exchange amount must be from 1 to 100")
    return (f"{map_name},{x},{y},4\tscript\t{npc_name}\t4_F_TELEPORTER,{{\n"
            f"    mes \"[KoreLab Exchange]\";\n"
            f"    mes \"Trade one item for one reward?\";\n"
            f"    next;\n"
            f"    if (countitem({material_id}) < {amount}) {{ mes \"Required item is missing.\"; close; }}\n"
            f"    delitem {material_id},{amount};\n"
            f"    getitem {reward_id},{amount};\n"
            f"    mes \"Exchange complete.\";\n"
            f"    close;\n"
            f"}}\n")


def _npc_branch_source(map_name: str, x: int, y: int, npc_name: str,
                       branches: list[dict], item_catalog: dict) -> str:
    if not re.fullmatch(r"[a-z0-9_]+", map_name) or not (0 <= x <= 500 and 0 <= y <= 500):
        raise ValueError("temporary NPC map coordinates are invalid")
    if not re.fullmatch(r"KoreLabM3[A-Z0-9]{6}", npc_name) or len(branches) < 2:
        raise ValueError("branch NPC requires a valid name and at least two choices")
    lines = [f"{map_name},{x},{y},4\tscript\t{npc_name}\t4_F_TELEPORTER,{{",
             '    mes "[KoreLab Branch Lab]";']
    branch_data = []
    for index, branch in enumerate(branches):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(branch.get("id", ""))):
            raise ValueError("dialogue branch ids must use lowercase letters, digits, and underscores")
        label = str(branch.get("label", ""))
        message = str(branch.get("message", ""))
        if (not label or ":" in label or '"' in label or "\\" in label
                or not message or '"' in message or "\\" in message):
            raise ValueError("dialogue labels/messages must be non-empty and safe for rAthena select strings")
        requirements = branch.get("requires", [])
        if not requirements:
            raise ValueError("each controlled branch must declare at least one inventory prerequisite")
        normalized = []
        for required in requirements:
            item = resolve_item(item_catalog, str(required["item"]))
            amount = int(required.get("amount", 1))
            if not 1 <= amount <= 100:
                raise ValueError("branch inventory prerequisites must require 1 to 100 items")
            normalized.append((int(item["id"]), amount))
        consume = branch.get("consume")
        grant = branch.get("grant")
        if not isinstance(consume, dict) or not isinstance(grant, dict):
            raise ValueError("each dialogue branch must declare consumed and granted items")
        consumed_item = resolve_item(item_catalog, str(consume["item"]))
        granted_item = resolve_item(item_catalog, str(grant["item"]))
        consumed_amount, granted_amount = int(consume.get("amount", 1)), int(grant.get("amount", 1))
        if not 1 <= consumed_amount <= 100 or not 1 <= granted_amount <= 100:
            raise ValueError("dialogue item outcomes must use quantities from 1 to 100")
        if int(consumed_item["id"]) == int(granted_item["id"]):
            raise ValueError("dialogue branches must consume and grant different items")
        if int(consumed_item["id"]) not in [item_id for item_id, _ in normalized]:
            raise ValueError("branch prerequisites must include its consumed item")
        quest_requirements = branch.get("quest_requires", [])
        quest_check = ""
        for requirement in quest_requirements:
            quest_id = int(requirement["id"])
            quest_state = requirement.get("state")
            if quest_id <= 0 or quest_state not in ("active", "hunt_complete"):
                raise ValueError("dialogue quest prerequisites support active or hunt_complete state")
            quest_check += (f" && checkquest({quest_id}) == 1" if quest_state == "active"
                            else f" && checkquest({quest_id},HUNTING) == 2")
        quest_action = branch.get("quest")
        if quest_action:
            quest_id = int(quest_action["id"])
            if quest_id <= 0 or quest_action.get("action") not in ("start", "complete"):
                raise ValueError("dialogue quest effects must start or complete a positive quest ID")
            expected_quest_state = -1 if quest_action["action"] == "start" else 1
            quest_check += f" && checkquest({quest_id}) == {expected_quest_state}"
        branch_data.append((branch, normalized, int(consumed_item["id"]), consumed_amount,
                            int(granted_item["id"]), granted_amount, quest_check, quest_action))
        variable = f".@eligible{index}"
        condition = " && ".join(f"countitem({item_id}) >= {amount}" for item_id, amount in normalized)
        lines.append(f"    {variable} = ({condition}{quest_check});")
    lines.extend(['    .@menu$ = "";', '    .@visible = 0;'])
    for index, (branch, *_rest) in enumerate(branch_data):
        variable = f".@eligible{index}"
        lines.extend([f"    if ({variable}) {{", f'        .@menu$ += "{branch["label"]}:";',
                      "        .@visible++;", "    }"])
    lines.extend(['    if (!.@visible) { mes "No available options."; close; }',
                  '    .@choice = select(.@menu$);', '    .@visible = 0;'])
    for index, (branch, _requirements, consumed_id, consumed_amount, granted_id, granted_amount,
                _quest_check, quest_action) in enumerate(branch_data):
        variable = f".@eligible{index}"
        lines.extend([f"    if ({variable}) {{", "        .@visible++;", "        if (.@choice == .@visible) {"])
        lines.append(f"            delitem {consumed_id},{consumed_amount};")
        lines.append(f"            getitem {granted_id},{granted_amount};")
        if quest_action:
            native = "setquest" if quest_action["action"] == "start" else "completequest"
            lines.append(f"            {native} {int(quest_action['id'])};")
        lines.append(f'            mes "{branch["message"]}";')
        lines.extend(["            close;", "        }", "    }"])
    lines.extend(['    mes "Unexpected dialogue choice.";', '    close;', '}'])
    return "\n".join(lines) + "\n"


def _macro_source(name: str, commands: list[str], marker: str,
                  progress_marker: str | None = None) -> str:
    if (not re.fullmatch(r"[a-z][a-z0-9_]*", name)
            or not re.fullmatch(r"KoreLabMacro_[A-Z0-9]{8}", marker)
            or (progress_marker is not None and not re.fullmatch(r"KoreLabProgress_[A-Z0-9]{8}", progress_marker))):
        raise ValueError("macro name or completion marker is invalid")
    if not commands:
        raise ValueError("macro must declare at least one OpenKore command")
    allowed = re.compile(r"(?:do move [0-9]{1,3} [0-9]{1,3}|pause [1-9][0-9]?|log [A-Za-z0-9_.:-]{1,80})$")
    if any(not isinstance(command, str) or not allowed.fullmatch(command) for command in commands):
        raise ValueError("macro supports only bounded OpenKore movement, pause, and log commands")
    body = [f"macro {name}{{"]
    for index, command in enumerate(commands):
        body.append(f"    {command}")
        if index == 0 and progress_marker:
            body.append(f"    log {progress_marker}")
    body.extend([f"    log {marker}", "}", ""])
    return "\n".join(body)


def _quest_fixture_source(quest_id: int, title: str, monster: str, count: int) -> str:
    if type(quest_id) is not int or not 1 <= quest_id <= 2_000_000_000:
        raise ValueError("temporary quest ID must be a positive rAthena integer")
    if (not isinstance(title, str) or not title or len(title) > 80
            or any(char in title for char in '\\"\n\r')):
        raise ValueError("temporary quest title contains unsupported characters")
    if not re.fullmatch(r"[A-Z0-9_]+", monster) or type(count) is not int or not 1 <= count <= 100:
        raise ValueError("temporary quest target must be an active monster and count from 1 to 100")
    return ("Header:\n"
            "  Type: QUEST_DB\n"
            "  Version: 3\n"
            "Body:\n"
            f"  - Id: {quest_id}\n"
            f"    Title: \"{title}\"\n"
            "    Targets:\n"
            f"      - Mob: {monster}\n"
            f"        Count: {count}\n")


def run(scenario_path: pathlib.Path, openkore_path: pathlib.Path,
        rathena_path: pathlib.Path, output: pathlib.Path | None = None) -> dict:
    scenario = load_scenario(scenario_path)
    openkore_path = openkore_path.expanduser().resolve()
    rathena_path = rathena_path.expanduser().resolve()
    if scenario.get("openkore", {}).get("portal_profile_id"):
        scenario["openkore"]["portal_rathena_source"] = str(rathena_path)
    verified_move_target = _resolve_verified_move_target(scenario, openkore_path, rathena_path)
    action, steps = validate_scenario_structure(scenario)
    macro_steps = [step for step in steps if step.get("type") == "macro"]
    duration = scenario["maximum_duration_seconds"]
    perl_path = preflight_perl(openkore_path,
        pathlib.Path(scenario["openkore"]["xstools_overlay"]) if scenario.get("openkore", {}).get("xstools_overlay") else None)
    runs = output or RUNS_DIR
    runs.mkdir(parents=True, exist_ok=True)
    run_dir = runs / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir.mkdir(mode=0o700, exist_ok=False)
    if macro_steps:
        macro_step = macro_steps[0]
        marker = f"KoreLabMacro_{secrets.token_hex(4).upper()}"
        progress_marker = f"KoreLabProgress_{secrets.token_hex(4).upper()}"
        macro_text = _macro_source(str(macro_step.get("name", "")),
                                   macro_step.get("commands", []), marker, progress_marker)
        macro_path = run_dir / "openkore-macros.txt"
        macro_path.write_text(macro_text, encoding="utf-8")
        set_private_permissions(macro_path, 0o600)
        scenario.setdefault("openkore", {})["macro_source"] = str(macro_path)
        macro_step["_completion_marker"] = marker
        macro_step["_progress_marker"] = progress_marker
    from .adapters.rathena import RathenaAdapter
    rathena = RathenaAdapter(rathena_path)
    identity, initial = scenario["identity"], scenario["initial_state"]
    character = identity["character"]
    combat = action.get("type") == "combat"
    sequence = action.get("type") == "sequence"
    monster = None
    semantic = None
    item_catalog = load_item_catalog(rathena_path) if sequence else None
    npc_step = next((step for step in steps if step.get("type") == "npc_exchange"), None)
    branch_steps = [step for step in steps if step.get("type") == "npc_dialogue"]
    branch_choices: dict[str, dict] = {}
    quest_ids: tuple[int, ...] = ()
    quest_semantics: dict[int, dict] = {}
    quest_fixture = scenario.get("quest_fixture") if sequence else None
    quest_fixture_path = rathena_path / "db/import/quest_db.yml" if quest_fixture else None
    quest_fixture_text: str | None = None
    quest_import_written = False
    quest_import_dir_created = False
    combat_steps = [step for step in steps if step.get("type") in
                    ("spawn_monster", "attack_monster", "await_native_ai")]
    combat_assessment: dict | None = None
    fixture_items: dict[str, dict[str, object]] = {}
    exchange_material = exchange_reward = None
    exchange_amount = 1
    equipment_assessments: dict[str, dict[str, object]] = {}
    if sequence:
        fixture_decl = scenario.get("prepare", {}).get("inventory", {}).get("items", [])
        if not isinstance(fixture_decl, list):
            raise ValueError("prepare.inventory.items must be an array")
        for entry in fixture_decl:
            if not isinstance(entry, dict) or "item" not in entry or type(entry.get("amount")) is not int or entry["amount"] < 1:
                raise ValueError("inventory fixtures require item AegisName and positive integer amount")
            item = resolve_item(item_catalog, str(entry["item"]))
            fixture_items[str(item["aegisname"])] = item
        for step in steps:
            for property_name in ("material", "reward", "item"):
                if property_name in step:
                    item = resolve_item(item_catalog, str(step[property_name]))
                    fixture_items[str(item["aegisname"])] = item
            if step.get("type") == "verify_inventory":
                expected = step.get("expected", {})
                for aegis_name in set(expected.get("inventory_delta", {})) | set(
                        expected.get("inventory_delta_ranges", {})):
                    item = resolve_item(item_catalog, str(aegis_name))
                    fixture_items[str(item["aegisname"])] = item
        dialogue = scenario.get("npc_dialogue", {})
        if branch_steps:
            choices = dialogue.get("choices", [])
            if not isinstance(choices, list) or len(choices) < 2:
                raise ValueError("npc_dialogue.choices must declare at least two branches")
            for choice in choices:
                choice_id = str(choice.get("id", ""))
                if not re.fullmatch(r"[a-z][a-z0-9_]*", choice_id) or choice_id in branch_choices:
                    raise ValueError("NPC choice IDs must be unique lowercase identifiers")
                normalized = {"id": choice_id, "label": str(choice.get("label", "")),
                              "requires": [], "message": str(choice.get("message", ""))}
                if choice.get("quest"):
                    normalized["quest"] = dict(choice["quest"])
                normalized["quest_requires"] = [dict(req) for req in choice.get("quest_requires", [])]
                for requirement in choice.get("requires", []):
                    item = resolve_item(item_catalog, str(requirement["item"]))
                    fixture_items[str(item["aegisname"])] = item
                    normalized["requires"].append({"item_id": int(item["id"]),
                        "amount": int(requirement.get("amount", 1)),
                        "aegisname": str(item["aegisname"])})
                for property_name in ("consume", "grant"):
                    item = resolve_item(item_catalog, str(choice[property_name]["item"]))
                    fixture_items[str(item["aegisname"])] = item
                    normalized[property_name] = {"item_id": int(item["id"]),
                        "amount": int(choice[property_name].get("amount", 1)),
                        "aegisname": str(item["aegisname"]), "name": str(item["name"])}
                if not normalized["requires"] or not normalized["message"]:
                    raise ValueError("each NPC choice needs prerequisites and an outcome message")
                if normalized["consume"]["item_id"] not in [r["item_id"] for r in normalized["requires"]]:
                    raise ValueError("choice prerequisites must include its consumed item")
                branch_choices[choice_id] = normalized
            for step in branch_steps:
                if str(step.get("choice", "")) not in branch_choices:
                    raise ValueError("NPC dialogue actions must reference a declared choice id")
            quest_ids = tuple(sorted({int(quest_ref["id"]) for choice in dialogue["choices"]
                for quest_ref in ([choice["quest"]] if choice.get("quest") else []) + choice.get("quest_requires", [])}))
            if quest_ids:
                quest_catalog = load_quest_catalog(rathena_path)
                quest_semantics = {int(row["id"]): row for row in quest_catalog["quests"]}
                if quest_fixture:
                    quest_id = int(quest_fixture["id"])
                    if quest_id in quest_semantics:
                        raise ValueError(f"temporary quest ID {quest_id} already exists in active rAthena data")
                    mob_catalog = load_monster_catalog(rathena_path)
                    mob = find_monster(mob_catalog, str(quest_fixture["monster"]))
                    quest_fixture_text = _quest_fixture_source(
                        quest_id, str(quest_fixture.get("title", "KoreLab controlled hunt")),
                        str(mob["aegis_name"]), int(quest_fixture.get("count", 1)))
                    quest_semantics[quest_id] = {"id": quest_id,
                        "title": str(quest_fixture.get("title", "KoreLab controlled hunt")),
                        "targets": [{"mob": str(mob["aegis_name"]),
                                     "count": int(quest_fixture.get("count", 1))}],
                        "drops": [], "source": "temporary KoreLab import", "temporary": True}
                    if quest_fixture_path.exists():
                        raise RuntimeError("refusing to replace an existing rAthena quest import")
                for quest_id in quest_ids:
                    if quest_id not in quest_semantics:
                        raise ValueError(f"quest {quest_id} is not present in the active rAthena quest database")
        if combat_steps:
            if not quest_fixture or not branch_steps:
                raise ValueError("ordered combat quest steps require a declared temporary quest and NPC dialogue")
            mob_catalog = load_monster_catalog(rathena_path)
            combat_mob = find_monster(mob_catalog, str(quest_fixture["monster"]))
            target_count = int(quest_fixture.get("count", 1))
            spawn_steps = [step for step in combat_steps if step.get("type") == "spawn_monster"]
            if (not 1 <= target_count <= 100 or len(spawn_steps) != 1
                    or int(spawn_steps[0].get("count", 1)) != target_count):
                raise ValueError("one controlled spawn must match the declared quest target count (1 to 100)")
        if npc_step:
            exchange_material = resolve_item(item_catalog, str(npc_step["material"]))
            exchange_reward = resolve_item(item_catalog, str(npc_step["reward"]))
            exchange_amount = int(npc_step.get("amount", 1))
            if not 1 <= exchange_amount <= 100:
                raise ValueError("NPC exchange amount must be from 1 to 100")
            declared_exchange = npc_step.get("expected", {})
            if (int(declared_exchange.get("material_delta", -exchange_amount)) != -exchange_amount
                    or int(declared_exchange.get("reward_delta", exchange_amount)) != exchange_amount):
                raise ValueError("NPC exchange expected item deltas must match its declared quantity")
            if exchange_material.get("type") not in ("Etc", "Healing", "Usable"):
                raise ValueError("NPC exchange material must be a consumable inventory item according to active rAthena data")
            if exchange_reward.get("type") not in ("Etc", "Healing", "Usable"):
                raise ValueError("NPC exchange reward must be a stackable inventory item according to active rAthena data")
            if exchange_material["id"] == exchange_reward["id"]:
                raise ValueError("NPC exchange material and reward must be distinct items")
            if str(exchange_material["aegisname"]) not in fixture_items:
                raise ValueError("scenario must prepare the NPC exchange material in inventory")
    ranged_drop_contracts: dict[str, dict] = {}
    if sequence:
        final_verification = next((step for step in steps if step.get("type") == "verify_inventory"), {})
        expected_ranges = final_verification.get("expected", {}).get("inventory_delta_ranges", {})
        if expected_ranges:
            if not quest_fixture or not any(step.get("type") == "await_native_ai" for step in steps):
                raise ValueError("random-drop inventory ranges require a temporary quest and native OpenKore AI observation")
            if not combat_steps:
                raise ValueError("random-drop inventory ranges require a declared controlled combat target")
            target_count = int(quest_fixture.get("count", 1))
            mob = find_monster(load_monster_catalog(rathena_path), str(quest_fixture["monster"]))
            deterministic_delta: dict[str, int] = defaultdict(int)
            for step in steps:
                if step.get("type") == "npc_dialogue":
                    choice = branch_choices[str(step["choice"])]
                    deterministic_delta[str(choice["consume"]["aegisname"])] -= int(choice["consume"]["amount"])
                    deterministic_delta[str(choice["grant"]["aegisname"])] += int(choice["grant"]["amount"])
                elif step.get("type") == "npc_exchange":
                    deterministic_delta[str(exchange_material["aegisname"])] -= exchange_amount
                    deterministic_delta[str(exchange_reward["aegisname"])] += exchange_amount
                elif step.get("type") == "collect_monster_drop":
                    item = resolve_item(item_catalog, str(step["item"]))
                    deterministic_delta[str(item["aegisname"])] += int(step.get("amount", 1))
            exact_expected = final_verification.get("expected", {}).get("inventory_delta", {})
            for step in steps:
                if step.get("type") == "await_native_ai":
                    for item_name, delta in step.get("inventory_delta", {}).items():
                        if item_name in expected_ranges:
                            raise ValueError(f"{item_name} cannot be both an eventMacro exact delta and a random-drop range")
            for selector, declared in expected_ranges.items():
                item = resolve_item(item_catalog, str(selector))
                aegis_name = str(item["aegisname"])
                if any(str(resolve_item(item_catalog, str(name))["aegisname"]) == aegis_name
                       for name in exact_expected):
                    raise ValueError(f"{aegis_name} cannot have both exact and ranged inventory expectations")
                base_delta = int(deterministic_delta.get(aegis_name, 0))
                actual_bounds, rates = _random_drop_delta_bounds(mob, aegis_name, target_count, base_delta)
                if declared != actual_bounds:
                    raise ValueError(f"declared delta range for {aegis_name} does not match active rAthena transaction/drop bounds: "
                                     f"expected min={actual_bounds['min']}, max={actual_bounds['max']}; got {declared}")
                ranged_drop_contracts[aegis_name] = {
                    "item_id": int(item["id"]), "monster": mob["aegis_name"],
                    "target_count": target_count,
                    "rates": rates,
                    "deterministic_transaction_delta": base_delta,
                    "verified_delta_bounds": actual_bounds,
                    "source": "active rAthena mob drop database and declared quest transactions"}
    if combat:
        catalog = load_monster_catalog(rathena_path)
        monster = find_monster(catalog, scenario["monster"])
        # Resolve actual character data below, before mutating either the player or account.
    report = {"scenario": scenario["name"], "status": "FAIL", "assertions": {},
              "run_directory": str(run_dir), "errors": []}
    if verified_move_target:
        report["verified_movement_target"] = verified_move_target
    if scenario.get("planning"):
        report["planning"] = scenario["planning"]
    if sequence:
        if ranged_drop_contracts:
            report["rathena_random_drop_bounds"] = ranged_drop_contracts
        report["active_rathena_items"] = [{key: item.get(key) for key in (
            "id", "aegisname", "name", "type", "weight", "source")}
            for item in fixture_items.values()]
        if quest_semantics:
            report["active_rathena_quests"] = [quest_semantics[quest_id] for quest_id in quest_ids]
            report["related_rathena_scripts"] = find_quest_scripts(rathena_path, quest_ids)
    adapter = None
    original = None
    prepared_attributes = {}
    character_state_restore_required = False
    inventory_restore_required = False
    requested_attributes = scenario.get("prepare", {}).get("attributes", {})
    transient_attributes = scenario.get("prepare", {}).get("transient_attributes", {})
    if not isinstance(requested_attributes, dict) or not isinstance(transient_attributes, dict):
        raise ValueError("prepare.attributes and prepare.transient_attributes must be objects")
    fixture_attributes = {**requested_attributes, **transient_attributes}
    requested_skills = scenario.get("prepare", {}).get("skills", {})
    inventory_fixture = scenario.get("prepare", {}).get("inventory", {}).get("items", [])
    inventory_ids = tuple(sorted({int(item["id"]) for item in fixture_items.values()})) if sequence else ()
    original_inventory_rows: list[dict[str, int]] | None = None
    original_inventory_all_rows: list[dict[str, int]] | None = None
    original_quest_rows: list[dict[str, int]] | None = None
    quest_fixture_started = False
    inventory_fixture_started = False
    temporary_npc_path: pathlib.Path | None = None
    temporary_npc_name: str | None = None
    npc_loaded = False
    step_results: dict[str, dict] = {}
    scenario_quest_states: dict[int, str] = {}
    skill_fixture_levels = ({str(action["skill_id"]): int(action.get("skill_level", 1))}
                            if combat and action.get("intent") in ("skill", "resistance") else {})
    prerequisite_skills: dict[str, int] = {}
    original_skill_rows = {}
    account_group = None
    temporary_group = int(scenario.get("server_setup", {}).get("temporary_group_id", 0))
    group_changed = False
    monster_spawned = False
    events_to_clean: dict[str, list[str]] = {}
    control_monster_name = None
    active_step: dict | None = None
    lock = None
    map_locks = []
    event_map_by_name: dict[str, str] = {}
    active_map = str(initial["map"])
    start_time = time.monotonic()
    previous_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def interrupt(signum, frame):
        raise RuntimeError(f"scenario interrupted by signal {signum}; running cleanup")

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        lock = _acquire_run_lock(f"{openkore_path}:{character}",
                                 "another KoreLab run is using this character")
        report["services"] = rathena.require_services()
        if combat or combat_steps:
            maps = {str(initial["map"])}
            maps.update(str(edge["to"]) for edge in scenario.get("openkore", {}).get("map_transitions", []))
            for map_name in sorted(maps):
                map_key = f"{rathena_path}:map:{map_name}"
                map_locks.append(_acquire_run_lock(
                    map_key, f"another KoreLab run is using combat map {map_name}"))
        original = rathena.character(character)
        # Every live session may change persistent character or inventory state,
        # including through server-side effects such as death. Serialize the
        # whole account before snapshotting, not only scenarios with declared DB fixtures.
        account_key = f"{openkore_path}:account:{original['account_id']}"
        account_lock = _acquire_run_lock(account_key,
                                         "another KoreLab run is using this account")
        lock.close()
        lock = account_lock
        if rathena.online_characters_for_account(int(original["account_id"])):
            raise RuntimeError("scenario requires every character on the selected account to be offline")
        original_skill_rows = rathena.character_skill_rows(original)
        if combat:
            account_group = rathena.account_group(int(original["account_id"]))
            intent = action.get("intent", "victory")
            needed_commands = ["monster", "killmonster2"]
            if intent in ("skill", "resistance"):
                needed_commands.append("heal")
            if intent == "defeat":
                needed_commands.append("alive")
            if temporary_group < 1 or not rathena.group_allows(temporary_group, tuple(needed_commands)):
                raise RuntimeError("combat setup requires a temporary rAthena group with the required fixture commands")
            original["skills"] = rathena.character_skills(original)
            original["skills"].update(skill_fixture_levels)
            semantic = assess_match(
                {**original, **fixture_attributes, "attack": int(fixture_attributes.get("str", original.get("str", 1)))}, monster,
                intent,
                allow_extreme=bool(action.get("allow_extreme", False)),
                reason=str(action.get("extreme_reason", "")),
                duration_seconds=float(action.get("duration_seconds", 30)),
                skill_id=str(action["skill_id"]) if "skill_id" in action else None,
                skill_level=int(action.get("skill_level", 1)),
            )
        if sequence:
            required = set()
            if npc_step or branch_steps:
                required.update(("loadnpc", "unloadnpc"))
            if quest_fixture:
                required.add("reloadquestdb")
            if combat_steps:
                required.update(("monster", "killmonster2"))
            account_group = rathena.account_group(int(original["account_id"]))
            if required and (temporary_group < 1 or not rathena.group_allows(temporary_group, tuple(sorted(required)))):
                raise RuntimeError(f"ordered scenario requires a temporary rAthena group with commands {sorted(required)}")
            if quest_fixture and combat_steps:
                target_mob = find_monster(load_monster_catalog(rathena_path), str(quest_fixture["monster"]))
                combat_assessment = assess_match(
                    {**original, **fixture_attributes,
                     "attack": int(fixture_attributes.get("str", original.get("str", 1)))},
                    target_mob, "victory", duration_seconds=float(scenario.get("maximum_duration_seconds", 60)))
                report["quest_combat_semantics"] = {
                    "quest_id": int(quest_fixture["id"]),
                    "target": {key: target_mob.get(key) for key in (
                        "id", "aegis_name", "name", "level", "hp", "attack", "defense",
                        "race", "size", "element", "element_level", "behavior")},
                    "assessment": combat_assessment,
                    "server_authoritative": True}
            existing_equipment = rathena.equipped_inventory_rows(original)
            local_slot_masks = equipment_location_masks(rathena_path)
            for step in steps:
                if step.get("type") != "equip_item":
                    continue
                item = resolve_item(item_catalog, str(step["item"]))
                assessment = assess_equipment(item, int(original["class"]), int(original["base_level"]),
                                              step.get("location"), rathena_path)
                expected = bool(step.get("expect_success", True))
                if assessment["can_equip"] != expected:
                    raise ValueError(f"equipment expectation for {item['aegisname']} conflicts with local rAthena data: {assessment['reasons']}")
                requested_mask = local_slot_masks.get(str(step.get("location")))
                if assessment["can_equip"] and requested_mask is not None:
                    conflicts = [row for row in existing_equipment
                                 if int(row["nameid"]) != int(item["id"])
                                 and int(row["equip"]) & requested_mask]
                    if conflicts:
                        raise RuntimeError(f"equipment slot {step['location']} is occupied; refusing to replace an undeclared item")
                equipment_assessments[str(item["aegisname"])] = assessment
        if original["online"] != 0:
            raise RuntimeError("scenario requires the selected character to be offline")
        if original["map"] != initial["map"] or original["x"] != initial["position"]["x"] or original["y"] != initial["position"]["y"]:
            raise RuntimeError(f"character is at {original['map']} ({original['x']},{original['y']}), "
                               f"expected declared start {initial['map']} {initial['position']}")
        report["initial_character_state"] = {k: original[k] for k in (
            "map", "x", "y", "online", "hp", "max_hp", "sp", "max_sp",
            "base_exp", "job_exp", "zeny")}
        original_inventory_all_rows = rathena.all_inventory_rows(original)
        if sequence:
            original_inventory_rows = [row for row in original_inventory_all_rows
                                       if int(row["nameid"]) in inventory_ids]
            if quest_ids:
                original_quest_rows = rathena.quest_rows(original, quest_ids)
                original_quest_map = {int(row["quest_id"]): int(row["state"]) for row in original_quest_rows}
                planned_quests = set(quest_id for quest_id, state in original_quest_map.items() if state == 0)
                for step in branch_steps:
                    choice = branch_choices[str(step["choice"])]
                    for requirement in choice.get("quest_requires", []):
                        if requirement.get("state") in ("active", "hunt_complete") and int(requirement["id"]) not in planned_quests:
                            raise RuntimeError(f"quest preflight found required quest {requirement['id']} is not active")
                    effect = choice.get("quest")
                    if effect and effect["action"] == "start":
                        if int(effect["id"]) in original_quest_map:
                            raise RuntimeError(f"quest {effect['id']} already has a saved state; refusing to restart it")
                        planned_quests.add(int(effect["id"]))
                    elif effect and effect["action"] == "complete":
                        if int(effect["id"]) not in planned_quests:
                            raise RuntimeError(f"quest {effect['id']} cannot be completed before an active start step")
                        planned_quests.remove(int(effect["id"]))
            if npc_step:
                material_prepared_count = rathena.inventory_amount(
                    original_inventory_rows, int(exchange_material["id"]))
                material_prepared_count += sum(int(entry["amount"]) for entry in inventory_fixture
                    if int(resolve_item(item_catalog, str(entry["item"]))["id"]) == int(exchange_material["id"]))
                if material_prepared_count < exchange_amount:
                    raise RuntimeError("rAthena inventory preflight found fewer materials than the NPC requires")
            if branch_steps:
                planned = {int(item["id"]): rathena.inventory_amount(original_inventory_rows, int(item["id"]))
                           for item in fixture_items.values()}
                for entry in inventory_fixture:
                    prepared_item = resolve_item(item_catalog, str(entry["item"]))
                    item_id = int(prepared_item["id"])
                    planned[item_id] = planned.get(item_id, 0) + int(entry["amount"])
                for step in steps:
                    if step.get("type") == "collect_monster_drop":
                        if not quest_fixture:
                            raise RuntimeError("planned monster drops require an active quest-monster fixture")
                        item = resolve_item(item_catalog, str(step["item"]))
                        amount = int(step.get("amount", 1))
                        mob = find_monster(load_monster_catalog(rathena_path), str(quest_fixture["monster"]))
                        guaranteed = sum(1 for drop in mob.get("drops", [])
                            if str(drop.get("item", "")).casefold() == str(item["aegisname"]).casefold()
                            and int(drop.get("rate", 0)) >= 10000)
                        if amount != 1 or guaranteed < amount:
                            raise RuntimeError("rAthena preflight found no supported guaranteed drop for the declared collection")
                        item_id = int(item["id"])
                        planned[item_id] = planned.get(item_id, 0) + amount
                    elif step.get("type") == "await_native_ai":
                        declared_delta = step.get("inventory_delta", {})
                        if declared_delta:
                            _apply_declared_inventory_delta(planned, item_catalog, declared_delta)
                    elif step.get("type") == "npc_dialogue":
                        choice = branch_choices[str(step["choice"])]
                        for requirement in choice["requires"]:
                            item_id = int(requirement["item_id"])
                            if planned.get(item_id, 0) < int(requirement["amount"]):
                                raise RuntimeError(f"rAthena preflight found an unavailable NPC branch prerequisite: {requirement['aegisname']}")
                        consume, grant = choice["consume"], choice["grant"]
                        planned[int(consume["item_id"])] -= int(consume["amount"])
                        planned[int(grant["item_id"])] = planned.get(int(grant["item_id"]), 0) + int(grant["amount"])
            report["rathena_inventory_before"] = {
                str(item["id"]): rathena.inventory_amount(original_inventory_rows, int(item["id"]))
                for item in fixture_items.values()}
        if requested_attributes or transient_attributes:
            if not fixture_attributes:
                raise ValueError("prepare.attributes must be a non-empty object")
            prepared_attributes = dict(fixture_attributes)
            character_state_restore_required = True
            rathena.update_character_fields(original, prepared_attributes,
                                            {key: int(original[key]) for key in prepared_attributes})
            report["prepared_character_attributes"] = dict(requested_attributes)
            if transient_attributes:
                report["transient_character_attribute_fixture"] = {
                    "initial_values": dict(transient_attributes),
                    "verification": "runtime effects may change these values; exact snapshot restoration is required"}
        if combat:
            character_state_restore_required = True
            setup = scenario.get("server_setup", {})
            if action.get("intent") in ("skill", "resistance") and "job_class_id" in setup:
                prepared_class = rathena.prepare_combat_character(
                    original, int(setup["job_class_id"]), int(setup.get("skill_points", 0)))
                original_skill_rows = rathena.character_skill_rows(original)
                prerequisite_skills = {str(k): int(v) for k, v in
                                       setup.get("prerequisite_skills", {}).items()}
                for skill_id, level in prerequisite_skills.items():
                    rathena.set_character_skill(original, skill_id, level, flag=0)
                report["prepared_job_class"] = {
                    "rathena_job_id": prepared_class["class"],
                    "temporary_skill_points": prepared_class["skill_point"],
                    "evidence": "guarded offline fixture, independently re-read from rAthena"}
                if prerequisite_skills:
                    report["prepared_prerequisite_skills"] = prerequisite_skills
            elif action.get("intent") in ("skill", "resistance") and "job_class" in setup:
                raise ValueError("skill setup requires numeric server_setup.job_class_id")
            # Group 4 is the local Event Manager role in rAthena's native group DB.
            group_changed = account_group != temporary_group
            if group_changed:
                rathena.set_account_group(int(original["account_id"]), int(account_group), temporary_group)
        if sequence and inventory_fixture:
            for entry in inventory_fixture:
                item = resolve_item(item_catalog, str(entry["item"]))
                inventory_fixture_started = True
                inventory_restore_required = True
                rathena.prepare_inventory_item(original, int(item["id"]), int(entry["amount"]))
                report.setdefault("prepared_inventory", {})[str(item["id"])] = {
                    "aegis_name": item["aegisname"], "added_amount": int(entry["amount"]),
                    "rathena_verified": True}
        if sequence and (npc_step or branch_steps):
            group_changed = account_group != temporary_group
            if group_changed:
                rathena.set_account_group(int(original["account_id"]), int(account_group), temporary_group)
            npc_config = (npc_step or dialogue).get("npc", {})
            temporary_npc_name = npc_config.get("name")
            if temporary_npc_name is None:
                suffix = secrets.token_hex(3).upper()
                temporary_npc_name = f"KoreLabM3{suffix}"
            else:
                match = re.fullmatch(r"KoreLabM3([A-Z0-9]{6})", str(temporary_npc_name))
                if not match:
                    raise ValueError("declared temporary NPC name must use KoreLabM3 plus six uppercase letters or digits")
                suffix = match.group(1)
            relative_file = f"npc/custom/korelab_m3_{suffix.lower()}.txt"
            temporary_npc_path = rathena_path / relative_file
            npc_position = npc_config.get("position", {})
            npc_map = str(npc_config.get("map", initial["map"]))
            npc_x = int(npc_position.get("x", initial["position"]["x"] + 1))
            npc_y = int(npc_position.get("y", initial["position"]["y"]))
            if branch_steps:
                temporary_npc_path.write_text(_npc_branch_source(npc_map, npc_x, npc_y,
                    temporary_npc_name, dialogue.get("choices", []), item_catalog), encoding="utf-8")
            else:
                temporary_npc_path.write_text(_npc_source(npc_map, npc_x, npc_y, temporary_npc_name,
                    int(exchange_material["id"]), int(exchange_reward["id"]), exchange_amount), encoding="utf-8")
            set_private_permissions(temporary_npc_path, 0o600)
        if sequence and quest_fixture:
            if quest_fixture_path is None or quest_fixture_text is None or quest_fixture_path.exists():
                raise RuntimeError("temporary quest fixture path is unavailable or already exists")
            if not quest_fixture_path.parent.exists():
                quest_fixture_path.parent.mkdir(mode=0o755, parents=True)
                quest_import_dir_created = True
            quest_fixture_path.write_text(quest_fixture_text, encoding="utf-8")
            set_private_permissions(quest_fixture_path, 0o600)
            quest_import_written = True
            report["temporary_quest_import"] = {
                "quest_id": int(quest_fixture["id"]), "path": str(quest_fixture_path),
                "source_created": True, "preexisting_file_preserved": True}
        prepared_character = rathena.character(character)
        if (prepared_character["online"] != 0
                or prepared_character["char_id"] != original["char_id"]
                or prepared_character["account_id"] != original["account_id"]):
            raise RuntimeError("prepared character identity or offline state changed before OpenKore startup")
        report["prepared_character_state"] = {key: prepared_character[key] for key in (
            "map", "x", "y", "online", "hp", "max_hp", "sp", "max_sp",
            "base_exp", "job_exp", "zeny")}
        adapter = OpenKoreAdapter(openkore_path, scenario, run_dir, start_time + duration, perl_path)
        # Mark restoration necessary before start: the client may authenticate,
        # observe a death, and then fail before start() returns.
        character_state_restore_required = True
        inventory_restore_required = True
        startup_sample_count = len(adapter._observer_rows())
        adapter.start()
        report["openkore_runtime_profile"] = adapter.runtime_profile_report
        state = adapter.state_after(startup_sample_count, lambda row:
            row.get("type") == "state" and row.get("connected_in_game") is True
            and row.get("map") == initial["map"] and position(row, False) is not None,
            timeout=min(30, duration))
        if sequence:
            # Fixtures were written while the account was offline and before
            # OpenKore started. Wait for the authoritative inventory packet from
            # this session instead of immediately disconnecting and relogging.
            expected_client_inventory = {int(item["id"]):
                rathena.inventory_amount(original_inventory_rows or [], int(item["id"]))
                for item in fixture_items.values()}
            for entry in inventory_fixture:
                prepared_item = resolve_item(item_catalog, str(entry["item"]))
                item_id = int(prepared_item["id"])
                expected_client_inventory[item_id] = expected_client_inventory.get(item_id, 0) + int(entry["amount"])
            state = adapter.wait_for_initial_inventory(int(state["session_generation"]), lambda row:
                row.get("connected_in_game") is True
                and row.get("inventory_observed") is True and all(
                inventory_counts(row.get("inventory", [])).get(item_id, 0) >= amount
                for item_id, amount in expected_client_inventory.items()), timeout=min(20, duration))
            report["openkore_fixture_refresh"] = "initial session inventory packet observed; no relog"
        if position(state, False) != initial["position"]:
            raise RuntimeError(f"OpenKore observed unexpected start position: {position(state, False)}")
        observed_attributes = state.get("character_attributes", {})
        if any(observed_attributes.get(key) != value for key, value in requested_attributes.items()):
            raise RuntimeError("OpenKore did not observe the declared prepared character attributes")
        observed_skills = state.get("character_skills", {})
        if any(int(observed_skills.get(str(skill_id), 0)) != int(level)
               for skill_id, level in requested_skills.items()):
            raise RuntimeError("OpenKore did not observe the declared prepared character skills")
        report["assertions"].update({"login_and_character_selection": "PASS",
                                     "structured_start_observation": "PASS"})
        if requested_attributes:
            report["openkore_observed_attributes"] = {
                key: observed_attributes[key] for key in requested_attributes
            }
        if requested_skills:
            report["openkore_observed_skills"] = {
                str(skill_id): int(observed_skills[str(skill_id)]) for skill_id in requested_skills
            }
        if combat and action.get("intent") in ("skill", "resistance"):
            expected_class = int(scenario["server_setup"]["job_class_id"])
            if int(state.get("character_attributes", {}).get("class", -1)) != expected_class:
                raise RuntimeError("OpenKore did not observe the prepared rAthena job class")
            learned = adapter.learn_skill(str(action["skill_id"]), int(action.get("skill_level", 1)))
            observed_learned = int(learned.get("character_skills", {}).get(str(action["skill_id"]), 0))
            if observed_learned < int(action.get("skill_level", 1)):
                raise RuntimeError("OpenKore did not observe the rAthena-validated learned skill")
            recovered = adapter.heal_test_character()
            report["server_prepared_combat_resources"] = {
                "hp": recovered.get("character_attributes", {}).get("hp"),
                "sp": recovered.get("character_attributes", {}).get("sp"),
                "evidence": "rAthena native @heal observed by OpenKore"}
            report["prepared_character_skills"] = {
                str(action["skill_id"]): observed_learned}
            report["openkore_observed_skills"] = {
                str(action["skill_id"]): observed_learned}
        if sequence:
            report["ordered_actions"] = []
            if equipment_assessments:
                report["equipment_semantics"] = equipment_assessments
            client_inventory = inventory_counts(state.get("inventory", []))
            server_inventory = {name: rathena.inventory_amount(original_inventory_rows, int(item["id"]))
                                for name, item in fixture_items.items()}
            for entry in inventory_fixture:
                item = resolve_item(item_catalog, str(entry["item"]))
                name = str(item["aegisname"])
                server_inventory[name] = server_inventory.get(name, 0) + int(entry["amount"])
            precondition_inventory = {"openkore": {
                name: client_inventory.get(int(item["id"]), 0)
                for name, item in fixture_items.items()}, "rathena": server_inventory}
            for index, step in enumerate(steps):
                active_step = step
                _step_preconditions(step, step_results, precondition_inventory)
                step_id, step_type = str(step["id"]), str(step["type"])
                if step_type == "npc_exchange":
                    if index != 0:
                        raise ValueError("npc_exchange must be the first action in this increment")
                    npc_loaded = True
                    result = adapter.interact_with_npc(temporary_npc_name, relative_file,
                        int(exchange_material["id"]), int(exchange_reward["id"]),
                        str(exchange_material["name"]), str(exchange_reward["name"]),
                        float(step.get("timeout_seconds", 30)), exchange_amount,
                        str(step.get("sequence", "x c")))
                    declared = step.get("expected", {})
                    result["expected_inventory_delta"] = {
                        str(exchange_material["aegisname"]): int(declared.get("material_delta", -exchange_amount)),
                        str(exchange_reward["aegisname"]): int(declared.get("reward_delta", exchange_amount))}
                    result.update({"id": step_id, "status": "PASS", "type": step_type})
                elif step_type == "npc_dialogue":
                    eventmacro_driver = step.get("driver", "runner") == "eventmacro"
                    if eventmacro_driver:
                        before_state = adapter.state(lambda row: row.get("connected_in_game") is True,
                            timeout=float(step.get("timeout_seconds", 30)))
                        before_counts = inventory_counts(before_state.get("inventory", []))
                    if not npc_loaded:
                        observer_sample_count = len(adapter._observer_rows())
                        npc_loaded = True
                        adapter.load_npc(temporary_npc_name, relative_file,
                                         float(step.get("timeout_seconds", 30)))
                    else:
                        observer_sample_count = len(adapter._observer_rows())
                    choice_id = str(step["choice"])
                    choice = branch_choices[choice_id]
                    if choice.get("quest"):
                        quest_fixture_started = True
                    all_choices = [branch_choices[key] for key in branch_choices]
                    names = {int(item["id"]): str(item["name"]) for item in fixture_items.values()}
                    try:
                        if eventmacro_driver:
                            consume_id = int(choice["consume"]["item_id"])
                            grant_id = int(choice["grant"]["item_id"])
                            before = {consume_id: before_counts.get(consume_id, 0),
                                      grant_id: before_counts.get(grant_id, 0)}
                            delta = {consume_id: -int(choice["consume"]["amount"]),
                                     grant_id: int(choice["grant"]["amount"])}
                            result = adapter.wait_eventmacro_dialogue(
                                npc_name=temporary_npc_name,
                                trigger=str(step["eventmacro_trigger"]),
                                marker=str(step["eventmacro_marker"]),
                                expected_choices=step["expected_available_choices"],
                                inventory_before=before, inventory_delta=delta,
                                observer_sample_count=observer_sample_count,
                                branch_message=str(choice["message"]),
                                timeout=float(step.get("timeout_seconds", 30)))
                            result.update({"id": choice_id, "type": "npc_dialogue",
                                           "selected_choice": choice["label"]})
                        else:
                            result = adapter.interact_with_branch(temporary_npc_name, choice_id,
                                str(choice["label"]), all_choices, names, choice["consume"],
                                choice["grant"], str(choice["message"]),
                                float(step.get("timeout_seconds", 30)), scenario_quest_states,
                                step.get("expected_available_choices"))
                    except Exception as exc:
                        classification = ("unexpected_dialogue_state" if "unexpected NPC dialogue choices" in str(exc)
                                          else "eventmacro_branch_failure" if step.get("driver") == "eventmacro"
                                          else "npc_interaction_failure")
                        failure = {"id": step_id, "type": step_type, "status": "FAIL",
                                   "failure_classification": classification,
                                   "error": f"{type(exc).__name__}: {exc}",
                                   "evidence": "OpenKore live dialogue observer and pre-selection failure"}
                        report["ordered_actions"].append(failure)
                        step_results[step_id] = failure
                        raise RuntimeError(f"{classification}: {exc}") from exc
                    if choice.get("quest"):
                        quest_effect = choice["quest"]
                        quest_state = "active" if quest_effect["action"] == "start" else "complete"
                        scenario_quest_states[int(quest_effect["id"])] = quest_state
                    _refresh_openkore_precondition_inventory(precondition_inventory,
                        adapter.state(lambda row: row.get("connected_in_game") is True, timeout=5),
                        fixture_items)
                elif step_type == "reload_questdb":
                    if not quest_import_written:
                        raise RuntimeError("quest DB reload has no temporary KoreLab quest import")
                    adapter.reload_quest_database(float(step.get("timeout_seconds", 15)))
                    result = {"id": step_id, "type": step_type, "status": "PASS",
                              "quest_id": int(quest_fixture["id"]),
                              "evidence": "rAthena native reloadquestdb acknowledgement after KoreLab import creation"}
                elif step_type == "spawn_monster":
                    if not quest_fixture:
                        raise RuntimeError("combat spawn requires the declared active quest fixture")
                    target_selector = str(step.get("monster", quest_fixture["monster"]))
                    target_mob = find_monster(load_monster_catalog(rathena_path), target_selector)
                    if str(target_mob["aegis_name"]).casefold() != str(quest_fixture["monster"]).casefold():
                        raise RuntimeError("spawned monster does not match the active quest target")
                    test_monster_name = "KoreLabQ" + datetime.now(timezone.utc).strftime("%H%M%S")
                    events_to_clean.setdefault(COMBAT_EVENT_LABEL, []).append(test_monster_name)
                    inventory_before_state = adapter.state(lambda row: row.get("connected_in_game") is True
                        and row.get("inventory_observed") is True, timeout=5)
                    spawn_inventory_before = inventory_counts(inventory_before_state.get("inventory", []))
                    result_state = adapter.spawn_monster(str(target_mob["aegis_name"]), test_monster_name,
                                                         COMBAT_EVENT_LABEL,
                                                         timeout=float(step.get("timeout_seconds", 15)),
                                                         count=int(step.get("count", 1)))
                    spawned_map = str(result_state.get("map", ""))
                    if spawned_map != active_map:
                        raise RuntimeError(f"controlled monster appeared on {spawned_map}, expected active map {active_map}")
                    event_map_by_name[test_monster_name] = spawned_map
                    monster_records = [mob for mob in result_state["nearby_monsters"]
                                       if mob.get("name", "").casefold() == test_monster_name.casefold()]
                    result = {"id": step_id, "type": step_type, "status": "PASS",
                              "name": test_monster_name,
                              "count": len(monster_records),
                              "monster": {key: target_mob.get(key) for key in (
                                  "id", "aegis_name", "name", "level", "hp", "attack",
                                  "defense", "race", "size", "element", "element_level")},
                              "positions": [mob.get("position") for mob in monster_records],
                              "inventory_before": {str(item_id): amount
                                  for item_id, amount in spawn_inventory_before.items()},
                              "evidence": "rAthena @monster created the quest target and OpenKore observed it"}
                elif step_type == "set_ai_mode":
                    before_state = adapter.state(lambda row: row.get("connected_in_game") is True
                        and row.get("inventory_observed") is True, timeout=5)
                    before_counts = inventory_counts(before_state.get("inventory", []))
                    result = adapter.set_ai_mode(str(step["mode"]), timeout=float(step.get("timeout_seconds", 10)))
                    result.update({"id": step_id, "type": step_type, "status": "PASS",
                        "inventory_before": {str(item_id): amount for item_id, amount in before_counts.items()}})
                elif step_type == "await_inventory_delta":
                    source = step_results.get(str(step.get("source_step", "")))
                    if not source or source.get("status") != "PASS" or not isinstance(source.get("inventory_before"), dict):
                        raise RuntimeError("await_inventory_delta requires a prior successful action with an inventory snapshot")
                    requested = step.get("inventory_delta", {})
                    expected_by_id = {}
                    names_by_id = {}
                    for aegis_name, delta in requested.items():
                        item = resolve_item(item_catalog, str(aegis_name))
                        expected_by_id[int(item["id"])] = int(delta)
                        names_by_id[int(item["id"])] = str(item["aegisname"])
                    inventory_before = {item_id: int(source["inventory_before"].get(str(item_id), 0))
                                        for item_id in expected_by_id}
                    try:
                        observed = adapter.state(lambda row: row.get("connected_in_game") is True
                            and row.get("inventory_observed") is True
                            and all(inventory_counts(row.get("inventory", [])).get(item_id, 0)
                                - inventory_before[item_id] == delta
                                for item_id, delta in expected_by_id.items()),
                            timeout=float(step.get("timeout_seconds", 20)))
                    except TimeoutError as exc:
                        failure = {"id": step_id, "type": step_type, "status": "FAIL",
                            "failure_classification": "native_inventory_policy_not_observed",
                            "error": f"{type(exc).__name__}: {exc}",
                            "evidence": "OpenKore structured inventory did not reach the declared delta"}
                        report["ordered_actions"].append(failure)
                        step_results[step_id] = failure
                        raise RuntimeError(f"native_inventory_policy_not_observed: {exc}") from exc
                    inventory_after = inventory_counts(observed.get("inventory", []))
                    result = {"id": step_id, "type": step_type, "status": "PASS",
                        "source_step": str(step["source_step"]),
                        "inventory_delta": {names_by_id[item_id]: inventory_after.get(item_id, 0)
                            - inventory_before[item_id] for item_id in expected_by_id},
                        "openkore_inventory_before": {names_by_id[item_id]: inventory_before[item_id]
                            for item_id in expected_by_id},
                        "openkore_inventory_after": {names_by_id[item_id]: inventory_after.get(item_id, 0)
                            for item_id in expected_by_id},
                        "evidence": "OpenKore stateObserver confirmed the declared item delta after the referenced action"}
                elif step_type == "attack_monster":
                    spawn_result = step_results.get(str(step.get("spawn_step", "")))
                    if not spawn_result or spawn_result.get("status") != "PASS":
                        raise RuntimeError("attack_monster precondition has no successful spawn result")
                    defeated = adapter.attack_monster(str(spawn_result["name"]),
                        float(step.get("timeout_seconds", 60)))
                    events_to_clean.get(COMBAT_EVENT_LABEL, []).remove(str(spawn_result["name"]))
                    if not events_to_clean.get(COMBAT_EVENT_LABEL):
                        events_to_clean.pop(COMBAT_EVENT_LABEL, None)
                    if quest_fixture_started:
                        scenario_quest_states[int(quest_fixture["id"])] = "hunt_complete"
                    result = {"id": step_id, "type": step_type, "status": "PASS",
                              "target": spawn_result["name"],
                              "monster_disappeared_from_client_view": not any(
                                  mob.get("name", "").casefold() == str(spawn_result["name"]).casefold()
                                  for mob in defeated.get("nearby_monsters", [])),
                              "character_hp_after_combat": defeated.get("character_attributes", {}).get("hp"),
                              "evidence": "OpenKore native attack command and structured target disappearance"}
                elif step_type == "await_native_ai":
                    spawn_result = step_results.get(str(step.get("spawn_step", "")))
                    if not spawn_result or spawn_result.get("status") != "PASS":
                        raise RuntimeError("await_native_ai requires an earlier observed controlled monster spawn")
                    if not scenario.get("openkore", {}).get("eventmacro_source"):
                        raise RuntimeError("await_native_ai requires a native eventMacro source loaded by this session")
                    requested_deltas = step.get("inventory_delta", {})
                    item_ids = {}
                    item_before = {}
                    item_names = {}
                    for aegis_name, delta in requested_deltas.items():
                        item = resolve_item(item_catalog, str(aegis_name))
                        item_id = int(item["id"])
                        item_ids[item_id] = int(delta)
                        item_names[item_id] = str(item["aegisname"])
                        item_before[item_id] = inventory_counts(adapter.state(
                            lambda row: row.get("connected_in_game") is True, timeout=5).get("inventory", [])
                            ).get(item_id, 0)
                    try:
                        observed = adapter.wait_native_ai_outcome(
                            str(spawn_result["name"]), str(step["eventmacro_marker"]),
                            item_before, item_ids, float(step.get("timeout_seconds", 90)))
                    except Exception as exc:
                        classification = ("timeout" if isinstance(exc, TimeoutError)
                                          else "disconnected" if re.search(r"disconnect|connection", str(exc), re.I)
                                          else "native_ai_or_eventmacro_failure")
                        failure = {"id": step_id, "type": step_type, "status": "FAIL",
                                   "failure_classification": classification,
                                   "eventmacro_marker_observed": str(step["eventmacro_marker"]) in adapter.output,
                                   "error": f"{type(exc).__name__}: {exc}",
                                   "evidence": "native OpenKore AI/eventMacro wait plus structured observer"}
                        report["ordered_actions"].append(failure)
                        step_results[step_id] = failure
                        raise RuntimeError(f"{classification}: {exc}") from exc
                    events_to_clean.get(COMBAT_EVENT_LABEL, []).remove(str(spawn_result["name"]))
                    if not events_to_clean.get(COMBAT_EVENT_LABEL):
                        events_to_clean.pop(COMBAT_EVENT_LABEL, None)
                    if quest_fixture_started:
                        scenario_quest_states[int(quest_fixture["id"])] = str(
                            step.get("quest_state_after_action", "hunt_complete"))
                    result = {"id": step_id, "type": step_type, **observed,
                        "target": spawn_result["name"],
                        "inventory_delta": {item_names[item_id]: delta
                                            for item_id, delta in item_ids.items()},
                        "quest_state_update": ({"id": int(quest_fixture["id"]),
                            "state": scenario_quest_states[int(quest_fixture["id"])],
                            "evidence": "native quest hunt completion and eventMacro condition verified"}
                            if quest_fixture_started else None),
                        "evidence": "OpenKore AI target policy, native eventMacro turn-in, structured actor/inventory observations"}
                    _refresh_openkore_precondition_inventory(precondition_inventory,
                        observed.get("inventory_observation", {}), fixture_items)
                elif step_type == "collect_monster_drop":
                    attack_result = step_results.get(str(step.get("attack_step", "")))
                    if not quest_fixture or not attack_result or attack_result.get("status") != "PASS":
                        raise RuntimeError("collect_monster_drop requires a successful earlier quest-monster defeat")
                    item = resolve_item(item_catalog, str(step["item"]))
                    amount = int(step.get("amount", 1))
                    if amount != 1:
                        raise ValueError("collect_monster_drop currently supports one declared item per action")
                    mob = find_monster(load_monster_catalog(rathena_path), str(quest_fixture["monster"]))
                    guaranteed = sum(1 for drop in mob.get("drops", [])
                        if str(drop.get("item", "")).casefold() == str(item["aegisname"]).casefold()
                        and int(drop.get("rate", 0)) >= 10000)
                    if guaranteed < amount:
                        raise ValueError(f"active mob data does not guarantee {amount} {item['aegisname']} drop(s)")
                    result = adapter.collect_monster_drop(int(item["id"]), str(item["name"]),
                        timeout=float(step.get("timeout_seconds", 20)))
                    result.update({"id": step_id, "type": step_type, "status": "PASS",
                        "monster": mob["aegis_name"], "drop_rate": 10000,
                        "evidence": "active mob drop data plus OpenKore take and structured inventory delta"})
                    precondition_inventory["openkore"][str(item["aegisname"])] = int(result["inventory_after"])
                elif step_type == "macro":
                    target = step.get("target")
                    if not isinstance(target, dict) or not all(type(target.get(axis)) is int for axis in ("x", "y")):
                        raise ValueError("macro actions require an integer target position")
                    try:
                        result = adapter.run_macro(str(step["name"]), str(step["_completion_marker"]),
                            target, str(initial["map"]), float(step.get("timeout_seconds", 30)),
                            progress_marker=step.get("_progress_marker"),
                            progress_target=step.get("progress_target"),
                            interrupt_after_progress=bool(step.get("interrupt_after_progress", False)))
                    except Exception as exc:
                        classification = ("timeout" if isinstance(exc, TimeoutError)
                                          else "disconnected" if re.search(r"disconnect|connection", str(exc), re.I)
                                          else "macro_failure")
                        progress_seen = bool(step.get("_progress_marker") and
                            re.search(rf"\[macro log\] {re.escape(str(step['_progress_marker']))}", adapter.output, re.I))
                        failure = {"id": step_id, "type": step_type, "status": "FAIL",
                                   "failure_classification": classification,
                                   "lifecycle": [{"state": "started"},
                                       *([{"state": "progress", "marker_observed": True}] if progress_seen else []),
                                       {"state": "failed", "classification": classification}],
                                   "error": f"{type(exc).__name__}: {exc}",
                                   "completion_marker_observed": bool(re.search(
                                       rf"\[macro log\] {re.escape(str(step['_completion_marker']))}",
                                       adapter.output, re.I))}
                        report["ordered_actions"].append(failure)
                        step_results[step_id] = failure
                        raise
                    result.update({"id": step_id, "target": target})
                elif step_type == "move":
                    target = step.get("target")
                    if not isinstance(target, dict) or not all(type(target.get(axis)) is int for axis in ("x", "y")):
                        raise ValueError("move actions require an integer target position")
                    target_map = str(target.get("map", active_map))
                    if target_map != active_map and (active_map, target_map) not in {
                            (str(edge["from"]), str(edge["to"]))
                            for edge in scenario.get("openkore", {}).get("map_transitions", [])}:
                        raise ValueError(f"undeclared map transition {active_map} -> {target_map}")
                    before_map = active_map
                    observed = adapter.move(target_map, {"x": target["x"], "y": target["y"]},
                                            float(step.get("timeout_seconds", 30)))
                    active_map = target_map
                    result = {"id": step_id, "type": step_type, "status": "PASS",
                        "previous_map": before_map, "map": observed.get("map"),
                        "map_transition_observed": before_map != target_map,
                        "position": position(observed),
                        "evidence": "OpenKore native move command and structured observer confirmed stationary arrival"}
                elif step_type == "restore_openkore_profile":
                    try:
                        restored = adapter.restore_runtime_profile(
                            str(step["restore_ai_mode"]), str(step["handoff_macro"]),
                            str(step["handoff_marker"]), float(step.get("timeout_seconds", 30)))
                    except Exception as exc:
                        failure = {"id": step_id, "type": step_type, "status": "FAIL",
                            "failure_classification": ("timeout" if isinstance(exc, TimeoutError)
                                else "profile_handoff_failure"),
                            "error": f"{type(exc).__name__}: {exc}",
                            "evidence": "native config restoration, control-file reload, and eventMacro handoff"}
                        report["ordered_actions"].append(failure)
                        step_results[step_id] = failure
                        raise RuntimeError(f"profile_handoff_failure: {exc}") from exc
                    result = {"id": step_id, "type": step_type, **restored}
                elif step_type == "unload_npc":
                    if not npc_loaded:
                        raise RuntimeError("NPC unload step has no loaded laboratory NPC")
                    adapter.unload_npc(temporary_npc_name)
                    npc_loaded = False
                    result = {"id": step_id, "type": step_type, "status": "PASS",
                              "npc_removed_from_client_view": True,
                              "evidence": "rAthena @unloadnpc acknowledgement and OpenKore observer"}
                elif step_type == "equip_item":
                    item = resolve_item(item_catalog, str(step["item"]))
                    inventory_fixture_started = True
                    result = adapter.equip_item(int(item["id"]), str(item["name"]),
                        expect_success=bool(step.get("expect_success", True)),
                        timeout=float(step.get("timeout_seconds", 15)))
                    result.update({"id": step_id, "type": step_type,
                        "equipment_semantics": equipment_assessments[str(item["aegisname"])]})
                elif step_type == "unequip_item":
                    item = resolve_item(item_catalog, str(step["item"]))
                    inventory_fixture_started = True
                    result = adapter.unequip_item(int(item["id"]), str(item["name"]),
                                                  timeout=float(step.get("timeout_seconds", 15)))
                    result.update({"id": step_id, "type": step_type})
                elif step_type == "verify_inventory":
                    if index != len(steps) - 1:
                        raise ValueError("verify_inventory must be the final step because it logs out for independent persistence checks")
                    adapter.archive_observer()
                    report["observer_artifact"] = str(run_dir / "openkore-state.jsonl")
                    adapter.close()
                    adapter = None
                    saved = rathena.wait_offline(character)
                    expected = step.get("expected", {})
                    expected_location = expected.get("location")
                    persisted_location = {"map": saved["map"], "x": saved["x"], "y": saved["y"]}
                    if expected_location:
                        if (not isinstance(expected_location, dict)
                                or persisted_location != {key: expected_location.get(key)
                                    for key in ("map", "x", "y")}):
                            raise RuntimeError(f"rAthena persisted location {persisted_location}, expected {expected_location}")
                        report["assertions"]["rathena_location_verified_after_logout"] = "PASS"
                    actual_rows = rathena.inventory_rows(original, inventory_ids)
                    expected_deltas = expected.get("inventory_delta", {})
                    expected_ranges = expected.get("inventory_delta_ranges", {})
                    observed_deltas = {}
                    verified_ranges = {}
                    for aegis_name in (*expected_deltas.keys(), *expected_ranges.keys()):
                        item = resolve_item(item_catalog, str(aegis_name))
                        item_id = int(item["id"])
                        before_amount = rathena.inventory_amount(original_inventory_rows, item_id)
                        for fixture in inventory_fixture:
                            prepared_item = resolve_item(item_catalog, str(fixture["item"]))
                            if int(prepared_item["id"]) == item_id:
                                before_amount += int(fixture["amount"])
                        after_amount = rathena.inventory_amount(actual_rows, item_id)
                        observed_deltas[str(aegis_name)] = after_amount - before_amount
                        if aegis_name in expected_ranges:
                            bounds = expected_ranges[aegis_name]
                            if not bounds["min"] <= observed_deltas[str(aegis_name)] <= bounds["max"]:
                                raise RuntimeError(f"rAthena inventory delta for {aegis_name} was "
                                    f"{observed_deltas[str(aegis_name)]}, outside declared range {bounds}")
                            verified_ranges[str(aegis_name)] = {**bounds,
                                "observed_delta": observed_deltas[str(aegis_name)],
                                "source_bound": ranged_drop_contracts[str(item["aegisname"])]}
                        elif observed_deltas[str(aegis_name)] != int(expected_deltas[aegis_name]):
                            raise RuntimeError(f"rAthena inventory delta for {aegis_name} was "
                                               f"{observed_deltas[str(aegis_name)]}, expected {expected_deltas[aegis_name]}")
                    quest_observation = None
                    expected_quest = step.get("expected", {}).get("quest")
                    if expected_quest:
                        quest_id = int(expected_quest["id"])
                        expected_state = {"active": 0, "complete": 2}.get(str(expected_quest["state"]))
                        if quest_id not in quest_ids or expected_state is None:
                            raise ValueError("quest verification requires a declared active-database quest and active/complete state")
                        actual_quest_rows = rathena.quest_rows(original, quest_ids)
                        matching_quest = next((row for row in actual_quest_rows if row["quest_id"] == quest_id), None)
                        if matching_quest is None or matching_quest["state"] != expected_state:
                            raise RuntimeError(f"rAthena did not persist quest {quest_id} in state {expected_quest['state']}")
                        quest_observation = {"id": quest_id, "state": str(expected_quest["state"]),
                            "database_state": matching_quest["state"],
                            "independent_server_observation": True}
                        report["assertions"]["rathena_quest_state_verified_after_logout"] = "PASS"
                    result = {"id": step_id, "type": step_type, "status": "PASS",
                              "rathena_inventory_delta": observed_deltas,
                              "rathena_inventory_delta_ranges": verified_ranges,
                              "rathena_quest": quest_observation,
                              "rathena_persisted_location": persisted_location,
                              "independent_server_observation": True,
                              "evidence": "rAthena inventory rows after OpenKore logout"}
                    report["assertions"]["rathena_inventory_transaction_verified"] = "PASS"
                elif step_type == "verify_equipment":
                    if index != len(steps) - 1:
                        raise ValueError("verify_equipment must be the final step because it logs out for independent persistence checks")
                    adapter.archive_observer()
                    report["observer_artifact"] = str(run_dir / "openkore-state.jsonl")
                    adapter.close()
                    adapter = None
                    saved = rathena.wait_offline(character)
                    expected_location = step.get("expected", {}).get("location")
                    persisted_location = {"map": saved["map"], "x": saved["x"], "y": saved["y"]}
                    if expected_location and persisted_location != expected_location:
                        raise RuntimeError(f"rAthena persisted location {persisted_location}, expected {expected_location}")
                    if expected_location:
                        report["assertions"]["rathena_location_verified_after_logout"] = "PASS"
                    actual_rows = rathena.inventory_rows(original, inventory_ids)
                    masks = equipment_location_masks(rathena_path)
                    verified = []
                    declared_equipment = step.get("expected", {}).get("equipment", [])
                    if not declared_equipment:
                        raise ValueError("verify_equipment requires expected.equipment entries")
                    for expectation in declared_equipment:
                        item = resolve_item(item_catalog, str(expectation["item"]))
                        item_id = int(item["id"])
                        matching = [row for row in actual_rows if int(row["nameid"]) == item_id]
                        should_equip = bool(expectation["equipped"])
                        equipped = [row for row in matching if int(row["equip"]) != 0]
                        if bool(equipped) != should_equip:
                            raise RuntimeError(f"rAthena persisted unexpected equipped state for {item['aegisname']}")
                        location = expectation.get("location")
                        mask = masks.get(str(location)) if location else None
                        if location and mask is None:
                            raise ValueError(f"unknown rAthena equipment location: {location}")
                        if should_equip and mask is not None and not any(int(row["equip"]) & mask for row in equipped):
                            raise RuntimeError(f"rAthena equipped {item['aegisname']} in an unexpected slot")
                        expected_amount = rathena.inventory_amount(original_inventory_rows, item_id)
                        for fixture in inventory_fixture:
                            if int(resolve_item(item_catalog, str(fixture["item"]))["id"]) == item_id:
                                expected_amount += int(fixture["amount"])
                        persisted_amount = rathena.inventory_amount(actual_rows, item_id)
                        if persisted_amount != expected_amount:
                            raise RuntimeError(f"rAthena inventory amount changed while equipping {item['aegisname']}")
                        verified.append({"item": item["aegisname"], "equipped": should_equip,
                            "equip_mask": max((int(row["equip"]) for row in equipped), default=0),
                            "location": location, "inventory_amount": persisted_amount})
                    inventory_deltas = _verify_persisted_inventory_deltas(
                        step.get("expected", {}).get("inventory_delta", {}), item_catalog,
                        original_inventory_rows, actual_rows, inventory_fixture,
                        rathena.inventory_amount)
                    result = {"id": step_id, "type": step_type, "status": "PASS",
                        "rathena_persisted_location": persisted_location,
                        "rathena_equipment": verified,
                        "rathena_inventory_delta": inventory_deltas,
                        "independent_server_observation": True,
                        "evidence": "rAthena inventory equip bitmasks after OpenKore logout"}
                    report["assertions"]["rathena_equipment_verified_after_logout"] = "PASS"
                else:
                    raise ValueError(f"unsupported ordered action type: {step_type}")
                expected_status = step.get("expected_status")
                if expected_status and result.get("status") != expected_status:
                    observed_failure = dict(result)
                    observed_failure.update({
                        "status": "FAIL",
                        "observed_status": result.get("status"),
                        "failure_classification": "unexpected_action_status",
                        "expected_status": expected_status,
                    })
                    report["ordered_actions"].append(observed_failure)
                    raise RuntimeError(f"action {step_id} returned {result.get('status')}, expected {expected_status}")
                step_results[step_id] = result
                result.setdefault("preconditions", step.get("preconditions", []))
                report["ordered_actions"].append(result)
                active_step = None
            report["assertions"]["ordered_actions_and_preconditions"] = "PASS"
        elif action.get("type") == "move_to_position":
            target = action["target"]
            observed = adapter.move(initial["map"], target, float(action.get("timeout_seconds", 30)))
            report["openkore_observed_target"] = {
                "map": observed["map"], "position": position(observed),
                "movement_state": observed["movement_state"], "evidence": "structured OpenKore observer"}
            report["assertions"]["native_openkore_move_completed"] = "PASS"
        elif action.get("type") == "combat":
            live_stats = state.get("combat_stats", {})
            live_attributes = state.get("character_attributes", {})
            intent = str(action.get("intent", "victory"))
            if not isinstance(live_stats.get("attack"), (int, float)) and intent not in ("escape",):
                raise RuntimeError("OpenKore did not report calculated character attack statistics")
            observed_attack = (int(live_stats["attack"]) if isinstance(live_stats.get("attack"), (int, float))
                               else max(1, int(live_attributes.get("str", original.get("str", 1)))) )
            semantic = assess_match(
                {**original, **live_attributes, "attack": observed_attack}, monster,
                intent,
                allow_extreme=bool(action.get("allow_extreme", False)),
                reason=str(action.get("extreme_reason", "")),
                duration_seconds=float(action.get("timeout_seconds", 60)),
                skill_id=str(action["skill_id"]) if "skill_id" in action else None,
                skill_level=int(action.get("skill_level", 1)))
            report["monster"] = {key: monster.get(key) for key in (
                "id", "aegis_name", "name", "level", "hp", "attack", "attack2", "defense",
                "magic_defense", "race", "size", "element", "element_level", "ai", "mode_bits",
                "behavior", "base_exp", "job_exp")}
            report["semantic_assessment"] = semantic
            stamp = datetime.now(timezone.utc).strftime("%H%M%S")
            test_monster_name = "KoreLabT" + stamp
            monster_spawned = True
            events_to_clean[COMBAT_EVENT_LABEL] = [test_monster_name]
            spawned = adapter.spawn_monster(str(monster["aegis_name"]), test_monster_name,
                                            COMBAT_EVENT_LABEL)
            spawned_target = next(mob for mob in spawned["nearby_monsters"]
                                  if mob.get("name") == test_monster_name)
            report["spawn_observation"] = {
                "name": spawned_target["name"],
                "position": spawned_target["position"],
                "test_name": test_monster_name,
                "evidence": "OpenKore actor list after rAthena @monster"}
            intent = str(action.get("intent", "victory"))
            if intent == "victory":
                defeated = adapter.attack_monster(test_monster_name,
                    float(action.get("timeout_seconds", 60)))
                report["combat_observation"] = {
                    "monster_disappeared_from_client_view": True,
                    "character_hp_after_combat": defeated.get("character_attributes", {}).get("hp"),
                    "combat_stats": defeated.get("combat_stats"),
                    "evidence": "OpenKore structured observer"}
                report["assertions"]["native_openkore_combat_completed"] = "PASS"
                cleanup_probe_name = "KoreLabP" + stamp
                control_name = "KoreLabC" + stamp
                control_monster_name = control_name
                events_to_clean[COMBAT_EVENT_LABEL].append(cleanup_probe_name)
                adapter.spawn_monster(str(monster["aegis_name"]), cleanup_probe_name,
                                      COMBAT_EVENT_LABEL, require_clear=False)
                adapter.spawn_monster(str(monster["aegis_name"]), control_name,
                                      None, require_clear=False)
                selective_cleanup = adapter.remove_test_monsters(
                    str(initial["map"]), COMBAT_EVENT_LABEL,
                    owned_names=(test_monster_name, cleanup_probe_name),
                    must_preserve_names=(control_name,))
                events_to_clean.pop(COMBAT_EVENT_LABEL, None)
                report.setdefault("cleanup", {})["test_map_monster_cleanup"] = {
                    "status": "PASS", "evidence": "rAthena event-filtered @killmonster2 acknowledgement",
                    "selective_verification": selective_cleanup}
                control_defeat = adapter.attack_monster(control_name,
                    float(action.get("timeout_seconds", 60)))
                report["cleanup"]["control_monster"] = {
                    "preserved_after_selective_cleanup": True,
                    "removed_by_openkore_attack": not any(
                        mob.get("name", "").casefold() == control_name.casefold()
                        for mob in control_defeat.get("nearby_monsters", []))}
            elif intent == "escape":
                destination = action.get("destination")
                if not isinstance(destination, dict) or not all(
                        isinstance(destination.get(axis), int) for axis in ("x", "y")):
                    raise ValueError("escape intent requires an integer destination x/y")
                escaped = adapter.flee_from_monster(test_monster_name, destination,
                    float(action.get("timeout_seconds", 45)))
                report["combat_observation"] = {
                    "flee_movement_completed": True,
                    "start_position": escaped["start_position"],
                    "destination": escaped["destination"],
                    "character_hp_after_escape": escaped["character_attributes"].get("hp"),
                    "combat_stats": escaped["combat_stats"],
                    "evidence": "OpenKore observed movement to declared escape destination"}
                report["assertions"]["native_openkore_escape_movement"] = "PASS"
                cleanup_result = adapter.remove_test_monsters(
                    str(initial["map"]), COMBAT_EVENT_LABEL, owned_names=(test_monster_name,))
                events_to_clean.pop(COMBAT_EVENT_LABEL, None)
                report.setdefault("cleanup", {})["test_map_monster_cleanup"] = {
                    "status": "PASS", "evidence": "rAthena event-filtered @killmonster2 acknowledgement",
                    "selective_verification": cleanup_result}
            elif intent == "defeat":
                dead = adapter.attack_until_dead(test_monster_name,
                    float(action.get("timeout_seconds", 60)))
                hp_after_combat = dead.get("character_attributes", {}).get("hp")
                if dead.get("character_attributes", {}).get("is_dead") is not True:
                    raise RuntimeError("OpenKore did not observe the expected character death")
                report["combat_observation"] = {
                    "character_hp_after_combat": hp_after_combat,
                    "character_dead": True,
                    "monster_remained_visible": any(
                        mob.get("name", "").casefold() == test_monster_name.casefold()
                        for mob in dead.get("nearby_monsters", [])),
                    "evidence": "fresh OpenKore structured state after native attack"}
                report["assertions"]["native_openkore_character_death"] = "PASS"
                revived = adapter.revive_test_character()
                report["rathena_revival"] = {
                    "character_hp_after_server_revive": revived["character_attributes"].get("hp"),
                    "evidence": "rAthena @alive response after observed death"}
                report["assertions"]["rathena_confirmed_death_and_revival"] = "PASS"
                cleanup_result = adapter.remove_test_monsters(
                    str(initial["map"]), COMBAT_EVENT_LABEL, owned_names=(test_monster_name,))
                events_to_clean.pop(COMBAT_EVENT_LABEL, None)
                report.setdefault("cleanup", {})["test_map_monster_cleanup"] = {
                    "status": "PASS", "evidence": "rAthena event-filtered @killmonster2 acknowledgement",
                    "selective_verification": cleanup_result}
                report["assertions"]["native_openkore_combat_completed"] = "PASS"
            elif intent == "resistance":
                skill_id = str(action.get("skill_id", ""))
                skill_level = int(action.get("skill_level", 1))
                comparison_monster = find_monster(catalog, scenario["comparison_monster"])
                comparison_name = "KoreLabR" + stamp
                events_to_clean[COMBAT_EVENT_LABEL].append(comparison_name)
                adapter.spawn_monster(str(comparison_monster["aegis_name"]), comparison_name,
                                      COMBAT_EVENT_LABEL, require_clear=False)
                samples = int(action.get("comparison_samples", 1))
                if not 1 <= samples <= 2:
                    raise ValueError("comparison_samples must be 1 or 2")
                baseline_damage, comparison_damage = [], []
                baseline_observations, comparison_observations = [], []
                recoveries = []
                first = None
                for sample_index in range(samples):
                    baseline = adapter.use_monster_skill(skill_id, test_monster_name, skill_level,
                        float(action.get("timeout_seconds", 20)), int(action.get("cast_range", 2)))
                    if baseline["monster_damage_after"] is None:
                        raise RuntimeError("baseline target died before its damage could be measured")
                    baseline_damage.append(baseline["monster_damage_after"] - baseline["monster_damage_before"])
                    baseline_observations.append(baseline)
                    if first is None:
                        first = baseline
                    recovered = adapter.heal_test_character()
                    recoveries.append({"hp": recovered.get("character_attributes", {}).get("hp"),
                                       "sp": recovered.get("character_attributes", {}).get("sp")})
                    comparison = adapter.use_monster_skill(skill_id, comparison_name, skill_level,
                        float(action.get("timeout_seconds", 20)), int(action.get("cast_range", 2)))
                    if comparison["monster_damage_after"] is None:
                        raise RuntimeError("comparison target died before its damage could be measured")
                    comparison_damage.append(comparison["monster_damage_after"] - comparison["monster_damage_before"])
                    comparison_observations.append(comparison)
                    if sample_index + 1 < samples:
                        recovered = adapter.heal_test_character()
                        recoveries.append({"hp": recovered.get("character_attributes", {}).get("hp"),
                                           "sp": recovered.get("character_attributes", {}).get("sp")})
                baseline_mean = sum(baseline_damage) / len(baseline_damage)
                comparison_mean = sum(comparison_damage) / len(comparison_damage)
                if comparison_mean <= baseline_mean:
                    raise RuntimeError("Fire Bolt average damage did not increase against the declared Earth target")
                report["elemental_resistance_comparison"] = {
                    "skill_id": skill_id, "skill_level": skill_level,
                    "attack_element": "Fire", "samples_per_target": samples,
                    "server_native_recoveries_between_casts": recoveries,
                    "baseline": {
                        "mob": report["monster"], "observed_name": test_monster_name,
                        "damage_samples": baseline_damage, "mean_damage": baseline_mean,
                        "observer": baseline_observations},
                    "comparison": {
                        "mob": {key: comparison_monster.get(key) for key in (
                            "id", "aegis_name", "name", "magic_defense", "element", "element_level")},
                        "observed_name": comparison_name, "damage_samples": comparison_damage,
                        "mean_damage": comparison_mean, "observer": comparison_observations},
                    "mean_damage_ratio": round(comparison_mean / baseline_mean, 4) if baseline_mean else None,
                    "evidence": "OpenKore damage-counter deltas from paired Fire Bolt casts in one session"}
                report["skill_observation"] = first
                report["assertions"]["controlled_elemental_damage_difference"] = "PASS"
                cleanup_result = adapter.remove_test_monsters(
                    str(initial["map"]), COMBAT_EVENT_LABEL,
                    owned_names=(test_monster_name, comparison_name))
                events_to_clean.pop(COMBAT_EVENT_LABEL, None)
                report.setdefault("cleanup", {})["test_map_monster_cleanup"] = {
                    "status": "PASS", "evidence": "rAthena event-filtered @killmonster2 acknowledgement",
                    "selective_verification": cleanup_result}
            elif intent == "skill":
                skill_id = str(action.get("skill_id", ""))
                skill_level = int(action.get("skill_level", 1))
                target_kind = action.get("skill_target", "monster")
                if target_kind != "monster":
                    raise ValueError("live skill execution currently supports monster targets")
                skill_result = adapter.use_monster_skill(skill_id, test_monster_name, skill_level,
                    float(action.get("timeout_seconds", 20)))
                report["skill_observation"] = skill_result
                report["assertions"]["native_openkore_skill_executed"] = "PASS"
                cleanup_result = adapter.remove_test_monsters(
                    str(initial["map"]), COMBAT_EVENT_LABEL, owned_names=(test_monster_name,))
                events_to_clean.pop(COMBAT_EVENT_LABEL, None)
                report.setdefault("cleanup", {})["test_map_monster_cleanup"] = {
                    "status": "PASS", "evidence": "rAthena event-filtered @killmonster2 acknowledgement",
                    "selective_verification": cleanup_result}
            else:
                raise ValueError(f"unsupported live combat intent: {intent}")
        else:
            raise ValueError(f"unsupported action type: {action.get('type')}")
        if adapter:
            adapter.archive_observer()
            temporary_workspace = adapter.temp_path
            adapter.close()
            report.setdefault("cleanup", {})["openkore_temporary_workspace"] = {
                "removed": not temporary_workspace.exists(),
                "evidence": "OpenKore session exited and private configuration/control overlays were deleted"}
            report["observer_artifact"] = str(run_dir / "openkore-state.jsonl")
            adapter = None
        saved = rathena.wait_offline(character)
        if action.get("type") == "move_to_position":
            report["rathena_persisted_position"] = {
                "map": saved["map"], "position": {"x": saved["x"], "y": saved["y"]},
                "evidence": "character row after OpenKore disconnect"}
            expected = scenario["expected"]
            if saved["map"] != expected["map"] or saved["x"] != expected["position"]["x"] or saved["y"] != expected["position"]["y"]:
                raise RuntimeError(f"rAthena persisted {saved['map']} ({saved['x']},{saved['y']}), "
                                   f"expected {expected['map']} {expected['position']}")
            report["assertions"]["rathena_persisted_target_verified"] = "PASS"
        elif action.get("type") == "combat" and action.get("intent", "victory") == "victory":
            base_exp_delta = int(saved["base_exp"]) - int(original["base_exp"])
            job_exp_delta = int(saved["job_exp"]) - int(original["job_exp"])
            base_level_delta = int(saved["base_level"]) - int(original["base_level"])
            job_level_delta = int(saved["job_level"]) - int(original["job_level"])
            if (base_exp_delta <= 0 and job_exp_delta <= 0 and base_level_delta <= 0
                    and job_level_delta <= 0) or int(saved["hp"]) <= 0:
                raise RuntimeError("rAthena database did not confirm a living character with positive combat experience")
            report["rathena_combat_result"] = {
                "base_level_delta": base_level_delta,
                "job_level_delta": job_level_delta,
                "base_experience_balance_delta": base_exp_delta,
                "job_experience_balance_delta": job_exp_delta,
                "character_alive_after_disconnect": True,
                "evidence": "rAthena character row after logout: experience balance and level fields"}
            report["assertions"]["rathena_confirmed_monster_defeat"] = "PASS"
        elif action.get("type") == "combat" and action.get("intent") == "defeat":
            if int(saved["hp"]) <= 0:
                raise RuntimeError("rAthena did not persist the server-side revive before logout")
            report["rathena_combat_result"] = {
                "character_alive_after_server_revive_and_disconnect": True,
                "persisted_hp_after_revive": int(saved["hp"]),
                "evidence": "rAthena character row after @alive and logout"}
        elif action.get("type") == "combat" and action.get("intent") == "escape":
            target = action["destination"]
            if (saved["map"] != initial["map"] or saved["x"] != target["x"]
                    or saved["y"] != target["y"] or int(saved["hp"]) <= 0):
                raise RuntimeError("rAthena did not persist the living character at the escape destination")
            report["rathena_escape_result"] = {
                "persisted_map": saved["map"], "persisted_position": {"x": saved["x"], "y": saved["y"]},
                "character_alive": True, "evidence": "rAthena character row after OpenKore disconnect"}
            report["assertions"]["rathena_confirmed_escape_position"] = "PASS"
        if requested_attributes:
            if any(saved.get(key) != value for key, value in requested_attributes.items()):
                raise RuntimeError("rAthena did not persist the prepared character attributes")
            report["rathena_persisted_attributes"] = {
                key: saved[key] for key in requested_attributes
            }
            report["assertions"]["prepared_attributes_verified_in_rathena"] = "PASS"
        if combat_steps:
            report["rathena_persisted_combat_progress"] = {
                key: saved[key] for key in ("base_level", "job_level", "base_exp", "job_exp", "hp", "max_hp")}
            report["assertions"]["rathena_combat_progress_verified_after_logout"] = "PASS"
        if skill_fixture_levels:
            persisted_skills = rathena.character_skill_rows(original)
            if any(int(persisted_skills.get(skill_id, {}).get("lv", 0)) != level
                   for skill_id, level in skill_fixture_levels.items()):
                raise RuntimeError("rAthena did not persist the prepared character skill")
            report["rathena_persisted_skills"] = {
                skill_id: persisted_skills[skill_id]["lv"] for skill_id in skill_fixture_levels
            }
            report["assertions"]["prepared_skills_verified_in_rathena"] = "PASS"
        if action.get("type") == "combat" and action.get("intent") in ("skill", "resistance"):
            skill_result = report["skill_observation"]
            if (int(saved["hp"]) <= 0
                    or int(saved["sp"]) >= skill_result["sp_before"]
                    or int(saved["sp"]) <= 0):
                raise RuntimeError("rAthena did not persist a living character with reduced SP after skill use")
            report["rathena_skill_result"] = {
                "persisted_hp": int(saved["hp"]), "persisted_sp": int(saved["sp"]),
                "openkore_hp_immediately_after_skill": skill_result["hp_after"],
                "openkore_sp_immediately_after_skill": skill_result["sp_after"],
                "openkore_sp_before_skill": skill_result["sp_before"],
                "persisted_sp_delta_after_observation": int(saved["sp"]) - skill_result["sp_after"],
                "evidence": "rAthena character row after OpenKore disconnect; later value may include passive regeneration"}
            report["assertions"]["rathena_confirmed_skill_result"] = "PASS"
        report["status"] = "PASS"
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        classification = _failure_classification(exc)
        report["failure_classification"] = classification
        if isinstance(exc, UnexpectedCharacterDeath):
            report["failed_action"] = {"id": active_step.get("id") if active_step else "session_start",
                "type": active_step.get("type") if active_step else "session_start", "status": "FAIL",
                "failure_classification": classification,
                "preconditions": active_step.get("preconditions", []) if active_step else [],
                "error": f"{type(exc).__name__}: {exc}"}
        elif sequence and active_step is not None:
            report["failed_action"] = {"id": active_step.get("id"),
                "type": active_step.get("type"), "status": "FAIL",
                "failure_classification": classification,
                "preconditions": active_step.get("preconditions", []),
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if adapter:
            if action.get("type") == "combat" and action.get("intent") == "defeat":
                try:
                    current = adapter.state(lambda row: row.get("connected_in_game") is True,
                                            allow_dead=True)
                    if current.get("character_attributes", {}).get("is_dead") is True:
                        adapter.revive_test_character()
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(f"revive before cleanup: {exc}")
                    report["status"] = "FAIL"
            for event_label, owned_names in list(events_to_clean.items()):
                grouped_names: dict[str, list[str]] = {}
                for name in owned_names:
                    grouped_names.setdefault(event_map_by_name.get(name, str(initial["map"])), []).append(name)
                for monster_map, map_names in grouped_names.items():
                    try:
                        cleanup_result = adapter.remove_test_monsters(
                            monster_map, event_label, owned_names=tuple(map_names))
                        report.setdefault("cleanup", {})[f"{event_label.split('::')[-1]}_{monster_map}_cleanup"] = {
                            "status": "PASS", "map": monster_map,
                            "evidence": "rAthena event-filtered @killmonster2 acknowledgement",
                            "selective_verification": cleanup_result}
                    except Exception as exc:
                        report.setdefault("cleanup_errors", []).append(f"selective monster cleanup on {monster_map}: {exc}")
                        report["status"] = "FAIL"
            if quest_import_written and quest_fixture_path and quest_fixture_path.exists():
                try:
                    quest_fixture_path.unlink()
                    if adapter.process and adapter.process.poll() is None:
                        adapter.reload_quest_database(timeout=15)
                    else:
                        cleanup_dir = run_dir / "quest-database-cleanup"
                        cleanup_dir.mkdir(mode=0o700, exist_ok=True)
                        maintenance_adapter = OpenKoreAdapter(openkore_path, scenario,
                            cleanup_dir, start_time + duration, perl_path)
                        try:
                            maintenance_adapter.start()
                            maintenance_adapter.reload_quest_database(timeout=15)
                        finally:
                            maintenance_adapter.close()
                        rathena.wait_offline(character)
                    quest_import_written = False
                    report.setdefault("cleanup", {})["temporary_quest_import"] = {
                        "source_file_removed": True, "active_database_reloaded": True,
                        "quest_id": int(quest_fixture["id"])}
                    if quest_import_dir_created:
                        quest_fixture_path.parent.rmdir()
                        report["cleanup"]["temporary_quest_import"]["created_import_directory_removed"] = True
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(f"temporary quest database cleanup: {exc}")
                    report["status"] = "FAIL"
            if npc_loaded and temporary_npc_name:
                try:
                    adapter.unload_npc(temporary_npc_name)
                    npc_loaded = False
                    report.setdefault("cleanup", {})["temporary_npc"] = {
                        "status": "PASS", "unloaded": True,
                        "evidence": "rAthena @unloadnpc acknowledgement and OpenKore observer"}
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(f"temporary NPC unload: {exc}")
                    report["status"] = "FAIL"
            try:
                if control_monster_name and adapter.process and adapter.process.poll() is None:
                    current = adapter.state(lambda row: row.get("connected_in_game") is True)
                    if any(mob.get("name", "").casefold() == control_monster_name.casefold()
                           for mob in current.get("nearby_monsters", [])):
                        adapter.attack_monster(control_monster_name, timeout=20)
            except Exception as exc:
                report.setdefault("cleanup_errors", []).append(f"control monster cleanup: {exc}")
                report["status"] = "FAIL"
            try:
                adapter.archive_observer()
                temporary_workspace = adapter.temp_path
                report["observer_artifact"] = str(run_dir / "openkore-state.jsonl")
                adapter.close()
                report.setdefault("cleanup", {})["openkore_temporary_workspace"] = {
                    "removed": not temporary_workspace.exists(),
                    "evidence": "OpenKore session exited and private configuration/control overlays were deleted"}
            except Exception as exc:
                report.setdefault("cleanup_errors", []).append(f"OpenKore shutdown: {exc}")
                report["status"] = "FAIL"
                try:
                    adapter.close()
                except Exception:
                    pass
        if original:
            report.setdefault("cleanup", {})
            cleanup_ok = True
            if temporary_npc_path and temporary_npc_path.exists():
                try:
                    temporary_npc_path.unlink()
                    report["cleanup"].setdefault("temporary_npc", {}).update({
                        "source_file_removed": True})
                except OSError as exc:
                    report.setdefault("cleanup_errors", []).append(f"temporary NPC source removal: {exc}")
                    cleanup_ok = False
            if quest_import_written and quest_fixture_path and quest_fixture_path.exists():
                try:
                    quest_fixture_path.unlink()
                    report.setdefault("cleanup", {})["temporary_quest_import"] = {
                        "source_file_removed": True,
                        "active_database_reload_pending": False,
                        "quest_id": int(quest_fixture["id"])}
                    if adapter is not None:
                        adapter.reload_quest_database(timeout=15)
                        report["cleanup"]["temporary_quest_import"]["active_database_reloaded"] = True
                    else:
                        cleanup_dir = run_dir / "quest-database-cleanup"
                        cleanup_dir.mkdir(mode=0o700, exist_ok=True)
                        maintenance_adapter = OpenKoreAdapter(openkore_path, scenario,
                            cleanup_dir, start_time + duration, perl_path)
                        try:
                            maintenance_adapter.start()
                            maintenance_adapter.reload_quest_database(timeout=15)
                        finally:
                            maintenance_adapter.close()
                        rathena.wait_offline(character)
                        report["cleanup"]["temporary_quest_import"]["active_database_reloaded"] = True
                    if quest_import_dir_created and quest_fixture_path.parent.exists():
                        quest_fixture_path.parent.rmdir()
                        report["cleanup"]["temporary_quest_import"]["created_import_directory_removed"] = True
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(f"temporary quest import removal: {exc}")
                    cleanup_ok = False
            if inventory_restore_required and original_inventory_all_rows is not None:
                try:
                    current_inventory = rathena.all_inventory_rows(original)
                    if current_inventory == original_inventory_all_rows:
                        restored_inventory = current_inventory
                        inventory_ids_to_restore = tuple(sorted({int(row["nameid"])
                            for row in original_inventory_all_rows} | set(inventory_ids)))
                    else:
                        inventory_ids_to_restore = tuple(sorted({int(row["nameid"])
                            for row in (*original_inventory_all_rows, *current_inventory)}
                            | set(inventory_ids)))
                        restored_inventory = (rathena.restore_inventory_items(original,
                            inventory_ids_to_restore, original_inventory_all_rows)
                            if inventory_ids_to_restore else [])
                    report["cleanup"]["restored_inventory"] = {
                        "item_ids": list(inventory_ids_to_restore), "rows_restored": len(restored_inventory),
                        "exact_snapshot_verified": restored_inventory == original_inventory_all_rows,
                        "scope": "complete_character_inventory_including_unplanned_game_drops"}
                    if restored_inventory != original_inventory_all_rows:
                        raise RuntimeError("full inventory restoration did not match the pre-run snapshot")
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(f"inventory restoration: {exc}")
                    cleanup_ok = False
            if quest_fixture_started and original_quest_rows is not None:
                try:
                    restored_quests = rathena.restore_quest_rows(original, quest_ids, original_quest_rows)
                    report["cleanup"]["restored_quest_state"] = {
                        "quest_ids": list(quest_ids), "rows_restored": len(restored_quests),
                        "exact_snapshot_verified": True}
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(f"quest restoration: {exc}")
                    cleanup_ok = False
            try:
                if character_state_restore_required:
                    restored = rathena.restore_character_snapshot(original)
                    report["cleanup"]["restored_character_state"] = {
                        key: restored[key] for key in ("base_level", "job_level", "base_exp", "job_exp",
                            "str", "agi", "vit", "int", "dex", "luk", "hp", "max_hp", "sp", "max_sp", "zeny")}
                    report["cleanup"]["restored_character_state"]["exact_snapshot_verified"] = all(
                        restored[key] == original[key] for key in rathena.CHARACTER_FIELDS)
                    if not report["cleanup"]["restored_character_state"]["exact_snapshot_verified"]:
                        raise RuntimeError("character state restoration did not match the original snapshot")
                skills_to_restore = {**skill_fixture_levels,
                                     **{skill_id: level for skill_id, level in prerequisite_skills.items()},
                                     **{str(skill_id): int(level) for skill_id, level in requested_skills.items()}}
                if skills_to_restore:
                    rathena.restore_character_skills(original, original_skill_rows,
                        skills_to_restore)
                    report["cleanup"]["restored_character_skills"] = {
                        str(skill_id): original_skill_rows.get(str(skill_id))
                        for skill_id in skills_to_restore}
            except Exception as exc:
                report.setdefault("cleanup_errors", []).append(f"character state or skill restoration: {exc}")
                cleanup_ok = False
            try:
                restored = rathena.restore_position(character, original)
                report["cleanup"].update({"status": "PASS", "restored_position": {
                    "map": restored["map"], "x": restored["x"], "y": restored["y"]}})
                if not cleanup_ok:
                    report["cleanup"]["status"] = "FAIL"
            except Exception as exc:
                report.setdefault("cleanup_errors", []).append(f"position restoration: {exc}")
                cleanup_ok = False
            if not cleanup_ok:
                report["status"] = "FAIL"
            if (combat or sequence) and account_group is not None and group_changed:
                try:
                    rathena.set_account_group(int(original["account_id"]), temporary_group, int(account_group))
                    report["cleanup"]["account_group_restored"] = int(account_group)
                except Exception as exc:
                    report.setdefault("cleanup_errors", []).append(f"account-group restoration: {exc}")
                    report["status"] = "FAIL"
        expected_failure = scenario.get("expected_failure")
        if expected_failure and report.get("errors"):
            marker = str(expected_failure.get("message_contains", ""))
            matched = bool(marker) and any(marker in error for error in report["errors"])
            cleanup_verified = report.get("cleanup", {}).get("status") == "PASS" and not report.get("cleanup_errors")
            if matched and cleanup_verified:
                report["status"] = "PASS"
                report["negative_case"] = {
                    "expected_failure_detected": True,
                    "classification": str(expected_failure.get("classification", "declared_negative")),
                    "cleanup_verified": True,
                    "evidence": report["errors"][0]}
        if lock:
            lock.close()
        for held_map_lock in map_locks:
            held_map_lock.close()
        report["elapsed_seconds"] = round(time.monotonic() - start_time, 3)
        report_path = run_dir / "report.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
    return report


def main() -> int:
    """Preserve the historical entry point while keeping CLI code isolated."""
    from .cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
