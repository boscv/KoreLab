"""Version-aware, source-backed technical capability index for installed RO tools.

Discovery is intentionally lexical and registry-oriented. It is not a Perl/C++ parser,
and registry-only discoveries are never presented as fully understood capabilities.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import time
from typing import Any
from pathlib import PureWindowsPath
from .paths import TECHNICAL_DIR


SCHEMA_VERSION = 1
DEFAULT_CATALOG = TECHNICAL_DIR / "catalog.json"
DEFAULT_FACTS = TECHNICAL_DIR / "confirmed.jsonl"
_STOPWORDS = {"a", "an", "and", "for", "from", "in", "is", "of", "or", "the", "to", "with", "this", "that"}
_FULLY_DOCUMENTED_STATUSES = {
    "implementation_reviewed", "objective_composition_reviewed",
    "registry_and_manual_indexed", "registry_and_configuration_indexed",
}


def _knowledge_maturity(entry: dict[str, Any]) -> tuple[str, list[str]]:
    """Report the strongest maturity supported by evidence present on the entry."""
    basis: list[str] = []
    if entry.get("source_references"):
        basis.append("source_reference")
    documented_statuses = {
        "implementation_reviewed", "implementation_reviewed_partial",
        "objective_composition_reviewed", "registry_and_manual_indexed",
        "registry_and_configuration_indexed",
    }
    if entry.get("documentation_status") in documented_statuses:
        basis.append("reviewed_documentation")
    if (entry.get("kind") == "confirmed_technical_fact"
            and entry.get("verification_status") in {
                "source_confirmed", "documentation_only", "experimentally_verified"}):
        basis.append("recorded_finding")
    evidence = entry.get("validation_evidence")
    if isinstance(evidence, dict) and all(evidence.get(key) for key in (
            "reference", "environment", "conditions")):
        entry_version = entry.get("version")
        entry_revision = entry_version.get("revision") if isinstance(entry_version, dict) else entry_version
        if entry_revision and evidence.get("version") == entry_revision:
            basis.append("runtime_validation_evidence")
            return "validated", basis
        basis.append("validation_version_unmatched")
    if "reviewed_documentation" in basis or "recorded_finding" in basis:
        return "documented", basis
    return "indexed", basis or ["no_supporting_reference"]


def _apply_maturity(entry: dict[str, Any]) -> dict[str, Any]:
    maturity, basis = _knowledge_maturity(entry)
    entry["maturity"] = maturity
    entry["maturity_basis"] = basis
    return entry


def _count_fully_documented(entries: list[dict[str, Any]]) -> int:
    return sum(entry.get("documentation_status") in _FULLY_DOCUMENTED_STATUSES for entry in entries)


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprinted_source(path: pathlib.Path, root: pathlib.Path) -> dict[str, str]:
    relative = path.relative_to(root).as_posix()
    if relative in {"control/config.txt", "control/timeouts.txt"}:
        keys = []
        for line in _lines(path):
            stripped = line.strip()
            match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*)\s+\S", stripped) if stripped and not stripped.startswith(("#", ";")) else None
            if match:
                keys.append(match.group(1))
        payload = json.dumps(keys, ensure_ascii=False, separators=(",", ":")).encode()
        return {"path": relative, "sha256": hashlib.sha256(payload).hexdigest(),
                "fingerprint_type": "setting_names_only"}
    return {"path": relative, "sha256": _sha(path), "fingerprint_type": "source_content"}


def _git_info(root: pathlib.Path) -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            return subprocess.run(["git", "-C", str(root), *args], check=True,
                capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    return {"revision": git("rev-parse", "--short=12", "HEAD"),
            "working_tree_modified": bool(git("status", "--porcelain", "--untracked-files=no"))}


def _ref(root: pathlib.Path, path: pathlib.Path, line: int | None = None) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return f"{relative}#{line}" if line else relative


def _lines(path: pathlib.Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _openkore_command_registry(root: pathlib.Path) -> tuple[list[dict[str, Any]], list[pathlib.Path]]:
    path = root / "src/Commands.pm"
    lines = _lines(path)
    source_text = "\n".join(lines)
    entries = []
    header = re.compile(r"^\s*\['([A-Za-z][A-Za-z0-9_-]*)'\s*,")
    starts = [(i, match) for i, line in enumerate(lines) if (match := header.match(line))]
    for position, (index, match) in enumerate(starts):
        # Bound each extraction to this one registry entry to prevent neighboring help leakage.
        end = starts[position + 1][0] if position + 1 < len(starts) else min(len(lines), index + 80)
        chunk = "\n".join(lines[index:end])
        description_match = re.search(r"\bT\(\s*\"([^\"]+)\"\s*\)", chunk)
        syntax = re.findall(r"\[\s*(?:T\(\s*)?\"([^\"]*<[^\"]*)\"", chunk)
        callback = re.search(r"&([A-Za-z_][A-Za-z0-9_]*)", chunk)
        refs = [_ref(root, path, index + 1)]
        if callback:
            implementation = re.search(rf"^\s*sub\s+{re.escape(callback.group(1))}\b", source_text, re.M)
            if implementation:
                implementation_line = source_text.count("\n", 0, implementation.start()) + 1
                refs.append(_ref(root, path, implementation_line))
        entries.append({"id": f"openkore.command.{match.group(1)}",
            "component": "openkore", "kind": "native_command", "name": match.group(1),
            "purpose": description_match.group(1) if description_match else None,
            "syntax_hints": list(dict.fromkeys(syntax)), "arguments": None,
            "preconditions": None, "side_effects": "unknown; inspect implementation before execution",
            "verification_status": "source_discovered",
            "knowledge_provenance": "source_registry_and_embedded_help" if description_match else "source_registry_only",
            "documentation_status": "registry_help" if description_match else "registry_only",
            "implementation_callback": callback.group(1) if callback else None,
            "source_references": refs})
    return entries, [path]


def _plugin_registry(root: pathlib.Path) -> tuple[list[dict[str, Any]], list[pathlib.Path]]:
    base = root / "plugins"
    if not base.is_dir():
        return [], []
    skipped = {"needs-review", "test", "tests", ".git"}
    plugin_files = [p for p in sorted(base.rglob("*.pl"))
                    if not skipped.intersection(p.relative_to(base).parts)]
    plugins: dict[str, dict[str, Any]] = {}
    plugin_commands: dict[tuple[str, str], dict[str, Any]] = {}
    plugin_hooks: dict[tuple[str, str], dict[str, Any]] = {}
    for path in plugin_files:
        try:
            lines = _lines(path)
        except OSError:
            continue
        text = "\n".join(lines)
        plugin_match = re.search(r"Plugins::register\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)", text)
        plugin_name = plugin_match.group(1) if plugin_match else path.parent.name
        if plugin_match:
            line = text[:plugin_match.start()].count("\n") + 1
            plugins.setdefault(plugin_name, {"id": f"openkore.plugin.{plugin_name}",
                "component": "openkore", "kind": "plugin", "name": plugin_name,
                "purpose": plugin_match.group(2) or None, "verification_status": "source_discovered",
                "knowledge_provenance": "plugin_source_registration",
                "documentation_status": "plugin_registration", "side_effects": "plugin-specific; inspect hooks and commands",
                "source_references": [_ref(root, path, line)]})
        for match in re.finditer(r"Commands::register\s*\(", text):
            snippet = text[match.start():match.start() + 6000]
            for cmd in re.finditer(r"\[\s*['\"]([A-Za-z][A-Za-z0-9_-]*)['\"]\s*,", snippet):
                command = cmd.group(1)
                line = text[:match.start() + cmd.start()].count("\n") + 1
                key = (plugin_name, command)
                plugin_commands.setdefault(key, {"id": f"openkore.plugin_command.{plugin_name}.{command}",
                    "component": "openkore", "kind": "plugin_command", "name": command,
                    "plugin": plugin_name, "purpose": None, "arguments": None,
                    "preconditions": None, "side_effects": "unknown; inspect plugin implementation",
                    "verification_status": "source_discovered", "documentation_status": "registry_only",
                    "knowledge_provenance": "plugin_source_registry",
                    "source_references": [_ref(root, path, line)]})
        for match in re.finditer(r"Plugins::addHooks\s*\(", text):
            snippet = text[match.start():match.start() + 10000]
            for hook_match in re.finditer(r"\[\s*['\"]([A-Za-z][A-Za-z0-9_.:-]*)['\"]\s*,", snippet):
                hook = hook_match.group(1)
                line = text[:match.start() + hook_match.start()].count("\n") + 1
                key = (plugin_name, hook)
                plugin_hooks.setdefault(key, {"id": f"openkore.plugin_hook.{plugin_name}.{hook}",
                    "component": "openkore", "kind": "plugin_hook", "name": hook,
                    "plugin": plugin_name, "purpose": None, "side_effects": "hook effects depend on callback implementation",
                    "verification_status": "source_discovered", "documentation_status": "hook_registration_only",
                    "knowledge_provenance": "plugin_source_hook_registration",
                    "source_references": [_ref(root, path, line)]})
    return list(plugins.values()) + list(plugin_commands.values()) + list(plugin_hooks.values()), plugin_files


def _config_keys(root: pathlib.Path) -> tuple[list[dict[str, Any]], list[pathlib.Path]]:
    default_paths = [root / "control/config.txt", root / "control/timeouts.txt"]
    entries: dict[str, dict[str, Any]] = {}
    source_paths = sorted([p for p in (root / "src").rglob("*.pm") if not {"auto", "deps"}.intersection(p.parts)] +
                          [p for p in (root / "src").rglob("*.pl") if not {"auto", "deps"}.intersection(p.parts)])
    for path in default_paths:
        if path.is_file():
            source_paths.append(path)
    for path in source_paths:
        kind = "timeout_key" if "timeouts.txt" in path.name else "configuration_key"
        for line_num, line in enumerate(_lines(path), 1):
            if path.suffix in (".pm", ".pl"):
                # Deterministically discover literal config/timeout hash keys in source.
                key_re = re.compile(r"\$(config|timeout)\s*\{\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))")
                for match in key_re.finditer(line):
                    namespace = match.group(1)
                    key = next(group for group in match.groups()[1:] if group is not None)
                    entry_kind = "timeout_key" if namespace == "timeout" else "configuration_key"
                    entry_id = f"openkore.{ 'timeout' if entry_kind == 'timeout_key' else 'config' }.{key}"
                    entry = entries.setdefault(entry_id, {"id": entry_id, "component": "openkore",
                        "kind": entry_kind, "name": key, "purpose": None, "value": None,
                        "accepted_values": None, "side_effects": "unknown; inspect configuration consumers",
                        "verification_status": "source_discovered", "documentation_status": "source_key_reference",
                        "knowledge_provenance": "source_config_reference", "source_references": []})
                    ref = _ref(root, path, line_num)
                    if ref not in entry["source_references"]:
                        entry["source_references"].append(ref)
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*)\s+\S", stripped)
            if not match:
                continue
            key = match.group(1)
            entry_id = f"openkore.{ 'timeout' if kind == 'timeout_key' else 'config' }.{key}"
            entry = entries.setdefault(entry_id, {"id": entry_id, "component": "openkore",
                "kind": kind, "name": key, "purpose": None, "value": None,
                "accepted_values": None, "side_effects": "unknown; see configuration documentation",
                "verification_status": "source_discovered", "documentation_status": "key_only",
                "knowledge_provenance": "active_local_config_key_only", "source_references": []})
            ref = _ref(root, path, line_num)
            if ref not in entry["source_references"]:
                entry["source_references"].append(ref)
    return list(entries.values()), list(dict.fromkeys(p for p in source_paths if p.is_file()))


def _rathena_registry(root: pathlib.Path) -> tuple[list[dict[str, Any]], list[pathlib.Path]]:
    entries: list[dict[str, Any]] = []
    sources: list[pathlib.Path] = []
    at_path = root / "src/map/atcommand.cpp"
    script_path = root / "src/map/script.cpp"
    at_doc = root / "doc/atcommands.txt"
    script_doc = root / "doc/script_commands.txt"
    at_docs = {}
    if at_doc.is_file():
        sources.append(at_doc)
        for n, line in enumerate(_lines(at_doc), 1):
            for name in re.findall(r"(?m)^\s*@([A-Za-z][A-Za-z0-9_]*)", line):
                at_docs.setdefault(name.casefold(), n)
    script_docs = {}
    if script_doc.is_file():
        sources.append(script_doc)
        for n, line in enumerate(_lines(script_doc), 1):
            for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", line):
                script_docs.setdefault(name.casefold(), n)
    if at_path.is_file():
        sources.append(at_path)
        lines = _lines(at_path)
        implementation_lines = {m.group(1): n for n, line in enumerate(lines, 1)
                                if (m := re.search(r"ACMD_FUNC\(\s*([A-Za-z][A-Za-z0-9_]*)", line))}
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                continue
            match = re.search(r"ACMD_DEF2?R?\(\s*(?:\"([^\"]+)\"\s*,\s*)?([A-Za-z][A-Za-z0-9_]*)", line)
            if match:
                alias, implementation = match.groups()
                name = alias or implementation
                doc_line = at_docs.get(name.casefold())
                entries.append({"id": f"rathena.atcommand.{name}", "component": "rathena",
                    "kind": "administrative_command", "name": name, "implementation": implementation,
                    "purpose": None, "syntax": None, "preconditions": None,
                    "side_effects": "potentially state-changing or privileged; permissions vary by group",
                    "verification_status": "source_discovered",
                    "documentation_status": "registry_plus_documentation_reference" if doc_line else "registry_only",
                    "knowledge_provenance": "source_registry_and_documentation_reference" if doc_line else "rathena_source_registry",
                    "source_references": [_ref(root, at_path, i + 1)] +
                        ([_ref(root, at_path, implementation_lines[implementation])] if implementation in implementation_lines else []) +
                        ([_ref(root, at_doc, doc_line)] if doc_line else [])})
    if script_path.is_file():
        sources.append(script_path)
        lines = _lines(script_path)
        implementation_lines = {m.group(1): n for n, line in enumerate(lines, 1)
                                if (m := re.search(r"BUILDIN_FUNC\(\s*([A-Za-z][A-Za-z0-9_]*)", line))}
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                continue
            match = re.search(r"BUILDIN_DEF2?\(\s*([A-Za-z][A-Za-z0-9_]*)\s*,\s*(?:\"([^\"]+)\"\s*,\s*)?\"([^\"]*)\"", line)
            if match:
                implementation, alias, signature = match.groups()
                name = alias or implementation
                doc_line = script_docs.get(name.casefold())
                entries.append({"id": f"rathena.script_builtin.{name}", "component": "rathena",
                    "kind": "script_builtin", "name": name, "implementation": implementation,
                    "purpose": None, "argument_signature": signature,
                    "preconditions": None, "side_effects": "varies by builtin; inspect implementation and script context",
                    "verification_status": "source_discovered",
                    "documentation_status": "registry_signature_plus_documentation_reference" if doc_line else "registry_signature",
                    "knowledge_provenance": "source_registry_and_documentation_reference" if doc_line else "rathena_source_registry_signature",
                    "source_references": [_ref(root, script_path, i + 1)] +
                        ([_ref(root, script_path, implementation_lines[implementation])] if implementation in implementation_lines else []) +
                        ([_ref(root, script_doc, doc_line)] if doc_line else [])})
    # Custom command includes can add live registry entries without changing the core list.
    for include in (root / "src/custom/atcommand_def.inc", root / "src/custom/atcommand.inc"):
        if include.is_file():
            sources.append(include)
            for i, line in enumerate(_lines(include)):
                if line.lstrip().startswith("#"):
                    continue
                match = re.search(r"ACMD_DEF2?R?\(\s*(?:\"([^\"]+)\"\s*,\s*)?([A-Za-z][A-Za-z0-9_]*)", line)
                if match:
                    alias, implementation = match.groups()
                    name = alias or implementation
                    entries.append({"id": f"rathena.atcommand.{name}", "component": "rathena",
                        "kind": "administrative_command", "name": name, "implementation": implementation,
                        "purpose": None, "syntax": None, "preconditions": None,
                        "side_effects": "potentially state-changing or privileged; inspect local custom implementation",
                        "verification_status": "source_discovered", "documentation_status": "registry_only",
                        "source_references": [_ref(root, include, i + 1)]})
    permission_refs = [_ref(root, p) for p in (root / "conf/atcommands.yml", root / "conf/groups.yml") if p.is_file()]
    if script_path.is_file():
        entries.append({"id": "rathena.script_interface", "component": "rathena", "kind": "script_api_guide",
            "name": "rAthena NPC script builtins", "purpose": "NPC scripts call registered server builtins; registry signatures and command documentation are indexed separately.",
            "examples": ["mes", "select", "next", "close", "getitem", "checkquest", "setquest", "completequest"],
            "preconditions": ["script runs in the server NPC/script engine", "builtin argument signature and script context are valid"],
            "observable_outcomes": ["server script execution, NPC dialogue, state changes and rewards"],
            "side_effects": "many builtins mutate character, inventory, map or quest state",
            "limitations": ["this catalog does not interpret NPC script control flow or validate server-specific prerequisites"],
            "verification_status": "source_confirmed", "documentation_status": "registry_and_manual_indexed",
            "knowledge_provenance": "rathena_source_and_documentation_index",
            "source_references": [_ref(root, script_path, 27930), _ref(root, script_doc) if script_doc.is_file() else _ref(root, script_path)]})
    if at_path.is_file():
        entries.append({"id": "rathena.atcommand_interface", "component": "rathena", "kind": "administrative_command_guide",
            "name": "rAthena administrative commands and permissions", "purpose": "Commands register through the atcommand registry; active names and permissions are filtered by group/configuration.",
            "preconditions": ["map-server command exists in the active registry", "account group has permission in active groups/atcommands configuration"],
            "observable_outcomes": ["server command response or permission rejection"],
            "side_effects": "administrative commands can change character/world/server state; treat undiscovered effects as privileged",
            "limitations": ["registry presence does not imply the current account can invoke the command", "local custom command includes can differ by checkout"],
            "verification_status": "source_confirmed", "documentation_status": "registry_and_configuration_indexed",
            "knowledge_provenance": "rathena_source_and_local_configuration_index",
            "source_references": [_ref(root, at_path, 11496), _ref(root, at_doc) if at_doc.is_file() else _ref(root, at_path)] + permission_refs})
    return entries, sources


def _eventmacro_entries(root: pathlib.Path) -> tuple[list[dict[str, Any]], list[pathlib.Path]]:
    base = root / "plugins/eventMacro"
    if not base.is_dir():
        return [], []
    files = [p for p in sorted(base.rglob("*.pm")) + sorted(base.glob("*.pl"))
             if not {"test", "tests"}.intersection(p.relative_to(base).parts)]
    condition_dir = base / "eventMacro/Condition"
    conditions = sorted(p.stem for p in condition_dir.glob("*.pm")) if condition_dir.is_dir() else []
    condition_type_dir = base / "eventMacro/Conditiontypes"
    condition_types = sorted(p.stem for p in condition_type_dir.glob("*.pm")) if condition_type_dir.is_dir() else []
    parser = base / "eventMacro/FileParser.pm"
    runner = base / "eventMacro/Runner.pm"
    data = base / "eventMacro/Data.pm"
    plugin = base / "eventMacro.pl"
    details = [
        {"id": "eventmacro.file_syntax", "component": "eventMacro", "kind": "language_guide",
         "name": "macro, automacro, and sub blocks", "purpose": "Parse named automation blocks and their command bodies.",
         "syntax": ["macro NAME { ... }", "automacro NAME { condition value; parameter value; call MACRO }", "sub NAME { ... }"],
         "preconditions": ["eventMacro plugin loaded", "configured eventMacro_file is readable"],
         "observable_outcomes": ["plugin logs parser/load errors", "eventMacro list/status expose parsed entries"],
         "known_failure_modes": ["if the configured file cannot be loaded at startup, the plugin unloads", "unknown top-level block types are ignored with parser errors", "duplicate macro names are flagged during parsing"],
         "side_effects": "automacro can start executable macro bodies when its conditions trigger",
         "examples": ["macro travel { do move 100 100; pause 2; }"] ,
         "limitations": ["syntax is custom; this index is a source-backed guide, not a validator or interpreter", "Perl sub blocks execute code unless Settings lockdown prevents it"],
         "verification_status": "source_confirmed", "documentation_status": "implementation_reviewed",
         "source_references": [_ref(root, parser), _ref(root, runner), _ref(root, data)]},
        {"id": "eventmacro.conditions", "component": "eventMacro", "kind": "condition_catalog",
         "name": "automacro conditions", "purpose": "Conditions are implemented by condition modules and can be state checks or event triggers.",
         "syntax": ["condition_key value", "condition_key { multiple values }"], "arguments": None,
         "preconditions": ["condition module exists in installed eventMacro tree", "condition's hooks/events and value parser accept the supplied form"],
         "observable_outcomes": ["eventMacro auto NAME reports condition status", "eventMacro status reports automacro state"],
         "side_effects": "conditions normally observe state; triggered call starts macro automation",
         "examples": [], "limitations": ["condition names alone do not establish accepted arguments; inspect each module"],
         "discovered_conditions": conditions, "discovered_condition_types": condition_types,
         "verification_status": "source_confirmed",
         "documentation_status": "condition_names_indexed_arguments_not_fully_documented",
         "source_references": [_ref(root, condition_dir), _ref(root, condition_type_dir)]},
        {"id": "eventmacro.commands_lifecycle", "component": "eventMacro", "kind": "plugin_command_guide",
         "name": "eventMacro commands and lifecycle", "purpose": "Control native macro execution and automacro evaluation.",
         "syntax": ["eventMacro MACRO", "eventMacro list", "eventMacro status [macro|automacro]", "eventMacro stop|pause|unpause", "eventMacro check [force_stop|force_start|resume]", "eventMacro var_get|var_set", "eventMacro enable|disable", "eventMacro include [on|off|list]"],
         "preconditions": ["plugin registered native commands eventMacro and emacro", "eventMacro source parsed"],
         "observable_outcomes": ["plugin messages and status", "macro execution through Runner and OpenKore Commands::run"],
         "known_failure_modes": ["requested macro or automacro name not found", "native command output may report parser or execution errors", "nested eventMacro and ai clear commands are rejected from a macro body"],
         "side_effects": "running/enabling macros may move, fight, consume items, change settings, or invoke any supported OpenKore command",
         "relevant_configuration": {"eventMacro_file": {"default": "eventMacros.txt", "behavior": "file loaded by plugin; modifying this config key reloads the source"},
             "eventMacro_orphans": {"default": "terminate", "accepted_values": ["terminate", "terminate_last_call", "reregister", "reregister_safe"]},
             "eventMacro_CheckOnAI": {"default": "auto", "accepted_values": ["auto", "manual", "off"]},
             "eventMacro_notWhenInQueue": {"default": "", "behavior": "comma-separated AI queue states that prevent automacro execution"},
             "timeout.eventMacro_delay": {"default": 1, "behavior": "default delay between macro commands"}},
         "examples": ["eventMacro list", "eventMacro korelab_probe", "eventMacro stop"],
         "limitations": ["native command interface may reload/control source but does not establish game-objective success", "macro execution errors depend on source/plugin logs"],
         "verification_status": "source_confirmed",
         "documentation_status": "implementation_reviewed",
         "source_references": [_ref(root, plugin), _ref(root, runner)]},
        {"id": "eventmacro.variables_control", "component": "eventMacro", "kind": "language_guide",
         "name": "macro variables and control flow", "purpose": "Runner parses control-flow blocks, scalar/array/hash interpolation, built-in keywords, labels, subcalls, and command statements.",
         "syntax": ["$scalar", "@array", "%hash", "$array[index]", "$hash{key}", "&config(key)", "if (...) { ... } else { ... }", "while (...) { ... }", "switch (...) { case ... }", ":label", "do OpenKore command", "set ...", "call MACRO [arguments]", "pause SECONDS", "stop"],
         "built_in_keywords": ["arg", "config", "defined", "eval", "exists", "inventory", "item", "monster", "npc", "player", "random", "questStatus", "storage", "vender", "other collection helpers in installed Data.pm"],
         "preconditions": ["statement must be recognized by installed Runner implementation"],
         "observable_outcomes": ["macro status, logs and effects of executed native commands"],
         "side_effects": "commands and embedded Perl subs may perform arbitrary client-side actions",
         "examples": [], "limitations": ["keyword list is representative, not exhaustive; installed Data.pm is authoritative", "eventMacro command execution rejects nested eventMacro commands and ai clear", "not all parser keywords or utility functions are summarized here; inspect source links for edge cases"],
         "verification_status": "source_confirmed", "documentation_status": "implementation_reviewed_partial",
         "source_references": [_ref(root, runner), _ref(root, data), _ref(root, base / "eventMacro/Utilities.pm")]},
    ]
    return details, files


def _plugin_api_guide(root: pathlib.Path) -> dict[str, Any]:
    return {"id": "openkore.plugin_api", "component": "openkore", "kind": "plugin_api_guide",
        "name": "OpenKore plugin registration and hooks", "purpose": "Plugins register lifecycle ownership, hooks, and console commands using native APIs.",
        "interfaces": ["Plugins::register", "Plugins::addHooks", "Plugins::callHook", "Commands::register", "Commands::run"],
        "preconditions": ["plugin source is installed/enabled by OpenKore configuration", "hook name and callback contract match the target version"],
        "observable_outcomes": ["plugin load/unload messages", "registered commands appear in command handling", "hook callback runs at its event"],
        "side_effects": "depends on plugin callback; command and hook effects are arbitrary",
        "examples": [], "limitations": ["source discovery does not establish that a plugin is enabled", "hook callback argument schemas must be checked at call sites"],
        "verification_status": "source_confirmed", "documentation_status": "implementation_reviewed_partial",
        "knowledge_provenance": "installed_plugin_api_implementation_review",
        "source_references": [_ref(root, root / "src/Plugins.pm"), _ref(root, root / "src/Commands.pm")]}


OBJECTIVE_GUIDES = [
    {"id": "objective.inventory_storage", "component": "openkore", "kind": "objective_guide",
     "name": "store selected inventory items", "terms": ["storage", "store", "kafra", "inventory", "put away"],
     "purpose": "Use OpenKore's native storage transaction commands or its configured auto-storage behavior.",
     "related_capabilities": ["openkore.command.storage", "openkore.command.autostorage", "openkore.config.storageAuto", "openkore.command.move", "openkore.command.talk", "openkore.command.talknpc"],
     "preconditions": ["storage service is available on the server", "character can use it", "OpenKore has the required NPC/storage interaction configured"],
     "side_effects": "item location changes; auto-storage can transfer every item selected by configuration rules",
     "relevant_configuration": {"storageAuto": "enable automatic transfer", "storageAuto_npc": "NPC locator/name used by storage behavior", "storageAuto_distance": "distance threshold used by auto-storage", "storageAuto_npc_type": "NPC interaction type", "storageAuto_npc_steps": "dialogue sequence", "storageAuto_password": "storage password when required", "storageAuto_keepOpen": "whether storage remains open", "storageAuto_useChatCommand": "use configured chat command for storage", "storageAuto_useItem": "use configured item to access storage", "storageAuto_onStart": "attempt storage on startup"},
     "examples": ["storage add <inventory item #> [<amount>]", "autostorage"],
     "limitations": ["NPC location, dialogue and server restrictions belong to server knowledge", "storageAuto rules are configuration-specific; inspect active settings before use"],
     "verification_status": "source_confirmed", "documentation_status": "objective_composition_reviewed",
     "source_references": ["src/Commands.pm#615", "src/Commands.pm#98", "src/Commands.pm#1358", "src/AI.pm#661", "src/Interface/Wx.pm#1200", "control/config.txt"]},
    {"id": "objective.combat", "component": "openkore", "kind": "objective_guide",
     "name": "combat and skills", "terms": ["combat", "fight", "attack", "monster", "skill", "cast"],
     "purpose": "Select a visible monster and use native attack/skill commands or configured OpenKore AI.",
     "related_capabilities": ["openkore.command.a", "openkore.command.skills", "openkore.command.skill", "openkore.config.attackAuto"],
     "preconditions": ["target visible/identified", "skill learned and usable", "server supports selected skill interaction"],
     "side_effects": "combat can cause death, consume SP/items, award drops/EXP, or alter quest progress",
     "examples": ["a <monster #>", "skills add <skill #>"],
     "limitations": ["server-specific damage, AI and skill restrictions must be learned from server evidence"],
     "verification_status": "source_confirmed", "documentation_status": "objective_composition_reviewed",
     "source_references": ["src/Commands.pm#1256", "src/Commands.pm#5352", "control/config.txt"]},
    {"id": "objective.items_equipment", "component": "openkore", "kind": "objective_guide",
     "name": "use, identify, equip, or refine items", "terms": ["item", "consumable", "equip", "equipment", "identify", "refine"],
     "related_capabilities": ["openkore.command.i", "openkore.command.item", "openkore.command.eq", "openkore.command.uneq", "openkore.command.identify", "openkore.command.refine"],
     "preconditions": ["item is available and operation is supported by client/server", "equipment job/level/slot rules satisfy server requirements"],
     "side_effects": "consumption, equipment replacement, zeny/material consumption, or irreversible refinement outcomes",
     "examples": ["eq <inventory item #>", "uneq <inventory item #>"],
     "limitations": ["do not assume item scripts or success rates from the command registry; consult server data and command implementation"],
     "verification_status": "source_confirmed", "documentation_status": "objective_composition_reviewed",
     "source_references": ["src/Commands.pm#225", "src/Commands.pm#375", "src/Commands.pm#708", "src/Commands.pm#802"]},
    {"id": "objective.npc_quest", "component": "openkore", "kind": "objective_guide",
     "name": "navigate, interact with NPCs, and progress quests", "terms": ["npc", "dialog", "quest", "navigation", "move", "portal"],
     "related_capabilities": ["openkore.command.move", "openkore.command.nl", "openkore.command.talk", "openkore.command.talknpc", "openkore.command.quest"],
     "preconditions": ["NPC/map/coordinate and required branch must be observed", "choice syntax is server dialogue-specific"],
     "side_effects": "dialogue choices can consume items, grant rewards, or permanently change quest state",
     "examples": ["move <x> <y> [<map name>]", "talk <NPC #>", "talk resp <response #>"],
     "limitations": ["command syntax does not identify the correct server-specific NPC branch or prerequisites"],
     "verification_status": "source_confirmed", "documentation_status": "objective_composition_reviewed",
     "source_references": ["src/Commands.pm#422", "src/Commands.pm#675", "src/Commands.pm#685", "src/Commands.pm#784"]},
    {"id": "objective.buy_sell", "component": "openkore", "kind": "objective_guide",
     "name": "buy or sell items", "terms": ["buy", "sell", "shop", "merchant", "zeny"],
     "related_capabilities": ["openkore.command.store", "openkore.command.buy", "openkore.command.sell", "openkore.command.autobuy", "openkore.command.autosell"],
     "preconditions": ["relevant NPC shop open and item list populated", "character has sufficient currency for purchases"],
     "side_effects": "changes inventory and zeny; sell completion can discard selected items",
     "examples": ["buy <store item #> [<amount>]", "sell <inventory item #> [<amount>]", "sell done"],
     "limitations": ["inventory control and server shop rules affect behavior"],
     "verification_status": "source_confirmed", "documentation_status": "objective_composition_reviewed",
     "source_references": ["src/Commands.pm#118", "src/Commands.pm#589", "src/Commands.pm#1358"]},
]


def _source_manifest(component: str, root: pathlib.Path, files: list[pathlib.Path]) -> dict[str, Any]:
    refs = []
    for path in sorted(set(p.resolve() for p in files if p.is_file())):
        refs.append(_fingerprinted_source(path, root))
    aggregate = hashlib.sha256(json.dumps(refs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    git = _git_info(root)
    return {"component": component, "revision": git["revision"],
            "working_tree_modified": git["working_tree_modified"],
            "source_fingerprint": aggregate, "sources": refs}


def build_catalog(openkore_root: pathlib.Path, rathena_root: pathlib.Path | None = None) -> dict[str, Any]:
    openkore_root = openkore_root.expanduser().resolve()
    if not (openkore_root / "src/Commands.pm").is_file():
        raise ValueError("OpenKore checkout does not contain src/Commands.pm")
    core, core_files = _openkore_command_registry(openkore_root)
    if (openkore_root / "src/Plugins.pm").is_file():
        core_files.append(openkore_root / "src/Plugins.pm")
    plugins, plugin_files = _plugin_registry(openkore_root)
    config, config_files = _config_keys(openkore_root)
    macros, macro_files = _eventmacro_entries(openkore_root)
    import copy
    objective_guides = copy.deepcopy(OBJECTIVE_GUIDES)
    entries = core + plugins + config + macros + [_plugin_api_guide(openkore_root)] + objective_guides
    components = [_source_manifest("openkore", openkore_root, core_files + plugin_files + config_files)]
    if (openkore_root / "plugins/eventMacro/eventMacro.pl").is_file():
        components.append(_source_manifest("eventMacro", openkore_root, macro_files))
    rathena_count = {"administrative_command": 0, "script_builtin": 0}
    if rathena_root:
        rathena_root = rathena_root.expanduser().resolve()
        native, native_files = _rathena_registry(rathena_root)
        entries += native
        for entry in native:
            if entry["kind"] in rathena_count:
                rathena_count[entry["kind"]] += 1
        if native_files:
            components.append(_source_manifest("rathena", rathena_root, native_files))
    version_by_component = {component["component"]: component for component in components}
    for entry in entries:
        version_info = version_by_component.get(entry.get("component"))
        if version_info:
            entry["version"] = {"revision": version_info.get("revision"),
                "source_fingerprint": version_info.get("source_fingerprint"),
                "compatibility": "matches indexed installed source snapshot",
                "working_tree_modified": version_info.get("working_tree_modified", False)}
    # The objective examples are tied to checked-in command help line references;
    # update to discovered refs when source line positions shift.
    command_lookup = {e["name"]: e for e in core}
    for entry in objective_guides:
        entry["related_capabilities"] = [cid for cid in entry["related_capabilities"]
            if cid.split(".")[-1] in command_lookup or cid.startswith("openkore.config.")]
    condition_guide = next((e for e in macros if e.get("id") == "eventmacro.conditions"), {})
    counts = {"openkore_commands_discovered": len(core),
        "plugin_sources_discovered": sum(e.get("kind") == "plugin" for e in plugins),
        "plugin_commands_discovered": sum(e.get("kind") == "plugin_command" for e in plugins),
        "plugin_hooks_discovered": sum(e.get("kind") == "plugin_hook" for e in plugins),
        "configuration_keys_discovered": len(config), "eventmacro_guides": len(macros),
        "eventmacro_condition_modules_indexed": len(condition_guide.get("discovered_conditions", [])),
        "eventmacro_condition_types_indexed": len(condition_guide.get("discovered_condition_types", [])),
        "objective_guides": len(objective_guides), **{f"{k}s_discovered": v for k, v in rathena_count.items()}}
    fully_documented = _count_fully_documented(entries)
    for entry in entries:
        entry.setdefault("knowledge_provenance", "installed_source_registry" if entry.get("verification_status") == "source_discovered"
                         else "installed_source_review")
        _apply_maturity(entry)
    maturity_counts = {level: sum(e.get("maturity") == level for e in entries)
                       for level in ("indexed", "documented", "validated")}
    return {"schema_version": SCHEMA_VERSION, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "catalog_type": "technical_capabilities_not_server_knowledge",
        "components": components,
        "coverage": {**counts, "entries_total": len(entries), "fully_documented_entries": fully_documented,
            "maturity_counts": maturity_counts,
            "discovery_claim": "partial; registry and configuration scans are lexical and do not prove complete coverage",
            "gaps": ["command argument/precondition/side-effect details are incomplete for registry-only discoveries",
                     "plugin activation depends on local OpenKore configuration and is not inferred by source scan",
                     "rAthena atcommand permissions depend on active groups/configuration",
                     "eventMacro condition arguments and utility functions are indexed but not exhaustively documented",
                     "rAthena script control flow and server-specific behavior are not interpreted"]},
        "entries": entries}


def write_catalog(catalog: dict[str, Any], output: pathlib.Path) -> pathlib.Path:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(output)
    return output


def load_catalog(path: pathlib.Path = DEFAULT_CATALOG) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(
            f"technical catalog is missing: {path}; run `korelab technical refresh --openkore PATH` "
            "or pass an existing catalog with --catalog")
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported technical catalog schema version")
    return catalog


def search_catalog(catalog: dict[str, Any], query: str, *, component: str | None = None) -> dict[str, Any]:
    terms = [t for t in re.findall(r"[a-z0-9_]+", query.casefold()) if t not in _STOPWORDS]
    if not terms:
        raise ValueError("query must contain searchable text")
    results = []
    for entry in catalog.get("entries", []):
        if component and entry.get("component") != component:
            continue
        haystack = json.dumps(entry, ensure_ascii=False, sort_keys=True).casefold()
        score = sum(1 for term in terms if term in haystack)
        if score:
            results.append((score, entry))
    results.sort(key=lambda item: (-item[0], item[1].get("id", "")))
    rows = [dict(score=score, **entry) for score, entry in results[:30]]
    status = "NOT_FOUND" if not rows else "AMBIGUOUS" if len(rows) > 1 and rows[0]["score"] == rows[1]["score"] else "MATCHED"
    return {"status": status, "query": query, "matches": rows}


def objective_catalog(catalog: dict[str, Any], objective: str) -> dict[str, Any]:
    terms = {t for t in re.findall(r"[a-z0-9_]+", objective.casefold()) if t not in _STOPWORDS}
    guides = []
    for entry in catalog.get("entries", []):
        if entry.get("kind") != "objective_guide":
            continue
        words = set(re.findall(r"[a-z0-9_]+", " ".join([entry.get("name", ""), *entry.get("terms", [])]).casefold()))
        overlap = len(terms & words)
        if overlap:
            guides.append((overlap, entry))
    guides.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    if not guides:
        return {"status": "UNSUPPORTED_OR_UNKNOWN", "objective": objective, "guides": [],
                "knowledge_assessment": "insufficient",
                "message": "No reviewed objective guide matches; search indexed capabilities or inspect source references. Do not treat absence from this partial catalog as proof the operation is impossible."}
    ambiguous = len(guides) > 1 and guides[0][0] == guides[1][0]
    best = guides[0][0]
    selected = [dict(match_terms=score, **entry) for score, entry in guides if score == best][:8]
    return {"status": "AMBIGUOUS" if ambiguous else "MATCHED", "objective": objective,
        "knowledge_assessment": "ambiguous" if ambiguous else "partial_guidance",
        "guides": selected,
        "investigation": {"next_steps": ["inspect the linked source references for command/API behavior not stated in the guide",
                "query server-scoped knowledge or observe the target server for permissions, NPCs, dialogue, items, maps, and mechanics",
                "record confirmed findings append-only; include environment and conditions for runtime-validated claims"],
            "matched_resources": [cid for guide in selected for cid in guide.get("related_capabilities", [])],
            "unresolved_server_facts": ["server permissions and availability", "server-specific entities and mechanics"]},
        "knowledge_domains": ["technical catalog", "server-scoped knowledge must be queried separately for NPC/item/map/quest facts"]}


def catalog_changes(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    def flatten(catalog: dict[str, Any] | None) -> dict[tuple[str, str], str]:
        if not catalog:
            return {}
        return {(component["component"], source["path"]): source["sha256"]
                for component in catalog.get("components", []) for source in component.get("sources", [])}
    old, new = flatten(previous), flatten(current)
    changed = sorted(key for key in old.keys() & new.keys() if old[key] != new[key])
    added, removed = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    return {"changed_sources": [f"{c}:{p}" for c, p in changed],
            "added_sources": [f"{c}:{p}" for c, p in added],
            "removed_sources": [f"{c}:{p}" for c, p in removed],
            "invalidated_components": sorted({c for c, _ in changed + removed})}


def preserve_unrequested_components(previous: dict[str, Any] | None, current: dict[str, Any],
                                    requested: set[str]) -> dict[str, Any]:
    """Keep prior optional component snapshots when refresh did not request them."""
    if not previous:
        return current
    old_components = {c["component"]: c for c in previous.get("components", [])}
    new_components = {c["component"]: c for c in current.get("components", [])}
    preserved = {name for name in old_components if name not in requested and name not in new_components}
    if not preserved:
        return current
    result = dict(current)
    result["components"] = list(current.get("components", [])) + [old_components[name] for name in sorted(preserved)]
    result["entries"] = list(current.get("entries", [])) + [e for e in previous.get("entries", []) if e.get("component") in preserved]
    coverage = dict(current.get("coverage", {}))
    for key, value in previous.get("coverage", {}).items():
        if any(token in key for token in ("administrative_commands", "script_builtins")):
            coverage[key] = value
    coverage["entries_total"] = len(result["entries"])
    coverage["fully_documented_entries"] = _count_fully_documented(result["entries"])
    coverage["maturity_counts"] = {level: sum(_knowledge_maturity(e)[0] == level for e in result["entries"])
                                   for level in ("indexed", "documented", "validated")}
    coverage["not_refreshed_components"] = sorted(preserved)
    result["coverage"] = coverage
    return result


def check_freshness(catalog: dict[str, Any], roots: dict[str, pathlib.Path]) -> dict[str, Any]:
    """Hash only cataloged source files; callers decide when to run full extraction."""
    stale = []
    missing = []
    new_sources = []
    revision_mismatches = []
    unchecked = []
    current_revisions = {}
    for component in catalog.get("components", []):
        name = component["component"]
        root = roots.get("openkore" if name == "eventMacro" else name)
        if root is None:
            unchecked.append(name)
            continue
        root = root.expanduser().resolve()
        git_state = _git_info(root)
        current_revisions[name] = git_state["revision"]
        if component.get("revision") and git_state["revision"] and component["revision"] != git_state["revision"]:
            revision_mismatches.append(name)
        indexed_paths = {source["path"] for source in component.get("sources", [])}
        if name in ("openkore", "eventMacro"):
            scan_root = root / ("plugins/eventMacro" if name == "eventMacro" else "plugins")
            skip = {"test", "tests", "needs-review", ".git"}
            if scan_root.is_dir():
                suffixes = {".pm", ".pl"} if name == "eventMacro" else {".pl"}
                present = {p.relative_to(root).as_posix() for p in scan_root.rglob("*")
                           if p.is_file() and p.suffix in suffixes and not skip.intersection(p.relative_to(scan_root).parts)}
                new_sources.extend(f"{name}:{path}" for path in sorted(present - indexed_paths))
            if name == "openkore":
                source_root = root / "src"
                present_src = {p.relative_to(root).as_posix() for p in source_root.rglob("*")
                    if p.is_file() and p.suffix in {".pm", ".pl"} and not {"auto", "deps"}.intersection(p.parts)}
                new_sources.extend(f"{name}:{path}" for path in sorted(present_src - indexed_paths))
        elif name == "rathena":
            custom_root = root / "src/custom"
            if custom_root.is_dir():
                present = {p.relative_to(root).as_posix() for p in custom_root.glob("atcommand*") if p.is_file()}
                new_sources.extend(f"{name}:{path}" for path in sorted(present - indexed_paths))
        for source in component.get("sources", []):
            path = (root / source["path"]).resolve()
            if not path.is_file():
                missing.append(f"{name}:{source['path']}")
            elif _fingerprinted_source(path, root)["sha256"] != source["sha256"]:
                stale.append(f"{name}:{source['path']}")
    status = "STALE" if stale or missing or new_sources or revision_mismatches else "UNCHECKED" if unchecked else "CURRENT"
    invalidated = set()
    stale_set = set(stale + missing + new_sources)
    invalidated_components = {item.split(":", 1)[0] for item in stale_set} | set(revision_mismatches)
    for entry in catalog.get("entries", []):
        component = entry.get("component")
        if component in invalidated_components:
            invalidated.add(entry.get("id"))
    return {"status": status, "stale_sources": stale,
            "missing_sources": missing, "new_sources": new_sources,
            "revision_mismatches": sorted(revision_mismatches), "current_revisions": current_revisions,
            "unchecked_components": sorted(set(unchecked)),
            "invalidated_components": sorted(invalidated_components),
            "invalidated_entries": sorted(x for x in invalidated if x),
            "catalog_revisions": {c["component"]: c.get("revision") for c in catalog.get("components", [])},
            "note": "freshness check hashes indexed source files only; run refresh to rebuild entries"}


def append_confirmed_fact(path: pathlib.Path, *, fact_id: str, statement: str,
                          source_reference: str, version: str, component: str,
                          status: str = "source_confirmed",
                          validation_evidence: dict[str, str] | None = None,
                          arguments: dict[str, Any] | None = None,
                          preconditions: list[str] | None = None,
                          side_effects: str | None = None,
                          source_sha256: str | None = None) -> dict[str, Any]:
    allowed = {"source_confirmed", "documentation_only", "experimentally_verified", "inferred"}
    if status not in allowed:
        raise ValueError(f"status must be one of {', '.join(sorted(allowed))}")
    if component not in {"openkore", "eventMacro", "rathena"}:
        raise ValueError("component must be openkore, eventMacro, or rathena")
    if not fact_id.strip() or not statement.strip() or not source_reference.strip() or not version.strip():
        raise ValueError("fact id, statement, source reference, version, and component are required")
    if source_sha256 is not None and not re.fullmatch(r"[a-fA-F0-9]{64}", source_sha256):
        raise ValueError("source SHA-256 must contain exactly 64 hexadecimal characters")
    if status == "experimentally_verified":
        if not isinstance(validation_evidence, dict) or not all(validation_evidence.get(key) for key in
                ("reference", "environment", "conditions")):
            raise ValueError("experimentally_verified facts require evidence reference, environment, and conditions")
    elif validation_evidence is not None and not all(validation_evidence.get(key) for key in
            ("reference", "environment", "conditions")):
        raise ValueError("validation evidence requires reference, environment, and conditions")
    normalized_ref = source_reference.split("#", 1)[0]
    if pathlib.PurePosixPath(normalized_ref).is_absolute() or PureWindowsPath(normalized_ref).is_absolute() or ".." in pathlib.PurePosixPath(normalized_ref).parts:
        raise ValueError("source reference must be a relative path within its component checkout")
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    if path.is_file():
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    conflict = any(r.get("fact_id") == fact_id and r.get("statement") != statement for r in records)
    record = {"schema_version": 1, "fact_id": fact_id, "statement": statement,
        "component": component, "source_reference": source_reference, "version": version, "status": status,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "conflicts_with_existing": conflict}
    if validation_evidence is not None:
        record["validation_evidence"] = {**validation_evidence, "version": version}
    if arguments is not None:
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        record["arguments"] = arguments
    if preconditions is not None:
        if not isinstance(preconditions, list) or not all(isinstance(value, str) and value.strip() for value in preconditions):
            raise ValueError("preconditions must be a list of nonempty strings")
        record["preconditions"] = preconditions
    if side_effects is not None:
        record["side_effects"] = side_effects
    if source_sha256 is not None:
        record["source_sha256"] = source_sha256.lower()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def load_confirmed_facts(path: pathlib.Path = DEFAULT_FACTS) -> list[dict[str, Any]]:
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def confirmed_fact_entry(record: dict[str, Any], ordinal: int) -> dict[str, Any]:
    entry = {"id": f"confirmed.{record['fact_id']}.{ordinal}", "component": record["component"],
        "kind": "confirmed_technical_fact", "name": record["fact_id"], "purpose": record["statement"],
        "verification_status": record["status"], "knowledge_provenance": "append_only_technical_knowledge",
        "version": record["version"], "conflicts_with_existing": record.get("conflicts_with_existing", False),
        "documentation_status": "manually_confirmed", "source_references": [record["source_reference"]],
        "validation_evidence": record.get("validation_evidence"),
        "recorded_at": record["recorded_at"]}
    for key in ("arguments", "preconditions", "side_effects", "source_sha256"):
        if key in record:
            entry[key] = record[key]
    if entry["validation_evidence"] is None:
        entry.pop("validation_evidence")
    if entry["conflicts_with_existing"]:
        entry["knowledge_conflict"] = "A different statement with this fact ID exists; review both records before reuse."
    return _apply_maturity(entry)
