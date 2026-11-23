"""Small history-preserving, server-scoped knowledge records with explicit provenance."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import os
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any

from .platform_support import set_private_permissions


PROVENANCE = {"local_server_data", "user_provided", "openkore_observed", "agent_inferred"}
_SERVER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class KnowledgeError(ValueError):
    pass


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class KnowledgeStore:
    """History-preserving JSONL; disagreements remain visible instead of overwriting facts."""

    def __init__(self, root: pathlib.Path, server_id: str, *, backend: str | None = None):
        if not _SERVER_ID.fullmatch(server_id):
            raise KnowledgeError("server_id must be a simple path-safe identifier")
        backend = backend or os.environ.get("KORELAB_KNOWLEDGE_BACKEND", "file")
        if backend not in {"file", "sqlite"}:
            raise KnowledgeError("KORELAB_KNOWLEDGE_BACKEND must be file or sqlite; this build does not implement other engines")
        self.backend = backend
        self.server_id = server_id
        self.root = root.expanduser().resolve()
        self.path = (self.root / server_id / "facts.jsonl" if backend == "file"
                     else self.root / "knowledge.sqlite3")
        self._connection: sqlite3.Connection | None = None
        self._transaction_depth = 0

    def _connect_sqlite(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        set_private_permissions(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("CREATE TABLE IF NOT EXISTS knowledge_facts ("
                           "server_id TEXT NOT NULL, fact_id TEXT NOT NULL, payload TEXT NOT NULL, "
                           "PRIMARY KEY(server_id,fact_id))")
        return connection

    @contextmanager
    def _transaction(self):
        if self.backend != "sqlite":
            yield
            return
        if self._connection is not None:
            self._transaction_depth += 1
            try:
                yield
            finally:
                self._transaction_depth -= 1
            return
        self._connection = self._connect_sqlite()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._transaction_depth = 1
            yield
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        finally:
            self._transaction_depth = 0
            self._connection.close()
            self._connection = None
            if self.path.exists():
                set_private_permissions(self.path, 0o600)

    def list_facts(self, *, include_history: bool = True) -> list[dict[str, Any]]:
        if self.backend == "sqlite":
            connection = self._connection
            own_connection = connection is None
            if own_connection:
                if not self.path.exists():
                    return []
                connection = sqlite3.connect(self.path, timeout=5)
            try:
                try:
                    rows = connection.execute("SELECT payload FROM knowledge_facts WHERE server_id=? ORDER BY rowid",
                                              (self.server_id,)).fetchall()
                except sqlite3.OperationalError as exc:
                    if "no such table" in str(exc).lower():
                        rows = []
                    else:
                        raise
                facts = [json.loads(payload) for (payload,) in rows]
                return facts if include_history else [row for row in facts if row.get("status") != "superseded"]
            finally:
                if own_connection:
                    connection.close()
        if not self.path.exists():
            return []
        rows = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise KnowledgeError(f"invalid knowledge JSON on line {line_number}") from exc
            if row.get("server_id") != self.server_id:
                raise KnowledgeError(f"knowledge line {line_number} belongs to a different server")
            rows.append(row)
        return rows if include_history else [row for row in rows if row.get("status") != "superseded"]

    def add(self, *, entity_type: str, entity_id: str, attribute: str, value: Any,
            provenance: str, evidence: str, observed_at: str | None = None) -> dict[str, Any]:
        if provenance not in PROVENANCE:
            raise KnowledgeError(f"unsupported provenance: {provenance}")
        if not all(isinstance(v, str) and v.strip() for v in (entity_type, entity_id, attribute, evidence)):
            raise KnowledgeError("entity_type, entity_id, attribute, and evidence must be non-empty strings")
        if provenance == "openkore_observed" and not evidence.strip():
            raise KnowledgeError("OpenKore observations require an evidence reference")
        if self.backend == "sqlite" and self._connection is None:
            with self._transaction():
                return self._add(entity_type=entity_type, entity_id=entity_id,
                    attribute=attribute, value=value, provenance=provenance,
                    evidence=evidence, observed_at=observed_at)
        return self._add(entity_type=entity_type, entity_id=entity_id, attribute=attribute,
                         value=value, provenance=provenance, evidence=evidence, observed_at=observed_at)

    def _add(self, *, entity_type: str, entity_id: str, attribute: str, value: Any,
             provenance: str, evidence: str, observed_at: str | None) -> dict[str, Any]:
        history = self.list_facts()
        matching = [row for row in history if (row["entity_type"], row["entity_id"], row["attribute"])
                    == (entity_type, entity_id, attribute) and row["status"] in ("active", "conflicted")]
        same = [row for row in matching if row["value"] == value]
        different = [row for row in matching if row["value"] != value]
        status = "conflicted" if different else "active"
        fact = {"schema_version": 1, "fact_id": str(uuid.uuid4()), "server_id": self.server_id,
                "entity_type": entity_type, "entity_id": entity_id, "attribute": attribute,
                "value": value, "provenance": provenance, "evidence": evidence,
                "recorded_at": _utc_now(), "observed_at": observed_at or _utc_now(), "status": status,
                "conflicts_with": [row["fact_id"] for row in different],
                "supersedes": [row["fact_id"] for row in same]}
        if different:
            for row in different:
                row["status"] = "conflicted"
                row.setdefault("conflicts_with", [])
                row["conflicts_with"] = sorted(set(row["conflicts_with"] + [fact["fact_id"]]))
        elif same:
            for row in same:
                row["status"] = "superseded"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write(history + [fact])
        return fact

    def mark_stale(self, fact_id: str, *, reason: str) -> dict[str, Any]:
        if self.backend == "sqlite" and self._connection is None:
            with self._transaction():
                return self.mark_stale(fact_id, reason=reason)
        rows = self.list_facts()
        for row in rows:
            if row["fact_id"] == fact_id:
                row["status"] = "stale"
                row["stale_reason"] = reason
                row["stale_at"] = _utc_now()
                self._write(rows)
                return row
        raise KnowledgeError(f"fact not found: {fact_id}")

    def export_document(self, destination: pathlib.Path) -> dict[str, Any]:
        """Export this server's complete provenance history as an explicit portable file."""
        destination = destination.expanduser().resolve()
        if destination.exists():
            raise FileExistsError("knowledge export destination already exists")
        facts = self.list_facts()
        document = {"schema_version": 1, "server_id": self.server_id,
                    "fact_count": len(facts), "facts": facts}
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        set_private_permissions(temporary, 0o600)
        temporary.replace(destination)
        return {"status": "EXPORTED", "server_id": self.server_id,
                "fact_count": len(facts), "path": str(destination)}

    def import_document(self, source: pathlib.Path) -> dict[str, Any]:
        """Import matching-server facts append-only, retaining source IDs and conflicts."""
        try:
            document = json.loads(source.expanduser().resolve().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeError(f"cannot read knowledge export: {exc}") from exc
        if (not isinstance(document, dict) or document.get("schema_version") != 1 or
                document.get("server_id") != self.server_id or not isinstance(document.get("facts"), list)):
            raise KnowledgeError("knowledge export schema or server_id does not match this knowledge store")
        incoming = document["facts"]
        required = ("entity_type", "entity_id", "attribute", "value", "provenance", "evidence")
        for index, row in enumerate(incoming):
            if not isinstance(row, dict) or row.get("server_id") != self.server_id:
                raise KnowledgeError(f"knowledge export fact {index} has an invalid server scope")
            if not all(key in row for key in required):
                raise KnowledgeError(f"knowledge export fact {index} is missing required fields")
            if row.get("provenance") not in PROVENANCE:
                raise KnowledgeError(f"knowledge export fact {index} has unsupported provenance")
            if not all(isinstance(row.get(key), str) and row[key].strip()
                       for key in ("entity_type", "entity_id", "attribute", "evidence")):
                raise KnowledgeError(f"knowledge export fact {index} has invalid text fields")
        if self.backend == "sqlite" and self._connection is None:
            with self._transaction():
                return self.import_document(source)
        existing = self.list_facts()
        imported_keys = {(row.get("imported_from", {}).get("server_id"),
                          row.get("imported_from", {}).get("fact_id")) for row in existing}
        imported = 0
        for row in incoming:
            original_id = row.get("fact_id")
            origin = (self.server_id, original_id)
            if not isinstance(original_id, str) or origin in imported_keys:
                continue
            fact = self.add(entity_type=row["entity_type"], entity_id=row["entity_id"],
                attribute=row["attribute"], value=row["value"], provenance=row["provenance"],
                evidence=row["evidence"], observed_at=row.get("observed_at"))
            history = self.list_facts()
            fact = next(item for item in history if item["fact_id"] == fact["fact_id"])
            fact["imported_from"] = {"server_id": self.server_id, "fact_id": original_id,
                                      "recorded_at": row.get("recorded_at")}
            self._write(history)
            imported_keys.add(origin)
            imported += 1
        return {"status": "IMPORTED", "server_id": self.server_id,
                "fact_count": imported, "conflicts_preserved": sum(
                    row.get("status") == "conflicted" for row in self.list_facts())}

    def _write(self, rows: list[dict[str, Any]]) -> None:
        if self.backend == "sqlite":
            connection = self._connection
            own_connection = connection is None
            if own_connection:
                with self._transaction():
                    self._write(rows)
                return
            connection.execute("DELETE FROM knowledge_facts WHERE server_id=?", (self.server_id,))
            connection.executemany("INSERT INTO knowledge_facts(server_id,fact_id,payload) VALUES(?,?,?)",
                [(self.server_id, row["fact_id"], json.dumps(row, sort_keys=True, ensure_ascii=False))
                 for row in rows])
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        set_private_permissions(self.path.parent, 0o700)
        temporary = self.path.with_suffix(".jsonl.tmp")
        temporary.write_text("".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows),
                             encoding="utf-8")
        set_private_permissions(temporary, 0o600)
        temporary.replace(self.path)
