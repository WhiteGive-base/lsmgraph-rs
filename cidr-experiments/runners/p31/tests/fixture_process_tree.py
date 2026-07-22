#!/usr/bin/env python3
"""Tiny process-tree and file-I/O fixture; never a performance benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def write_bytes(path: Path, size: int, seed: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    block = hashlib.sha256(seed).digest() * 4096
    remaining = size
    with path.open("wb") as handle:
        while remaining:
            chunk = block[: min(len(block), remaining)]
            handle.write(chunk)
            remaining -= len(chunk)
        handle.flush()
        os.fsync(handle.fileno())


def keep_alive(seconds: float, allocation_mib: int) -> None:
    allocation = bytearray(allocation_mib * 2**20)
    for offset in range(0, len(allocation), 4096):
        allocation[offset] = offset % 251
    deadline = time.monotonic() + seconds
    digest = b"fixture"
    while time.monotonic() < deadline:
        for _ in range(1000):
            digest = hashlib.sha256(digest).digest()
        time.sleep(0.02)
    if not allocation or not digest:
        raise AssertionError("fixture allocation unexpectedly disappeared")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--temp", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--child", action="store_true")
    return parser.parse_args()


def child(args: argparse.Namespace) -> int:
    write_bytes(args.store / "delta" / "child.data", 192 * 1024, b"child-payload")
    write_bytes(args.temp / "child-spill.tmp", 128 * 1024, b"child-temp")
    keep_alive(args.seconds, 3)
    return 0


def parent(args: argparse.Namespace) -> int:
    if args.ready_file is not None:
        ready = json.loads(args.ready_file.read_text(encoding="utf-8"))
        if ready.get("state") != "READY" or ready.get("resource_sample_index") != 0:
            raise RuntimeError("adapter fixture was released without a first-sample collector gate")
    files = [
        (args.store / "levels" / "base.edge", 1024 * 1024, b"payload"),
        (args.store / "metadata.idx", 256 * 1024, b"metadata"),
        (args.store / "query-signature.sidecar", 128 * 1024, b"sidecar"),
        (args.store / "MANIFEST-000001", 64 * 1024, b"manifest"),
        (args.store / "wal" / "000001.wal", 64 * 1024, b"wal"),
        (args.store / "tmp" / "pending.tmp", 32 * 1024, b"store-temp"),
        (args.temp / "spill.tmp", 512 * 1024, b"external-temp"),
    ]
    for path, size, seed in files:
        write_bytes(path, size, seed)

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
        "--store",
        str(args.store),
        "--temp",
        str(args.temp),
        "--seconds",
        str(args.seconds),
    ]
    process = subprocess.Popen(command)
    keep_alive(args.seconds, 5)
    child_rc = process.wait(timeout=max(args.seconds + 5.0, 10.0))
    return child_rc if child_rc else args.exit_code


def main() -> int:
    args = parse_args()
    if args.seconds <= 0:
        raise SystemExit("--seconds must be positive")
    return child(args) if args.child else parent(args)


if __name__ == "__main__":
    sys.exit(main())
