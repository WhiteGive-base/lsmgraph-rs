#!/usr/bin/env python3
"""Generate one canonical, seed-fixed JSONL operation trace from an LDBC CSV."""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import random
from pathlib import Path

PERSON_LABEL = 1
LABEL_SHIFT = 56
EDGE_TYPE_KNOWS = 1


def encode_person(external_id: int) -> int:
    return (PERSON_LABEL << LABEL_SHIFT) | (external_id & ((1 << LABEL_SHIFT) - 1))


def read_edges(path: Path, limit: int) -> list[tuple[int, int, int]]:
    edges: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int]] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="|")
        header = next(reader, None)
        if not header or len(header) < 2:
            raise ValueError(f"missing two-column header in {path}")
        for row in reader:
            if len(row) < 2:
                continue
            src_external = int(row[0].strip())
            dst_external = int(row[1].strip())
            if src_external == dst_external or (src_external, dst_external) in seen:
                continue
            seen.add((src_external, dst_external))
            edges.append(
                (encode_person(src_external), encode_person(dst_external), EDGE_TYPE_KNOWS)
            )
            if len(edges) >= limit:
                break
    if len(edges) < limit:
        raise ValueError(f"only {len(edges)} distinct edges available, requested {limit}")
    return edges


def emit(records: list[dict[str, int | str]], op: str, **fields: int) -> None:
    records.append({"op": op, "seq": len(records), **fields})


def unique_prefix(values: list[int], limit: int) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def build_trace(
    edges: list[tuple[int, int, int]],
    seed: int,
    flush_every: int,
    hot_query_repeats: int,
    delete_count: int,
) -> tuple[list[dict[str, int | str]], int]:
    rng = random.Random(seed)
    frequencies = collections.Counter(src for src, _, _ in edges)
    hot_src = min(
        (src for src, count in frequencies.items() if count == max(frequencies.values())),
        default=edges[0][0],
    )
    shuffled = list(edges)
    rng.shuffle(shuffled)
    records: list[dict[str, int | str]] = []

    for offset in range(0, len(shuffled), flush_every):
        batch = shuffled[offset : offset + flush_every]
        for src, dst, edge_type in batch:
            emit(records, "insert", src=src, dst=dst, edge_type=edge_type)
        emit(records, "flush")
        targets = unique_prefix([hot_src, *(src for src, _, _ in batch)], 4)
        for _ in range(hot_query_repeats):
            emit(records, "query", src=hot_src, edge_type=EDGE_TYPE_KNOWS)
        for src in targets:
            emit(records, "query", src=src, edge_type=EDGE_TYPE_KNOWS)
        emit(records, "maintenance")
        for src in targets:
            emit(records, "query", src=src, edge_type=EDGE_TYPE_KNOWS)

    if delete_count:
        selected = sorted(rng.sample(range(len(shuffled)), min(delete_count, len(shuffled))))
        deleted = [shuffled[index] for index in selected]
        for src, dst, edge_type in deleted:
            emit(records, "delete", src=src, dst=dst, edge_type=edge_type)
        emit(records, "flush")
        targets = unique_prefix([hot_src, *(src for src, _, _ in deleted)], 8)
        for src in targets:
            emit(records, "query", src=src, edge_type=EDGE_TYPE_KNOWS)
        emit(records, "maintenance")
        for src in targets:
            emit(records, "query", src=src, edge_type=EDGE_TYPE_KNOWS)

    return records, hot_src


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-csv", type=Path, required=True)
    parser.add_argument("--trace-out", type=Path, required=True)
    parser.add_argument("--meta-out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--edge-limit", type=int, default=48)
    parser.add_argument("--flush-every", type=int, default=8)
    parser.add_argument("--hot-query-repeats", type=int, default=2)
    parser.add_argument("--delete-count", type=int, default=6)
    args = parser.parse_args()

    if args.edge_limit <= 0 or args.flush_every <= 0 or args.hot_query_repeats <= 0:
        parser.error("edge-limit, flush-every, and hot-query-repeats must be positive")
    if args.delete_count < 0:
        parser.error("delete-count must be non-negative")
    if args.trace_out.exists() or args.meta_out.exists():
        parser.error("trace-out and meta-out must not already exist")

    edges = read_edges(args.source_csv, args.edge_limit)
    records, hot_src = build_trace(
        edges,
        args.seed,
        args.flush_every,
        args.hot_query_repeats,
        args.delete_count,
    )
    args.trace_out.parent.mkdir(parents=True, exist_ok=True)
    with args.trace_out.open("x", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    op_counts = collections.Counter(str(record["op"]) for record in records)
    metadata = {
        "trace_version": 1,
        "seed": args.seed,
        "source_csv": str(args.source_csv.resolve()),
        "source_csv_sha256": sha256_file(args.source_csv),
        "trace_sha256": sha256_file(args.trace_out),
        "edge_limit": args.edge_limit,
        "flush_every": args.flush_every,
        "hot_query_repeats": args.hot_query_repeats,
        "delete_count": args.delete_count,
        "hot_src": hot_src,
        "operations": len(records),
        "operation_counts": dict(sorted(op_counts.items())),
    }
    with args.meta_out.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(metadata, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
