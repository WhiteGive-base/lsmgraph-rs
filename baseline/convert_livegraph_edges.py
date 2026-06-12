#!/usr/bin/env python3
import argparse
import json
from collections import Counter


def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert lsmgraph raw edge triples to the dense LiveGraph driver format."
    )
    parser.add_argument("--input", required=True, help="Raw triples: src edge_type dst")
    parser.add_argument("--output", required=True, help="Dense edge-list output")
    parser.add_argument("--summary", help="Optional JSON summary path")
    return parser.parse_args()


def read_triple(line, line_no):
    parts = line.split()
    if len(parts) < 3:
        raise ValueError(f"line {line_no}: expected at least 3 columns")
    return int(parts[0]), int(parts[1]), int(parts[2])


def main():
    args = parse_args()
    dense = {}
    edge_count = 0
    per_type = Counter()

    with open(args.input, "r", encoding="utf-8") as fh:
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

    with open(args.input, "r", encoding="utf-8") as src_fh, open(
        args.output, "w", encoding="utf-8"
    ) as out:
        out.write(f"{len(dense)}\n")
        for line_no, line in enumerate(src_fh, 1):
            if not line.strip():
                continue
            src, edge_type, dst = read_triple(line, line_no)
            out.write(f"{dense[src]} {edge_type} {dense[dst]}\n")

    summary = {
        "input": args.input,
        "output": args.output,
        "vertex_count": len(dense),
        "edge_count": edge_count,
        "edge_types": dict(sorted(per_type.items())),
    }
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
            fh.write("\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
