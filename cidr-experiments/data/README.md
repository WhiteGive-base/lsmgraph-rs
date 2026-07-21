# Data

- `source-registry.tsv`：当前完整的 20 条来源、协议、版本、硬件和 `REUSE/FIX/RERUN` 判定；与 `work/evidence-audit/EVIDENCE-REGISTRY.tsv` 字节级一致。
- `normalized/provisional/`：由 `normalize_existing.py` 从旧 raw/summary 重建的诊断 TSV；每行保留 claim boundary。
- `normalized/candidates/`：1,082 条逐 metric provenance 候选，供正式重跑 schema 和旧证据核对；不是正式绘图输入。
- `provenance/`：规范化数据到原始文件/字段的逐项映射。

规则：不能确认来源、查询集合、单位或 correctness gate 的数字不得进入 `normalized/`。缺失值保持为空，不允许推测或补零。

`provisional` 不等同于最终 figure-ready 数据。正式绘图必须使用 [`validate_tidy_data.py`](validate_tidy_data.py) 和 `plan/FIGURE-DATA-REQUIREMENTS.tsv` 的 run-level 契约。
