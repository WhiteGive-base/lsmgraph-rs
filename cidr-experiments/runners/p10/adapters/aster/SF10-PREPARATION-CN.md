# Aster SF10 store 准备流程

`prepare_sf10_store.py` 只生成 correctness-only 冻结 store，不产生论文性能点。
它严格执行以下顺序：

1. 校验 final worker、canonical dense/truth、P01 manifest/FORMAL-PASS 和 P02B
   SHA256SUMS；校验 integration 与 Aster source 均 clean。
2. 在全新空 `db/` 中调用 Aster adapter 的 `fresh` 生命周期。
3. 关闭建库进程后离线生成 `aster-store-manifest.json`。
4. 对同一冻结 store 执行一次 warmup 1,700 queries 和一次 measured 1,700
   queries；要求每阶段 0 timeout、0 mismatch、总邻居数 84,104,814。
5. 再次校验逻辑 store SHA、repo/source 状态，最后才原子写入
   `DONE.correctness-only`。

任一阶段失败只保留 `FAILED.json`、阶段命令、stdout/stderr 和已有 store；runner
不会删除或覆盖失败证据，也不会续跑已有目录。SIGINT/SIGTERM/SIGHUP 会终止当前
子进程组并留下失败标记。runner 不启动、停止或探测任何数据库服务。

## 正式准备命令

先创建一次固定父目录；每次尝试必须使用新的 run ID：

```bash
install -d -m 0755 /data/WorkSpace/results/P10-ASTER-STORES

cd /data/WorkSpace/lsmgraph-cidr-integration
timeout --signal=TERM --kill-after=30s 100m \
  ionice -c2 -n7 nice -n10 \
  python3 -B cidr-experiments/runners/p10/adapters/aster/prepare_sf10_store.py \
    --output-root /data/WorkSpace/results/P10-ASTER-STORES/P10-ASTER-SF10-FRESH-YYYYMMDDTHHMMSSZ-6abb258e \
    --repo-root /data/WorkSpace/lsmgraph-cidr-integration \
    --source-root /data/WorkSpace/lsmgraph-cidr-deps/Aster-clean \
    --binary /data/WorkSpace/results/P10-ASTER-BUILD/P10-ASTER-BUILD-20260721T200600Z-6abb258e/bin/aster_p10_worker
```

wrapper 会验证自身实际处于 `nice >= 10` 和 `ionice best-effort/7`；省略优先级前缀
会在读取 6.6 GB dense 文件前 fail closed。

## 产物与验收

成功目录至少包含：

- `fresh-request.json`、`fresh-output/`；
- `aster-store-manifest.json`；
- `reopen-request.json`、`reopen-output/`；
- 三阶段 command/result/stdout/stderr；
- `preflight.json`、`preparation-summary.json`、`provenance.env`；
- 小型审计产物的 `SHA256SUMS`；
- `DONE.correctness-only`。

最终必须同时满足：`checked=1700`、`mismatches=0`、`timeouts=0`、
`total_neighbors=84104814`、`performance_eligible=false`、
`formal_performance_points=0`。物理 store 文件由 `aster-store-manifest.json` 内的
逐文件 SHA 覆盖，不在顶层 `SHA256SUMS` 中重复读取。

历史核心 import 为 13.24 分钟。final worker 的 fresh import/correctness 预计
16--27 分钟，离线 freeze 3--8 分钟，冻结 reopen correctness 8--15 分钟；整体预计
27--50 分钟，外层保守超时为 100 分钟。预计最终新增 13--16 GB，执行前预留
20 GB；峰值内存按 16--22 GiB 估计，建议至少保留 24 GiB。
