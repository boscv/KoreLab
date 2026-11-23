from __future__ import annotations

import os
import io
import queue
import sqlite3
import time
from types import SimpleNamespace

import pytest

from korelab import database, dependencies
from korelab.knowledge import KnowledgeStore
from korelab.locking import try_lock
from korelab.platform_support import _private_sddl, set_private_permissions


def _doctor_report(*, providers=None, managers=None):
    return {
        "system": "Darwin", "architecture": "arm64",
        "python": {"supported": True},
        "package_managers": managers or [{"id": "brew", "path": "/brew"}],
        "capabilities": providers or {},
    }


def test_windows_private_acl_sddl_has_valid_file_and_directory_entries():
    sid = "S-1-5-21-100"
    assert _private_sddl(sid, directory=False) == f"D:P(A;FA;;;{sid})"
    assert _private_sddl(sid, directory=True) == f"D:P(A;OICI;FA;;;{sid})"


def test_doctor_does_not_probe_an_unconfigured_remote_endpoint(monkeypatch):
    monkeypatch.setenv("KORELAB_DB_HOST", "db.example.invalid")
    monkeypatch.delenv("KORELAB_OPENKORE", raising=False)
    monkeypatch.delenv("KORELAB_RATHENA", raising=False)
    monkeypatch.setattr(dependencies, "_local_service",
                        lambda *_: pytest.fail("remote endpoint was contacted"))
    report = dependencies.discover()
    configured = report["capabilities"]["rathena-database-connection"][0]
    assert configured["configured"] is True
    assert configured["running"] is False
    assert "db.example.invalid" not in str(report)


def test_doctor_rejects_invalid_database_port_without_connecting(monkeypatch):
    monkeypatch.setenv("KORELAB_DB_PORT", "not-a-port")
    monkeypatch.delenv("KORELAB_OPENKORE", raising=False)
    monkeypatch.delenv("KORELAB_RATHENA", raising=False)
    monkeypatch.setattr(dependencies, "_local_service",
                        lambda *_: pytest.fail("invalid port must not be probed"))
    report = dependencies.discover()
    assert any("KORELAB_DB_PORT" in error for error in report["configuration_errors"])


def test_doctor_ranks_capabilities_without_provider_preference(monkeypatch):
    monkeypatch.delenv("KORELAB_DB_PROVIDER", raising=False)
    monkeypatch.delenv("KORELAB_OPENKORE", raising=False)
    monkeypatch.delenv("KORELAB_RATHENA", raising=False)
    monkeypatch.setattr(dependencies, "_version", lambda *_: None)
    monkeypatch.setattr(dependencies, "_module", lambda *_: False)
    monkeypatch.setattr(dependencies.shutil, "which", lambda *_: None)
    monkeypatch.setattr(dependencies, "_local_service", lambda *_: False)
    monkeypatch.setattr(dependencies, "local_listener_processes", lambda *_: [])
    report = dependencies.discover()
    assert report["capabilities"]["rathena-mysql-protocol"]
    assert report["capabilities"]["korelab-local-storage"]
    assert report["configuration_errors"] == []


def test_doctor_ranks_preferred_mysql_provider_first(monkeypatch):
    monkeypatch.setenv("KORELAB_DB_PROVIDER", "mysql-client")
    monkeypatch.delenv("KORELAB_OPENKORE", raising=False)
    monkeypatch.delenv("KORELAB_RATHENA", raising=False)
    monkeypatch.setattr(dependencies, "_version", lambda *_: None)
    monkeypatch.setattr(dependencies, "_module", lambda *_: False)
    monkeypatch.setattr(dependencies.shutil, "which",
                        lambda name: "/usr/bin/mysql" if name == "mysql" else None)
    monkeypatch.setattr(dependencies, "_local_service", lambda *_: False)
    monkeypatch.setattr(dependencies, "local_listener_processes", lambda *_: [])
    report = dependencies.discover()
    providers = report["capabilities"]["rathena-mysql-protocol"]
    assert providers[0]["id"] == "mysql-client"
    assert report["configuration_errors"] == []


def test_doctor_rejects_preference_for_uninstalled_mysql_provider(monkeypatch):
    monkeypatch.setenv("KORELAB_DB_PROVIDER", "pymysql")
    monkeypatch.delenv("KORELAB_OPENKORE", raising=False)
    monkeypatch.delenv("KORELAB_RATHENA", raising=False)
    monkeypatch.setattr(dependencies, "_version", lambda *_: None)
    monkeypatch.setattr(dependencies, "_module", lambda *_: False)
    monkeypatch.setattr(dependencies.shutil, "which", lambda *_: None)
    monkeypatch.setattr(dependencies, "_local_service", lambda *_: False)
    monkeypatch.setattr(dependencies, "local_listener_processes", lambda *_: [])
    report = dependencies.discover()
    assert any("KORELAB_DB_PROVIDER" in error for error in report["configuration_errors"])


def test_doctor_verify_failure_returns_nonzero_cli_status(monkeypatch, capsys):
    from korelab import cli

    monkeypatch.setattr("sys.argv", ["korelab", "doctor", "--verify-database"])
    monkeypatch.setattr(dependencies, "discover", lambda **_kwargs: {
        "configuration_errors": [],
        "rathena_database_verification": {"status": "FAILED", "message": "probe failed"},
    })
    assert cli.main() == 1
    assert '"status": "FAILED"' in capsys.readouterr().out


def test_database_verification_error_redacts_configured_connection_values(monkeypatch):
    password, username, host = "private-password-marker", "private-user-marker", "private-host-marker"
    monkeypatch.setenv("KORELAB_DB_PASSWORD", password)
    monkeypatch.setenv("KORELAB_DB_USER", username)
    monkeypatch.setenv("KORELAB_DB_HOST", host)

    class Provider:
        id = "pymysql"
        driver = "pymysql"
        def verify_rathena(self):
            raise RuntimeError(f"connect failed for {username}@{host} using {password}")

    monkeypatch.setattr(database, "choose_mysql_provider", lambda: Provider())
    result = dependencies._verify_rathena_database()
    assert result["status"] == "FAILED"
    assert all(secret not in result["message"] for secret in (password, username, host))


def test_doctor_validates_explicit_source_checkouts(tmp_path, monkeypatch):
    openkore = tmp_path / "openkore"
    rathena = tmp_path / "rathena"
    openkore.mkdir(); rathena.mkdir()
    (openkore / "openkore.pl").write_text("# local")
    (rathena / "conf").mkdir()
    (rathena / "conf/inter_athena.conf").write_text("# config")
    monkeypatch.setattr(dependencies, "_local_service", lambda *_: False)
    report = dependencies.discover(openkore_path=openkore, rathena_path=rathena)
    assert report["capabilities"]["openkore-source"][0]["installed"] is True
    assert report["capabilities"]["rathena-source"][0]["installed"] is True


def test_setup_plan_reuses_existing_mysql_client_without_duplicate_install(monkeypatch):
    report = _doctor_report(providers={
        "rathena-mysql-protocol": [
            {"id": "mysql-client", "installed": True, "compatible": True},
        ],
        "mysql-compatible-server-software": [], "rathena-database-service": [],
    })
    monkeypatch.setattr(dependencies, "discover", lambda: report)
    plan = dependencies.setup_plan("rathena-mysql-protocol")
    assert plan["choices"] == []
    assert plan["available_compatible_providers"] == ["mysql-client"]


def test_setup_apply_reuses_selected_provider_without_invoking_installer(monkeypatch):
    report = _doctor_report(providers={
        "rathena-mysql-protocol": [
            {"id": "mysql-client", "installed": True, "compatible": True},
        ],
        "mysql-compatible-server-software": [], "rathena-database-service": [],
    })
    monkeypatch.setattr(dependencies, "discover", lambda: report)
    monkeypatch.setattr(dependencies.subprocess, "run",
                        lambda *_args, **_kwargs: pytest.fail("installer invoked"))
    result = dependencies.apply_setup("rathena-mysql-protocol", "mysql-client")
    assert result["status"] == "REUSED"


def test_setup_apply_rejects_unknown_provider(monkeypatch):
    report = _doctor_report(providers={"rathena-mysql-protocol": [],
        "mysql-compatible-server-software": [], "rathena-database-service": []})
    monkeypatch.setattr(dependencies, "discover", lambda: report)
    with pytest.raises(ValueError, match="unavailable or incompatible"):
        dependencies.apply_setup("rathena-mysql-protocol", "made-up-client")


def test_setup_apply_requires_explicit_elevation_authorization(monkeypatch):
    report = _doctor_report(managers=[{"id": "winget", "path": "winget.exe"}], providers={
        "rathena-mysql-protocol": [], "rathena-database-verification": [],
        "mysql-compatible-server-software": [], "rathena-database-service": [],
        "openkore-perl-runtime": [],
    })
    report["system"] = "Windows"
    monkeypatch.setattr(dependencies, "discover", lambda: report)
    with pytest.raises(ValueError, match="--allow-elevation"):
        dependencies.apply_setup("openkore-perl-runtime", "winget")


def test_existing_command_client_does_not_hide_optional_python_verifier(monkeypatch):
    report = _doctor_report(providers={
        "rathena-mysql-protocol": [
            {"id": "mysql-client", "installed": True, "compatible": True},
        ],
        "rathena-database-verification": [
            {"id": "pymysql", "installed": False, "compatible": False},
        ],
        "mysql-compatible-server-software": [
            {"id": "mariadbd-server", "installed": True, "compatible": True},
        ], "rathena-database-service": [],
        "openkore-perl-runtime": [
            {"id": "perl-runtime", "installed": True, "compatible": True},
        ],
    })
    monkeypatch.setattr(dependencies, "discover", lambda: report)
    plan = dependencies.setup_plan()
    assert {item["capability"] for item in plan["choices"]} == {"rathena-database-verification"}
    assert plan["choices"][0]["provider"] == "pip"


def test_mysql_driver_selection_respects_user_preference(monkeypatch):
    monkeypatch.setenv("KORELAB_DB_PROVIDER", "mysql-client")
    monkeypatch.setattr(database.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(database.shutil, "which", lambda name: "/usr/bin/mysql" if name == "mysql" else None)
    provider = database.choose_mysql_provider()
    assert provider.id == "mysql-client"


def test_mysql_driver_formats_result_rows_and_keeps_password_out_of_args(monkeypatch):
    seen = {}

    class Cursor:
        description = (("name",), ("optional",))
        def execute(self, statement):
            seen["statement"] = statement
        def fetchall(self):
            return [("row", None)]
        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()
        def commit(self):
            seen["committed"] = True
        def rollback(self):
            seen["rolled_back"] = True
        def close(self):
            seen["closed"] = True

    provider = database.PythonMySQLProvider("pymysql")
    monkeypatch.setattr(provider, "_connection", lambda **_: Connection())
    result = provider.execute("SELECT name, optional FROM test")
    assert result.returncode == 0
    assert result.stdout == "row\t\\N\n"
    assert seen["committed"] and seen["closed"]


@pytest.mark.parametrize("driver", ["pymysql", "mysql-connector-python", "mariadb-python"])
def test_python_mysql_connections_disable_autocommit_for_atomic_batches(monkeypatch, driver):
    seen = {}

    class Connection:
        pass

    def connect(**kwargs):
        seen.update(kwargs)
        return Connection()

    monkeypatch.setattr(database.importlib, "import_module",
                        lambda _name: SimpleNamespace(connect=connect))
    provider = database.PythonMySQLProvider(driver)
    provider._connection()
    assert seen["autocommit"] is False


def test_mysql_cli_batches_are_explicitly_transactional(monkeypatch):
    seen = {}
    monkeypatch.setattr(database.subprocess, "run", lambda command, **kwargs:
        (seen.update(command=command, env=kwargs["env"]) or
         SimpleNamespace(returncode=0, stdout="", stderr="")))
    provider = database.MySQLCLIProvider("/usr/bin/mysql")
    result = provider.execute("UPDATE fixture SET value=1; SELECT ROW_COUNT()")
    batch = seen["command"][seen["command"].index("-e") + 1]
    assert result.returncode == 0
    assert batch.startswith("START TRANSACTION;")
    assert batch.endswith("COMMIT;")


def test_rathena_database_probe_verifies_schema_and_transaction_without_live_rows(monkeypatch):
    executed = []

    class Cursor:
        description = None
        result = None
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            executed.append(normalized)
            if normalized == "SELECT VERSION()":
                self.result = ("10.11.0", "MariaDB Server")
            elif normalized == "SELECT VERSION(), @@version_comment":
                self.result = ("10.11.0", "MariaDB Server")
            elif "FROM information_schema.tables" in normalized:
                self.result = [(name, "InnoDB") for name in params[1:]]
            elif "SELECT value FROM korelab_provider_probe" in normalized:
                self.result = (2,)
            elif "SELECT COUNT(*) FROM korelab_provider_probe" in normalized:
                self.result = (0,)
            elif normalized == "SHOW GRANTS":
                self.result = ("GRANT SELECT, INSERT, UPDATE, DELETE ON *.* TO test",)
            else:
                self.result = None
        def fetchone(self):
            value, self.result = self.result, None
            return value
        def fetchall(self):
            value = self.result or []
            self.result = None
            return value
        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()
        def rollback(self):
            executed.append("ROLLBACK")
        def close(self):
            executed.append("CLOSE")

    provider = database.PythonMySQLProvider("pymysql")
    monkeypatch.setattr(provider, "_connection", lambda **_: Connection())
    result = provider.verify_rathena()
    assert result["status"] == "VERIFIED"
    assert result["server_vendor"] == "MariaDB"
    assert "rathena_tables" in result
    assert "write_permissions" in result
    assert any("UPDATE `ragnarok`.`char`" in sql and "WHERE 1=0" in sql for sql in executed)
    assert any("DELETE FROM `ragnarok`.`inventory` WHERE 1=0" in sql for sql in executed)
    assert any("INSERT INTO `ragnarok`.`memo`" in sql and "WHERE 1=0" in sql for sql in executed)
    assert "ROLLBACK" in executed and executed[-1] == "CLOSE"


def test_rathena_database_probe_rejects_nontransactional_character_schema(monkeypatch):
    class Cursor:
        def execute(self, sql, params=None):
            self.sql = sql
            self.params = params
        def fetchone(self):
            return ("8.0", "MySQL Community Server") if "VERSION" in self.sql else None
        def fetchall(self):
            return [(name, "MyISAM" if name == "char" else "InnoDB")
                    for name in self.params[1:]]
        def close(self):
            pass
    class Connection:
        def cursor(self):
            return Cursor()
        def rollback(self):
            pass
        def close(self):
            self.closed = True
    connection = Connection()
    provider = database.PythonMySQLProvider("pymysql")
    monkeypatch.setattr(provider, "_connection", lambda **_: connection)
    with pytest.raises(RuntimeError, match="not InnoDB"):
        provider.verify_rathena()
    assert connection.closed


def test_rathena_database_probe_rejects_non_mysql_protocol_identity(monkeypatch):
    class Cursor:
        def execute(self, sql, params=None):
            self.sql = sql
        def fetchone(self):
            return ("PostgreSQL 16", "PostgreSQL")
        def close(self):
            pass
    class Connection:
        def cursor(self):
            return Cursor()
        def close(self):
            self.closed = True
    connection = Connection()
    provider = database.PythonMySQLProvider("pymysql")
    monkeypatch.setattr(provider, "_connection", lambda **_: connection)
    with pytest.raises(RuntimeError, match="did not identify itself as MySQL or MariaDB"):
        provider.verify_rathena()
    assert connection.closed


def test_sqlite_knowledge_backend_preserves_history_and_server_scope(tmp_path):
    store = KnowledgeStore(tmp_path, "server-a", backend="sqlite")
    first = store.add(entity_type="item", entity_id="1", attribute="weight", value=10,
                      provenance="user_provided", evidence="fixture")
    second = store.add(entity_type="item", entity_id="1", attribute="weight", value=12,
                       provenance="user_provided", evidence="updated fixture")
    other = KnowledgeStore(tmp_path, "server-b", backend="sqlite")
    assert [row["fact_id"] for row in store.list_facts()] == [first["fact_id"], second["fact_id"]]
    assert store.list_facts()[0]["status"] == "conflicted"
    assert other.list_facts() == []
    assert store.path.name == "knowledge.sqlite3"
    assert store.path.stat().st_mode & 0o077 == 0


def test_sqlite_knowledge_write_failure_rolls_back_whole_history(tmp_path):
    store = KnowledgeStore(tmp_path, "server-a", backend="sqlite")
    original = store.add(entity_type="item", entity_id="1", attribute="weight", value=10,
                         provenance="user_provided", evidence="fixture")
    duplicate = dict(original)
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction():
            store._write([original, duplicate])
    assert store.list_facts() == [original]


def test_file_storage_remains_default(tmp_path):
    store = KnowledgeStore(tmp_path, "local")
    assert store.backend == "file"
    assert store.path.name == "facts.jsonl"


def test_only_implemented_local_storage_backends_are_accepted(tmp_path):
    with pytest.raises(ValueError, match="file or sqlite"):
        KnowledgeStore(tmp_path, "local", backend="postgresql")


def test_private_permission_helper_preserves_posix_mode_on_current_host(tmp_path):
    target = tmp_path / "private"
    target.write_text("local")
    set_private_permissions(target, 0o600)
    assert target.stat().st_mode & 0o777 == 0o600


def test_run_lock_rejects_duplicate_lock(tmp_path):
    target = tmp_path / "run.lock"
    first = target.open("a+b")
    try:
        try_lock(first)
        second = target.open("a+b")
        try:
            with pytest.raises(BlockingIOError):
                try_lock(second)
        finally:
            second.close()
    finally:
        first.close()


def test_process_identity_rejects_executable_outside_checkout(tmp_path):
    from korelab.processes import process_is_under
    root = tmp_path / "rathena"
    root.mkdir()
    binary = root / "map-server.exe"
    binary.touch()
    assert process_is_under(str(binary), root)
    assert not process_is_under(str(binary), tmp_path / "other")




def test_windows_listener_inspection_uses_powershell_owner_metadata(monkeypatch):
    from korelab import processes
    monkeypatch.setattr(processes.platform, "system", lambda: "Windows")
    monkeypatch.setattr(processes.shutil, "which", lambda _name: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    monkeypatch.setattr(processes.subprocess, "run", lambda *_args, **_kwargs:
        SimpleNamespace(stdout='{"port":6900,"path":"C:\\\\rathena\\\\login-server.exe"}', returncode=0))
    owners = processes.local_listener_processes(6900)
    assert owners == [{"port": "6900", "path": r"C:\rathena\login-server.exe"}]


def test_windows_openkore_pipe_output_and_input(monkeypatch):
    from korelab.adapters import openkore

    class Log:
        def __init__(self):
            self.value = ""
        def write(self, value):
            self.value += value
        def flush(self):
            pass

    adapter = object.__new__(openkore.OpenKoreAdapter)
    adapter.deadline = time.monotonic() + 5
    adapter._pipe_output = queue.Queue()
    adapter._pipe_output.put(b"native console output\n")
    adapter.output = ""
    adapter.log_pending = ""
    adapter.log = Log()
    adapter.sensitive_values = ()
    adapter.process = SimpleNamespace(stdin=io.BytesIO())
    monkeypatch.setattr(openkore.os, "name", "nt")
    adapter._read(0.1)
    adapter.command("status")
    assert adapter.output == "native console output\n"
    assert adapter.log.value == "native console output\n"
    assert adapter.process.stdin.getvalue() == b"status\n"
