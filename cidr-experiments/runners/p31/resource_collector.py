#!/usr/bin/env python3
"""Low-overhead process-tree, host, device, and store resource collector.

The fixed TSV files written here are the authoritative telemetry.  ``pidstat``
and ``iostat`` raw streams are retained as independent audit artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from resource_schema import (
    CONTAINER_IDENTITY_SCHEMA_VERSION,
    DISK_CATEGORIES,
    DISK_COLUMNS,
    IOSTAT_COLUMNS,
    RESOURCE_COLUMNS,
    RESOURCE_SCHEMA_VERSION,
)

_STOP = False
_DATE_LINE = re.compile(r"^\d{2}/\d{2}/\d{2}(?:\d{2})?\s+\d{1,2}:\d{2}:\d{2}(?:\s+[AP]M)?$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
PROCESS_IDENTITY_SCHEMA_VERSION = "cidr-process-identity-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def atomic_json(path: Path, value: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _signal_handler(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


@dataclass(frozen=True)
class ProcStat:
    pid: int
    ppid: int
    pgrp: int
    utime_ticks: int
    stime_ticks: int
    start_ticks: int
    state: str = "R"


@dataclass
class ProcCounters:
    stat: ProcStat
    rss_bytes: int
    rss_readable: bool
    pss_bytes: int
    pss_readable: bool
    io_readable: bool
    read_bytes: int
    write_bytes: int
    cancelled_write_bytes: int
    rchar: int
    wchar: int


@dataclass(frozen=True)
class DeviceCounters:
    reads: int
    sectors_read: int
    read_ms: int
    writes: int
    sectors_written: int
    write_ms: int
    io_ms: int


def parse_proc_stat_text(text: str) -> ProcStat:
    left = text.find("(")
    right = text.rfind(")")
    if left <= 0 or right <= left:
        raise ValueError("malformed /proc/<pid>/stat")
    pid = int(text[:left].strip())
    tail = text[right + 1 :].split()
    if len(tail) < 20:
        raise ValueError("short /proc/<pid>/stat")
    # tail[0] is field 3 (state), hence field N is tail[N-3].
    return ProcStat(
        pid=pid,
        ppid=int(tail[1]),
        pgrp=int(tail[2]),
        utime_ticks=int(tail[11]),
        stime_ticks=int(tail[12]),
        start_ticks=int(tail[19]),
        state=tail[0],
    )


def read_proc_stat(pid: int) -> ProcStat | None:
    try:
        return parse_proc_stat_text(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
        return None


def proc_stat_is_live(stat: ProcStat) -> bool:
    """Return whether /proc reports a process that can still own live resources."""

    return stat.state not in {"Z", "X", "x"}


def _read_kib_field(path: Path, key: str) -> int | None:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(key + ":"):
                fields = line.split()
                return int(fields[1]) * 1024
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
        return None
    return None


def read_proc_io(pid: int) -> tuple[bool, dict[str, int]]:
    values = {
        "read_bytes": 0,
        "write_bytes": 0,
        "cancelled_write_bytes": 0,
        "rchar": 0,
        "wchar": 0,
    }
    try:
        for line in Path(f"/proc/{pid}/io").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            if key in values:
                values[key] = int(raw.strip())
        return True, values
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
        return False, values


def read_proc_counters(pid: int) -> ProcCounters | None:
    stat = read_proc_stat(pid)
    if stat is None or not proc_stat_is_live(stat):
        return None
    rss_value = _read_kib_field(Path(f"/proc/{pid}/status"), "VmRSS")
    pss_value = _read_kib_field(Path(f"/proc/{pid}/smaps_rollup"), "Pss")
    io_ok, io = read_proc_io(pid)
    if rss_value is None or pss_value is None or not io_ok:
        confirmed = read_proc_stat(pid)
        if (
            confirmed is None
            or confirmed.start_ticks != stat.start_ticks
            or not proc_stat_is_live(confirmed)
        ):
            return None
    return ProcCounters(
        stat=stat,
        rss_bytes=rss_value or 0,
        rss_readable=rss_value is not None,
        pss_bytes=pss_value or 0,
        pss_readable=pss_value is not None,
        io_readable=io_ok,
        read_bytes=io["read_bytes"],
        write_bytes=io["write_bytes"],
        cancelled_write_bytes=io["cancelled_write_bytes"],
        rchar=io["rchar"],
        wchar=io["wchar"],
    )


def read_children(pid: int) -> list[int]:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
        return [int(value) for value in raw.split()]
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
        return []


def process_group_members(pgid: int) -> set[int]:
    members: set[int] = set()
    if pgid <= 0:
        return members
    try:
        proc_entries = os.scandir("/proc")
    except OSError:
        return members
    with proc_entries:
        for entry in proc_entries:
            if not entry.name.isdigit():
                continue
            stat = read_proc_stat(int(entry.name))
            if stat is not None and stat.pgrp == pgid:
                members.add(stat.pid)
    return members


def descendants(roots: Iterable[int]) -> set[int]:
    found: set[int] = set()
    pending = [pid for pid in roots if pid > 0]
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        if read_proc_stat(pid) is None:
            continue
        found.add(pid)
        pending.extend(read_children(pid))
    return found


def resolve_container_identity(name: str) -> dict[str, object] | None:
    """Return one strict identity snapshot for a running Docker container."""

    if shutil.which("docker") is None:
        return None
    try:
        output = subprocess.check_output(
            ["docker", "inspect", "--type", "container", name],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        values = json.loads(output)
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            return None
        value = values[0]
        state = value.get("State")
        container_id = value.get("Id")
        pid = state.get("Pid") if isinstance(state, dict) else None
        started_at = state.get("StartedAt") if isinstance(state, dict) else None
        running = state.get("Running") if isinstance(state, dict) else None
        restart_count = value.get("RestartCount")
        if (
            not isinstance(container_id, str)
            or _CONTAINER_ID.fullmatch(container_id) is None
            or type(pid) is not int
            or pid <= 0
            or not isinstance(started_at, str)
            or not started_at
            or running is not True
            or type(restart_count) is not int
            or restart_count < 0
        ):
            return None
        proc_stat = read_proc_stat(pid)
        if proc_stat is None or proc_stat.pid != pid or proc_stat.start_ticks <= 0:
            return None
        return {
            "container_id": container_id,
            "pid": pid,
            "process_start_ticks": proc_stat.start_ticks,
            "started_at": started_at,
            "restart_count": restart_count,
        }
    except (json.JSONDecodeError, subprocess.SubprocessError, ValueError, OSError):
        return None


def resolve_container_pid(name: str) -> int | None:
    """Compatibility helper; the collector itself records full identities."""

    identity = resolve_container_identity(name)
    return int(identity["pid"]) if identity is not None else None


def identity_key(identity: dict[str, object]) -> tuple[str, int, int, str, int]:
    return (
        str(identity["container_id"]),
        int(identity["pid"]),
        int(identity["process_start_ticks"]),
        str(identity["started_at"]),
        int(identity["restart_count"]),
    )


def record_container_identity(
    name: str,
    identity: dict[str, object],
    sample_index: int,
    history: dict[str, list[dict[str, object]]],
    unique: dict[str, list[dict[str, object]]],
) -> None:
    """Record every resolution and a deterministic first-seen unique set."""

    snapshot = {
        "container_id": str(identity["container_id"]),
        "pid": int(identity["pid"]),
        "process_start_ticks": int(identity["process_start_ticks"]),
        "started_at": str(identity["started_at"]),
        "restart_count": int(identity["restart_count"]),
    }
    history.setdefault(name, []).append(
        {
            **snapshot,
            "observed_at_utc": utc_now(),
            "before_sample_index": sample_index,
        }
    )
    identities = unique.setdefault(name, [])
    if identity_key(snapshot) not in {identity_key(value) for value in identities}:
        identities.append(snapshot)


class ProcessAccumulator:
    """Aggregate deltas without charging pre-run work from attached services.

    Processes born at or after the wrapped command root are part of this run, so
    their full lifetime counters are credited on first observation.  Older
    ``--extra-pid``/container processes establish a zero baseline when first
    observed.  Keeping the last value by (PID, start-time) also prevents PID
    reuse or a transiently unreadable process from double-counting counters.
    """

    COUNTER_FIELDS = (
        "utime_ticks",
        "stime_ticks",
        "read_bytes",
        "write_bytes",
        "cancelled_write_bytes",
        "rchar",
        "wchar",
    )

    def __init__(self, zero_baseline_start_ticks: int | None = None) -> None:
        try:
            self.clock_ticks = float(os.sysconf(os.sysconf_names["SC_CLK_TCK"]))
        except (AttributeError, OSError, ValueError):
            # Unit-test fallback for non-POSIX hosts; Linux formal runs use sysconf.
            self.clock_ticks = 100.0
        self.zero_baseline_start_ticks = zero_baseline_start_ticks
        self.active: dict[tuple[int, int], ProcCounters] = {}
        self.last_seen: dict[tuple[int, int], ProcCounters] = {}
        self.totals = {field: 0 for field in self.COUNTER_FIELDS}
        self.previous_total_cpu_s: float | None = None
        self.previous_read_bytes: int | None = None
        self.previous_write_bytes: int | None = None
        self.previous_monotonic: float | None = None

    @staticmethod
    def _counter(proc: ProcCounters, field: str) -> int:
        if field == "utime_ticks":
            return proc.stat.utime_ticks
        if field == "stime_ticks":
            return proc.stat.stime_ticks
        return int(getattr(proc, field))

    def snapshot(
        self,
        pids: Iterable[int],
        now_mono: float,
        zero_baseline_pids: Iterable[int] = (),
    ) -> dict[str, object]:
        forced_zero = set(zero_baseline_pids)
        current: dict[tuple[int, int], ProcCounters] = {}
        for pid in sorted(set(pids)):
            proc = read_proc_counters(pid)
            if proc is not None:
                current[(pid, proc.stat.start_ticks)] = proc

        for key, proc in current.items():
            previous = self.last_seen.get(key)
            if previous is None:
                if (
                    proc.stat.pid not in forced_zero
                    and self.zero_baseline_start_ticks is not None
                    and proc.stat.start_ticks >= self.zero_baseline_start_ticks
                ):
                    for field in self.COUNTER_FIELDS:
                        self.totals[field] += self._counter(proc, field)
            else:
                for field in self.COUNTER_FIELDS:
                    delta = self._counter(proc, field) - self._counter(previous, field)
                    self.totals[field] += max(delta, 0)
            self.last_seen[key] = proc
        self.active = current
        totals = dict(self.totals)

        user_s = totals["utime_ticks"] / self.clock_ticks
        sys_s = totals["stime_ticks"] / self.clock_ticks
        total_cpu_s = user_s + sys_s
        cpu_pct = 0.0
        read_rate = 0.0
        write_rate = 0.0
        if self.previous_monotonic is not None:
            elapsed = max(now_mono - self.previous_monotonic, 1.0e-9)
            cpu_pct = max(total_cpu_s - (self.previous_total_cpu_s or 0.0), 0.0) / elapsed * 100.0
            read_rate = max(totals["read_bytes"] - (self.previous_read_bytes or 0), 0) / elapsed / 2**20
            write_rate = max(totals["write_bytes"] - (self.previous_write_bytes or 0), 0) / elapsed / 2**20
        self.previous_monotonic = now_mono
        self.previous_total_cpu_s = total_cpu_s
        self.previous_read_bytes = totals["read_bytes"]
        self.previous_write_bytes = totals["write_bytes"]

        return {
            "process_count": len(current),
            "pids": ",".join(str(key[0]) for key in sorted(current)),
            "process_user_cpu_s": user_s,
            "process_sys_cpu_s": sys_s,
            "process_cpu_pct": cpu_pct,
            "process_rss_bytes": sum(proc.rss_bytes for proc in current.values()),
            "process_rss_unreadable": sum(not proc.rss_readable for proc in current.values()),
            "process_pss_bytes": sum(proc.pss_bytes for proc in current.values()),
            "process_pss_unreadable": sum(not proc.pss_readable for proc in current.values()),
            "process_io_unreadable": sum(not proc.io_readable for proc in current.values()),
            "process_read_bytes": totals["read_bytes"],
            "process_write_bytes": totals["write_bytes"],
            "process_cancelled_write_bytes": totals["cancelled_write_bytes"],
            "process_rchar": totals["rchar"],
            "process_wchar": totals["wchar"],
            "process_read_mib_s": read_rate,
            "process_write_mib_s": write_rate,
        }

    def identities(self) -> list[dict[str, int]]:
        """Return the exact PID/start-time identities from the latest sample."""

        return [
            {
                "pid": proc.stat.pid,
                "ppid": proc.stat.ppid,
                "pgrp": proc.stat.pgrp,
                "start_ticks": proc.stat.start_ticks,
            }
            for _, proc in sorted(self.active.items())
        ]


def read_meminfo() -> dict[str, int]:
    wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    result = {key: 0 for key in wanted}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            if key in wanted:
                result[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return result


def read_loadavg() -> tuple[float, float, float]:
    try:
        fields = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        return float(fields[0]), float(fields[1]), float(fields[2])
    except (OSError, ValueError, IndexError):
        return 0.0, 0.0, 0.0


def read_device_counters(device: str) -> DeviceCounters | None:
    try:
        for line in Path("/proc/diskstats").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 14 and fields[2] == device:
                values = [int(raw) for raw in fields[3:14]]
                return DeviceCounters(
                    reads=values[0],
                    sectors_read=values[2],
                    read_ms=values[3],
                    writes=values[4],
                    sectors_written=values[6],
                    write_ms=values[7],
                    io_ms=values[9],
                )
    except (OSError, ValueError):
        return None
    return None


def device_delta(previous: DeviceCounters | None, current: DeviceCounters | None, elapsed: float) -> dict[str, object]:
    empty = {
        "device_read_iops": "",
        "device_write_iops": "",
        "device_read_mib_s": "",
        "device_write_mib_s": "",
        "device_read_await_ms": "",
        "device_write_await_ms": "",
        "device_await_ms": "",
        "device_util_pct": "",
    }
    if previous is None or current is None or elapsed <= 0:
        return empty
    deltas = [
        current.reads - previous.reads,
        current.sectors_read - previous.sectors_read,
        current.read_ms - previous.read_ms,
        current.writes - previous.writes,
        current.sectors_written - previous.sectors_written,
        current.write_ms - previous.write_ms,
        current.io_ms - previous.io_ms,
    ]
    if any(value < 0 for value in deltas):
        return empty
    reads, read_sectors, read_ms, writes, write_sectors, write_ms, io_ms = deltas
    operations = reads + writes
    return {
        "device_read_iops": reads / elapsed,
        "device_write_iops": writes / elapsed,
        "device_read_mib_s": read_sectors * 512 / elapsed / 2**20,
        "device_write_mib_s": write_sectors * 512 / elapsed / 2**20,
        "device_read_await_ms": read_ms / reads if reads else 0.0,
        "device_write_await_ms": write_ms / writes if writes else 0.0,
        "device_await_ms": (read_ms + write_ms) / operations if operations else 0.0,
        "device_util_pct": min(io_ms / (elapsed * 1000.0) * 100.0, 100.0),
    }


def classify_store_file(relative: Path, root_role: str = "store") -> str:
    if root_role == "temp":
        return "temp"
    lowered_parts = [part.lower() for part in relative.parts]
    name = lowered_parts[-1] if lowered_parts else ""
    suffix = relative.suffix.lower()
    if any(part in {"tmp", "temp", ".tmp", ".temp"} for part in lowered_parts) or name.endswith((".tmp", ".partial", ".pending")):
        return "temp"
    if name in {"manifest", "current", "lock"} or name.startswith(("manifest-", "options-")):
        return "manifest"
    if "wal" in name or any(part == "wal" for part in lowered_parts) or suffix == ".wal":
        return "wal"
    if "catalog" in name or name.startswith("schema"):
        return "catalog"
    if any(
        token in name
        for token in (
            "sidecar",
            "degree",
            "signature",
            "semantic_index",
            "semantic-index",
            "budgeted-edge-candidates",
        )
    ):
        return "sidecar"
    if name == "meta.json" or suffix in {".meta", ".idx", ".index", ".offset", ".bloom"} or any(
        token in name for token in ("metadata", "offset", "bloom")
    ):
        return "metadata"
    if name.startswith(("snb_vertices", "snb_edge_props")) or suffix in {
        ".edge",
        ".csr",
        ".sst",
        ".data",
        ".bin",
    } or any(
        part in {"levels", "base_graph", "delta", "payload"} for part in lowered_parts
    ):
        return "payload"
    return "other"


def scan_disk_root(role: str, label: str, root: Path, mono: float, sample_index: int) -> dict[str, object]:
    started = time.monotonic()
    totals = {category: 0 for category in DISK_CATEGORIES}
    files = 0
    scan_errors = 0
    # ``exists`` means a usable directory, not merely a filesystem object.
    exists = root.is_dir()
    if exists:
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                size = entry.stat(follow_symlinks=False).st_size
                                relative = Path(entry.path).relative_to(root)
                                totals[classify_store_file(relative, role)] += size
                                files += 1
                        except FileNotFoundError:
                            continue
                        except (PermissionError, OSError):
                            scan_errors += 1
                            continue
            except (FileNotFoundError, NotADirectoryError):
                continue
            except (PermissionError, OSError):
                scan_errors += 1
                continue
    total = sum(totals.values())
    return {
        "schema_version": RESOURCE_SCHEMA_VERSION,
        "timestamp_utc": utc_now(),
        "monotonic_s": mono,
        "sample_index": sample_index,
        "root_role": role,
        "root_label": label,
        "root_path": str(root),
        "exists": int(exists),
        "scan_complete": int(scan_errors == 0),
        "scan_error_count": scan_errors,
        "total_bytes": total,
        "file_count": files,
        **{f"{category}_bytes": totals[category] for category in DISK_CATEGORIES},
        "scan_elapsed_ms": (time.monotonic() - started) * 1000.0,
    }


def parse_root_spec(raw: str, role: str) -> tuple[str, str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"expected LABEL=PATH, got {raw!r}")
    label, path = raw.split("=", 1)
    if not label or not path or any(ch in label for ch in "\t\n\r"):
        raise argparse.ArgumentTypeError(f"invalid LABEL=PATH: {raw!r}")
    return role, label, Path(path).resolve()


def _float(row: dict[str, str], *keys: str, required: bool = False) -> float:
    for key in keys:
        raw = row.get(key)
        if raw not in (None, ""):
            try:
                value = float(raw)
                if not math.isfinite(value):
                    raise ValueError(f"non-finite iostat value for {key}: {raw!r}")
                return value
            except ValueError as exc:
                raise ValueError(f"invalid iostat value for {key}: {raw!r}") from exc
    if required:
        raise ValueError(f"missing iostat column; expected one of {keys!r}")
    return 0.0


def parse_iostat_raw(text: str, device: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    header: list[str] | None = None
    timestamp = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _DATE_LINE.match(line):
            timestamp = line
            continue
        fields = line.split()
        if fields and fields[0] == "Device":
            header = fields
            continue
        if header is None or not fields or fields[0] != device or len(fields) < len(header):
            continue
        parsed = dict(zip(header, fields))
        read_iops = _float(parsed, "r/s", required=True)
        write_iops = _float(parsed, "w/s", required=True)
        if "rMB/s" in parsed:
            read_mib = _float(parsed, "rMB/s", required=True)
        else:
            read_mib = _float(parsed, "rkB/s", required=True) / 1024.0
        if "wMB/s" in parsed:
            write_mib = _float(parsed, "wMB/s", required=True)
        else:
            write_mib = _float(parsed, "wkB/s", required=True) / 1024.0
        read_await = _float(parsed, "r_await", required=True)
        write_await = _float(parsed, "w_await", required=True)
        operations = read_iops + write_iops
        await_ms = _float(parsed, "await")
        if "await" not in parsed:
            await_ms = (read_await * read_iops + write_await * write_iops) / operations if operations else 0.0
        rows.append(
            {
                "schema_version": RESOURCE_SCHEMA_VERSION,
                "sample_index": len(rows),
                "timestamp_raw": timestamp,
                "device": device,
                "read_iops": read_iops,
                "write_iops": write_iops,
                "read_mib_s": read_mib,
                "write_mib_s": write_mib,
                "read_await_ms": read_await,
                "write_await_ms": write_await,
                "await_ms": await_ms,
                "aqu_sz": _float(parsed, "aqu-sz", "avgqu-sz", required=True),
                "util_pct": _float(parsed, "%util", required=True),
            }
        )
    return rows


def write_iostat_tsv(raw_path: Path, output_path: Path, device: str) -> int:
    text = raw_path.read_text(encoding="utf-8", errors="replace") if raw_path.exists() else ""
    rows = parse_iostat_raw(text, device)
    with output_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=IOSTAT_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def start_auxiliary(command: list[str], output: Path) -> tuple[subprocess.Popen[str] | None, object | None]:
    if shutil.which(command[0]) is None:
        output.touch(exist_ok=False)
        return None, None
    handle = output.open("x", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            command,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        return proc, handle
    except OSError:
        handle.close()
        return None, None


def stop_auxiliary(proc: subprocess.Popen[str] | None, handle: object | None) -> None:
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    if handle is not None:
        handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--root-pid", required=True, type=int)
    parser.add_argument("--root-pgid", type=int)
    parser.add_argument("--stop-file", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--disk-interval", type=float, default=15.0)
    parser.add_argument("--device", default="nvme1n1")
    parser.add_argument("--data-mount", type=Path, default=Path("/data"))
    parser.add_argument("--store", action="append", default=[])
    parser.add_argument("--temp", action="append", default=[])
    parser.add_argument("--extra-pid", action="append", default=[], type=int)
    parser.add_argument("--container", action="append", default=[])
    parser.add_argument("--orphan-grace", type=float, default=30.0)
    args = parser.parse_args()
    if args.interval <= 0 or args.disk_interval <= 0 or args.orphan_grace < 0:
        parser.error("intervals must be positive and orphan grace non-negative")
    return args


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    args.data_mount = args.data_mount.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    roots = [parse_root_spec(raw, "store") for raw in args.store]
    roots.extend(parse_root_spec(raw, "temp") for raw in args.temp)
    if len({(role, label) for role, label, _ in roots}) != len(roots):
        raise SystemExit("duplicate disk root label")

    resource_path = run_dir / "resource-samples.tsv"
    disk_path = run_dir / "disk-samples.tsv"
    iostat_tsv_path = run_dir / "iostat-samples.tsv"
    ready_path = args.ready_file.resolve()
    if ready_path.parent != run_dir or ready_path.name != "collector-ready.json":
        raise SystemExit("--ready-file must be RUN_DIR/collector-ready.json")
    owned_paths = [
        resource_path,
        disk_path,
        iostat_tsv_path,
        run_dir / "pidstat.raw",
        run_dir / "iostat.raw",
        run_dir / "collector-status.json",
        ready_path,
    ]
    if any(path.exists() for path in owned_paths):
        raise SystemExit("refusing to overwrite existing collector artifact")

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGHUP, _signal_handler)

    pidstat_interval = max(1, int(math.ceil(args.interval)))
    pidstat_proc, pidstat_handle = start_auxiliary(
        [
            "pidstat",
            "-h",
            "-r",
            "-u",
            "-d",
            "-T",
            "ALL",
            "-p",
            str(args.root_pid),
            str(pidstat_interval),
        ],
        run_dir / "pidstat.raw",
    )
    iostat_proc, iostat_handle = start_auxiliary(
        ["iostat", "-dxm", "-y", "-t", str(pidstat_interval), args.device],
        run_dir / "iostat.raw",
    )

    started_mono = time.monotonic()
    root_initial = read_proc_stat(args.root_pid)
    root_pgid = args.root_pgid or (root_initial.pgrp if root_initial else args.root_pid)
    root_start_ticks = root_initial.start_ticks if root_initial else None
    accumulator = ProcessAccumulator(root_start_ticks)
    previous_device = read_device_counters(args.device)
    previous_device_mono = started_mono
    known_pids: set[int] = set()
    container_pids: dict[str, int] = {}
    container_current: dict[str, dict[str, object]] = {}
    containers_seen: dict[str, int] = {}
    container_identity_history: dict[str, list[dict[str, object]]] = {
        name: [] for name in args.container
    }
    container_identity_unique_set: dict[str, list[dict[str, object]]] = {
        name: [] for name in args.container
    }
    extra_pids_seen: set[int] = set()
    process_identity_intervals: dict[tuple[int, int], dict[str, int]] = {}
    collector_ready: dict[str, object] | None = None
    next_container_resolve = 0.0
    next_disk = started_mono
    next_resource = started_mono
    sample_index = 0
    disk_round = 0
    disk_rows = 0
    root_seen_alive = False
    orphan_since: float | None = None
    errors: list[str] = []

    try:
        with resource_path.open("x", encoding="utf-8", newline="") as resource_handle, disk_path.open(
            "x", encoding="utf-8", newline=""
        ) as disk_handle:
            resource_writer = csv.DictWriter(
                resource_handle, fieldnames=RESOURCE_COLUMNS, delimiter="\t", lineterminator="\n"
            )
            disk_writer = csv.DictWriter(disk_handle, fieldnames=DISK_COLUMNS, delimiter="\t", lineterminator="\n")
            resource_writer.writeheader()
            disk_writer.writeheader()
            resource_handle.flush()
            disk_handle.flush()

            while True:
                now = time.monotonic()
                stop_requested = args.stop_file.exists() or _STOP
                if now >= next_container_resolve or stop_requested:
                    for name in args.container:
                        identity = resolve_container_identity(name)
                        if identity is not None:
                            record_container_identity(
                                name,
                                identity,
                                sample_index,
                                container_identity_history,
                                container_identity_unique_set,
                            )
                            resolved = int(identity["pid"])
                            container_current[name] = identity
                            container_pids[name] = resolved
                            containers_seen[name] = resolved
                            if len(container_identity_unique_set[name]) > 1:
                                message = f"container identity changed during collection: {name}"
                                if message not in errors:
                                    errors.append(message)
                        else:
                            container_current.pop(name, None)
                            container_pids.pop(name, None)
                            if collector_ready is not None:
                                message = f"container disappeared after collector readiness: {name}"
                                if message not in errors:
                                    errors.append(message)
                    next_container_resolve = now + 10.0

                external_roots = {*args.extra_pid, *container_pids.values()}
                external_pids = descendants(external_roots)
                discovery_roots = {args.root_pid, *external_roots, *known_pids}
                extra_pids_seen.update(pid for pid in args.extra_pid if read_proc_stat(pid) is not None)
                pids = descendants(discovery_roots) | process_group_members(root_pgid)
                known_pids = set(pids)
                proc_values = accumulator.snapshot(pids, now, external_pids)
                for identity in accumulator.identities():
                    identity_key = (identity["pid"], identity["start_ticks"])
                    current = process_identity_intervals.get(identity_key)
                    if current is None:
                        process_identity_intervals[identity_key] = {
                            **identity,
                            "first_sample_index": sample_index,
                            "last_sample_index": sample_index,
                            "sample_count": 1,
                        }
                    else:
                        current["last_sample_index"] = sample_index
                        current["sample_count"] += 1
                sampled_pids = {
                    int(raw) for raw in str(proc_values["pids"]).split(",") if raw
                }
                sampled_external_pids = external_pids & sampled_pids

                root_stat = read_proc_stat(args.root_pid)
                root_alive = int(
                    root_stat is not None
                    and root_stat.start_ticks == root_start_ticks
                    and proc_stat_is_live(root_stat)
                )
                root_seen_alive = root_seen_alive or bool(root_alive)
                current_device = read_device_counters(args.device)
                dev_values = device_delta(previous_device, current_device, now - previous_device_mono)
                previous_device = current_device
                previous_device_mono = now
                load1, load5, load15 = read_loadavg()
                mem = read_meminfo()
                try:
                    data_stat = os.statvfs(args.data_mount)
                    data_free = data_stat.f_bavail * data_stat.f_frsize
                except OSError:
                    data_free = 0
                row = {
                    "schema_version": RESOURCE_SCHEMA_VERSION,
                    "timestamp_utc": utc_now(),
                    "monotonic_s": now - started_mono,
                    "sample_index": sample_index,
                    "root_pid": args.root_pid,
                    "root_alive": root_alive,
                    **proc_values,
                    "host_load1": load1,
                    "host_load5": load5,
                    "host_load15": load15,
                    "host_mem_total_bytes": mem["MemTotal"],
                    "host_mem_available_bytes": mem["MemAvailable"],
                    "host_swap_total_bytes": mem["SwapTotal"],
                    "host_swap_free_bytes": mem["SwapFree"],
                    "device": args.device,
                    **dev_values,
                    "data_mount": str(args.data_mount),
                    "data_free_bytes": data_free,
                }
                resource_writer.writerow(row)
                resource_handle.flush()
                if collector_ready is None and sample_index == 0:
                    containers_resolved = set(container_current) == set(args.container)
                    container_roots_sampled = all(pid in sampled_pids for pid in container_pids.values())
                    extra_roots_sampled = all(pid in sampled_pids for pid in args.extra_pid)
                    if containers_resolved and container_roots_sampled and extra_roots_sampled:
                        collector_ready = {
                            "schema_version": CONTAINER_IDENTITY_SCHEMA_VERSION,
                            "state": "READY",
                            "ready_at_utc": utc_now(),
                            "root_pid": args.root_pid,
                            "resource_sample_index": sample_index,
                            "external_zero_baseline": True,
                            "containers": {
                                name: {
                                    "container_id": str(container_current[name]["container_id"]),
                                    "pid": int(container_current[name]["pid"]),
                                    "started_at": str(container_current[name]["started_at"]),
                                    "restart_count": int(container_current[name]["restart_count"]),
                                }
                                for name in args.container
                            },
                            "extra_pids": list(args.extra_pid),
                            "sample_pids": sorted(sampled_pids),
                            "external_zero_baseline_pids": sorted(sampled_external_pids),
                        }
                        atomic_json(ready_path, collector_ready)
                    else:
                        errors.append(
                            "external container/PID zero baseline was not established in the first sample"
                        )
                sample_index += 1

                if now >= next_disk or stop_requested:
                    for role, label, root in roots:
                        disk_writer.writerow(scan_disk_root(role, label, root, now - started_mono, disk_round))
                        disk_rows += 1
                    if roots:
                        disk_round += 1
                    disk_handle.flush()
                    next_disk = now + args.disk_interval

                active_count = int(proc_values["process_count"])
                if not root_alive and active_count == 0:
                    orphan_since = orphan_since or now
                else:
                    orphan_since = None
                if stop_requested:
                    break
                if collector_ready is None and errors:
                    break
                if orphan_since is not None and now - orphan_since >= args.orphan_grace:
                    errors.append("collector auto-stopped after command process tree disappeared without stop marker")
                    break
                next_resource += args.interval
                if next_resource <= time.monotonic():
                    next_resource = time.monotonic() + args.interval
                time.sleep(max(next_resource - time.monotonic(), 0.01))
    except Exception as exc:  # noqa: BLE001 - collector must preserve a failure artifact
        errors.append(f"fatal collector exception: {type(exc).__name__}: {exc}")
    finally:
        stop_auxiliary(pidstat_proc, pidstat_handle)
        stop_auxiliary(iostat_proc, iostat_handle)

    try:
        iostat_rows = write_iostat_tsv(run_dir / "iostat.raw", iostat_tsv_path, args.device)
    except Exception as exc:  # noqa: BLE001
        iostat_rows = 0
        errors.append(f"iostat parse failed: {type(exc).__name__}: {exc}")

    status = {
        "schema_version": RESOURCE_SCHEMA_VERSION,
        "state": "DONE" if not errors else "FAILED",
        "started_at_utc": datetime.fromtimestamp(time.time() - (time.monotonic() - started_mono), timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "ended_at_utc": utc_now(),
        "root_pid": args.root_pid,
        "root_pgid": root_pgid,
        "root_seen_alive": root_seen_alive,
        "resource_samples": sample_index,
        "disk_samples": disk_rows,
        "disk_rounds": disk_round,
        "iostat_samples": iostat_rows,
        "pidstat_started": pidstat_proc is not None,
        "iostat_started": iostat_proc is not None,
        "device": args.device,
        "data_mount": str(args.data_mount),
        "interval_s": args.interval,
        "disk_interval_s": args.disk_interval,
        "stores": [{"role": role, "label": label, "path": str(path)} for role, label, path in roots],
        "containers": args.container,
        "containers_seen": containers_seen,
        "container_identity_schema_version": CONTAINER_IDENTITY_SCHEMA_VERSION,
        "container_identity_history": container_identity_history,
        "container_identity_unique_set": container_identity_unique_set,
        "process_identity_schema_version": PROCESS_IDENTITY_SCHEMA_VERSION,
        "process_identity_unique_set": [
            process_identity_intervals[key] for key in sorted(process_identity_intervals)
        ],
        "collector_ready": collector_ready
        or {
            "schema_version": CONTAINER_IDENTITY_SCHEMA_VERSION,
            "state": "NOT_READY",
            "root_pid": args.root_pid,
            "external_zero_baseline": False,
        },
        "extra_pids": args.extra_pid,
        "extra_pids_seen": sorted(extra_pids_seen),
        "errors": errors,
    }
    atomic_json(run_dir / "collector-status.json", status)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
