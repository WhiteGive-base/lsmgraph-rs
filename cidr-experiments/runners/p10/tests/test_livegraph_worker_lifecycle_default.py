#!/usr/bin/env python3
"""Default, no-LiveGraph-binary tests for detached worker containment."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


P10_DIR = Path(__file__).resolve().parents[1]
ADAPTER = P10_DIR / "adapters/livegraph_adapter.py"
SPEC = importlib.util.spec_from_file_location("livegraph_worker_lifecycle_default", ADAPTER)
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


class LiveGraphWorkerLifecycleDefaultTests(unittest.TestCase):
    def test_same_process_alive_binds_start_ticks_and_treats_unreadable_as_live(self) -> None:
        pid = os.getpid()
        start_ticks = adapter.process_start_ticks(pid)
        self.assertTrue(adapter.same_process_alive(pid, start_ticks))
        self.assertFalse(adapter.same_process_alive(pid, start_ticks + 1))
        self.assertFalse(adapter.same_process_alive(2**30, start_ticks))
        with mock.patch.object(
            adapter,
            "process_start_ticks",
            side_effect=adapter.ContractError("injected proc read failure"),
        ):
            self.assertTrue(adapter.same_process_alive(pid, start_ticks))
        with mock.patch.object(
            adapter.Path,
            "iterdir",
            side_effect=PermissionError("injected proc enumeration failure"),
        ):
            with self.assertRaises(adapter.ContractError):
                adapter.process_group_members(pid)

    def test_success_records_pid_and_start_ticks_without_external_binary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-worker-default-") as raw:
            root = Path(raw)
            output, temp = root / "output", root / "temp"
            output.mkdir(); temp.mkdir()
            worker = executable(root / "worker.py", "raise SystemExit(0)\n")
            code, lifecycle = adapter.run_worker(
                [str(worker)], {"PATH": "/usr/bin:/bin", "LANG": "C"}, temp, output,
                adapter.StageJournal(output / "stages.jsonl"),
            )
            self.assertEqual(code, 0)
            identity = lifecycle["identity"]
            self.assertGreater(identity["pid"], 0)
            self.assertGreater(identity["proc_start_ticks"], 0)
            self.assertEqual(identity["process_group_id"], identity["pid"])
            self.assertEqual(identity["process_group_members_after_wait"], [])
            self.assertFalse(adapter.same_process_alive(identity["pid"], identity["proc_start_ticks"]))
            self.assertEqual(adapter.process_group_members(identity["pid"]), set())

    def test_journal_failure_still_kills_and_reaps_worker_group(self) -> None:
        class BrokenJournal:
            def event(self, *_args: object, **_kwargs: object) -> None:
                raise OSError("injected fsync failure")

        with tempfile.TemporaryDirectory(prefix="livegraph-worker-journal-fail-") as raw:
            root = Path(raw)
            output, temp = root / "output", root / "temp"
            output.mkdir(); temp.mkdir()
            worker = executable(root / "worker.py", "import time\ntime.sleep(60)\n")
            with self.assertRaises(OSError):
                adapter.run_worker(
                    [str(worker)], {"PATH": "/usr/bin:/bin", "LANG": "C"},
                    temp, output, BrokenJournal(),
                )
            start = json.loads((output / "worker-start.json").read_text(encoding="utf-8"))
            self.assertFalse(adapter.same_process_alive(start["pid"], start["proc_start_ticks"]))
            self.assertEqual(adapter.process_group_members(start["pid"]), set())

    def test_exited_leader_with_live_descendant_is_failed_and_group_is_killed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-worker-descendant-") as raw:
            root = Path(raw)
            output, temp = root / "output", root / "temp"
            output.mkdir(); temp.mkdir()
            child_pid = root / "child.pid"
            worker = executable(
                root / "worker.py",
                "import pathlib, subprocess, sys\n"
                f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\npathlib.Path({str(child_pid)!r}).write_text(str(p.pid))\n",
            )
            with self.assertRaises(adapter.ContractError):
                adapter.run_worker(
                    [str(worker)], {"PATH": "/usr/bin:/bin", "LANG": "C"}, temp, output,
                    adapter.StageJournal(output / "stages.jsonl"),
                )
            start = json.loads((output / "worker-start.json").read_text(encoding="utf-8"))
            self.assertEqual(adapter.process_group_members(start["pid"]), set())

    def test_sigterm_is_forwarded_before_journal_io_and_group_is_reaped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-worker-signal-default-") as raw:
            root = Path(raw)
            output, temp = root / "output", root / "temp"
            output.mkdir(); temp.mkdir()
            worker = executable(root / "worker.py", "import time\ntime.sleep(60)\n")
            harness = root / "harness.py"
            harness.write_text(
                "import importlib.util, pathlib, sys\n"
                f"spec=importlib.util.spec_from_file_location('lg', {str(ADAPTER)!r})\n"
                "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                f"out=pathlib.Path({str(output)!r}); tmp=pathlib.Path({str(temp)!r})\n"
                "try:\n"
                f" m.run_worker([{str(worker)!r}], {{'PATH':'/usr/bin:/bin','LANG':'C'}}, tmp, out, m.StageJournal(out/'stages.jsonl'))\n"
                "except m.ContractError as exc:\n"
                " raise SystemExit(7 if 'received signal' in str(exc) else 8)\n"
                "raise SystemExit(9)\n",
                encoding="utf-8",
            )
            process = subprocess.Popen([sys.executable, "-B", str(harness)])
            start_path = output / "worker-start.json"
            deadline = time.monotonic() + 10
            while not start_path.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(start_path.is_file())
            start = json.loads(start_path.read_text(encoding="utf-8"))
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=10), 7)
            self.assertFalse(adapter.same_process_alive(start["pid"], start["proc_start_ticks"]))
            self.assertEqual(adapter.process_group_members(start["pid"]), set())


if __name__ == "__main__":
    unittest.main()
