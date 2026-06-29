# Stage 7 — C2 real SF30 read-amplification proxy 总结

日期：2026-06-22  
目的：补强 C2 的真实 SF30 证据。原有 SF30 open-mode 已证明 pruning surface retention 与 write cost；本阶段增加 metadata-level typed-neighbor replay，用真实 post-merge segment metadata 估计 read-amplification 后果。

## 产物

- Runner：`src/bin/c2_merge_retention.rs`
- 脚本：`baseline/path-b-c2/run_c2_sf30_readamp_proxy_20260621.sh`
- 日志目录：`remote-logs/c2-sf30-readamp-proxy-20260621/`
- JSON：
  - `remote-logs/c2-sf30-readamp-proxy-20260621/naive-proxy.json`
  - `remote-logs/c2-sf30-readamp-proxy-20260621/semantic-proxy.json`
- 临时 store：
  - `store/c2-sf30-proxy-naive-20260621`
  - `store/c2-sf30-proxy-semantic-20260621`

## 方法

1. 复用 `store/c2-sf30-base`，避免重新导入 LDBC SF30。
2. 复制出 naive / semantic 两份 store。
3. 对每份 store 执行 `--build-l1-from-l0`，得到真实 SF30 的 exact L1 partitions。
4. 运行 L1→L2 merge：
   - naive：普通 merge，可能输出 mixed segments。
   - semantic：按 `(src_label, edge_type)` 分组输出。
5. 在 post-merge segment metadata 上 replay 40 个 typed-neighbor `(src_label, edge_type)` partitions，统计 candidate segments/bytes。

## 核心结果

| policy | retention | write_amp | output segs | query partitions | avg candidate segs/query | candidate bytes total | exact bytes before | weighted read-amp proxy |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| naive | 0.0 | 1.07 | 503 | 40 | 93.6 | 282.16 GB | 43.25 GB | 6.52x |
| semantic | 1.0 | 1.24 | 528 | 40 | 13.2 | 43.25 GB | 43.25 GB | 1.00x |

## 解读

- Naive merge 在真实 SF30 上把 exact pruning surface 退化成 mixed surface，typed partitions 需要保守读取大量无关 segments。
- Semantic merge 保留 exact surface，使 candidate bytes 等于 exact partition bytes。
- 这补上了真实 SF30 的 read-amplification proxy：不再只是 synthetic rows 证明 read 后果。

## 边界

这不是完整 end-to-end SF30 read workload：

- 没有执行完整 body decode。
- 没有报告 end-to-end latency。
- 不替代 controlled synthetic rows 的 correctness/read workload 证据。

论文中应写成：real SF30 metadata-level replay / candidate-byte proxy，而不是 full SF30 read workload。

## 投稿写法

可写：

> On real SF30, a metadata-level replay over 40 typed-neighbor partitions shows that naive mixed outputs require 93.6 candidate segments/query and 282.16 GB aggregate candidate bytes (6.52x weighted candidate-byte proxy), whereas semantic outputs require 13.2 segments/query and 43.25 GB (1.00x). This bridges the controlled read-amplification result to the real SF30 segment distribution without claiming a full body-read workload.

不可写：

- "SF30 full read workload completed"
- "end-to-end SF30 read latency improved by 6.52x"
- "overhead is negligible"
- "production scheduler validated"
