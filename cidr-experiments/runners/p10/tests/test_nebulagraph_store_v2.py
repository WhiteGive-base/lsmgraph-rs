#!/usr/bin/env python3
"""Pure tempdir/mock gates for the NebulaGraph offline store-v2 chain."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

P10_DIR = Path(__file__).resolve().parents[1]
NEBULA_DIR = P10_DIR / "adapters" / "nebulagraph"
sys.path.insert(0, str(P10_DIR))
sys.path.insert(0, str(NEBULA_DIR))

import clone_store  # noqa: E402
import build_import_receipt as import_freezer  # noqa: E402
import build_store_manifest as freezer  # noqa: E402
import nebula_adapter as adapter  # noqa: E402
import store_contract as contract  # noqa: E402
from p10_contract import ContractError  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class NebulaGraphStoreV2Test(unittest.TestCase):
    maxDiff = None

    def _repo(self, root: Path) -> dict[str, object]:
        return {
            "root": str(root.resolve()),
            "head": "a" * 40,
            "clean": True,
            "status_sha256": contract.hashlib.sha256(b"").hexdigest(),
        }

    def _images(self) -> list[dict[str, str]]:
        result = []
        for index, role in enumerate(("graphd", "metad", "storaged"), start=1):
            expected = contract.EXPECTED_IMAGES[role]
            result.append(
                {
                    "role": role,
                    "tag": expected["tag"],
                    "repo_digest": f"{expected['tag'].split(':', 1)[0]}@{expected['digest']}",
                    "image_id": "sha256:" + str(index) * 64,
                }
            )
        return result

    def _strict_import(
        self, root: Path
    ) -> tuple[dict[str, object], Path, Path, Path, dict[str, object], list[dict[str, str]]]:
        dataset = root / "dense.txt"
        dataset.write_text("1\n0 1 0\n", encoding="utf-8")
        truth = root / "truth.tsv"
        truth.write_text(
            "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n"
            "0\t1\t0\t1\t0\t0\n",
            encoding="utf-8",
        )
        raw = root / "raw-dataset.json"
        write_json(raw, {"schema_version": "p02b-dataset-manifest-v1"})
        importer = root / "importer.py"
        importer.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        importer.chmod(0o755)
        runtime = root / "runtime.json"
        stdout = root / "import.stdout"
        stderr = root / "import.stderr"
        result = root / "import-result.json"
        correctness = root / "correctness.json"
        report = root / "importer-report.json"
        observations = root / "correctness.tsv"
        argv_json = root / "argv.json"
        stdout.write_text("ok\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        images = self._images()
        write_json(runtime, {"runtime": "pinned"})
        dataset_sha = contract.file_ref(dataset)["sha256"]
        truth_sha = contract.file_ref(truth)["sha256"]
        source = root / "imported-store"
        source.mkdir()
        (source / "meta").mkdir()
        (source / "meta" / "part").write_text("store\n", encoding="utf-8")
        raw_ref = contract.file_ref(raw)
        dataset_ref = contract.file_ref(dataset)
        truth_ref = contract.file_ref(truth)
        runtime_ref = contract.file_ref(runtime)
        wrapper_ref = contract.file_ref(Path(import_freezer.__file__))
        importer_ref = contract.file_ref(importer)
        argv = [
            str(importer.resolve()),
            "--raw-dataset-manifest", str(raw.resolve()),
            "--dense-dataset", str(dataset.resolve()),
            "--truth", str(truth.resolve()),
            "--runtime-manifest", str(runtime.resolve()),
            "--store-root", str(source.resolve()),
            "--importer-report", str(report.resolve()),
            "--correctness-observations", str(observations.resolve()),
        ]
        write_json(argv_json, argv)
        loaded = {
            "vertex_count": contract.FORMAL_VERTEX_COUNT,
            "edge_count": contract.FORMAL_EDGE_COUNT,
            "edge_type_count": 34,
        }
        write_json(
            report,
            {
                "schema_version": contract.IMPORTER_REPORT_SCHEMA,
                "store_root": str(source.resolve()),
                "raw_dataset_manifest": raw_ref,
                "dense_dataset": dataset_ref,
                "truth": truth_ref,
                "runtime_manifest": runtime_ref,
                "loaded": loaded,
            },
        )
        observations.write_text(
            "query_index\tedge_type\tsrc\texpected_count\tactual_count\t"
            "expected_sum_hash\tactual_sum_hash\texpected_xor_hash\tactual_xor_hash\tstatus\n"
            "0\t1\t0\t1\t1\t0\t0\t0\t0\tok\n",
            encoding="utf-8",
        )
        producer = {"path": wrapper_ref["path"], "sha256": wrapper_ref["sha256"]}
        inputs = {
            "raw_dataset_manifest": raw_ref,
            "dense_dataset": dataset_ref,
            "truth": truth_ref,
            "runtime_manifest": runtime_ref,
        }
        write_json(
            result,
            {
                "schema_version": contract.IMPORT_EXECUTION_SCHEMA,
                "state": "PASS",
                "producer": producer,
                "started_at_utc": "2026-07-22T00:00:00Z",
                "completed_at_utc": "2026-07-22T01:00:00Z",
                "exit_code": 0,
                "timed_out": False,
                "cwd": str(root.resolve()),
                "importer": importer_ref,
                "argv": argv,
                "argv_sha256": contract.canonical_json_sha(argv),
                "argv_source": contract.file_ref(argv_json),
                "target_store": str(source.resolve()),
                "inputs": inputs,
                "stdout": contract.file_ref(stdout),
                "stderr": contract.file_ref(stderr),
                "report": contract.file_ref(report),
                "loaded": loaded,
                "images": images,
            },
        )
        write_json(
            correctness,
            {
                "schema_version": contract.IMPORT_CORRECTNESS_SCHEMA,
                "state": "PASS",
                "producer": producer,
                "importer": importer_ref,
                "argv_sha256": contract.canonical_json_sha(argv),
                "target_store": str(source.resolve()),
                "query_count": 1,
                "mismatch_count": 0,
                "timeout_count": 0,
                "truth": truth_ref,
                "dataset": dataset_ref,
                "observations": contract.file_ref(observations),
            },
        )
        repo = self._repo(root)
        value: dict[str, object] = {
            "schema_version": contract.IMPORT_RECEIPT_SCHEMA,
            "state": "PASS",
            "formal_eligible": True,
            "historical_tag_only": False,
            "system_version": contract.SYSTEM_VERSION,
            "completed_at_utc": "2026-07-22T01:00:00Z",
            "repo": repo,
            "wrapper": {**wrapper_ref, "policy": "execute-and-derive-evidence-v1"},
            "importer": {
                **importer_ref,
                "cwd": str(root.resolve()),
                "argv": argv,
                "argv_sha256": contract.canonical_json_sha(argv),
                "argv_source": contract.file_ref(argv_json),
            },
            "inputs": inputs,
            "images": images,
            "execution": {
                "started_at_utc": "2026-07-22T00:00:00Z",
                "completed_at_utc": "2026-07-22T01:00:00Z",
                "exit_code": 0,
                "timed_out": False,
                "stdout": contract.file_ref(stdout),
                "stderr": contract.file_ref(stderr),
                "report": contract.file_ref(report),
                "result": contract.file_ref(result),
            },
            "loaded": loaded,
            "correctness": {
                "query_count": 1,
                "mismatch_count": 0,
                "timeout_count": 0,
                "observations": contract.file_ref(observations),
                "result": contract.file_ref(correctness),
            },
            "store": {"path": str(source.resolve()), **contract.tree_identity(source)},
        }
        receipt = root / "import-receipt.json"
        write_json(receipt, value)
        return value, receipt, dataset, truth, repo, images

    def _validate_import(
        self,
        value: dict[str, object],
        receipt: Path,
        dataset: Path,
        truth: Path,
        repo: dict[str, object],
        images: list[dict[str, str]],
        raw_reference: dict[str, str] | None = None,
        runtime_reference: dict[str, str] | None = None,
    ) -> tuple[Path, dict[str, object]]:
        write_json(receipt, value)
        reference = contract.file_ref(receipt)
        if raw_reference is None:
            raw_reference = value["inputs"]["raw_dataset_manifest"]
        if runtime_reference is None:
            runtime_reference = value["inputs"]["runtime_manifest"]
        dataset_sha = contract.file_ref(dataset)["sha256"]
        truth_sha = contract.file_ref(truth)["sha256"]
        with mock.patch.object(contract, "FORMAL_DATASET_SHA256", dataset_sha):
            with mock.patch.object(contract, "FORMAL_TRUTH_SHA256", truth_sha):
                with mock.patch.object(contract, "FORMAL_QUERY_COUNT", 1):
                    return contract.validate_import_receipt(
                        reference,
                        repo=repo,
                        raw_dataset_manifest_path=Path(raw_reference["path"]),
                        raw_dataset_manifest_sha256=raw_reference["sha256"],
                        dataset_path=dataset,
                        dataset_sha256=dataset_sha,
                        truth_path=truth,
                        truth_sha256=truth_sha,
                        runtime_manifest_path=Path(runtime_reference["path"]),
                        runtime_manifest_sha256=runtime_reference["sha256"],
                        runtime_images=images,
                        recompute_artifacts=True,
                    )

    def test_strict_import_receipt_accepts_only_pinned_nonhistorical_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-import-v2-") as raw:
            root = Path(raw)
            value, receipt, dataset, truth, repo, images = self._strict_import(root)
            _, parsed = self._validate_import(value, receipt, dataset, truth, repo, images)
            self.assertEqual(parsed["schema_version"], contract.IMPORT_RECEIPT_SCHEMA)
            value["historical_tag_only"] = True
            with self.assertRaisesRegex(ContractError, "tag-only.*never"):
                self._validate_import(value, receipt, dataset, truth, repo, images)

    def test_generic_historical_source_result_cannot_be_upgraded_to_formal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-historical-v2-") as raw:
            root = Path(raw)
            value, receipt, dataset, truth, repo, images = self._strict_import(root)
            raw_reference = value["inputs"]["raw_dataset_manifest"]
            runtime_reference = value["inputs"]["runtime_manifest"]
            write_json(
                receipt,
                {
                    "schema_version": "historical-sf10-store-copy-v1",
                    "source": contract.file_ref(dataset),
                    "result": contract.file_ref(truth),
                    "formal_eligible": True,
                },
            )
            with self.assertRaisesRegex(ContractError, "key drift|strict"):
                self._validate_import(value={
                    "schema_version": "historical-sf10-store-copy-v1",
                    "source": contract.file_ref(dataset),
                    "result": contract.file_ref(truth),
                    "formal_eligible": True,
                }, receipt=receipt, dataset=dataset, truth=truth, repo=repo, images=images,
                    raw_reference=raw_reference, runtime_reference=runtime_reference)

    def test_import_dense_sha_and_image_role_order_are_bound(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-import-drift-v2-") as raw:
            root = Path(raw)
            value, receipt, dataset, truth, repo, images = self._strict_import(root)
            value["inputs"]["dense_dataset"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ContractError, "lineage mismatch"):
                self._validate_import(value, receipt, dataset, truth, repo, images)
            second = root / "second"
            second.mkdir()
            value, receipt, dataset, truth, repo, images = self._strict_import(second)
            value["images"][0], value["images"][1] = value["images"][1], value["images"][0]
            with self.assertRaisesRegex(ContractError, "role/order drift"):
                self._validate_import(value, receipt, dataset, truth, repo, images)

    def test_formal_logical_hosts_are_frozen_and_separate_from_container_names(self) -> None:
        containers = {"metad": "run-r1-meta", "storaged": "run-r1-storage", "graphd": "run-r1-graph"}
        hosts = dict(contract.LOGICAL_HOSTS)
        parsed_containers, parsed_hosts, _ = contract.validate_identity_names(
            containers, hosts, "run-r1-net", formal=True
        )
        self.assertEqual(parsed_containers, containers)
        self.assertEqual(parsed_hosts, hosts)
        collided = dict(hosts)
        collided["metad"] = containers["metad"]
        with self.assertRaisesRegex(ContractError, "distinct|historical"):
            contract.validate_identity_names(containers, collided, "run-r1-net", formal=True)
        drifted = dict(hosts)
        drifted["metad"] = "different-meta"
        with self.assertRaisesRegex(ContractError, "historical RAFT"):
            contract.validate_identity_names(containers, drifted, "run-r1-net", formal=True)

    def test_tree_v2_accepts_internal_relative_symlink_and_rejects_escape_or_dangling(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-tree-v2-") as raw:
            root = Path(raw) / "store"
            root.mkdir()
            (root / "data").mkdir()
            (root / "data" / "part").write_text("x\n", encoding="utf-8")
            os.symlink("data/part", root / "safe-link")
            identity = contract.tree_identity(root)
            self.assertEqual(identity["symlink_count"], 1)
            (root / "safe-link").unlink()
            outside = Path(raw) / "outside"
            outside.write_text("outside\n", encoding="utf-8")
            os.symlink("../outside", root / "escape")
            with self.assertRaisesRegex(ContractError, "escapes"):
                contract.tree_identity(root)
            (root / "escape").unlink()
            os.symlink("missing", root / "dangling")
            with self.assertRaisesRegex(ContractError, "dangling"):
                contract.tree_identity(root)

    def test_tree_v2_detects_post_hash_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-tree-race-v2-") as raw:
            root = Path(raw)
            payload = root / "part"
            payload.write_text("before\n", encoding="utf-8")
            original = contract.tree_metadata
            invoked = False

            def mutate_then_scan(path: Path):
                nonlocal invoked
                if not invoked:
                    invoked = True
                    payload.write_text("after-with-different-size\n", encoding="utf-8")
                return original(path)

            with mock.patch.object(contract, "tree_metadata", side_effect=mutate_then_scan):
                with self.assertRaisesRegex(ContractError, "changed after content hashing"):
                    contract.tree_identity(root)

    def test_offline_gate_rejects_parent_mount_and_accepts_unrelated_mount(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-offline-v2-") as raw:
            root = Path(raw) / "stores" / "run-r1"
            root.mkdir(parents=True)
            listed = subprocess.CompletedProcess([], 0, "abc\n", "")

            def runner_for(source: Path):
                inspected = subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps([{"Name": "/busy", "Mounts": [{"Type": "volume", "Source": str(source)}]}]),
                    "",
                )
                return mock.Mock(side_effect=[listed, inspected])

            with self.assertRaisesRegex(ContractError, "overlaps"):
                contract.assert_offline((root,), docker="docker", runner=runner_for(root.parent))
            contract.assert_offline(
                (root,), docker="docker", runner=runner_for(Path(raw) / "unrelated")
            )

    def test_offline_gate_fails_closed_on_malformed_inspect(self) -> None:
        listed = subprocess.CompletedProcess([], 0, "abc\n", "")
        malformed = subprocess.CompletedProcess([], 0, "not-json", "")
        runner = mock.Mock(side_effect=[listed, malformed])
        with self.assertRaisesRegex(ContractError, "malformed JSON"):
            contract.assert_offline((Path("/data/store"),), docker="docker", runner=runner)

    def test_exclusive_json_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-output-v2-") as raw:
            output = Path(raw) / "receipt.json"
            contract.write_json_exclusive(output, {"state": "PASS"})
            original = output.read_bytes()
            with self.assertRaisesRegex(ContractError, "overwrite"):
                contract.write_json_exclusive(output, {"state": "DIFFERENT"})
            self.assertEqual(output.read_bytes(), original)

    def test_fixture_freezer_emits_v2_and_adapter_accepts_it_without_docker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-fixture-freezer-v2-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            dataset = root / "dense.txt"
            dataset.write_text("1\n0 1 0\n", encoding="utf-8")
            truth = root / "truth.tsv"
            truth.write_text(
                "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n"
                "0\t1\t0\t1\t0\t0\n",
                encoding="utf-8",
            )
            output = root / "store.json"
            arguments = argparse.Namespace(
                mode="fixture",
                data_root=store,
                dataset=dataset,
                truth=truth,
                space="fixture_space",
                container_prefix="fixture-r1",
                run_id=None,
                repeat_index=None,
                graph_host="127.0.0.1",
                graph_port=0,
                user="root",
                password_env="CIDR_NEBULA_PASSWORD",
                password_sha256="d" * 64,
                repo_root=None,
                docker=Path("/usr/bin/docker"),
                runtime_manifest=None,
                runtime_manifest_sha256=None,
                raw_dataset_manifest=None,
                raw_dataset_manifest_sha256=None,
                import_receipt=None,
                import_receipt_sha256=None,
                clone_receipt=None,
                clone_receipt_sha256=None,
                output=output,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                freezer.run(arguments)
            value = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(value["schema_version"], contract.STORE_SCHEMA)
            self.assertFalse(value["formal_eligible"])
            self.assertTrue(set(value["containers"].values()).isdisjoint(value["logical_hosts"].values()))
            request = {
                "system_version": contract.SYSTEM_VERSION,
                "execution_mode": "fixture",
                "store_roots": [{"label": "nebulagraph", "path": str(store), "sha256": ""}],
                "dataset": contract.file_ref(dataset),
                "truth": {**contract.file_ref(truth), "query_count": 1},
                "external_service": {
                    "containers": [
                        value["containers"][role] for role in ("graphd", "metad", "storaged")
                    ]
                },
            }
            parsed, labels = adapter.validate_store_manifest(
                output,
                request,
                dataset,
                truth,
                store,
                root / "unused-runtime.json",
                "0" * 64,
                {"images": self._images()},
                None,
                None,
            )
            self.assertEqual(parsed["logical_hosts"], value["logical_hosts"])
            self.assertEqual(labels, {1: "E_P1"})

    def test_import_wrapper_executes_pinned_argv_and_derives_typed_results(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-import-builder-v2-") as raw:
            root = Path(raw)
            repo_root = root / "repo"
            repo_root.mkdir()
            store = root / "imported-store"
            importer = repo_root / "importer.py"
            importer.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            importer.chmod(0o755)
            docker = root / "docker"
            docker.write_text("#!/bin/sh\n", encoding="utf-8")
            docker.chmod(0o755)
            dataset = root / "dense.txt"
            truth = root / "truth.tsv"
            raw_manifest = root / "raw.json"
            dataset.write_text("dense\n", encoding="utf-8")
            truth.write_text(
                "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n"
                "0\t1\t0\t1\t0\t0\n",
                encoding="utf-8",
            )
            write_json(raw_manifest, {"schema_version": "p02b-dataset-manifest-v1"})
            dataset_sha = contract.file_ref(dataset)["sha256"]
            truth_sha = contract.file_ref(truth)["sha256"]
            images = self._images()
            runtime = root / "runtime.json"
            write_json(
                runtime,
                {
                    "schema_version": "cidr-p10-nebulagraph-runtime-v1",
                    "system_version": contract.SYSTEM_VERSION,
                    "client": {},
                    "docker": {},
                    "images": images,
                },
            )
            report = root / "importer-report.json"
            observations = root / "correctness-observations.tsv"
            argv = [
                str(importer.resolve()),
                "--raw-dataset-manifest", str(raw_manifest.resolve()),
                "--dense-dataset", str(dataset.resolve()),
                "--truth", str(truth.resolve()),
                "--runtime-manifest", str(runtime.resolve()),
                "--store-root", str(store.resolve()),
                "--importer-report", str(report.resolve()),
                "--correctness-observations", str(observations.resolve()),
            ]
            argv_json = root / "argv.json"
            argv_json.write_text(json.dumps(argv) + "\n", encoding="utf-8")
            stdout = root / "stdout"
            stderr = root / "stderr"
            execution = root / "execution.json"
            correctness = root / "correctness.json"
            output = root / "import-receipt.json"
            arguments = argparse.Namespace(
                repo_root=repo_root,
                docker=docker,
                store_root=store,
                importer=importer,
                importer_sha256=contract.file_ref(importer)["sha256"],
                argv_json=argv_json,
                raw_dataset_manifest=raw_manifest,
                dense_dataset=dataset,
                truth=truth,
                runtime_manifest=runtime,
                runtime_manifest_sha256=contract.file_ref(runtime)["sha256"],
                stdout=stdout,
                stderr=stderr,
                importer_report=report,
                correctness_observations=observations,
                execution_result=execution,
                correctness_result=correctness,
                timeout_seconds=60,
                output=output,
            )
            repo = self._repo(repo_root)

            # A handwritten PASS artifact can never be consumed as an input.
            write_json(execution, {"state": "PASS"})
            with self.assertRaisesRegex(ContractError, "overwrite import execution result"):
                import_freezer.run(arguments)
            execution.unlink()

            bad_argv = argv[:]
            truth_flag = bad_argv.index("--truth")
            del bad_argv[truth_flag : truth_flag + 2]
            write_json(argv_json, bad_argv)
            with mock.patch.object(import_freezer, "FORMAL_DATASET_SHA256", dataset_sha), mock.patch.object(
                import_freezer, "FORMAL_TRUTH_SHA256", truth_sha
            ), mock.patch.object(import_freezer, "current_git_state", return_value=repo), mock.patch.object(
                import_freezer.subprocess, "run"
            ) as forbidden_execution:
                with self.assertRaisesRegex(ContractError, "requires one --truth"):
                    import_freezer.run(arguments)
                forbidden_execution.assert_not_called()
            write_json(argv_json, argv)

            def execute(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertEqual(command, argv)
                store.mkdir()
                (store / "part").write_text("store\n", encoding="utf-8")
                write_json(
                    report,
                    {
                        "schema_version": contract.IMPORTER_REPORT_SCHEMA,
                        "store_root": str(store.resolve()),
                        "raw_dataset_manifest": contract.file_ref(raw_manifest),
                        "dense_dataset": contract.file_ref(dataset),
                        "truth": contract.file_ref(truth),
                        "runtime_manifest": contract.file_ref(runtime),
                        "loaded": {
                            "vertex_count": contract.FORMAL_VERTEX_COUNT,
                            "edge_count": contract.FORMAL_EDGE_COUNT,
                            "edge_type_count": 34,
                        },
                    },
                )
                observations.write_text(
                    "\t".join(import_freezer.OBSERVATION_COLUMNS) + "\n"
                    "0\t1\t0\t1\t1\t0\t0\t0\t0\tok\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(import_freezer, "FORMAL_DATASET_SHA256", dataset_sha):
                with mock.patch.object(import_freezer, "FORMAL_TRUTH_SHA256", truth_sha):
                    with mock.patch.object(import_freezer, "FORMAL_QUERY_COUNT", 1):
                        with mock.patch.object(import_freezer, "current_git_state", return_value=repo):
                            with mock.patch.object(import_freezer, "assert_offline") as offline:
                                with mock.patch.object(import_freezer.subprocess, "run", side_effect=execute) as invoked:
                                    with contextlib.redirect_stdout(io.StringIO()):
                                        import_freezer.run(arguments)
            value = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(value["schema_version"], contract.IMPORT_RECEIPT_SCHEMA)
            self.assertFalse(value["historical_tag_only"])
            self.assertEqual(value["wrapper"]["policy"], "execute-and-derive-evidence-v1")
            self.assertEqual(invoked.call_count, 1)
            self.assertEqual(invoked.call_args.kwargs["cwd"], str(repo_root.resolve()))
            self.assertEqual(invoked.call_args.kwargs["timeout"], 60)
            self.assertEqual(json.loads(execution.read_text(encoding="utf-8"))["state"], "PASS")
            self.assertEqual(json.loads(correctness.read_text(encoding="utf-8"))["state"], "PASS")
            self.assertEqual(value["store"]["sha256"], contract.tree_identity(store)["sha256"])
            self.assertEqual(offline.call_count, 2)

            failure_dir = root / "failure"
            failure_dir.mkdir()
            failure_store = failure_dir / "store"
            failure_report = failure_dir / "report.json"
            failure_observations = failure_dir / "observations.tsv"
            failure_argv = [
                str(importer.resolve()),
                "--raw-dataset-manifest", str(raw_manifest.resolve()),
                "--dense-dataset", str(dataset.resolve()),
                "--truth", str(truth.resolve()),
                "--runtime-manifest", str(runtime.resolve()),
                "--store-root", str(failure_store.resolve()),
                "--importer-report", str(failure_report.resolve()),
                "--correctness-observations", str(failure_observations.resolve()),
            ]
            failure_argv_json = failure_dir / "argv.json"
            write_json(failure_argv_json, failure_argv)
            failure_args = argparse.Namespace(
                **{
                    **vars(arguments),
                    "store_root": failure_store,
                    "argv_json": failure_argv_json,
                    "stdout": failure_dir / "stdout",
                    "stderr": failure_dir / "stderr",
                    "importer_report": failure_report,
                    "correctness_observations": failure_observations,
                    "execution_result": failure_dir / "execution.json",
                    "correctness_result": failure_dir / "correctness.json",
                    "output": failure_dir / "receipt.json",
                }
            )
            with mock.patch.object(import_freezer, "FORMAL_DATASET_SHA256", dataset_sha), mock.patch.object(
                import_freezer, "FORMAL_TRUTH_SHA256", truth_sha
            ), mock.patch.object(import_freezer, "current_git_state", return_value=repo), mock.patch.object(
                import_freezer.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(failure_argv, 9),
            ):
                with self.assertRaisesRegex(ContractError, "exited 9"):
                    import_freezer.run(failure_args)
            failure_execution = json.loads(
                failure_args.execution_result.read_text(encoding="utf-8")
            )
            self.assertEqual(failure_execution["state"], "FAIL")
            self.assertEqual(failure_execution["exit_code"], 9)
            self.assertFalse(failure_args.output.exists())
            self.assertFalse(failure_args.correctness_result.exists())

    def test_store_v1_shape_is_rejected_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-store-v1-reject-") as raw:
            root = Path(raw)
            manifest = root / "store-v1.json"
            write_json(
                manifest,
                {
                    "schema_version": "cidr-p10-nebulagraph-store-v1",
                    "system_version": contract.SYSTEM_VERSION,
                    "formal_eligible": True,
                    "import_provenance": {
                        "kind": "historical-sf10-store-copy-v1",
                        "source": None,
                        "result": None,
                    },
                },
            )
            with self.assertRaisesRegex(ContractError, "key drift|schema"):
                adapter.validate_store_manifest(
                    manifest,
                    {"execution_mode": "formal", "system_version": contract.SYSTEM_VERSION},
                    root / "missing-dataset",
                    root / "missing-truth",
                    root / "missing-store",
                    root / "missing-runtime",
                    "0" * 64,
                    {"images": self._images()},
                    self._repo(root),
                    {},
                )

    def test_clone_rejects_existing_or_overlapping_target_before_external_calls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-clone-target-v2-") as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            existing = root / "existing"
            existing.mkdir()
            output = root / "clone.json"
            base = {
                "source": source,
                "target": existing,
                "run_id": "run-r1",
                "repeat_index": 1,
                "repo_root": root,
                "docker": Path("/usr/bin/docker"),
                "copy_tool": Path("/usr/bin/cp"),
                "copy_mode": "reflink",
                "output": output,
            }
            with self.assertRaisesRegex(ContractError, "already exists"):
                clone_store.run(argparse.Namespace(**base))
            base["target"] = source / "nested"
            with self.assertRaisesRegex(ContractError, "overlap"):
                clone_store.run(argparse.Namespace(**base))

    def test_clone_inode_gate_rejects_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-clone-inode-v2-") as raw:
            root = Path(raw)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            original = source / "part"
            original.write_text("x\n", encoding="utf-8")
            os.link(original, target / "part")
            records = {"part": {"type": "file", "size_bytes": 2, "sha256": "0" * 64}}
            with self.assertRaisesRegex(ContractError, "shares.*inode"):
                clone_store._reject_shared_inodes(source, target, records, records)

    def test_clone_receipt_strictly_binds_source_target_repo_and_copy_argv(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-clone-receipt-v2-") as raw:
            root = Path(raw)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            (source / "part").write_text("same\n", encoding="utf-8")
            (target / "part").write_text("same\n", encoding="utf-8")
            source_tree = {"path": str(source.resolve()), **contract.tree_identity(source)}
            target_tree = {"path": str(target.resolve()), **contract.tree_identity(target)}
            copy_tool = root / "cp"
            copy_tool.write_text("#!/bin/sh\n", encoding="utf-8")
            copy_tool.chmod(0o755)
            staging = target.parent / f".{target.name}.clone-{'b' * 32}"
            argv = [
                str(copy_tool.resolve()),
                "-a",
                "--reflink=always",
                "--no-target-directory",
                "--",
                str(source.resolve()),
                str(staging),
            ]
            repo = self._repo(root)
            value = {
                "schema_version": contract.CLONE_RECEIPT_SCHEMA,
                "state": "PASS",
                "created_at_utc": "2026-07-22T02:00:00Z",
                "run_id": "run-r1",
                "repeat_index": 1,
                "repo": repo,
                "method": contract.CLONE_METHOD_REFLINK,
                "copy_tool": {
                    "path": str(copy_tool.resolve()),
                    "sha256": contract.file_ref(copy_tool)["sha256"],
                    "argv": argv,
                    "argv_sha256": contract.canonical_json_sha(argv),
                    "exit_code": 0,
                },
                "source_pre": source_tree,
                "source_post": dict(source_tree),
                "target": target_tree,
                "safety": {
                    "target_absent_before": True,
                    "source_target_nonoverlap": True,
                    "offline_check_count": 4,
                    "no_shared_regular_inodes": True,
                    "symlink_policy": contract.SYMLINK_POLICY,
                    "publish_noreplace": True,
                },
            }
            receipt = root / "clone-receipt.json"
            write_json(receipt, value)
            _, parsed = contract.validate_clone_receipt(
                contract.file_ref(receipt),
                repo=repo,
                imported_tree=source_tree,
                target_path=target,
                target_tree=target_tree,
                run_id="run-r1",
                repeat_index=1,
            )
            self.assertEqual(parsed["method"], contract.CLONE_METHOD_REFLINK)
            value["source_post"]["sha256"] = "0" * 64
            write_json(receipt, value)
            with self.assertRaisesRegex(ContractError, "source pre/post.*sha256"):
                contract.validate_clone_receipt(
                    contract.file_ref(receipt),
                    repo=repo,
                    imported_tree=source_tree,
                    target_path=target,
                    target_tree=target_tree,
                    run_id="run-r1",
                    repeat_index=1,
                )


if __name__ == "__main__":
    unittest.main()
