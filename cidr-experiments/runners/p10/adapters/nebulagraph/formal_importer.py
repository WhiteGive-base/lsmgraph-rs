#!/usr/bin/env python3
"""Import canonical SF10 typed-neighbor data into a formal NebulaGraph store.

This program is intentionally a narrow producer for build_import_receipt.py.  It
creates one offline golden store, writes the exact importer report requested by
the wrapper, and records one digest observation for every canonical truth row.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


MASK = (1 << 64) - 1
REPORT_SCHEMA = "cidr-p10-nebulagraph-importer-report-v1"
OBSERVATION_COLUMNS = (
    "query_index",
    "edge_type",
    "src",
    "expected_count",
    "actual_count",
    "expected_sum_hash",
    "actual_sum_hash",
    "expected_xor_hash",
    "actual_xor_hash",
    "status",
)
LOGICAL_HOSTS = {
    "metad": "seml0-nebula-meta-sf10",
    "storaged": "seml0-nebula-storage-sf10",
    "graphd": "seml0-nebula-graph-sf10",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dataset-manifest", required=True, type=Path)
    parser.add_argument("--dense-dataset", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--importer-report", required=True, type=Path)
    parser.add_argument("--correctness-observations", required=True, type=Path)
    parser.add_argument("--docker", type=Path, default=Path("/usr/bin/docker"))
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--graph-port", type=int, default=39669)
    parser.add_argument("--container-prefix", required=True)
    return parser.parse_args()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_ref(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {"path": str(resolved), "sha256": sha256_file(resolved)}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def call(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        output = (completed.stdout or "")[-4000:] if capture else ""
        raise RuntimeError(
            "command failed (exit={}): {} {}".format(
                completed.returncode, " ".join(argv), output
            )
        )
    return completed


def load_baseline_helpers(repo_root: Path) -> Any:
    path = (
        repo_root
        / "baseline"
        / "external-baselines-20260626"
        / "3plus3-baselines"
        / "scripts"
        / "run_nebulagraph_baseline.py"
    )
    require(path.is_file(), f"missing tracked NebulaGraph helper: {path}")
    spec = importlib.util.spec_from_file_location("cidr_nebula_baseline", path)
    require(spec is not None and spec.loader is not None, "cannot load baseline helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_details(path: Path) -> tuple[Path, dict[str, str]]:
    value = load_json(path)
    require(value.get("schema_version") == "cidr-p10-nebulagraph-runtime-v1",
            "runtime manifest schema drift")
    require(value.get("system_version") == "NebulaGraph 3.8.0",
            "runtime system version drift")
    client_root = Path(value["client"]["tree"]["path"]).resolve()
    require(client_root.is_dir(), "runtime client root is missing")
    images = value.get("images")
    require(isinstance(images, list) and len(images) == 3,
            "runtime manifest must contain three images")
    image_by_role = {item["role"]: item["repo_digest"] for item in images}
    require(set(image_by_role) == {"graphd", "metad", "storaged"},
            "runtime image roles drift")
    return client_root, image_by_role


def names(prefix: str) -> dict[str, str]:
    require(
        prefix
        and len(prefix) <= 80
        and all(ch.isalnum() or ch in "_.-" for ch in prefix),
        "unsafe container prefix",
    )
    return {
        "network": f"{prefix}-net",
        "metad": f"{prefix}-meta",
        "storaged": f"{prefix}-storage",
        "graphd": f"{prefix}-graph",
    }


def cleanup(docker: Path, owned: dict[str, str]) -> None:
    for role in ("graphd", "storaged", "metad"):
        call([str(docker), "rm", "-f", owned[role]], check=False, timeout=120)
    call([str(docker), "network", "rm", owned["network"]], check=False, timeout=120)


def start_cluster(
    docker: Path,
    store: Path,
    logs: Path,
    images: dict[str, str],
    owned: dict[str, str],
    graph_port: int,
) -> None:
    call([str(docker), "network", "create", owned["network"]], timeout=60)

    common = [
        str(docker),
        "run",
        "-d",
        "--network",
        owned["network"],
        "--restart",
        "no",
    ]

    call(
        common
        + [
            "--name",
            owned["metad"],
            "--network-alias",
            LOGICAL_HOSTS["metad"],
            "--mount",
            f"type=bind,src={store / 'meta'},dst=/data/meta",
            "--mount",
            f"type=bind,src={logs / 'metad'},dst=/logs",
            images["metad"],
            f"--meta_server_addrs={LOGICAL_HOSTS['metad']}:9559",
            f"--local_ip={LOGICAL_HOSTS['metad']}",
            "--ws_ip=0.0.0.0",
            "--port=9559",
            "--ws_http_port=19559",
            "--data_path=/data/meta",
            "--log_dir=/logs",
        ]
    )
    time.sleep(5)

    call(
        common
        + [
            "--name",
            owned["storaged"],
            "--network-alias",
            LOGICAL_HOSTS["storaged"],
            "--mount",
            f"type=bind,src={store / 'storage'},dst=/data/storage",
            "--mount",
            f"type=bind,src={logs / 'storaged'},dst=/logs",
            images["storaged"],
            f"--meta_server_addrs={LOGICAL_HOSTS['metad']}:9559",
            f"--local_ip={LOGICAL_HOSTS['storaged']}",
            "--ws_ip=0.0.0.0",
            "--port=9779",
            "--ws_http_port=19779",
            "--data_path=/data/storage",
            "--log_dir=/logs",
        ]
    )

    call(
        common
        + [
            "--name",
            owned["graphd"],
            "--network-alias",
            LOGICAL_HOSTS["graphd"],
            "-p",
            f"127.0.0.1:{graph_port}:9669",
            "--mount",
            f"type=bind,src={store / 'graph'},dst=/data/graph",
            "--mount",
            f"type=bind,src={logs / 'graphd'},dst=/logs",
            images["graphd"],
            f"--meta_server_addrs={LOGICAL_HOSTS['metad']}:9559",
            f"--local_ip={LOGICAL_HOSTS['graphd']}",
            "--ws_ip=0.0.0.0",
            "--port=9669",
            "--ws_http_port=19669",
            "--log_dir=/logs",
        ]
    )


def stop_cluster(docker: Path, owned: dict[str, str]) -> None:
    for role in ("graphd", "storaged", "metad"):
        call(
            [str(docker), "stop", "--time", "120", owned[role]],
            check=False,
            timeout=150,
        )
    cleanup(docker, owned)


def read_truth(path: Path) -> list[dict[str, int]]:
    rows: list[dict[str, int]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
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
    require(len(rows) == 1700, f"truth row count drift: {len(rows)}")
    return rows


def query_observations(
    session: Any,
    helpers: Any,
    truth_rows: list[dict[str, int]],
    output: Path,
) -> None:
    with output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=OBSERVATION_COLUMNS,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for position, row in enumerate(truth_rows):
            result = session.execute(
                f"GO FROM {row['src']} OVER {helpers.rel_type(row['edge_type'])} "
                "YIELD dst(edge) AS dst"
            )
            require(result.is_succeeded(), f"query {position} failed: {result.error_msg()}")
            digest = helpers.Digest()
            for record in result:
                digest.add(record.values()[0].as_int())
            actual = digest.as_dict()
            writer.writerow(
                {
                    "query_index": row["query_index"],
                    "edge_type": row["edge_type"],
                    "src": row["src"],
                    "expected_count": row["count"],
                    "actual_count": actual["count"],
                    "expected_sum_hash": row["sum_hash"],
                    "actual_sum_hash": actual["sum_hash"],
                    "expected_xor_hash": row["xor_hash"],
                    "actual_xor_hash": actual["xor_hash"],
                    "status": "ok",
                }
            )
            if position and position % 100 == 0:
                print(f"correctness_progress={position}/1700", flush=True)


def run(args: argparse.Namespace) -> None:
    repo_root = Path.cwd().resolve()
    require((repo_root / ".git").exists(), "importer cwd must be repository root")
    for path in (
        args.raw_dataset_manifest,
        args.dense_dataset,
        args.truth,
        args.runtime_manifest,
    ):
        require(path.is_absolute() and path.is_file(), f"missing absolute input: {path}")
    for path in (args.store_root, args.importer_report, args.correctness_observations):
        require(path.is_absolute() and not path.exists(), f"output must be absent: {path}")
    require(1 <= args.batch_size <= 5000, "batch size is out of bounds")
    require(1024 <= args.graph_port <= 65535, "graph port is out of bounds")
    require(args.docker.is_file() and os.access(args.docker, os.X_OK),
            "Docker client is unavailable")

    client_root, images = runtime_details(args.runtime_manifest)
    sys.path.insert(0, str(client_root))
    helpers = load_baseline_helpers(repo_root)
    owned = names(args.container_prefix)
    store = args.store_root.resolve(strict=False)
    store.mkdir(mode=0o700)
    for role_dir in ("meta", "storage", "graph"):
        (store / role_dir).mkdir(mode=0o700)
    log_root = Path(tempfile.mkdtemp(prefix="nebula-formal-import-logs-", dir=str(store.parent)))
    for role_dir in ("metad", "storaged", "graphd"):
        (log_root / role_dir).mkdir(mode=0o700)

    pool = None
    session = None
    vertex_count = 0
    edge_count = 0
    cleanup(args.docker, owned)
    try:
        start_cluster(
            args.docker,
            store,
            log_root,
            images,
            owned,
            args.graph_port,
        )
        pool, session = helpers.wait_session(args.graph_port, timeout_s=240)
        edge_types = helpers.discover_edge_types(args.dense_dataset)
        require(len(edge_types) == 34, f"edge type count drift: {len(edge_types)}")
        helpers.setup_schema(session, "sf10", edge_types)
        vertex_count, edge_count, load_seconds = helpers.load_edges(
            session, args.dense_dataset, args.batch_size
        )
        require(vertex_count == 29_987_835, f"vertex count drift: {vertex_count}")
        require(edge_count == 355_185_382, f"edge count drift: {edge_count}")
        print(
            f"load_complete edges={edge_count} seconds={load_seconds:.3f}",
            flush=True,
        )
        query_observations(session, helpers, read_truth(args.truth), args.correctness_observations)
    finally:
        if session is not None:
            session.release()
        if pool is not None:
            pool.close()
        stop_cluster(args.docker, owned)
        shutil.rmtree(log_root, ignore_errors=True)

    report = {
        "schema_version": REPORT_SCHEMA,
        "store_root": str(store),
        "raw_dataset_manifest": file_ref(args.raw_dataset_manifest),
        "dense_dataset": file_ref(args.dense_dataset),
        "truth": file_ref(args.truth),
        "runtime_manifest": file_ref(args.runtime_manifest),
        "loaded": {
            "vertex_count": vertex_count,
            "edge_count": edge_count,
            "edge_type_count": 34,
        },
    }
    args.importer_report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"importer_report={args.importer_report}", flush=True)


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"formal_importer: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
