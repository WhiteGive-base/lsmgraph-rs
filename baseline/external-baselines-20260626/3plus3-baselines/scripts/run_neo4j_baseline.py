#!/usr/bin/env python3
"""Run Neo4j typed-neighbor baseline for the SemL0 3+3 package.

The runner imports every dense edge type as its own outgoing Neo4j relationship
type. Correctness is checked against the full dense edge list.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase


MASK = (1 << 64) - 1


@dataclass(frozen=True)
class Query:
    edge_type: int
    src: int


@dataclass
class Digest:
    count: int = 0
    sum_hash: int = 0
    xor_hash: int = 0


def repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parents[4]


def run_dir(root: Path, scale: str) -> Path:
    return root / "baseline" / "external-baselines-20260626" / "3plus3-baselines" / "systems" / "neo4j" / run_label(scale)


def run_label(scale: str) -> str:
    return "sf10-main" if scale == "sf10" else "sf1-smoke"


def dense_path(root: Path, scale: str) -> Path:
    if scale == "sf1":
        return root / "baseline" / "external-baselines-20260624" / "livegraph" / "sf1-smoke" / "edges-dense.txt"
    if scale == "sf10":
        return root / "baseline" / "external-baselines-20260624" / "livegraph" / "sf10-typed-neighbor" / "edges-dense.txt"
    raise ValueError(f"unsupported scale: {scale}")


IMPORT_SCHEMA_VERSION = "all-dense-edge-types-v2"


def rel_type(edge_type: int) -> str:
    if edge_type > 0:
        return f"E_P{edge_type}"
    return f"E_N{abs(edge_type)}"


def mix64(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & MASK
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & MASK
    return (x ^ (x >> 31)) & MASK


def digest_add(digest: Digest, dst: int) -> None:
    h1 = mix64(dst)
    h2 = mix64(dst ^ 0xD6E8FEB86659FD93)
    digest.count += 1
    digest.sum_hash = (digest.sum_hash + h1) & MASK
    digest.xor_hash ^= h2


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return math.nan
    values = sorted(values)
    idx = min(len(values) - 1, max(0, math.ceil((pct / 100.0) * len(values)) - 1))
    return values[idx]


def call(cmd: list[str], log_path: Path, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
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
        )
        elapsed = time.time() - started
        log.write(proc.stdout)
        log.write(f"\n[exit={proc.returncode} elapsed_s={elapsed:.3f}]\n")
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}; see {log_path}")
    return proc


def read_vertex_count(path: Path) -> int:
    with path.open("r", encoding="utf-8") as f:
        line = f.readline().strip()
    return int(line)


def convert_import_csv(edges: Path, import_dir: Path, overwrite: bool) -> dict[str, Any]:
    import_dir.mkdir(parents=True, exist_ok=True)
    done = import_dir / "CONVERT_DONE"
    nodes = import_dir / "nodes.csv"
    rels = import_dir / "rels.csv"
    summary = import_dir / "convert-summary.json"
    if done.exists() and nodes.exists() and rels.exists() and summary.exists() and not overwrite:
        current = json.loads(summary.read_text(encoding="utf-8"))
        if current.get("schema_version") == IMPORT_SCHEMA_VERSION:
            return current

    if overwrite:
        for path in [nodes, rels, summary, done]:
            if path.exists():
                path.unlink()

    start = time.time()
    vertex_count = read_vertex_count(edges)
    edge_count = 0
    imported_edges = 0
    etypes: dict[int, int] = {}

    with nodes.open("w", encoding="utf-8", newline="") as nf:
        nf.write("neo_id:ID(V),id:long\n")
        for node_id in range(vertex_count):
            nf.write(f"{node_id},{node_id}\n")

    with edges.open("r", encoding="utf-8") as inf, rels.open("w", encoding="utf-8", newline="") as rf:
        header = inf.readline()
        if not header:
            raise RuntimeError(f"empty dense edge file: {edges}")
        rf.write(":START_ID(V),:END_ID(V),:TYPE\n")
        for line in inf:
            src_s, et_s, dst_s = line.split()
            edge_count += 1
            et = int(et_s)
            etypes[et] = etypes.get(et, 0) + 1
            imported_edges += 1
            rf.write(f"{src_s},{dst_s},{rel_type(et)}\n")

    out = {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "vertex_count": vertex_count,
        "dense_edges": edge_count,
        "relationships_imported": imported_edges,
        "edge_types": etypes,
        "elapsed_s": time.time() - start,
        "nodes_csv": str(nodes),
        "rels_csv": str(rels),
        "model": "all dense edge types imported as distinct outgoing relationship types",
    }
    summary.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    done.write_text("done\n", encoding="utf-8")
    return out


def reservoir_add(reservoir: list[int], seen: int, src: int, samples: int, rng: random.Random) -> None:
    if len(reservoir) < samples:
        reservoir.append(src)
        return
    if samples <= 0:
        return
    idx = rng.randrange(seen)
    if idx < samples:
        reservoir[idx] = src


def generate_truth(edges: Path, workload_dir: Path, samples: int, seed: int, overwrite: bool) -> dict[str, Any]:
    workload_dir.mkdir(parents=True, exist_ok=True)
    done = workload_dir / f"truth-s{samples}-seed{seed}.DONE"
    truth_path = workload_dir / f"truth-s{samples}-seed{seed}.tsv"
    summary_path = workload_dir / f"truth-s{samples}-seed{seed}.json"
    if done.exists() and truth_path.exists() and summary_path.exists() and not overwrite:
        return json.loads(summary_path.read_text(encoding="utf-8"))

    rng = random.Random(seed)
    reservoirs: dict[int, list[int]] = {}
    seen: dict[int, int] = {}
    edge_count = 0
    start = time.time()

    with edges.open("r", encoding="utf-8") as f:
        vertex_count = int(f.readline().strip())
        for line in f:
            src_s, et_s, _dst_s = line.split()
            src = int(src_s)
            et = int(et_s)
            edge_count += 1
            seen[et] = seen.get(et, 0) + 1
            reservoir = reservoirs.setdefault(et, [])
            reservoir_add(reservoir, seen[et], src, samples, rng)

    queries = [Query(et, src) for et in sorted(reservoirs) for src in reservoirs[et]]
    truth: dict[Query, Digest] = {q: Digest() for q in queries}
    with edges.open("r", encoding="utf-8") as f:
        f.readline()
        for line in f:
            src_s, et_s, dst_s = line.split()
            key = Query(int(et_s), int(src_s))
            digest = truth.get(key)
            if digest is not None:
                digest_add(digest, int(dst_s))

    with truth_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(["query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"])
        for idx, query in enumerate(queries):
            digest = truth[query]
            writer.writerow([idx, query.edge_type, query.src, digest.count, digest.sum_hash, digest.xor_hash])

    per_type = []
    for et in sorted(reservoirs):
        q = [query for query in queries if query.edge_type == et]
        per_type.append(
            {
                "edge_type": et,
                "sampled_queries": len(q),
                "unique_queries": len(set(q)),
                "truth_neighbors": sum(truth[query].count for query in q),
            }
        )
    out = {
        "vertex_count": vertex_count,
        "edge_count": edge_count,
        "samples_per_edge_type": samples,
        "seed": seed,
        "sampled_queries": len(queries),
        "unique_queries": len(truth),
        "truth_path": str(truth_path),
        "per_edge_type": per_type,
        "elapsed_s": time.time() - start,
    }
    summary_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    done.write_text("done\n", encoding="utf-8")
    return out


def remove_container(name: str, log_path: Path) -> None:
    call(["docker", "rm", "-f", name], log_path, check=False)


def import_database(data_dir: Path, import_dir: Path, image: str, log_path: Path, overwrite: bool) -> float:
    done = data_dir.parent / f"NEO4J_IMPORT_DONE-{IMPORT_SCHEMA_VERSION}"
    import_s_path = data_dir.parent / f"neo4j-import-s-{IMPORT_SCHEMA_VERSION}.txt"
    if done.exists() and not overwrite:
        return float(import_s_path.read_text(encoding="utf-8").strip())
    if overwrite and data_dir.exists():
        resolved = data_dir.resolve()
        if "3plus3-baselines" not in str(resolved):
            raise RuntimeError(f"refusing to delete non-package path: {resolved}")
        try:
            shutil.rmtree(data_dir)
        except PermissionError:
            chown_tree(data_dir, image, log_path)
            shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{data_dir}:/data",
        "-v",
        f"{import_dir}:/import:ro",
        image,
        "neo4j-admin",
        "database",
        "import",
        "full",
        "--overwrite-destination=true",
        "--id-type=integer",
        "--nodes=V=/import/nodes.csv",
        "--relationships=/import/rels.csv",
        "--",
        "neo4j",
    ]
    call(cmd, log_path)
    elapsed = time.time() - start
    import_s_path.write_text(f"{elapsed:.6f}\n", encoding="utf-8")
    done.write_text("done\n", encoding="utf-8")
    return elapsed


def chown_tree(path: Path, image: str, log_path: Path) -> None:
    uid = str(os.getuid())
    gid = str(os.getgid())
    call(
        [
            "docker",
            "run",
            "--rm",
            "-u",
            "root",
            "-v",
            f"{path}:/target",
            image,
            "chown",
            "-R",
            f"{uid}:{gid}",
            "/target",
        ],
        log_path,
        check=False,
    )


def start_server(data_dir: Path, image: str, container: str, port: int, log_path: Path) -> None:
    remove_container(container, log_path)
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container,
        "-p",
        f"127.0.0.1:{port}:7687",
        "-v",
        f"{data_dir}:/data",
        "-e",
        "NEO4J_AUTH=none",
        image,
    ]
    call(cmd, log_path)


def wait_for_neo4j(uri: str, timeout_s: int = 180) -> Any:
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            driver = GraphDatabase.driver(uri, auth=None)
            with driver.session(database="neo4j") as session:
                session.run("RETURN 1 AS ok").single()
            return driver
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"Neo4j did not become ready: {last_error}")


def ensure_index(driver: Any) -> float:
    start = time.time()
    with driver.session(database="neo4j") as session:
        session.run("CREATE INDEX v_id IF NOT EXISTS FOR (v:V) ON (v.id)").consume()
        session.run("CALL db.awaitIndexes()").consume()
    return time.time() - start


def query_text(edge_type: int) -> str:
    t = rel_type(edge_type)
    return f"MATCH (s:V {{id: $src}})-[:{t}]->(d:V) RETURN d.id AS dst"


def run_queries(driver: Any, truth_path: Path, output_path: Path, max_mismatches: int) -> dict[str, Any]:
    rows = []
    with truth_path.open("r", encoding="utf-8", newline="") as f:
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

    query_cache: dict[int, str] = {}
    latencies_us: list[float] = []
    mismatches = []
    total_neighbors = 0
    positive_neighbors = 0
    negative_neighbors = 0
    positive_latencies: list[float] = []
    negative_latencies: list[float] = []
    started = time.time()
    with driver.session(database="neo4j") as session:
        for row in rows:
            et = row["edge_type"]
            query = query_cache.setdefault(et, query_text(et))
            t0 = time.perf_counter_ns()
            digest = Digest()
            for record in session.run(query, src=row["src"]):
                digest_add(digest, int(record["dst"]))
            elapsed_us = (time.perf_counter_ns() - t0) / 1000.0
            latencies_us.append(elapsed_us)
            total_neighbors += digest.count
            if et > 0:
                positive_neighbors += digest.count
                positive_latencies.append(elapsed_us)
            else:
                negative_neighbors += digest.count
                negative_latencies.append(elapsed_us)
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
                        "observed": {
                            "count": digest.count,
                            "sum_hash": digest.sum_hash,
                            "xor_hash": digest.xor_hash,
                        },
                    }
                )
    elapsed_s = time.time() - started

    def summary(vals: list[float]) -> dict[str, Any]:
        return {
            "ops": len(vals),
            "avg_us": (sum(vals) / len(vals)) if vals else None,
            "p50_us": percentile(vals, 50) if vals else None,
            "p90_us": percentile(vals, 90) if vals else None,
            "p99_us": percentile(vals, 99) if vals else None,
        }

    out = {
        "status": "PASS" if not mismatches else "FAIL",
        "checked": len(rows),
        "mismatches": len(mismatches),
        "elapsed_s": elapsed_s,
        "neighbors": {
            "all": total_neighbors,
            "positive": positive_neighbors,
            "negative": negative_neighbors,
        },
        "all_types": summary(latencies_us),
        "positive_edge_types": summary(positive_latencies),
        "negative_edge_types": summary(negative_latencies),
        "sample_mismatches": mismatches,
    }
    output_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def du_bytes(path: Path) -> int:
    proc = subprocess.run(["du", "-sb", str(path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        return 0
    return int(proc.stdout.split()[0])


def write_status(system_dir: Path, status: dict[str, Any]) -> None:
    (system_dir / "status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_outputs(system_dir: Path, scale: str, load_s: float, disk_bytes: int, query_result: dict[str, Any], truth_summary: dict[str, Any], import_summary: dict[str, Any]) -> None:
    metrics_path = system_dir / "metrics.tsv"
    correctness_path = system_dir / "correctness.tsv"
    rows = [
        ("all-types-summary", query_result["all_types"], query_result["neighbors"]["all"]),
        ("positive-edge-types-summary", query_result["positive_edge_types"], query_result["neighbors"]["positive"]),
        ("negative-edge-types-summary", query_result["negative_edge_types"], query_result["neighbors"]["negative"]),
    ]
    with metrics_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
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
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for scope, summary, neighbors in rows:
            writer.writerow(
                {
                    "system": "Neo4j Community",
                    "group": "open-source-graph-db",
                    "scale": scale.upper(),
                    "scope": scope,
                    "ops": summary["ops"],
                    "avg_us": summary["avg_us"] if summary["avg_us"] is not None else "",
                    "p50_us": summary["p50_us"] if summary["p50_us"] is not None else "",
                    "p90_us": summary["p90_us"] if summary["p90_us"] is not None else "",
                    "p99_us": summary["p99_us"] if summary["p99_us"] is not None else "",
                    "load_s": load_s,
                    "peak_rss_kb": "",
                    "disk_total_bytes": disk_bytes,
                    "correctness": "PASS" if query_result["status"] == "PASS" else "FAIL",
                    "claim_level": "measured graph DB baseline; SF10 main" if scale == "sf10" else "measured graph DB baseline; SF1 smoke until SF10 passes",
                    "raw_artifact": f"baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j/{run_label(scale)}",
                    "notes": f"neighbors={neighbors}; samples={truth_summary['samples_per_edge_type']}; relationships_imported={import_summary['relationships_imported']}",
                }
            )

    with correctness_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["system", "scale", "check", "expected", "observed", "status", "raw_artifact", "notes"],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(
            {
                "system": "Neo4j Community",
                "scale": scale.upper(),
                "check": "query_count_hash_digest",
                "expected": "mismatches=0",
                "observed": f"checked={query_result['checked']}; mismatches={query_result['mismatches']}",
                "status": query_result["status"],
                "raw_artifact": f"baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j/{run_label(scale)}/query-results.json",
                "notes": "fixed-seed dense-list truth vs Neo4j typed-neighbor traversal; all dense edge types are imported as distinct relationship types",
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", choices=["sf1", "sf10"], default="sf1")
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--image", default="neo4j:5.26.24")
    parser.add_argument("--port", type=int, default=17687)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    root = repo_root()
    system_dir = root / "baseline" / "external-baselines-20260626" / "3plus3-baselines" / "systems" / "neo4j"
    rd = run_dir(root, args.scale)
    rd.mkdir(parents=True, exist_ok=True)
    log_path = rd / "run.log"
    container = f"seml0-neo4j-{args.scale}-3p3"
    try:
        edges = dense_path(root, args.scale)
        import_summary = convert_import_csv(edges, rd / "import", args.overwrite)
        truth_summary = generate_truth(edges, rd / "workload", args.samples, args.seed, args.overwrite)
        load_s = import_database(rd / "neo4j-data", rd / "import", args.image, log_path, args.overwrite)
        start_server(rd / "neo4j-data", args.image, container, args.port, log_path)
        driver = wait_for_neo4j(f"bolt://127.0.0.1:{args.port}")
        index_s = ensure_index(driver)
        query_result = run_queries(
            driver,
            Path(truth_summary["truth_path"]),
            rd / "query-results.json",
            max_mismatches=20,
        )
        driver.close()
        remove_container(container, log_path)
        disk_bytes = du_bytes(rd / "neo4j-data")
        write_outputs(system_dir, args.scale, load_s + index_s, disk_bytes, query_result, truth_summary, import_summary)
        previous_status_path = system_dir / "status.json"
        previous_status = load_json_file(previous_status_path)
        if args.scale == "sf1":
            sf1 = "PASS" if query_result["status"] == "PASS" else "FAIL"
            sf10 = "READY-SF10" if query_result["status"] == "PASS" else "WAIT-SF1"
        else:
            sf1 = previous_status.get("sf1", "PASS")
            sf10 = "PASS" if query_result["status"] == "PASS" else "FAIL"
        write_status(
            system_dir,
            {
                "status": f"{args.scale.upper()} DONE" if query_result["status"] == "PASS" else f"{args.scale.upper()} FAILED",
                "sf1": sf1,
                "sf10": sf10,
                "correctness": query_result["status"],
                "raw": f"baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j/{run_label(args.scale)}",
                "notes": f"Neo4j {args.image}; samples_per_edge_type={args.samples}; import model=all dense edge types as distinct outgoing relationship types.",
                "version": args.image,
            },
        )
        (rd / ("DONE" if query_result["status"] == "PASS" else "FAILED")).write_text(
            json.dumps({"status": query_result["status"], "checked": query_result["checked"], "mismatches": query_result["mismatches"]}) + "\n",
            encoding="utf-8",
        )
        return 0 if query_result["status"] == "PASS" else 1
    except Exception as exc:  # noqa: BLE001
        try:
            remove_container(container, log_path)
        except Exception:
            pass
        write_status(
            system_dir,
            {
                "status": f"{args.scale.upper()} FAILED",
                "sf1": "FAIL" if args.scale == "sf1" else "PASS-UNKNOWN",
                "sf10": "WAIT-SF1" if args.scale == "sf1" else "FAIL",
                "correctness": "FAIL",
                "raw": f"baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j/{run_label(args.scale)}",
                "notes": str(exc),
                "version": args.image,
            },
        )
        (rd / "FAILED").write_text(str(exc) + "\n", encoding="utf-8")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
