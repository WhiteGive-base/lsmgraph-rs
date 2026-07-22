#!/usr/bin/env python3
"""No-binary unit tests for exact LiveGraph/P31 cross-binding."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))
from p10_contract import ContractError, validate_livegraph_p31_binding  # noqa: E402


def ref(name: str, char: str) -> dict[str, object]:
    return {"path": f"/tmp/livegraph-binding/{name}", "sha256": char * 64, "size_bytes": 1}


def fixture() -> tuple[dict, dict]:
    binary, dataset, truth, library = ref("binary", "1"), ref("dense", "2"), ref("truth", "3"), ref("lib", "4")
    receipt, marker, source = ref("build-receipt", "5"), ref("DONE.build", "6"), ref("source-lib", "7")
    p02b_result, validator = ref("sentinel", "8"), ref("validator", "9")
    pass_marker, p02b_provenance = ref("PASS", "a"), ref("p02b-provenance", "b")
    seal = {**ref("dataset-seal", "c"), "content": {}}
    pid, start_ticks = 4321, 987654
    provenance = {
        "schema_version": "p10-livegraph-adapter-provenance-v2",
        "artifacts": {"binary": binary, "dataset": dataset, "truth": truth, "runtime_library": library},
        "store": {"root": "/tmp/livegraph-binding/store"},
        "temp": {"root": "/tmp/livegraph-binding/temp"},
        "formal_build": {
            "receipt": receipt, "marker": marker, "source_library": source,
            "liblivegraph": library,
            "integration": {"root": "/repo", "head": "d" * 40, "dirty": False, "status_lines": []},
        },
        "p02b_admission": {
            "result": p02b_result, "validator": validator,
            "admission": {
                "pass_marker": pass_marker["path"], "pass_marker_sha256": pass_marker["sha256"],
                "provenance": p02b_provenance["path"], "provenance_sha256": p02b_provenance["sha256"],
                "host": {"hostname": "host", "fingerprint_sha256": "e" * 64},
            },
        },
        "dataset_seal": seal,
        "worker_lifecycle": {
            "identity": {
                "pid": pid,
                "proc_start_ticks": start_ticks,
                "process_group_id": pid,
                "same_process_alive_after_wait": False,
                "process_group_members_after_wait": [],
            }
        },
    }
    inputs = {
        "binary": binary,
        "dataset": {**dataset, "content_sha256_mode": "declared-no-read-v1"},
        "truth": truth, "query_or_trace": truth,
        "livegraph_build_receipt": receipt, "livegraph_build_marker": marker,
        "livegraph_source_library": source, "livegraph_runtime_library": library,
        "livegraph_p02b_result": p02b_result, "livegraph_p02b_validator": validator,
        "livegraph_p02b_pass_marker": pass_marker, "livegraph_p02b_provenance": p02b_provenance,
        "livegraph_dataset_seal": seal,
    }
    p31 = {
        "inputs": inputs,
        "disk_roots": [
            {"role": "store", "path": provenance["store"]["root"]},
            {"role": "temp", "path": provenance["temp"]["root"]},
        ],
        "collector": {"containers": [], "extra_pids": []},
        "collector_result": {
            "process_identity_schema_version": "cidr-process-identity-v1",
            "process_identity_unique_set": [
                {"pid": pid, "start_ticks": start_ticks, "pgrp": pid,
                 "ppid": 4000, "first_sample_index": 1, "last_sample_index": 2, "sample_count": 2}
            ],
        },
        "repo": {"git_sha": "d" * 40, "dirty": False},
        "host": {"hostname": "host", "fingerprint_sha256": "e" * 64},
    }
    return provenance, p31


class LiveGraphP31BindingTests(unittest.TestCase):
    def test_accepts_exact_worker_receipt_and_all_gate_inputs(self) -> None:
        provenance, p31 = fixture()
        validate_livegraph_p31_binding(provenance, p31)

    def test_rejects_worker_start_ticks_temp_and_p02b_drift(self) -> None:
        for mutation in ("start_ticks", "temp", "p02b", "process_group"):
            provenance, p31 = fixture()
            if mutation == "start_ticks":
                p31["collector_result"]["process_identity_unique_set"][0]["start_ticks"] += 1
            elif mutation == "temp":
                p31["disk_roots"][1]["path"] += "-other"
            else:
                if mutation == "p02b":
                    p31["inputs"]["livegraph_p02b_result"] = {
                        **p31["inputs"]["livegraph_p02b_result"],
                        "sha256": "0" * 64,
                    }
                else:
                    provenance["worker_lifecycle"]["identity"]["process_group_id"] += 1
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                validate_livegraph_p31_binding(provenance, p31)


if __name__ == "__main__":
    unittest.main()
