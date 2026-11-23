from __future__ import annotations

import re
from pathlib import Path


def _quest_rows(path: Path) -> list[dict]:
    """Read the stable, declarative subset of rAthena's QUEST_DB YAML."""
    rows = []
    current = None
    section = None
    target = None
    for line in path.read_text(encoding="utf-8").splitlines():
        start = re.match(r"^  - Id:\s*(\d+)\s*$", line)
        if start:
            if current:
                rows.append(current)
            current = {"id": int(start.group(1)), "targets": [], "drops": []}
            section = None
            target = None
            continue
        if current is None:
            continue
        field = re.match(r"^    (Title|TimeLimit|Targets|Drops):\s*(.*?)\s*$", line)
        if field:
            key, value = field.groups()
            if key in ("Targets", "Drops"):
                section = key.lower()
                target = None
            elif value:
                current[{"Title": "title", "TimeLimit": "time_limit"}[key]] = value.strip("\"'")
            continue
        list_item = re.match(r"^      - (Mob|Count|Id|Race|Size|Element|MinLevel|MaxLevel|Location|Item|Rate):\s*(.*?)\s*$", line)
        if section and list_item:
            target = {}
            current[section].append(target)
        item = list_item or re.match(r"^        (Mob|Count|Id|Race|Size|Element|MinLevel|MaxLevel|Location|Item|Rate):\s*(.*?)\s*$", line)
        if item and target is not None:
            key, value = item.groups()
            normalized = {"Mob": "mob", "Count": "count", "Id": "target_id",
                          "Race": "race", "Size": "size", "Element": "element",
                          "MinLevel": "min_level", "MaxLevel": "max_level",
                          "Location": "location", "Item": "item", "Rate": "rate"}[key]
            value = value.strip("\"'")
            target[normalized] = int(value) if re.fullmatch(r"\d+", value) else value
    if current:
        rows.append(current)
    return rows


def load_quest_catalog(rathena: Path) -> dict:
    """Load the active Renewal quest DB plus its local import override."""
    checkout = rathena.expanduser().resolve()
    config = (checkout / "src/config/renewal.hpp").read_text(encoding="utf-8")
    mode = "renewal" if re.search(r"^\s*#define RENEWAL\s*$", config, re.M) else "pre-re"
    folder = "re" if mode == "renewal" else "pre-re"
    sources = [checkout / "db" / folder / "quest_db.yml"]
    imported = checkout / "db/import/quest_db.yml"
    if imported.is_file():
        sources.append(imported)
    merged: dict[int, dict] = {}
    for source in sources:
        for row in _quest_rows(source):
            merged[row["id"]] = {**merged.get(row["id"], {}), **row,
                "source": str(source)}
    return {"mode": mode, "sources": [str(path) for path in sources],
            "quests": sorted(merged.values(), key=lambda row: row["id"])}


def analyze_script_source(path: Path) -> dict:
    """Classify common quest script primitives without evaluating rAthena code."""
    text = path.read_text(encoding="utf-8", errors="replace")
    supported = {
        "npc_interaction": bool(re.search(r"(?im)^\s*[^\n]*\bscript\b", text)),
        "dialogue": bool(re.search(r"\b(?:mes|next|close|end)\b", text)),
        "dialogue_choices": bool(re.search(r"\bselect\s*\(", text)),
        "quest_prerequisites": bool(re.search(r"\bcheckquest\s*\(", text)),
        "inventory_prerequisites": bool(re.search(r"\bcountitem\s*\(", text)),
        "inventory_consumption": bool(re.search(r"\bdelitem\s+", text)),
        "rewards": bool(re.search(r"\b(?:getitem|getitem2)\s+", text)),
        "quest_start": bool(re.search(r"\bsetquest\s+", text)),
        "quest_completion": bool(re.search(r"\bcompletequest\s+", text)),
        "movement": bool(re.search(r"\b(?:warp|mapwarp)\s+", text)),
        "combat_spawn": bool(re.search(r"\bmonster\s+", text)),
    }
    unsupported = []
    if re.search(r"\b(?:callsub|callfunc|callshop|instance_create|instance_enter|donpcevent)\b", text):
        unsupported.append("indirect function, shop, instance, or event flow requires manual review")
    if re.search(r"\b(?:rand|rand2|areamonster|killmonster|getitem2)\b", text):
        unsupported.append("randomized, area-spawn, broad-cleanup, or extended-item behavior is not interpreted")
    return {"path": str(path), "supported_patterns": supported,
            "unsupported_or_ambiguous": unsupported,
            "server_authoritative": True}


def find_quest_scripts(rathena: Path, quest_ids: tuple[int, ...]) -> list[dict]:
    """Find scripts with direct quest-ID operations and summarize their patterns."""
    root = rathena.expanduser().resolve() / "npc"
    if not root.is_dir() or not quest_ids:
        return []
    expressions = [re.compile(rf"\b(?:setquest|checkquest|completequest|erasequest)\s*\(?\s*{quest_id}\b", re.I)
                    for quest_id in quest_ids]
    result = []
    for path in root.rglob("*.txt"):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(pattern.search(source) for pattern in expressions):
            result.append(analyze_script_source(path))
    return result
