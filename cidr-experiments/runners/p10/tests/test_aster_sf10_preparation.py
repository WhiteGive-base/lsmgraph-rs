#!/usr/bin/env python3
"""Static and real-tiny tests for the Aster SF10 preparation wrapper."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

P10_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = P10_DIR.parents[2]
ASTER_PREP_DIR = P10_DIR / "adapters" / "aster"
sys.path.insert(0, str(ASTER_PREP_DIR))

from prepare_sf10_store import (  # noqa: E402
    CANONICAL_DENSE,
    CANONICAL_OUTPUT_PARENT,
    CANONICAL_P01_ROOT,
    CANONICAL_P02B_ROOT,
    CANONICAL_REPO,
    CANONICAL_SOURCE,
    CANONICAL_TRUTH,
    CANONICAL_WORKER,
    DENSE_SHA256,
    EXPECTED_EDGES,
    EXPECTED_QUERIES,
    EXPECTED_TOTAL_NEIGHBORS,
    EXPECTED_VERTICES,
    FINAL_WORKER_SHA256,
    P01_MANIFEST_SHA256,
    P02B_SHA256SUMS_SHA256,
    SOURCE_COMMIT,
    TRUTH_SHA256,
    PreparationError,
    PreparationSpec,
    canonical_spec,
    execute_preparation,
    prepare_output_root,
    request_document,
    sha256_file,
    validate_truth,
)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git_head(root: Path) -> str:
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def fixture_spec(root: Path, repo: Path, source: Path, binary: Path) -> PreparationSpec:
    root.mkdir()
    dataset = repo / "baseline" / "shared-truth" / "fixtures" / "dense-edges.txt"
    truth = repo / "baseline" / "shared-truth" / "fixtures" / "truth.tsv"
    p01 = root / "p01"
    p02b = root / "p02b"
    p01.mkdir()
    p02b.mkdir()

    dataset_sha = sha256_file(dataset)
    truth_sha = sha256_file(truth)
    p01_manifest = p01 / "id-map-manifest.json"
    write_json(
        p01_manifest,
        {
            "format": "seml0-shared-id-map",
            "status": "PASS",
            "formal_pass": True,
            "verification_complete": True,
            "vertex_count": 3,
            "recovery": {
                "expected_vertex_count": 3,
                "expected_edge_count": 3,
                "inputs": {
                    "dense": {
                        "path": str(dataset),
                        "sha256": dataset_sha,
                        "size_bytes": dataset.stat().st_size,
                    }
                },
                "validation": {
                    "complete_edge_count": "PASS",
                    "complete_vertex_count": "PASS",
                    "edge_type_lockstep": "PASS",
                    "endpoint_lockstep": "PASS",
                    "first_seen_dense_order": "PASS",
                    "input_sha256_during_lockstep": "PASS",
                    "input_sha256_preflight": "PASS",
                    "prefix_bijection": "PASS",
                    "verified_edge_rows": 3,
                    "recovered_vertex_count": 3,
                    "reached_dense_eof": True,
                },
            },
        },
    )
    p01_manifest_sha = sha256_file(p01_manifest)
    p01_pass = p01 / "FORMAL-PASS"
    p01_pass.write_text(f"id-map-manifest.json sha256 {p01_manifest_sha}\n", encoding="utf-8")
    p01_sums = p01 / "SHA256SUMS"
    p01_sums.write_text(f"{p01_manifest_sha}  id-map-manifest.json\n", encoding="utf-8")

    dataset_manifest = p02b / "sf10-dataset-manifest.json"
    write_json(
        dataset_manifest,
        {
            "schema_version": "p02b-dataset-manifest-v1",
            "dataset_root": str(root / "source-dataset"),
            "dataset_sha256": "a" * 64,
            "file_count": 1,
            "total_bytes": dataset.stat().st_size,
            "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
        },
    )
    plan = p02b / "sf10-shared-truth-plan.json"
    preflight = p02b / "sf10-plan-preflight.json"
    write_json(plan, {"fixture": True})
    write_json(preflight, {"fixture": True, "state": "PASS"})
    required = {
        dataset_manifest.name: sha256_file(dataset_manifest),
        plan.name: sha256_file(plan),
        preflight.name: sha256_file(preflight),
    }
    p02b_sums = p02b / "SHA256SUMS"
    p02b_sums.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(required.items())),
        encoding="utf-8",
    )
    return PreparationSpec(
        repo_root=repo,
        source_root=source,
        source_commit=git_head(source),
        binary=binary,
        binary_sha256=sha256_file(binary),
        dataset=dataset,
        dataset_sha256=dataset_sha,
        dataset_size=dataset.stat().st_size,
        truth=truth,
        truth_sha256=truth_sha,
        expected_queries=2,
        expected_total_neighbors=3,
        expected_vertices=3,
        expected_edges=3,
        p01_manifest=p01_manifest,
        p01_manifest_sha256=p01_manifest_sha,
        p01_formal_pass=p01_pass,
        p01_formal_pass_sha256=sha256_file(p01_pass),
        p01_sha256sums=p01_sums,
        p01_sha256sums_sha256=sha256_file(p01_sums),
        p02b_dataset_manifest=dataset_manifest,
        p02b_dataset_manifest_sha256=required[dataset_manifest.name],
        p02b_sha256sums=p02b_sums,
        p02b_sha256sums_sha256=sha256_file(p02b_sums),
        p02b_required_sums=required,
        p02b_dataset_tree_sha256="a" * 64,
        p02b_dataset_files=1,
        p02b_dataset_bytes=dataset.stat().st_size,
    )


class AsterSf10PreparationStaticTest(unittest.TestCase):
    def test_canonical_paths_and_hashes_are_frozen(self) -> None:
        spec = canonical_spec()
        self.assertEqual(spec.repo_root, CANONICAL_REPO)
        self.assertEqual(spec.source_root, CANONICAL_SOURCE)
        self.assertEqual(spec.binary, CANONICAL_WORKER)
        self.assertEqual(spec.dataset, CANONICAL_DENSE)
        self.assertEqual(spec.truth, CANONICAL_TRUTH)
        self.assertEqual(spec.p01_manifest.parent, CANONICAL_P01_ROOT)
        self.assertEqual(spec.p02b_sha256sums.parent, CANONICAL_P02B_ROOT)
        self.assertEqual(spec.source_commit, SOURCE_COMMIT)
        self.assertEqual(spec.binary_sha256, FINAL_WORKER_SHA256)
        self.assertEqual(spec.dataset_sha256, DENSE_SHA256)
        self.assertEqual(spec.truth_sha256, TRUTH_SHA256)
        self.assertEqual(spec.p01_manifest_sha256, P01_MANIFEST_SHA256)
        self.assertEqual(spec.p02b_sha256sums_sha256, P02B_SHA256SUMS_SHA256)
        self.assertEqual(spec.expected_queries, EXPECTED_QUERIES)
        self.assertEqual(spec.expected_total_neighbors, EXPECTED_TOTAL_NEIGHBORS)
        self.assertEqual(spec.expected_vertices, EXPECTED_VERTICES)
        self.assertEqual(spec.expected_edges, EXPECTED_EDGES)
        self.assertEqual(CANONICAL_OUTPUT_PARENT, Path("/data/WorkSpace/results/P10-ASTER-STORES"))

    def test_request_is_one_by_one_correctness_only(self) -> None:
        spec = canonical_spec()
        request = request_document(spec, "fixture-run", Path("/tmp/aster-store"), "")
        self.assertEqual(request["execution_mode"], "fixture")
        self.assertEqual(request["process_lifetime"], "fixture-process-lifetime-v1")
        self.assertEqual(request["runtime_libraries"], [])
        self.assertEqual(request["store_roots"][0]["sha256"], "")
        self.assertEqual(request["timing"]["warmup_passes"], 1)
        self.assertEqual(request["timing"]["measured_passes"], 1)
        self.assertEqual(request["truth"]["query_count"], 1700)

    def test_existing_output_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aster-prep-existing-") as raw:
            existing = Path(raw) / "attempt"
            existing.mkdir()
            evidence = existing / "prior-failure.log"
            evidence.write_text("preserve\n", encoding="utf-8")
            with self.assertRaisesRegex(PreparationError, "already exists"):
                prepare_output_root(existing, None)
            self.assertEqual(evidence.read_text(encoding="utf-8"), "preserve\n")

    def test_truth_total_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aster-prep-truth-") as raw:
            truth = Path(raw) / "truth.tsv"
            truth.write_text(
                "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n"
                "0\t1\t0\t2\t0\t0\n",
                encoding="utf-8",
            )
            spec = replace(
                canonical_spec(),
                truth=truth,
                truth_sha256=sha256_file(truth),
                expected_queries=1,
                expected_total_neighbors=3,
            )
            with self.assertRaisesRegex(PreparationError, "total-neighbor"):
                validate_truth(spec)


class AsterSf10PreparationTinyPipelineTest(unittest.TestCase):
    def test_real_tiny_fresh_freeze_reopen_pipeline(self) -> None:
        binary_raw = os.environ.get("ASTER_P10_BINARY")
        source_raw = os.environ.get("ASTER_SOURCE_ROOT")
        if not binary_raw or not source_raw:
            self.skipTest("ASTER_P10_BINARY and ASTER_SOURCE_ROOT are not set")
        binary = Path(binary_raw).resolve()
        source = Path(source_raw).resolve()
        repo = Path(os.environ.get("ASTER_PREP_REPO_ROOT", str(REPO_ROOT))).resolve()
        if not binary.is_file() or not source.is_dir() or not repo.is_dir():
            self.skipTest("Aster tiny preparation inputs are missing")
        with tempfile.TemporaryDirectory(prefix="aster-prep-real-tiny-") as raw:
            root = Path(raw)
            spec = fixture_spec(root / "lineage", repo, source, binary)
            output = root / "run"
            summary = execute_preparation(
                output,
                spec,
                require_priority=False,
                required_output_parent=None,
            )
            self.assertEqual(summary["state"], "PASS")
            self.assertFalse(summary["performance_eligible"])
            self.assertEqual(summary["formal_performance_points"], 0)
            self.assertEqual(summary["acceptance"]["checked"], 2)
            self.assertEqual(summary["acceptance"]["mismatches"], 0)
            self.assertEqual(summary["acceptance"]["total_neighbors"], 3)
            self.assertTrue((output / "DONE.correctness-only").is_file())
            self.assertFalse((output / "FAILED.json").exists())
            self.assertTrue((output / "aster-store-manifest.json").is_file())
            self.assertTrue((output / "SHA256SUMS").is_file())


if __name__ == "__main__":
    unittest.main()
