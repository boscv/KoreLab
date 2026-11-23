from __future__ import annotations

import pathlib
import socket
import tempfile
import threading
import time
import unittest

from korelab.adapters.openkore_socket import OpenKoreSocketAdapter, _decode_frame, _frame


def recv_frame(conn: socket.socket) -> tuple[str, dict[str, str]]:
    header = conn.recv(4)
    while len(header) < 4:
        header += conn.recv(4 - len(header))
    size = int.from_bytes(header, "big")
    body = bytearray()
    while len(body) < size - 4:
        body.extend(conn.recv(size - 4 - len(body)))
    return _decode_frame(header + body)


class OpenKoreSocketAdapterTests(unittest.TestCase):
    def test_native_bus_frame_round_trip(self):
        frame = _frame("input", {"data": "where"})
        self.assertEqual(_decode_frame(frame), ("input", {"data": "where"}))
        self.assertEqual(frame.hex(), "000000190005696e7075740464617461000000057768657265")

    def test_attach_uses_native_socket_input_and_disconnects_without_stopping_process(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = pathlib.Path(temp_name)
            socket_path = temp / "console.socket"
            observer = temp / "observer.jsonl"
            observer.write_text('{"type":"state","connected_in_game":true}\n', encoding="utf-8")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(1)
            received: list[tuple[str, dict[str, str]]] = []

            def serve():
                conn, _ = server.accept()
                with conn:
                    received.append(recv_frame(conn))
                    conn.sendall(_frame("output", {"type": "message", "message": "OpenKore ready\n", "domain": "console"}))
                    received.append(recv_frame(conn))
                    conn.sendall(_frame("output", {"type": "message", "message": "Location: test\n", "domain": "console"}))
                    conn.recv(1)

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            adapter = OpenKoreSocketAdapter(socket_path, observer, time.monotonic() + 3)
            try:
                adapter.start()
                response = adapter.capture_command("where", timeout=0.5, quiet_period=0.05)
                self.assertIn("Location: test", response)
            finally:
                adapter.close()
                worker.join(timeout=2)
                server.close()
            self.assertEqual(received[0], ("set active", {}))
            self.assertEqual(received[1], ("input", {"data": "where"}))

    def test_attach_rejects_non_socket_and_missing_structured_observer(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = pathlib.Path(temp_name)
            regular = temp / "not-a-socket"
            regular.write_text("x", encoding="utf-8")
            observer = temp / "observer.jsonl"
            observer.write_text("{}\n", encoding="utf-8")
            adapter = OpenKoreSocketAdapter(regular, observer, time.monotonic() + 2)
            with self.assertRaisesRegex(ValueError, "Unix socket"):
                adapter.start()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            socket_path = temp / "console.socket"
            listener.bind(str(socket_path))
            listener.listen(1)
            missing_observer = OpenKoreSocketAdapter(socket_path, temp / "missing" / "observer.jsonl", time.monotonic() + 2)
            with self.assertRaisesRegex(FileNotFoundError, "observer directory"):
                missing_observer.start()
            listener.close()


if __name__ == "__main__":
    unittest.main()
