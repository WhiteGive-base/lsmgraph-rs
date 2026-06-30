# Removed Table Backup

These tables were removed from the English TeX draft and Chinese reading draft
after the corresponding figures were added.  The data is preserved here for
traceability and for later appendix/table reconstruction if needed.

## W6 SF100 Main Results

All comparable rows checked 45,000 operations with zero mismatches.

| Variant | Candidate L0 | Read MiB | Avg us | L0 files |
|---|---:|---:|---:|---:|
| naive | 49,257,601 | 3,461.6 | 53,513.8 | 1,703 |
| kv-lsm | 49,257,601 | 3,461.6 | 53,892.0 | 1,703 |
| schema | 5,929,197 | 820.0 | 9,816.8 | 3,444 |
| budg-b64 | 6,022,507 | 819.7 | 8,722.1 | 3,483 |
| semantic | 7,782,877 | 642.7 | 8,250.9 | 6,615 |
| oracle | 532,193 | 1,257.8 | 9,723.8 | 1,703 |

Full variant data used in Figure 2 is kept in:

`scripts/plot_cidr_figures.py`

## C2 Lifecycle Retention Under Compaction

Real SF30 read amplification is a metadata-level proxy, not a full body-read
workload.

| Scale | Policy | Retention | Output | Write amp | Read after |
|---|---|---:|---:|---:|---|
| SF1 synth | naive | 0.0 | 1 | 1.22 | 4x blow-up |
| SF1 synth | semantic | 1.0 | 4 | 1.87 | flat |
| SF10c synth | naive | 0.0 | 1 | 1.14 | 6x blow-up |
| SF10c synth | semantic | 1.0 | 6 | 1.85 | flat |
| SF30 real | naive | 0.0 | 503 | 1.07 | 6.52x proxy |
| SF30 real | semantic | 1.0 | 528 | 1.24 | 1.00x proxy |

## Scope-Limited Baselines

External rows passed the count/hash digest gate on the sampled typed-neighbor
workload; LSMGraph-style is an internal layout row, not an official external
LSMGraph artifact.

| System | Scale | Ops | Avg us | P99 us | Disk bytes | Scope |
|---|---|---:|---:|---:|---:|---|
| LiveGraph | SF10 | 32,140 | 391.063 | - | 38,654,705,664 | external storage baseline |
| Aster RocksGraph | SF10 | 1,700 | 1,408.27 | 15,809.60 | 10,401,409,041 | external typed-neighbor bridge |
| Neo4j Community | SF10 | 1,700 | 1,974,806.09 | 34,522,340.13 | 16,664,449,041 | external graph DB baseline |
| TuGraph | SF10 | 1,700 | 3,775.45 | 158,870 | 14,960,539,904 | external embedded API baseline |
| NebulaGraph | SF10 | 1,700 | 744,146.62 | 14,228,169.28 | 46,185,803,519 | external nGQL edge-type model |
| LSMGraph-style | SF100 | 129,485 | 556.741 | 10,000 | 140,589,867,058 | internal layout row |
