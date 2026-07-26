# Figure 1 / E01 只读盘点

盘点日期：2026-07-26（Windows 仅作仓库镜像；没有在此目录启动实验）。

## 冻结入口

* renderer：`cidr-experiments/figures/scripts/formal/plot_figure1_end_to_end.py`
* common loader/statistics：`cidr-experiments/figures/scripts/formal/plot_support.py`
* field contract：`cidr-experiments/plan/FIGURE-DATA-REQUIREMENTS.tsv`
* figure specification：`cidr-experiments/plan/FIGURE-SPEC-CN.md`

renderer 的固定顺序是 `SemL0`、`SemL0-naive`、`LiveGraph`、`Aster RocksGraph`、
`TuGraph`、`NebulaGraph`、`Neo4j`。它要求每组至少三个独立 `run_id`，锁定
`dataset_id/input_sha256/workload_id/query_trace_sha256/directed_edge_count/
cache_state/concurrency/interface_scope/host_fingerprint`，并将 SemL0 与
SemL0-naive 视为同一底层引擎来锁定 `git_sha/binary_sha256/system_version`。
P50/P95/P99 必须单调，`completed_qps` 必须与
`completed_queries/measurement_s` 在 1% 内一致，load/build time 和 final disk
必须为正。

renderer 本身不是 admission gate：`plot_support.load_rows()` 会先筛掉
digest-failed/mismatching rows，之后才聚合；它没有能力识别旧 conditional
manifest、FAILED/SUPERSEDED marker、P31/lease lineage 或 18+3 选择性拼接。
因此 E01 v2 normalizer 在 renderer 之前严格拒绝这些情况，并把全部
`formal_eligible/performance_eligible` 绑定到 fresh formal evidence；即使
normalization PASS，`paper_claim_eligible` 仍保持 `false`，等待独立 Figure/QA
claim receipt。

## 旧 L5 evidence

只读来源为
`cidr-experiments/l6-control/sample-l5/L5-CONDITIONAL-COMBINED.json`，
SHA-256=`3996ab4b8d99bb192904db850e418f4d66b518be6d771cec2e8fbcc9340c9138`。
它是六系统×三重复（18 cells），缺 `SemL0-naive:r1-3`，且
`formal_eligible=false`、`performance_eligible=false`、
`paper_claim_eligible=false`。各 cell 的 validated/P31/DONE/FAILED lineage
见 `existing-evidence-inventory.json`；该 inventory 只登记事实，不复制 raw，
也不把旧数据交给 normalizer。

## SemL0-naive 缺口

仓库没有可接受的 E01 v2 naive 三重复 formal manifest、独立 cost receipt 或
fresh formal P31/lease lineage。因而当前只能准备合同和负向测试；任何
`18 old + 3 naive` 组合都被明确标记为 forbidden，必须重新取得 fresh gate/lease
后按固定 21-run 顺序执行。
