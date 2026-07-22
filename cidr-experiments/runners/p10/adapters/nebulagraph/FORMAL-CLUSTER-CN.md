# NebulaGraph formal 集群生命周期门禁

`formal_cluster.py` 只负责 P10 计时区间之外的集群生命周期。P10 adapter 仍是
`external-prestarted`：计时期间只连接，不启动、停止、重启或修改 store。

## 固定不变量

- 镜像必须是 runtime manifest 中的三个精确 RepoDigest 和本地 image ID；禁止 tag-only 启动。
- 每个 repeat 使用唯一的三个容器名和一个唯一 network；容器名不能等于历史 RAFT 身份。
- metad、storaged、graphd 的 network alias 固定为历史 SF10 store 中的三个 RAFT 身份。
- 三个 data mount 必须来自同一个 store-v2 clone 的 `meta/`、`storage/`、`graph/`；日志使用独立目录。
- 只有 graphd 的 `9669/tcp` 映射到 `127.0.0.1`；restart policy 必须为 `no`。
- start 后重新核对容器 ID、image ID、RepoDigest、命令、挂载、端口、network、StartedAt、PID 和 restart count。
- live gate 必须看到三个精确 `ONLINE` host、1..64 分区、replica factor 1、INT64 VID、34 个精确 edge schema，以及零 tag、零 edge index、零 tag index。
- nGQL 有每条查询超时；POSIX `setitimer` 对整个启动/门禁阶段施加硬截止时间。

## 三阶段回执

1. `preflight`：重新消费 request/runtime/store/P02B，核对 clean Git HEAD、当前主机指纹、输入 SHA、client tree、离线 mount、名字空闲、端口空闲，并输出不可覆盖的 preflight receipt。
2. `start`：只消费 SHA 绑定且未过期的 preflight receipt，创建唯一 network/容器，通过 live gate 后输出 start receipt。该阶段全部位于 benchmark 计时之前。
3. `stop`：只对 start receipt 中已验证的精确容器 ID 和 network ID 执行逆序 stop/remove，保留角色日志，并输出不可覆盖的 stop receipt。该阶段全部位于 benchmark 计时之后。

`start` 还必须预留 `--partial-output` 和 `--cleanup-output`。若启动中途失败：

- 控制器先写 partial-start receipt，把每项资源标为 `NOT_ATTEMPTED`、`KNOWN` 或 `UNCERTAIN`；
- 仅当所有已尝试资源都有完整 ID，且用该 ID 得到的 inspect 仍严格匹配 preflight 身份时，才自动按 ID 逆序 stop/remove，并写 cleanup `PASS`；
- 任一 ID 不确定、inspect 身份漂移、membership 漂移或 Docker 状态不可确认时，写 cleanup `BLOCKED`，不执行任何按名字的查询或删除；
- `cleanup` 子命令可在环境恢复后消费原 partial-start receipt 再次尝试。每次尝试必须使用新的不可覆盖输出路径。

所有 receipt 路径必须位于 Git worktree、store clone 和日志目录之外，父目录必须预先存在；已有文件一律拒绝覆盖。

## 操作约束

- `preflight` 后若任何输入、Git HEAD、主机身份或 client tree 改变，原 receipt 自动失效，必须重新生成。
- `start` 失败后，只有 cleanup `PASS` 才能证明已知 ID 无残留；cleanup `BLOCKED` 时应保留终端错误和角色日志，释放阻塞条件后用显式 `cleanup` 重试，不得复用该 repeat 的路径和名字。
- `stop` 前若 PID、StartedAt、image、restart count、network membership 或任一容器 ID 漂移，控制器 fail closed，不执行 teardown。
- 正式运行前仍需满足服务器资源 sentinel；本控制器不会把重负载环境下的数据标记为 performance-eligible。

多个 repeat 必须先通过 `validate_repeat_isolation()`：repeat index 恰为 1..N，store clone 与日志根目录两两不重叠，容器名、network 名和 graph host port 全局唯一；镜像、client、认证、space、schema 与 query timeout 则必须完全相同。`run_suite.py run --repeat-index N` 可只执行一个已隔离 repeat，并保留真实的 `repeat_index=N`；只跑部分 repeat 时只能产生 `PARTIAL-DONE`，不能产生 `DONE`。

## 静态验证

`test_nebulagraph_formal_cluster.py` 使用临时目录和 mock Docker/Nebula client 验证上述契约，不启动或查询任何真实容器，也不读取正式大 store。
