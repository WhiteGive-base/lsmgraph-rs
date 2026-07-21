#!/usr/bin/env python3
"""Recover a frozen original<->dense ID map from aligned edge lists.

The historical LiveGraph converter assigned dense IDs in first-seen order but
did not persist the map.  This tool recovers that exact map only after pinning
the raw edge list, dense edge list, and historical converter by SHA-256.  It
then streams the two edge lists in lockstep and checks every endpoint against
the converter's first-seen rule before atomically publishing map artifacts.

Any --max-rows run is deliberately a fixture/diagnostic run: its manifest uses
an unsupported partial format, no FORMAL-PASS marker is written, and the
process exits with EXIT_PARTIAL.  It can therefore never be mistaken for a
complete recovery by the existing SemL0 ID-map loader or by shell automation.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import tempfile
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, Optional, Tuple


EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3

UINT64_MAX = (1 << 64) - 1
MAPPING_HASH_OFFSET = 0xCBF29CE484222325
MAPPING_HASH_PRIME = 0x100000001B3
MAPPING_HASH_ALGORITHM = "fnv1a64-le-dense-original-v1"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# These values identify the preserved input bytes used to create the archived
# SF10 dense truth.  Counts are also pinned so a valid hash cannot accidentally
# be paired with a command intended for another scale factor.
LINEAGE_PROFILES = {
    "sf10-20260624": {
        "raw_sha256": "85a1ea5e2e93692f487919be56b2c7a6e1e435fc00af9463b3ddf2960cc15d9e",
        "dense_sha256": "9727fed3d710a3f0897b1ee7379411a091e0568bef05902de8b1787efc39d258",
        "converter_sha256": "4d0448ab8b4d9abdb93bb6de0a85ac6ff1568d3f53181921b93a0a08e10974b0",
        "vertex_count": 29_987_835,
        "edge_count": 355_185_382,
    }
}


class RecoveryError(RuntimeError):
    """A lineage or lockstep validation failed."""


@dataclass(frozen=True)
class ExpectedLineage:
    profile: Optional[str]
    raw_sha256: str
    dense_sha256: str
    converter_sha256: str
    vertex_count: Optional[int]
    edge_count: Optional[int]


@dataclass(frozen=True)
class InputFingerprint:
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class LogicalLine:
    physical_line: int
    text: str


class HashingLogicalReader:
    """Read non-empty ASCII lines while hashing every physical input byte."""

    def __init__(self, path: Path):
        self.path = path
        self._fh: Optional[BinaryIO] = None
        self._digest = hashlib.sha256()
        self.physical_line = 0
        self.eof = False

    def __enter__(self) -> "HashingLogicalReader":
        self._fh = self.path.open("rb", buffering=1024 * 1024)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._fh is not None:
            self._fh.close()

    def next(self) -> Optional[LogicalLine]:
        if self._fh is None:
            raise RuntimeError("reader is not open")
        while True:
            raw = self._fh.readline()
            if raw == b"":
                self.eof = True
                return None
            self.physical_line += 1
            self._digest.update(raw)
            if not raw.strip():
                continue
            try:
                text = raw.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise RecoveryError(
                    f"{self.path}:{self.physical_line}: non-ASCII input"
                ) from exc
            return LogicalLine(self.physical_line, text)

    def hexdigest(self) -> str:
        if not self.eof:
            raise RecoveryError(f"cannot finalize incomplete hash for {self.path}")
        return self._digest.hexdigest()


class MappingState:
    """In-memory map with compact dense-order storage and first-seen checks."""

    def __init__(self, header_vertex_count: int):
        self.header_vertex_count = header_vertex_count
        self.original_to_dense: Dict[int, int] = {}
        self.dense_to_original = array("Q")
        self._mapping_hash = MAPPING_HASH_OFFSET

    def observe(self, original_id: int, dense_id: int, context: str) -> None:
        if not 0 <= original_id <= UINT64_MAX:
            raise RecoveryError(f"{context}: original ID {original_id} is outside u64")
        if dense_id < 0 or dense_id >= self.header_vertex_count:
            raise RecoveryError(
                f"{context}: dense ID {dense_id} is outside header range "
                f"[0, {self.header_vertex_count})"
            )

        previous = self.original_to_dense.get(original_id)
        if previous is not None:
            if previous != dense_id:
                raise RecoveryError(
                    f"{context}: original ID {original_id} changed dense ID "
                    f"from {previous} to {dense_id}"
                )
            return

        expected_dense = len(self.dense_to_original)
        if dense_id != expected_dense:
            raise RecoveryError(
                f"{context}: first-seen original ID {original_id} must receive "
                f"next dense ID {expected_dense}, found {dense_id}"
            )
        self.original_to_dense[original_id] = dense_id
        self.dense_to_original.append(original_id)
        self._hash_pair(dense_id, original_id)

    def _hash_pair(self, dense_id: int, original_id: int) -> None:
        payload = dense_id.to_bytes(8, "little") + original_id.to_bytes(8, "little")
        value = self._mapping_hash
        for byte in payload:
            value ^= byte
            value = (value * MAPPING_HASH_PRIME) & UINT64_MAX
        self._mapping_hash = value

    @property
    def vertex_count(self) -> int:
        return len(self.dense_to_original)

    @property
    def mapping_hash(self) -> str:
        return f"{self._mapping_hash:016x}"


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed recovery of the historical first-seen dense ID map "
            "from frozen raw and dense edge lists."
        )
    )
    parser.add_argument("--raw", required=True, type=Path, help="Frozen raw triples")
    parser.add_argument(
        "--dense", required=True, type=Path, help="Frozen dense list with vertex header"
    )
    parser.add_argument(
        "--converter", required=True, type=Path, help="Historical converter source"
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--lineage-profile", choices=sorted(LINEAGE_PROFILES))
    parser.add_argument("--expected-raw-sha256")
    parser.add_argument("--expected-dense-sha256")
    parser.add_argument("--expected-converter-sha256")
    parser.add_argument("--expected-vertex-count", type=int)
    parser.add_argument("--expected-edge-count", type=int)
    parser.add_argument(
        "--max-rows",
        type=int,
        help=(
            "Fixture/diagnostic prefix only. Always emits PARTIAL_NOT_FORMAL, "
            "never FORMAL-PASS, and exits with status 3."
        ),
    )
    args = parser.parse_args(argv)
    if args.max_rows is not None and args.max_rows <= 0:
        parser.error("--max-rows must be positive")
    if args.expected_vertex_count is not None and args.expected_vertex_count < 0:
        parser.error("--expected-vertex-count must be non-negative")
    if args.expected_edge_count is not None and args.expected_edge_count < 0:
        parser.error("--expected-edge-count must be non-negative")
    return args


def normalize_sha256(value: Optional[str], option: str) -> Optional[str]:
    if value is None:
        return None
    if not SHA256_RE.fullmatch(value):
        raise RecoveryError(f"{option} must be exactly 64 hexadecimal characters")
    return value.lower()


def resolve_expected(args: argparse.Namespace) -> ExpectedLineage:
    supplied = {
        "raw_sha256": normalize_sha256(args.expected_raw_sha256, "--expected-raw-sha256"),
        "dense_sha256": normalize_sha256(
            args.expected_dense_sha256, "--expected-dense-sha256"
        ),
        "converter_sha256": normalize_sha256(
            args.expected_converter_sha256, "--expected-converter-sha256"
        ),
        "vertex_count": args.expected_vertex_count,
        "edge_count": args.expected_edge_count,
    }
    profile = LINEAGE_PROFILES.get(args.lineage_profile, {})
    resolved = {}
    for key in ("raw_sha256", "dense_sha256", "converter_sha256"):
        profile_value = profile.get(key)
        supplied_value = supplied[key]
        if profile_value is not None and supplied_value not in (None, profile_value):
            raise RecoveryError(
                f"--lineage-profile {args.lineage_profile} pins {key}={profile_value}; "
                f"refusing override {supplied_value}"
            )
        resolved[key] = profile_value or supplied_value
        if resolved[key] is None:
            raise RecoveryError(
                "provide --lineage-profile or all three --expected-*-sha256 values"
            )

    for key in ("vertex_count", "edge_count"):
        profile_value = profile.get(key)
        supplied_value = supplied[key]
        if profile_value is not None and supplied_value not in (None, profile_value):
            raise RecoveryError(
                f"--lineage-profile {args.lineage_profile} pins {key}={profile_value}; "
                f"refusing override {supplied_value}"
            )
        resolved[key] = profile_value if profile_value is not None else supplied_value

    return ExpectedLineage(profile=args.lineage_profile, **resolved)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb", buffering=1024 * 1024) as fh:
            for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RecoveryError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def fingerprint(path: Path, expected_sha256: str, label: str) -> InputFingerprint:
    try:
        if not path.is_file():
            raise RecoveryError(f"{label} is not a regular file: {path}")
        size = path.stat().st_size
    except OSError as exc:
        raise RecoveryError(f"cannot stat {label} {path}: {exc}") from exc
    actual = sha256_file(path)
    if not hmac.compare_digest(actual, expected_sha256):
        raise RecoveryError(
            f"{label} SHA-256 mismatch: expected {expected_sha256}, found {actual} ({path})"
        )
    return InputFingerprint(path=path.resolve(), size_bytes=size, sha256=actual)


def parse_header(line: LogicalLine, dense_path: Path) -> int:
    parts = line.text.split()
    if len(parts) != 1:
        raise RecoveryError(
            f"{dense_path}:{line.physical_line}: dense header must contain one integer"
        )
    try:
        value = int(parts[0])
    except ValueError as exc:
        raise RecoveryError(
            f"{dense_path}:{line.physical_line}: invalid vertex-count header"
        ) from exc
    if value < 0:
        raise RecoveryError(
            f"{dense_path}:{line.physical_line}: negative vertex-count header {value}"
        )
    return value


def parse_raw_triple(line: LogicalLine, path: Path) -> Tuple[int, int, int]:
    parts = line.text.split()
    if len(parts) < 3:
        raise RecoveryError(f"{path}:{line.physical_line}: expected at least 3 columns")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError as exc:
        raise RecoveryError(f"{path}:{line.physical_line}: invalid integer triple") from exc


def parse_dense_triple(line: LogicalLine, path: Path) -> Tuple[int, int, int]:
    parts = line.text.split()
    if len(parts) != 3:
        raise RecoveryError(f"{path}:{line.physical_line}: expected exactly 3 columns")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError as exc:
        raise RecoveryError(f"{path}:{line.physical_line}: invalid integer triple") from exc


def verify_lockstep(
    raw_path: Path,
    dense_path: Path,
    expected: ExpectedLineage,
    max_rows: Optional[int],
) -> Tuple[MappingState, dict]:
    with HashingLogicalReader(raw_path) as raw_reader, HashingLogicalReader(
        dense_path
    ) as dense_reader:
        header_line = dense_reader.next()
        if header_line is None:
            raise RecoveryError(f"dense edge list is empty: {dense_path}")
        header_vertex_count = parse_header(header_line, dense_path)
        if (
            expected.vertex_count is not None
            and header_vertex_count != expected.vertex_count
        ):
            raise RecoveryError(
                f"dense header vertex count mismatch: expected {expected.vertex_count}, "
                f"found {header_vertex_count}"
            )

        state = MappingState(header_vertex_count)
        edge_rows = 0
        reached_both_eof = False

        while max_rows is None or edge_rows < max_rows:
            raw_line = raw_reader.next()
            dense_line = dense_reader.next()
            if raw_line is None and dense_line is None:
                reached_both_eof = True
                break
            if raw_line is None:
                raise RecoveryError(
                    f"raw edge list ended after {edge_rows} rows but dense has "
                    f"extra data at physical line {dense_line.physical_line}"
                )
            if dense_line is None:
                raise RecoveryError(
                    f"dense edge list ended after {edge_rows} rows but raw has "
                    f"extra data at physical line {raw_line.physical_line}"
                )

            original_src, raw_type, original_dst = parse_raw_triple(raw_line, raw_path)
            dense_src, dense_type, dense_dst = parse_dense_triple(dense_line, dense_path)
            logical_row = edge_rows + 1
            if raw_type != dense_type:
                raise RecoveryError(
                    f"edge row {logical_row}: type mismatch raw={raw_type} dense={dense_type} "
                    f"({raw_path}:{raw_line.physical_line}, "
                    f"{dense_path}:{dense_line.physical_line})"
                )
            state.observe(original_src, dense_src, f"edge row {logical_row} source")
            state.observe(original_dst, dense_dst, f"edge row {logical_row} destination")
            edge_rows += 1

        partial_requested = max_rows is not None
        if not partial_requested:
            if not reached_both_eof:
                raise RecoveryError("internal error: formal run stopped before both inputs reached EOF")
            raw_second_sha = raw_reader.hexdigest()
            dense_second_sha = dense_reader.hexdigest()
            if not hmac.compare_digest(raw_second_sha, expected.raw_sha256):
                raise RecoveryError(
                    "raw SHA-256 changed between preflight and lockstep validation: "
                    f"expected {expected.raw_sha256}, found {raw_second_sha}"
                )
            if not hmac.compare_digest(dense_second_sha, expected.dense_sha256):
                raise RecoveryError(
                    "dense SHA-256 changed between preflight and lockstep validation: "
                    f"expected {expected.dense_sha256}, found {dense_second_sha}"
                )
            if edge_rows != (expected.edge_count if expected.edge_count is not None else edge_rows):
                raise RecoveryError(
                    f"edge-count mismatch: expected {expected.edge_count}, found {edge_rows}"
                )
            if state.vertex_count != header_vertex_count:
                raise RecoveryError(
                    f"recovered vertex-count mismatch: dense header={header_vertex_count}, "
                    f"recovered={state.vertex_count}"
                )

        validation = {
            "partial_requested": partial_requested,
            "max_rows": max_rows,
            "verified_edge_rows": edge_rows,
            "dense_header_vertex_count": header_vertex_count,
            "recovered_vertex_count": state.vertex_count,
            "reached_raw_eof": raw_reader.eof,
            "reached_dense_eof": dense_reader.eof,
            "input_sha256_preflight": "PASS",
            "input_sha256_during_lockstep": (
                "NOT_RUN_PARTIAL" if partial_requested else "PASS"
            ),
            "edge_type_lockstep": "PASS",
            "endpoint_lockstep": "PASS",
            "first_seen_dense_order": "PASS",
            "prefix_bijection": "PASS",
            "complete_edge_count": "NOT_RUN_PARTIAL" if partial_requested else "PASS",
            "complete_vertex_count": "NOT_RUN_PARTIAL" if partial_requested else "PASS",
        }
        return state, validation


def write_text_map(path: Path, header: str, rows: Iterable[str]) -> None:
    with path.open("w", encoding="ascii", newline="\n", buffering=1024 * 1024) as fh:
        fh.write(header)
        for row in rows:
            fh.write(row)


def publish_maps(
    output_dir: Path,
    state: MappingState,
    validation: dict,
    expected: ExpectedLineage,
    inputs: Dict[str, InputFingerprint],
) -> dict:
    partial = validation["partial_requested"]
    if os.path.lexists(output_dir):
        raise RecoveryError(f"output directory already exists; refusing overwrite: {output_dir}")

    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=parent))
    published = False
    try:
        dense_name = "dense-to-original.tsv"
        original_name = "original-to-dense.tsv"
        dense_path = temp_dir / dense_name
        original_path = temp_dir / original_name

        write_text_map(
            dense_path,
            "dense_id\toriginal_id\n",
            (f"{dense_id}\t{original_id}\n" for dense_id, original_id in enumerate(state.dense_to_original)),
        )
        write_text_map(
            original_path,
            "original_id\tdense_id\n",
            (
                f"{original_id}\t{state.original_to_dense[original_id]}\n"
                for original_id in sorted(state.original_to_dense)
            ),
        )
        dense_sha256 = sha256_file(dense_path)
        original_sha256 = sha256_file(original_path)

        status = "PARTIAL_NOT_FORMAL" if partial else "PASS"
        manifest = {
            "format": (
                "seml0-shared-id-map-partial" if partial else "seml0-shared-id-map"
            ),
            "format_version": 1,
            "status": status,
            "formal_pass": not partial,
            "verification_complete": not partial,
            "vertex_count": state.vertex_count,
            "mapping_hash_algorithm": MAPPING_HASH_ALGORITHM,
            "mapping_hash": state.mapping_hash,
            "original_to_dense": {
                "path": original_name,
                "sha256": original_sha256,
                "size_bytes": original_path.stat().st_size,
            },
            "dense_to_original": {
                "path": dense_name,
                "sha256": dense_sha256,
                "size_bytes": dense_path.stat().st_size,
            },
            "recovery": {
                "method": "sha256-pinned-lockstep-first-seen-v1",
                "lineage_profile": expected.profile,
                "expected_vertex_count": expected.vertex_count,
                "expected_edge_count": expected.edge_count,
                "validation": validation,
                "inputs": {
                    label: {
                        "path": str(item.path),
                        "size_bytes": item.size_bytes,
                        "sha256": item.sha256,
                    }
                    for label, item in sorted(inputs.items())
                },
            },
        }
        manifest_path = temp_dir / "id-map-manifest.json"
        with manifest_path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.write("\n")
        manifest_sha256 = sha256_file(manifest_path)

        checksums_path = temp_dir / "SHA256SUMS"
        with checksums_path.open("w", encoding="ascii", newline="\n") as fh:
            fh.write(f"{dense_sha256}  {dense_name}\n")
            fh.write(f"{manifest_sha256}  id-map-manifest.json\n")
            fh.write(f"{original_sha256}  {original_name}\n")

        if partial:
            (temp_dir / "PARTIAL-NOT-FORMAL").write_text(
                "This directory contains a --max-rows prefix fixture only.\n"
                "It is not a complete ID map and must not be used for formal results.\n",
                encoding="ascii",
            )
        else:
            (temp_dir / "FORMAL-PASS").write_text(
                f"id-map-manifest.json sha256 {manifest_sha256}\n", encoding="ascii"
            )

        os.replace(temp_dir, output_dir)
        published = True
        return {
            "status": status,
            "formal_pass": not partial,
            "output_dir": str(output_dir.resolve()),
            "manifest_sha256": manifest_sha256,
            "mapping_hash": state.mapping_hash,
            "vertex_count": state.vertex_count,
            "verified_edge_rows": validation["verified_edge_rows"],
        }
    finally:
        if not published and temp_dir.exists():
            shutil.rmtree(temp_dir)


def run(args: argparse.Namespace) -> Tuple[int, dict]:
    expected = resolve_expected(args)
    output_dir = args.output_dir.resolve()
    if os.path.lexists(output_dir):
        raise RecoveryError(f"output directory already exists; refusing overwrite: {output_dir}")

    raw_path = args.raw.resolve()
    dense_path = args.dense.resolve()
    converter_path = args.converter.resolve()
    inputs = {
        "converter": fingerprint(
            converter_path, expected.converter_sha256, "historical converter"
        ),
        "dense": fingerprint(dense_path, expected.dense_sha256, "dense edge list"),
        "raw": fingerprint(raw_path, expected.raw_sha256, "raw edge list"),
    }

    state, validation = verify_lockstep(raw_path, dense_path, expected, args.max_rows)
    # The converter is small, so pin it again immediately before publication.
    converter_second_sha = sha256_file(converter_path)
    if not hmac.compare_digest(converter_second_sha, expected.converter_sha256):
        raise RecoveryError(
            "historical converter changed after preflight: "
            f"expected {expected.converter_sha256}, found {converter_second_sha}"
        )
    result = publish_maps(output_dir, state, validation, expected, inputs)
    return (EXIT_PARTIAL if args.max_rows is not None else 0), result


def main(argv: Optional[Iterable[str]] = None) -> int:
    try:
        args = parse_args(argv)
        exit_code, result = run(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        if exit_code == EXIT_PARTIAL:
            print(
                "PARTIAL_NOT_FORMAL: --max-rows output is fixture-only; "
                f"exiting {EXIT_PARTIAL}",
                file=sys.stderr,
            )
        return exit_code
    except RecoveryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("ERROR: interrupted; no formal output was published", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
