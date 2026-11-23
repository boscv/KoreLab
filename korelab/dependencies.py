"""Capability-oriented dependency discovery and explicit setup planning."""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .processes import local_listener_processes, process_is_under


@dataclass(frozen=True)
class Provider:
    id: str
    capability: str
    installed: bool
    compatible: bool
    reliability: int
    source: str
    version: str | None = None
    configured: bool = False
    running: bool = False
    verified: bool = False
    detail: str = ""


def _version(command: str) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return next((line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()), None)


def _module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _local_service(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _port_setting(name: str, default: int, errors: list[str]) -> int:
    value = os.environ.get(name, str(default))
    try:
        port = int(value)
    except ValueError:
        errors.append(f"{name} must be an integer TCP port")
        return 0
    if not 1 <= port <= 65535:
        errors.append(f"{name} must be between 1 and 65535")
        return 0
    return port


def _path_env(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else None


def _package_managers() -> list[dict[str, str]]:
    names = {"Windows": ("winget", "choco", "scoop"),
             "Darwin": ("brew",), "Linux": ("apt-get", "dnf", "zypper", "pacman")}.get(
                 platform.system(), ())
    return [{"id": name, "path": shutil.which(name)} for name in names if shutil.which(name)]


def discover(*, verify_database: bool = False, openkore_path: Path | None = None,
             rathena_path: Path | None = None) -> dict:
    """Inspect local providers and explicitly configured endpoints only."""
    system = platform.system()
    arch = platform.machine().lower()
    is_x64 = arch in {"x86_64", "amd64", "x64"}
    configured_db = any(name in os.environ for name in (
        "KORELAB_DB_HOST", "KORELAB_DB_PORT", "KORELAB_DB_USER", "KORELAB_DB_PASSWORD",
        "KORELAB_CHAR_DB", "KORELAB_LOGIN_DB"))
    db_host = os.environ.get("KORELAB_DB_HOST", "127.0.0.1")
    configuration_errors: list[str] = []
    db_port = _port_setting("KORELAB_DB_PORT", 3306, configuration_errors)
    local_host = db_host in {"localhost", "127.0.0.1", "::1"}

    providers: list[Provider] = []
    providers.append(Provider("python-current", "python-runtime", True, sys.version_info >= (3, 10),
        100, "current-process", platform.python_version(), detail=sys.executable))
    for name in ("perl",):
        version = _version(name)
        providers.append(Provider(f"{name}-runtime", "openkore-perl-runtime", bool(shutil.which(name)),
            bool(version), 80, "native-tool", version, detail=shutil.which(name) or "not found"))
    for executable, provider_id in (("mariadb", "mariadb-client"), ("mysql", "mysql-client")):
        path = shutil.which(executable)
        providers.append(Provider(provider_id, "rathena-mysql-protocol", bool(path), bool(path),
            70 if executable == "mariadb" else 65, "native-tool", _version(executable),
            detail=path or "not found"))
    for executable in ("mariadbd", "mysqld"):
        path = shutil.which(executable)
        providers.append(Provider(f"{executable}-server", "mysql-compatible-server-software",
            bool(path), bool(path), 85, "native-tool", _version(executable), detail=path or "not found"))
    for module, provider_id, reliability in (("pymysql", "pymysql", 90),
                                               ("mysql.connector", "mysql-connector-python", 90),
                                               ("mariadb", "mariadb-python", 85)):
        installed = _module(module)
        providers.append(Provider(provider_id, "rathena-mysql-protocol", installed, installed,
            reliability, "python-driver", configured=installed,
            detail=f"Python module {module}" if installed else f"Python module {module} is not installed"))
        providers.append(Provider(provider_id, "rathena-database-verification", installed, installed,
            reliability, "python-driver", configured=installed,
            detail=f"Python module {module} can run schema/transaction probes" if installed else
                   f"Python module {module} is required for schema/transaction probes"))
    providers.extend((
        Provider("file-jsonl", "korelab-local-storage", True, True, 100,
                 "built-in", detail="Default append-only file storage"),
        Provider("sqlite3", "korelab-local-storage", _module("sqlite3"), _module("sqlite3"), 85,
                 "python-standard-library", detail="Optional local SQLite backend"),
    ))
    openkore = (openkore_path.expanduser().resolve() if openkore_path else _path_env("KORELAB_OPENKORE"))
    rathena = (rathena_path.expanduser().resolve() if rathena_path else _path_env("KORELAB_RATHENA"))
    openkore_ok = bool(openkore and (openkore / "openkore.pl").is_file())
    rathena_ok = bool(rathena and (rathena / "conf" / "inter_athena.conf").is_file())
    xstools = {"status": "NOT_CONFIGURED", "detail": "Set KORELAB_OPENKORE to inspect its Perl/XSTools build."}
    if openkore_ok:
        try:
            from .runner import preflight_perl
            perl_path = preflight_perl(openkore)
            xstools = {"status": "VERIFIED", "perl": str(perl_path),
                       "detail": "Current Perl matches the installed native XSTools build marker."}
        except Exception as exc:
            xstools = {"status": "INCOMPATIBLE", "detail": f"{type(exc).__name__}: {exc}"}
    providers.extend((
        Provider("openkore-source", "openkore-source", openkore_ok, openkore_ok, 100,
                 "configured-path", configured=bool(openkore), detail=str(openkore) if openkore else "set KORELAB_OPENKORE"),
        Provider("rathena-source", "rathena-source", rathena_ok, rathena_ok, 100,
                 "configured-path", configured=bool(rathena), detail=str(rathena) if rathena else "set KORELAB_RATHENA"),
    ))
    # Do not probe a configured remote host unless the user explicitly asks
    # for `doctor --verify-database`; local endpoint checks are harmless.
    db_running = bool(db_port and (local_host or verify_database)
                      and _local_service(db_host, db_port))
    providers.append(Provider("configured-mysql-service", "rathena-database-service", False,
        False, 100 if local_host else 70, "configured-endpoint", configured=configured_db,
        running=db_running, verified=False,
        detail="Configured endpoint values are not echoed" if configured_db else
               f"Default local endpoint {db_host}:{db_port}; accepts TCP but engine/process identity is not verified"))
    providers.append(Provider("user-configured-database", "rathena-database-connection",
        configured_db, True, 90, "environment", configured=configured_db,
        running=db_running if local_host else False, verified=False,
        detail="Endpoint values are not echoed; remote endpoints are checked only with --verify-database"))

    service_ports = {"login": _port_setting("KORELAB_LOGIN_PORT", 6900, configuration_errors),
                     "character": _port_setting("KORELAB_CHAR_PORT", 6121, configuration_errors),
                     "map": _port_setting("KORELAB_MAP_PORT", 5121, configuration_errors)}
    service_identity = {}
    for role, port in service_ports.items():
        owners = local_listener_processes(port) if port else []
        matches = [owner for owner in owners if rathena and process_is_under(owner["path"], rathena)]
        service_identity[role] = {"port": port, "listener_detected": bool(owners),
            "owner_paths": [str(Path(owner["path"]).name) for owner in owners],
            "expected_rathena_process_verified": bool(matches) if rathena else False,
            "verification": "matched configured rAthena checkout" if matches else
                            "unverified; configure KORELAB_RATHENA and inspect process ownership"}
    database_owners = local_listener_processes(db_port) if local_host and db_port else []

    # Process identity and configuration are not inferred from a listening port.
    # The adapter performs the actual server/schema/permission probe on request.
    if verify_database:
        if not configured_db:
            verification = {"status": "NOT_CONFIGURED", "message": "Set KORELAB_DB_HOST and schema/user settings before database verification."}
        elif not db_running:
            verification = {"status": "UNAVAILABLE", "message": "The explicitly configured MySQL/MariaDB endpoint did not accept a connection."}
        else:
            verification = _verify_rathena_database()
    else:
        verification = {"status": "NOT_REQUESTED"}

    ranked: dict[str, list[dict]] = {}
    preference = os.environ.get("KORELAB_DB_PROVIDER")
    mysql_provider_ids = {p.id for p in providers
                          if p.capability == "rathena-mysql-protocol"
                          and p.installed and p.compatible}
    if preference and preference not in mysql_provider_ids:
        configuration_errors.append(
            "KORELAB_DB_PROVIDER does not match an installed provider ID; run `python -m korelab doctor` to list providers")
    for capability in sorted({p.capability for p in providers}):
        candidates = [p for p in providers if p.capability == capability]
        preferred = preference if capability == "rathena-mysql-protocol" else None
        candidates.sort(key=lambda p: (p.id == preferred, p.compatible, p.installed,
                                       p.configured, p.reliability), reverse=True)
        ranked[capability] = [asdict(p) for p in candidates]
    return {"system": system, "release": platform.release(), "architecture": arch,
            "native_windows_x64": system == "Windows" and is_x64,
            "python": {"version": platform.python_version(), "executable": sys.executable,
                       "supported": sys.version_info >= (3, 10)},
            "package_managers": _package_managers(), "capabilities": ranked,
            "configuration_errors": configuration_errors,
            "openkore_xstools": xstools,
            "platform_runtime": _platform_runtime_summary(system),
            "local_rathena_services": service_identity,
            "local_database_listeners": [{"process": Path(owner["path"]).name, "port": db_port}
                                          for owner in database_owners],
            "rathena_database_verification": verification,
            "rathena_local_server_process_identity": "verified only when listener executable is inside configured KORELAB_RATHENA"}


def _platform_runtime_summary(system: str) -> dict:
    if system == "Windows":
        import socket
        return {"process_console": "implemented using bounded stdin/stdout pipes; native OpenKore behavior unvalidated",
                "run_lock": "msvcrt byte-range lock",
                "private_files": "protected Windows DACL for current user; ACL application fails closed",
                "service_process_identity": "PowerShell TCP owner and executable-path inspection",
                "socket_attachment": "AF_UNIX availability: " + str(hasattr(socket, "AF_UNIX")) + "; OpenKore Perl socket support requires native validation"}
    return {"process_console": "POSIX PTY",
            "run_lock": "fcntl flock",
            "private_files": "POSIX owner mode",
            "service_process_identity": "lsof or ss executable-path inspection",
            "socket_attachment": "AF_UNIX"}


def _verify_rathena_database() -> dict:
    """Run the opt-in connector-level rAthena schema and transaction probe."""
    from .database import choose_mysql_provider

    provider = choose_mysql_provider()
    if provider is None:
        return {"status": "UNAVAILABLE", "message": "Install a compatible MySQL/MariaDB Python driver or client, or set KORELAB_DB_PROVIDER to a supported provider."}
    if not hasattr(provider, "driver"):
        return {"status": "UNAVAILABLE", "provider": provider.id,
                "message": "Database schema and transaction verification requires a compatible Python MySQL driver; install PyMySQL with `python -m korelab setup --apply --capability rathena-database-verification --provider pip`. The existing command-line client remains usable for normal rAthena queries."}
    try:
        return provider.verify_rathena()
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        for key in ("KORELAB_DB_PASSWORD", "KORELAB_DB_USER", "KORELAB_DB_HOST"):
            secret = os.environ.get(key)
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return {"status": "FAILED", "provider": provider.id,
                "message": f"Database verification failed: {message}"}


def setup_plan(capability: str | None = None) -> dict:
    """Return package-manager choices without changing the machine."""
    report = discover()
    if report.get("configuration_errors"):
        return {"status": "REJECTED", "system": report["system"], "architecture": report["architecture"],
                "configuration_errors": report["configuration_errors"], "choices": [],
                "message": "Correct the reported environment settings before requesting an installation plan."}
    managers = report["package_managers"]
    packages = {
        "openkore-perl-runtime": {"Darwin": {"brew": "perl"},
                                  "Windows": {"winget": "StrawberryPerl.StrawberryPerl"},
                                  "Linux": {"apt-get": "perl"}},
        "rathena-database-verification": {"Darwin": {"pip": "PyMySQL"},
                                          "Windows": {"pip": "PyMySQL"},
                                          "Linux": {"pip": "PyMySQL"}},
        "rathena-mysql-protocol": {"Darwin": {"pip": "PyMySQL"},
                                   "Windows": {"pip": "PyMySQL"},
                                   "Linux": {"pip": "PyMySQL"}},
        "rathena-database-service": {"Darwin": {"brew": "mariadb"},
                                     "Windows": {"winget": "MariaDB.Server"},
                                     "Linux": {"apt-get": "mariadb-server"}},
    }
    plans = []
    if report["python"]["supported"] and capability in (None, "rathena-mysql-protocol"):
        plans.append({"capability": "rathena-mysql-protocol", "provider": "pip",
                      "package": "PyMySQL", "mutating": True,
                      "action": "install only after explicit --apply and provider selection"})
    if report["python"]["supported"] and capability in (None, "rathena-database-verification"):
        plans.append({"capability": "rathena-database-verification", "provider": "pip",
                      "package": "PyMySQL", "mutating": True,
                      "action": "install only after explicit --apply and provider selection"})
    if capability:
        plans = [item for item in plans if item["capability"] == capability]
    for name, platform_packages in packages.items():
        if capability and capability != name:
            continue
        for manager in managers:
            package = platform_packages.get(report["system"], {}).get(manager["id"])
            if package:
                plans.append({"capability": name, "provider": manager["id"], "package": package,
                              "mutating": True,
                              "elevation_may_be_required": name.endswith("service") or manager["id"] == "winget",
                              "action": "install only after explicit --apply and provider selection"})
    available_provider = any(item["installed"] and item["compatible"]
        for item in report["capabilities"].get("rathena-mysql-protocol", []))
    verification_driver = any(item["installed"] and item["compatible"]
        for item in report["capabilities"].get("rathena-database-verification", []))
    server_installed = any(item["installed"] for item in
        report["capabilities"].get("mysql-compatible-server-software", []))
    service_running = any(item["running"] for item in
        report["capabilities"].get("rathena-database-service", []))
    if available_provider:
        plans = [item for item in plans if item["capability"] != "rathena-mysql-protocol"]
    if verification_driver:
        plans = [item for item in plans if item["capability"] != "rathena-database-verification"]
    elif not available_provider and capability is None:
        # One PyMySQL installation fulfills both capabilities; avoid proposing
        # the same package twice in the general setup plan.
        plans = [item for item in plans if item["capability"] != "rathena-mysql-protocol"]
    if server_installed or service_running:
        plans = [item for item in plans if item["capability"] != "rathena-database-service"]
    perl_installed = any(item["installed"] and item["compatible"] for item in
        report["capabilities"].get("openkore-perl-runtime", []))
    if perl_installed:
        plans = [item for item in plans if item["capability"] != "openkore-perl-runtime"]
    capability_group = {"rathena-mysql-protocol": "rathena-mysql-protocol",
                        "rathena-database-verification": "rathena-database-verification",
                        "rathena-database-service": "mysql-compatible-server-software",
                        "openkore-perl-runtime": "openkore-perl-runtime"}.get(
                            capability, "rathena-mysql-protocol")
    available_ids = [item["id"] for item in report["capabilities"].get(
        capability_group, []) if item["installed"] and item["compatible"]]
    return {"status": "PLAN_ONLY", "system": report["system"], "architecture": report["architecture"],
            "detected_package_managers": managers, "choices": plans,
            "available_compatible_providers": available_ids,
            "note": "Existing compatible providers take precedence. Setup never starts a service, edits external source trees, or changes database accounts/passwords."}


def apply_setup(capability: str, provider_id: str, *, allow_elevation: bool = False) -> dict:
    """Apply one reviewed package plan; never elevate or configure services."""
    plan = setup_plan(capability)
    if provider_id in plan["available_compatible_providers"]:
        return {"status": "REUSED", "capability": capability,
                "provider": provider_id,
                "message": "The selected compatible provider is already installed; no duplicate was installed."}
    choice = next((item for item in plan["choices"] if item["provider"] == provider_id), None)
    if not choice:
        raise ValueError("the requested provider is unavailable or incompatible on this operating system")
    if choice.get("elevation_may_be_required") and not allow_elevation:
        raise ValueError("this provider may request elevated permissions; review the package plan and repeat with --allow-elevation only if authorized")
    if capability == "rathena-database-service":
        raise ValueError("automatic database-server installation is disabled; install through the native package manager after reviewing the plan")
    command = {"pip": [sys.executable, "-m", "pip", "install", choice["package"]],
               "brew": ["brew", "install", choice["package"]],
               "winget": ["winget", "install", "--id", choice["package"], "--exact",
                          "--accept-source-agreements", "--accept-package-agreements"],
               "apt-get": ["apt-get", "install", choice["package"]]}.get(provider_id)
    if not command:
        raise ValueError(f"automatic installation is not implemented for provider {provider_id}")
    if provider_id == "apt-get":
        raise ValueError("system package installation may require elevation; run the displayed package-manager command yourself")
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=900)
    except subprocess.TimeoutExpired:
        return {"status": "FAILED", "provider": provider_id, "capability": capability,
                "message": "Package installation exceeded the 15 minute bound; inspect the package manager state and run `korelab doctor` before retrying."}
    return {"status": "APPLIED" if result.returncode == 0 else "FAILED",
            "provider": provider_id, "capability": capability,
            "returncode": result.returncode,
            "message": result.stderr.strip()[-1000:] if result.returncode else "Installation command completed; run `korelab doctor` to verify the provider."}
