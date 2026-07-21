# P10 NebulaGraph 真实适配器

该适配器执行真实 NebulaGraph 3.8.0 nGQL typed-neighbor 查询：

```ngql
GO FROM <dense_src> OVER <edge_label> YIELD dst(edge) AS dst
```

查询计时使用 `CLOCK_MONOTONIC`，边界覆盖客户端调用、结果物化和
`count/sum_hash/xor_hash` 摘要计算。一个 session 依次完成全部 warmup 和
measured 查询，中间不重启 graphd、storaged、metad，也不重建连接池。

## 冻结身份

- graphd：`vesoft/nebula-graphd@sha256:1040573cc684ea6cc5e673b667422e9abab607c9d553c2c17c7bac6ad8a74e05`
- metad：`vesoft/nebula-metad@sha256:ab687bd32d3e441d436842b41427ba0961a5e169c46997ef03fa4dcfd5388b35`
- storaged：`vesoft/nebula-storaged@sha256:7142642ee69001a5b5c50520196538d58bb2ec3930707c067cb4c4e2be3890f9`
- Python 客户端：`nebula3-python==3.8.3`，同时冻结完整 `pydeps` 文件树和
  dist-info `METADATA` SHA-256。

`build_runtime_manifest.py` 会核对 tag 的 RepoDigest 和本地 image ID；不接受
`latest`、仅有 tag 而无 digest 的镜像，或未冻结的 Python client。

## 两种生命周期

- `managed-fixture`：仅限 fixture。使用唯一的三个容器名、唯一 internal
  network 和 Docker 分配的 localhost 随机端口；fixture 完成后清理其拥有的
  containers、network 和临时数据，绝不复用已有服务。
- `external-prestarted`：仅限 formal。适配器只连接已经启动且由 P31 明确覆盖的
  三个容器，不启动、停止或重启服务。warmup 和 measured 共用一个 session。

formal store 必须是历史 SF10 store 的新 reflink/copy，不能把
`baseline/.../sf10-main-r1/data` 原目录直接交给适配器。应在服务启动前对副本运行
`build_store_manifest.py --mode formal`，将其 tree SHA、数据集、1700-query truth、
importer/result provenance 冻结；之后再以该副本启动唯一容器组。

## Formal 入口约束

formal adapter args 必须同时提供：

- runtime manifest 及其 SHA-256；
- store manifest 及其 SHA-256；
- canonical P02B result、validator 及 validator SHA-256；
- `CIDR_NEBULA_PASSWORD`，其 SHA-256 必须匹配 store manifest。

适配器和共享 P10 validator 会双重拒绝以下情况：非 canonical 1700-query truth、
store/dataset/truth 血缘漂移、三个镜像任一个漂移、client 版本或文件树漂移、容器
名单与 suite/P31 不一致、缺 P02B PASS、session 重开或服务重启。

当前禁止在服务器重负载阶段运行 SF10 性能计时。历史 44 GiB store 只能在新路径
做 correctness-only 验证，并将 `performance_eligible=false` 写入外层运行 provenance。
