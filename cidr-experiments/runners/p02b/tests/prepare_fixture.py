#!/usr/bin/env python3
"""Create a tiny, explicitly non-formal P02B smoke fixture."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def expand_first_cpus() -> List[int]:
    raw = ""
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("Cpus_allowed_list:"):
            raw = line.split(":", 1)[1].strip()
            break
    cpus: List[int] = []
    for token in raw.split(","):
        if not token:
            continue
        if "-" in token:
            left, right = (int(value) for value in token.split("-", 1))
            cpus.extend(range(left, right + 1))
        else:
            cpus.append(int(token))
    cpus = sorted(set(cpus))
    if len(cpus) < 2:
        raise SystemExit("fixture needs two allowed CPUs")
    return cpus[:2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    dataset = root / "dataset"
    store = root / "store"
    dataset.mkdir()
    store.mkdir()
    (dataset / "fixture.tsv").write_text("src\tedge_type\tdst\n100\t7\t200\n", encoding="utf-8")
    (store / "fixture.store").write_text("immutable fixture store\n", encoding="utf-8")
    write_json(
        root / "dataset-manifest.json",
        {
            "schema_version": "p02b-dataset-manifest-v1",
            "dataset_root": str(dataset),
            "dataset_sha256": sha256(dataset / "fixture.tsv"),
            "hash_method": "fixture-single-file-sha256",
        },
    )
    write_json(
        root / "store-manifest.json",
        {
            "schema_version": "p02b-store-manifest-v1",
            "store_path": str(store),
            "store_sha256": sha256(store / "fixture.store"),
            "hash_method": "fixture-single-file-sha256",
        },
    )

    truth = root / "truth.tsv"
    truth.write_text(
        "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n"
        "0\t7\t0\t2\t10\t20\n"
        "1\t7\t1\t1\t11\t21\n"
        "2\t7\t2\t1\t12\t22\n"
        "3\t-7\t0\t1\t13\t23\n"
        "4\t-7\t1\t2\t14\t24\n"
        "5\t-7\t2\t1\t15\t25\n",
        encoding="utf-8",
    )
    id_map = root / "id-map"
    id_map.mkdir()
    dense_to_original = id_map / "dense-to-original.tsv"
    original_to_dense = id_map / "original-to-dense.tsv"
    dense_to_original.write_text(
        "dense_id\toriginal_id\n0\t100\n1\t200\n2\t300\n", encoding="utf-8"
    )
    original_to_dense.write_text(
        "original_id\tdense_id\n100\t0\n200\t1\n300\t2\n", encoding="utf-8"
    )
    id_manifest = {
        "format": "seml0-shared-id-map",
        "format_version": 1,
        "status": "PASS",
        "formal_pass": True,
        "verification_complete": True,
        "mapping_hash": "0123456789abcdef",
        "mapping_hash_algorithm": "fnv1a64-le-dense-original-v1",
        "dense_to_original": {
            "path": dense_to_original.name,
            "sha256": sha256(dense_to_original),
            "size_bytes": dense_to_original.stat().st_size,
        },
        "original_to_dense": {
            "path": original_to_dense.name,
            "sha256": sha256(original_to_dense),
            "size_bytes": original_to_dense.stat().st_size,
        },
    }
    write_json(id_map / "id-map-manifest.json", id_manifest)
    manifest_sha = sha256(id_map / "id-map-manifest.json")
    (id_map / "FORMAL-PASS").write_text(
        "id-map-manifest.json sha256 {}\n".format(manifest_sha), encoding="utf-8"
    )
    (id_map / "SHA256SUMS").write_text(
        "{}  dense-to-original.tsv\n{}  id-map-manifest.json\n{}  original-to-dense.tsv\n".format(
            sha256(dense_to_original), manifest_sha, sha256(original_to_dense)
        ),
        encoding="utf-8",
    )

    p03 = root / "P03-CLEAN-WINDOW-MONITOR"
    clean_run = p03 / "raw" / "fixture-clean"
    clean_run.mkdir(parents=True)
    monitor = p03 / "monitor_clean_window.sh"
    monitor.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    (clean_run / "classification.env").write_text(
        "performance_eligible=false\n"
        "purpose=clean_window_readiness_only\n"
        "gate_mode=seml0\n"
        "run_id=fixture-clean\n"
        "script_sha256={}\n"
        "git_head={}\n"
        "host=fixture\n"
        "ready_samples=3\n".format(sha256(monitor), "0" * 40),
        encoding="utf-8",
    )
    header = (
        "timestamp\tsample\tmetric_pass\tservice_pass\tsample_pass\tstreak\t"
        "reasons\tgate_mode\n"
    )
    rows = "".join(
        "{}\t{}\t1\t1\t1\t{}\tnone\tseml0\n".format(now, index, index)
        for index in range(1, 4)
    )
    (clean_run / "samples.tsv").write_text(header + rows, encoding="utf-8")
    (clean_run / "latest.tsv").write_text(header + rows.splitlines(True)[-1], encoding="utf-8")
    (clean_run / "STATE").write_text(
        "performance_eligible=false\nsample_pass=1\nstreak=3\nrequired_streak=3\nreasons=none\n",
        encoding="utf-8",
    )
    (clean_run / "COMPLETE").write_text("run_id=fixture-clean\n", encoding="utf-8")
    (clean_run / "READY").write_text(
        "performance_eligible=false\n"
        "readiness_gate=PASS\n"
        "gate_mode=seml0\n"
        "run_id=fixture-clean\n"
        "ready_time={}\n"
        "samples=3\n"
        "consecutive_passes=3\n"
        "latest_sample={}\n".format(now, (clean_run / "latest.tsv").resolve()),
        encoding="utf-8",
    )

    housekeeping, formal = expand_first_cpus()
    write_json(
        root / "config.json",
        {
            "schema_version": "p02b-sf10-sentinel-config-v1",
            "task_id": "P02B-SF10-SENTINEL",
            "scale": "sf10",
            "fixture_mode": True,
            "independent_runs": 3,
            "expected_queries": 6,
            "warmup_runs": 1,
            "measured_repeats": 1,
            "minimum_measured_seconds_per_run": 0.1,
            "correctness_timeout_seconds": 30,
            "repeat_timeout_seconds": 30,
            "semantic_degree_hint": True,
            "force_signature": False,
            "l0_layout": "schema",
            "io_backend": "blocking",
            "cache_policy": "no-drop-caches;independent-process;in-process-warmup;os-cache-as-is",
            "cpu": {
                "housekeeping_cpuset": str(housekeeping),
                "formal_cpuset": str(formal),
                "threads": 1,
            },
            "p31": {
                "device": "nvme1n1",
                "data_mount": "/data",
                "interval_seconds": 0.2,
                "disk_interval_seconds": 0.5,
                "min_samples": 2,
                "require_aux_tools": True,
            },
            "thresholds": {"qps_cv_max": 0.03, "p99_cv_max": 0.05},
            "clean_ready": {"max_age_seconds": 300, "minimum_consecutive_samples": 3},
        },
    )
    write_json(
        root / "paths.json",
        {
            "clean_ready": str(clean_run / "READY"),
            "config": str(root / "config.json"),
            "dataset_manifest": str(root / "dataset-manifest.json"),
            "id_map": str(id_map),
            "query_plan": str(root / "query-plan.json"),
            "store": str(store),
            "store_manifest": str(root / "store-manifest.json"),
            "truth": str(truth),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
