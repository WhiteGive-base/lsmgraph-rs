#!/usr/bin/env python3
import argparse
import bisect
import json
import os
import struct
import time
from collections import OrderedDict
from pathlib import Path

MIXED_EDGE_TYPE = 0
UNKNOWN_SOURCE_LABEL = 0
EDGE_OFFSET_LEN = 24
DISK_EDGE_BODY_LEN = 32


def source_label(src: int) -> int:
    label = src >> 56
    return label if 1 <= label <= 8 else UNKNOWN_SOURCE_LABEL


def load_manifest(store: Path):
    live = {}
    with (store / "MANIFEST").open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            op = rec.get("op")
            if op == "CreateFile":
                meta = rec["meta"]
                live[int(meta["file_id"])] = meta
            elif op == "DeleteFile":
                live.pop(int(rec["file_id"]), None)
    metas = list(live.values())
    metas.sort(key=lambda m: (
        int(m.get("src_label", 0)),
        int(m.get("edge_type_partition", 0)),
        int(m.get("min_src", 0)),
        int(m.get("file_id", 0)),
    ))
    return metas


def rel_path(meta):
    return Path("levels") / f"L{int(meta.get('level', 0))}" / f"{int(meta['file_id']):012}.edge"


def read_header(path: Path):
    buf = os.pread(os.open(path, os.O_RDONLY), 128, 0)
    if len(buf) < 128:
        raise RuntimeError(f"short header: {path}")
    magic = struct.unpack_from("<I", buf, 0)[0]
    if magic != 0x47534D4C:
        raise RuntimeError(f"bad CSR magic in {path}: {magic:#x}")
    return {
        "edge_offset_count": struct.unpack_from("<Q", buf, 56)[0],
        "edge_body_count": struct.unpack_from("<Q", buf, 64)[0],
        "offsets_offset": struct.unpack_from("<Q", buf, 72)[0],
        "offsets_len": struct.unpack_from("<Q", buf, 80)[0],
        "bodies_offset": struct.unpack_from("<Q", buf, 88)[0],
        "bodies_len": struct.unpack_from("<Q", buf, 96)[0],
    }


class HeaderCache:
    def __init__(self, capacity=4096):
        self.capacity = capacity
        self.cache = OrderedDict()

    def get(self, path: Path):
        key = str(path)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        fd = os.open(path, os.O_RDONLY)
        try:
            buf = os.pread(fd, 128, 0)
        finally:
            os.close(fd)
        if len(buf) < 128:
            raise RuntimeError(f"short header: {path}")
        magic = struct.unpack_from("<I", buf, 0)[0]
        if magic != 0x47534D4C:
            raise RuntimeError(f"bad CSR magic in {path}: {magic:#x}")
        header = {
            "edge_offset_count": struct.unpack_from("<Q", buf, 56)[0],
            "edge_body_count": struct.unpack_from("<Q", buf, 64)[0],
            "offsets_offset": struct.unpack_from("<Q", buf, 72)[0],
            "offsets_len": struct.unpack_from("<Q", buf, 80)[0],
            "bodies_offset": struct.unpack_from("<Q", buf, 88)[0],
            "bodies_len": struct.unpack_from("<Q", buf, 96)[0],
        }
        self.cache[key] = header
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
        return header


def offset_for_src(fd: int, header, src: int):
    count = int(header["edge_offset_count"])
    base = int(header["offsets_offset"])
    lo, hi = 0, count
    while lo < hi:
        mid = (lo + hi) // 2
        buf = os.pread(fd, EDGE_OFFSET_LEN, base + mid * EDGE_OFFSET_LEN)
        if len(buf) != EDGE_OFFSET_LEN:
            raise RuntimeError("short offset read")
        mid_src = struct.unpack_from("<Q", buf, 0)[0]
        if mid_src < src:
            lo = mid + 1
        else:
            hi = mid
    if lo >= count:
        return None
    buf = os.pread(fd, EDGE_OFFSET_LEN, base + lo * EDGE_OFFSET_LEN)
    got_src, first_idx, edge_count = struct.unpack("<QQQ", buf)
    if got_src != src:
        return None
    return first_idx, edge_count


def may_contain(meta, src: int, edge_type: int):
    src_label = int(meta.get("src_label", UNKNOWN_SOURCE_LABEL))
    if src_label not in (UNKNOWN_SOURCE_LABEL, source_label(src),):
        return False
    part = int(meta.get("edge_type_partition", MIXED_EDGE_TYPE))
    if part not in (MIXED_EDGE_TYPE, edge_type):
        return False
    direction = str(meta.get("direction", "unknown"))
    if direction not in ("unknown", "out", "both"):
        return False
    return int(meta.get("min_src", 0)) <= src <= int(meta.get("max_src", 0))


def visible_edges_for_sample(store: Path, metas, header_cache: HeaderCache, src: int, edge_type: int, snapshot: int):
    updates = []
    candidates = 0
    offset_probes = 0
    body_reads = 0
    for meta in metas:
        if not may_contain(meta, src, edge_type):
            continue
        candidates += 1
        path = store / rel_path(meta)
        header = header_cache.get(path)
        fd = os.open(path, os.O_RDONLY)
        try:
            off = offset_for_src(fd, header, src)
            offset_probes += 1
            if off is None:
                continue
            first_idx, edge_count = off
            if edge_count == 0:
                continue
            body_off = int(header["bodies_offset"]) + first_idx * DISK_EDGE_BODY_LEN
            body_len = edge_count * DISK_EDGE_BODY_LEN
            bodies = os.pread(fd, body_len, body_off)
            if len(bodies) != body_len:
                raise RuntimeError(f"short body read in {path}")
            body_reads += 1
        finally:
            os.close(fd)
        for i in range(0, len(bodies), DISK_EDGE_BODY_LEN):
            chunk = bodies[i:i + DISK_EDGE_BODY_LEN]
            dst = struct.unpack_from("<Q", chunk, 0)[0]
            ts = struct.unpack_from("<Q", chunk, 8)[0]
            et = struct.unpack_from("<i", chunk, 24)[0]
            marker = chunk[28]
            if et == edge_type and ts <= snapshot:
                updates.append((src, et, dst, ts, marker))

    latest = {}
    for edge in updates:
        key = (edge[0], edge[1], edge[2])
        old = latest.get(key)
        if old is None or edge[3] > old[3]:
            latest[key] = edge
    visible = sorted((e[2], e[1], e[3]) for e in latest.values() if e[4] == 0)
    return visible, candidates, offset_probes, body_reads


def select_samples(plan, samples_per_edge_type: int):
    entries = []
    for entry in plan["entries"]:
        samples = entry.get("samples", [])
        if samples_per_edge_type > 0:
            samples = samples[:samples_per_edge_type]
        entries.append((int(entry["edge_type"]), samples))
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--left-store", required=True)
    ap.add_argument("--right-store", required=True)
    ap.add_argument("--sample-plan", required=True)
    ap.add_argument("--samples-per-edge-type", type=int, default=100)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-mismatches", type=int, default=1)
    args = ap.parse_args()

    started = time.time()
    left_store = Path(args.left_store)
    right_store = Path(args.right_store)
    with open(args.sample_plan, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    left_metas = load_manifest(left_store)
    right_metas = load_manifest(right_store)
    left_snapshot = max(int(m.get("max_ts", 0)) for m in left_metas)
    right_snapshot = max(int(m.get("max_ts", 0)) for m in right_metas)
    snapshot = min(left_snapshot, right_snapshot)
    left_cache = HeaderCache()
    right_cache = HeaderCache()

    checked = passed = mismatches = 0
    left_edges_total = right_edges_total = 0
    left_candidates = right_candidates = 0
    left_offset_probes = right_offset_probes = 0
    left_body_reads = right_body_reads = 0
    first_mismatch = None
    per_edge_type = []

    for edge_type, samples in select_samples(plan, args.samples_per_edge_type):
        et_checked = et_passed = et_mismatches = 0
        et_left_edges = et_right_edges = 0
        for sample in samples:
            src = int(sample["src"])
            left, lc, lop, lbr = visible_edges_for_sample(
                left_store, left_metas, left_cache, src, edge_type, snapshot
            )
            right, rc, rop, rbr = visible_edges_for_sample(
                right_store, right_metas, right_cache, src, edge_type, snapshot
            )
            checked += 1
            et_checked += 1
            left_edges_total += len(left)
            right_edges_total += len(right)
            et_left_edges += len(left)
            et_right_edges += len(right)
            left_candidates += lc
            right_candidates += rc
            left_offset_probes += lop
            right_offset_probes += rop
            left_body_reads += lbr
            right_body_reads += rbr
            if left == right:
                passed += 1
                et_passed += 1
            else:
                mismatches += 1
                et_mismatches += 1
                if first_mismatch is None:
                    first_mismatch = {
                        "edge_type": edge_type,
                        "src": src,
                        "degree": sample.get("degree"),
                        "left_count": len(left),
                        "right_count": len(right),
                        "left_preview": left[:8],
                        "right_preview": right[:8],
                    }
                if mismatches >= args.max_mismatches:
                    break
        per_edge_type.append({
            "edge_type": edge_type,
            "checked": et_checked,
            "passed": et_passed,
            "mismatches": et_mismatches,
            "left_edges": et_left_edges,
            "right_edges": et_right_edges,
        })
        if mismatches >= args.max_mismatches:
            break

    out = {
        "left_store": str(left_store),
        "right_store": str(right_store),
        "sample_plan": args.sample_plan,
        "samples_per_edge_type": args.samples_per_edge_type,
        "snapshot": snapshot,
        "checked": checked,
        "passed": passed,
        "mismatches": mismatches,
        "left_edges_total": left_edges_total,
        "right_edges_total": right_edges_total,
        "left_candidates": left_candidates,
        "right_candidates": right_candidates,
        "left_offset_probes": left_offset_probes,
        "right_offset_probes": right_offset_probes,
        "left_body_reads": left_body_reads,
        "right_body_reads": right_body_reads,
        "elapsed_s": round(time.time() - started, 3),
        "first_mismatch": first_mismatch,
        "per_edge_type": per_edge_type,
    }
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
