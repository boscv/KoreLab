"""Client for OpenKore's native Interface::Socket Bus protocol.

This adapter attaches to an already-running process. It never launches or stops
OpenKore; closing the KoreLab session only closes this Unix-socket connection.
"""
from __future__ import annotations

import json
import pathlib
import re
import select
import socket
import stat
import struct
import time
from typing import Callable

from .openkore import position, redact


MAX_FRAME = 1_000_000


def _frame(message_id: str, values: dict[str, str] | None = None) -> bytes:
    mid = message_id.encode("utf-8")
    if len(mid) > 255:
        raise ValueError("OpenKore socket message ID is too long")
    payload = bytearray((0, len(mid)))
    payload.extend(mid)
    for key, value in (values or {}).items():
        key_bytes, value_bytes = key.encode("utf-8"), str(value).encode("utf-8")
        if len(key_bytes) > 255 or len(value_bytes) > 0xFFFFFF:
            raise ValueError("OpenKore socket message field is too large")
        payload.append(len(key_bytes))
        payload.extend(key_bytes)
        # Native Interface::Console input reaches Bus::Messages as bytes on
        # this Perl build (value type 0), matching attach-to-console.pl.
        payload.append(0)
        payload.extend(len(value_bytes).to_bytes(3, "big"))
        payload.extend(value_bytes)
    body = bytes(payload)
    return struct.pack(">I", len(body) + 4) + body


def _decode_frame(frame: bytes) -> tuple[str, dict[str, str]]:
    if len(frame) < 6 or struct.unpack(">I", frame[:4])[0] != len(frame):
        raise ValueError("invalid OpenKore socket frame")
    options, mid_length = frame[4], frame[5]
    if options != 0:
        raise ValueError("unsupported OpenKore socket array message")
    offset = 6
    end_id = offset + mid_length
    if end_id > len(frame):
        raise ValueError("truncated OpenKore socket message ID")
    message_id = frame[offset:end_id].decode("utf-8")
    offset = end_id
    values: dict[str, str] = {}
    while offset < len(frame):
        key_length = frame[offset]
        offset += 1
        end_key = offset + key_length
        if end_key + 4 > len(frame):
            raise ValueError("truncated OpenKore socket message field")
        key = frame[offset:end_key].decode("utf-8")
        offset = end_key
        kind = frame[offset]
        length = int.from_bytes(frame[offset + 1:offset + 4], "big")
        offset += 4
        end_value = offset + length
        if end_value > len(frame):
            raise ValueError("truncated OpenKore socket message value")
        raw = frame[offset:end_value]
        offset = end_value
        if kind == 1:
            value = raw.decode("utf-8", "replace")
        elif kind == 2 and length == 4:
            value = str(struct.unpack(">I", raw)[0])
        elif kind == 0:
            value = raw.decode("utf-8", "replace")
        else:
            raise ValueError(f"unsupported OpenKore socket value type {kind}")
        values[key] = value
    return message_id, values


class OpenKoreSocketAdapter:
    """Bounded agent adapter for an existing native OpenKore socket session."""

    def __init__(self, socket_path: pathlib.Path, observer_file: pathlib.Path, deadline: float):
        self.socket_path = socket_path.expanduser().resolve()
        self.observer_file = observer_file.expanduser().resolve()
        self.deadline = deadline
        self.process = None
        self.output = ""
        self.sensitive_values: tuple[str, ...] = ()
        self.socket: socket.socket | None = None
        self.buffer = bytearray()
        self.runtime_profile_report = {"mode": "attached_native_socket", "temporary_workspace": False}
        self.temp_path = pathlib.Path("/nonexistent/korelab-attached-session-workspace")
        self.scenario = {"identity": {"master": "attached", "username": "", "character": "", "character_slot": 0}}
        self.eventmacro_control_dir = None
        self._last_observer_count = 0

    def start(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("this Python/Windows build does not support AF_UNIX; use --agent-session with a launched local OpenKore process")
        try:
            socket_info = self.socket_path.lstat()
        except OSError as exc:
            raise FileNotFoundError("OpenKore native console socket was not found") from exc
        if not stat.S_ISSOCK(socket_info.st_mode):
            raise ValueError("--attach-console must point to an existing Unix socket")
        if self.observer_file.exists():
            observer_info = self.observer_file.lstat()
            if not stat.S_ISREG(observer_info.st_mode):
                raise ValueError("--observer-file must point to a regular JSONL file")
        elif not self.observer_file.parent.is_dir():
            raise FileNotFoundError("structured OpenKore observer directory was not found")
        self._observer_rows()  # Validate readability and existing JSON records before attaching.
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(5)
        try:
            self.socket.connect(str(self.socket_path))
            self.socket.settimeout(None)
            self.socket.sendall(_frame("set active"))
            self._read(0.2)
        except OSError as exc:
            self.close()
            raise RuntimeError("could not attach to the existing OpenKore process") from exc

    def _read(self, timeout: float = 0.2) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("attached OpenKore session exceeded its declared lifetime")
        if self.socket is None:
            raise RuntimeError("OpenKore socket session is closed")
        ready, _, _ = select.select([self.socket], [], [], max(0, timeout))
        if not ready:
            return
        chunk = self.socket.recv(65536)
        if not chunk:
            raise RuntimeError("the attached OpenKore process closed its native console socket")
        self.buffer.extend(chunk)
        while len(self.buffer) >= 4:
            size = struct.unpack(">I", self.buffer[:4])[0]
            if size < 6 or size > MAX_FRAME:
                raise RuntimeError("OpenKore socket sent an invalid frame length")
            if len(self.buffer) < size:
                break
            frame = bytes(self.buffer[:size])
            del self.buffer[:size]
            message_id, fields = _decode_frame(frame)
            if message_id == "output":
                self.output += fields.get("message", "")

    def command(self, value: str) -> None:
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("OpenKore socket command must be one non-empty line")
        if self.socket is None:
            raise RuntimeError("OpenKore socket session is closed")
        self.socket.sendall(_frame("input", {"data": value}))

    def capture_command(self, value: str, *, timeout: float = 5, quiet_period: float = 0.35) -> str:
        cursor = len(self.output)
        self.command(value)
        deadline = min(self.deadline, time.monotonic() + timeout)
        last_change, seen = time.monotonic(), ""
        while time.monotonic() < deadline:
            self._read(min(0.1, deadline - time.monotonic()))
            current = self.output[cursor:]
            if current != seen:
                seen, last_change = current, time.monotonic()
            elif current and time.monotonic() - last_change >= quiet_period:
                break
        return redact(self.output[cursor:], self.sensitive_values)

    def _wait_after(self, pattern: str, cursor: int, timeout: float = 30) -> re.Match:
        end = min(self.deadline, time.monotonic() + timeout)
        compiled = re.compile(pattern, re.I | re.S)
        while time.monotonic() < end:
            match = compiled.search(self.output[cursor:])
            if match:
                return match
            self._read(min(0.1, end - time.monotonic()))
        raise TimeoutError("expected OpenKore socket output was not observed")

    def _observer_rows(self) -> list[dict]:
        try:
            lines = self.observer_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []  # The plugin may create its first JSONL sample after map entry.
        except OSError as exc:
            raise RuntimeError("structured OpenKore observer became unavailable") from exc
        rows = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue  # Ignore the writer's current incomplete tail.
            if isinstance(value, dict):
                rows.append(value)
        self._last_observer_count = len(rows)
        return rows

    def state_after(self, sample_count: int, predicate: Callable[[dict], bool], timeout: float = 15) -> dict:
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            rows = self._observer_rows()
            for row in rows[sample_count:]:
                if predicate(row):
                    return row
            self._read(min(0.1, end - time.monotonic()))
        raise TimeoutError("attached OpenKore observer did not report the requested state")

    def state(self, predicate: Callable[[dict], bool], timeout: float = 15) -> dict:
        rows = self._observer_rows()
        return self.state_after(len(rows), predicate, timeout)

    def move(self, map_name: str, target: dict[str, int], timeout: float) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_]+", map_name):
            raise ValueError("map name contains unsupported characters")
        before = self._observer_rows()
        self.command(f"move {target['x']} {target['y']}")
        return self.state_after(len(before), lambda row: row.get("connected_in_game") is True
            and row.get("map") == map_name and position(row) == target
            and row.get("movement_state") == "stationary", timeout)

    def observe_npc_dialogue(self, name: str, timeout: float = 15) -> dict:
        rows = self._observer_rows()
        state = next((row for row in reversed(rows) if row.get("connected_in_game") is True
            and any(str(npc.get("name", "")).casefold() == name.casefold()
                    and int(npc.get("name_id", 0)) > 0 for npc in row.get("nearby_npcs", []))), None)
        if state is None:
            raise TimeoutError("requested NPC is not present in the attached session observations")
        npc = next(npc for npc in state["nearby_npcs"]
            if str(npc.get("name", "")).casefold() == name.casefold() and int(npc.get("name_id", 0)) > 0)
        sample_count = len(rows)
        self.command(f"talknpc ID_{int(npc['name_id'])} x")
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            dialogues = [row for row in self._observer_rows()[sample_count:]
                if row.get("type") == "dialogue" and str(row.get("name", "")).casefold() == name.casefold()]
            if dialogues:
                return {"status": "OBSERVED", "npc": name, "position": npc.get("position"),
                        "responses": dialogues[-1].get("responses", []),
                        "completion": "native dialogue opened and observed; no response selected"}
            self._read(0.1)
        raise TimeoutError("attached OpenKore did not expose the NPC dialogue choices")

    def archive_observer(self) -> None:
        # The observer remains owned by the OpenKore process; do not copy all of
        # its potentially long-lived history into each attachment report.
        return None

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None
