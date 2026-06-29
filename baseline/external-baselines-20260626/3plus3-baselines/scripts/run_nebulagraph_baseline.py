#!/usr/bin/env python3
"""Run NebulaGraph typed-neighbor baseline for the SemL0 3+3 package."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


MASK = (1 << 64) - 1
IMAGE_VERSION = "v3.8.0"
PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"


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


def rel_type(edge_type: int) -> str:
    return f"E_P{edge_type}" if edge_type > 0 else f"E_N{abs(edge_type)}"


def mix64(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & MASK
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & MASK
    return (x ^ (x >> 31)) & MASK


class Digest:
    def __init__(self) -> None:
        self.count = 0
        self.sum_hash = 0
        self.xor_hash = 0

    def add(self, dst: int) -> None:
        h1 = mix64(dst)
        h2 = mix64(dst ^ 0xD6E8FEB86659FD93)
        self.count += 1
        self.sum_hash = (self.sum_hash + h1) & MASK
        self.xor_hash ^= h2

    def as_dict(self) -> dict[str, int]:
        return {"count": self.count, "sum_hash": self.sum_hash, "xor_hash": self.xor_hash}


def call(cmd: list[str], log_path: Path, cwd: Path | None = None, check: bool = True, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
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
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}; see {log_path}")
    return proc


def ensure_client(root: Path, log_path: Path) -> Path:
    target = package_dir(root) / "systems" / "nebulagraph" / "pydeps"
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))
    try:
        import nebula3  # noqa: F401
        return target
    except Exception:
        pass
    target.mkdir(parents=True, exist_ok=True)
    try:
        import pip  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"pip is unavailable for --target install: {exc}") from exc
    if True:
        env = os.environ.copy()
        for key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
            env.pop(key, None)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"$ {sys.executable} -m pip install --target {target} -i {PYPI_INDEX} nebula3-python\n")
            log.flush()
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--target", str(target), "-i", PYPI_INDEX, "nebula3-python"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=300,
            )
            log.write(proc.stdout)
            log.write(f"\n[exit={proc.returncode}]\n")
        if proc.returncode != 0:
            raise RuntimeError("failed to install nebula3-python")
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))
    import nebula3  # noqa: F401
    return target


def import_nebula_client(target: Path) -> None:
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))


def container_names(scale: str) -> dict[str, str]:
    return {
        "network": f"seml0-nebula-{scale}",
        "meta": f"seml0-nebula-meta-{scale}",
        "storage": f"seml0-nebula-storage-{scale}",
        "graph": f"seml0-nebula-graph-{scale}",
    }


def cleanup_containers(scale: str, log_path: Path) -> None:
    names = container_names(scale)
    for name in [names["graph"], names["storage"], names["meta"]]:
        call(["docker", "rm", "-f", name], log_path, check=False)
    call(["docker", "network", "rm", names["network"]], log_path, check=False)


def remove_tree(path: Path, log_path: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    if "3plus3-baselines" not in str(resolved):
        raise RuntimeError(f"refusing to delete non-package path: {resolved}")
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        pass
    call(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{path}:/target",
            f"vesoft/nebula-graphd:{IMAGE_VERSION}",
            "-lc",
            "rm -rf /target/* /target/.[!.]* /target/..?* 2>/dev/null || true",
        ],
        log_path,
        check=False,
    )
    shutil.rmtree(path)


def start_services(root: Path, scale: str, run_dir: Path, log_path: Path, overwrite: bool) -> int:
    names = container_names(scale)
    port = 19669 if scale == "sf1" else 29669
    data_root = run_dir / "data"
    if overwrite and data_root.exists():
        remove_tree(data_root, log_path)
    for sub in ["meta", "storage", "graph", "logs"]:
        (data_root / sub).mkdir(parents=True, exist_ok=True)
    cleanup_containers(scale, log_path)
    call(["docker", "network", "create", names["network"]], log_path)
    call(
        [
            "docker",
            "run",
            "-d",
            "--name",
            names["meta"],
            "--network",
            names["network"],
            "-v",
            f"{data_root / 'meta'}:/data/meta",
            "-v",
            f"{data_root / 'logs'}:/logs",
            f"vesoft/nebula-metad:{IMAGE_VERSION}",
            f"--meta_server_addrs={names['meta']}:9559",
            f"--local_ip={names['meta']}",
            "--ws_ip=0.0.0.0",
            "--port=9559",
            "--ws_http_port=19559",
            "--data_path=/data/meta",
            "--log_dir=/logs",
        ],
        log_path,
    )
    time.sleep(5)
    call(
        [
            "docker",
            "run",
            "-d",
            "--name",
            names["storage"],
            "--network",
            names["network"],
            "-v",
            f"{data_root / 'storage'}:/data/storage",
            "-v",
            f"{data_root / 'logs'}:/logs",
            f"vesoft/nebula-storaged:{IMAGE_VERSION}",
            f"--meta_server_addrs={names['meta']}:9559",
            f"--local_ip={names['storage']}",
            "--ws_ip=0.0.0.0",
            "--port=9779",
            "--ws_http_port=19779",
            "--data_path=/data/storage",
            "--log_dir=/logs",
        ],
        log_path,
    )
    call(
        [
            "docker",
            "run",
            "-d",
            "--name",
            names["graph"],
            "--network",
            names["network"],
            "-p",
            f"127.0.0.1:{port}:9669",
            "-v",
            f"{data_root / 'graph'}:/data/graph",
            "-v",
            f"{data_root / 'logs'}:/logs",
            f"vesoft/nebula-graphd:{IMAGE_VERSION}",
            f"--meta_server_addrs={names['meta']}:9559",
            f"--local_ip={names['graph']}",
            "--ws_ip=0.0.0.0",
            "--port=9669",
            "--ws_http_port=19669",
            "--log_dir=/logs",
        ],
        log_path,
    )
    return port


def wait_session(port: int, timeout_s: int = 180) -> Any:
    from nebula3.Config import Config
    from nebula3.gclient.net import ConnectionPool

    config = Config()
    config.max_connection_pool_size = 10
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            pool = ConnectionPool()
            ok = pool.init([("127.0.0.1", port)], config)
            if not ok:
                raise RuntimeError("pool.init returned false")
            session = pool.get_session("root", "nebula")
            result = session.execute("SHOW HOSTS")
            if result.is_succeeded():
                return pool, session
            last_error = RuntimeError(str(result.error_msg()))
            session.release()
            pool.close()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(2)
    raise RuntimeError(f"NebulaGraph did not become ready: {last_error}")


def execute_ok(session: Any, stmt: str) -> None:
    result = session.execute(stmt)
    if not result.is_succeeded():
        raise RuntimeError(f"nGQL failed: {stmt}; {result.error_msg()}")


def register_storage(session: Any, scale: str, timeout_s: int = 180) -> None:
    storage = container_names(scale)["storage"]
    result = session.execute(f'ADD HOSTS "{storage}":9779')
    if not result.is_succeeded():
        msg = str(result.error_msg())
        if "existed" not in msg.lower() and "exist" not in msg.lower():
            raise RuntimeError(f"ADD HOSTS failed: {msg}")
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        result = session.execute("SHOW HOSTS")
        if result.is_succeeded():
            text = str(result)
            last = text
            if storage in text and "ONLINE" in text.upper():
                return
        time.sleep(2)
    raise RuntimeError(f"storage host did not become ONLINE: {last}")


def read_truth(path: Path) -> list[dict[str, int]]:
    rows = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            rows.append(
                {
                    "query_index": int(row["query_index"]),
                    "edge_type": int(row["edge_type"]),
                    "src": int(row["src"]),
                    "count": int(row["count"]),
                    "sum_hash": int(row["sum_hash"]),
                    "xor_hash": int(row["xor_hash"]),
                }
            )
    return rows


def discover_edge_types(path: Path) -> list[int]:
    types: set[int] = set()
    with path.open("r", encoding="utf-8") as f:
        f.readline()
        for line in f:
            _src, et, _dst = line.split()
            types.add(int(et))
    return sorted(types)


def setup_schema(session: Any, scale: str, edge_types: list[int]) -> None:
    register_storage(session, scale)
    space = f"seml0_{scale}"
    execute_ok(session, f"DROP SPACE IF EXISTS {space}")
    execute_ok(session, f"CREATE SPACE IF NOT EXISTS {space}(partition_num=64, replica_factor=1, vid_type=INT64)")
    time.sleep(20)
    execute_ok(session, f"USE {space}")
    for et in edge_types:
        execute_ok(session, f"CREATE EDGE IF NOT EXISTS {rel_type(et)}()")
    time.sleep(10)


def insert_buffer(session: Any, edge_type: int, pairs: list[tuple[int, int]]) -> None:
    if not pairs:
        return
    values = ",".join(f"{src}->{dst}:()" for src, dst in pairs)
    execute_ok(session, f"INSERT EDGE {rel_type(edge_type)}() VALUES {values}")


def load_edges(session: Any, path: Path, batch_size: int) -> tuple[int, int, float]:
    start = time.time()
    buffers: dict[int, list[tuple[int, int]]] = {}
    edge_count = 0
    vertex_count = 0
    with path.open("r", encoding="utf-8") as f:
        vertex_count = int(f.readline().strip())
        for line in f:
            src_s, et_s, dst_s = line.split()
            et = int(et_s)
            buf = buffers.setdefault(et, [])
            buf.append((int(src_s), int(dst_s)))
            edge_count += 1
            if len(buf) >= batch_size:
                insert_buffer(session, et, buf)
                buf.clear()
    for et, buf in buffers.items():
        insert_buffer(session, et, buf)
    return vertex_count, edge_count, time.time() - start


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, math.ceil((pct / 100.0) * len(values)) - 1))
    return values[idx]


def query_digest(session: Any, rows: list[dict[str, int]], max_mismatches: int) -> dict[str, Any]:
    latencies: list[float] = []
    pos_latencies: list[float] = []
    neg_latencies: list[float] = []
    mismatches = []
    total_neighbors = 0
    pos_neighbors = 0
    neg_neighbors = 0
    start = time.time()
    for row in rows:
        et = row["edge_type"]
        t0 = time.perf_counter_ns()
        result = session.execute(f"GO FROM {row['src']} OVER {rel_type(et)} YIELD dst(edge) AS dst")
        if not result.is_succeeded():
            raise RuntimeError(f"query failed: {result.error_msg()}")
        digest = Digest()
        for record in result:
            digest.add(record.values()[0].as_int())
        elapsed_us = (time.perf_counter_ns() - t0) / 1000.0
        latencies.append(elapsed_us)
        total_neighbors += digest.count
        if et > 0:
            pos_latencies.append(elapsed_us)
            pos_neighbors += digest.count
        else:
            neg_latencies.append(elapsed_us)
            neg_neighbors += digest.count
        ok = (
            digest.count == row["count"]
            and digest.sum_hash == row["sum_hash"]
            and digest.xor_hash == row["xor_hash"]
        )
        if not ok and len(mismatches) < max_mismatches:
            mismatches.append(
                {
                    "query_index": row["query_index"],
                    "edge_type": et,
                    "src": row["src"],
                    "expected": {
                        "count": row["count"],
                        "sum_hash": row["sum_hash"],
                        "xor_hash": row["xor_hash"],
                    },
                    "observed": digest.as_dict(),
                }
            )

    def summary(vals: list[float]) -> dict[str, Any]:
        return {
            "ops": len(vals),
            "avg_us": (sum(vals) / len(vals)) if vals else 0,
            "p50_us": percentile(vals, 50),
            "p90_us": percentile(vals, 90),
            "p99_us": percentile(vals, 99),
        }

    return {
        "status": "PASS" if not mismatches else "FAIL",
        "checked": len(rows),
        "mismatches": len(mismatches),
        "query_s": time.time() - start,
        "neighbors": {"all": total_neighbors, "positive": pos_neighbors, "negative": neg_neighbors},
        "all_types": summary(latencies),
        "positive_edge_types": summary(pos_latencies),
        "negative_edge_types": summary(neg_latencies),
        "sample_mismatches": mismatches,
    }


def du_bytes(path: Path) -> int:
    proc = subprocess.run(["du", "-sb", str(path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not proc.stdout.strip():
        return 0
    return int(proc.stdout.split()[0])


def docker_peak_rss_kb(names: dict[str, str]) -> int:
    peak = 0
    for name in [names["meta"], names["storage"], names["graph"]]:
        pid_out = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", name], text=True, stdout=subprocess.PIPE)
        if pid_out.returncode != 0:
            continue
        pid = pid_out.stdout.strip()
        status = Path("/proc") / pid / "status"
        if not status.exists():
            continue
        for line in status.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmHWM:"):
                peak += int(line.split()[1])
                break
    return peak


def run_scale(root: Path, scale: str, overwrite: bool, batch_size: int, sf10_project_limit_s: int) -> dict[str, Any]:
    out = package_dir(root)
    system_dir = out / "systems" / "nebulagraph"
    run_dir = system_dir / run_label(scale)
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "result.json"
    if result_path.exists() and not overwrite:
        return json.loads(result_path.read_text(encoding="utf-8"))
    for marker in ["DONE", "FAILED", "SKIPPED"]:
        p = run_dir / marker
        if p.exists():
            p.unlink()
    log_path = run_dir / "run.log"
    if log_path.exists():
        log_path.unlink()
    client_target = ensure_client(root, log_path)
    import_nebula_client(client_target)
    names = container_names(scale)
    port = start_services(root, scale, run_dir, log_path, overwrite)
    pool = None
    session = None
    try:
        pool, session = wait_session(port)
        edge_types = discover_edge_types(dense_path(root, scale))
        setup_schema(session, scale, edge_types)
        load_start = time.time()
        vertex_count, edge_count, load_s = load_edges(session, dense_path(root, scale), batch_size)
        throughput = edge_count / load_s if load_s > 0 else 0
        q = query_digest(session, read_truth(truth_path(root, scale)), max_mismatches=10)
        doc = {
            "system": "NebulaGraph",
            "status": q["status"],
            "scale": scale,
            "image_version": IMAGE_VERSION,
            "dense_path": str(dense_path(root, scale)),
            "truth_path": str(truth_path(root, scale)),
            "run_dir": str(run_dir),
            "loaded_vertices_header": vertex_count,
            "loaded_edges": edge_count,
            "load_s": load_s,
            "load_edges_per_s": throughput,
            "query_s": q["query_s"],
            "checked": q["checked"],
            "mismatches": q["mismatches"],
            "total_neighbors": q["neighbors"]["all"],
            "positive_neighbors": q["neighbors"]["positive"],
            "negative_neighbors": q["neighbors"]["negative"],
            "all_types": q["all_types"],
            "positive_edge_types": q["positive_edge_types"],
            "negative_edge_types": q["negative_edge_types"],
            "sample_mismatches": q["sample_mismatches"],
            "peak_rss_kb": docker_peak_rss_kb(names),
            "disk_total_bytes": du_bytes(run_dir / "data"),
            "batch_size": batch_size,
        }
        result_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (run_dir / ("DONE" if q["status"] == "PASS" else "FAILED")).write_text(q["status"].lower() + "\n", encoding="utf-8")
        return doc
    except Exception as exc:  # noqa: BLE001
        doc = {
            "system": "NebulaGraph",
            "status": "FAIL",
            "scale": scale,
            "error": str(exc),
            "run_dir": str(run_dir),
        }
        result_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (run_dir / "FAILED").write_text(str(exc) + "\n", encoding="utf-8")
        return doc
    finally:
        if session is not None:
            session.release()
        if pool is not None:
            pool.close()
        cleanup_containers(scale, log_path)


def maybe_skip_sf10(root: Path, sf1: dict[str, Any], sf10_limit_s: int) -> dict[str, Any] | None:
    if sf1.get("status") != "PASS":
        return None
    sf1_edges = float(sf1.get("loaded_edges", 0) or 0)
    sf1_load_s = float(sf1.get("load_s", 0) or 0)
    if sf1_edges <= 0 or sf1_load_s <= 0:
        return None
    sf10_edges = 355185382
    projected = sf10_edges * sf1_load_s / sf1_edges
    if projected <= sf10_limit_s:
        return None
    run_dir = package_dir(root) / "systems" / "nebulagraph" / run_label("sf10")
    run_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "system": "NebulaGraph",
        "scale": "sf10",
        "status": "SKIPPED-PROJECTED-TOO-SLOW",
        "projected_load_s": projected,
        "limit_s": sf10_limit_s,
        "basis": {
            "sf1_edges": sf1_edges,
            "sf1_load_s": sf1_load_s,
            "sf1_edges_per_s": sf1_edges / sf1_load_s,
        },
    }
    (run_dir / "result.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (run_dir / "SKIPPED").write_text("projected too slow\n", encoding="utf-8")
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
        m = doc.get(key, {})
        rows.append(
            {
                "system": "NebulaGraph",
                "group": "open-source-graph-db",
                "scale": scale.upper(),
                "scope": scope,
                "ops": m.get("ops", ""),
                "avg_us": m.get("avg_us", ""),
                "p50_us": m.get("p50_us", ""),
                "p90_us": m.get("p90_us", ""),
                "p99_us": m.get("p99_us", ""),
                "load_s": doc.get("load_s", ""),
                "peak_rss_kb": doc.get("peak_rss_kb", ""),
                "disk_total_bytes": doc.get("disk_total_bytes", ""),
                "correctness": "PASS",
                "claim_level": "measured graph DB baseline; NebulaGraph nGQL edge-type model",
                "raw_artifact": f"baseline/external-baselines-20260626/3plus3-baselines/systems/nebulagraph/{run_label(scale)}",
                "notes": f"neighbors={neighbors}; batch_size={doc.get('batch_size', '')}; image={IMAGE_VERSION}",
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


def write_outputs(root: Path, results: dict[str, dict[str, Any]]) -> None:
    system_dir = package_dir(root) / "systems" / "nebulagraph"
    metrics: list[dict[str, Any]] = []
    correctness: list[dict[str, Any]] = []
    sf1_state = "TODO"
    sf10_state = "WAIT-SF1"
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
                "system": "NebulaGraph",
                "scale": scale.upper(),
                "check": "query_count_digest",
                "expected": f"checked={doc.get('checked', '')}; mismatches=0",
                "observed": f"checked={doc.get('checked', '')}; mismatches={doc.get('mismatches', '')}",
                "status": "PASS" if state == "PASS" else state,
                "raw_artifact": f"baseline/external-baselines-20260626/3plus3-baselines/systems/nebulagraph/{run_label(scale)}/result.json",
                "notes": "fixed Neo4j truth-s50-seed42.tsv vs NebulaGraph typed edge traversal",
            }
        )
    if results.get("sf10", {}).get("status") == "PASS":
        overall = "SF10 DONE"
    elif results.get("sf1", {}).get("status") == "PASS":
        overall = "SF1 DONE; SF10 " + str(results.get("sf10", {}).get("status", "WAIT"))
    elif results.get("sf1"):
        overall = "SF1 " + str(results["sf1"].get("status", "UNKNOWN"))
    else:
        overall = "PENDING"
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
        "raw": "baseline/external-baselines-20260626/3plus3-baselines/systems/nebulagraph",
        "notes": "NebulaGraph server images available; nGQL edge-type model uses one edge type per dense edge type.",
        "version": f"vesoft/nebula-graphd/metad/storaged:{IMAGE_VERSION}",
    }
    (system_dir / "status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def merge_existing_results(root: Path, results: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    system_dir = package_dir(root) / "systems" / "nebulagraph"
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
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--sf10-project-limit-s", type=int, default=86400)
    args = parser.parse_args()
    root = repo_root()
    results: dict[str, dict[str, Any]] = {}
    if args.scale in {"sf1", "all"}:
        results["sf1"] = run_scale(root, "sf1", args.overwrite, args.batch_size, args.sf10_project_limit_s)
    if args.scale == "sf10":
        results["sf10"] = run_scale(root, "sf10", args.overwrite, args.batch_size, args.sf10_project_limit_s)
    elif args.scale == "all" and results.get("sf1", {}).get("status") == "PASS":
        skipped = maybe_skip_sf10(root, results["sf1"], args.sf10_project_limit_s)
        results["sf10"] = skipped if skipped else run_scale(root, "sf10", args.overwrite, args.batch_size, args.sf10_project_limit_s)
    results = merge_existing_results(root, results)
    write_outputs(root, results)


if __name__ == "__main__":
    main()
