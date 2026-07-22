# CIDR external-system 最终组合预检

审计时间：2026-07-22 08:37 CST  
性质：只读工程预检；未运行 Docker、SF10 或大规模 I/O；正式性能点为 0。

## 1. 本轮冻结快照

| 分支 | HEAD | 状态 |
|---|---|---|
| clean integration | `63e207ba2c0216af925c3849753209cc5c3a1c12` | clean |
| Neo4j candidate | `b53f9db8940e0a2e3a59b5a665f277b0a1a194ea` | clean；仍缺 strict-3 与 authoritative Entrypoint/Cmd |
| LiveGraph LG4 committed base | `9be66bcffdbac7b20d6f2add3a3f6384fef636b9` | clean；作者后续三批尚未计入 |
| NebulaGraph F2 | `6c8dafb24375729dbe68f8227d2678532f0d7821` | clean；F3/F4 尚未计入 |

上述四组 classic `merge-tree`（main↔Neo4j、main↔LG4、main↔Nebula、LG4↔Nebula）均无文本冲突。这只证明当前提交树可自动合并，不代表正式证据链已经通过。

## 2. 语义冲突热点

- Neo4j/shared 基线大改 `p10_contract.py`、`run_suite.py`、P31 collector/schema/validator、`suite-manifest.schema.json` 和 P20 consumer tests。
- NebulaGraph 又在 Neo4j 基线之上修改 `p10_contract.py`、`run_suite.py`、`adapter-request.schema.json` 及 lifecycle/store/import receipt。
- 当前已提交 LG4 仅修改 LiveGraph 专属文件；后续 binding/shared 批次会改变这一结论。
- P02B 当前无文本冲突，但最终 request 必须继续绑定同一个 PASS receipt、repo root/HEAD、sentinel binary、冻结输入 SHA 和 lineage。
- `b53f9db` 不能直接合并：formal repeats 必须精确等于 3，Neo4j `Config.Entrypoint/Cmd` 必须由权威 baseline 逐阶段绑定。

## 3. 冻结合并顺序

1. 合入 Neo4j/shared 基础栈与独立的 `strict repeats==3 + Neo4j Entrypoint/Cmd` 提交。
2. 对最终 shared 基线重新冻结 SHA，并对 NebulaGraph、LiveGraph 分别重跑只读 merge-tree/共享文件预检。
3. 合入 NebulaGraph F1--F4；它对共享 dispatch/schema 的改动最大。
4. 合入 LiveGraph binding，再合入 LiveGraph build/lifecycle。
5. 运行组合纯测试、三套真实 tiny correctness，最后更新文档与全目录 SHA 清单。

若保留提交图，优先直接 merge 最终分支。若线性 cherry-pick，不得 cherry-pick Nebula 的 merge commit `e82105a`，也不得重复拣入其已包含的 Neo4j 祖先提交。

以下每批新提交都必须二次预检，不能沿用本报告的无冲突结论：

- shared strict-3 + Neo4j Entrypoint/Cmd；
- LiveGraph binding；
- LiveGraph build/lifecycle；
- NebulaGraph F3；
- NebulaGraph F4。

## 4. 组合纯测试 gate

- P02B：现有 14/14 全过。
- 共享 P10：`test_p10_orchestrator.py`、`test_p31_consumer_contract.py`。
- P31/P20：P31 Python tests + shell smoke；P20 全部 `test_*.py`。
- Neo4j：hardening、runtime lifecycle、sealed receipts、strict template、adapter contract。
- NebulaGraph：formal request、formal cluster、run-suite lifecycle、store-v2；本阶段排除真实 Docker fixture。
- LiveGraph：build receipt、formal launcher、adapter/lifecycle；不能把整组因缺 binary 而 skip 当作 PASS。
- 原有系统：Aster、SemL0、TuGraph 全回归。
- 全局负例：formal repeats `1/2/4` 必须拒绝，`3` 必须接受；六系统均覆盖。
- 只允许预先登记的真实 fixture skip，不允许新增或意外 skip。

## 5. 真实 tiny 顺序

纯测试全绿后，在最终集成 commit 上串行运行：

1. LiveGraph：最终 source HEAD 构建 binary/library；无 ambient `LD_LIBRARY_PATH`；验证 tiny truth、PID/start-ticks/P31 绑定与退出清理。
2. Neo4j：官方 `5.26.24` RepoDigest、authoritative Entrypoint/Cmd、numeric uid:gid；执行 import→launch→readiness→P31→stop→rm→absence。
3. NebulaGraph：三容器精确 RepoDigest；logical alias 与 RAFT host identity 一致；验证真实 `SHOW PARTS` 列结构和精确 ID/network 清理。

三组均为 correctness/engineering-only，必须写 `performance_eligible=false`，不得计入正式 timing。

## 6. 最终同步 gate

当前 `SHA256SUMS` 的 145/145 PASS 只证明已列条目自洽；审计时 `cidr-experiments/` 实际有 407 个文件。全部工程合并后必须重新生成覆盖全目录（除 manifest 自身）的清单，并满足：

- Linux 文件总数 = manifest 行数 + 1；
- Linux `sha256sum -c SHA256SUMS` 全部 PASS；
- Linux→Windows 反向同步后相对路径集合完全一致；
- Windows/Linux 逐文件 SHA-256 完全一致；
- 最终 commit 只在上述检查完成后创建。
