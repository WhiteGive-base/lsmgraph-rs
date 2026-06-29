# 99 Appendix 中文版

## Claim → evidence map

- C1 read-amp/candidate reduction：`baseline/sf100-matrix-20260613-cn.md`，`baseline/w8-property-2hop-summary-20260615-cn.md`。
- C2 lifecycle retention：`baseline/path-b-c2/stage6-gate-verdict-20260618-cn.md`，`baseline/path-b-c2/stage5-sf30-real-summary-20260619-cn.md`，`remote-logs/c2-sf30-real-20260619/{naive,semantic}.json`。
- C3 schema/snapshot correctness：`baseline/w13-schema-evolution-summary-20260614.md`。
- External baseline：`remote-logs/livegraph-sf10-20260612/`。

## Reproduction pointers

- `cargo test --lib`
- `cargo test --test engine_tests`
- `cargo run --bin c2-merge-retention -- --sources <N> --edge-types <list> --output <json>`

## Planned figures

- Pipeline / lifecycle diagram。
- Schema-snapshot timeline。
- C2 retention before/after + cost bar。

