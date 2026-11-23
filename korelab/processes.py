"""Local listener ownership inspection without probing remote systems."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path


def local_listener_processes(port: int) -> list[dict[str, str]]:
    """Return local listener executable paths where the OS can identify them."""
    if platform.system() == "Windows":
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            return []
        script = ("$rows = Get-NetTCPConnection -State Listen -LocalPort " + str(int(port)) +
                  " -ErrorAction SilentlyContinue | ForEach-Object { "
                  "$p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.OwningProcess); "
                  "[PSCustomObject]@{port=$_.LocalPort; path=$p.ExecutablePath} }; "
                  "$rows | ConvertTo-Json -Compress")
        try:
            result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
                                    capture_output=True, text=True, timeout=6, check=False)
            values = json.loads(result.stdout) if result.stdout.strip() else []
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return []
        if isinstance(values, dict):
            values = [values]
        return [{"path": str(row.get("path") or ""), "port": str(row.get("port", port))}
                for row in values if isinstance(row, dict) and row.get("path")]
    if platform.system() == "Darwin":
        lsof = shutil.which("lsof")
        if not lsof:
            return []
        try:
            pids = subprocess.run([lsof, "-nP", "-t", f"-iTCP:{int(port)}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=4, check=False).stdout.split()
            found = []
            for pid in pids:
                output = subprocess.run([lsof, "-a", "-p", pid, "-d", "txt", "-Fn"],
                    capture_output=True, text=True, timeout=4, check=False).stdout
                path = next((line[1:] for line in output.splitlines()
                             if line.startswith("n") and line[1:].startswith("/")), "")
                if path:
                    found.append({"path": path, "port": str(port)})
            return found
        except (OSError, subprocess.SubprocessError):
            return []
    if platform.system() == "Linux":
        ss = shutil.which("ss")
        if not ss:
            return []
        try:
            output = subprocess.run([ss, "-lptn", f"sport = :{int(port)}"],
                capture_output=True, text=True, timeout=4, check=False).stdout
            pids = set(re.findall(r"pid=(\d+)", output))
            found = []
            for pid in pids:
                executable = Path("/proc") / pid / "exe"
                if executable.exists():
                    found.append({"path": str(executable.resolve()), "port": str(port)})
            return found
        except (OSError, subprocess.SubprocessError):
            return []
    return []


def process_is_under(path: str, root: Path) -> bool:
    if not path:
        return False
    try:
        Path(path).resolve().relative_to(root.expanduser().resolve())
        return True
    except (OSError, ValueError):
        return False
