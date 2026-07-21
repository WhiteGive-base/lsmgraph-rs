# TuGraph P10 正式运行就绪状态

状态：**adapter/worker/contract 已实现并通过隔离 fixture；正式 SF10 数据尚未具备，因此不得开始正式 timing。**

## 已完成

- TuGraph 4.5.2 C++ embedded worker；同进程 warmup → measured。
- 固定 1700 查询顺序、truth digest、34 个 typed-edge label。
- 逐查询 latency/status/count/sum/xor，以及 measured P50/P95/P99 和 phase events。
- 原生 worker、`liblgraph.so`、headers、GCC 8 build image、编译器和 Docker client 的 SHA/provenance。
- store tree lineage、dataset/truth/importer/config lineage 和 dense VID 合约。
- `CIDR_TUGRAPH_PASSWORD` 的非 argv、哈希校验登录契约。
- P02B formal admission fail-closed；P31 以 adapter 根进程覆盖原生 worker 子进程。
- 真实 TuGraph 小 fixture：1700 warmup + 1700 measured 全部通过，无 mismatch/timeout。
- 共享 P10/P31 测试 8/8 通过。

## 正式运行仍缺少的外部依赖

1. **匹配的 TuGraph SF10 store 不存在。** 需要从 P01 的同一 dense graph 输入重新导入，内部 VID 必须从 0 连续且等于 dense ID，34 个 edge label 必须与 `sf10-edge-type-labels.tsv` 一致。
2. 需要冻结 importer、import config、source revision，并由 P02B 生成该 store 的 canonical tree SHA-256。
3. 需要用权威 dataset/truth 路径生成 `formal_eligible=true` 的 TuGraph store manifest。
4. 需要在正式 artifacts 目录重新构建 worker/runtime manifest，并把其 SHA 写入六系统 formal suite manifest。
5. 需要设置与 store manifest 匹配的 `CIDR_TUGRAPH_PASSWORD`。
6. 需要服务器进入干净窗口并取得 P02B formal PASS；P02B 未放行时 adapter 会在打开 store 前失败。

## 明确不复用的现有对象

当前运行中的 `tugraph_finbench` 是 TuGraph 4.0.0 外部服务容器，挂载的数据与 P10 matched SF10 store 无关。它不是本 adapter 的 runtime、build store 或 P31 统计对象；本实现从未启动、停止、重启、exec 或挂载其数据库。

## formal suite 的 TuGraph system 片段

见 `formal-system.template.json`。填入真实绝对路径和 SHA 后仍需通过六系统 manifest 的 formal 静态校验；模板本身不是可运行的 formal manifest。

## 可重复兼容性结论

- 宿主 GCC 9 编译的 4.5.2 C/C++ embedded probe：进入 Galaxy 时 SIGSEGV。
- 把宿主 `libgfortran.so.4` 直接放入 Ubuntu 18.04 runtime：因需要 `GLIBC_2.29` 而 fail-closed。
- digest-pinned Ubuntu 18.04 image 内 GCC 8.4 离线编译、宿主原生运行：Galaxy 建库和完整 adapter fixture 均通过。

因此正式入口固定为“镜像内 GCC 8 构建 + 宿主原生 embedded 运行”，不得切回宿主 GCC 9，也不得降级成 TuGraph 4.0.0 冒充 4.5.2。
