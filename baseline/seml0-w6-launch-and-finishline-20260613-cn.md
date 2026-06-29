# 下一步执行卡 + 发表完成线（2026-06-13）

> 一页纸版本。完整工单见 `PLAN-SEML0-SIGMOD2027-CODEX-CN.md`（schema-evolution 核查＝§0.5、
> W13＝§2 Batch C、Gate 5/7＝§3、完成线＝§6）。

## A. 现在状态（已核实）

- 引擎已冻结：commit `324a1e2`、tag `engine-freeze-sigmod2027`；W1-fix/W2 完成；W3/W4/W5 已合并（代码就绪）。
- W6 runner `baseline/run_w6_sf100_matrix_20260613.sh` 已就位并过 SF1 dry-run（9 变体、3 repeats、A/B/A sentinel、compare mismatches=0），锁在 `W6_ALLOW_SF100=1`。
- /data free 561G；待回收的生成 store ~88G（见 B）。
- **schema-evolution 结论**：机制已实现且已测（`src/schema.rs` 全 catalog、段 footer `schema_epoch`、
  `tests/engine_tests.rs:1942` 已证旧段演进后可读+正确；剪枝靠 stable edge_type id 自动跳旧 exact 段）。
  **不破冻结**，落成 W13＝实验+写作。这是几乎零成本的差异化贡献。

## B. 下一步（W6 启动会话的 step 0 → 1，按序）

**step 0 资源回收（删 ~88G，→ ~649G）**——这些是 profile/smoke 残留，删之前各 `du -sh` 复核：
```
store/w2-c10-sf30-schema        41G   ← W2 profile，已出报告，可删
store/w2-c10-sf30-budg-b64      41G   ← 同上
store/w2-c10-sf1-schema         1.4G  store/w2-c10-sf1-budg-b64   1.4G
store/w6-sf1-dryrun-20260613    1.4G  store/w4-smoke-sf1-schema-20260613-0048  1.4G
store/w5-merge-smoke-20260613-0038  698M
```
**保留勿删**：`store/sf{1,10,30,100}-base-graph`（基线）、`store/qslsm-sf100-strong-baseline-20260610`（132G，
旧格式 SF100 参照，W6 全部完成且确认后再问用户删）。

**step 1 发射 W6**（过夜，单-SF100 规约：期间不起任何别的 SF100 任务）：
- 事前 ETA 已知：9 变体 ×（import ~1.3h + bench 3×~5min + compare）≈ 15–20h；磁盘峰值 naive 锚 + 当前变体 ≈ 2×132G，561G→649G 充足。
- 启动：`W6_ALLOW_SF100=1 bash baseline/run_w6_sf100_matrix_20260613.sh`（按 runner 实际入参为准；带 nohup + 心跳 + DONE）。
- abort：单变体 import >3h 或 MemAvailable<80G 或 /data<200G 即停。
- 跑完：用 `summarize_strong_baseline.py` / `extract_funnel_p99.py` 再生全表，**立即判 Gate 1（b64 延迟）+ Gate 2（RSS）** 并写入 `baseline/sf100-matrix-<date>-cn.md`。

## C. W6 之后（均 SF30 级，单-SF100 规约不再约束，可错峰并行）

- **W7**（跑 W3 已写的 workload-shift runner）→ 判 Gate 4
- **W8**（跑 W4 已写的 property+2hop runner）→ 判 Gate 3
- **W9**（跑 W5 已写的稳态 runner）→ 判 Gate 5
- **W13**（写 schema-evolution runner + 实验 + 3 invariant/theorem）→ 判 Gate 7
- **W10**（数据卫生 + claim 安全化 + limitations）→ 写作冻结
- W11/W12 = Tier 3 加分，随缘，不阻塞。

## D. 发表完成线（明确答案）

- **Tier 1 = 最低可投线**：W6 + W13 + W10。关掉全部一票否决项，论文诚实可投（标题可能收窄）。
- **Tier 1+2 = 有竞争力线（推荐目标）**：再加 W7 + W8 + W9。因为这三者**代码已就绪**，只差跑批（小时级），
  边际成本小、收益大（保住 property graph 全义标题 + self-tuning + schema-safe 三贡献）。
- **Tier 3 = 加分**：W11/W12，绝不阻塞。
- 一句话：**做到 Tier 1 能投，做到 Tier 1+2 才值得投。** 顺序 W6→Gate1/2→W7/W8/W9/W13→W10→投；离 10/17 截稿留 ≥1 月缓冲。
