# W14 SF100 Split Matrix + Tuning Progress

Last update: 2026-06-16 22:21 CST

## 当前覆盖状态

当前阶段：`Step B SF100 minimal scenario matrix FAILED at type-only semantic compare timeout`。


| Done | 阶段                                     | 状态          | 执行时间/耗时                                                                                                                                                | 产物/日志                                                       | 下一步                                                                |
| ---- | -------------------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------- | ------------------------------------------------------------------ |
| [x]  | SF1 A/B tuning validation              | DONE        | baseline 1:34；tuned 0:42                                                                                                                               | `remote-logs/w14-tuning-sf1-ab-20260616-codex1`             | 作为提速 smoke 证据                                                      |
| [x]  | SF30 tuned import smoke                | DONE        | 19:43                                                                                                                                                  | `remote-logs/w14-tuning-sf30-edge-import-20260616-codex1`   | 作为 SF100 import 估时依据                                               |
| [x]  | runner tuning patch                    | DONE        | 分钟级；未单独计时                                                                                                                                              | `baseline/run_w14_semantic_necessity_20260615.sh`           | 已通过 `bash -n`                                                      |
| [x]  | Step A: SF100 tuned import-only matrix | DONE        | 总计约 4h07m；semantic sidecar 51m49s                                                                                                                      | `remote-logs/w14-sf100-import-only-tuned-20260616-codex2`   | 三个 import JSON valid；`DONE` 和 `IMPORT_ONLY_DONE` 已写入               |
| [ ]  | Step B: SF100 minimal scenario matrix  | FAILED / FALLBACK | 15:26 启动；22:19 失败；总耗时约 6h53m；停在 `type-only`；sample-plan 约 59m；edge compare 1h47m14s，mismatches=0；budg compare 1h42m18s，mismatches=0；semantic bench 13m18s；semantic compare 7200s timeout，JSON 0 bytes | `remote-logs/w14-sf100-scenarios-min-tuned-20260616-codex1/FAILED` | 不继续 `property-required / degree-class`；先优化 compare/sample-plan 路径或改成 lighter correctness check |
| [ ]  | Step B: full 6-scenario matrix         | NOT STARTED | 0                                                                                                                                                      | 待 minimal matrix verdict                                    | 仅在 minimal matrix GO 后扩展                                           |


## 预计耗时与停止条件

Step A 预计耗时：

```text
edge-type-only: 65-90 min
budg-b64: 70-100 min
semantic: 70-110 min
total import-only: 3.5-5h
```

Step A 停止条件：

```text
任一 import >2h
MemAvailable <80GiB
/data free <200GiB
import JSON invalid
runner FAILED
```

Step B 预计耗时：

```text
minimal matrix, 3 scenarios, SAMPLES=100: 原估 3-8h；按 22:04 实测需要上调
full 6 scenarios, SAMPLES=500: only after minimal GO
```

### 2026-06-16 22:04 CST Step B ETA 修正

当前实测说明 Step B 的主要瓶颈不是 query bench，而是每次 compare 重新打开/加载 SF100 store 的 wall time。

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
launch: 2026-06-16 15:26 CST
elapsed: about 6h38m
current scenario: type-only
current command: compare schema vs semantic
current compare start: 2026-06-16 20:19:08 CST
current compare elapsed at 22:04: about 1h45m
compare timeout: 7200s, about 2026-06-16 22:19 CST
completed compares:
  schema vs edge-type-only: 1h47m14s, mismatches=0
  schema vs budg-b64: 1h42m18s, mismatches=0
remaining scenarios after type-only:
  property-required
  degree-class
```

ETA 判断：

```text
type-only complete ETA: 0-20 min if semantic compare completes before timeout.
current-speed full Step B ETA: about 12-15h more after type-only, because two scenarios remain and each may spend about 5.5-7h on plan/open/compare.
optimistic ETA: about 6-8h more if later scenarios reuse cache or compare path becomes materially faster.
failure branch: if current semantic compare reaches timeout around 22:19, mark Step B FAILED/FALLBACK and do not continue property-required/degree-class until compare/sample-plan path is optimized.
```

### 2026-06-16 22:21 CST Step B 最终状态

```text
DONE: absent
FAILED: 2026-06-16T22:19:17+08:00 unexpected exit status=124
failure stage: compare-type-only-semantic
failure reason: COMPARE_TIMEOUT_SECONDS=7200 exceeded
partial matrix completed:
  type-only schema bench: DONE
  type-only edge-type-only bench: DONE
  type-only schema vs edge-type-only compare: DONE, mismatches=0
  type-only budg-b64 bench: DONE
  type-only schema vs budg-b64 compare: DONE, mismatches=0
  type-only semantic bench: DONE
  type-only schema vs semantic compare: FAILED, JSON 0 bytes
not started:
  property-required
  degree-class
resource at 22:21:
  /data free: 239G
  MemAvailable: about 448GiB
```

判定：

```text
This Step B run is complete as FAILED/FALLBACK, not still running.
Do not estimate remaining runtime for this run; no remaining runner is active.
G1/W14 remains FALLBACK until the compare path is optimized or replaced by a lighter correctness check.
```

## 资源快照

2026-06-16 11:00 CST：

```text
/data: 2.0T total, 1.2T used, 664G free, 65% used
/tmp(root): 295G total, 266G used, 17G free, 95% used
Mem: 503Gi total, 51Gi used, 335Gi free, 447Gi available
running lsmgraph jobs: none
existing Step A run/store: none
```

判定：

```text
GO to launch Step A. Do not launch Step B until Step A has DONE + IMPORT_ONLY_DONE + valid import JSONs.
```

## SF1 初步提速验证

Run:

```text
remote-logs/w14-tuning-sf1-ab-20260616-codex1
```


| Done | Mode     | 状态   | JSON  | Wall time | Store | 结论                            |
| ---- | -------- | ---- | ----- | --------- | ----- | ----------------------------- |
| [x]  | baseline | DONE | valid | 1:34      | 3.6G  | builds/writes adjacency cache |
| [x]  | tuned    | DONE | valid | 0:42      | 1.4G  | `SNB_SKIP_ADJ_CACHE=1`        |


提速：

```text
94s -> 42s
约 2.2x faster
```

## SF30 提速验证

Run:

```text
remote-logs/w14-tuning-sf30-edge-import-20260616-codex1
```

结果：

```text
status=0
json_valid=1
input_rows=608041914
directed_edges=1087848423
elapsed=19:43.22
store_size=41G
```

旧未调优对比：

```text
remote-logs/w14-semantic-necessity-sf30-smoke-20260615-codex2
adjacency cache complete total_elapsed_s=3304.9
```

提速：

```text
3304.9s -> 1183.2s
约 2.79x faster
```

## 下一步最小动作

启动 Step A：

```bash
cd /data/WorkSpace/lsmgraph-rs
env RUN_ID=w14-sf100-import-only-tuned-20260616-codex1 \
  SCALE=sf100 W14_ALLOW_SF100=1 W14_IMPORT_ONLY=1 \
  W14_SKIP_ADJ_CACHE=1 \
  SF100_SCHEMA_STORE=/data/WorkSpace/lsmgraph-rs/store/qslsm-sf100-strong-baseline-20260610/schema \
  VARIANTS="schema edge-type-only budg-b64 semantic" \
  IMPORT_TIMEOUT_SECONDS=7200 \
  STORE_ROOT=/data/WorkSpace/lsmgraph-rs/store/w14-sf100-import-only-tuned-20260616-codex1 \
  baseline/launch_w14_semantic_necessity_20260615.sh
```

启动记录：


| Run id                                        | 状态                   | 说明                                                                                                          |
| --------------------------------------------- | -------------------- | ----------------------------------------------------------------------------------------------------------- |
| `w14-sf100-import-only-tuned-20260616-codex1` | FAILED before import | runner 未把逗号分隔的 `VARIANTS` 转为空格，报 `unknown variant schema,edge-type-only,budg-b64,semantic`；未开始导入，store 仅 4K |
| `w14-sf100-import-only-tuned-20260616-codex2` | RUNNING              | 修复 `VARIANTS="${VARIANTS//,/ }"` 后重启；当前 `edge-type-only` import                                             |


### 2026-06-16 11:07 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
FAILED: absent
current variant: edge-type-only
current stage: SF100 import, person_knows_person_0_0.csv
W14_SKIP_ADJ_CACHE: active; stderr shows SKIP vertex JSONL + edge-prop persistence
elapsed: about 28s
store root size: 838M
/data free: 663G
MemAvailable: 446GiB
lsmgraph RSS: about 1.9GiB
```

判定：

```text
RUNNING normally. Continue monitoring. Stop if import exceeds 2h, /data <200G, or MemAvailable <80GiB.
```

### 2026-06-16 11:13 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
FAILED: absent
current variant: edge-type-only
current stage: SF100 import, person_likes_comment_0_0.csv
progress sample: rows=174000000, directed_edges=348000000, elapsed_s=350.8 within current file
elapsed: about 7m
store root size: 13G
/data free: 651G
MemAvailable: 445GiB
lsmgraph RSS: about 1.9GiB
```

判定：

```text
RUNNING normally. Throughput is consistent with the tuned SF30 extrapolation. Continue monitoring.
```

### 2026-06-16 11:24 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
FAILED: absent
current variant: edge-type-only
current stage: SF100 import, forum_hasMember_person_0_0.csv
progress sample: rows=147000000, directed_edges=294000000, elapsed_s=305.0 within current file
elapsed: about 18m
store root size: 34G
/data free: 630G
MemAvailable: 445GiB
lsmgraph RSS: about 2.0GiB
```

判定：

```text
RUNNING normally. Disk remains above stop line; continue monitoring Step A.
```

### 2026-06-16 11:28 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
FAILED: absent
current variant: edge-type-only
current stage: SF100 import, comment_hasTag_tag_0_0.csv
progress sample: rows=46000000, directed_edges=92000000, elapsed_s=92.1 within current file
elapsed: about 21m
store root size: 40G
/data free: 624G
MemAvailable: 445GiB
lsmgraph RSS: about 1.9GiB
```

判定：

```text
RUNNING normally. CPU about 209%, memory stable, no FAILED marker. Continue Step A; do not start Step B yet.
```

### 2026-06-16 11:35 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
FAILED: absent
current variant: edge-type-only
current stage: SF100 import, comment_hasTag_tag_0_0.csv
progress sample: rows=262000000, directed_edges=524000000, elapsed_s=529.1 within current file
elapsed: about 29m
store root size: 55G
/data free: 610G
MemAvailable: 445GiB
lsmgraph RSS: about 2.0GiB
```

备注：

```text
11:33 local SSH reconnect failed once with Permission denied; retry succeeded. Remote nohup runner kept running and no FAILED marker was created.
```

判定：

```text
RUNNING normally. Resource stop lines are not close. Continue Step A import-only matrix.
```

### 2026-06-16 12:18 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
edge-type-only-import.json: valid, 76 bytes
edge-type-only input_rows: 1995609800
edge-type-only directed_edges: 3570968680
edge-type-only store bytes: 143757360279, about 134G
current variant: budg-b64
current stage: SF100 import, person_likes_comment_0_0.csv
progress sample: rows=180000000, directed_edges=360000000, elapsed_s=358.9 within current file
budg-b64 import elapsed: about 7m
store root size: 148G
budg-b64 store size: 14G
/data free: 517G
MemAvailable: 445GiB
lsmgraph RSS: about 2.0GiB
```

判定：

```text
RUNNING normally. First SF100 tuned import finished successfully and the runner has moved to budg-b64.
No resource stop line is close. Do not launch Step B or any second SF100 job until Step A writes IMPORT_ONLY_DONE.
```

### 2026-06-16 12:21 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
current variant: budg-b64
current stage: SF100 import, person_likes_post_0_0.csv
progress sample: rows=34000000, directed_edges=68000000, elapsed_s=70.3 within current file
budg-b64-import.json: 0 bytes, expected until import exits
store root size: 154G
budg-b64 store size: 20G
/data free: 510G
MemAvailable: 445GiB
```

判定：

```text
RUNNING normally. budg-b64 has advanced to the next file. No stop condition triggered.
```

### 2026-06-16 12:22 CST speedup audit

当前这次 SF100 tuned split retry 已经在启动前应用的提速：


| Done | 提速项                              | 当前状态                          | 影响                                                                                        |
| ---- | -------------------------------- | ----------------------------- | ----------------------------------------------------------------------------------------- |
| [x]  | `SNB_SKIP_ADJ_CACHE=1`           | active in import env          | 跳过 SNB vertex JSONL + edge-prop persistence，避免旧 SF100 在 adjacency cache/finalization 阶段卡住 |
| [x]  | `W14_IMPORT_ONLY=1`              | active in Step A              | 先只导入 store，不跑 scenario bench/compare，降低失败半径                                               |
| [x]  | reuse SF100 schema store         | active                        | 不重复导入 schema baseline                                                                     |
| [x]  | split Step A / Step B            | active                        | Step A 完成后复用 stores 跑 Step B，避免 all-in-one 长跑不可恢复                                         |
| [x]  | `W14_SKIP_SEM_INDEX_FOR_PLAN=1`  | planned for Step B            | 只在 sample-plan 阶段跳过 sem index，不影响 compare correctness                                     |
| [x]  | minimal scenario first           | planned for Step B            | 先跑 `type-only / property-required / degree-class`, `SAMPLES=100`，GO 后才扩 full matrix       |
| [x]  | larger metadata cache for Step B | planned                       | Step B 使用 `CSR_METADATA_CACHE_ENTRIES=32768`，用内存换 metadata lookup 开销                      |
| [ ]  | streaming degree sidecar write   | source patched, build pending | 去掉 sidecar 写出前的全量 Vec 拷贝和排序；当前运行进程不受影响，下一次 build/run 生效                                   |


不建议在当前 import 中途再做的提速：


| 提速项                                             | 当前决定 | 原因                                                             |
| ----------------------------------------------- | ---- | -------------------------------------------------------------- |
| stop and rebuild with `target-cpu=native` / LTO | 不做   | 需要重启当前 SF100 import；收益不确定，且会改变当前矩阵执行条件                         |
| increase `MEMGRAPH_BYTES`                       | 不做   | 会改变 segment shape，和已完成的 `edge-type-only` 以及复用 schema store 不可比 |
| parallel import multiple SF100 variants         | 不做   | 会抢 `/data` IO/cache，并增加触发 `/data <200G` 的风险                    |
| delete completed Step A stores before Step B    | 不做   | Step B 需要复用这些 stores；只能 Step B summary 后再清理                    |
| renice current import to higher priority        | 不做   | 当前吞吐正常；无确认权限时不做系统级调度变更                                         |


14:45 补充：

```text
源码已同步一个备用提速补丁：write_degree_directory_sidecar 不再把 degree directory entries 收集到 Vec 后排序，
改为先 count 非零 mask，再直接流式写 HashMap entries。
读取端 read_degree_directory_sidecar 重建 HashMap，不依赖 entry 顺序，所以这是低风险优化。
当前正在运行的 lsmgraph 进程仍是旧 binary；该补丁只会在下一次 cargo build 后生效。
```

判定：

```text
Current run is already the optimized W14 SF100 path. Continue running; do not restart.
The next safe speedup point is Step B launch: set CSR_METADATA_CACHE_ENTRIES=32768 and keep SAMPLES=100 for the minimal matrix.
```

### 2026-06-16 12:24 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
current variant: budg-b64
current stage: SF100 import, forum_hasMember_person_0_0.csv
recent completed file: person_likes_post_0_0.csv, rows=99515108, directed_edges=199030216, elapsed_s=205.3
budg-b64-import.json: 0 bytes, expected until import exits
store root size: 159G
budg-b64 store size: 25G
/data free: 505G
MemAvailable: 445GiB
```

判定：

```text
RUNNING normally. budg-b64 is progressing through the remaining medium files. No stop condition triggered.
```

### 2026-06-16 12:35 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
current variant: budg-b64
current stage: SF100 import, comment_hasTag_tag_0_0.csv
progress sample: rows=146000000, directed_edges=292000000, elapsed_s=289.6 within current file
budg-b64 import elapsed: about 25m
budg-b64-import.json: 0 bytes, expected until import exits
runner elapsed: about 1h29m
store root size: 181G
budg-b64 store size: 47G
/data free: 483G
MemAvailable: 445GiB
```

备注：

```text
12:34 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
```

判定：

```text
RUNNING normally. Disk is declining as expected but remains above the 200G stop line.
Estimated Step A final disk floor after budg-b64 + semantic remains tight but acceptable; continue monitoring before starting Step B.
```

### 2026-06-16 12:47 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
current variant: budg-b64
current stage: SF100 import, post_0_0.csv semantic edge passes
progress sample: IsLocatedIn pass rows=12000000, directed_edges=24000000, elapsed_s=27.6 within current file
budg-b64 import elapsed: about 37m
budg-b64-import.json: 0 bytes, expected until import exits
runner elapsed: about 1h41m
store root size: 207G
budg-b64 store size: 73G
/data free: 457G
MemAvailable: 445GiB
```

备注：

```text
12:46 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
```

判定：

```text
RUNNING normally. budg-b64 is in the later post/comment-derived passes. Disk remains above the 200G stop line.
```

### 2026-06-16 12:53 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
current variant: budg-b64
current stage: SF100 import, comment_0_0.csv
progress sample: rows=115000000, directed_edges=230000000, elapsed_s=258.3 within current file
budg-b64 import elapsed: about 43m
budg-b64-import.json: 0 bytes, expected until import exits
runner elapsed: about 1h47m
store root size: 221G
budg-b64 store size: 87G
/data free: 444G
MemAvailable: 445GiB
```

备注：

```text
12:52 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
```

判定：

```text
RUNNING normally. budg-b64 is in the final large comment file; continue monitoring until import JSON lands.
```

### 2026-06-16 13:04 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
current variant: budg-b64
current stage: SF100 import, comment_0_0.csv
progress sample: rows=189000000, directed_edges=378000000, elapsed_s=425.0 within current file
budg-b64 import elapsed: about 54m
budg-b64-import.json: 0 bytes, expected until import exits
runner elapsed: about 1h58m
store root size: 245G
budg-b64 store size: 112G
/data free: 419G
MemAvailable: 445GiB
```

备注：

```text
13:03 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
```

判定：

```text
RUNNING normally. Disk remains above the 200G stop line, but Step A final disk floor is now the main risk to watch before launching Step B.
```

### 2026-06-16 13:15 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
completed variant: budg-b64
budg-b64-import.json: valid, 76 bytes
budg-b64 input_rows: 1995609800
budg-b64 directed_edges: 3570968680
budg-b64 edge import complete elapsed_s: 3844.2
budg-b64 total import elapsed_s: 3846.1
current variant: semantic
current stage: SF100 import, person_knows_person_0_0.csv
semantic import elapsed: about 40s
semantic-import.json: 0 bytes, expected until import exits
runner elapsed: about 2h09m
store root size: 269G
edge-type-only store size: 134G
budg-b64 store size: 134G
semantic store size: 1.2G
/data free: 395G
MemAvailable: 446GiB
```

备注：

```text
13:14 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
```

判定：

```text
RUNNING normally. Step A has completed 2/3 imported variants. Only semantic import remains before IMPORT_ONLY_DONE.
Disk remains above the 200G stop line, but Step B launch must re-check /data after semantic completes.
```

### 2026-06-16 13:28 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
completed variant: budg-b64
current variant: semantic
current stage: SF100 import, person_hasInterest_tag_0_0.csv
recent completed file: person_likes_post_0_0.csv, rows=99515108, directed_edges=199030216, elapsed_s=219.3
semantic import elapsed: about 13m
semantic-import.json: 0 bytes, expected until import exits
runner elapsed: about 2h22m
store root size: 292G
edge-type-only store size: 134G
budg-b64 store size: 134G
semantic store size: 24G
/data free: 372G
MemAvailable: 443GiB
```

备注：

```text
13:27 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
free memory is low because page cache is high; MemAvailable remains high, so the memory stop line is not close.
```

判定：

```text
RUNNING normally. Disk remains above the 200G stop line. Continue semantic import.
```

### 2026-06-16 13:39 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
completed variant: budg-b64
current variant: semantic
current stage: SF100 import, comment_hasTag_tag_0_0.csv
progress sample: rows=114000000, directed_edges=228000000, elapsed_s=234.8 within current file
semantic import elapsed: about 24m
semantic-import.json: 0 bytes, expected until import exits
runner elapsed: about 2h33m
store root size: 313G
edge-type-only store size: 134G
budg-b64 store size: 134G
semantic store size: 45G
/data free: 352G
MemAvailable: 441GiB
```

备注：

```text
13:38 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
free memory is down to 2.8GiB because page cache is high; MemAvailable remains 441GiB, so memory stop line is not close.
```

判定：

```text
RUNNING normally. Disk remains above the 200G stop line, but final Step A disk headroom should be checked before Step B.
```

### 2026-06-16 13:51 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
completed variant: budg-b64
current variant: semantic
current stage: SF100 import, post_0_0.csv
progress sample: rows=53000000, directed_edges=106000000, elapsed_s=128.4 within current file
semantic import elapsed: about 36m
semantic-import.json: 0 bytes, expected until import exits
runner elapsed: about 2h45m
store root size: 334G
edge-type-only store size: 134G
budg-b64 store size: 134G
semantic store size: 67G
/data free: 330G
MemAvailable: 431GiB
```

备注：

```text
13:50 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
```

判定：

```text
RUNNING normally. Disk remains above the 200G stop line. Continue semantic import.
```

### 2026-06-16 14:02 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
completed variant: budg-b64
current variant: semantic
current stage: SF100 import, comment_0_0.csv
progress sample: rows=192000000, directed_edges=384000000, elapsed_s=423.6 within current file
semantic import elapsed: about 47m
semantic-import.json: 0 bytes, expected until import exits
runner elapsed: about 2h56m
store root size: 361G
edge-type-only store size: 134G
budg-b64 store size: 134G
semantic store size: 93G
/data free: 303G
MemAvailable: 397GiB
```

备注：

```text
14:01 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
```

判定：

```text
RUNNING normally. Disk remains above the 200G stop line, but Step B cannot start until semantic completes and /data is rechecked.
```

### 2026-06-16 14:14 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
completed variant: budg-b64
current variant: semantic
current stage: SF100 import, comment_0_0.csv / ReplyOfPost pass
progress sample: rows=94000000, directed_edges=92648674, elapsed_s=112.2 within current file/pass
semantic import elapsed: about 59m
semantic-import.json: 0 bytes, expected until import exits
runner elapsed: about 3h08m
store root size: 386G
edge-type-only store size: 134G
budg-b64 store size: 134G
semantic store size: 118G
/data free: 278G
MemAvailable: 391GiB
```

备注：

```text
14:13 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
```

判定：

```text
RUNNING normally, but disk headroom is now the gating resource.
Do not start Step B until semantic completes and /data free is confirmed above 200G with enough room for logs/results.
```

### 2026-06-16 14:21 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
completed variant: budg-b64
current variant: semantic
current stage: SF100 import, comment_0_0.csv / ReplyOfPost pass
progress sample: rows=201000000, directed_edges=203913530, elapsed_s=251.2 within current file/pass
semantic import elapsed: about 1h06m
semantic-import.json: 0 bytes, expected until import exits
runner elapsed: about 3h15m
store root size: 401G
edge-type-only store size: 134G
budg-b64 store size: 134G
semantic store size: 133G
/data free: 263G
MemAvailable: 356GiB
```

备注：

```text
14:20 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
```

判定：

```text
RUNNING normally, but disk is now tight enough that Step B must not start automatically.
Wait for semantic JSON, then re-check /data. If free space is near 200G, clean safe temporary stores before Step B.
```

### 2026-06-16 14:27 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
RUNNING: yes
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent
completed variant: edge-type-only
completed variant: budg-b64
current variant: semantic
current stage: semantic import finalization, persist semantic sidecars
semantic edge import complete elapsed_s: 3977.1
semantic total_elapsed_s before sidecars: 3979.8
semantic-import.json: 0 bytes, expected until import exits
runner elapsed: about 3h21m
store root size: 402G
edge-type-only store size: 134G
budg-b64 store size: 134G
semantic store size: 134G
/data free: 262G
MemAvailable: 337GiB
```

备注：

```text
14:26 local SSH reconnect failed once with Permission denied; retry succeeded. Remote runner kept running normally.
```

判定：

```text
RUNNING normally, in finalization. Wait for semantic-import.json and IMPORT_ONLY_DONE before starting Step B.
Disk remains above the 200G stop line, but Step B should start only after a fresh resource check.
```

### 2026-06-16 14:30 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent

completed import JSON:
- edge-type-only-import.json: valid, input_rows=1995609800, directed_edges=3570968680
- budg-b64-import.json: valid, input_rows=1995609800, directed_edges=3570968680

current variant: semantic
current stage: semantic import finalization, persist semantic sidecars
semantic edge import complete elapsed_s: 3977.1
semantic total_elapsed_s before sidecars: 3979.8
semantic-import.json: 0 bytes, expected until import exits

process:
- runner bash elapsed: about 3h24m
- semantic lsmgraph elapsed: about 1h15m
- semantic lsmgraph CPU: about 222%
- semantic lsmgraph RSS: about 115GiB

store:
- total Step A store root: 402G
- edge-type-only: 134G
- budg-b64: 134G
- semantic: 134G

resources:
- /data free: 262G
- /tmp(root) free: 17G
- MemAvailable: 337GiB
```

判定：

```text
RUNNING normally, but disk headroom is now tight.
Do not start Step B until semantic-import.json is valid and /data is rechecked.
If /data is near 200G after Step A, pause for cleanup decision before Step B.
```

### 2026-06-16 14:40 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent

current variant: semantic
current stage: still in semantic sidecar persistence
semantic-import.json: 0 bytes
runner elapsed: about 3h34m
semantic process elapsed: about 1h26m
semantic lsmgraph CPU: about 206%
semantic lsmgraph RSS: about 115GiB

/data free: 262G
/tmp(root) free: 17G
MemAvailable: 337GiB
```

判定：

```text
RUNNING but watch the import timeout.
No DONE/FAILED marker yet. No Step B launch.
If this remains in sidecar persistence until the 7200s import timeout, mark Step A semantic FAILED and keep prior FALLBACK.
```

### 2026-06-16 14:53 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent

current variant: semantic
current stage: still in semantic sidecar persistence
semantic-import.json: 0 bytes
runner elapsed: about 3h46m
semantic process elapsed: about 1h38m
semantic lsmgraph CPU: about 193%
semantic lsmgraph RSS: about 120GiB

/data free: 262G
/tmp(root) free: 17G
MemAvailable: 333GiB
```

判定：

```text
RUNNING, but likely spending time in the old sorted sidecar path.
Continue until timeout or completion; do not interrupt before the explicit 7200s guard.
If timeout fires, rebuild with the staged streaming sidecar patch and rerun only the semantic import path.
```

重跑注意事项：

```text
run_w14 ensure_store 只检查 "$store/MANIFEST"。
如果 semantic 超时，partial semantic store 可能已经有 MANIFEST，但缺少 valid semantic-import.json / sidecar。
因此不能直接用同一 STORE_ROOT 重跑 semantic，否则可能误复用半成品。
超时后的安全路径是：记录 FAILED -> 校验 semantic store sidecar/JSON -> 清理或隔离 partial semantic store -> cargo build patched binary -> 只重跑 semantic import。
```

### 2026-06-16 15:06 CST heartbeat

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
DONE: absent
IMPORT_ONLY_DONE: absent
FAILED: absent

current variant: semantic
current stage: still in semantic sidecar persistence
semantic-import.json: 0 bytes
runner elapsed: about 4h00m
semantic process elapsed: about 1h51m
semantic lsmgraph CPU: about 182%
semantic lsmgraph RSS: about 120GiB

/data free: 262G
/tmp(root) free: 17G
MemAvailable: 333GiB
```

判定：

```text
Still running, close to the 7200s import timeout.
Do not launch Step B.
If it times out, use the already staged streaming sidecar patch and rerun semantic after safe cleanup of the partial semantic store.
```

### 2026-06-16 15:20 CST Step A completion validation

```text
run id: w14-sf100-import-only-tuned-20260616-codex2
DONE: 2026-06-16T15:13:33+08:00
IMPORT_ONLY_DONE: 2026-06-16T15:13:33+08:00
FAILED: absent

edge-type-only-import.json: valid, input_rows=1995609800, directed_edges=3570968680, snapshot=3570968680
budg-b64-import.json: valid, input_rows=1995609800, directed_edges=3570968680, snapshot=3570968680
semantic-import.json: valid, input_rows=1995609800, directed_edges=3570968680, snapshot=3570968680

manifest bytes:
- schema reuse: 141242590040
- edge-type-only: 143757360279
- budg-b64: 143762243779
- semantic: 157157488792

semantic DEGREE_DIRECTORY: 13388017580 bytes
semantic sidecar persist elapsed_s: 3109.2

/data free: 250G
/tmp(root) free: 17G
MemAvailable: 447GiB
```

判定：

```text
Step A DONE.
The bottleneck is confirmed: degree sidecar persistence took about 51m49s.
Proceed to Step B only with existing stores; do not start another import.
```

### 2026-06-16 15:24 CST patch validation before Step B

```text
streaming degree sidecar source patch: synced to src/graph.rs
cargo test degree_directory_sidecar_round_trips_and_rejects_stale_files --lib: passed
cargo fmt --check: blocked by unrelated W7 formatting diffs
rustfmt --edition 2024 --check src/graph.rs: only import-order diffs, no syntax issue
```

判定：

```text
Patch is not needed for current Step A stores, but Step B runner will build the patched source.
If release build fails, Step B must stop and write FAILED before any further experiment.
```

### 2026-06-16 15:27 CST Step B launch

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
run root: /data/WorkSpace/lsmgraph-rs/remote-logs/w14-sf100-scenarios-min-tuned-20260616-codex1
store root: /data/WorkSpace/lsmgraph-rs/store/w14-sf100-import-only-tuned-20260616-codex2
variants: schema,edge-type-only,budg-b64,semantic
scenarios: type-only,property-required,degree-class
SAMPLES: 100
WARMUP_RUNS: 0
REPEATS: 1
CSR_METADATA_CACHE_ENTRIES: 32768
IMPORT_TIMEOUT_SECONDS: 600
BENCH_TIMEOUT_SECONDS: 7200
COMPARE_TIMEOUT_SECONDS: 7200
```

15:27 heartbeat:

```text
DONE: absent
FAILED: absent
current stage: cargo build --release
/data free: 250G
MemAvailable: 447GiB
```

判定：

```text
Step B launched normally. Watch for store reuse after build; any unexpected import is a stop condition.
```

### 2026-06-16 15:33 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

build: DONE, release target built in 23.67s
store reuse: schema, edge-type-only, budg-b64, semantic all reused
current scenario: type-only
current stage: sample-plan-type-only
sample-plan env: SNB_SKIP_SEM_INDEX=1
sample-plan elapsed: about 6m30s
sample-plan process RSS: about 106GiB
sample-plan process CPU: about 97%

/data free: 250G
MemAvailable: 345GiB
```

判定：

```text
Step B is running normally.
No unexpected import occurred.
Current bottleneck is opening/scanning the SF100 schema store to create the sample plan, even with semantic index rebuild skipped.
Do not launch G2/G3 heavy tasks.
```

### 2026-06-16 15:45 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: sample-plan-type-only
sample-plan output: not yet written
schema-plan-source.json: 0 bytes
sample-plan elapsed: about 18m07s
sample-plan process RSS: about 133GiB
sample-plan process CPU: about 99%

/data free: 250G
MemAvailable: 320GiB
```

判定：

```text
Still running normally under the 7200s timeout.
Current bottleneck is sample-plan generation from the SF100 schema store.
If this stage becomes the next timeout, implement a sample-plan fast path instead of repeating the same full store-open path.
```

### 2026-06-16 16:04 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: sample-plan-type-only
sample-plan output: not yet written
schema-plan-source.json: 0 bytes
sample-plan elapsed: about 37m10s
sample-plan process RSS: about 255GiB
sample-plan process CPU: about 99%

/data free: 250G
MemAvailable: 204GiB
```

判定：

```text
Still above the 80GiB memory stop line, but this is now a clear runner bottleneck.
The code path builds a full Vec<EdgeRecord> from SF100 before sampling 100 sources.
If it completes, do not repeat the same full-scan plan generation for every scenario without reassessment.
If it times out or approaches the memory stop line, stop Step B and switch to precomputed sample-plan files.
```

### 2026-06-16 16:14 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: sample-plan-type-only
sample-plan output: not yet written
schema-plan-source.json: 0 bytes
sample-plan elapsed: about 47m21s
sample-plan process RSS: about 299GiB
sample-plan process CPU: about 99%

/data free: 250G
MemAvailable: 162GiB
```

判定：

```text
Still above the 80GiB stop line, but close enough to treat sample-plan generation as the next bottleneck.
Prepare precomputed sample-plan support while the current process continues.
If MemAvailable approaches 80GiB, stop the current Step B runner and relaunch with precomputed plans.
```

### 2026-06-16 16:20 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: sample-plan-type-only
sample-plan output: not yet written
schema-plan-source.json: 0 bytes
runner elapsed: about 54m06s
sample-plan elapsed: about 53m42s
sample-plan process RSS: about 317GiB
sample-plan process CPU: about 100%

/data free: 250G
MemAvailable: 144GiB
```

判定：

```text
Still running above the 80GiB memory stop line, but memory headroom is shrinking.
Next action: keep monitoring closely; if MemAvailable approaches 80GiB, stop this sample-plan path and relaunch Step B with precomputed sample-plan files.
```

### 2026-06-16 16:35 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
completed stage: sample-plan-type-only
sample-plan duration: about 59m
sample-plan.json: 8.3K
schema-plan-source.json: 404K

current stage: bench-type-only-schema
schema bench elapsed: about 9m14s
schema bench process RSS: about 49GiB
schema bench process CPU: about 99%

/data free: 248G
MemAvailable: 400GiB
```

判定：

```text
Sample-plan completed without crossing the memory stop line.
Memory pressure is now normal again.
Continue Step B; no need to switch to precomputed sample-plan yet, but record sample-plan as a runner bottleneck.
```

### 2026-06-16 16:48 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
completed stages:
- sample-plan-type-only: 15:27:03 -> 16:26:06, about 59m03s
- bench-type-only-schema: 16:26:06 -> 16:36:12, about 10m06s
- bench-type-only-edge-type-only: 16:36:12 -> 16:36:14, about 2s

current stage: compare-type-only-edge-type-only
compare elapsed: about 12m28s
compare output: 0 bytes, expected until process exits
compare process RSS: about 26GiB
compare process CPU: about 100%

/data free: 239G
MemAvailable: 422GiB
```

判定：

```text
Step B is progressing.
Current compare is slower than bench, but resource use is safe.
Continue monitoring; compare mismatch or timeout remains a stop condition.
```

### 2026-06-16 17:03 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: compare-type-only-edge-type-only
compare elapsed: about 26m53s
compare output: 0 bytes, expected until process exits
compare process RSS: about 26GiB
compare process CPU: about 100%

/data free: 239G
MemAvailable: 422GiB
```

判定：

```text
Compare is long but still within the 7200s timeout.
Resource use is safe; continue monitoring.
If compare output reports mismatches != 0, stop Step B and keep W14 as FALLBACK.
```

### 2026-06-16 17:22 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: compare-type-only-edge-type-only
compare elapsed: about 46m16s
compare output: 0 bytes, expected until process exits
compare process RSS: about 26GiB
compare process CPU: about 100%

/data free: 239G
MemAvailable: 422GiB
```

判定：

```text
Compare remains long but still within the 7200s timeout.
No resource stop condition is close.
Continue monitoring; if this compare times out, Step B becomes FAILED/FALLBACK and the next optimization target is compare path attribution.
```

### 2026-06-16 17:47 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: compare-type-only-edge-type-only
compare elapsed: about 1h10m51s
compare output: 0 bytes, expected until process exits
compare process RSS: about 26GiB
compare process CPU: about 100%

/data free: 239G
MemAvailable: 422GiB
```

判定：

```text
Compare is still within the 7200s timeout, with about 49m until timeout.
Resource use is safe.
If this compare times out, do not continue the remaining variants/scenarios; record Step B FAILED/FALLBACK and optimize compare path before retrying.
```

### 2026-06-16 18:11 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: compare-type-only-edge-type-only
compare elapsed: about 1h35m39s
compare output: 0 bytes, expected until process exits
compare process RSS: about 26GiB
compare process CPU: about 100%

/data free: 239G
MemAvailable: 422GiB
```

判定：

```text
Compare has about 24m left before the 7200s timeout.
No resource stop condition is close.
Wait for DONE/FAILED; do not start any other heavy runner.
```

### 2026-06-16 18:45 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
completed stages:
- compare-type-only-edge-type-only: 16:36:14 -> 18:23:28, about 1h47m14s
- compare-schema-vs-edge-type-only.json: valid
- checked: 100
- mismatches: 0
- JSON elapsed_ms: 16447
- bench-type-only-budg-b64: 18:23:28 -> 18:23:31, about 3s

current stage: compare-type-only-budg-b64
current compare elapsed: about 21m54s
current compare output: 0 bytes, expected until process exits
current compare process RSS: about 26GiB
current compare process CPU: about 100%

/data free: 239G
MemAvailable: 422GiB
```

判定：

```text
edge-type-only correctness passed: mismatches=0.
The long wrapper time versus short JSON elapsed_ms suggests open/rebuild/load dominates compare wall time.
Continue budg-b64 compare; timeout or mismatch remains a stop condition.
```

### 2026-06-16 19:19 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: compare-type-only-budg-b64
compare elapsed: about 56m03s
compare output: 0 bytes, expected until process exits
compare process RSS: about 26GiB
compare process CPU: about 100%

/data free: 239G
MemAvailable: 422GiB
```

判定：

```text
Still within timeout and resources are safe.
The repeated long compare wrapper confirms that open/rebuild/load dominates SF100 compare wall time.
Continue monitoring; no G2/G3 heavy runner.
```

### 2026-06-16 20:05 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: compare-type-only-budg-b64
compare elapsed: about 1h41m44s
compare output: 0 bytes, expected until process exits
compare process RSS: about 26GiB
compare process CPU: about 100%

/data free: 239G
MemAvailable: 422GiB
```

判定：

```text
Still within timeout.
Resource use is safe.
Continue until compare JSON or timeout.
```

### 2026-06-16 20:28 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
completed stages:
- compare-type-only-budg-b64: 18:23:32 -> 20:05:50, about 1h42m18s
- compare-schema-vs-budg-b64.json: valid
- checked: 100
- mismatches: 0
- JSON elapsed_ms: 16612
- bench-type-only-semantic: 20:05:50 -> 20:19:08, about 13m18s

current stage: compare-type-only-semantic
current compare elapsed: about 9m02s
current compare output: 0 bytes, expected until process exits
current compare process RSS: about 26GiB
current compare process CPU: about 100%

/data free: 239G
MemAvailable: 422GiB
```

判定：

```text
budg-b64 correctness passed: mismatches=0.
Continue semantic compare; timeout or mismatch remains a stop condition.
```

### 2026-06-16 21:11 CST Step B heartbeat

```text
run id: w14-sf100-scenarios-min-tuned-20260616-codex1
DONE: absent
FAILED: absent

current scenario: type-only
current stage: compare-type-only-semantic
compare elapsed: about 52m42s
compare output: 0 bytes, expected until process exits
compare process RSS: about 26GiB
compare process CPU: about 100%

/data free: 239G
MemAvailable: 422GiB
```

判定：

```text
Still within timeout and resources are safe.
Continue semantic compare.
```

Step A 完成后记录：

```text
DONE / FAILED
IMPORT_ONLY_DONE
edge-type-only-import.json valid/invalid
budg-b64-import.json valid/invalid
semantic-import.json valid/invalid
manifest.tsv store bytes
保留/删除策略
```

## 目标

把完整 W14 SF100 matrix 拆成两步执行，避免再次因为 all-in-one 长跑失败：

```text
Step A: SF100 tuned import-only matrix
Step B: 复用 Step A stores 跑 scenario / bench / compare matrix
```

当前只做 W14 import/runner path 调优，不启动完整 S3，不改论文 claim。

## 已完成调优

### 1. import 跳过 SNB adjacency cache

W14 runner 新增：

```text
W14_SKIP_ADJ_CACHE=1
```

作用：

```text
import 时设置 SNB_SKIP_ADJ_CACHE=1
跳过 snb_adjacency.bin 构建/写入
跳过 vertex JSONL + edge-prop persistence
```

依据：W14 的 `storage-bench` / `neighbor-compare` 直接打开 Engine/CSR，不依赖 SNB HTTP adjacency cache。

### 2. import 尾部 sidecar heartbeat

`src/bin/lsmgraph.rs` 在 import 尾部新增：

```text
[import] persist semantic sidecars start
[import] persist semantic sidecars complete
```

作用：避免再次出现 `adjacency build complete` 之后长时间无日志，无法判断卡在哪个阶段。

### 3. sample-plan 专用 semantic index skip

W14 runner 新增：

```text
W14_SKIP_SEM_INDEX_FOR_PLAN=1
```

作用：

```text
sample-plan 阶段设置 SNB_SKIP_SEM_INDEX=1
bench/compare 阶段不设置
```

原因：sample-plan 只需要生成 query samples，不应承担 rebuild semantic index 的成本；但 compare 曾有旧路径风险，所以 compare 不跳过 semantic index。

### 4. metadata cache 参数化

W14 runner 新增：

```text
CSR_METADATA_CACHE_ENTRIES=4096
```

默认保持旧值。SF100 Step B 可按资源调高到：

```text
CSR_METADATA_CACHE_ENTRIES=16384 或 32768
```

目的：减少大量 L0 CSR metadata/offset array 在 bench/compare 里的 cache thrash。风险是内存占用上升，因此只在 MemAvailable 充足时启用。

### 5. import-only mode

W14 runner 新增：

```text
W14_IMPORT_ONLY=1
```

行为：

```text
build -> ensure/reuse/import all variants -> write IMPORT_ONLY_DONE + DONE -> exit
```

这样 Step A 可以只验证 SF100 imports，不进入 scenario matrix。

## 已跑 smoke

### SF30 tuned import

Run:

```text
remote-logs/w14-tuning-sf30-edge-import-20260616-codex1
store/w14-tuning-sf30-edge-import-20260616-codex1/edge-type-only
```

结果：

```text
status=0
json_valid=1
input_rows=608041914
directed_edges=1087848423
elapsed=19:43.22
max_rss=2331600 KB
store_size=41G
```

关键日志：

```text
[snb-full] edge import complete ... elapsed_s=1181.2
[snb-full] SKIP adjacency cache (SNB_SKIP_ADJ_CACHE set) ... total_elapsed_s=1182.9
[import] persist semantic sidecars complete elapsed_s=0.0
```

旧未调优 SF30 edge-type-only import 对比：

```text
run: remote-logs/w14-semantic-necessity-sf30-smoke-20260615-codex2
edge import complete: elapsed_s=2025.4
adjacency cache complete: total_elapsed_s=3304.9
```

提速结论：

```text
3304.9s -> 1183.2s
约 2.79x faster
节省约 35.4 分钟
```

注意：该 SF30 tuned run 是用户中断本地等待后远端继续完成的；证据以远端 `status=0`、`json_valid=1`、`Exit status: 0` 为准。

### SF1 A/B import

Run:

```text
remote-logs/w14-tuning-sf1-ab-20260616-codex1
store/w14-tuning-sf1-ab-20260616-codex1
```

结果：


| Mode     | JSON  | Wall time | Key path                      | Store |
| -------- | ----- | --------- | ----------------------------- | ----- |
| baseline | valid | 1:34      | builds/writes adjacency cache | 3.6G  |
| tuned    | valid | 0:42      | `SNB_SKIP_ADJ_CACHE=1`        | 1.4G  |


关键日志：

```text
baseline edge import complete elapsed_s=58.1
baseline adjacency cache complete total_elapsed_s=85.2
tuned edge import complete elapsed_s=40.0
tuned SKIP adjacency cache total_elapsed_s=41.7
```

提速结论：

```text
94s -> 42s
约 2.2x faster
```

## Step A：SF100 tuned import-only matrix

目标：

```text
复用 existing SF100 schema store；
顺序导入 edge-type-only / budg-b64 / semantic；
不跑 scenario bench/compare；
验证 tuned import path 在 SF100 可完成。
```

建议命令：

```bash
cd /data/WorkSpace/lsmgraph-rs
env RUN_ID=w14-sf100-import-only-tuned-20260616-codex1 \
  SCALE=sf100 W14_ALLOW_SF100=1 W14_IMPORT_ONLY=1 \
  W14_SKIP_ADJ_CACHE=1 \
  SF100_SCHEMA_STORE=/data/WorkSpace/lsmgraph-rs/store/qslsm-sf100-strong-baseline-20260610/schema \
  VARIANTS="schema edge-type-only budg-b64 semantic" \
  IMPORT_TIMEOUT_SECONDS=7200 \
  STORE_ROOT=/data/WorkSpace/lsmgraph-rs/store/w14-sf100-import-only-tuned-20260616-codex1 \
  baseline/launch_w14_semantic_necessity_20260615.sh
```

预计：

```text
edge-type-only: 65-90 min
budg-b64: 70-100 min
semantic: 70-110 min
total import-only: 3.5-5h
```

停止条件：

```text
任一 import >2h
MemAvailable <80GiB
/data free <200GiB
import JSON invalid
runner FAILED
```

Step A 完成线：

```text
DONE exists
IMPORT_ONLY_DONE exists
edge-type-only-import.json valid
budg-b64-import.json valid
semantic-import.json valid
manifest.tsv records store bytes
```

## Step B：SF100 scenario matrix

目标：

```text
复用 Step A stores，不再导入；
先跑最小 scenario matrix，再决定是否扩 full 6 scenarios。
```

建议最小矩阵：

```text
SCENARIOS="type-only property-required degree-class"
SAMPLES=100 或 200
WARMUP_RUNS=0
REPEATS=1
CSR_METADATA_CACHE_ENTRIES=32768
```

建议命令：

```bash
cd /data/WorkSpace/lsmgraph-rs
env RUN_ID=w14-sf100-scenarios-min-tuned-20260616-codex1 \
  SCALE=sf100 W14_ALLOW_SF100=1 \
  W14_SKIP_ADJ_CACHE=1 W14_SKIP_SEM_INDEX_FOR_PLAN=1 \
  CSR_METADATA_CACHE_ENTRIES=32768 \
  SF100_SCHEMA_STORE=/data/WorkSpace/lsmgraph-rs/store/qslsm-sf100-strong-baseline-20260610/schema \
  STORE_ROOT=/data/WorkSpace/lsmgraph-rs/store/w14-sf100-import-only-tuned-20260616-codex1 \
  SCENARIOS="type-only property-required degree-class" \
  SAMPLES=100 WARMUP_RUNS=0 REPEATS=1 \
  IMPORT_TIMEOUT_SECONDS=600 \
  BENCH_TIMEOUT_SECONDS=7200 COMPARE_TIMEOUT_SECONDS=7200 \
  baseline/launch_w14_semantic_necessity_20260615.sh
```

注意：`STORE_ROOT` 指向 Step A 的 store root，所以 `ensure_store` 应该全部走 reuse；`IMPORT_TIMEOUT_SECONDS=600` 是防止误导入。

Step B 扩展策略：

```text
最小矩阵 GO -> 再跑 full 6 scenarios / SAMPLES=500
最小矩阵 FALLBACK -> 只写 conservative W14 claim，不扩 full matrix
最小矩阵 FAILED -> 记录失败原因，进入 G2/C10 latency attribution
```

## 进一步提速方向


| 优先级 | 调优                                                  | 状态                      | 风险                                         |
| --- | --------------------------------------------------- | ----------------------- | ------------------------------------------ |
| P0  | `SNB_SKIP_ADJ_CACHE=1`                              | done, SF30/SF1 positive | 不能用于需要 SNB HTTP adjacency cache 的实验；W14 OK |
| P0  | `W14_IMPORT_ONLY=1`                                 | implemented             | 需要 Step B 复用同一 store root                  |
| P1  | `SNB_SKIP_SEM_INDEX=1` only for sample-plan         | implemented             | 不用于 compare                                |
| P1  | `CSR_METADATA_CACHE_ENTRIES=16384/32768` for Step B | planned                 | 内存占用上升                                     |
| P1  | scenario subset first                               | planned                 | claim 必须标注 subset                          |
| P2  | per-variant/import-delete after summary             | planned only            | Step B 需要 stores，不能过早删除                    |
| P2  | larger `MEMGRAPH_BYTES`                             | not enabled             | 会改变 segment shape；若启用必须所有 variants 一致      |


## 当前判定

```text
SF30/SF1 tuned smoke 显示 import path 有明显提速。
下一步可以跑 Step A：SF100 tuned import-only matrix。
不建议直接跑完整 Step B full 6 scenarios；应先跑 minimal scenarios。
```

## 保留/删除

当前保留：

```text
remote-logs/w14-tuning-sf30-edge-import-20260616-codex1
store/w14-tuning-sf30-edge-import-20260616-codex1
remote-logs/w14-tuning-sf1-ab-20260616-codex1
store/w14-tuning-sf1-ab-20260616-codex1
```

保留原因：作为 tuning smoke 证据。若需要释放空间，可删除两个 store，保留 remote-logs。
