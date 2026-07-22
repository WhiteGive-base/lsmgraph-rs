# P20 pristine inventory 与 storage-bench digest 审计

## 1. `p20-store-inventory-v1` 精确契约

`run_single_profile.py::validate_store_manifest` 要求顶层至少包含以下字段：

| 字段 | 精确要求 |
|---|---|
| `schema_version` | JSON number `1`；不能是字符串，也不能是布尔值 |
| `state` | `FROZEN` |
| `inventory_schema` | `p20-store-inventory-v1` |
| `store_path` | 与实际 pristine store 解析后的绝对路径完全相同 |
| `scale` | `sf10` 或 `sf30`，且与 run 一致 |
| `l0_layout` | `semantic-budgeted` |
| `dataset_sha256` | 与 run 的冻结 dataset SHA-256 一致 |
| `binary_sha256` | 与 run 的冻结 benchmark binary SHA-256 一致 |
| `inventory_sha256` | 下述 canonical inventory digest |
| `files` | 非空数组 |

每个 `files[]` 对象必须**恰好**只有：

```json
{"path":"relative/posix/path","size_bytes":123,"sha256":"64 lowercase hex"}
```

其中 `path` 必须是非空、安全、相对 POSIX 路径，不能含 `.`、`..`，不能重复；文件必须是普通文件，目录/文件符号链接都被拒绝。builder 额外输出 `file_count`、`total_bytes`、`hash_method`，P20 consumer 允许这些审计字段。

canonical digest 算法与 runner 完全一致：

1. 仅保留每行的 `path`、`size_bytes`、`sha256`。
2. 按 `path` 的字符串顺序升序排列。
3. 对整个数组执行 Python 等价序列化：

   ```python
   json.dumps(rows, sort_keys=True, separators=(",", ":")) + "\n"
   ```

4. 对 UTF-8 bytes 计算 SHA-256。

注意：P02B 的 `sha256-tree-v1` 是对 `file\0path\0size\0sha\n` records 递增 hash；它和 P20 canonical-JSON 算法**不是同一个 digest**。因此现有 P02B `budg-b64` tree SHA `ae77255c...` 只能作为 P02B lineage，不能直接填进 P20 `--pristine-store-sha256`。此外 P02B manifest 没有 per-file hashes，不能改字段名后冒充 P20 manifest。

`build_pristine_inventory.py` 在全量 hash 前后各做一次 file identity 扫描，并对每个文件检查 `(device,inode,size,mtime_ns,ctime_ns)` 在 hash 过程中不变；Linux 上以 `O_NOFOLLOW` 打开文件并对同一 descriptor 前后 `fstat`，store 根目录 identity 也必须稳定。输出必须不存在且不能位于 store 内。`--verify` 会重新计算全部文件 SHA，而不是只比较文件数/大小。

## 2. `storage-bench-result-digest-v1` 精确语义

Rust `storage_bench_result_digest` 对每条 sample query 返回的可见 `EdgeRecord` 先按以下 tuple 排序：

```text
(src, dst, edge_type, ts, marker_as_u8)
```

然后从 FNV-1a 64 offset basis `0xcbf29ce484222325` 开始，依次吸收：

```text
src:u64 LE | dst:u64 LE | edge_type:i32 LE | ts:u64 LE | marker:u8
```

最终格式化为 16 位小写 hex，写入每条 `result_digests[]` 的 `result_digest`；同时记录 `src` 与 `result_count`。该 digest 与返回顺序无关，但会检测 destination/type/timestamp/marker 的变化。

entry digest 从相同 FNV offset 开始，按 sample plan 的原始顺序依次吸收：

```text
sample.src:u64 LE | int(sample.result_digest, 16):u64 LE
```

`build_workload_truth.py` 会从一次稳定、非 symlink 且 SHA 匹配的 bytes 同时完成 hash 与 JSON 解析，避免验 SHA 后二次读取；它重新计算 entry fold，并要求 benchmark 顶层 digest 与唯一 measured round 中的 digest 完全相同，同时核对 sample plan 路径/SHA、entry identity、source 顺序、degree、workload flags、property mode/id、query 数和 reference output SHA。

它不能从摘要 JSON 重新计算单条 EdgeRecord digest，因为摘要不携带完整 edge records；因此输入的 reference output SHA 是显式 trust root，必须来自已审计的 reference/oracle 流程，不能把未经验证的待测 stage 自称为 truth。

## 3. 当前 SF10 输入审计

- 当前可复用 W6 plan：SHA-256 `61f3bd9f391c0a056f76d60c228a44801d00265b8f8798c495e127fbfc424d0c`，9 个 edge types × 1000 samples = 9000 queries。
- degree 分布覆盖实现的三个非空 class：low (`1..16`) 5785、medium (`17..1024`) 3171、high (`>1024`) 44；可复用为 typed-one-hop 与 degree-hint plan，但 high 仅 44，论文中不要声称等额分层。
- 当前 W6 raw `budg-b64-bench.json` 的 `emit_result_digests=false`，不能生成正式 truth。
- 当前 SF10 `budg-b64` 是 `SNB_SKIP_ADJ_CACHE` import，日志明确跳过 vertex JSONL 与 edge-prop JSONL；`SCHEMA_CATALOG.json` 的 `properties={}`。
- 更关键的是，现有 `snb-full` 即使不设置该环境变量，也通过 `engine.insert_edge(...)` 写 LSM topology；SNB edge props 只进入旁路 `snb_edge_props.jsonl`，没有调用 `insert_edge_with_property_values_prototype`，所以不会形成 CSR property columns / property-presence bitmap。
- 旧 W8 SF30 的 schema/budg-b64/semantic `property-presence.json` 均出现 `neighbor_edges=0`；这是 vacuous workload，不能作为 P20 正式 property evidence。
- 只读扫描现存 50+ 个 `SCHEMA_CATALOG.json` 未找到非空 property catalog。因此现在没有可复用的正式 property-bearing SemL0 store。

结论：typed-one-hop/degree plan 可复用，但三类 truth、P20 full inventory 与 A0--A6 raw 尚未生成；property-presence 还额外依赖一个真正把 property values 写入 Engine/CSR 的 materializer/importer，以及独立的 positive-result-aware sample plan。property plan 的 path/SHA 不得与 typed/degree 相同，且每个 `entries[].edge_type` 必须为 `null`；truth builder 会拒绝 typed edge filter 和 `positive_result_samples=0`，防止共线或旧 W8 类型的退化结果被错误封成 PASS。
