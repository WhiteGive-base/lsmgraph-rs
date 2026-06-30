# lsmgraph-rs

`lsmgraph-rs` 是一个基于 Rust 的 LSM 风格动态属性图存储引擎原型。公开分支只保留代码、测试和必要说明，不包含数据集、生成的 store、实验日志、论文草稿或 vendored LDBC driver。

## 包含内容

- Tokio engine shell 和 bounded pread/pwrite I/O 抽象。
- 可选 Linux `direct-io` 与 `io-uring` 后端。
- MemGraph rotation、L0 version chain、L0 到 L1 compaction、多层查找。
- CSR segment reader/writer、稀疏 source offset 和语义元数据。
- SNB 拓扑/属性导入、DGS-compatible HTTP adapter。
- 覆盖 engine、CSR、schema、semantic、lifecycle 行为的回归测试。

## 构建

```bash
cargo test
cargo build --release
cargo build --release --features direct-io,uring
```

`direct-io` 和 `uring` 主要面向 Linux。做基础可移植性检查时先使用默认构建。

## 数据路径

示例默认使用环境变量，不假设任何私有服务器目录：

```bash
export LDBC_SNB_DATA=${LDBC_SNB_DATA:-data/social_network}
export LSMGRAPH_STORE=${LSMGRAPH_STORE:-store/sf1-full}
```

`LDBC_SNB_DATA` 指向用户自行准备的 LDBC SNB social network 数据目录。`LSMGRAPH_STORE` 是本地生成目录，已被 Git 忽略。

## 导入示例

导入 `person_knows`：

```bash
target/release/lsmgraph import \
  --input "$LDBC_SNB_DATA" \
  --data-dir store/sf1 \
  --relation person_knows
```

导入当前支持的 SNB topology：

```bash
target/release/lsmgraph import \
  --input "$LDBC_SNB_DATA" \
  --data-dir store/sf1-topology \
  --relation all-topology
```

导入 label-encoded SNB topology 和属性：

```bash
target/release/lsmgraph import \
  --input "$LDBC_SNB_DATA" \
  --data-dir "$LSMGRAPH_STORE" \
  --relation snb-full
```

重建 SNB adjacency cache：

```bash
target/release/lsmgraph snb-cache --data-dir "$LSMGRAPH_STORE"
```

## HTTP Adapter

```bash
target/release/lsmgraph snb-server \
  --data-dir "$LSMGRAPH_STORE" \
  --host 127.0.0.1 \
  --port 9090
```

Java driver 和 validation 参数文件属于可选外部资产；如需使用，请通过 CLI 参数显式传入路径。

## License

本项目使用 MIT OR Apache-2.0 双许可证。
