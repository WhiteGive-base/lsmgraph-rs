#!/usr/bin/env python3
"""Real LiveGraph fixture smoke for the P10 adapter (never performance data)."""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

P10_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = P10_DIR.parents[2]
sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    FRESH_IMPORT_PROCESS_LIFETIME,
    INTERFACE_SCOPE,
    REQUEST_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    TRUTH_DIGEST_ALGORITHM,
    sha256_file,
    validate_adapter_outputs,
)

ADAPTER_SPEC = importlib.util.spec_from_file_location(
    "livegraph_adapter_under_test", P10_DIR / "adapters/livegraph_adapter.py"
)
assert ADAPTER_SPEC is not None and ADAPTER_SPEC.loader is not None
adapter_module = importlib.util.module_from_spec(ADAPTER_SPEC)
ADAPTER_SPEC.loader.exec_module(adapter_module)


class LiveGraphAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw_binary = os.environ.get("LIVEGRAPH_P10_BINARY")
        if not raw_binary:
            raise unittest.SkipTest("LIVEGRAPH_P10_BINARY is not set")
        cls.binary = Path(raw_binary).resolve()
        if not cls.binary.is_file():
            raise unittest.SkipTest(f"LiveGraph worker is missing: {cls.binary}")
        cls.adapter = P10_DIR / "adapters" / "livegraph_adapter.py"
        cls.runner = P10_DIR / "run_suite.py"
        cls.fixture_manifest = Path(__file__).resolve().parent / "fixture-suite.json"
        cls.fixture_p31 = Path(__file__).resolve().parent / "fixture_p31.sh"
        cls.dataset = REPO_ROOT / "baseline" / "shared-truth" / "fixtures" / "dense-edges.txt"
        cls.truth = REPO_ROOT / "baseline" / "shared-truth" / "fixtures" / "truth.tsv"
        capability = subprocess.run(
            [str(cls.binary), "--capabilities"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        cls.capability = json.loads(capability.stdout)
        cls.runtime_library = Path(cls.capability["runtime_library_path"]).resolve()

    def request(
        self,
        store: Path,
        *,
        execution_mode: str = "fixture",
        truth: Path | None = None,
        query_count: int = 2,
    ) -> dict:
        truth = truth or self.truth
        return {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "suite_id": "p10-livegraph-real-fixture",
            "run_id": "fixture-r01",
            "execution_mode": execution_mode,
            "system_id": "livegraph",
            "group": "embedded",
            "system_version": "audited-livegraph-fixture",
            "interface_scope": INTERFACE_SCOPE,
            "repeat_index": 2,
            "process_lifetime": FRESH_IMPORT_PROCESS_LIFETIME,
            "binary": {"path": str(self.binary), "sha256": sha256_file(self.binary)},
            "dataset": {"path": str(self.dataset), "sha256": sha256_file(self.dataset)},
            "runtime_libraries": [
                {
                    "path": str(self.runtime_library),
                    "sha256": sha256_file(self.runtime_library),
                }
            ],
            "store_roots": [
                {
                    "label": "livegraph",
                    "path": str(store),
                    "sha256": "",
                }
            ],
            "truth": {
                "path": str(truth),
                "sha256": sha256_file(truth),
                "query_count": query_count,
                "digest_algorithm": TRUTH_DIGEST_ALGORITHM,
            },
            "timing": {
                "timing_boundary": TIMING_BOUNDARY,
                "clock": CLOCK_NAME,
                "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
                "process_reuse_between_phases": True,
                "warmup_passes": 2,
                "measured_passes": 2,
                "concurrency": 1,
                "per_query_timeout_ms": 1000,
                "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
            },
        }

    def invoke(
        self,
        request: dict,
        root: Path,
        *,
        extra: list[str] | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        request_path = root / "request.json"
        output = root / "output"
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        command = [
            sys.executable,
            str(self.adapter),
            *(extra or []),
            "--request",
            str(request_path),
            "--output-dir",
            str(output),
        ]
        return subprocess.run(
            command,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_real_livegraph_fixture_is_contract_valid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-fixture-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store)
            completed = self.invoke(request, root)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            truth_rows = []
            with self.truth.open("r", encoding="utf-8") as handle:
                header = next(handle)
                self.assertEqual(header, "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n")
                for line in handle:
                    fields = [int(value) for value in line.rstrip("\n").split("\t")]
                    truth_rows.append(dict(zip(("query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"), fields)))
            system = {
                "id": "livegraph",
                "display_name": "LiveGraph",
                "group": "embedded",
                "system_version": request["system_version"],
            }
            validated = validate_adapter_outputs(
                output_dir=root / "output",
                request=request,
                system=system,
                truth_rows=truth_rows,
                max_timeouts=0,
            )
            self.assertEqual(validated["query_count"], 2)
            self.assertEqual(validated["completed_queries"], 4)
            self.assertEqual(validated["mismatch_queries"], 0)
            self.assertGreater(validated["import_wall_s"], 0)
            self.assertGreater(validated["import_store_logical_bytes"], 0)
            self.assertTrue((store / "livegraph-block").is_file())
            self.assertTrue((store / "livegraph-wal").is_file())
            provenance = json.loads(
                (root / "output/adapter-provenance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["schema_version"], "p10-livegraph-adapter-provenance-v1")
            self.assertEqual(provenance["temp"]["actual_value"], str(root / "output/tmp"))
            self.assertEqual(
                provenance["environment"]["injected"]["LD_LIBRARY_PATH"],
                str(self.runtime_library.parent),
            )
            self.assertIsNone(provenance["formal_build"])
            self.assertIsNone(provenance["p02b_admission"])
            lifecycle = provenance["worker_lifecycle"]
            self.assertEqual(lifecycle["identity"]["state"], "EXITED")
            self.assertFalse(lifecycle["identity"]["same_process_alive_after_wait"])
            stages = [
                json.loads(line)["stage"]
                for line in (root / "output/adapter-stage-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                stages,
                [
                    "preflight-passed",
                    "worker-started",
                    "worker-exited",
                    "worker-output-validated",
                    "adapter-result-published",
                    "adapter-complete",
                ],
            )

    def test_worker_declares_fresh_import_and_runtime_library(self) -> None:
        self.assertEqual(
            self.capability["process_lifetime"], FRESH_IMPORT_PROCESS_LIFETIME
        )
        self.assertEqual(Path(self.capability["runtime_library_path"]).resolve(), self.runtime_library)

    def test_orchestrator_and_p31_bridge_execute_real_adapter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-orchestrated-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            temp_base = root / "temp"
            temp_base.mkdir()
            manifest = json.loads(self.fixture_manifest.read_text(encoding="utf-8"))
            manifest["protocol"]["repeats"] = 2
            livegraph = next(system for system in manifest["systems"] if system["id"] == "livegraph")
            livegraph["adapter"] = {
                "path": str(self.adapter),
                "sha256": sha256_file(self.adapter),
                "args": [],
            }
            livegraph["binary"] = {
                "path": str(self.binary),
                "sha256": sha256_file(self.binary),
            }
            livegraph["process_lifetime"] = FRESH_IMPORT_PROCESS_LIFETIME
            livegraph["runtime_libraries"] = [
                {
                    "path": str(self.runtime_library),
                    "sha256": sha256_file(self.runtime_library),
                }
            ]
            livegraph["store_roots"] = [
                {"label": "livegraph", "path": str(store), "sha256": ""}
            ]
            livegraph["temp_roots"] = [{"label": "scratch", "path": str(temp_base)}]
            manifest_path = root / "suite.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            run_root = root / "run"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(self.runner),
                    "run",
                    "--manifest",
                    str(manifest_path),
                    "--run-root",
                    str(run_root),
                    "--mode",
                    "fixture",
                    "--p31-wrapper",
                    str(self.fixture_p31),
                    "--system",
                    "livegraph",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((run_root / "PARTIAL-DONE").is_file())
            validated = json.loads(
                (
                    run_root
                    / "systems"
                    / "livegraph"
                    / "repeat-01"
                    / "validated-result.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(validated["completed_queries"], 2)
            self.assertEqual(validated["mismatch_queries"], 0)
            self.assertEqual(validated["process_lifetime"], FRESH_IMPORT_PROCESS_LIFETIME)
            self.assertGreater(validated["import_wall_s"], 0)
            with (run_root / "repeat-results.tsv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                repeat_row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(
                repeat_row["process_lifetime"], FRESH_IMPORT_PROCESS_LIFETIME
            )
            self.assertGreater(float(repeat_row["import_wall_s"]), 0)
            self.assertGreater(int(repeat_row["import_store_logical_bytes"]), 0)
            with (run_root / "system-results.tsv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                system_row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertGreater(float(system_row["median_import_wall_s"]), 0)
            request = json.loads(
                (
                    run_root
                    / "systems"
                    / "livegraph"
                    / "repeat-01"
                    / "adapter-request.json"
                ).read_text(encoding="utf-8")
            )
            actual_store = Path(request["store_roots"][0]["path"])
            self.assertEqual(actual_store.parent, store)
            self.assertNotEqual(actual_store, store)
            self.assertTrue((actual_store / "livegraph-block").is_file())
            second_request = json.loads(
                (
                    run_root
                    / "systems"
                    / "livegraph"
                    / "repeat-02"
                    / "adapter-request.json"
                ).read_text(encoding="utf-8")
            )
            second_store = Path(second_request["store_roots"][0]["path"])
            self.assertNotEqual(second_store, actual_store)
            self.assertEqual(second_store.parent, store)
            self.assertEqual(len(list(temp_base.iterdir())), 2)
            p31_argv = (
                run_root / "systems" / "livegraph" / "repeat-01" / "p31" / "argv.txt"
            ).read_text(encoding="utf-8")
            for required in (
                "--binary-sha256",
                sha256_file(self.binary),
                "--dataset-sha256",
                sha256_file(self.dataset),
                "--truth-sha256",
                sha256_file(self.truth),
                "--config-sha256",
                "--store",
            ):
                self.assertIn(required, p31_argv)

    def test_formal_mode_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-formal-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store, execution_mode="formal")
            request["timing"]["warmup_passes"] = 1
            request["timing"]["measured_passes"] = 1
            completed = self.invoke(request, root)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("formal LiveGraph P10", completed.stderr)
            self.assertFalse((root / "output" / "adapter-result.json").exists())

    def test_formal_contract_rejects_noncanonical_1700_query_synthetic_truth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-formal-contract-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            source_rows = [line.split("\t") for line in self.truth.read_text(encoding="utf-8").splitlines()[1:]]
            formal_truth = root / "truth-1700.tsv"
            lines = ["query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash"]
            for query_index in range(1700):
                source = source_rows[query_index % len(source_rows)]
                lines.append("\t".join((str(query_index), *source[1:])))
            formal_truth.write_text("\n".join(lines) + "\n", encoding="utf-8")
            request = self.request(
                store,
                execution_mode="formal",
                truth=formal_truth,
                query_count=1700,
            )
            request["timing"]["warmup_passes"] = 1
            request["timing"]["measured_passes"] = 1
            completed = self.invoke(request, root)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("canonical SF10 dense dataset", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])

    def test_formal_contract_accepts_1700_query_synthetic_truth(self) -> None:
        """Retain the historical test ID under the stronger pinned-truth contract.

        The earlier assertion accepted any 1700-row synthetic truth.  Formal
        LiveGraph now requires the canonical SF10 dense truth, so preserving the
        old test must assert the fail-closed replacement rather than re-open the
        retired acceptance path.
        """

        self.test_formal_contract_rejects_noncanonical_1700_query_synthetic_truth()

    def test_formal_protocol_rejects_noncanonical_pass_count_before_store_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-formal-passes-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store, execution_mode="formal")
            completed = self.invoke(request, root)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("exactly one warmup and one measured pass", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])

    def test_runtime_library_sha_mismatch_fails_before_store_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-lib-sha-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store)
            request["runtime_libraries"][0]["sha256"] = "0" * 64
            completed = self.invoke(request, root)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("SHA-256 mismatch", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])

    def test_binary_sha_mismatch_fails_before_store_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-sha-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store)
            request["binary"]["sha256"] = "0" * 64
            completed = self.invoke(request, root)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("SHA-256 mismatch", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])

    def test_whole_store_must_be_empty_before_worker_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-store-empty-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            (store / "unrelated-junk").write_text("must fail\n", encoding="utf-8")
            completed = self.invoke(self.request(store), root)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("wholly empty", completed.stderr)
            self.assertEqual([path.name for path in store.iterdir()], ["unrelated-junk"])

    def test_inherited_ld_preload_is_rejected_before_worker_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-preload-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            environment = dict(os.environ)
            environment["LD_PRELOAD"] = ""
            completed = self.invoke(self.request(store), root, environment=environment)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("rejects inherited LD_PRELOAD", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])

    def test_multiple_runtime_libraries_are_rejected_before_worker_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-multiple-libs-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store)
            request["runtime_libraries"].append(dict(request["runtime_libraries"][0]))
            completed = self.invoke(request, root)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("exactly one liblivegraph.so", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])

    def test_formal_tmpdir_is_the_orchestrator_materialized_temp_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-formal-temp-") as raw:
            root = Path(raw)
            temp_base = root / "temp"
            temp_base.mkdir()
            store = root / "livegraph-store-livegraph-0123456789ab-r02"
            store.mkdir()
            expected = temp_base / "livegraph-temp-scratch-0123456789ab-r02"
            expected.mkdir()
            output = root / "output"
            output.mkdir()
            args = SimpleNamespace(
                temp_base=temp_base,
                temp_label="scratch",
                store_label="livegraph",
            )
            request = {"execution_mode": "formal", "repeat_index": 2}
            actual = adapter_module.resolve_temp_root(args, request, store, output)
            self.assertEqual(actual, expected.resolve())

    def test_formal_receipt_set_is_mandatory_and_fixture_cannot_claim_it(self) -> None:
        empty = SimpleNamespace(
            build_receipt=None,
            build_receipt_sha256=None,
            p02b_result=None,
            p02b_result_sha256=None,
            p02b_validator=None,
            p02b_validator_sha256=None,
            p02b_max_age_seconds=None,
        )
        with self.assertRaises(adapter_module.ContractError):
            adapter_module.formal_receipts(empty, {"execution_mode": "formal"})
        claimed = SimpleNamespace(**vars(empty))
        claimed.build_receipt = Path("/tmp/not-a-formal-receipt")
        with self.assertRaises(adapter_module.ContractError):
            adapter_module.formal_receipts(claimed, {"execution_mode": "fixture"})

    def test_sigterm_is_forwarded_and_worker_pid_is_reaped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-signal-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            library = root / "liblivegraph.so"
            library.write_bytes(b"fixture runtime identity")
            worker = root / "sleeping-livegraph-worker"
            worker.write_text(
                "#!/usr/bin/python3\n"
                "import json, sys, time\n"
                f"LIB = {str(library.resolve())!r}\n"
                "if '--capabilities' in sys.argv:\n"
                "    print(json.dumps({'schema_version':'cidr-livegraph-p10-worker-v1',"
                "'process_lifetime':'fresh-import-and-query-process-lifetime-v1',"
                "'runtime_library_path':LIB}))\n"
                "    raise SystemExit(0)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            worker.chmod(0o755)
            request = self.request(store)
            request["binary"] = {"path": str(worker), "sha256": sha256_file(worker)}
            request["runtime_libraries"] = [
                {"path": str(library), "sha256": sha256_file(library)}
            ]
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
            output = root / "output"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(self.adapter),
                    "--request",
                    str(request_path),
                    "--output-dir",
                    str(output),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            start_path = output / "worker-start.json"
            deadline = time.monotonic() + 10
            while not start_path.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(start_path.is_file(), "worker did not reach the lifecycle start gate")
            start = json.loads(start_path.read_text(encoding="utf-8"))
            process.terminate()
            _, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("received signal", stderr)
            exit_receipt = json.loads(
                (output / "worker-exit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(exit_receipt["pid"], start["pid"])
            self.assertEqual(exit_receipt["proc_start_ticks"], start["proc_start_ticks"])
            self.assertFalse(
                adapter_module.same_process_alive(start["pid"], start["proc_start_ticks"])
            )
            self.assertTrue((output / "adapter-failure.json").is_file())
            self.assertEqual(list(store.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
