from __future__ import annotations

import argparse
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time

from .capabilities import capability_catalog, capability_for
from .monsters import assess_match, find_monster, load_monster_catalog
from .quests import find_quest_scripts, load_quest_catalog
from .scenario import load_scenario
from .paths import KNOWLEDGE_DIR, RUNS_DIR, USER_DATA_DIR
from .technical_knowledge import DEFAULT_CATALOG, DEFAULT_FACTS
from .platform_support import set_private_permissions


class _ConsoleInput:
    """Read piped/console JSONL without select() on Windows console handles."""
    def __init__(self):
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.thread = threading.Thread(target=self._read, daemon=True, name="korelab-stdin")
        self.thread.start()

    def _read(self) -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                self.lines.put(None)
                return
            self.lines.put(line)

    def get(self, timeout: float) -> str | None:
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            return ""


def _parse_start_point(value: str | None) -> dict | None:
    if value is None:
        return None
    parts = value.split(",")
    if len(parts) != 3 or not parts[0] or not parts[1].isdigit() or not parts[2].isdigit():
        raise ValueError("--start-point must be MAP,X,Y with nonnegative integer coordinates")
    return {"map": parts[0], "x": int(parts[1]), "y": int(parts[2])}


def main() -> int:
    parser = argparse.ArgumentParser(prog="korelab", description="Run a declarative OpenKore/rAthena scenario")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_cmd = sub.add_parser("doctor", help="inspect local runtime capabilities and providers")
    doctor_cmd.add_argument("--openkore", type=pathlib.Path, help="inspect this OpenKore checkout")
    doctor_cmd.add_argument("--rathena", type=pathlib.Path, help="inspect this rAthena checkout")
    doctor_cmd.add_argument("--verify-database", action="store_true",
                            help="connect only to the explicitly configured rAthena MySQL/MariaDB endpoint and verify schema/transaction behavior")
    setup_cmd = sub.add_parser("setup", help="plan or explicitly apply a compatible dependency installation")
    setup_cmd.add_argument("--capability", choices=("rathena-mysql-protocol",
        "rathena-database-verification", "rathena-database-service", "openkore-perl-runtime"))
    setup_cmd.add_argument("--provider", help="exact provider ID shown by `setup` plan")
    setup_cmd.add_argument("--apply", action="store_true", help="apply one selected plan; never starts/configures services")
    setup_cmd.add_argument("--allow-elevation", action="store_true",
                           help="authorize a selected installer that may request elevated permissions")
    lab_cmd = sub.add_parser("lab", help="prepare and manage an owned disposable rAthena laboratory")
    lab_actions = lab_cmd.add_subparsers(dest="lab_action", required=True)
    lab_plan = lab_actions.add_parser("plan", help="show a non-mutating managed laboratory plan")
    lab_create = lab_actions.add_parser("create", help="create an isolated database and server configuration")
    for command in (lab_plan, lab_create):
        command.add_argument("--openkore", type=pathlib.Path, required=True)
        command.add_argument("--rathena-runtime", type=pathlib.Path, required=True,
                             help="built rAthena runtime root; source checkout is never modified")
        command.add_argument("--xstools-overlay", type=pathlib.Path,
                             help="private compatible XSTools build; original OpenKore source remains unchanged")
        command.add_argument("--name", default="korelab")
        command.add_argument("--agents", type=int, default=1,
                             help="independent native OpenKore identities to prepare (1-100)")
        command.add_argument("--combat-observer", action="store_true",
                             help="load a private rAthena kill-event observer for managed fleet evidence")
        command.add_argument("--start-point", metavar="MAP,X,Y",
                             help="verified native spawn point for disposable new characters")
        command.add_argument("--data-home", type=pathlib.Path,
                             help="private laboratory state root; defaults to KORELAB_DATA_HOME")
    lab_status = lab_actions.add_parser("status", help="inspect an owned laboratory")
    lab_status.add_argument("id")
    lab_stop = lab_actions.add_parser("stop", help="stop only processes owned by the selected laboratory")
    lab_stop.add_argument("id")
    lab_destroy = lab_actions.add_parser("destroy", help="stop and remove one owned disposable laboratory")
    lab_destroy.add_argument("id")
    lab_start = lab_actions.add_parser("start", help="initialize its private DB and start owned rAthena services")
    lab_start.add_argument("id")
    lab_start.add_argument("--timeout", type=float, default=45)
    lab_bootstrap = lab_actions.add_parser("bootstrap", help="register and create a novice through native OpenKore/rAthena protocols")
    lab_bootstrap.add_argument("id")
    lab_bootstrap.add_argument("--timeout", type=float, default=120)
    lab_bootstrap.add_argument("--agent", default="agent-001")
    lab_bootstrap.add_argument("--all", action="store_true",
                               help="provision every configured agent through native OpenKore protocols")
    fleet_run = lab_actions.add_parser("fleet-run",
        help="provision and run a disposable population of concurrent native OpenKore combat agents")
    fleet_run.add_argument("--openkore", type=pathlib.Path, required=True)
    fleet_run.add_argument("--rathena-runtime", type=pathlib.Path, required=True)
    fleet_run.add_argument("--xstools-overlay", type=pathlib.Path, required=True)
    fleet_run.add_argument("--agents", type=int,
        help="population size; defaults to the saved roster size or 50 without a roster")
    fleet_run.add_argument("--lock-map",
        help="default lock map when no saved roster supplies per-character maps")
    fleet_run.add_argument("--start-point", required=True, metavar="MAP,X,Y")
    fleet_run.add_argument("--window-seconds", type=int, default=300)
    fleet_run.add_argument("--sample-interval", type=int, default=5)
    fleet_run.add_argument("--startup-batch", type=int, default=5)
    fleet_run.add_argument("--diverse-roster", action="store_true",
        help="legacy built-in diverse roster; prefer --roster for reusable editable definitions")
    fleet_run.add_argument("--roster", type=pathlib.Path,
        help="credential-free JSON character roster/build definition")
    fleet_run.add_argument("--data-home", type=pathlib.Path)
    for command in (lab_status, lab_stop, lab_destroy, lab_start, lab_bootstrap):
        command.add_argument("--data-home", type=pathlib.Path)
    portals_cmd = sub.add_parser("portals", help="reconcile one OpenKore route against active rAthena warp evidence")
    portal_actions = portals_cmd.add_subparsers(dest="portal_action", required=True)
    portal_reconcile = portal_actions.add_parser("reconcile", help="inspect and persist one verified portal correction")
    portal_reconcile.add_argument("--server-id", required=True,
        help="stable managed-laboratory/profile ID used to isolate persistent corrections")
    portal_reconcile.add_argument("--profile", required=True,
        help="human-readable OpenKore server profile name for the evidence record")
    portal_reconcile.add_argument("--openkore", type=pathlib.Path, required=True,
        help="OpenKore checkout whose native portal table is being reconciled")
    portal_reconcile.add_argument("--rathena", type=pathlib.Path, required=True,
        help="rAthena source/runtime checkout containing its active NPC script hierarchy")
    portal_reconcile.add_argument("--table-folder", action="append",
        help="selected OpenKore addTableFolders entry; repeat in profile order, or infer from --profile")
    portal_reconcile.add_argument("--source", required=True, metavar="MAP,X,Y",
        help="targeted rAthena portal source location (the warp center or a cell in its trigger area)")
    portal_reconcile.add_argument("--remove-obsolete", action="store_true",
        help="retire one exact portal row only when the active source hierarchy has no standard warp there")
    portal_reconcile.add_argument("--destination", metavar="MAP,X,Y",
        help="exact old OpenKore destination; required together with --remove-obsolete")
    portal_reconcile.add_argument("--edition", choices=("auto", "renewal", "pre-re"), default="auto")
    portal_reconcile.add_argument("--data-home", type=pathlib.Path, default=USER_DATA_DIR,
        help="private KoreLab state root; corrections are written under portal-profiles")
    run_cmd = sub.add_parser("run", help="run a real local scenario")
    run_cmd.add_argument("scenario", type=pathlib.Path)
    run_cmd.add_argument("--openkore", type=pathlib.Path,
                         default=pathlib.Path(os.environ["KORELAB_OPENKORE"]) if os.environ.get("KORELAB_OPENKORE") else None)
    run_cmd.add_argument("--rathena", type=pathlib.Path,
                         default=pathlib.Path(os.environ["KORELAB_RATHENA"]) if os.environ.get("KORELAB_RATHENA") else None)
    run_cmd.add_argument("--output", type=pathlib.Path)
    catalog_cmd = sub.add_parser("catalog", help="inspect the active rAthena monster catalog")
    catalog_cmd.add_argument("--rathena", type=pathlib.Path,
                             default=pathlib.Path(os.environ["KORELAB_RATHENA"]) if os.environ.get("KORELAB_RATHENA") else None)
    catalog_cmd.add_argument("--query", help="monster name, AegisName, or ID substring")
    assess_cmd = sub.add_parser("assess", help="semantically assess combat scenario risk without mutation")
    assess_cmd.add_argument("scenario", type=pathlib.Path)
    assess_cmd.add_argument("--rathena", type=pathlib.Path,
                            default=pathlib.Path(os.environ["KORELAB_RATHENA"]) if os.environ.get("KORELAB_RATHENA") else None)
    quests_cmd = sub.add_parser("quests", help="inspect active rAthena quest data and directly related scripts")
    quests_cmd.add_argument("--rathena", type=pathlib.Path,
                            default=pathlib.Path(os.environ["KORELAB_RATHENA"]) if os.environ.get("KORELAB_RATHENA") else None)
    quests_cmd.add_argument("--id", type=int, help="filter to one quest ID")
    capabilities_cmd = sub.add_parser("capabilities", help="list implemented runner capabilities")
    capabilities_cmd.add_argument("--action", help="show one exact ordered or legacy action")
    plan_cmd = sub.add_parser("plan", help="compile a structured objective to an M3-compatible scenario")
    plan_cmd.add_argument("objective", type=pathlib.Path)
    plan_cmd.add_argument("--rathena", type=pathlib.Path,
                          default=pathlib.Path(os.environ["KORELAB_RATHENA"]) if os.environ.get("KORELAB_RATHENA") else None)
    plan_cmd.add_argument("--openkore", type=pathlib.Path,
                          default=pathlib.Path(os.environ["KORELAB_OPENKORE"]) if os.environ.get("KORELAB_OPENKORE") else None)
    plan_cmd.add_argument("--output", type=pathlib.Path, required=True,
                          help="path for the generated executable scenario")
    agent_cmd = sub.add_parser("agent-session", help="observe and operate OpenKore without rAthena APIs")
    agent_cmd.add_argument("scenario", type=pathlib.Path, nargs="?")
    agent_cmd.add_argument("--openkore", type=pathlib.Path,
                           default=pathlib.Path(os.environ["KORELAB_OPENKORE"]) if os.environ.get("KORELAB_OPENKORE") else None)
    agent_cmd.add_argument("--profile-config", type=pathlib.Path,
                           help="existing local OpenKore config containing credentials; never copied to reports")
    agent_cmd.add_argument("--attach-console", type=pathlib.Path,
                           help="attach to an existing OpenKore Interface::Socket session; does not launch or stop that process")
    agent_cmd.add_argument("--observer-file", type=pathlib.Path,
                           help="active structured stateObserver JSONL file required with --attach-console")
    agent_cmd.add_argument("--profile", help="exact server profile name from this OpenKore checkout")
    agent_cmd.add_argument("--server-index", type=int, help="OpenKore server index selected after login")
    agent_cmd.add_argument("--character", help="exact character name to select")
    agent_cmd.add_argument("--character-slot", type=int, help="character slot to select")
    agent_cmd.add_argument("--server-id", required=True)
    agent_cmd.add_argument("--knowledge-root", type=pathlib.Path, default=KNOWLEDGE_DIR)
    agent_cmd.add_argument("--technical-catalog", type=pathlib.Path,
                           default=DEFAULT_CATALOG,
                           help="local technical catalog (refresh with `technical refresh`)")
    agent_cmd.add_argument("--action", choices=("observe", "command", "move", "npc-dialogue"), default="observe")
    # execute_read_only enforces the action allowlist, including bounded
    # parameterized inspections such as eventmacro_auto:<name>.
    agent_cmd.add_argument("--operation", help="bounded read-only operation name")
    agent_cmd.add_argument("--map")
    agent_cmd.add_argument("--x", type=int)
    agent_cmd.add_argument("--y", type=int)
    agent_cmd.add_argument("--timeout", type=int, default=60, help="action timeout in seconds")
    agent_cmd.add_argument("--npc")
    agent_cmd.add_argument("--authorize", action="append",
                           choices=("move", "npc", "eventmacro", "ai-mode", "openkore-config",
                                    "openkore-profile", "pickup-item", "spawn-monsters", "npc-fixture"), default=[])
    agent_cmd.add_argument("--eventmacro-source", type=pathlib.Path)
    agent_cmd.add_argument("--generate-move-eventmacro", help="generate native eventMacro source from --x/--y")
    agent_cmd.add_argument("--run-eventmacro")
    agent_cmd.add_argument("--allow-eventmacro-side-effects", action="store_true")
    agent_cmd.add_argument("--expect-map")
    agent_cmd.add_argument("--expect-x", type=int)
    agent_cmd.add_argument("--expect-y", type=int)
    agent_cmd.add_argument("--interrupt-after", type=float)
    agent_cmd.add_argument("--replacement-eventmacro-source", type=pathlib.Path)
    agent_cmd.add_argument("--replacement-run-eventmacro")
    agent_cmd.add_argument("--export-eventmacro", type=pathlib.Path)
    agent_cmd.add_argument("--interactive", action="store_true",
                           help="keep one authenticated OpenKore session open and accept JSONL action requests")
    agent_cmd.add_argument("--session-duration", type=int, default=1800,
                           help="maximum interactive OpenKore session lifetime in seconds")
    sub.add_parser("agent-capabilities", help="list available agent-facing operations")
    knowledge_cmd = sub.add_parser("knowledge", help="inspect server-scoped knowledge history")
    knowledge_cmd.add_argument("--server-id", required=True)
    knowledge_cmd.add_argument("--knowledge-root", type=pathlib.Path, default=KNOWLEDGE_DIR)
    knowledge_cmd.add_argument("--export", type=pathlib.Path, help="write a portable server-scoped knowledge document")
    knowledge_cmd.add_argument("--import", dest="import_path", type=pathlib.Path,
                               help="append facts from a matching server-scoped knowledge document")
    technical_cmd = sub.add_parser("technical", help="inspect version-aware OpenKore/eventMacro/rAthena technical knowledge")
    technical_cmd.add_argument("operation", choices=("refresh", "status", "coverage", "list", "search", "objective", "show", "add"))
    technical_cmd.add_argument("query", nargs="?", help="search query, objective, or exact capability ID")
    technical_cmd.add_argument("--openkore", type=pathlib.Path,
        default=pathlib.Path(os.environ["KORELAB_OPENKORE"]) if os.environ.get("KORELAB_OPENKORE") else None)
    technical_cmd.add_argument("--rathena", type=pathlib.Path,
        default=pathlib.Path(os.environ["KORELAB_RATHENA"]) if os.environ.get("KORELAB_RATHENA") else None)
    technical_cmd.add_argument("--catalog", type=pathlib.Path)
    technical_cmd.add_argument("--component", choices=("openkore", "eventMacro", "rathena"))
    technical_cmd.add_argument("--id", help="fact ID for append-only confirmed technical knowledge")
    technical_cmd.add_argument("--statement", help="confirmed statement to append")
    technical_cmd.add_argument("--source-reference", help="relative source/doc reference")
    technical_cmd.add_argument("--version", help="component revision/version for the confirmed fact")
    technical_cmd.add_argument("--verification-status", choices=("source_confirmed", "documentation_only", "experimentally_verified", "inferred"), default="source_confirmed")
    technical_cmd.add_argument("--validation-evidence-reference", help="relative report/evidence reference for runtime validation")
    technical_cmd.add_argument("--validation-environment", help="component versions and runtime environment used for validation")
    technical_cmd.add_argument("--validation-conditions", help="specific test conditions and observed outcome")
    technical_cmd.add_argument("--arguments-json", help="JSON object of confirmed argument names and meanings")
    technical_cmd.add_argument("--precondition", action="append", default=[], help="confirmed precondition; may be repeated")
    technical_cmd.add_argument("--side-effects", help="confirmed effects of the capability")
    technical_cmd.add_argument("--source-sha256", help="SHA-256 of the referenced source file at review time")
    technical_cmd.add_argument("--facts", type=pathlib.Path)
    args = parser.parse_args()
    if args.command == "doctor":
        from .dependencies import discover
        report = discover(verify_database=args.verify_database,
                          openkore_path=args.openkore,
                          rathena_path=args.rathena)
        print(json.dumps(report, indent=2))
        verification = report.get("rathena_database_verification", {})
        verification_failed = args.verify_database and verification.get("status") != "VERIFIED"
        return 1 if report.get("configuration_errors") or verification_failed else 0
    if args.command == "setup":
        from .dependencies import apply_setup, setup_plan
        try:
            if args.apply:
                if not args.capability or not args.provider:
                    raise ValueError("--apply requires both --capability and an explicit --provider from the plan")
                result = apply_setup(args.capability, args.provider,
                                     allow_elevation=args.allow_elevation)
            else:
                if args.allow_elevation:
                    raise ValueError("--allow-elevation is valid only together with --apply")
                if args.provider:
                    raise ValueError("--provider is valid only with --apply")
                result = setup_plan(args.capability)
            print(json.dumps(result, indent=2))
            return 0 if result.get("status") not in {"FAILED", "REJECTED"} else 1
        except (OSError, ValueError, RuntimeError) as exc:
            print(json.dumps({"status": "REJECTED", "error": str(exc)}, indent=2))
            return 2
    if args.command == "lab":
        if args.lab_action == "fleet-run":
            from .fleet import ConcurrentFleet
            try:
                fleet = ConcurrentFleet(args.data_home)
                result = fleet.run(openkore=args.openkore,
                    rathena_runtime=args.rathena_runtime, xstools_overlay=args.xstools_overlay,
                    count=args.agents, lock_map=args.lock_map,
                    start_point=_parse_start_point(args.start_point),
                    window_seconds=args.window_seconds, sample_interval=args.sample_interval,
                    startup_batch=args.startup_batch, diverse_roster=args.diverse_roster,
                    roster_path=args.roster)
                print(json.dumps(result, indent=2))
                return 0 if result.get("acceptance") == "PASS" else 1
            except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
                print(json.dumps({"acceptance": "FAILED", "error": str(exc)}, indent=2))
                return 1
        from .laboratory import ManagedLaboratory
        try:
            manager = ManagedLaboratory(args.data_home)
            if args.lab_action == "plan":
                result = manager.plan(openkore=args.openkore,
                    rathena_runtime=args.rathena_runtime, name=args.name,
                    xstools_overlay=args.xstools_overlay,
                    start_point=_parse_start_point(args.start_point))
            elif args.lab_action == "create":
                result = manager.create(openkore=args.openkore,
                    rathena_runtime=args.rathena_runtime, name=args.name,
                    xstools_overlay=args.xstools_overlay,
                    start_point=_parse_start_point(args.start_point),
                    agent_count=args.agents, combat_observer=args.combat_observer)
            elif args.lab_action == "start":
                result = manager.start(args.id, timeout=args.timeout)
            elif args.lab_action == "bootstrap":
                if args.all:
                    owner = json.loads((manager._owned(args.id) / "owner.json").read_text(encoding="utf-8"))
                    result = [manager.bootstrap(args.id, agent_id=agent["id"], timeout=args.timeout)
                              for agent in owner.get("agents", [])]
                else:
                    result = manager.bootstrap(args.id, agent_id=args.agent,
                                               timeout=args.timeout)
            elif args.lab_action == "status":
                result = manager.inspect(args.id)
            elif args.lab_action == "stop":
                result = manager.stop(args.id)
            else:
                result = manager.destroy(args.id)
            print(json.dumps(result, indent=2))
            return 0
        except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
            print(json.dumps({"status": "FAILED", "error": str(exc)}, indent=2))
            return 1
    if args.command == "portals":
        from .openkore_profile import parse_profile_file
        from .portal_sync import reconcile_portal
        try:
            if not pathlib.Path(args.rathena / "src/config/renewal.hpp").is_file():
                owner_path = args.rathena / "owner.json"
                if owner_path.is_file():
                    owner = json.loads(owner_path.read_text(encoding="utf-8"))
                    source = pathlib.Path(owner.get("rathena_runtime_source", ""))
                    if (source / "src/config/renewal.hpp").is_file():
                        args.rathena = source
            if args.table_folder:
                folders = tuple(args.table_folder)
            else:
                sections = {}
                for profile_file in (args.openkore / "tables/servers.txt", args.openkore / "control/servers.txt"):
                    if profile_file.is_file():
                        sections = parse_profile_file(profile_file)
                        if args.profile.casefold() in sections:
                            break
                section = sections.get(args.profile.casefold())
                if not section:
                    raise ValueError("profile is not present in this OpenKore checkout; provide its --table-folder values")
                folders = tuple(part for part in section.get("addtablefolders", "").split(";") if part)
            if not folders:
                raise ValueError("selected OpenKore profile has no addTableFolders; provide --table-folder")
            parts = args.source.split(",")
            if (len(parts) != 3 or not parts[0]
                    or not parts[1].isdigit() or not parts[2].isdigit()):
                raise ValueError("--source must be MAP,X,Y with nonnegative integer coordinates")
            obsolete_destination = None
            if args.remove_obsolete:
                destination_parts = (args.destination or "").split(",")
                if (len(destination_parts) != 3 or not destination_parts[0]
                        or not destination_parts[1].isdigit() or not destination_parts[2].isdigit()):
                    raise ValueError("--remove-obsolete requires --destination MAP,X,Y for the exact stale row")
                obsolete_destination = (destination_parts[0], int(destination_parts[1]),
                                        int(destination_parts[2]))
            elif args.destination:
                raise ValueError("--destination is valid only with --remove-obsolete")
            result = reconcile_portal(server_id=args.server_id, data_home=args.data_home,
                openkore_tables=args.openkore / "tables", table_folders=folders,
                rathena_root=args.rathena, source_map=parts[0], source_x=int(parts[1]),
                source_y=int(parts[2]), profile_name=args.profile, edition=args.edition,
                obsolete_destination=obsolete_destination)
            result["profile"] = args.profile
            print(json.dumps(result, indent=2))
            return 0
        except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
            print(json.dumps({"status": "UNRESOLVED", "error": str(exc),
                              "table_changed": False}, indent=2))
            return 2
    required_paths = {"run": ("openkore", "rathena"), "catalog": ("rathena",),
                      "assess": ("rathena",), "quests": ("rathena",),
                      "plan": ("rathena",), "capabilities": (), "agent-capabilities": (),
                      "knowledge": (), "technical": (), "agent-session": ("openkore",)}
    missing_paths = [name for name in required_paths[args.command] if getattr(args, name) is None]
    if missing_paths:
        parser.error("provide " + " and ".join(f"--{name} or KORELAB_{name.upper()}" for name in missing_paths))
    if args.command == "catalog":
        catalog = load_monster_catalog(args.rathena)
        query = args.query.casefold() if args.query else None
        exact = [m for m in catalog["monsters"] if query is not None and any(
            query == str(m.get(key, "")).casefold() for key in ("id", "name", "aegis_name"))]
        matches = exact or [m for m in catalog["monsters"] if query is None or any(
            query in str(m.get(key, "")).casefold() for key in ("id", "name", "aegis_name"))]
        entries = matches[:25] if query is None else matches[:50]
        print(json.dumps({"mode": catalog["mode"], "sources": catalog["sources"],
                          "total_monsters": catalog["count"], "matched_monsters": len(matches),
                          "truncated": len(entries) < len(matches), "results": entries}, indent=2))
        return 0 if entries else 1
    if args.command == "capabilities":
        if args.action:
            capability = capability_for(args.action)
            if capability is None:
                print(json.dumps({"status": "NOT_SUPPORTED", "action": args.action}, indent=2))
                return 1
            print(json.dumps(capability, indent=2))
            return 0
        print(json.dumps(capability_catalog(), indent=2))
        return 0
    if args.command == "agent-capabilities":
        from .agent import OpenKoreAgentSession
        print(json.dumps(OpenKoreAgentSession.capabilities(), indent=2))
        return 0
    if args.command == "knowledge":
        from .knowledge import KnowledgeStore
        try:
            if args.export and args.import_path:
                raise ValueError("choose only one of --export or --import")
            store = KnowledgeStore(args.knowledge_root, args.server_id)
            if args.export:
                result = store.export_document(args.export)
            elif args.import_path:
                result = store.import_document(args.import_path)
            else:
                result = {"server_id": args.server_id, "facts": store.list_facts()}
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        except (ValueError, OSError) as exc:
            print(json.dumps({"status": "REJECTED", "error": str(exc)}, indent=2))
            return 2
    if args.command == "technical":
        from .technical_knowledge import (append_confirmed_fact,
            build_catalog, catalog_changes, check_freshness, confirmed_fact_entry, load_confirmed_facts,
            load_catalog, search_catalog,
            objective_catalog, preserve_unrequested_components, write_catalog)
        catalog_path = args.catalog or DEFAULT_CATALOG
        try:
            if args.operation == "refresh":
                if not args.openkore:
                    raise ValueError("technical refresh requires --openkore or KORELAB_OPENKORE")
                previous = load_catalog(catalog_path) if catalog_path.is_file() else None
                refreshed = build_catalog(args.openkore, args.rathena)
                requested_components = {"openkore", "eventMacro"} | ({"rathena"} if args.rathena else set())
                refreshed = preserve_unrequested_components(previous, refreshed, requested_components)
                change_set = catalog_changes(previous, refreshed)
                write_catalog(refreshed, catalog_path)
                print(json.dumps({"status": "REFRESHED", "catalog": str(catalog_path.resolve()),
                    "coverage": refreshed["coverage"], "source_changes": change_set}, indent=2))
                return 0
            if args.operation == "add":
                evidence_parts = (args.validation_evidence_reference, args.validation_environment,
                                  args.validation_conditions)
                if any(evidence_parts) and not all(evidence_parts):
                    raise ValueError("validation evidence requires --validation-evidence-reference, --validation-environment, and --validation-conditions together")
                validation_evidence = ({"reference": args.validation_evidence_reference,
                    "environment": args.validation_environment, "conditions": args.validation_conditions}
                    if all(evidence_parts) else None)
                fact = append_confirmed_fact(args.facts or DEFAULT_FACTS, fact_id=args.id or "",
                    statement=args.statement or "", source_reference=args.source_reference or "",
                    version=args.version or "", component=args.component or "",
                    status=args.verification_status, validation_evidence=validation_evidence,
                    arguments=json.loads(args.arguments_json) if args.arguments_json else None,
                    preconditions=args.precondition or None, side_effects=args.side_effects,
                    source_sha256=args.source_sha256)
                print(json.dumps({"status": "APPENDED", "fact": fact}, indent=2, ensure_ascii=False))
                return 0
            catalog = load_catalog(catalog_path)
            fact_records = load_confirmed_facts(args.facts or DEFAULT_FACTS)
            catalog = dict(catalog)
            catalog["entries"] = list(catalog.get("entries", [])) + [
                confirmed_fact_entry(record, ordinal) for ordinal, record in enumerate(fact_records)]
            if args.operation == "status":
                if not args.openkore and not args.rathena:
                    raise ValueError("technical status requires at least one source root")
                roots = {name: path for name, path in (("openkore", args.openkore), ("rathena", args.rathena)) if path}
                result = check_freshness(catalog, roots)
            elif args.operation == "coverage":
                result = {"status": "PARTIAL", "coverage": {**catalog.get("coverage", {}),
                          "confirmed_technical_facts": len(fact_records)},
                          "components": [{k: v for k, v in item.items() if k != "sources"} |
                                         {"indexed_source_files": len(item.get("sources", []))}
                                         for item in catalog.get("components", [])]}
            elif args.operation == "list":
                entries = [e for e in catalog.get("entries", [])
                           if not args.component or e.get("component") == args.component]
                result = {"status": "OK", "count": len(entries), "capabilities": entries}
            elif args.operation == "show":
                if not args.query:
                    raise ValueError("technical show requires a capability ID")
                matches = [e for e in catalog.get("entries", []) if e.get("id") == args.query]
                result = {"status": "FOUND" if matches else "NOT_FOUND", "capability": matches[0] if matches else None}
            elif args.operation == "search":
                if not args.query:
                    raise ValueError("technical search requires a query")
                result = search_catalog(catalog, args.query, component=args.component)
            elif args.operation == "objective":
                if not args.query:
                    raise ValueError("technical objective requires an objective description")
                result = objective_catalog(catalog, args.query)
                fact_matches = search_catalog(
                    {"entries": [e for e in catalog["entries"] if e.get("kind") == "confirmed_technical_fact"]},
                    args.query, component=args.component)["matches"]
                result["confirmed_technical_facts"] = fact_matches
                if result.get("status") == "UNSUPPORTED_OR_UNKNOWN" and fact_matches:
                    result["knowledge_assessment"] = "known_findings_without_reviewed_guide"
                    result["message"] = ("No reviewed objective guide matches. Relevant recorded findings are available; "
                        "inspect their sources and limitations before deciding whether they support this task.")
            else:
                raise ValueError(f"unsupported technical operation: {args.operation}")
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("status") not in ("NOT_FOUND", "NOTFOUND", "UNSUPPORTED_OR_UNKNOWN") else 1
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
            print(json.dumps({"status": "REJECTED", "error": str(exc)}, indent=2))
            return 2
    if args.command == "agent-session":
        import time
        from .agent import (OpenKoreAgentSession, build_move_eventmacro,
                            execute_interactive_request, export_eventmacro_source,
                            validate_eventmacro_source)
        from .openkore_runtime import requires_profile_authorization
        from .knowledge import KnowledgeStore
        from .openkore_profile import (find_server_profile, read_openkore_credentials,
                                       validate_selection)
        try:
            if not 1 <= args.timeout <= 900:
                raise ValueError("--timeout must be between 1 and 900 seconds")
            if args.interactive and not 60 <= args.session_duration <= 7200:
                raise ValueError("--session-duration must be between 60 and 7200 seconds")
            if args.interactive and args.action != "observe":
                raise ValueError("interactive mode accepts actions only through JSONL requests; leave --action as observe")
            if args.openkore is None:
                raise ValueError("--openkore is required so the selected installation can be checked against the technical catalog")
            openkore_path = args.openkore.expanduser().resolve()
            attached = args.attach_console is not None
            if attached:
                if args.scenario or args.profile_config or any(value is not None for value in
                        (args.profile, args.character, args.server_index, args.character_slot)):
                    raise ValueError("--attach-console cannot be combined with startup scenarios or profile selection")
                if not args.observer_file:
                    raise ValueError("--attach-console requires --observer-file for structured, independently observed outcomes")
                if args.eventmacro_source or args.generate_move_eventmacro or args.run_eventmacro or args.replacement_eventmacro_source:
                    raise ValueError("eventMacro source lifecycle requires a KoreLab-managed startup session; attached sessions may use already loaded native commands only")
                profile_config = None
                settings = {}
                scenario = {"name": "agent_attached_session", "identity": {
                    "master": "attached", "username": "", "character": "", "character_slot": 0},
                    "openkore": {}, "maximum_duration_seconds": args.session_duration if args.interactive else args.timeout + 30}
                selected_profile = "attached_native_socket"
            else:
                profile_config = (args.profile_config.expanduser().resolve() if args.profile_config
                                  else openkore_path / "control/config.txt")
                settings, _secrets = read_openkore_credentials(profile_config)
            selection_requested = any(value is not None for value in
                                      (args.profile, args.character, args.server_index, args.character_slot))
            if attached:
                pass
            elif args.scenario and selection_requested:
                raw = json.loads(args.scenario.expanduser().resolve().read_text(encoding="utf-8"))
                scenario = {"name": raw.get("name", "agent_session"), "openkore": raw.get("openkore", {}),
                            "maximum_duration_seconds": raw.get("maximum_duration_seconds", 180)}
            elif args.scenario:
                scenario = load_scenario(args.scenario.expanduser().resolve())
            else:
                scenario = {"name": "agent_session", "openkore": {}, "maximum_duration_seconds": 180}
            scenario.setdefault("openkore", {})
            if args.interactive:
                scenario["maximum_duration_seconds"] = args.session_duration
            if attached:
                pass
            elif selection_requested:
                if not args.profile or not args.character:
                    raise ValueError("explicit profile selection requires both --profile and --character")
                profile = find_server_profile(openkore_path, args.profile)
                index = args.server_index if args.server_index is not None else int(settings.get("server", "0"))
                slot = args.character_slot if args.character_slot is not None else int(settings.get("char", "0"))
                selection = validate_selection(args.profile, index, args.character, slot)
                scenario["identity"] = {"master": selection["profile"], "username": settings.get("username", ""),
                    "character": selection["character"], "character_slot": selection["character_slot"]}
                if not scenario["identity"]["username"]:
                    raise ValueError("selected OpenKore profile config does not contain a username")
                scenario["openkore"]["profile_selection"] = selection
                selected_profile = profile["name"]
            else:
                if not args.scenario:
                    raise ValueError("provide a scenario or explicitly select --profile and --character")
                scenario["identity"]
                selected_profile = scenario["identity"]["master"]
                if not settings.get("master"):
                    raise ValueError("selected OpenKore profile config has no master server profile")
                if args.profile_config:
                    scenario["openkore"]["profile_config_path"] = str(profile_config)
            if not attached:
                scenario["openkore"]["profile_config_path"] = str(profile_config)
            if args.action == "move" and (not args.map or args.x is None or args.y is None):
                raise ValueError("move requires --map, --x and --y")
            if args.action == "command" and not args.operation:
                raise ValueError("command requires a read-only --operation")
            if args.action == "npc-dialogue" and not args.npc:
                raise ValueError("npc-dialogue requires --npc")
            authorized = set(args.authorize)
            if args.allow_eventmacro_side_effects:
                authorized.add("eventmacro")
            profile = scenario["openkore"]
            if args.interactive and not attached and "openkore-config" in authorized:
                declared_runtime_keys = set((profile.get("config_overrides") or {}).keys())
                if not declared_runtime_keys:
                    raise ValueError("openkore-config requires keys predeclared in the isolated scenario config_overrides")
            else:
                declared_runtime_keys = set()
            runtime_profile_requested = requires_profile_authorization(profile)
            if runtime_profile_requested and "openkore-profile" not in authorized:
                print(json.dumps({"status": "AUTHORIZATION_REQUIRED",
                    "required": "--authorize openkore-profile",
                    "reason": "the requested OpenKore profile can change autonomous gameplay behavior"},
                    indent=2, ensure_ascii=False))
                return 2
            if "eventmacro" in authorized and not args.run_eventmacro and not args.interactive:
                raise ValueError("eventMacro authorization requires --run-eventmacro")
            macro_source = args.eventmacro_source.expanduser().resolve() if args.eventmacro_source else None
            if args.interactive and macro_source and "eventmacro" not in authorized:
                raise ValueError("interactive eventMacro source loading requires --authorize eventmacro")
            if args.interactive and "eventmacro" in authorized and not macro_source:
                raise ValueError("interactive eventMacro authorization requires --eventmacro-source")
            if args.generate_move_eventmacro:
                if macro_source:
                    raise ValueError("choose --eventmacro-source or --generate-move-eventmacro, not both")
                if args.x is None or args.y is None:
                    raise ValueError("generated move macro requires --x and --y")
            if args.run_eventmacro or (args.interactive and macro_source):
                OpenKoreAgentSession.require_permission("eventmacro", authorized)
                if not macro_source and not args.generate_move_eventmacro:
                    raise ValueError("eventMacro execution requires a source file or generated movement macro")
            if args.allow_eventmacro_side_effects and not args.run_eventmacro and not args.interactive:
                raise ValueError("eventMacro authorization requires --run-eventmacro")
            replacement_source = (args.replacement_eventmacro_source.expanduser().resolve()
                                  if args.replacement_eventmacro_source else None)
            if replacement_source and not args.run_eventmacro:
                raise ValueError("source replacement requires an initial --run-eventmacro")
            if args.replacement_run_eventmacro and not replacement_source:
                raise ValueError("replacement macro execution requires --replacement-eventmacro-source")
            initial_macros = validate_eventmacro_source(macro_source) if macro_source else []
            replacement_macros = validate_eventmacro_source(replacement_source) if replacement_source else []
            if args.run_eventmacro and args.run_eventmacro not in initial_macros and not args.generate_move_eventmacro:
                raise ValueError("requested macro name is not declared in the supplied source")
            replacement_name = args.replacement_run_eventmacro or args.run_eventmacro
            if replacement_source and replacement_name not in replacement_macros:
                raise ValueError("replacement source does not declare the requested macro name")
            if args.interrupt_after is not None and (args.interrupt_after <= 0 or args.interrupt_after >= args.timeout):
                raise ValueError("--interrupt-after must be positive and shorter than --timeout")
            expected_map = args.expect_map or (args.map if args.action == "move" else None)
            expected_position = None
            target_x = args.expect_x if args.expect_x is not None else (args.x if args.action == "move" else None)
            target_y = args.expect_y if args.expect_y is not None else (args.y if args.action == "move" else None)
            if target_x is not None or target_y is not None:
                if not expected_map or target_x is None or target_y is None:
                    raise ValueError("objective verification requires --expect-map, --expect-x and --expect-y")
                expected_position = {"x": target_x, "y": target_y}
            perl_path = None if attached else preflight_perl(openkore_path)
            run_dir = RUNS_DIR / ("agent-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
            run_dir.mkdir(parents=True, exist_ok=False)
            set_private_permissions(run_dir, 0o700)
            if args.generate_move_eventmacro:
                generated = run_dir / "eventmacro-generated.txt"
                generated.write_text(build_move_eventmacro(args.generate_move_eventmacro, args.x, args.y), encoding="utf-8")
                set_private_permissions(generated, 0o600)
                macro_source = generated
                initial_macros = [args.generate_move_eventmacro]
            if macro_source:
                scenario["openkore"]["eventmacro_source"] = str(macro_source)
            from .technical_resolution import resolve_agent_action
            resolutions = []
            if args.action in {"observe", "command", "move", "npc-dialogue"}:
                action_resolution = resolve_agent_action(args.action,
                    catalog_path=args.technical_catalog,
                    facts_path=DEFAULT_FACTS,
                    openkore_root=openkore_path,
                    operation=args.operation if args.action == "command" else None,
                    supplied_arguments=({"map": args.map, "x": args.x, "y": args.y} if args.action == "move"
                                        else {"npc": args.npc} if args.action == "npc-dialogue" else None))
                action_resolution["decision_evidence"]["permission_context"]["caller_authorized"] = (
                    args.action == "move" and "move" in authorized or
                    args.action == "npc-dialogue" and "npc" in authorized)
                resolutions.append(action_resolution)
                required_authorization = "move" if args.action == "move" else "npc" if args.action == "npc-dialogue" else None
                if required_authorization and required_authorization not in authorized:
                    shutil.rmtree(run_dir, ignore_errors=True)
                    print(json.dumps({"status": "AUTHORIZATION_REQUIRED", "required": required_authorization,
                        "technical_resolution": action_resolution}, indent=2, ensure_ascii=False))
                    return 2
                if args.action in {"move", "npc-dialogue"} and action_resolution["status"] != "RESOLVED":
                    shutil.rmtree(run_dir, ignore_errors=True)
                    print(json.dumps({"status": "TECHNICAL_REVIEW_REQUIRED",
                        "technical_resolution": action_resolution}, indent=2, ensure_ascii=False))
                    return 2
            if args.run_eventmacro:
                macro_resolution = resolve_agent_action("eventmacro",
                    catalog_path=args.technical_catalog,
                    facts_path=DEFAULT_FACTS,
                    openkore_root=openkore_path, artifact=macro_source)
                macro_resolution["decision_evidence"]["permission_context"]["caller_authorized"] = True
                resolutions.append(macro_resolution)
                if macro_resolution["status"] != "RESOLVED":
                    shutil.rmtree(run_dir, ignore_errors=True)
                    print(json.dumps({"status": "TECHNICAL_REVIEW_REQUIRED",
                        "technical_resolution": macro_resolution}, indent=2, ensure_ascii=False))
                    return 2
            if attached:
                from .adapters.openkore_socket import OpenKoreSocketAdapter
                adapter = OpenKoreSocketAdapter(args.attach_console, args.observer_file,
                    time.monotonic() + int(scenario.get("maximum_duration_seconds", 180)))
            else:
                adapter = OpenKoreAdapter(openkore_path, scenario, run_dir,
                    time.monotonic() + int(scenario.get("maximum_duration_seconds", 180)), perl_path)
            try:
                adapter.start()
                session = OpenKoreAgentSession(adapter, KnowledgeStore(args.knowledge_root, args.server_id),
                    mutable_config_keys=declared_runtime_keys)
                result = {"status": "OBSERVED", "server_id": args.server_id,
                          "technical_resolutions": resolutions,
                          "openkore_runtime_profile": adapter.runtime_profile_report,
                          "connection": ({"mode": "attached_native_socket", "openkore_process_stopped": False}
                              if attached else {"profile": selected_profile,
                                         "server_index": scenario.get("openkore", {}).get("profile_selection", {}).get("server_index", settings.get("server", "0")),
                                         "character_slot": scenario["identity"]["character_slot"]}),
                          "initial_state": ({"observation": next((row for row in reversed(adapter._observer_rows())
                              if row.get("type") == "state"), None),
                              "status": "ATTACHED_WITHOUT_REQUIRING_IN_GAME_STATE"}
                              if attached and args.action == "command" and not args.interactive
                              else session.observe_and_store())}
                if args.action == "observe":
                    result["action_result"] = session.perform_action("observe", timeout=args.timeout)
                elif args.action == "command":
                    result["action_result"] = session.perform_action("command",
                        inputs={"operation": args.operation}, timeout=args.timeout)
                elif args.action == "move":
                    result["action_result"] = session.perform_action("move",
                        inputs={"map": args.map, "x": args.x, "y": args.y}, authorized=authorized, timeout=args.timeout)
                elif args.action == "npc-dialogue":
                    result["action_result"] = session.perform_action("npc-dialogue",
                        inputs={"npc": args.npc}, authorized=authorized, timeout=args.timeout)
                if resolutions and args.action in {"observe", "command", "move", "npc-dialogue"}:
                    result["action_result"]["technical_resolution"] = resolutions[0]
                if result.get("action_result", {}).get("status") == "VERIFIED":
                    result["status"] = "PASS"
                elif result.get("action_result", {}).get("status") in ("FAILED", "TIMEOUT", "NO_RESPONSE"):
                    result["status"] = "FAIL"
                final_source = macro_source
                if args.run_eventmacro:
                    first = session.run_eventmacro(args.run_eventmacro, authorized=True,
                        timeout=args.timeout, expected_map=expected_map,
                        expected_position=expected_position, interrupt_after=args.interrupt_after)
                    first["technical_resolution"] = next(item for item in resolutions if item["action"] == "eventmacro")
                    result["eventmacro"] = {"initial": first}
                    final_macro_result = first
                    if replacement_source:
                        result["eventmacro"]["replacement_stop"] = session.stop_eventmacro()
                        if result["eventmacro"]["replacement_stop"]["status"] == "UNCONFIRMED":
                            raise RuntimeError("native eventMacro stop state was not confirmed before source replacement")
                        result["eventmacro"]["replacement"] = session.replace_eventmacro(replacement_source)
                        adapter.scenario.setdefault("openkore", {})["eventmacro_source"] = str(replacement_source)
                        final_source = replacement_source
                        second = session.run_eventmacro(replacement_name, authorized=True,
                            timeout=args.timeout, expected_map=expected_map,
                            expected_position=expected_position)
                        second["technical_resolution"] = next(item for item in resolutions if item["action"] == "eventmacro")
                        result["eventmacro"]["rerun"] = second
                        final_macro_result = second
                    result["status"] = ("PASS" if final_macro_result["lifecycle_status"] == "COMPLETED"
                                        and final_macro_result["objective_status"] == "VERIFIED" else "FAIL"
                                        if final_macro_result["objective_status"] == "FAILED" else "OBSERVED")
                    if args.export_eventmacro:
                        if final_macro_result["objective_status"] != "VERIFIED" or final_source is None:
                            raise RuntimeError("eventMacro export requires an independently verified objective result")
                        result["eventmacro"]["export"] = export_eventmacro_source(
                            final_source, args.export_eventmacro, runtime_profile=profile)
                if args.interactive:
                    console_input = _ConsoleInput() if os.name == "nt" else None
                    result["interactive_actions"] = []
                    print(json.dumps({"event": "session_started", "status": "PASS",
                        "server_id": args.server_id, "profile": selected_profile,
                        "character_slot": scenario["identity"]["character_slot"],
                        "loaded_macros": initial_macros,
                        "observation": result["initial_state"]["observation"]}, ensure_ascii=False), flush=True)
                    allowed_macros = set(initial_macros) | set(replacement_macros)
                    while True:
                        remaining = adapter.deadline - time.monotonic()
                        if remaining <= 0:
                            expired = {"status": "SESSION_TIMEOUT", "error": "interactive session lifetime expired"}
                            result["interactive_actions"].append(expired)
                            print(json.dumps(expired, ensure_ascii=False), flush=True)
                            break
                        if adapter.process and adapter.process.poll() is not None:
                            disconnected = {"status": "CLIENT_EXITED", "error": "OpenKore process exited during the interactive session"}
                            result["interactive_actions"].append(disconnected)
                            print(json.dumps(disconnected, ensure_ascii=False), flush=True)
                            break
                        if console_input:
                            raw_request = console_input.get(min(1.0, remaining))
                            if raw_request == "":
                                continue
                        else:
                            import select
                            readable, _, _ = select.select([sys.stdin], [], [], min(1.0, remaining))
                            if not readable:
                                continue
                            raw_request = sys.stdin.readline()
                        if not raw_request:
                            break
                        request = None
                        try:
                            if len(raw_request) > 1_000_000:
                                raise ValueError("interactive request exceeds 1 MB")
                            request = json.loads(raw_request)
                            if not isinstance(request, dict):
                                raise ValueError("request must be a JSON object")
                            if request.get("action") in {"stop", "quit"}:
                                if set(request) - {"request_id", "action"}:
                                    raise ValueError("stop request accepts only request_id and action")
                                stop_result = {"request_id": request.get("request_id"),
                                    "action": request.get("action"), "status": "SESSION_CLOSING"}
                                result["interactive_actions"].append(stop_result)
                                print(json.dumps(stop_result, ensure_ascii=False), flush=True)
                                break
                            action_name = request.get("action")
                            supplied = ({"map": request.get("map"), "x": request.get("x"), "y": request.get("y")}
                                        if action_name == "move" else {"npc": request.get("npc")}
                                        if action_name == "npc-dialogue" else
                                        {"key": request.get("key"), "value": request.get("value")}
                                        if action_name == "config-set" else
                                        {"item_id": request.get("item_id"),
                                         "client_index": request.get("client_index")}
                                        if action_name == "pickup-item" else None)
                            if action_name == "spawn-monsters":
                                supplied = {"aegis_name": request.get("aegis_name"),
                                            "count": request.get("count")}
                            elif action_name == "load-test-npc":
                                supplied = {"npc_name": request.get("npc_name"),
                                            "relative_file": request.get("relative_file")}
                            elif action_name == "unload-test-npc":
                                supplied = {"npc_name": request.get("npc_name"),
                                            "relative_file": request.get("relative_file")}
                            resolution_action = ("eventmacro" if action_name in {"eventmacro-start", "eventmacro-stop",
                                                                                  "eventmacro-reload"}
                                                 else action_name)
                            action_resolution = resolve_agent_action(resolution_action,
                                catalog_path=args.technical_catalog,
                                facts_path=DEFAULT_FACTS,
                                openkore_root=openkore_path,
                                operation=request.get("operation") if action_name == "command" else None,
                                supplied_arguments=supplied,
                                artifact=(pathlib.Path(request["source"]).expanduser().resolve()
                                          if action_name == "eventmacro-reload" and isinstance(request.get("source"), str)
                                          else macro_source if resolution_action == "eventmacro" else None))
                            if resolution_action != action_name:
                                action_resolution["requested_action"] = action_name
                            required_permission = {"move": "move", "npc-dialogue": "npc",
                                                   "ai-mode": "ai-mode",
                                                   "pickup-item": "pickup-item",
                                                   "spawn-monsters": "spawn-monsters",
                                                   "load-test-npc": "npc-fixture",
                                                   "unload-test-npc": "npc-fixture",
                                                   "config-set": "openkore-config",
                                                   "eventmacro": "eventmacro", "eventmacro-start": "eventmacro",
                                                   "eventmacro-stop": "eventmacro", "eventmacro-reload": "eventmacro"}.get(action_name)
                            action_resolution["decision_evidence"]["permission_context"]["caller_authorized"] = (
                                required_permission is None or required_permission in authorized)
                            if required_permission and required_permission not in authorized:
                                raise PermissionError(f"action requires --authorize {required_permission}")
                            if required_permission and action_resolution["status"] != "RESOLVED":
                                raise RuntimeError("TECHNICAL_REVIEW_REQUIRED: action knowledge is not currently resolved")
                            entry = execute_interactive_request(session, request,
                                authorized=authorized, loaded_macros=allowed_macros,
                                default_timeout=args.timeout)
                            if (action_name == "eventmacro-reload"
                                    and entry.get("result", {}).get("status") == "LOADED"):
                                # Subsequent preflight must resolve the source now loaded by the
                                # native plugin, not keep attributing actions to the startup file.
                                macro_source = pathlib.Path(request["source"]).expanduser().resolve()
                            entry["technical_resolution"] = action_resolution
                            result["interactive_actions"].append(entry)
                            print(json.dumps(entry, ensure_ascii=False), flush=True)
                        except (ValueError, KeyError, RuntimeError, PermissionError, TimeoutError) as exc:
                            current_request = locals().get("request")
                            rejected = {"request_id": (current_request.get("request_id")
                                if isinstance(current_request, dict) else None),
                                "status": ("AUTHORIZATION_REQUIRED" if isinstance(exc, PermissionError) else
                                           "TIMEOUT" if isinstance(exc, TimeoutError) else
                                           "TECHNICAL_REVIEW_REQUIRED" if "TECHNICAL_REVIEW_REQUIRED" in str(exc) else
                                           "ACTION_FAILED" if isinstance(exc, RuntimeError) else "REJECTED"),
                                "error": f"{type(exc).__name__}: {exc}"}
                            result["interactive_actions"].append(rejected)
                            print(json.dumps(rejected, ensure_ascii=False), flush=True)
                    if any(entry.get("status") in ("REJECTED", "AUTHORIZATION_REQUIRED", "TECHNICAL_REVIEW_REQUIRED",
                                                    "TIMEOUT", "ACTION_FAILED", "SESSION_TIMEOUT", "CLIENT_EXITED")
                           or entry.get("result", {}).get("status") in ("FAILED", "TIMEOUT", "INTERRUPTED", "NO_RESPONSE")
                           or entry.get("result", {}).get("status") == "CLIENT_EXITED"
                           or (entry.get("action") != "eventmacro-stop"
                               and entry.get("result", {}).get("lifecycle_status") in ("FAILED", "TIMEOUT", "INTERRUPTED"))
                           or entry.get("result", {}).get("objective_status") == "FAILED"
                           for entry in result["interactive_actions"]):
                        result["status"] = "FAIL"
                adapter.archive_observer()
            finally:
                temporary_workspace = adapter.temp_path
                adapter.close()
                if "result" in locals():
                    result.setdefault("openkore_runtime_profile", adapter.runtime_profile_report)
                    result["openkore_runtime_profile"]["temporary_workspace_removed"] = not temporary_workspace.exists()
            result["evidence_directory"] = str(run_dir)
            result["evidence_file"] = str(run_dir / "agent-session.json")
            evidence = run_dir / "agent-session.json"
            evidence.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            set_private_permissions(evidence, 0o600)
            if args.interactive:
                print(json.dumps({"event": "session_closed", "status": result["status"],
                    "interactive_action_count": len(result.get("interactive_actions", [])),
                    "evidence_file": result["evidence_file"]}, ensure_ascii=False), flush=True)
            else:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["status"] in ("PASS", "OBSERVED") else 1
        except (ValueError, KeyError, FileNotFoundError, OSError, RuntimeError, TimeoutError, PermissionError) as exc:
            from .adapters.openkore import OpenKoreStageError
            details = ({"classification": exc.classification, "stage": exc.stage}
                       if isinstance(exc, OpenKoreStageError)
                       else {"classification": type(exc).__name__, "error": str(exc)})
            print(json.dumps({"status": "FAILED", **details}, indent=2))
            return 1
    if args.command == "plan":
        from .planner import PlanningError, plan_objective_file, write_planned_scenario
        try:
            plan = plan_objective_file(args.objective, args.rathena, args.openkore)
            scenario_path = write_planned_scenario(plan, args.output)
            print(json.dumps({"status": plan["status"], "objective_id": plan["objective_id"],
                "scenario_path": str(scenario_path),
                "artifact_paths": plan.get("artifact_paths", {}),
                "input_provenance": plan.get("input_provenance", []),
                "selected_capabilities": plan["selected_capabilities"],
                "action_plan": plan["action_plan"],
                "semantic_checks": plan["semantic_checks"]}, indent=2))
            return 0
        except PlanningError as exc:
            print(json.dumps({"status": "REJECTED", "classification": exc.code,
                              "error": str(exc), "details": exc.details}, indent=2))
            return 2
        except (ValueError, KeyError, FileNotFoundError, OSError, RuntimeError) as exc:
            print(json.dumps({"status": "REJECTED", "classification": "planning_input_or_environment_error",
                              "error": f"{type(exc).__name__}: {exc}"}, indent=2))
            return 2
    if args.command == "quests":
        catalog = load_quest_catalog(args.rathena)
        rows = catalog["quests"]
        if args.id is not None:
            rows = [row for row in rows if int(row["id"]) == args.id]
        ids = tuple(int(row["id"]) for row in rows)
        print(json.dumps({"mode": catalog["mode"], "sources": catalog["sources"],
                          "count": len(rows), "quests": rows,
                          "related_scripts": find_quest_scripts(args.rathena, ids),
                          "interpretation_scope": "declarative targets and common direct script primitives; server remains authoritative"},
                         indent=2))
        return 0 if rows else 1
    if args.command == "assess":
        from .adapters.rathena import RathenaAdapter
        scenario = load_scenario(args.scenario.expanduser().resolve())
        rathena = RathenaAdapter(args.rathena)
        character = rathena.character(scenario["identity"]["character"])
        action = scenario["action"]
        character.update(scenario.get("prepare", {}).get("attributes", {}))
        character["skills"] = rathena.character_skills(character)
        character["skills"].update({str(skill_id): int(level) for skill_id, level in
                                    scenario.get("prepare", {}).get("skills", {}).items()})
        if (action.get("intent") in ("skill", "resistance")
                and "job_class_id" in scenario.get("server_setup", {})
                and int(scenario.get("server_setup", {}).get("skill_points", 0))
                >= int(action.get("skill_level", 1))):
            character["skills"][str(action["skill_id"])] = int(action.get("skill_level", 1))
        catalog = load_monster_catalog(args.rathena)
        target = find_monster(catalog, scenario["monster"])
        comparison_target = (find_monster(catalog, scenario["comparison_monster"] )
                             if "comparison_monster" in scenario else None)
        assessment = assess_match(character, target, action.get("intent", "victory"),
            allow_extreme=bool(action.get("allow_extreme", False)),
            reason=str(action.get("extreme_reason", "")),
            duration_seconds=float(action.get("duration_seconds", 30)),
            skill_id=str(action["skill_id"]) if "skill_id" in action else None,
            skill_level=int(action.get("skill_level", 1)))
        print(json.dumps({"catalog_mode": catalog["mode"], "monster": target,
                          "comparison_monster": comparison_target,
                          "assessment": assessment}, indent=2))
        return 0
    from .runner import run
    try:
        report = run(args.scenario.expanduser().resolve(), args.openkore, args.rathena, args.output)
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
