#!/usr/bin/env python3
"""Run Aster/RocksGraph typed-neighbor baseline for the SemL0 3+3 package."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


ASTER_COMMIT = "6abb258e577c479325092a8ac0e7691fdfd154c2"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def package_dir(root: Path) -> Path:
    return root / "baseline" / "external-baselines-20260626" / "3plus3-baselines"


def dense_path(root: Path, scale: str) -> Path:
    if scale == "sf1":
        return root / "baseline" / "external-baselines-20260624" / "livegraph" / "sf1-smoke" / "edges-dense.txt"
    if scale == "sf10":
        return root / "baseline" / "external-baselines-20260624" / "livegraph" / "sf10-typed-neighbor" / "edges-dense.txt"
    raise ValueError(scale)


def truth_path(root: Path, scale: str) -> Path:
    run = "sf1-smoke" if scale == "sf1" else "sf10-main"
    return (
        package_dir(root)
        / "systems"
        / "neo4j"
        / run
        / "workload"
        / "truth-s50-seed42.tsv"
    )


def run_label(scale: str) -> str:
    return "sf1-smoke-r1" if scale == "sf1" else "sf10-main-r1"


def call(cmd: list[str], log_path: Path, cwd: Path | None = None, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with log_path.open("a", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
        )
        log.write(proc.stdout)
        log.write(f"\n[exit={proc.returncode} elapsed_s={time.time() - started:.3f}]\n")
    if proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}; see {log_path}")
    return proc


def call_allow_fail(cmd: list[str], log_path: Path, cwd: Path | None = None, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with log_path.open("a", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
            )
            log.write(proc.stdout)
            log.write(f"\n[exit={proc.returncode} elapsed_s={time.time() - started:.3f}]\n")
            return proc
        except subprocess.TimeoutExpired as exc:
            log.write((exc.stdout or "") if isinstance(exc.stdout, str) else "")
            log.write(f"\n[TIMEOUT elapsed_s={time.time() - started:.3f} timeout_s={timeout_s}]\n")
            raise


def ensure_aster(root: Path, log_path: Path) -> Path:
    aster = root / "deps" / "Aster"
    env = os.environ.copy()
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        env.pop(key, None)
    if not (aster / ".git").exists():
        call(
            [
                "git",
                "-c",
                "http.proxy=",
                "-c",
                "https.proxy=",
                "clone",
                "https://github.com/NTU-Siqiang-Group/Aster.git",
                str(aster),
            ],
            log_path,
        )
    call(["git", "checkout", ASTER_COMMIT], log_path, cwd=aster)
    return aster


def ensure_userspace_boost(root: Path, log_path: Path) -> Path:
    header = Path("/usr/include/boost/functional/hash.hpp")
    if header.exists():
        return Path("/usr/include")
    boost_root = root / "deps" / "_external_work" / "boost-root"
    boost_header = boost_root / "usr" / "include" / "boost" / "functional" / "hash.hpp"
    if boost_header.exists():
        return boost_root / "usr" / "include"
    apt_dir = root / "deps" / "_external_work" / "apt"
    apt_dir.mkdir(parents=True, exist_ok=True)
    call(["apt-get", "download", "libboost1.71-dev"], log_path, cwd=apt_dir)
    debs = sorted(apt_dir.glob("libboost1.71-dev_*_amd64.deb"))
    if not debs:
        raise RuntimeError("libboost1.71-dev deb not found after apt-get download")
    boost_root.mkdir(parents=True, exist_ok=True)
    call(["dpkg-deb", "-x", str(debs[-1]), str(boost_root)], log_path)
    if not boost_header.exists():
        raise RuntimeError(f"boost header still missing: {boost_header}")
    return boost_root / "usr" / "include"


def ensure_userspace_gflags(root: Path, log_path: Path) -> tuple[Path, Path]:
    header = Path("/usr/include/gflags/gflags.h")
    lib = Path("/usr/lib/x86_64-linux-gnu/libgflags.so")
    if header.exists() and lib.exists():
        return Path("/usr/include"), Path("/usr/lib/x86_64-linux-gnu")
    gflags_root = root / "deps" / "_external_work" / "gflags-root"
    gflags_header = gflags_root / "usr" / "include" / "gflags" / "gflags.h"
    gflags_lib = gflags_root / "usr" / "lib" / "x86_64-linux-gnu" / "libgflags.so"
    if gflags_header.exists() and gflags_lib.exists():
        return gflags_root / "usr" / "include", gflags_root / "usr" / "lib" / "x86_64-linux-gnu"
    apt_dir = root / "deps" / "_external_work" / "apt"
    apt_dir.mkdir(parents=True, exist_ok=True)
    call(["apt-get", "download", "libgflags-dev", "libgflags2.2"], log_path, cwd=apt_dir)
    gflags_root.mkdir(parents=True, exist_ok=True)
    for deb in sorted(apt_dir.glob("libgflags*_amd64.deb")):
        call(["dpkg-deb", "-x", str(deb), str(gflags_root)], log_path)
    if not gflags_header.exists() or not gflags_lib.exists():
        raise RuntimeError(f"gflags userspace dependency missing under {gflags_root}")
    return gflags_root / "usr" / "include", gflags_root / "usr" / "lib" / "x86_64-linux-gnu"


def build_aster(root: Path, system_dir: Path) -> dict[str, Any]:
    build_log = system_dir / "build" / "build.log"
    build_log.parent.mkdir(parents=True, exist_ok=True)
    if build_log.exists():
        build_log.unlink()
    aster = ensure_aster(root, build_log)
    boost_inc = ensure_userspace_boost(root, build_log)
    gflags_inc, gflags_lib = ensure_userspace_gflags(root, build_log)
    extra = f"EXTRA_CXXFLAGS=-I{boost_inc} -I{gflags_inc} -Wno-error=shadow -Wno-shadow"
    exec_ldflags = f"EXEC_LDFLAGS=-L{gflags_lib} -Wl,-rpath,{gflags_lib} -lgflags"
    call(["make", "-j32", extra, "static_lib"], build_log, cwd=aster, timeout_s=3600)
    call(["make", extra, exec_ldflags, "all"], build_log, cwd=aster / "graph_test", timeout_s=600)
    call(["./graph_example", "--load_mode=tiny"], build_log, cwd=aster / "graph_test", timeout_s=300)
    binary = package_dir(root) / "scripts" / "aster_typed_driver"
    compile_cmd = [
        "g++",
        "-std=c++17",
        "-O2",
        "-DNDEBUG",
        f"-I{aster / 'include'}",
        f"-I{boost_inc}",
        str(package_dir(root) / "scripts" / "aster_typed_driver.cpp"),
        "-o",
        str(binary),
        str(aster / "librocksdb.a"),
        "-lpthread",
        "-lrt",
        "-ldl",
        "-lz",
        "-lnuma",
        "-ltbb",
    ]
    call(compile_cmd, build_log, timeout_s=600)
    return {
        "aster_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=aster, text=True).strip(),
        "boost_include": str(boost_inc),
        "gflags_include": str(gflags_inc),
        "gflags_lib": str(gflags_lib),
        "driver": str(binary),
        "build_log": str(build_log),
    }


def du_bytes(path: Path) -> int:
    proc = subprocess.run(["du", "-sb", str(path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not proc.stdout.strip():
        return 0
    return int(proc.stdout.split()[0])


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def run_scale(root: Path, scale: str, timeout_s: int, overwrite: bool) -> dict[str, Any]:
    out = package_dir(root)
    system_dir = out / "systems" / "aster"
    run_dir = system_dir / run_label(scale)
    run_dir.mkdir(parents=True, exist_ok=True)
    result = run_dir / "result.json"
    done = run_dir / "DONE"
    failed = run_dir / "FAILED"
    timeout_marker = run_dir / "TIMEOUT"
    if done.exists() and result.exists() and not overwrite:
        return load_json(result)
    for marker in [done, failed, timeout_marker]:
        if marker.exists():
            marker.unlink()
    db_dir = run_dir / "db"
    if overwrite and db_dir.exists():
        resolved = db_dir.resolve()
        if "3plus3-baselines" not in str(resolved):
            raise RuntimeError(f"refusing to delete non-package path: {resolved}")
        shutil.rmtree(db_dir)
    binary = out / "scripts" / "aster_typed_driver"
    run_log = run_dir / "run.log"
    if run_log.exists():
        run_log.unlink()
    cmd = [
        str(binary),
        f"--dense={dense_path(root, scale)}",
        f"--truth={truth_path(root, scale)}",
        f"--db={db_dir}",
        f"--out={result}",
        f"--scale={scale}",
    ]
    try:
        proc = call_allow_fail(cmd, run_log, timeout_s=timeout_s)
    except subprocess.TimeoutExpired:
        timeout_marker.write_text("timeout\n", encoding="utf-8")
        return {
            "system": "Aster RocksGraph",
            "scale": scale,
            "status": "TIMEOUT",
            "timeout_s": timeout_s,
            "db_dir": str(db_dir),
        }
    doc = load_json(result)
    doc["disk_total_bytes"] = du_bytes(db_dir)
    result.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if proc.returncode == 0 and doc.get("status") == "PASS":
        done.write_text("done\n", encoding="utf-8")
    else:
        failed.write_text("failed\n", encoding="utf-8")
    return doc


def summary_rows_from_result(scale: str, doc: dict[str, Any]) -> list[dict[str, Any]]:
    if doc.get("status") != "PASS":
        return []
    rows = []
    scopes = [
        ("all-types-summary", "all_types", doc.get("total_neighbors", "")),
        ("positive-edge-types-summary", "positive_edge_types", doc.get("positive_neighbors", "")),
        ("negative-edge-types-summary", "negative_edge_types", doc.get("negative_neighbors", "")),
    ]
    for scope, key, neighbors in scopes:
        metrics = doc.get(key, {})
        rows.append(
            {
                "system": "Aster RocksGraph",
                "group": "problem-adjacent",
                "scale": scale.upper(),
                "scope": scope,
                "ops": metrics.get("ops", ""),
                "avg_us": metrics.get("avg_us", ""),
                "p50_us": metrics.get("p50_us", ""),
                "p90_us": metrics.get("p90_us", ""),
                "p99_us": metrics.get("p99_us", ""),
                "load_s": doc.get("load_s", ""),
                "peak_rss_kb": doc.get("peak_rss_kb", ""),
                "disk_total_bytes": doc.get("disk_total_bytes", ""),
                "correctness": "PASS",
                "claim_level": "measured Aster/RocksGraph typed-neighbor bridge baseline",
                "raw_artifact": f"baseline/external-baselines-20260626/3plus3-baselines/systems/aster/{run_label(scale)}",
                "notes": f"neighbors={neighbors}; bridge=compact logical vertex per (edge_type,src)",
            }
        )
    return rows


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_outputs(root: Path, build: dict[str, Any], results: dict[str, dict[str, Any]]) -> None:
    system_dir = package_dir(root) / "systems" / "aster"
    metrics: list[dict[str, Any]] = []
    correctness: list[dict[str, Any]] = []
    sf1_state = "TODO"
    sf10_state = "WAIT-SF1"
    overall = "PENDING"
    for scale in ["sf1", "sf10"]:
        doc = results.get(scale, {})
        if not doc:
            continue
        state = str(doc.get("status", "UNKNOWN"))
        if scale == "sf1":
            sf1_state = "PASS" if state == "PASS" else state
        else:
            sf10_state = "PASS" if state == "PASS" else state
        metrics.extend(summary_rows_from_result(scale, doc))
        correctness.append(
            {
                "system": "Aster RocksGraph",
                "scale": scale.upper(),
                "check": "query_count_digest",
                "expected": f"checked={doc.get('checked', '')}; mismatches=0",
                "observed": f"checked={doc.get('checked', '')}; mismatches={doc.get('mismatches', '')}",
                "status": "PASS" if state == "PASS" else state,
                "raw_artifact": f"baseline/external-baselines-20260626/3plus3-baselines/systems/aster/{run_label(scale)}/result.json",
                "notes": "fixed Neo4j truth-s50-seed42.tsv vs Aster compact typed-neighbor bridge",
            }
        )
    if results.get("sf10", {}).get("status") == "PASS":
        overall = "SF10 DONE"
    elif results.get("sf1", {}).get("status") == "PASS":
        overall = "SF1 DONE; SF10 " + str(results.get("sf10", {}).get("status", "WAIT"))
    elif results.get("sf1"):
        overall = "SF1 " + str(results["sf1"].get("status", "UNKNOWN"))

    write_tsv(
        system_dir / "metrics.tsv",
        metrics,
        [
            "system",
            "group",
            "scale",
            "scope",
            "ops",
            "avg_us",
            "p50_us",
            "p90_us",
            "p99_us",
            "load_s",
            "peak_rss_kb",
            "disk_total_bytes",
            "correctness",
            "claim_level",
            "raw_artifact",
            "notes",
        ],
    )
    write_tsv(
        system_dir / "correctness.tsv",
        correctness,
        ["system", "scale", "check", "expected", "observed", "status", "raw_artifact", "notes"],
    )
    status = {
        "status": overall,
        "sf1": sf1_state,
        "sf10": sf10_state,
        "correctness": "PASS" if any(r.get("status") == "PASS" for r in correctness) else "TODO",
        "raw": "baseline/external-baselines-20260626/3plus3-baselines/systems/aster",
        "notes": "Aster/RocksGraph source available; typed-neighbor bridge uses compact logical vertex per (edge_type,src) to avoid sparse-id Morris counter blow-up.",
        "version": build.get("aster_commit", ""),
    }
    (system_dir / "status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def merge_existing_results(root: Path, results: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    system_dir = package_dir(root) / "systems" / "aster"
    merged = dict(results)
    for scale in ["sf1", "sf10"]:
        if scale in merged and merged[scale]:
            continue
        path = system_dir / run_label(scale) / "result.json"
        if path.exists():
            merged[scale] = json.loads(path.read_text(encoding="utf-8"))
    return merged


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", choices=["sf1", "sf10", "all"], default="all")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--sf10-timeout-s", type=int, default=86400)
    args = parser.parse_args()
    root = repo_root()
    system_dir = package_dir(root) / "systems" / "aster"
    system_dir.mkdir(parents=True, exist_ok=True)
    build = build_aster(root, system_dir)
    results: dict[str, dict[str, Any]] = {}
    if args.scale in {"sf1", "all"}:
        results["sf1"] = run_scale(root, "sf1", timeout_s=7200, overwrite=args.overwrite)
    if args.scale == "sf10" or (args.scale == "all" and results.get("sf1", {}).get("status") == "PASS"):
        results["sf10"] = run_scale(root, "sf10", timeout_s=args.sf10_timeout_s, overwrite=args.overwrite)
    results = merge_existing_results(root, results)
    write_outputs(root, build, results)


if __name__ == "__main__":
    main()
