#!/usr/bin/env python3
"""Probe an owned disposable rAthena char-server's pre-auth packet guard.

The probe sends one minimal CH_SELECT_CHAR header before login. A patched
server closes that client connection and continues serving; the unpatched
source at the pinned revision dereferences a null character session and exits.
This script intentionally does not start or stop services. Pass only an
owner-verified disposable laboratory endpoint.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time


def probe(host: str, port: int, pid: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout) as client:
        client.settimeout(timeout)
        client.sendall(b"\x66\x00\x00")  # CH_SELECT_CHAR, slot 0; no login packet
        response = client.recv(64)
        if response:
            raise RuntimeError("char-server replied to an unauthenticated character-select packet")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError as exc:
            raise RuntimeError("char-server exited after the unauthenticated packet") from exc
        try:
            with socket.create_connection((host, port), timeout=0.5):
                print("PASS: rejected pre-auth packet; char-server remained available")
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("char-server process exists but its listener did not recover")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    try:
        probe(args.host, args.port, args.pid, args.timeout)
    except (OSError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
