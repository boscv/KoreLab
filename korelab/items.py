from __future__ import annotations

import re
from pathlib import Path


def load_item_catalog(checkout: Path) -> dict[str, dict[str, object]]:
    """Read active item DB imports from the checked out rAthena database."""
    db = checkout / "db"
    root = db / ("re" if (db / "re" / "item_db.yml").is_file() else "pre-re")
    main = root / "item_db.yml"
    if not main.is_file():
        raise FileNotFoundError("active rAthena item_db.yml was not found")
    files = [main]
    text = main.read_text(encoding="utf-8")
    for rel in re.findall(r"(?m)^\s*- Path:\s*([^#\s]+)", text):
        candidate = checkout / rel
        if candidate.is_file():
            files.append(candidate)
    import_dir = db / "import"
    for candidate in sorted(import_dir.glob("item_db*.yml")) if import_dir.exists() else []:
        files.append(candidate)
    records: dict[str, dict[str, object]] = {}
    for path in files:
        content = path.read_text(encoding="utf-8")
        for block in re.split(r"(?m)^  - Id:\s*", content)[1:]:
            lines = block.splitlines()
            if not lines or not lines[0].strip().isdigit():
                continue
            record: dict[str, object] = {"id": int(lines[0].strip()), "source": str(path.relative_to(checkout))}
            for key in ("AegisName", "Name", "Type", "SubType", "Weight", "Attack", "Defense",
                        "EquipLevelMin", "EquipLevelMax", "WeaponLevel", "ArmorLevel", "Gender"):
                match = re.search(rf"(?m)^    {key}:\s*([^\r\n]+)", block)
                if match:
                    raw = match.group(1).split("#", 1)[0].strip().strip('"\'')
                    numeric = {"Weight", "Attack", "Defense", "EquipLevelMin", "EquipLevelMax",
                               "WeaponLevel", "ArmorLevel"}
                    record[key.lower()] = int(raw) if key in numeric and raw.isdigit() else raw
            for key in ("Jobs", "Classes", "Locations"):
                nested = re.search(rf"(?ms)^    {key}:\s*\n((?:^      [^\r\n]+\n?)+)", block)
                if nested:
                    options: dict[str, bool] = {}
                    for line in nested.group(1).splitlines():
                        pair = re.match(r"^      ([A-Za-z0-9_]+):\s*(true|false)\s*$", line)
                        if pair:
                            options[pair.group(1)] = pair.group(2) == "true"
                    record[key.lower()] = options
            if "aegisname" in record:
                # Later import entries are the server's override semantics.
                records[str(record["aegisname"])] = record
    if not records:
        raise ValueError("active rAthena item database contained no item records")
    return records


def resolve_item(catalog: dict[str, dict[str, object]], aegis_name: str) -> dict[str, object]:
    item = catalog.get(aegis_name)
    if not item:
        raise ValueError(f"item {aegis_name!r} is not present in the active rAthena item database")
    return item


def class_job_name(checkout: Path, class_id: int) -> str:
    """Resolve a server class ID through rAthena's active job enum source."""
    source = (checkout / "src/common/mmo.hpp").read_text(encoding="utf-8")
    enum = re.search(r"(?ms)enum e_job\s*\{(.*?)\}", source)
    if not enum:
        raise ValueError("rAthena e_job enum could not be read")
    numeric = -1
    for line in enum.group(1).splitlines():
        match = re.search(r"\b(JOB_[A-Z0-9_]+)\s*(?:=\s*(\d+))?\s*,", line)
        if not match:
            continue
        numeric = int(match.group(2)) if match.group(2) else numeric + 1
        if numeric == class_id:
            return match.group(1).removeprefix("JOB_")
    raise ValueError(f"rAthena class ID {class_id} has no declared e_job entry")


_JOB_ALIASES = {
    "LORD_KNIGHT": "KNIGHT", "LORD_KNIGHT2": "KNIGHT", "HIGH_PRIEST": "PRIEST",
    "HIGH_WIZARD": "WIZARD", "WHITESMITH": "BLACKSMITH", "SNIPER": "HUNTER",
    "ASSASSIN_CROSS": "ASSASSIN", "PALADIN": "CRUSADER", "CHAMPION": "MONK",
    "PROFESSOR": "SAGE", "STALKER": "ROGUE", "CREATOR": "ALCHEMIST",
}


def job_candidates(job: str) -> set[str]:
    canonical = _JOB_ALIASES.get(job, job)
    canonical = re.sub(r"_(HIGH|BABY|BABY2|2|E)$", "", canonical)
    if canonical == "SUPER_NOVICE":
        names = {"SuperNovice"}
    else:
        names = {"".join(piece.title() for piece in canonical.split("_"))}
    return names


def assess_equipment(item: dict[str, object], job_id: int, base_level: int,
                     requested_location: str | None, checkout: Path) -> dict[str, object]:
    if item.get("type") not in ("Armor", "Weapon"):
        return {"can_equip": False, "reasons": ["item type is not equippable"]}
    job = class_job_name(checkout, job_id)
    jobs = item.get("jobs", {})
    allowed_jobs = {name for name, enabled in jobs.items() if enabled} if isinstance(jobs, dict) else set()
    aliases = job_candidates(job)
    if job in _JOB_ALIASES:
        aliases.add("".join(piece.title() for piece in _JOB_ALIASES[job].split("_")))
    reasons = []
    if allowed_jobs and not (aliases & allowed_jobs):
        reasons.append(f"rAthena Jobs restriction excludes {job}")
    minimum = int(item.get("equiplevelmin", 0))
    maximum = int(item.get("equiplevelmax", 0))
    if base_level < minimum:
        reasons.append(f"base level {base_level} is below EquipLevelMin {minimum}")
    if maximum and base_level > maximum:
        reasons.append(f"base level {base_level} exceeds EquipLevelMax {maximum}")
    locations = item.get("locations", {})
    supported_locations = {name for name, enabled in locations.items() if enabled} if isinstance(locations, dict) else set()
    if not supported_locations:
        reasons.append("rAthena item data declares no equipment Locations")
    if requested_location and requested_location not in supported_locations:
        reasons.append(f"item Locations does not include {requested_location}")
    return {"can_equip": not reasons, "job": job, "base_level": base_level,
            "allowed_jobs": sorted(allowed_jobs), "equip_level_min": minimum,
            "equip_level_max": maximum, "locations": sorted(supported_locations),
            "requested_location": requested_location, "reasons": reasons}


def equipment_location_masks(checkout: Path) -> dict[str, int]:
    source = (checkout / "src/common/mmo.hpp").read_text(encoding="utf-8")
    enum = re.search(r"(?ms)enum equip_pos\s*:\s*uint32\s*\{(.*?)\}", source)
    if not enum:
        raise ValueError("rAthena equipment location constants could not be read")
    constants: dict[str, int] = {}
    for name, value in re.findall(r"\b(EQP_[A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)", enum.group(1)):
        constants[name] = int(value, 0)
    aliases = {
        "Right_Hand": "EQP_HAND_R", "Left_Hand": "EQP_HAND_L", "Armor": "EQP_ARMOR",
        "Shoes": "EQP_SHOES", "Garment": "EQP_GARMENT", "Right_Accessory": "EQP_ACC_R",
        "Left_Accessory": "EQP_ACC_L", "Head_Top": "EQP_HEAD_TOP", "Head_Mid": "EQP_HEAD_MID",
        "Head_Low": "EQP_HEAD_LOW", "Ammo": "EQP_AMMO", "Costume_Head_Top": "EQP_COSTUME_HEAD_TOP",
        "Costume_Head_Mid": "EQP_COSTUME_HEAD_MID", "Costume_Head_Low": "EQP_COSTUME_HEAD_LOW",
        "Costume_Garment": "EQP_COSTUME_GARMENT", "Shadow_Armor": "EQP_SHADOW_ARMOR",
        "Shadow_Weapon": "EQP_SHADOW_WEAPON", "Shadow_Shield": "EQP_SHADOW_SHIELD",
    }
    masks = {name: constants[key] for name, key in aliases.items() if key in constants}
    if "EQP_HAND_R" in constants and "EQP_HAND_L" in constants:
        masks["Both_Hand"] = constants["EQP_HAND_R"] | constants["EQP_HAND_L"]
    return masks
