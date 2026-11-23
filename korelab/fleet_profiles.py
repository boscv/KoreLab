"""Small, source-checked native OpenKore profiles for managed combat fleets."""
from __future__ import annotations

import json
import re
from pathlib import Path


# First-job IDs follow the enum order in rAthena's src/common/mmo.hpp.
# These low-level fixtures intentionally use one legal first-job skill and a
# level-one weapon; OpenKore performs all normal movement and combat.
ARCHETYPES = (
    {"job": "Swordman", "class_id": 1, "skill": "SM_BASH", "skill_id": 5,
     "weapon": "Sword", "weapon_id": 1101, "map": "prt_fild08",
     "targets": ("Poring", "Lunatic", "Fabre")},
    {"job": "Mage", "class_id": 2, "skill": "MG_LIGHTNINGBOLT", "skill_id": 20,
     "weapon": "Rod", "weapon_id": 1601, "map": "prt_fild08",
     "targets": ("Poring", "Lunatic", "Fabre")},
    {"job": "Archer", "class_id": 3, "skill": "AC_DOUBLE", "skill_id": 46,
     "weapon": "Bow", "weapon_id": 1701, "ammo": "Arrow", "ammo_id": 1750,
     "map": "prt_fild08", "targets": ("Poring", "Lunatic", "Fabre")},
    {"job": "Acolyte", "class_id": 4, "skill": "AL_HOLYLIGHT", "skill_id": 156,
     "weapon": "Club", "weapon_id": 1501, "map": "prt_fild08",
     "targets": ("Poring", "Lunatic", "Fabre")},
    {"job": "Merchant", "class_id": 5, "skill": "MC_MAMMONITE", "skill_id": 42,
     "weapon": "Sword", "weapon_id": 1101, "map": "prt_fild08",
     "targets": ("Poring", "Lunatic", "Fabre"), "zeny": 100_000},
    {"job": "Thief", "class_id": 6, "skill": "TF_POISON", "skill_id": 52,
     "weapon": "Knife", "weapon_id": 1201, "map": "prt_fild07",
     "targets": ("Rocker",)},
)
FIRST_JOB_CLASS_IDS = {"Swordman": 1, "Mage": 2, "Archer": 3,
                       "Acolyte": 4, "Merchant": 5, "Thief": 6}


def fleet_archetype(index: int) -> dict:
    """Return a copy of the rotating six-class roster entry for a zero-based index."""
    if type(index) is not int or index < 0:
        raise ValueError("fleet agent index must be a non-negative integer")
    archetype = {**ARCHETYPES[index % len(ARCHETYPES)]}
    # Split the already route-validated roster across the two neighboring
    # Prontera fields. prt_fild07 has a large Rocker spawn and a direct native
    # route to prt_fild08; the other field has the established low-level mobs.
    if index % 2:
        archetype.update(map="prt_fild07", targets=("Rocker",))
    else:
        archetype.update(map="prt_fild08", targets=("Poring", "Lunatic", "Fabre"))
    return {**archetype, "base_level": 10,
            "job_level": 1, "skill_level": 1,
            "arrow_amount": 500 if "ammo_id" in ARCHETYPES[index % len(ARCHETYPES)] else 0}


def load_fleet_roster(path: Path) -> dict:
    """Load an editable, credential-free fleet roster JSON file."""
    source = Path(path).expanduser().resolve()
    return _load_fleet_roster(source, set())


def _load_fleet_roster(source: Path, ancestors: set[Path]) -> dict:
    if source in ancestors:
        raise ValueError("fleet roster inheritance contains a cycle")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read fleet roster {source}: {exc}") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("fleet roster must be a JSON object with version 1")
    forbidden = {"password", "credential", "credentials", "secret", "token", "login_pin",
                 "username", "userid", "account", "account_id", "character_id"}

    def check_private_fields(value):
        if isinstance(value, dict):
            if forbidden.intersection(str(key).casefold() for key in value):
                raise ValueError("fleet roster definitions cannot contain credentials or secrets")
            for child in value.values():
                check_private_fields(child)
        elif isinstance(value, list):
            for child in value:
                check_private_fields(child)

    check_private_fields(document)
    if "extends" in document:
        base_path = (source.parent / str(document["extends"])).resolve()
        if base_path.parent != source.parent:
            raise ValueError("fleet roster inheritance must remain within its directory")
        base = _load_fleet_roster(base_path, ancestors | {source})
        overrides = document.get("attribute_overrides", {})
        if not isinstance(overrides, dict):
            raise ValueError("attribute_overrides must be an object keyed by build name")
        builds = {name: dict(build) for name, build in base["builds"].items()}
        for name, attributes in overrides.items():
            if name not in builds or not isinstance(attributes, dict):
                raise ValueError(f"unknown fleet build in attribute_overrides: {name}")
            builds[name]["attributes"] = dict(attributes)
        base["id"] = document.get("id", base["id"])
        base["builds"] = builds
        return base

    roster_id = document.get("id")
    builds = document.get("builds")
    composition = document.get("composition")
    openkore_settings = document.get("openkore", {})
    if not isinstance(openkore_settings, dict):
        raise ValueError("fleet roster openkore settings must be an object")
    native_config = openkore_settings.get("config_overrides", {})
    if not isinstance(roster_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", roster_id):
        raise ValueError("fleet roster id must be a short lowercase identifier")
    if not isinstance(builds, dict) or not builds or not isinstance(composition, list):
        raise ValueError("fleet roster requires builds and composition")
    if not isinstance(native_config, dict):
        raise ValueError("fleet roster openkore.config_overrides must be an object")
    normalized = {}
    for name, raw in builds.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", name) \
                or not isinstance(raw, dict):
            raise ValueError("fleet build names and definitions must be simple objects")
        build = dict(raw)
        build["name"] = name
        targets = build.get("targets", [])
        if not isinstance(targets, list):
            raise ValueError(f"fleet build {name} targets must be a list of monster names")
        build["targets"] = targets
        if "ammo_amount" in build and type(build["ammo_amount"]) is not int:
            raise ValueError(f"fleet build {name} ammo_amount must be an integer")
        build["arrow_amount"] = build.get("ammo_amount", 0)
        if "initial_zeny" in build:
            if type(build["initial_zeny"]) is not int or build["initial_zeny"] < 0:
                raise ValueError(f"fleet build {name} initial_zeny must be a non-negative integer")
            build["zeny"] = build["initial_zeny"]
        required = ("job", "class_id", "base_level", "job_level", "attributes", "skill",
                    "skill_id", "skill_level", "weapon", "weapon_id", "lock_map", "targets")
        if any(key not in build for key in required):
            raise ValueError(f"fleet build {name} is missing required class, stat, skill, gear, or map fields")
        if (build["job"] not in FIRST_JOB_CLASS_IDS
                or type(build["class_id"]) is not int
                or build["class_id"] != FIRST_JOB_CLASS_IDS[build["job"]]):
            raise ValueError(f"fleet build {name} has an unsupported or mismatched first-job class")
        if (type(build["base_level"]) is not int or not 1 <= build["base_level"] <= 99
                or type(build["job_level"]) is not int or not 1 <= build["job_level"] <= 50
                or type(build["skill_level"]) is not int or not 1 <= build["skill_level"] <= 99):
            raise ValueError(f"fleet build {name} has an invalid first-job level or skill level")
        if any(type(build[key]) is not int or build[key] < 1
               for key in ("skill_id", "weapon_id")):
            raise ValueError(f"fleet build {name} skill and weapon IDs must be positive integers")
        if not re.fullmatch(r"[A-Z0-9_]+", str(build["skill"])):
            raise ValueError(f"fleet build {name} has an invalid skill handle")
        if not re.fullmatch(r"[a-z0-9_]+", str(build["lock_map"])):
            raise ValueError(f"fleet build {name} has an invalid lock map")
        if not build["targets"] or any(not isinstance(target, str) or not target.strip()
                                        for target in build["targets"]):
            raise ValueError(f"fleet build {name} requires at least one monster target")
        if "ammo_id" in build and (type(build["ammo_id"]) is not int or build["arrow_amount"] < 1):
            raise ValueError(f"fleet build {name} ammunition requires an item ID and positive amount")
        if "ammo_id" not in build and build["arrow_amount"] != 0:
            raise ValueError(f"fleet build {name} specifies ammunition without an item ID")
        attributes = build["attributes"]
        if not isinstance(attributes, dict) or set(attributes) != {"str", "agi", "vit", "int", "dex", "luk"} \
                or any(type(value) is not int or not 1 <= value <= 99 for value in attributes.values()):
            raise ValueError(f"fleet build {name} needs six integer base attributes from 1 to 99")
        build.setdefault("skill_distance", 9 if build["job"] in ("Mage", "Archer") else 1)
        build.setdefault("skill_condition", "zeny > 1000" if build["job"] == "Merchant" else "sp > 0")
        if type(build["skill_distance"]) is not int or not 1 <= build["skill_distance"] <= 14:
            raise ValueError(f"fleet build {name} skill distance must be between 1 and 14")
        if not re.fullmatch(r"(?:sp|zeny)\s*>\s*\d+", str(build["skill_condition"])):
            raise ValueError(f"fleet build {name} has an unsupported native skill condition")
        extra_skills = build.get("additional_skills", [])
        if not isinstance(extra_skills, list):
            raise ValueError(f"fleet build {name} additional_skills must be a list")
        extra_ids = set()
        for extra in extra_skills:
            if (not isinstance(extra, dict) or set(extra) != {"skill", "skill_id", "skill_level"}
                    or not re.fullmatch(r"[A-Z0-9_]+", str(extra.get("skill", "")))
                    or type(extra.get("skill_id")) is not int or extra["skill_id"] < 1
                    or type(extra.get("skill_level")) is not int or extra["skill_level"] < 1):
                raise ValueError(f"fleet build {name} additional skills require name, ID, and positive level")
            if extra["skill_id"] in extra_ids or extra["skill_id"] == int(build["skill_id"]):
                raise ValueError(f"fleet build {name} contains a duplicate prepared skill")
            extra_ids.add(extra["skill_id"])
        build["map"] = build["lock_map"]
        normalized[name] = build
    counts = []
    for entry in composition:
        if not isinstance(entry, dict) or entry.get("build") not in normalized \
                or type(entry.get("count")) is not int or entry["count"] < 1:
            raise ValueError("fleet composition entries require a known build and positive count")
        counts.append((entry["build"], entry["count"]))
    if not counts:
        raise ValueError("fleet roster composition cannot be empty")
    population = sum(count for _, count in counts)
    sequence = [name for rank in range(max(count for _, count in counts))
                for name, count in counts if count > rank]
    return {"id": roster_id, "builds": normalized, "sequence": sequence,
            "population": population, "openkore_config": dict(native_config),
            "source": str(source)}


def expand_fleet_roster(roster: dict, count: int) -> list[dict]:
    if type(count) is not int or count < 1:
        raise ValueError("fleet roster count must be a positive integer")
    sequence, builds = roster["sequence"], roster["builds"]
    return [{**builds[sequence[index % len(sequence)]], "roster_index": index}
            for index in range(count)]


def status_point_cost(start: int, target: int, *, renewal: bool = True) -> int:
    """Return rAthena's stat-point cost for an increase from start to target."""
    if type(start) is not int or type(target) is not int or not 1 <= start <= target <= 99:
        raise ValueError("base attributes must be integers from 1 to 99 and cannot be reduced")
    if renewal:
        return sum(2 + (value - 1) // 10 for value in range(start, target))
    return sum(1 + (value + 9) // 10 for value in range(start, target))


def uses_renewal_stat_cost(rathena: Path) -> bool:
    header = Path(rathena) / "src/config/renewal.hpp"
    if not header.is_file():
        raise RuntimeError("rAthena stat-cost definition is unavailable")
    source = header.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"(?m)^\s*#define\s+RENEWAL_STAT\b", source):
        raise RuntimeError("managed fleet attribute builds require the source-verified RE stat-cost mode")
    return True


def skill_config_block(archetype: dict) -> str:
    """Render one ordinary OpenKore attackSkillSlot block for a prepared skill."""
    skill = str(archetype["skill"])
    distance = int(archetype.get("skill_distance", 9 if archetype["job"] in ("Mage", "Archer") else 1))
    condition = str(archetype.get("skill_condition", "zeny > 1000" if archetype["job"] == "Merchant" else "sp > 0"))
    return (f"\nattackSkillSlot {skill} {{\n"
            f"    lvl {int(archetype['skill_level'])}\n"
            f"    dist {distance}\n    maxDist {distance}\n"
            f"    {condition}\n    maxAttempts 3\n}}\n")


def validate_fleet_archetypes(rathena: Path, profiles=None) -> None:
    """Check every hard-coded first-job skill and item against the active RE DB."""
    root = Path(rathena)
    db = root / "db/re"
    enum = (root / "src/common/mmo.hpp").read_text(encoding="utf-8")
    enum_match = re.search(r"enum\s+e_job\s*\{(.*?)\};", enum, re.S)
    if not enum_match:
        raise RuntimeError("rAthena job enum is unavailable; cannot validate fleet classes")
    symbols = re.findall(r"\bJOB_(?:NOVICE|SWORDMAN|MAGE|ARCHER|ACOLYTE|MERCHANT|THIEF)\b",
                         enum_match.group(1))
    expected = ["JOB_NOVICE", "JOB_SWORDMAN", "JOB_MAGE", "JOB_ARCHER",
                "JOB_ACOLYTE", "JOB_MERCHANT", "JOB_THIEF"]
    if symbols[:len(expected)] != expected:
        raise RuntimeError("rAthena first-job enum differs from the managed fleet roster")

    skill_db = (db / "skill_db.yml").read_text(encoding="utf-8")
    skill_ids = {name: int(skill_id) for skill_id, name in re.findall(
        r"(?ms)^  - Id:\s*(\d+)\s*\n    Name:\s*([A-Z0-9_]+)", skill_db)}
    skill_records = {int(match.group(1)): match.group(0) for match in re.finditer(
        r"(?ms)^  - Id:\s*(\d+)\s*\n.*?(?=^  - Id:|\Z)", skill_db)}
    skill_tree = (db / "skill_tree.yml").read_text(encoding="utf-8")
    item_sections = re.split(r"(?=^  - Id:)", (db / "item_db_equip.yml").read_text(encoding="utf-8"), flags=re.M)
    item_records = {int(match.group(1)): section for section in item_sections
                    if (match := re.match(r"  - Id:\s*(\d+)", section))}
    ammo_sections = re.split(r"(?=^  - Id:)", (db / "item_db_etc.yml").read_text(encoding="utf-8"), flags=re.M)
    ammo_records = {int(match.group(1)): section for section in ammo_sections
                    if (match := re.match(r"  - Id:\s*(\d+)", section))}
    for profile in (profiles if profiles is not None else ARCHETYPES):
        if skill_ids.get(profile["skill"]) != profile["skill_id"]:
            raise RuntimeError(f"rAthena skill ID changed for {profile['skill']}")
        skill_record = skill_records.get(int(profile["skill_id"]), "")
        maximum = re.search(r"(?m)^    MaxLevel:\s*(\d+)\s*$", skill_record)
        if not maximum or not 1 <= int(profile["skill_level"]) <= int(maximum.group(1)):
            raise RuntimeError(f"rAthena skill level is invalid for {profile['skill']}")
        job_block = re.search(rf"(?ms)^  - Job: {re.escape(profile['job'])}\s*$.*?(?=^  - Job:|\Z)",
                              skill_tree)
        if not job_block or not re.search(rf"(?m)^      - Name: {re.escape(profile['skill'])}\s*$",
                                          job_block.group(0)):
            raise RuntimeError(f"{profile['skill']} is not in the {profile['job']} skill tree")
        novice_block = re.search(r"(?ms)^  - Job: Novice\s*$.*?(?=^  - Job:|\Z)", skill_tree)
        for extra in profile.get("additional_skills", []):
            if skill_ids.get(extra["skill"]) != extra["skill_id"]:
                raise RuntimeError(f"rAthena skill ID changed for {extra['skill']}")
            extra_record = skill_records.get(extra["skill_id"], "")
            extra_maximum = re.search(r"(?m)^    MaxLevel:\s*(\d+)\s*$", extra_record)
            if not extra_maximum or extra["skill_level"] > int(extra_maximum.group(1)):
                raise RuntimeError(f"rAthena skill level is invalid for {extra['skill']}")
            inherited_novice = (extra["skill"] == "NV_BASIC" and novice_block and
                re.search(r"(?m)^      - Name: NV_BASIC\s*$", novice_block.group(0)) and
                re.search(r"(?m)^    Inherit:\s*$", job_block.group(0)) and
                re.search(r"(?m)^      Novice: true\s*$", job_block.group(0)))
            direct_skill = re.search(rf"(?m)^      - Name: {re.escape(extra['skill'])}\s*$",
                                     job_block.group(0))
            if not (inherited_novice or direct_skill):
                raise RuntimeError(f"{extra['skill']} is not available to the {profile['job']} build")
        skill_entry = re.search(rf"(?ms)^      - Name: {re.escape(profile['skill'])}\s*$.*?"
                                r"(?=^      - Name:|^  - Job:|\Z)", job_block.group(0))
        if not skill_entry or "Requires:" in skill_entry.group(0):
            raise RuntimeError(f"fleet skill {profile['skill']} has unresolved skill prerequisites")
        for item_id, item_job, source in ((profile["weapon_id"], profile["job"], item_records),):
            block = source.get(item_id, "")
            if not block or not re.search(rf"(?m)^      {re.escape(item_job)}: true\s*$", block):
                raise RuntimeError(f"rAthena item {item_id} is not valid for {profile['job']}")
        if "ammo_id" in profile:
            ammo = ammo_records.get(int(profile["ammo_id"]), "")
            if not ammo or not re.search(r"(?m)^      Archer: true\s*$", ammo):
                raise RuntimeError("rAthena arrow fixture is missing or not valid for Archers")


def validate_fleet_maps(openkore: Path, rathena: Path, profiles=None) -> None:
    """Preflight the selected maps against server index, client fields and mob scripts."""
    client_fields = Path(openkore) / "fields"
    map_index = (Path(rathena) / "db/map_index.txt").read_text(encoding="utf-8")
    spawn_root = Path(rathena) / "npc/re/mobs/fields"
    spawn_text = "\n".join(path.read_text(encoding="utf-8", errors="replace")
        for path in spawn_root.glob("*.txt"))
    profiles = profiles if profiles is not None else [
        {**profile, "lock_map": profile.get("map")} for profile in ARCHETYPES]
    for map_name in sorted({profile.get("lock_map", profile.get("map")) for profile in profiles}):
        if not re.search(rf"(?m)^\s*{re.escape(map_name)}\s*$", map_index):
            raise RuntimeError(f"rAthena map index does not contain fleet lock map {map_name}")
        if not any((client_fields / f"{map_name}{suffix}").is_file()
                   for suffix in (".fld", ".fld2.gz")):
            raise RuntimeError(f"OpenKore field data is missing for fleet lock map {map_name}")
        lines = [line for line in spawn_text.splitlines()
                 if line.startswith(map_name + ",") and "\tmonster\t" in line]
        if not lines:
            raise RuntimeError(f"rAthena RE field scripts contain no ordinary monster spawn on {map_name}")
        for target in {name for profile in profiles
                       if profile.get("lock_map", profile.get("map")) == map_name
                       for name in profile["targets"]}:
            if not any(re.search(rf"\tmonster\t{re.escape(target)}\t", line) for line in lines):
                raise RuntimeError(f"rAthena RE field scripts do not spawn target {target} on {map_name}")

