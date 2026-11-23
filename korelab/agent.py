"""Minimal agent-facing facade over real OpenKore sessions; deliberately rAthena-free."""
from __future__ import annotations

import re
import hashlib
import json
import pathlib
import shutil
import secrets
import time
from typing import Any
from .paths import TECHNICAL_DIR

from .adapters.openkore import position, redact
from .knowledge import KnowledgeStore


class UnsupportedOperation(ValueError):
    pass


def validate_eventmacro_source(source: pathlib.Path) -> list[str]:
    """Check file size, balanced blocks, and declared macro names; not a language parser."""
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValueError("eventMacro source file does not exist")
    text = source.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > 1_000_000:
        raise ValueError("eventMacro source exceeds the 1 MB limit")
    declarations = re.findall(r"(?im)^\s*macro\s+([A-Za-z][A-Za-z0-9_]*)\s*\{", text)
    if not declarations or len(declarations) != len(set(declarations)):
        raise ValueError("eventMacro source must declare uniquely named macro blocks")
    depth = 0
    for raw_line in text.splitlines():
        line = re.sub(r"#.*$", "", raw_line)
        depth += line.count("{") - line.count("}")
        if depth < 0:
            raise ValueError("eventMacro source has an unmatched closing brace")
    if depth:
        raise ValueError("eventMacro source has unclosed macro or automacro blocks")
    return declarations


def build_move_eventmacro(name: str, x: int, y: int) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
        raise ValueError("eventMacro name is invalid")
    if type(x) is not int or type(y) is not int or not (0 <= x <= 999 and 0 <= y <= 999):
        raise ValueError("eventMacro target must use integer coordinates from 0 to 999")
    suffix = secrets.token_hex(4).upper()
    return (f"macro {name} {{\n"
            f"    log KORELAB_EVENTMACRO_STARTED_{suffix}\n"
            f"    do move {x} {y}\n"
            f"    pause 3\n"
            f"    log KORELAB_EVENTMACRO_PROGRESS_{suffix}\n"
            f"    log KORELAB_EVENTMACRO_COMPLETED_{suffix}\n"
            "}\n")


def export_eventmacro_source(source: pathlib.Path, destination: pathlib.Path,
                             runtime_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    macros = validate_eventmacro_source(source)
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o755)
    target = destination / source.name
    manifest_path = destination / "manifest.json"
    if target.exists() or manifest_path.exists():
        raise FileExistsError("eventMacro export target already exists")
    runtime_requirements = {"plugin": "eventMacro", "eventMacro_file": target.name}
    omitted_sensitive_keys = []
    pending_control_files = []
    if runtime_profile is not None:
        safe_ai_modes = {"on", "manual", "off"}
        ai_mode = runtime_profile.get("ai_mode", "manual")
        if ai_mode not in safe_ai_modes:
            raise ValueError("eventMacro export runtime profile has an unsupported AI mode")
        safe_overrides = {}
        config_overrides = runtime_profile.get("config_overrides", {})
        if not isinstance(config_overrides, dict):
            raise ValueError("eventMacro export config_overrides must be an object")
        sensitive = re.compile(r"password|passwd|username|account|master|server|login|token|secret|pin|email|char", re.I)
        for key, value in sorted(config_overrides.items()):
            if sensitive.search(str(key)):
                omitted_sensitive_keys.append(str(key))
                continue
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"eventMacro export config value for {key} must be scalar")
            if isinstance(value, str) and ("\n" in value or "\r" in value):
                raise ValueError(f"eventMacro export config value for {key} contains a line break")
            safe_overrides[str(key)] = value

        control_files = runtime_profile.get("control_file_overrides", {})
        if not isinstance(control_files, dict):
            raise ValueError("eventMacro export control_file_overrides must be an object")
        exported_controls = []
        for filename, declaration in sorted(control_files.items()):
            if (not re.fullmatch(r"[A-Za-z0-9_.-]+\.txt", str(filename)) or
                    ".." in str(filename) or not isinstance(declaration, dict)):
                raise ValueError("eventMacro export contains an unsafe control-file declaration")
            mode, content = declaration.get("mode"), declaration.get("content")
            if mode not in {"append", "replace"} or not isinstance(content, str):
                raise ValueError(f"eventMacro export control file {filename} requires mode and text content")
            control_target = destination / "control" / filename
            if control_target.exists():
                raise FileExistsError(f"eventMacro export control file already exists: {filename}")
            encoded = content.encode("utf-8")
            pending_control_files.append((control_target, encoded))
            exported_controls.append({"file": f"control/{filename}", "mode": mode,
                                     "sha256": hashlib.sha256(encoded).hexdigest()})
        runtime_requirements.update({"ai_mode": ai_mode, "config_overrides": safe_overrides,
            "control_file_overlays": exported_controls,
            "omitted_sensitive_config_keys": omitted_sensitive_keys})
    shutil.copyfile(source, target)
    target.chmod(0o644)
    for control_target, encoded in pending_control_files:
        control_target.parent.mkdir(mode=0o755, exist_ok=True)
        control_target.write_bytes(encoded)
        control_target.chmod(0o644)
    manifest = {"schema_version": 2, "source": target.name,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "macro_names": macros, "plugin": "OpenKore native eventMacro",
                "required_configuration": runtime_requirements}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o644)
    return {"source_path": str(target), "manifest_path": str(manifest_path), "sha256": manifest["sha256"]}


def execute_interactive_request(session: "OpenKoreAgentSession", request: dict[str, Any], *,
                                authorized: set[str], loaded_macros: set[str],
                                default_timeout: int = 60) -> dict[str, Any]:
    """Execute one structured request over an already authenticated OpenKore session."""
    if not isinstance(request, dict):
        raise ValueError("interactive request must be a JSON object")
    allowed_fields = {"request_id", "action", "timeout", "operation", "map", "x", "y", "mode", "key", "value",
                      "item_id", "item_name", "client_index",
                      "npc", "name", "source", "expect_map", "expect_x", "expect_y",
                      "aegis_name", "count", "npc_name", "relative_file"}
    unknown_fields = sorted(set(request) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"unsupported interactive request fields: {', '.join(unknown_fields)}")
    request_id = request.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 100):
        raise ValueError("request_id must be a string of at most 100 characters")
    action = request.get("action")
    timeout = request.get("timeout", default_timeout)
    if type(timeout) is not int or not 1 <= timeout <= 900:
        raise ValueError("interactive action timeout must be an integer from 1 to 900 seconds")
    action_fields = {"observe": set(), "wait-position-change": {"map", "x", "y"},
        "command": {"operation"}, "ai-mode": {"mode"},
        "pickup-item": {"item_id", "item_name", "client_index"},
        "spawn-monsters": {"aegis_name", "count"},
        "load-test-npc": {"npc_name", "relative_file"},
        "unload-test-npc": {"npc_name", "relative_file"},
        "config-set": {"key", "value"},
        "move": {"map", "x", "y"}, "npc-dialogue": {"npc"},
        "eventmacro": {"name", "expect_map", "expect_x", "expect_y"},
        "eventmacro-start": {"name"}, "eventmacro-stop": set(), "eventmacro-reload": {"source"},
        "wait-reconnect": set()}
    if action not in action_fields:
        raise UnsupportedOperation(f"unsupported interactive action: {action}")
    inapplicable = set(request) - ({"request_id", "action", "timeout"} | action_fields[action])
    if inapplicable:
        raise ValueError(f"fields are not valid for interactive action {action}: {', '.join(sorted(inapplicable))}")
    if action == "eventmacro":
        name = request.get("name")
        if not isinstance(name, str) or name not in loaded_macros:
            raise UnsupportedOperation("eventMacro name is not in the source loaded for this session")
        expected_fields = {"expect_map", "expect_x", "expect_y"} & set(request)
        if expected_fields and expected_fields != {"expect_map", "expect_x", "expect_y"}:
            raise ValueError("eventMacro objective requires expect_map, expect_x, and expect_y together")
        expected_position = ({"x": request["expect_x"], "y": request["expect_y"]}
                             if expected_fields else None)
        if expected_position and any(type(expected_position[axis]) is not int for axis in ("x", "y")):
            raise ValueError("eventMacro expected coordinates must be integers")
        result = session.run_eventmacro(name, authorized="eventmacro" in authorized, timeout=timeout,
            expected_map=request.get("expect_map"),
            expected_position=expected_position)
    elif action == "eventmacro-start":
        name = request.get("name")
        if not isinstance(name, str) or name not in loaded_macros:
            raise UnsupportedOperation("eventMacro name is not in the source loaded for this session")
        result = session.start_eventmacro(name, authorized="eventmacro" in authorized, timeout=timeout)
    elif action == "eventmacro-stop":
        result = session.stop_owned_eventmacro(authorized="eventmacro" in authorized, timeout=timeout)
    elif action == "eventmacro-reload":
        source = request.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("eventmacro-reload requires a source file path")
        result = session.reload_eventmacro(pathlib.Path(source).expanduser().resolve(),
            authorized="eventmacro" in authorized, timeout=timeout)
        if result.get("status") == "LOADED" and isinstance(loaded_macros, set):
            loaded_macros.clear()
            loaded_macros.update(result["macro_names_verified"])
    elif action == "observe":
        result = session.perform_action("observe", timeout=timeout)
    elif action == "wait-position-change":
        result = session.perform_action("wait-position-change",
            inputs={key: request.get(key) for key in ("map", "x", "y")}, timeout=timeout)
    elif action == "command":
        result = session.perform_action("command", inputs={"operation": request.get("operation")},
                                        timeout=timeout)
    elif action == "pickup-item":
        result = session.perform_action("pickup-item", inputs={key: request.get(key)
            for key in ("item_id", "item_name", "client_index")}, authorized=authorized, timeout=timeout)
    elif action == "spawn-monsters":
        result = session.perform_action("spawn-monsters", inputs={key: request.get(key)
            for key in ("aegis_name", "count")}, authorized=authorized, timeout=timeout)
    elif action == "load-test-npc":
        result = session.perform_action("load-test-npc", inputs={key: request.get(key)
            for key in ("npc_name", "relative_file")}, authorized=authorized, timeout=timeout)
    elif action == "unload-test-npc":
        result = session.perform_action("unload-test-npc", inputs={key: request.get(key)
            for key in ("npc_name", "relative_file")},
            authorized=authorized, timeout=timeout)
    elif action == "ai-mode":
        result = session.perform_action("ai-mode", inputs={"mode": request.get("mode")},
            authorized=authorized, timeout=timeout)
    elif action == "config-set":
        result = session.perform_action("config-set", inputs={"key": request.get("key"),
            "value": request.get("value")}, authorized=authorized, timeout=timeout)
    elif action == "move":
        result = session.perform_action("move", inputs={key: request.get(key) for key in ("map", "x", "y")},
            authorized=authorized, timeout=timeout)
    elif action == "npc-dialogue":
        result = session.perform_action("npc-dialogue", inputs={"npc": request.get("npc")},
            authorized=authorized, timeout=timeout)
    elif action == "wait-reconnect":
        result = session.wait_reconnect(timeout=timeout)
    else:  # pragma: no cover - action names are checked above
        raise UnsupportedOperation(f"unsupported interactive action: {action}")
    wrapped = {"request_id": request_id, "action": action, "result": result}
    if action in {"observe", "move", "npc-dialogue", "eventmacro", "ai-mode", "config-set", "pickup-item", "spawn-monsters", "load-test-npc", "unload-test-npc"}:
        sample = session.observe_and_store(timeout=min(timeout, 15))
        fact = sample.get("knowledge_fact", {})
        wrapped["post_action_observation"] = {"observation": sample.get("observation"),
            "knowledge_fact": {key: fact.get(key) for key in ("fact_id", "provenance", "evidence", "status")}}
    return wrapped


READ_COMMANDS = {
    "help": "help", "status": "status", "where": "where", "look": "look",
    "inventory": "i", "skills": "skills", "stats": "stats",
    "eventmacro_list": "eventMacro list", "eventmacro_status": "eventMacro status",
    "lockmapsweep_status": "lockMapSweep status",
}
EVENTMACRO_AUTO_OPERATION = re.compile(r"^eventmacro_auto:([A-Za-z][A-Za-z0-9_]*)$")
READ_RESPONSE_MARKERS = {
    "help": (r"Available commands", r"commands? don.t exist", r"Type 'help'"),
    "status": (r"Status:", r"Base Level", r"Connected to"),
    "where": (r"\bis at\b", r"\bLocation:"),
    "look": (r"\bMap:", r"\bMonsters:", r"\bNPCs:"),
    "inventory": (r"\bInventory\b", r"\bcarrying\b", r"\bWeight:\s*\d"),
    "skills": (r"\bSkills\b", r"\bSkill points\b", r"\bSkill list\b"),
    "stats": (r"\bBase Level\b", r"\bJob Level\b", r"\bStr:\s*\d"),
    "eventmacro_list": (r"(?m)^\s*[A-Za-z][A-Za-z0-9_]*\s*$", r"No macros"),
    "eventmacro_status": (r"Macro name:", r"There's no macro currently running"),
    "eventmacro_auto": (r"Printing information about automacro", r"Automacro '.+' not found"),
    "lockmapsweep_status": (r"\[lockMapSweep\] ready=\d+ map=",),
}
AGENT_CAPABILITIES = [
    {"name": "lockmapsweep_status", "permission": "read_only",
     "required_inputs": ["LockMapSweep plugin loaded"],
     "preconditions": ["the plugin registers its native lockMapSweep command"],
     "observations": ["native plugin readiness, active lockMap, block and pending route state"],
     "completion": "plugin-specific status output matches the installed command contract",
     "failures": ["plugin command unavailable", "timeout"],
     "limitations": ["only available when this reference plugin is loaded; command output does not verify a completed server-side movement"]},
    {"name": "eventmacro_inspect_automacro", "permission": "read_only",
     "required_inputs": ["automacro name"],
     "preconditions": ["native eventMacro plugin is loaded", "automacro name is declared"],
     "observations": ["native eventMacro auto output, false/true condition counts, each condition state, trigger state, and AI check states"],
     "completion": "command-specific native automacro inspection output is captured",
     "failures": ["unknown automacro", "plugin unavailable", "timeout"],
     "limitations": ["console output may include interleaved messages; the result is diagnostic evidence, not proof of macro execution or objective success"]},
    {"name": "ai-mode", "permission": "reversible_gameplay", "authorization": "--authorize ai-mode",
     "required_inputs": ["mode: on, manual, or off"],
     "preconditions": ["active OpenKore session", "caller explicitly authorizes the AI mode transition"],
     "observations": ["native OpenKore ai command response", "subsequent structured state and gameplay effects"],
     "completion": "OpenKore confirms the requested AI mode; this does not establish an objective outcome",
     "failures": ["unsupported mode", "native response timeout"],
     "limitations": ["changing AI mode can start or stop native automation; other profile and control-file settings remain effective"]},
    {"name": "config-set", "permission": "temporary_runtime_configuration", "authorization": "--authorize openkore-config",
     "required_inputs": ["key declared in this session's isolated config_overrides", "single-line scalar value"],
     "preconditions": ["startup-managed local OpenKore session", "key is not sensitive", "caller explicitly authorizes a runtime configuration change"],
     "observations": ["native OpenKore conf response", "subsequent plugin output and structured game state"],
     "completion": "OpenKore confirms the temporary runtime value; resulting behavior must be checked separately",
     "failures": ["authorization denied", "undeclared key", "sensitive key", "invalid value", "native response timeout"],
     "limitations": ["does not edit the source config file; unavailable for attached sessions; only keys predeclared by the scenario can be changed"]},
    {"name": "wait-position-change", "permission": "read_only",
     "required_inputs": ["map", "x", "y"],
     "preconditions": ["startup-managed session with the structured state observer"],
     "observations": ["a newer in-game observer sample at a different map or coordinate"],
     "completion": "observed position change only; the caller must verify the requested objective separately",
     "failures": ["invalid origin", "timeout", "session ended"],
     "limitations": ["does not initiate movement and is unavailable for socket-attached sessions without a compatible observer"]},
    {"name": "interactive_session", "permission": "read_only_by_default",
     "required_inputs": ["JSONL requests on stdin", "explicit per-action authorization"],
     "preconditions": ["valid OpenKore profile and character selection", "session lifetime within declared maximum"],
     "observations": ["streamed structured action results", "post-action state samples", "server-scoped knowledge records"],
     "completion": "explicit stop request or input EOF; final sanitized session evidence written",
     "failures": ["connection stage failure", "unsupported request", "permission denial", "action timeout", "session deadline"],
     "limitations": ["startup-managed mode does not resume after its OpenKore process exits; use native Socket attachment for a still-running process", "wait_reconnect only observes OpenKore's native reconnect behavior"]},
    {"name": "attach_openkore_socket", "permission": "read_only_by_default",
     "required_inputs": ["OpenKore Interface::Socket path", "active stateObserver JSONL path", "OpenKore source checkout"],
     "preconditions": ["OpenKore is already running with its native Socket interface", "stateObserver is active and writing structured samples"],
     "observations": ["native OpenKore console events", "structured observer samples", "knowledge records"],
     "completion": "attached agent session closes its own socket while leaving the OpenKore process running",
     "failures": ["missing socket", "missing observer", "closed OpenKore process", "unsupported or unauthorized action"],
     "limitations": ["requires local filesystem access to the native Unix socket and observer file", "one-shot read-only commands can attach before map entry, but state-changing operations require fresh in-game observer state", "cannot install/replace eventMacro source or control the lifetime of the existing process"]},
    {"name": "observe", "permission": "read_only", "required_inputs": [],
     "preconditions": ["in-game OpenKore session"], "observations": ["character, map, position, actors, NPCs, inventory/equipment, resources"],
     "completion": "fresh stateObserver sample", "failures": ["disconnected", "timeout"],
     "limitations": ["only fields emitted by stateObserver"]},
    {"name": "wait-reconnect", "permission": "read_only", "required_inputs": ["timeout"],
     "preconditions": ["same OpenKore process is running", "stateObserver remains available"],
     "observations": ["disconnect and subsequent in-game state from the OpenKore observer"],
     "completion": "same process is observed in-game after a disconnected state",
     "failures": ["OpenKore process exit", "no disconnect observed", "native reconnect timeout"],
     "limitations": ["does not initiate a connection or bypass server/client reconnect policy"]},
    {"name": "runtime_profile", "permission": "gameplay_automation", "authorization": "--authorize openkore-profile",
     "required_inputs": ["optional AI mode", "installed OpenKore config-key overrides", "registered control-file overlays"],
     "preconditions": ["keys and control files are discovered from the selected OpenKore source tree"],
     "observations": ["selected AI mode", "overridden key names", "control-file source references and hashes"],
     "completion": "OpenKore starts with a private temporary profile; original config/control files remain unchanged",
     "failures": ["unknown config key", "unregistered control file", "invalid override syntax", "missing authorization"],
     "limitations": ["configuration semantics remain version/configuration specific; runtime behavior needs separate observation"]},
    {"name": "command", "permission": "read_only", "required_inputs": ["operation"],
     "preconditions": ["active OpenKore session", "operation on read-only allowlist"],
     "observations": ["captured console output"], "completion": "nonempty output or bounded quiet interval",
     "failures": ["unsupported operation", "timeout"], "limitations": ["unsolicited output may be interleaved"]},
    {"name": "move", "permission": "reversible_gameplay", "authorization": "--authorize move",
     "required_inputs": ["map", "x", "y"], "preconditions": ["in-game on requested map"],
     "observations": ["stateObserver map, coordinates, movement state"],
     "completion": "requested coordinate observed and stationary", "failures": ["wrong map", "unreachable coordinate", "timeout"],
     "limitations": ["map transitions are not yet planned by this operation"]},
    {"name": "pickup-item", "permission": "gameplay_interaction", "authorization": "--authorize pickup-item",
     "required_inputs": ["visible item_id", "visible client_index", "item_name"],
     "preconditions": ["the latest OpenKore observer sample contains the requested ground item at that index"],
     "observations": ["native OpenKore take command", "structured inventory increase"],
     "completion": "the selected ground item's inventory count increased",
     "failures": ["item moved or disappeared", "native pickup rejection", "inventory unchanged", "timeout"],
     "limitations": ["takes one visible stack unit; the agent chooses whether the item is needed"]},
    {"name": "spawn-monsters", "permission": "controlled_server_fixture", "authorization": "--authorize spawn-monsters",
     "required_inputs": ["AegisName", "bounded count"],
     "preconditions": ["local controlled test server grants @monster", "active OpenKore session"],
     "observations": ["unique named monsters in a fresh OpenKore state sample"],
     "completion": "all requested unique named monsters are visible to OpenKore",
     "failures": ["server denies @monster", "spawned monsters outside client view", "timeout"],
     "limitations": ["controlled test fixture only; this is not an objective planner or gameplay executor"]},
    {"name": "load-test-npc", "permission": "controlled_server_fixture", "authorization": "--authorize npc-fixture",
     "required_inputs": ["controlled KoreLab NPC name", "npc/custom source path"],
     "preconditions": ["the declared temporary NPC source exists on the local rAthena server", "server grants @loadnpc"],
     "observations": ["native OpenKore atcommand response", "reconnect refresh", "NPC visible in a fresh state sample"],
     "completion": "OpenKore observed the loaded fixture NPC after native relog", "failures": ["source missing", "server rejects command", "NPC not visible"],
     "limitations": ["local controlled fixtures only; does not edit server files or affect production quests"]},
    {"name": "unload-test-npc", "permission": "controlled_server_fixture", "authorization": "--authorize npc-fixture",
     "required_inputs": ["controlled KoreLab NPC name", "source path of the loaded fixture"],
     "preconditions": ["the temporary NPC is loaded", "caller explicitly authorizes fixture cleanup"],
     "observations": ["native OpenKore atcommand acknowledgement", "NPC absence from a fresh state sample"],
     "completion": "OpenKore no longer observes the fixture NPC", "failures": ["server rejects command", "NPC remains visible"],
     "limitations": ["unloads only a KoreLab fixture path; local source file removal is a separate cleanup step"]},
    {"name": "npc_dialogue", "permission": "persistent_or_resource_effect", "authorization": "--authorize npc",
     "required_inputs": ["NPC name"], "preconditions": ["NPC currently visible to OpenKore"],
     "observations": ["observed NPC position and live dialogue choices"],
     "completion": "dialogue response captured; no choice selected", "failures": ["NPC unavailable", "unexpected/no dialogue", "timeout"],
     "limitations": ["does not select dialogue choices or infer hidden outcomes"]},
    {"name": "run_eventmacro", "permission": "arbitrary_gameplay_automation", "authorization": "--allow-eventmacro-side-effects",
     "required_inputs": ["source", "macro name", "optional map/coordinate postcondition"], "preconditions": ["native eventMacro loaded", "source passes structural checks"],
     "observations": ["native lifecycle markers and optional structured target state"],
     "completion": "marker plus separately verified objective when declared", "failures": ["macro failure", "timeout", "interruption", "objective postcondition mismatch"],
     "limitations": ["KoreLab does not interpret eventMacro; source remains untrusted executable input"]},
    {"name": "eventmacro-start", "permission": "arbitrary_gameplay_automation", "authorization": "--allow-eventmacro-side-effects",
     "required_inputs": ["loaded macro name"], "preconditions": ["eventMacro reports no running macro", "macro was declared in loaded source"],
     "observations": ["native command response; poll eventMacro_status for runtime state"],
     "completion": "start request submitted to the native plugin; gameplay completion is not implied",
     "failures": ["existing macro active", "plugin response unconfirmed", "macro source unavailable"],
     "limitations": ["macro behavior remains opaque; use native status and structured game observations"]},
    {"name": "eventmacro-stop", "permission": "arbitrary_gameplay_automation", "authorization": "--allow-eventmacro-side-effects",
     "required_inputs": [], "preconditions": ["the running macro was started through this KoreLab session"],
     "observations": ["native eventMacro stop response"],
     "completion": "native plugin confirms it stopped the macro started by this session",
     "failures": ["no session-owned active macro", "native stop response unconfirmed"],
     "limitations": ["stops eventMacro execution only; it does not reverse gameplay actions already performed"]},
    {"name": "eventmacro-reload", "permission": "arbitrary_gameplay_automation", "authorization": "--allow-eventmacro-side-effects",
     "required_inputs": ["replacement source file"], "preconditions": ["native plugin confirms no macro is running", "source passes structural checks"],
     "observations": ["eventMacro_file reload response and eventMacro list output"],
     "completion": "native plugin exposes the replacement macro names; source hash is recorded",
     "failures": ["active macro", "invalid source", "plugin reload or name verification failure"],
     "limitations": ["reload does not run or validate the replacement macro's gameplay objective"]},
    {"name": "record_knowledge", "permission": "read_only", "required_inputs": ["server id", "fact", "provenance", "evidence"],
     "preconditions": ["valid provenance and evidence"], "observations": ["server-scoped JSONL history"],
     "completion": "fact persisted", "failures": ["invalid provenance", "conflicting or unwritable store"],
     "limitations": ["conflicts remain explicit for agent resolution"]},
]


class OpenKoreAgentSession:
    """Wrap an already started OpenKoreAdapter, without importing local server adapters."""

    def __init__(self, adapter: Any, knowledge: KnowledgeStore, *,
                 mutable_config_keys: set[str] | None = None):
        self.adapter = adapter
        self.knowledge = knowledge
        self.mutable_config_keys = set(mutable_config_keys or ())
        self.session_owned_macro_active = False
        self.session_owned_macro_name: str | None = None

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {"schema_version": 1,
                "runtime_dependency": "OpenKore plus a configured game server endpoint; no rAthena source/database/API",
                "technical_resolution": {"stage": "before OpenKore starts for an agent-session action",
                    "catalog": str(TECHNICAL_DIR / "catalog.json"),
                    "report_fields": ["resource IDs and maturity", "component revision", "source hashes and excerpts",
                                      "known arguments", "permission context", "confirmed findings and conflicts"],
                    "effectful_actions_require": "a current, non-conflicting documented contract; otherwise return investigation references before execution",
                    "read_only_gap_behavior": "allowlisted reads may run for investigation and retain partial/indexed status"},
                "capabilities": AGENT_CAPABILITIES}

    @staticmethod
    def require_permission(permission: str, authorized: set[str]) -> None:
        if permission not in authorized:
            raise PermissionError(f"operation requires explicit authorization: {permission}")

    def observe(self, timeout: float = 15) -> dict[str, Any]:
        sample_count = len(self.adapter._observer_rows())
        return self.adapter.state_after(sample_count, lambda row: row.get("type") == "state"
            and row.get("connected_in_game") is True and row.get("inventory_observed") is True, timeout=timeout)

    def wait_reconnect(self, timeout: float = 60) -> dict[str, Any]:
        """Observe OpenKore's own disconnect/reconnect transition; do not initiate a login."""
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 900:
            raise ValueError("reconnect observation timeout must be between 1 and 900 seconds")
        history = self.adapter._observer_rows()
        cursor = len(history)
        saw_disconnect = any(row.get("type") == "state" and row.get("connected_in_game") is False
                             for row in history)
        disconnected_state = next((row for row in reversed(history)
            if row.get("type") == "state" and row.get("connected_in_game") is False), None)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            process = getattr(self.adapter, "process", None)
            if process and process.poll() is not None:
                return {"status": "CLIENT_EXITED", "phase": "reconnect_wait",
                        "evidence": "OpenKore process exited before an in-game recovery state"}
            self.adapter._read(min(0.2, deadline - time.monotonic()))
            rows = self.adapter._observer_rows()
            for row in rows[cursor:]:
                cursor += 1
                if row.get("type") != "state":
                    continue
                if row.get("connected_in_game") is False:
                    saw_disconnect = True
                    disconnected_state = row
                elif saw_disconnect and row.get("connected_in_game") is True and row.get("inventory_observed") is True:
                    observed_position = position(row)
                    value = {"map": row.get("map"), "position": observed_position,
                             "disconnect_observed_at": disconnected_state.get("time") if disconnected_state else None,
                             "reconnected_observed_at": row.get("time")}
                    fact = self._record_discovery("connection_recovered", str(row.get("map", "unknown")),
                        value, "OpenKore stateObserver recorded disconnected then in-game state in one client process")
                    return {"status": "VERIFIED", "observation": value,
                            "knowledge_fact": {key: fact.get(key) for key in ("fact_id", "provenance", "evidence", "status")},
                            "completion": "OpenKore native connection recovered in the same process"}
        return {"status": "TIMEOUT", "phase": "reconnect_wait" if saw_disconnect else "disconnect_not_observed",
                "saw_disconnect": saw_disconnect,
                "completion": "a disconnect followed by an in-game state was not observed before timeout"}

    def execute_read_only(self, operation: str, *, quiet_period: float = 0.35,
                          timeout: float = 5) -> dict[str, Any]:
        if not isinstance(operation, str):
            raise UnsupportedOperation("unsupported or non-read-only operation")
        auto_match = EVENTMACRO_AUTO_OPERATION.fullmatch(operation)
        if auto_match:
            command = f"eventMacro auto {auto_match.group(1)}"
            response_kind = "eventmacro_auto"
        elif operation in READ_COMMANDS:
            command = READ_COMMANDS[operation]
            response_kind = operation
        else:
            raise UnsupportedOperation(f"unsupported or non-read-only operation: {operation}")
        response = self.adapter.capture_command(command, timeout=timeout, quiet_period=quiet_period)
        matched = any(re.search(marker, response, re.I) for marker in READ_RESPONSE_MARKERS[response_kind])
        return {"operation": operation, "command": command, "status": "OBSERVED" if matched else "NO_RESPONSE",
                "response": response, "capture_nonempty": bool(response.strip()),
                "response_contract_matched": matched,
                "response_classification": "command_specific_output" if matched else "unclassified_console_output",
                "observation_method": "OpenKore console output captured until a quiet interval and checked against a command-specific response marker",
                "correlation_limit": "unsolicited server/plugin messages can be interleaved because OpenKore emits no response correlation IDs; unmatched output is not treated as a command response"}

    def perform_action(self, action: str, *, inputs: dict[str, Any] | None = None,
                       authorized: set[str] | None = None, timeout: float = 30) -> dict[str, Any]:
        inputs, authorized = inputs or {}, authorized or set()
        if action == "observe":
            state = self.observe(timeout=min(timeout, 15))
            return {"action": action, "status": "OBSERVED", "observations": state,
                    "completion": "fresh structured state sample"}
        if action == "wait-position-change":
            waiter = getattr(self.adapter, "wait_position_change", None)
            if not callable(waiter):
                raise UnsupportedOperation("the active OpenKore adapter does not expose structured position waiting")
            return {"action": action, **waiter(inputs.get("map"), inputs.get("x"), inputs.get("y"),
                timeout=min(timeout, 120))}
        if action == "command":
            operation = inputs.get("operation")
            result = self.execute_read_only(operation, timeout=min(timeout, 10))
            return {"action": action, **result}
        if action == "pickup-item":
            self.require_permission("pickup-item", authorized)
            item_id = inputs.get("item_id")
            item_name = inputs.get("item_name")
            client_index = inputs.get("client_index")
            if type(item_id) is not int or item_id <= 0:
                raise ValueError("pickup-item requires a positive observed item ID")
            if not isinstance(item_name, str) or not re.fullmatch(r"[A-Za-z0-9_ ]{1,64}", item_name):
                raise ValueError("pickup-item requires a bounded item name")
            if type(client_index) is not int or client_index < 0:
                raise ValueError("pickup-item requires a non-negative observed ground-item index")
            result = self.adapter.collect_monster_drop(item_id, item_name, timeout=timeout,
                                                       client_index=client_index)
            return {"action": action, **result,
                    "evidence": "native OpenKore take selected the observed ground item and stateObserver confirmed inventory increase"}
        if action == "spawn-monsters":
            self.require_permission("spawn-monsters", authorized)
            aegis_name, count = inputs.get("aegis_name"), inputs.get("count")
            if not isinstance(aegis_name, str) or not re.fullmatch(r"[A-Z0-9_]+", aegis_name):
                raise ValueError("spawn-monsters requires a valid monster AegisName")
            if type(count) is not int or not 1 <= count <= 10:
                raise ValueError("spawn-monsters count must be an integer from 1 to 10")
            spawn = getattr(self.adapter, "spawn_monster", None)
            if not callable(spawn):
                raise UnsupportedOperation("the active OpenKore adapter does not expose controlled test spawning")
            test_name = f"KoreLabA{secrets.randbelow(1_000_000):06d}"
            observed = spawn(aegis_name, test_name, "KoreLabCombat::OnMobDeath",
                             timeout=timeout, count=count)
            matches = [row for row in observed.get("nearby_monsters", [])
                       if str(row.get("name", "")).casefold() == test_name.casefold()]
            if len(matches) < count:
                raise RuntimeError("OpenKore spawn returned without the requested named targets in its final sample")
            return {"action": action, "status": "OBSERVED", "aegis_name": aegis_name,
                    "test_name": test_name, "requested_count": count, "observed_count": len(matches),
                    "observations": observed,
                    "evidence": "existing KoreLab OpenKoreAdapter sent native c @monster and stateObserver confirmed named targets"}
        if action == "load-test-npc":
            self.require_permission("npc-fixture", authorized)
            npc_name, relative_file = inputs.get("npc_name"), inputs.get("relative_file")
            if not isinstance(npc_name, str) or not re.fullmatch(r"KoreLab[A-Za-z0-9_]{1,40}", npc_name):
                raise ValueError("load-test-npc requires a bounded KoreLab NPC name")
            if not isinstance(relative_file, str) or not re.fullmatch(
                    r"npc/custom/korelab_[a-z0-9_-]{1,64}\.txt", relative_file, re.I):
                raise ValueError("load-test-npc requires a relative npc/custom KoreLab fixture path")
            loader = getattr(self.adapter, "load_npc", None)
            if not callable(loader):
                raise UnsupportedOperation("the active OpenKore adapter does not expose controlled NPC fixture loading")
            result = loader(npc_name, relative_file, timeout=timeout)
            return {"action": action, "status": "OBSERVED", "npc_name": npc_name,
                    "relative_file": relative_file, "observations": result.get("state"),
                    "evidence": "native OpenKore @loadnpc acknowledgement and post-relog stateObserver NPC visibility"}
        if action == "unload-test-npc":
            self.require_permission("npc-fixture", authorized)
            npc_name = inputs.get("npc_name")
            if not isinstance(npc_name, str) or not re.fullmatch(r"KoreLab[A-Za-z0-9_]{1,40}", npc_name):
                raise ValueError("unload-test-npc requires a bounded KoreLab NPC name")
            relative_file = inputs.get("relative_file")
            if not isinstance(relative_file, str) or not re.fullmatch(
                    r"npc/custom/korelab_[a-z0-9_-]{1,64}\.txt", relative_file, re.I):
                raise ValueError("unload-test-npc requires the relative path of the loaded NPC fixture")
            unloader = getattr(self.adapter, "unload_npc_file", None)
            if not callable(unloader):
                raise UnsupportedOperation("the active OpenKore adapter does not expose controlled NPC source removal")
            result = unloader(relative_file, npc_name, timeout=timeout)
            return {"action": action, "status": "OBSERVED", "npc_name": npc_name,
                    "relative_file": relative_file, "observations": result.get("state"),
                    "native_response": result.get("native_response"),
                    "evidence": result.get("evidence")}
        if action == "ai-mode":
            self.require_permission("ai-mode", authorized)
            mode = inputs.get("mode")
            if not isinstance(mode, str):
                raise ValueError("ai-mode requires mode on, manual, or off")
            result = self.adapter.set_ai_mode(mode, timeout=timeout)
            return {"action": action, "status": "OBSERVED", **result,
                    "completion": "OpenKore confirmed the native AI mode; gameplay objectives remain separately verifiable"}
        if action == "config-set":
            self.require_permission("openkore-config", authorized)
            key, value = inputs.get("key"), inputs.get("value")
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
                raise ValueError("config-set key must be a single OpenKore configuration identifier")
            if key not in self.mutable_config_keys:
                raise UnsupportedOperation("config-set key was not declared in the isolated scenario profile")
            if re.search(r"password|passwd|username|account|master|server|login|token|secret|pin|email|char", key, re.I):
                raise UnsupportedOperation("sensitive OpenKore configuration keys cannot be changed through agent-session")
            if type(value) not in {str, int, float}:
                raise ValueError("config-set value must be a string or number")
            value_text = str(value)
            if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value_text):
                raise ValueError("config-set value must use bounded single-token characters")
            setter = getattr(self.adapter, "set_config_value", None)
            if not callable(setter):
                raise UnsupportedOperation("the active OpenKore adapter does not support runtime config changes")
            result = setter(key, value_text, timeout=timeout)
            return {"action": action, "status": "APPLIED", "key": key,
                    "native_response": result.get("native_response"),
                    "plugin_events": result.get("plugin_events", []),
                    "evidence": "OpenKore native conf command confirmed the runtime setting",
                    "completion": "setting was applied to the isolated runtime; requested plugin behavior remains separately verifiable"}
        if action == "move":
            self.require_permission("move", authorized)
            map_name, x, y = inputs.get("map"), inputs.get("x"), inputs.get("y")
            if (not isinstance(map_name, str) or not re.fullmatch(r"[a-z0-9_]+", map_name)
                    or type(x) is not int or type(y) is not int or not (0 <= x <= 999 and 0 <= y <= 999)):
                raise ValueError("move requires map and integer x/y coordinates between 0 and 999")
            before = self.observe(timeout=min(timeout, 15))
            if before.get("connected_in_game") is not True:
                raise RuntimeError("movement precondition failed: OpenKore is not in game")
            current_map = str(before.get("map", ""))
            if current_map != map_name:
                profile = getattr(self.adapter, "scenario", {})
                declared = profile.get("openkore", {}).get("map_transitions", [])
                allowed = any(isinstance(edge, dict) and edge.get("from") == current_map
                              and edge.get("to") == map_name
                              and re.fullmatch(r"[a-z0-9_]+", str(edge.get("from", "")))
                              and re.fullmatch(r"[a-z0-9_]+", str(edge.get("to", "")))
                              for edge in declared)
                if not allowed:
                    raise ValueError("cross-map movement requires a scenario-declared from/to map transition")
            try:
                observed = self.adapter.move(map_name, {"x": x, "y": y}, timeout)
            except TimeoutError:
                return {"action": action, "status": "TIMEOUT", "requested": {"map": map_name, "x": x, "y": y},
                        "observations": self.observe(timeout=min(timeout, 5)),
                        "completion": "target position was not observed before timeout"}
            self._record_discovery("map_coordinate_reached", map_name, {"x": x, "y": y},
                                   "OpenKore stateObserver confirmed requested position and stationary state")
            if current_map != map_name:
                self._record_discovery("map_transition_confirmed", current_map, {"to": map_name,
                    "x": x, "y": y}, "OpenKore native move routed through a server portal and stateObserver confirmed destination map")
            return {"action": action, "status": "VERIFIED", "requested": {"map": map_name, "x": x, "y": y},
                    "previous_map": current_map, "map_transition": current_map != map_name,
                    "observations": observed, "completion": "target map/coordinate observed with stationary movement"}
        if action == "npc-dialogue":
            self.require_permission("npc", authorized)
            name = inputs.get("npc")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("npc-dialogue requires an NPC name")
            try:
                result = self.adapter.observe_npc_dialogue(name, timeout)
            except TimeoutError:
                return {"action": action, "status": "TIMEOUT", "npc": name,
                        "observations": self.observe(timeout=min(timeout, 5)),
                        "completion": "live dialogue response was not observed before timeout"}
            self._record_discovery("npc_dialogue_choices", name,
                                   {"position": result.get("position"), "responses": result.get("responses", [])},
                                   "OpenKore stateObserver captured live NPC dialogue responses")
            return {"action": action, **result}
        raise UnsupportedOperation(f"unsupported agent action: {action}")

    def _record_discovery(self, attribute: str, entity_id: str, value: Any, evidence: str) -> dict[str, Any]:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return self.knowledge.add(entity_type="observation", entity_id=entity_id,
            attribute=f"{attribute}@{timestamp}", value=value, provenance="openkore_observed", evidence=evidence)

    def run_eventmacro(self, name: str, *, authorized: bool = False,
                       timeout: float = 15, expected_map: str | None = None,
                       expected_position: dict[str, int] | None = None,
                       interrupt_after: float | None = None) -> dict[str, Any]:
        if not authorized:
            raise PermissionError("eventMacro execution requires explicit authorization")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            raise ValueError("invalid eventMacro name")
        sample_count = len(self.adapter._observer_rows())
        cursor = len(self.adapter.output)
        self.adapter.command(f"eventMacro {name}")
        self.session_owned_macro_active = True
        self.session_owned_macro_name = name
        lifecycle: list[dict[str, str]] = []
        started = time.monotonic()
        interrupted = False
        while time.monotonic() - started < timeout:
            self.adapter._read(0.1)
            excerpt = self.adapter.output[cursor:]
            for prefix, state in (("KORELAB_EVENTMACRO_STARTED", "started"),
                                  ("KORELAB_EVENTMACRO_PROGRESS", "progress"),
                                  ("KORELAB_EVENTMACRO_COMPLETED", "completed")):
                marker = re.search(rf"{prefix}(?:_[A-F0-9]{{8}})?", excerpt)
                if marker and not any(item["state"] == state for item in lifecycle):
                    lifecycle.append({"state": state, "evidence": marker.group(0)})
            failed = re.search(r"KORELAB_EVENTMACRO_FAILED(?:_[A-F0-9]{8})?", excerpt)
            if failed:
                lifecycle.append({"state": "failed", "evidence": failed.group(0)})
                break
            if any(item["state"] == "completed" for item in lifecycle):
                break
            if interrupt_after is not None and not interrupted and time.monotonic() - started >= interrupt_after:
                interrupt_cursor = len(self.adapter.output)
                self.adapter.command("eventMacro stop")
                try:
                    self.adapter._wait_after(r"Stopping macro", interrupt_cursor, timeout=3)
                    lifecycle.append({"state": "interrupted", "evidence": "native eventMacro stop command confirmed"})
                    interrupted = True
                except TimeoutError:
                    lifecycle.append({"state": "interruption_failed", "evidence": "native plugin did not confirm macro stop"})
                    break
                break
        states = [item["state"] for item in lifecycle]
        timed_out = "completed" not in states and "failed" not in states and "interrupted" not in states and "interruption_failed" not in states
        if timed_out:
            lifecycle.append({"state": "timeout", "evidence": "no completion marker before timeout"})
            states.append("timeout")
        stop_result = None
        if timed_out or "failed" in states or "interruption_failed" in states:
            stop_result = self.stop_eventmacro(timeout=min(timeout, 5))
            lifecycle.append({"state": "cleanup", "evidence": f"native stop result {stop_result['status']}"})
        lifecycle_status = "COMPLETED" if "started" in states and "completed" in states and "failed" not in states else "INTERRUPTED" if "interrupted" in states else "TIMEOUT" if timed_out else "FAILED"
        self.session_owned_macro_active = False
        self.session_owned_macro_name = None
        objective_status = "NOT_CHECKED"
        objective_state = None
        if lifecycle_status == "COMPLETED" and expected_map and expected_position:
            try:
                objective_state = self.adapter.state_after(sample_count, lambda row: row.get("type") == "state"
                    and row.get("connected_in_game") is True and row.get("map") == expected_map
                    and position(row) == expected_position
                    and row.get("movement_state") == "stationary", timeout=timeout)
                objective_status = "VERIFIED"
                self._record_discovery("macro_target_reached", expected_map,
                    {"position": expected_position, "macro": name},
                    "OpenKore structured observer independently confirmed target after native macro completion")
            except TimeoutError:
                rows = self.adapter._observer_rows()
                objective_state = next((row for row in reversed(rows) if row.get("type") == "state"), None)
                objective_status = "FAILED"
        return {"macro": name, "lifecycle_status": lifecycle_status,
                "objective_status": objective_status, "objective_observation": objective_state,
                "lifecycle": lifecycle, "command_accepted": f"eventMacro {name}" in self.adapter.output[cursor:],
                "response": redact(self.adapter.output[cursor:], getattr(self.adapter, "sensitive_values", ())),
                "timeout_or_failure_cleanup": stop_result,
                "limitation": "completion marker proves the source reached its marker; objective-specific postconditions must be checked separately"}

    def replace_eventmacro(self, source: pathlib.Path) -> dict[str, Any]:
        macros = validate_eventmacro_source(source)
        result = self.adapter.replace_eventmacro_source(source, macros)
        result["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        return result

    def reload_eventmacro(self, source: pathlib.Path, *, authorized: bool = False,
                          timeout: float = 10) -> dict[str, Any]:
        if not authorized:
            raise PermissionError("eventMacro source replacement requires explicit authorization")
        if not source.is_file():
            raise ValueError("eventMacro replacement source file does not exist")
        status = self.adapter.capture_command("eventMacro status", timeout=timeout)
        if not re.search(r"There's no macro currently running", status, re.I):
            return {"status": "REJECTED_ACTIVE_MACRO", "native_status": status,
                    "completion": "source was not replaced because the native plugin did not confirm an idle macro state"}
        self.session_owned_macro_active = False
        self.session_owned_macro_name = None
        return self.replace_eventmacro(source)

    def start_eventmacro(self, name: str, *, authorized: bool = False, timeout: float = 5) -> dict[str, Any]:
        if not authorized:
            raise PermissionError("eventMacro execution requires explicit authorization")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            raise ValueError("invalid eventMacro name")
        status = self.adapter.capture_command("eventMacro status", timeout=timeout)
        if re.search(r"There's a macro currently running", status, re.I):
            return {"status": "REJECTED_ACTIVE_MACRO", "native_status": status,
                    "completion": "no macro was started because the native plugin reports another macro running"}
        if not re.search(r"There's no macro currently running", status, re.I):
            return {"status": "UNCONFIRMED", "native_status": status,
                    "completion": "no macro was started because the native plugin did not confirm an idle state"}
        response = self.adapter.capture_command(f"eventMacro {name}", timeout=timeout, quiet_period=0.1)
        self.session_owned_macro_active = True
        self.session_owned_macro_name = name
        return {"status": "START_REQUESTED", "macro": name, "native_status_before_start": status,
                "native_response": response,
                "completion": "native start command submitted; poll eventMacro_status and game observations to establish progress or completion"}

    def stop_owned_eventmacro(self, *, authorized: bool = False, timeout: float = 5) -> dict[str, Any]:
        if not authorized:
            raise PermissionError("eventMacro stop requires explicit authorization")
        if not self.session_owned_macro_active:
            return {"status": "NO_SESSION_MACRO", "completion": "KoreLab did not start an active macro in this session"}
        native_status = self.adapter.capture_command("eventMacro status", timeout=timeout)
        if re.search(r"There's no macro currently running", native_status, re.I):
            self.session_owned_macro_active = False
            self.session_owned_macro_name = None
            return {"status": "IDLE", "native_status": native_status,
                    "completion": "native plugin reports no macro is running; nothing was stopped"}
        if not re.search(rf"Macro name:\s*{re.escape(self.session_owned_macro_name or '')}\s*(?:\r?$)",
                         native_status, re.I | re.M):
            self.session_owned_macro_active = False
            self.session_owned_macro_name = None
            return {"status": "OWNERSHIP_MISMATCH", "native_status": native_status,
                    "completion": "native status does not identify the session-owned macro; no macro was stopped"}
        result = self.stop_eventmacro(timeout=timeout)
        if result["status"] == "INTERRUPTED":
            self.session_owned_macro_active = False
            self.session_owned_macro_name = None
            return {**result, "status": "VERIFIED", "lifecycle_status": "INTERRUPTED",
                    "completion": "native plugin confirmed interruption of the session-owned macro"}
        return {"status": "UNCONFIRMED", **result,
                "completion": "native plugin did not confirm interruption; ownership retained for retry"}

    def stop_eventmacro(self, timeout: float = 5) -> dict[str, str]:
        response = self.adapter.capture_command("eventMacro stop", timeout=timeout)
        if re.search(r"Stopping macro", response, re.I):
            state = "INTERRUPTED"
        elif re.search(r"no macro currently running", response, re.I):
            state = "IDLE"
        else:
            state = "UNCONFIRMED"
        return {"status": state, "response": response,
                "evidence": "native eventMacro stop command response"}

    def store_observation(self, state: dict[str, Any]) -> dict[str, Any]:
        observed_at = str(state.get("time", "unknown-time"))
        return self.knowledge.add(entity_type="character", entity_id="active-character",
            attribute=f"session_state@{observed_at}", value=state, provenance="openkore_observed",
            evidence="OpenKore stateObserver JSONL; independently sampled during active session")

    def observe_and_store(self, timeout: float = 15) -> dict[str, Any]:
        state = self.observe(timeout)
        fact = self.store_observation(state)
        return {"observation": state, "knowledge_fact": fact}
