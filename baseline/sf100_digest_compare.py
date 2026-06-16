#!/usr/bin/env python3
"""Sampled differential correctness check from storage-bench result digests.

This is the W14 Step B compare path. Instead of re-opening two SF100 engines through
`neighbor-compare` (which paid the manifest/index/semantic-index open cost twice per
scenario), we compare the per-sample `result_digest` that `storage-bench
--emit-result-digests` already embeds in each variant's bench JSON.

Contract (kept intentionally honest):
  * This is *sampled differential validation*, not a formal proof. `mismatches=0` means
    "every sampled query returned the same visible edge set as the schema baseline", and
    must be reported as such.
  * The schema bench JSON is the ground-truth baseline; each variant must reproduce it
    exactly for every sampled (edge_type, src) key.

A sample is keyed by (edge_type, src). For each key we require:
  * the key exists on both sides (no dropped / extra samples),
  * identical `result_count`,
  * identical `result_digest`.

Exit status is non-zero when checked==0 or mismatches>0 so the W14 runner's `fail` trips.
"""
import argparse
import json
import sys
from pathlib import Path


def load_digest_index(path: Path):
    """Return (index, entry_digests, meta).

    index: {(edge_type, src): {"result_count", "result_digest", "degree",
                               "dst_label", "property_predicate_mode"}}
    entry_digests: {edge_type: entry_result_digest}
    """
    data = json.loads(path.read_text())
    if not data.get("emit_result_digests", False):
        raise SystemExit(
            f"{path}: bench JSON has emit_result_digests != true; "
            "re-run storage-bench with --emit-result-digests"
        )
    index = {}
    entry_digests = {}
    for entry in data.get("benchmarks", []):
        edge_type = entry.get("edge_type")
        entry_digests[json.dumps(edge_type)] = entry.get("entry_result_digest")
        digests = entry.get("result_digests")
        if digests is None:
            raise SystemExit(
                f"{path}: benchmark entry edge_type={edge_type} has no result_digests"
            )
        for sample in digests:
            key = (json.dumps(sample.get("edge_type")), sample.get("src"))
            if key in index:
                raise SystemExit(f"{path}: duplicate sample key {key}")
            index[key] = {
                "result_count": sample.get("result_count"),
                "result_digest": sample.get("result_digest"),
                "degree": sample.get("degree"),
                "dst_label": sample.get("dst_label"),
                "property_predicate_mode": sample.get("property_predicate_mode"),
            }
    meta = {
        "data_dir": data.get("data_dir"),
        "property_predicate_mode": data.get("property_predicate_mode"),
        "semantic_degree_hint": data.get("semantic_degree_hint"),
        "force_signature": data.get("force_signature"),
    }
    return index, entry_digests, meta


def compare(baseline_path: Path, variant_path: Path, max_mismatches: int):
    base_index, base_entry, base_meta = load_digest_index(baseline_path)
    var_index, var_entry, var_meta = load_digest_index(variant_path)

    base_keys = set(base_index)
    var_keys = set(var_index)
    missing_in_variant = sorted(base_keys - var_keys)
    extra_in_variant = sorted(var_keys - base_keys)

    checked = 0
    mismatches = 0
    first_mismatch = None
    mismatch_examples = []

    for key in sorted(base_keys & var_keys):
        checked += 1
        base = base_index[key]
        var = var_index[key]
        if (
            base["result_count"] != var["result_count"]
            or base["result_digest"] != var["result_digest"]
        ):
            mismatches += 1
            example = {
                "edge_type": json.loads(key[0]),
                "src": key[1],
                "degree": base.get("degree"),
                "dst_label": base.get("dst_label"),
                "property_predicate_mode": base.get("property_predicate_mode"),
                "baseline_count": base["result_count"],
                "variant_count": var["result_count"],
                "baseline_digest": base["result_digest"],
                "variant_digest": var["result_digest"],
            }
            if first_mismatch is None:
                first_mismatch = example
            if len(mismatch_examples) < max(1, max_mismatches):
                mismatch_examples.append(example)

    # Key-set mismatches count as failures too (a dropped / extra sample is a correctness bug).
    keyset_mismatch = len(missing_in_variant) + len(extra_in_variant)
    mismatches += keyset_mismatch

    # Fast entry-level cross check (a single digest per edge_type).
    entry_mismatches = []
    for edge_type, digest in base_entry.items():
        if var_entry.get(edge_type) != digest:
            entry_mismatches.append(
                {
                    "edge_type": json.loads(edge_type),
                    "baseline_entry_digest": digest,
                    "variant_entry_digest": var_entry.get(edge_type),
                }
            )

    summary = {
        "baseline": str(baseline_path),
        "variant": str(variant_path),
        "baseline_data_dir": base_meta.get("data_dir"),
        "variant_data_dir": var_meta.get("data_dir"),
        "checked": checked,
        "mismatches": mismatches,
        "sample_value_mismatches": mismatches - keyset_mismatch,
        "missing_in_variant": missing_in_variant[:20],
        "extra_in_variant": extra_in_variant[:20],
        "missing_in_variant_total": len(missing_in_variant),
        "extra_in_variant_total": len(extra_in_variant),
        "entry_digest_mismatches": entry_mismatches,
        "first_mismatch": first_mismatch,
        "mismatch_examples": mismatch_examples,
        "verdict": "PASS" if (checked > 0 and mismatches == 0) else "FAIL",
    }
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path,
                        help="schema bench JSON (ground-truth baseline)")
    parser.add_argument("--variant", required=True, type=Path,
                        help="variant bench JSON to validate against the baseline")
    parser.add_argument("--max-mismatches", type=int, default=1,
                        help="number of mismatch examples to retain in the report")
    parser.add_argument("--out", type=Path, default=None,
                        help="optional path to write the JSON summary")
    args = parser.parse_args()

    summary = compare(args.baseline, args.variant, args.max_mismatches)
    text = json.dumps(summary, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    print(text)

    if summary["checked"] <= 0:
        print("FAIL: checked=0 (no comparable samples)", file=sys.stderr)
        return 2
    if summary["mismatches"] != 0:
        print(f"FAIL: mismatches={summary['mismatches']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
