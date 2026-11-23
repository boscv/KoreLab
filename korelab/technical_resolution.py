"""Resolve agent actions against a small, source-backed slice of the technical catalog."""
from __future__ import annotations

import hashlib
import pathlib
import re
from typing import Any

from .technical_knowledge import (DEFAULT_CATALOG, DEFAULT_FACTS, confirmed_fact_entry,
                                  load_catalog, load_confirmed_facts)


ACTION_RESOURCES = {
    "observe": [],
    "wait-position-change": [],
    "ai-mode": ["openkore.command.ai"],
    "config-set": ["openkore.command.conf"],
    "wait-reconnect": ["openkore.network.reconnect_after_disconnect"],
    "command": [],
    "move": ["objective.npc_quest", "openkore.command.move"],
    "pickup-item": ["openkore.command.take"],
    "spawn-monsters": ["openkore.command.c", "objective.combat"],
    "load-test-npc": ["openkore.command.c", "objective.npc_quest"],
    "unload-test-npc": ["openkore.command.c", "objective.npc_quest"],
    "npc-dialogue": ["objective.npc_quest", "openkore.command.talknpc"],
    "eventmacro": ["eventmacro.file_syntax", "eventmacro.commands_lifecycle"],
    "eventmacro-start": ["eventmacro.file_syntax", "eventmacro.commands_lifecycle"],
    "eventmacro-stop": ["eventmacro.commands_lifecycle"],
    "eventmacro-reload": ["eventmacro.file_syntax", "eventmacro.commands_lifecycle"],
}
COMMAND_NAMES = {"inventory": "i", "eventmacro_list": "eventMacro", "eventmacro_status": "eventMacro"}
COMMAND_GUIDES = {"inventory": "objective.items_equipment", "skills": "objective.combat"}
MACRO_COMMAND_GUIDES = {
    "move": ("objective.npc_quest", "openkore.command.move"),
    "a": ("objective.combat", "openkore.command.a"),
    "attack": ("objective.combat", "openkore.command.a"),
    "skills": ("objective.combat", "openkore.command.skills"),
    "storage": ("objective.inventory_storage", "openkore.command.storage"),
    "autostorage": ("objective.inventory_storage", "openkore.command.autostorage"),
    "i": ("objective.items_equipment", "openkore.command.i"),
    "eq": ("objective.items_equipment", "openkore.command.eq"),
    "uneq": ("objective.items_equipment", "openkore.command.uneq"),
    "talk": ("objective.npc_quest", "openkore.command.talk"),
    "talknpc": ("objective.npc_quest", "openkore.command.talknpc"),
}


def assess_operation_evidence(*, dispatched: bool, server_response: str | None = None,
                             observed_effect: bool = False,
                             persisted_effect: bool = False) -> dict[str, Any]:
    """Grade typed evidence without treating dispatch or chat echoes as success."""
    allowed = {None, "handler_success", "handler_failure", "permission_denied", "public_chat_echo"}
    if server_response not in allowed:
        raise ValueError("server_response must be a recognized evidence type")
    if persisted_effect and not observed_effect:
        raise ValueError("persisted effect requires an independently observed effect")
    if observed_effect:
        level = "persisted_state_verified" if persisted_effect else "state_effect_observed"
    elif server_response == "handler_success":
        level = "server_acknowledged"
    elif server_response in {"handler_failure", "permission_denied"}:
        level = "server_rejected"
    elif dispatched:
        level = "dispatched_only"
    else:
        level = "not_dispatched"
    return {"evidence_level": level, "dispatched": dispatched,
            "server_response": server_response, "observed_effect": observed_effect,
            "persisted_effect": persisted_effect,
            "success": bool(observed_effect)}


def _source_path(root: pathlib.Path, component: str, reference: str) -> pathlib.Path:
    relative = reference.split("#", 1)[0]
    if component == "eventMacro":
        return root / "plugins/eventMacro" / relative.removeprefix("plugins/eventMacro/")
    return root / relative


def _excerpt(path: pathlib.Path, reference: str) -> str | None:
    match = re.search(r"#(\d+)$", reference)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not match:
        return "\n".join(lines[:12])
    line = int(match.group(1))
    return "\n".join(f"{n}: {lines[n - 1]}" for n in range(max(1, line - 2), min(len(lines), line + 2) + 1))


def resolve_agent_action(action: str, *, catalog_path: pathlib.Path = DEFAULT_CATALOG,
                         facts_path: pathlib.Path = DEFAULT_FACTS,
                         openkore_root: pathlib.Path | None = None,
                         operation: str | None = None,
                         artifact: pathlib.Path | None = None,
                         supplied_arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return only action-relevant catalog entries, provenance and directed source excerpts."""
    catalog = load_catalog(catalog_path)
    facts = load_confirmed_facts(facts_path)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in catalog.get("entries", []):
        grouped.setdefault(entry.get("id"), []).append(entry)
    entries = {key: rows[0] for key, rows in grouped.items() if len(rows) == 1}
    resource_ids = list(ACTION_RESOURCES.get(action, []))
    macro_commands: list[str] = []
    if action == "command":
        command = COMMAND_NAMES.get(operation or "", operation or "")
        if operation and operation.startswith("eventmacro_auto:"):
            command = "eventMacro"
            resource_ids.append("eventmacro.commands_lifecycle")
        if operation in COMMAND_GUIDES:
            resource_ids.append(COMMAND_GUIDES[operation])
        if command == "eventMacro":
            resource_ids.append("eventmacro.commands_lifecycle")
        else:
            resource_ids.append(f"openkore.command.{command}")
    if action == "eventmacro" and artifact and artifact.is_file():
        for line in artifact.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^\s*do\s+([A-Za-z][A-Za-z0-9_-]*)\b", line, re.I)
            if not match:
                continue
            command = match.group(1).casefold()
            macro_commands.append(command)
            resource_ids.extend(MACRO_COMMAND_GUIDES.get(command, (f"openkore.command.{command}",)))
            resource_ids.append("eventmacro.command.do")
    fact_components = {"eventmacro": "eventMacro", "rathena": "rathena", "openkore": "openkore"}
    for resource_id in dict.fromkeys(resource_ids):
        if resource_id in grouped:
            continue
        prefix = resource_id.split(".", 1)[0]
        component = fact_components.get(prefix, "openkore")
        records = [record for record in facts if record.get("fact_id") == resource_id
                   and record.get("component") == component]
        for ordinal, record in enumerate(records, 1):
            entry = confirmed_fact_entry(record, ordinal)
            entry["id"] = resource_id
            grouped.setdefault(resource_id, []).append(entry)
    selected = [entries[key] for key in dict.fromkeys(resource_ids) if key in entries]
    selected.extend(entry for key in dict.fromkeys(resource_ids) if key not in entries
                    for entry in grouped.get(key, []))
    absent = [key for key in dict.fromkeys(resource_ids) if key not in grouped]
    ambiguous = [key for key in dict.fromkeys(resource_ids) if len(grouped.get(key, [])) > 1]
    source_index = {(component["component"], source["path"]): source
                    for component in catalog.get("components", []) for source in component.get("sources", [])}
    component_revisions = {component["component"]: component.get("revision")
                           for component in catalog.get("components", [])}
    findings = []
    conflicts = []
    for record in facts:
        record_source = record.get("source_reference", "").split("#", 1)[0]
        if any(record.get("component") == entry.get("component") and record_source in {
                ref.split("#", 1)[0] for ref in entry.get("source_references", [])}
               for entry in selected):
            findings.append(record)
            if record.get("conflicts_with_existing"):
                conflicts.append(record.get("fact_id"))
    fact_contracts = {}
    for entry in selected:
        for record in findings:
            revision = entry.get("version", {}).get("revision") if isinstance(entry.get("version"), dict) else entry.get("version")
            if (record.get("fact_id") == entry.get("id") and record.get("component") == entry.get("component")
                    and record.get("version") == revision == component_revisions.get(entry.get("component")) and record.get("arguments")
                    and record.get("preconditions") and not record.get("conflicts_with_existing")
                    and record.get("status") in {"source_confirmed", "documentation_only", "experimentally_verified"}):
                fact_contracts[entry["id"]] = {"fact_id": record["fact_id"], "version": record["version"],
                    "arguments": record["arguments"], "preconditions": record["preconditions"],
                    "side_effects": record.get("side_effects"), "source_reference": record["source_reference"]}
    inspected: list[dict[str, Any]] = []
    stale: list[str] = []
    for entry in selected:
        component = entry.get("component", "openkore")
        root = openkore_root
        for reference in entry.get("source_references", []):
            if not root:
                continue
            path = _source_path(root, component, reference)
            indexed = source_index.get((component, reference.split("#", 1)[0]))
            current = None
            if path.is_file():
                if indexed and indexed.get("fingerprint_type") == "setting_names_only":
                    keys = [m.group(1) for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                            if (m := re.match(r"^([A-Za-z][A-Za-z0-9_.-]*)\s+\S", line.strip()))]
                    import json
                    current = hashlib.sha256(json.dumps(keys, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                else:
                    current = hashlib.sha256(path.read_bytes()).hexdigest()
            fact_hashes = [record.get("source_sha256") for record in facts
                           if record.get("component") == component
                           and record.get("source_reference", "").split("#", 1)[0] == reference.split("#", 1)[0]
                           and record.get("fact_id") == entry.get("id")]
            expected_hash = indexed.get("sha256") if indexed else next((value for value in fact_hashes if value), None)
            unchanged = bool(expected_hash and current and current == expected_hash)
            if indexed and not unchanged:
                stale.append(reference)
            if indexed is None and root and not unchanged:
                stale.append(reference)
            if reference not in [item["reference"] for item in inspected]:
                inspected.append({"component": component, "reference": reference,
                    "sha256": current, "catalog_sha256": expected_hash,
                    "source_status": "CURRENT" if unchanged else "CHANGED_OR_UNINDEXED",
                    "excerpt": _excerpt(path, reference)})
    maturities = [entry.get("maturity", "indexed") for entry in selected]
    guide_present = any(entry.get("kind") in {"objective_guide", "language_guide", "plugin_command_guide"}
                        for entry in selected)
    guides_complete = guide_present and all(entry.get("maturity") in {"documented", "validated"}
                                            for entry in selected if entry.get("kind") in {
                                                "objective_guide", "language_guide", "plugin_command_guide"})
    indexed_contracts_complete = bool(selected) and all(entry.get("maturity") in {"documented", "validated"}
        or entry.get("id") in fact_contracts for entry in selected
        if entry.get("kind") in {"native_command", "plugin_command", "confirmed_technical_fact"})
    enough_guidance = guides_complete or indexed_contracts_complete
    status = "RESOLVED" if not absent and not ambiguous and not stale and not conflicts and (
        enough_guidance or action in {"observe", "wait-position-change"}) else "INVESTIGATION_REQUIRED"
    return {
        "schema_version": 1, "action": action, "operation": operation,
        "literal_eventmacro_commands_found": list(dict.fromkeys(macro_commands)),
        "eventmacro_dynamic_command_limit": "Only literal `do <command>` lines are indexed; variable-built or indirect command strings remain unclassified.",
        "status": status, "knowledge_assessment": "documented_contract" if enough_guidance else "partial_or_indexed",
        "catalog": {"path": str(catalog_path.resolve()), "generated_at": catalog.get("generated_at"),
                    "components": {entry.get("component"): component_revisions.get(entry.get("component"))
                                   for entry in selected}},
        "references": selected, "unresolved_resource_ids": absent, "ambiguous_resource_ids": ambiguous,
        "source_checks": inspected, "changed_or_unindexed_sources": sorted(set(stale)),
        "source_excerpts_for_investigation": [item for item in inspected if item.get("excerpt")],
        "confirmed_findings": findings, "conflicts": sorted(set(conflicts)),
        "confirmed_argument_contracts": fact_contracts,
        "arguments_known": supplied_arguments or ({"operation": operation} if operation else {}),
        "server_scoped_facts_required_separately": action in {"move", "npc-dialogue"},
        "artifact": ({"path": str(artifact.resolve()), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                      "semantics": "source supplied to native eventMacro runtime; KoreLab does not interpret it"}
                     if artifact and artifact.is_file() else None),
        "investigation": (None if status == "RESOLVED" else {
            "next": "inspect the listed source excerpts and exact references; record established syntax/conditions with technical add, then resolve again",
            "execution_guidance": "Do not infer arguments, configuration values, plugin activation, or server-specific behavior from indexed names alone."}),
        "decision_evidence": {"resource_ids": [entry.get("id") for entry in selected],
            "maturity": maturities, "catalog_versions": {entry.get("id"): entry.get("version") for entry in selected},
            "arguments_known": supplied_arguments or ({"operation": operation} if operation else {}),
            "permission_context": {"action": action, "caller_authorized": action in {"move", "npc-dialogue", "eventmacro"}},
            "source_hashes": {f"{item['component']}:{item['reference']}": item.get("sha256") for item in inspected}}
    }
