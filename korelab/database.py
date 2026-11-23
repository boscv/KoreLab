"""Small MySQL/MariaDB protocol boundary used only for rAthena integration."""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class QueryResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class MySQLProvider:
    id = "mysql-protocol"

    def execute(self, sql: str, *, timeout: int = 10) -> QueryResult:
        raise NotImplementedError

    def verify_rathena(self) -> dict:
        raise NotImplementedError


class PythonMySQLProvider(MySQLProvider):
    def __init__(self, driver: str):
        self.driver = driver
        self.id = driver

    def _connection(self, *, timeout: int = 10):
        host = os.environ.get("KORELAB_DB_HOST", "127.0.0.1")
        port = int(os.environ.get("KORELAB_DB_PORT", "3306"))
        user = os.environ.get("KORELAB_DB_USER", "ragnarok")
        password = os.environ.get("KORELAB_DB_PASSWORD", "")
        database = os.environ.get("KORELAB_CHAR_DB", "ragnarok")
        if self.driver == "pymysql":
            module = importlib.import_module("pymysql")
            return module.connect(host=host, port=port, user=user, password=password,
                                  database=database, connect_timeout=timeout,
                                  read_timeout=timeout, write_timeout=timeout,
                                  charset="utf8mb4", autocommit=False)
        if self.driver == "mysql-connector-python":
            module = importlib.import_module("mysql.connector")
            return module.connect(host=host, port=port, user=user, password=password,
                                  database=database, connection_timeout=timeout,
                                  autocommit=False, charset="utf8mb4")
        if self.driver == "mariadb-python":
            module = importlib.import_module("mariadb")
            return module.connect(host=host, port=port, user=user, password=password,
                                  database=database, connect_timeout=timeout,
                                  autocommit=False)
        raise ValueError(f"unsupported MySQL protocol driver: {self.driver}")

    def _placeholder(self) -> str:
        return "?" if self.driver == "mariadb-python" else "%s"

    def execute(self, sql: str, *, timeout: int = 10) -> QueryResult:
        connection = None
        try:
            connection = self._connection(timeout=timeout)
            output = []
            # rAthenaAdapter builds simple semicolon-separated statements; none
            # contain embedded semicolons or user-supplied SQL fragments.
            statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
            cursor = connection.cursor()
            try:
                for statement in statements:
                    cursor.execute(statement)
                    if cursor.description:
                        output.extend("\t".join("\\N" if value is None else str(value)
                                                for value in row)
                                      for row in cursor.fetchall())
            finally:
                cursor.close()
            connection.commit()
            return QueryResult(0, "\n".join(output) + ("\n" if output else ""))
        except Exception as exc:
            if connection:
                try:
                    connection.rollback()
                except Exception:
                    pass
            return QueryResult(1, "", f"{type(exc).__name__}: {exc}")
        finally:
            if connection:
                connection.close()

    def verify_rathena(self) -> dict:
        char_db = _identifier(os.environ.get("KORELAB_CHAR_DB", "ragnarok"))
        login_db = _identifier(os.environ.get("KORELAB_LOGIN_DB", "ragnarok"))
        connection = self._connection(timeout=5)
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT VERSION(), @@version_comment")
            server_version, version_comment = cursor.fetchone()
            server_identity = f"{server_version} {version_comment}".casefold()
            if "mariadb" in server_identity:
                server_vendor = "MariaDB"
            elif "mysql" in server_identity:
                server_vendor = "MySQL"
            else:
                raise RuntimeError("configured endpoint did not identify itself as MySQL or MariaDB")
            required = {char_db: {"char", "inventory", "skill", "storage", "quest", "memo"}}
            required.setdefault(login_db, set()).add("login")
            engines = {}
            for schema, names in required.items():
                marker = self._placeholder()
                placeholders = ",".join(marker for _ in names)
                cursor.execute("SELECT table_name, engine FROM information_schema.tables "
                    f"WHERE table_schema={marker} AND table_name IN ({placeholders})",
                    (schema, *sorted(names)))
                engines[schema] = {str(name).casefold(): engine for name, engine in cursor.fetchall()}
                missing = names - engines[schema].keys()
                if missing:
                    raise RuntimeError(f"configured rAthena schema {schema!r} lacks required tables: {', '.join(sorted(missing))}")
                nontransactional = [name for name, engine in engines[schema].items()
                                    if str(engine or "").casefold() != "innodb"]
                if nontransactional:
                    raise RuntimeError(f"rAthena tables {', '.join(sorted(nontransactional))} are not InnoDB; guarded restoration cannot be verified safely")
            cursor.execute(f"SELECT char_id FROM `{char_db}`.`char` LIMIT 0")
            cursor.execute(f"SELECT account_id FROM `{login_db}`.`login` LIMIT 0")
            # A private temporary InnoDB table verifies read/write transaction
            # behavior without modifying rAthena-owned data.
            cursor.execute("CREATE TEMPORARY TABLE korelab_provider_probe "
                           "(id INT PRIMARY KEY, value INT NOT NULL) ENGINE=InnoDB")
            cursor.execute("START TRANSACTION")
            cursor.execute("INSERT INTO korelab_provider_probe VALUES (1, 1)")
            cursor.execute("UPDATE korelab_provider_probe SET value=2 WHERE id=1")
            cursor.execute("SELECT value FROM korelab_provider_probe WHERE id=1")
            if cursor.fetchone()[0] != 2:
                raise RuntimeError("database transaction read-after-write verification failed")
            connection.rollback()
            cursor.execute("SELECT COUNT(*) FROM korelab_provider_probe")
            rolled_back = int(cursor.fetchone()[0]) == 0
            cursor.execute("DROP TEMPORARY TABLE korelab_provider_probe")
            if not rolled_back:
                raise RuntimeError("database rollback did not remove uncommitted probe data")
            zero_row_probes = (
                f"UPDATE `{char_db}`.`char` SET char_id=char_id WHERE 1=0",
                f"UPDATE `{login_db}`.`login` SET group_id=group_id WHERE 1=0",
                f"DELETE FROM `{char_db}`.`inventory` WHERE 1=0",
                f"INSERT INTO `{char_db}`.`inventory` (char_id,nameid,amount,identify) SELECT 0,0,0,0 WHERE 1=0",
                f"DELETE FROM `{char_db}`.`skill` WHERE 1=0",
                f"INSERT INTO `{char_db}`.`skill` (char_id,id,lv,flag) SELECT 0,0,0,0 WHERE 1=0",
                f"DELETE FROM `{char_db}`.`storage` WHERE 1=0",
                f"INSERT INTO `{char_db}`.`storage` (id,account_id,nameid,amount) SELECT 0,0,0,0 WHERE 1=0",
                f"DELETE FROM `{char_db}`.`quest` WHERE 1=0",
                f"INSERT INTO `{char_db}`.`quest` (char_id,quest_id,state,time,count1,count2,count3) SELECT 0,0,0,0,0,0,0 WHERE 1=0",
                f"DELETE FROM `{char_db}`.`memo` WHERE 1=0",
                f"INSERT INTO `{char_db}`.`memo` (char_id,`map`,x,y) SELECT 0,'',0,0 WHERE 1=0",
            )
            cursor.execute("START TRANSACTION")
            for statement in zero_row_probes:
                cursor.execute(statement)
            connection.rollback()
            cursor.execute("SHOW GRANTS")
            grants = [str(row[0]) for row in cursor.fetchall()]
            return {"status": "VERIFIED", "provider": self.id,
                    "server_version": str(server_version), "server_vendor": server_vendor,
                    "authenticated": True,
                    "character_schema": char_db, "login_schema": login_db,
                    "rathena_tables": {schema: engines[schema] for schema in required},
                    "read_access": "verified on char and login schemas",
                    "write_permissions": "zero-row update/delete/insert authorization accepted on scenario-restored tables",
                    "transaction_behavior": "InnoDB temporary-table insert/update/rollback verified",
                    "grants_reported": bool(grants),
                    "warning": "Production character write privileges are reported by SHOW GRANTS but are not exercised on live rows."}
        finally:
            try:
                connection.close()
            except Exception:
                pass


class MySQLCLIProvider(MySQLProvider):
    def __init__(self, executable: str):
        self.executable = executable
        self.id = "mariadb-client" if PathName(executable).lower().endswith("mariadb") else "mysql-client"

    def execute(self, sql: str, *, timeout: int = 10) -> QueryResult:
        host = os.environ.get("KORELAB_DB_HOST", "127.0.0.1")
        port = os.environ.get("KORELAB_DB_PORT", "3306")
        user = os.environ.get("KORELAB_DB_USER", "ragnarok")
        command = [self.executable, "--protocol=tcp", "-h", host, "-P", port, "-u", user]
        environment = os.environ.copy()
        if "KORELAB_DB_PASSWORD" in environment:
            # MySQL clients honor MYSQL_PWD; it is kept out of argv and never
            # included in diagnostic output.
            environment["MYSQL_PWD"] = environment["KORELAB_DB_PASSWORD"]
        else:
            command.append("--skip-password")
        # The adapter groups guarded restoration statements into one batch.
        # Explicit transaction boundaries ensure a later statement failure
        # cannot leave earlier changes committed by the client's autocommit.
        batch = f"START TRANSACTION;\n{sql.rstrip().rstrip(';')};\nCOMMIT;"
        command.extend(["--batch", "--raw", "--skip-column-names", "-e", batch])
        try:
            result = subprocess.run(command, text=True, capture_output=True,
                                    timeout=timeout, check=False, env=environment)
            return QueryResult(result.returncode, result.stdout, result.stderr)
        except (OSError, subprocess.SubprocessError) as exc:
            return QueryResult(1, "", f"{type(exc).__name__}: {exc}")

    def verify_rathena(self) -> dict:
        raise RuntimeError("full schema/transaction verification requires a compatible Python MySQL driver")


def PathName(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _identifier(value: str) -> str:
    import re
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError("database schema must be an alphanumeric SQL identifier")
    return value


def choose_mysql_provider() -> MySQLProvider | None:
    import importlib.util

    choices: dict[str, Any] = {}
    for module_name, provider_id in (("pymysql", "pymysql"),
                                     ("mysql.connector", "mysql-connector-python"),
                                     ("mariadb", "mariadb-python")):
        try:
            if importlib.util.find_spec(module_name):
                choices[provider_id] = PythonMySQLProvider(provider_id)
        except (ImportError, ModuleNotFoundError, ValueError):
            continue
    for executable, provider_id in (("mariadb", "mariadb-client"), ("mysql", "mysql-client")):
        path = shutil.which(executable)
        if path:
            choices[provider_id] = MySQLCLIProvider(path)
    preferred = os.environ.get("KORELAB_DB_PROVIDER")
    if preferred:
        if preferred not in choices:
            raise RuntimeError(f"configured KORELAB_DB_PROVIDER {preferred!r} is unavailable; run `korelab doctor`")
        return choices[preferred]
    for provider_id in ("pymysql", "mysql-connector-python", "mariadb-python", "mariadb-client", "mysql-client"):
        if provider_id in choices:
            return choices[provider_id]
    return None
