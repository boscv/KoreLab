"""Small process sampler for comparable local OpenKore acceptance runs."""
from __future__ import annotations

import statistics
import subprocess
import threading
import time
from datetime import datetime
import json
from pathlib import Path


def _cpu_seconds(value: str) -> float:
    """Parse macOS ps cputime values such as 1:02.34 or 1-02:03:04."""
    value = value.strip()
    days = 0
    if "-" in value:
        day_text, value = value.split("-", 1)
        days = int(day_text)
    parts = value.split(":")
    if len(parts) == 2:
        hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
    elif len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    else:
        return 0.0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


class ProcessSampler:
    """Sample CPU and resident memory for each OpenKore process in a run."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.pids: set[int] = set()
        self.samples: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add(self, pid: int) -> None:
        with self._lock:
            self.pids.add(int(pid))
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                pids = tuple(self.pids)
            for pid in pids:
                try:
                    result = subprocess.run(
                        ["ps", "-p", str(pid), "-o", "cputime=,rss=,%cpu="],
                        capture_output=True, text=True, check=False, timeout=1,
                    )
                    parts = result.stdout.split()
                    if result.returncode == 0 and len(parts) == 3:
                        self.samples.append({
                            "pid": pid,
                            "cpu_seconds": _cpu_seconds(parts[0]),
                            "rss_kb": int(parts[1]),
                            "cpu_percent": float(parts[2]),
                            "sampled_at_monotonic": time.monotonic(),
                        })
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    continue
            self._stop.wait(self.interval)

    def finish(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval * 2))
        by_pid: dict[int, list[dict]] = {}
        for sample in self.samples:
            by_pid.setdefault(sample["pid"], []).append(sample)
        cpu_seconds = 0.0
        process_summaries = []
        for pid, samples in sorted(by_pid.items()):
            final_cpu = max((s["cpu_seconds"] for s in samples), default=0.0)
            cpu_seconds += final_cpu
            process_summaries.append({
                "pid": pid,
                "samples": len(samples),
                "cpu_seconds": round(final_cpu, 2),
                "mean_cpu_percent": round(statistics.mean(s["cpu_percent"] for s in samples), 2),
                "peak_cpu_percent": round(max(s["cpu_percent"] for s in samples), 2),
                "median_rss_kb": int(statistics.median(s["rss_kb"] for s in samples)),
                "peak_rss_kb": max(s["rss_kb"] for s in samples),
            })
        all_cpu = [s["cpu_percent"] for s in self.samples]
        all_rss = [s["rss_kb"] for s in self.samples]
        return {
            "sampling_interval_seconds": self.interval,
            "openkore_process_count": len(by_pid),
            "sample_count": len(self.samples),
            "cumulative_openkore_cpu_seconds": round(cpu_seconds, 2),
            "mean_cpu_percent": round(statistics.mean(all_cpu), 2) if all_cpu else None,
            "peak_cpu_percent": round(max(all_cpu), 2) if all_cpu else None,
            "median_rss_kb": int(statistics.median(all_rss)) if all_rss else None,
            "peak_rss_kb": max(all_rss) if all_rss else None,
            "processes": process_summaries,
            "sampling_scope": "OpenKore Perl processes only; excludes runner, rAthena and MySQL",
        }


def workflow_timing(observer_paths: list[Path]) -> dict:
    """Estimate objective-stage durations from fresh structured observations."""
    rows = []
    for path in observer_paths:
        try:
            rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
        except (OSError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda row: row.get("time", ""))
    if not rows:
        return {"status": "UNAVAILABLE", "reason": "no structured observer samples"}

    def stamp(predicate):
        for row in rows:
            if predicate(row):
                try:
                    return datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    return None
        return None

    def seconds(start, end):
        return round((end - start).total_seconds(), 1) if start and end and end >= start else None

    in_game = stamp(lambda r: r.get("connected_in_game") is True)
    yuno = stamp(lambda r: r.get("map") == "yuno")
    storage_queue = stamp(lambda r: "storageAuto" in (r.get("native_ai_queue") or []))
    stored = stamp(lambda r: int((r.get("character_attributes") or {}).get("weight", -1)) == 0
                   and r.get("inventory_observed") is True)
    maze = stamp(lambda r: r.get("map") == "yuno_in05")
    valkyrie = stamp(lambda r: r.get("map") == "valkyrie")
    reborn = stamp(lambda r: int((r.get("character_attributes") or {}).get("class", -1)) == 4001)
    field = stamp(lambda r: r.get("map") == "prt_fild08"
                  and int((r.get("character_attributes") or {}).get("class", -1)) == 4001)
    exp = stamp(lambda r: int((r.get("character_attributes") or {}).get("base_exp", 0)) > 0
                and int((r.get("character_attributes") or {}).get("class", -1)) == 4001)
    return {
        "source": "structured observer state timestamps; one-second sampling and stage boundaries are approximate",
        "first_in_game_to_yuno_seconds": seconds(in_game, yuno),
        "storage_queue_to_zero_weight_seconds": seconds(storage_queue, stored),
        "yuno_to_maze_seconds": seconds(yuno, maze),
        "maze_to_valkyrie_seconds": seconds(maze, valkyrie),
        "valkyrie_to_high_novice_seconds": seconds(valkyrie, reborn),
        "high_novice_to_field_seconds": seconds(reborn, field),
        "field_to_first_experience_seconds": seconds(field, exp),
    }
