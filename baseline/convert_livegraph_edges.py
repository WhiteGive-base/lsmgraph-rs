#!/usr/bin/env python3
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


MAPPING_HASH_OFFSET = 0xCBF29CE484222325
MAPPING_HASH_PRIME = 0x100000001B3
MAPPING_HASH_ALGORITHM = "fnv1a64-le-dense-original-v1"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert lsmgraph raw edge triples to the dense LiveGraph driver format."
    )
    parser.add_argument("--input", required=True, help="Raw triples: src edge_type dst")
    parser.add_argument("--output", required=True, help="Dense edge-list output")
    parser.add_argument("--summary", help="Optional JSON summary path")
    parser.add_argument(
        "--id-map-dir",
        help=(
            "Optional directory for versioned original<->dense TSV maps, "
            "their SHA-256 checksums, and id-map-manifest.json"
        ),
    )
    return parser.parse_args()


def read_triple(line, line_no):
    parts = line.split()
    if len(parts) < 3:
        raise ValueError(f"line {line_no}: expected at least 3 columns")
    return int(parts[0]), int(parts[1]), int(parts[2])


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mapping_hash(dense_to_original):
    """Hash canonical (dense_id, original_id) pairs without platform text rules."""
    value = MAPPING_HASH_OFFSET
    for dense_id, original_id in enumerate(dense_to_original):
        for byte in dense_id.to_bytes(8, "little") + original_id.to_bytes(8, "little"):
            value ^= byte
            value = (value * MAPPING_HASH_PRIME) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def write_id_maps(original_to_dense, map_dir):
    map_dir = Path(map_dir)
    map_dir.mkdir(parents=True, exist_ok=True)
    dense_to_original = [0] * len(original_to_dense)
    for original_id, dense_id in original_to_dense.items():
        if original_id < 0 or original_id > 0xFFFFFFFFFFFFFFFF:
            raise ValueError(f"original vertex id out of u64 range: {original_id}")
        dense_to_original[dense_id] = original_id

    original_path = map_dir / "original-to-dense.tsv"
    dense_path = map_dir / "dense-to-original.tsv"
    with original_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("original_id\tdense_id\n")
        for original_id in sorted(original_to_dense):
            fh.write(f"{original_id}\t{original_to_dense[original_id]}\n")
    with dense_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("dense_id\toriginal_id\n")
        for dense_id, original_id in enumerate(dense_to_original):
            fh.write(f"{dense_id}\t{original_id}\n")

    original_sha256 = sha256_file(original_path)
    dense_sha256 = sha256_file(dense_path)
    manifest = {
        "format": "seml0-shared-id-map",
        "format_version": 1,
        "vertex_count": len(dense_to_original),
        "mapping_hash_algorithm": MAPPING_HASH_ALGORITHM,
        "mapping_hash": mapping_hash(dense_to_original),
        "original_to_dense": {
            "path": original_path.name,
            "sha256": original_sha256,
        },
        "dense_to_original": {
            "path": dense_path.name,
            "sha256": dense_sha256,
        },
    }
    manifest_path = map_dir / "id-map-manifest.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    checksums_path = map_dir / "SHA256SUMS"
    with checksums_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{dense_sha256}  {dense_path.name}\n")
        fh.write(f"{original_sha256}  {original_path.name}\n")

    return {
        "directory": str(map_dir),
        "manifest": str(manifest_path),
        "checksums": str(checksums_path),
        **manifest,
    }


def convert_edges(input_path, output_path, summary_path=None, id_map_dir=None):
    dense = {}
    edge_count = 0
    per_type = Counter()

    with open(input_path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            src, edge_type, dst = read_triple(line, line_no)
            if src not in dense:
                dense[src] = len(dense)
            if dst not in dense:
                dense[dst] = len(dense)
            edge_count += 1
            per_type[edge_type] += 1

    with open(input_path, "r", encoding="utf-8") as src_fh, open(
        output_path, "w", encoding="utf-8", newline="\n"
    ) as out:
        out.write(f"{len(dense)}\n")
        for line_no, line in enumerate(src_fh, 1):
            if not line.strip():
                continue
            src, edge_type, dst = read_triple(line, line_no)
            out.write(f"{dense[src]} {edge_type} {dense[dst]}\n")

    summary = {
        "input": str(input_path),
        "output": str(output_path),
        "vertex_count": len(dense),
        "edge_count": edge_count,
        "edge_types": dict(sorted(per_type.items())),
        "id_map": write_id_maps(dense, id_map_dir) if id_map_dir else None,
    }
    if summary_path:
        with open(summary_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True)
            fh.write("\n")
    return summary


def main():
    args = parse_args()
    summary = convert_edges(args.input, args.output, args.summary, args.id_map_dir)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
