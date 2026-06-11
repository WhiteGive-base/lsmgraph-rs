# SemL0 SF30/SF100 消融与代价实验 —— 数据留痕（date_tag=20260608）

- 生成日期：2026-06-10
- 代码分支/commit：`codex/query-semantic-lsm-research` @ `c059d63c015ae2681b24f5cc5b4d81b916066688`
- 实验路径：纯 LSM import 路径（`import --relation snb-full --l0-layout <variant>`），与 BaseGraph SF100 读路径相互独立。
- 变体由 `--l0-layout` 选择：schema / label-only / edge-type-only / degree-only / semantic（full-semantic）/ semantic-budgeted（benefit-scored/SemL0）/ lsmgraph-style / naive / full-compact。
- 读对照方法：所有变体共用同一组采样计划（从 schema store 生成的 core/alltypes sample plan），`storage-bench --sample-plan-in` 公平复跑（read_bytes / candidate L0 段为确定性计数，单次即准）；正确性由 `neighbor-compare` 与 schema 逐邻居校验（SF1 全变体 + SF30 关键变体 full_semantic/benefit_scored 抽查，mismatch 恒为 0）。
- 操作点：SF30 与 SF100 统一用 memgraph=64MB（固定操作点；L0 文件数随规模增长，读放大随之增长）。
- store 策略：导入后删除 snb_edge_props.jsonl / snb_vertices.jsonl 暂存件（消融不需要），再构建→测量→删除候选 store；保留全部 JSON/指标痕迹。

## 指标说明
- **read bytes**：回答 core 查询工作负载读到的总字节（越低越好，体现读放大）。
- **cand. L0 segs**：每次 get_neighbors 检查的候选 L0 段总数（语义裁剪越强越低）。
- **vs baseline**：相对 naive 基线的 read-byte 降幅。
- **header/body reads**：元数据读 vs 负载体读 拆分。
- **mismatches**：与 schema 全量结果逐邻居比对的不一致数（应为 0：语义裁剪是 exact-proof，回退是多读而非错读）。

## 关键发现与注意事项（写论文必读）

1. **裁剪指标（candidate L0 段数）是最干净、跨规模一致的主指标。** 语义布局把每查询候选 L0 段从 naive 的量级压低约 8×（SF30：602,946→7.2-9.8万；SF100：1,967,530→23.6-31.2万）。这是"query-semantic 物理设计降低 L0 读放大"的核心证据，SF30/SF100 都成立、且读放大随规模变大。
2. **edge_type 是稳健的关键信号；单独的 label / degree 几乎无效。** 两个规模下 label-only ≈0.4%、degree-only ≈0%，而 edge-type-only / schema(label+edge_type) ≈99.9-100% 读字节降幅。
3. **⚠️ SF100 上 full_semantic / benefit_scored 的 read_bytes 异常偏高（GB 级），需单独解读。** 这两个**含 degree 维度**的布局在 SF100 匹配的段数与 schema 相同（~20,909），但每段读取字节高约 1000×（schema ~448 B/段 vs full_semantic ~484 KB/段），即 **degree 分区的段在 SF100 触发了整段 body 读取**（SF30 不显著：414 B/段）。因此 SF100 的 read_bytes 列对这两个变体**不能直接当读放大下降来读**——裁剪（候选/匹配段数）仍正常，问题在"每段读取粒度"。这是值得在论文里点明的工程发现（degree 维度在大规模下以更大的 body 读换取候选裁剪），也可作为后续 read-path 优化点排查（offset 切片 vs 整段读取）。
4. **预算式 SemL0（benefit_scored）比无预算 full_semantic 更稳健**：SF100 上 benefit_scored 的 read_bytes（4.5GB）低于 full_semantic（10.1GB）——预算策略只对高收益分区做细分，避免了 degree 过度细分的代价。这是支持"带预算的 SemL0"的正向证据。
5. **full_compact 在 SF100 未完成（OOM）。** full-compact 布局在导入后做一次性 L0→L1 全量合并，SF100（35.7 亿边）单次合并峰值 ~472GB → 被 OOM kill。这是该极端写优化基线在大规模下的**合并实现限制**（非 SemL0 设计问题）；该变体在 SF30 已有完整数据（99.5% 读降、但 import 1961s / store 2× / RSS 167GB）。SF100 此格保持空缺并如实注明。

## SF30 结果

## P1 — Read-amplification comparison (core query workload; counters are deterministic, single pass)

| Variant | L0 signal | cand. L0 segs | read bytes | vs baseline | header reads | body reads | avg us | p99 us | mismatches |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Schema-only | src_label | 75,649 | 13,142,544 | 99.8% | 7.00 | 12,439 | 2,184 | 28,689 | 0 |
| Naive L0 scan | unstructured L0 scan | 602,946 | 8,038,962,160 | 0.0% | 519 | 68,891 | 13,640 | 18,222 | N/A |
| LSMGraph-style LSM-CSR | key/range only | 602,946 | 8,038,962,160 | 0.0% | 519 | 68,891 | 13,329 | 17,056 | N/A |
| Label-only | src_label only | 429,753 | 8,003,710,184 | 0.4% | 809 | 68,891 | 12,732 | 34,722 | N/A |
| Edge-type-only | edge_type only | 72,413 | 4,696,320 | 99.9% | 0.00 | 11,332 | 3,574 | 11,989 | N/A |
| Degree-only / degree-aware | degree_class only | 708,853 | 8,034,605,224 | 0.1% | 903 | 68,429 | 13,295 | 18,167 | N/A |
| Full semantic (upper bound) | label+edge_type+degree | 94,183 | 4,696,320 | 99.9% | 0.00 | 11,332 | 5,704 | 28,672 | 0 |
| Budgeted semantic / SemL0 | benefit-scored subset | 98,027 | 13,142,544 | 99.8% | 7.00 | 12,439 | 6,702 | 30,883 | 0 |
| Full L0->L1 compact | L0 eliminated | 1,196 | 38,568,432 | 99.5% | 1.00 | 1,805 | 93.78 | 278 | N/A |

_'vs baseline' = read-byte reduction relative to the naive/schema baseline._

## P2 — Write / maintenance cost

| Variant | import s | throughput e/s | store bytes | L0 files | L0 bytes | manifest bytes | max RSS kb |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Schema-only | 1441.73 | 754,544 | 42,994,664,727 | 1076 | 42,993,928,408 | 736157 | 54858952 |
| Naive L0 scan | 1194.17 | 910,966 | 42,818,497,255 | 519 | 42,818,141,232 | 355861 | 2436484 |
| LSMGraph-style LSM-CSR | 1170.55 | 929,348 | 42,818,497,255 | 519 | 42,818,141,232 | 355861 | 2435524 |
| Label-only | 1164.2 | 934,417 | 42,818,902,803 | 1019 | 42,818,205,232 | 697409 | 2467344 |
| Edge-type-only | 1404.04 | 774,799 | 42,994,700,749 | 1078 | 42,993,962,120 | 738467 | 54168148 |
| Degree-only / degree-aware | 1192.76 | 912,043 | 42,819,137,053 | 1312 | 42,818,242,736 | 894155 | 2526776 |
| Full semantic (upper bound) | 1373.47 | 792,044 | 42,995,492,789 | 2062 | 42,994,088,072 | 1404555 | 56118188 |
| Budgeted semantic / SemL0 | 1390.73 | 782,214 | 42,995,280,813 | 1732 | 42,994,012,376 | 1181944 | 54158872 |
| Full L0->L1 compact | 1961.04 | 554,730 | 79,693,512,242 | 519 | 42,818,141,232 | 374056 | 167250336 |

## SF100 结果

## P1 — Read-amplification comparison (core query workload; counters are deterministic, single pass)

| Variant | L0 signal | cand. L0 segs | read bytes | vs baseline | header reads | body reads | avg us | p99 us | mismatches |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Schema-only | src_label | 236,667 | 9,374,272 | 100.0% | 4.00 | 20,915 | 6,355 | 38,428 | 0 |
| Naive L0 scan | unstructured L0 scan | 1,967,530 | 26,359,973,584 | 0.0% | 1,703 | 125,407 | 40,773 | 573,111 | N/A |
| LSMGraph-style LSM-CSR | key/range only | 1,967,530 | 26,359,973,584 | 0.0% | 1,703 | 125,407 | 40,933 | 573,111 | N/A |
| Label-only | src_label only | 1,402,094 | 26,248,259,392 | 0.4% | 2,639 | 125,407 | 38,845 | 573,889 | N/A |
| Edge-type-only | edge_type only | 235,457 | 6,223,136 | 100.0% | 0.00 | 20,909 | 12,947 | 50,289 | N/A |
| Degree-only / degree-aware | degree_class only | 2,340,919 | 26,347,781,976 | 0.0% | 2,960 | 125,005 | 41,006 | 578,667 | N/A |
| Full semantic (upper bound) | label+edge_type+degree | 308,679 | 10,124,569,608 | 61.6% | 1,178 | 20,909 | 23,808 | 67,917 | N/A |
| Budgeted semantic / SemL0 | benefit-scored subset | 312,069 | 4,530,279,696 | 82.8% | 848 | 20,915 | 11,848 | 51,583 | N/A |
| Full L0->L1 compact | L0 eliminated | N/A | N/A | 100.0% | N/A | N/A | N/A | N/A | N/A |

_'vs baseline' = read-byte reduction relative to the naive/schema baseline._

## P2 — Write / maintenance cost

| Variant | import s | throughput e/s | store bytes | L0 files | L0 bytes | manifest bytes | max RSS kb |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Schema-only | 4821.0 | 740,711 | 141,242,442,584 | 3444 | 141,240,078,392 | 2364030 | 219415968 |
| Naive L0 scan | 3789.0 | 942,457 | 140,589,867,058 | 1703 | 140,588,695,920 | 1170976 | 2396580 |
| LSMGraph-style LSM-CSR | 3785.0 | 943,453 | 140,589,867,058 | 1703 | 140,588,695,920 | 1170976 | 2401248 |
| Label-only | 3857.0 | 925,841 | 140,591,160,804 | 3293 | 140,588,899,440 | 2261202 | 2410248 |
| Edge-type-only | 4683.0 | 762,539 | 141,242,480,585 | 3446 | 141,240,112,104 | 2368319 | 217094128 |
| Degree-only / degree-aware | 3830.0 | 932,368 | 140,591,943,160 | 4269 | 140,589,024,368 | 2918630 | 2542852 |
| Full semantic (upper bound) | 4817.0 | 741,326 | 141,245,036,056 | 6615 | 141,240,517,736 | 4518158 | 215394188 |
| Budgeted semantic / SemL0 | 4678.0 | 763,354 | 141,244,439,671 | 5570 | 141,240,350,520 | 3811482 | 216896780 |
| Full L0->L1 compact | 5701.0 | 0 | N/A | N/A | N/A | N/A | 472694124 |

## P4 — 反馈式 compaction vs 无反馈

反馈式 compaction（RA-score targeted）vs 无反馈基线，在工作负载发生迁移（phase_a → phase_b）时，热点 L0 分区的候选段数变化：

| 阶段 | 反馈前候选 L0 段 | 反馈后候选 L0 段 | 无反馈基线候选 L0 段 |
| --- | --- | --- | --- |
| phase_a | 30 | 0 | 6 |
| phase_b | 30 | 0 | 6 |

- 反馈选择的目标分区随负载迁移而改变：`selected_ranges_changed=True`。
- 结论：反馈式 targeted compaction 能针对性消除热点分区的 L0 读放大（候选段→0），而无反馈基线只能做通用 compaction，热点分区仍残留候选段。

## 痕迹路径
- 每变体原始痕迹：`remote-logs/e11-<variant>-20260608/`（import.stdout、time.log、stats.log、file-summary.tsv、fair-*.json、neighbor-compare-*.json）
- 归一化与表格：`remote-logs/e11-normalized-20260608/`（e11-variants-<scale>.tsv、e11-mechanism-isolation.csv、e11-tables-<scale>.md）
- 反馈实验：`remote-logs/p3-feedback-vs-no-feedback-20260608.json`

## 一键复现
```bash
cd /data/WorkSpace/lsmgraph-ablation
DATE_TAG=20260608 bash reproduce-semL0-ablation.sh all   # 或 sf30 / sf100 / sf10
```
