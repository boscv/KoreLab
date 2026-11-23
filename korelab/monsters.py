from __future__ import annotations

import math
import re
from pathlib import Path


FIELDS = {
    "Id": "id", "AegisName": "aegis_name", "Name": "name", "Level": "level",
    "Hp": "hp", "Attack": "attack", "Attack2": "attack2", "Defense": "defense",
    "MagicDefense": "magic_defense", "Str": "str", "Agi": "agi", "Vit": "vit",
    "Int": "int", "Dex": "dex", "Luk": "luk", "Size": "size", "Race": "race",
    "Element": "element", "ElementLevel": "element_level", "Ai": "ai",
    "Class": "class", "AttackRange": "attack_range", "SkillRange": "skill_range",
    "ChaseRange": "chase_range", "WalkSpeed": "walk_speed", "AttackDelay": "attack_delay",
    "BaseExp": "base_exp", "JobExp": "job_exp",
}


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _parse_mob_file(path: Path) -> list[dict]:
    rows = []
    current = None
    nested = None
    current_drop = None
    for line in path.read_text(encoding="utf-8").splitlines():
        start = re.match(r"^  - Id:\s*(\d+)\s*$", line)
        if start:
            if current:
                rows.append(current)
            current, nested = {"id": int(start.group(1)), "explicit_modes": {}}, None
            current_drop = None
            continue
        if current is None:
            continue
        field = re.match(r"^    ([A-Za-z][A-Za-z0-9]*):\s*(.*?)\s*$", line)
        if field:
            key, value = field.groups()
            nested = key if key in ("Modes", "RaceGroups", "Drops") and not value else None
            if key == "Drops" and not value:
                current["drops"] = []
            current_drop = None
            if key in FIELDS and value:
                current[FIELDS[key]] = value.strip() if key == "Ai" else _scalar(value)
            continue
        drop_start = re.match(r"^      - Item:\s*(.*?)\s*$", line)
        if drop_start and nested == "Drops":
            current_drop = {"item": _scalar(drop_start.group(1))}
            current["drops"].append(current_drop)
            continue
        drop_field = re.match(r"^        (Rate|StealProtected):\s*(.*?)\s*$", line)
        if drop_field and nested == "Drops" and current_drop is not None:
            key, value = drop_field.groups()
            current_drop["rate" if key == "Rate" else "steal_protected"] = _scalar(value)
            continue
        item = re.match(r"^      ([A-Za-z][A-Za-z0-9]*):\s*(.*?)\s*$", line)
        if item and nested == "Modes":
            current["explicit_modes"][item.group(1)] = _scalar(item.group(2))
    if current:
        rows.append(current)
    return rows


def _enum_values(path: Path, prefix: str) -> dict[str, int]:
    result = {}
    for name, value in re.findall(rf"\b({prefix}[A-Z0-9_]*)\s*=\s*(0x[0-9A-Fa-f]+|\d+)",
                                 path.read_text(encoding="utf-8")):
        result[name] = int(value, 0)
    return result


def _active_database(checkout: Path) -> tuple[str, Path]:
    config = (checkout / "src/config/renewal.hpp").read_text(encoding="utf-8")
    mode = "renewal" if re.search(r"^\s*#define RENEWAL\s*$", config, re.M) else "pre-re"
    folder = "re" if mode == "renewal" else "pre-re"
    return mode, checkout / "db" / folder / "mob_db.yml"


def load_monster_catalog(checkout: Path) -> dict:
    checkout = checkout.expanduser().resolve()
    mode, base = _active_database(checkout)
    if not base.is_file():
        raise FileNotFoundError(f"active rAthena monster database not found: {base}")
    sources = [base]
    override = checkout / "db/import/mob_db.yml"
    if override.is_file():
        sources.append(override)
    values = {}
    for source in sources:
        for row in _parse_mob_file(source):
            if "aegis_name" not in row or "name" not in row:
                continue
            values[row["id"]] = {**values.get(row["id"], {}), **row}

    mob_types = _enum_values(checkout / "src/map/mob.hpp", "MONSTER_TYPE_")
    mode_flags = _enum_values(checkout / "src/common/mmo.hpp", "MD_")
    result = []
    for row in values.values():
        defaults = {"level": 1, "hp": 1, "attack": 0, "attack2": 0, "defense": 0,
                    "magic_defense": 0, "str": 1, "agi": 1, "vit": 1, "int": 1,
                    "dex": 1, "luk": 1, "size": "Small", "race": "Formless",
                    "element": "Neutral", "element_level": 1, "ai": "06", "class": "Normal",
                    "attack_range": 0, "skill_range": 0, "chase_range": 0,
                    "walk_speed": 150, "attack_delay": 0, "base_exp": 0, "job_exp": 0}
        for key, value in defaults.items():
            row.setdefault(key, value)
        ai_code = str(row.get("ai", "06"))
        mode_value = mob_types.get("MONSTER_TYPE_" + ai_code, 0)
        for key, value in row.get("explicit_modes", {}).items():
            normalized = key.replace("_", "").casefold()
            bit = next((mask for name, mask in mode_flags.items()
                        if name.startswith("MD_") and name[3:].replace("_", "").casefold() == normalized), 0)
            if value is True:
                mode_value |= bit
            elif value is False:
                mode_value &= ~bit
        modes = sorted(name[3:].lower() for name, bit in mode_flags.items()
                       if name != "MD_NONE" and bit and mode_value & bit)
        row["mode_bits"] = mode_value
        row["behavior"] = {
            "can_move": "canmove" in modes,
            "can_attack": "canattack" in modes,
            "aggressive": "aggressive" in modes,
            "assist": "assist" in modes,
            "looter": "looter" in modes,
            "mode_flags": modes,
            "explicit_modes": row.get("explicit_modes", {}),
        }
        row.setdefault("drops", [])
        result.append(row)
    result.sort(key=lambda mob: (str(mob["name"]).casefold(), int(mob["id"])))
    return {"mode": mode, "sources": [str(p) for p in sources], "count": len(result), "monsters": result}


def find_monster(catalog: dict, selector) -> dict:
    monsters = catalog["monsters"]
    if isinstance(selector, (str, int)):
        matches = [mob for mob in monsters if str(selector).casefold() in
                   (str(mob["id"]).casefold(), str(mob["aegis_name"]).casefold(),
                    str(mob["name"]).casefold())]
    elif isinstance(selector, dict):
        matches = monsters
        for key, expected in selector.items():
            if key == "level_min":
                matches = [mob for mob in matches if int(mob.get("level", 1)) >= expected]
            elif key == "level_max":
                matches = [mob for mob in matches if int(mob.get("level", 1)) <= expected]
            elif key in ("race", "size", "element"):
                matches = [mob for mob in matches if str(mob.get(key, "")).casefold() == str(expected).casefold()]
            elif key == "aggressive":
                matches = [mob for mob in matches if mob["behavior"]["aggressive"] is expected]
            else:
                raise ValueError(f"unsupported monster selector: {key}")
    else:
        raise ValueError("monster selector must be a name, ID, or supported filter object")
    if not matches:
        raise ValueError("no monster in the active rAthena database matches the selector")
    return matches[0]


def assess_match(character: dict, monster: dict, intent: str, *, allow_extreme: bool = False,
                 reason: str = "", duration_seconds: float = 30,
                 skill_id: str | None = None, skill_level: int = 1) -> dict:
    if intent not in {"victory", "defeat", "escape", "resistance", "skill"}:
        raise ValueError("intent must be victory, defeat, escape, resistance, or skill")
    hp = int(character.get("max_hp", character.get("hp", 0)))
    if character.get("attack") is not None:
        attack = max(1, int(character["attack"]))
    else:
        attack = max(1, int(character.get("str", 1)) + int(character.get("base_level", 1)) // 4)
    dealt = max(1, attack - int(monster.get("defense", 0)))
    hits_to_win = math.ceil(int(monster.get("hp", 1)) / dealt)
    attack_delay = max(0.3, float(monster.get("attack_delay", 1000)) / 1000)
    incoming_hits = (math.ceil(max(0, duration_seconds) / attack_delay)
                     if intent == "resistance" else math.ceil(hits_to_win / attack_delay))
    incoming = incoming_hits * max(1, int(monster.get("attack", 0)))
    predicted_win = incoming < hp and monster["behavior"]["can_move"] and monster["behavior"]["can_attack"]
    if not monster["behavior"]["can_attack"]:
        predicted_win = hits_to_win <= max(1, int(duration_seconds))
    if intent == "victory":
        compatible, summary = predicted_win, "estimated_win" if predicted_win else "estimated_risk_or_timeout"
    elif intent == "defeat":
        compatible, summary = incoming >= hp, "estimated_defeat" if incoming >= hp else "character_may_survive"
    elif intent == "escape":
        compatible = bool(monster["behavior"]["aggressive"] or monster["behavior"]["can_attack"])
        summary = "escape_requires_live_target_and_reachable_destination"
    elif intent == "resistance":
        compatible = incoming < hp
        summary = "estimated_survival" if compatible else "estimated_lethal_pressure"
    else:
        skills = character.get("skills", {})
        available_level = int(skills.get(str(skill_id), 0)) if skill_id is not None else 0
        compatible = available_level >= skill_level
        summary = "requested_skill_available" if compatible else "requested_skill_not_learned_at_level"
    if not compatible and not (allow_extreme and reason.strip()):
        raise ValueError(f"semantic guard rejected {intent}: {summary}; declare allow_extreme and a reason for an intentional extreme")
    return {"intent": intent, "assessment": summary, "estimated_player_attack": attack,
            "estimated_damage_per_hit": dealt, "estimated_hits_to_win": hits_to_win,
            "estimated_incoming_damage": incoming, "estimated_character_hp": hp,
            "requested_skill_id": skill_id, "requested_skill_level": skill_level,
            "available_skill_level": int(character.get("skills", {}).get(str(skill_id), 0)) if skill_id else 0,
            "allow_extreme": bool(allow_extreme), "extreme_reason": reason if allow_extreme else None,
            "assessment_is_heuristic": True}
