# TuGraph 4.5.2 SF10 fresh store 构建

本目录的 `tugraph_sf10_importer.cpp` 只用于从 P01 冻结的 dense edge list
构建一份全新的 TuGraph 4.5.2 embedded store。它不打开、迁移或覆盖已有
TuGraph 数据库。

## 固定不变量

- store 路径必须是绝对路径且在启动时不存在；父目录必须已经存在。
- 顶点严格按 dense ID 从 0 连续创建，并逐点检查 `internal VID == dense ID`。
- edge type 必须精确覆盖 `[-17,-1] U [1,17]`，label 来自
  `sf10-edge-type-labels.tsv`。
- 输入顶点数、边数、数据 SHA-256、importer/runtime SHA-256 和源码 revision
  都写入 import config。
- 密码只从 `CIDR_TUGRAPH_PASSWORD` 环境变量读取；argv、JSON 和日志均不含
  密码明文。
- importer 由 `nice -n 19 ionice -c3` 启动。构建和导入耗时只记作工程日志，
  `performance_eligible=false`、`formal_performance_points=0`。

## 构建

TuGraph 4.5.2 必须使用本机已有、digest-pinned 的 Ubuntu 18.04 镜像内
GCC 8 编译，容器设置为 `--pull=never --network none`。容器仅参与编译；
importer 在宿主机原生运行。

```bash
python3 -B build_tugraph_importer.py \
  --output /ABS/ARTIFACTS/tugraph-sf10-importer \
  --runtime-manifest /ABS/ARTIFACTS/tugraph-importer-runtime.json
```

## 冻结并执行一次 import

以下命令中的 store 路径必须从未存在。`make_sf10_import_config.py` 会完整
计算并验证 dataset SHA，同时把当前环境中密码的 SHA-256（不是明文）写入
config。

```bash
export CIDR_TUGRAPH_PASSWORD='由实验操作者安全提供'
python3 -B make_sf10_import_config.py \
  --dataset /ABS/edges-dense.txt \
  --dataset-sha256 DATASET_SHA256 \
  --edge-type-map /ABS/sf10-edge-type-labels.tsv \
  --store /ABS/FRESH-RUN/store \
  --importer /ABS/ARTIFACTS/tugraph-sf10-importer \
  --runtime-manifest /ABS/ARTIFACTS/tugraph-importer-runtime.json \
  --source-revision FULL_GIT_SHA \
  --expected-vertices 29987835 \
  --expected-edges 355185382 \
  --output /ABS/FRESH-RUN/import-config.json

python3 -B run_tugraph_sf10_import.py \
  --config /ABS/FRESH-RUN/import-config.json \
  --result-json /ABS/FRESH-RUN/import-result.json
```

成功导入后仍需依次生成 P02B canonical store lineage、TuGraph store manifest，
再由最终 P10 adapter/worker 完成 1700 warmup + 1700 measured correctness-only
校验。只有 0 mismatch、0 timeout、总邻居数 84,104,814 才能固化
`DONE.correctness-only`；这些步骤不能产生正式性能点。
