# CIDR 正式实验并发分组与资源门控

> 适用主机：`finbench`。本文件只定义调度与门控；不会启动 benchmark，也不改变 P20 实现。

## 1. 已核验的主机边界

- CPU：64 个物理核、SMT2、128 个逻辑 CPU；NUMA 0=`0-63`，NUMA 1=`64-127`，每两个相邻 CPU 是同一个物理核的两个线程。
- 内存：495 GiB、无 swap；两个 NUMA 节点各约 248 GiB。
- 工作盘：`/data/WorkSpace -> /dev/nvme1n1p1`，P31 应传 `--device nvme1n1 --data-mount /data`。
- 另一个盘：`/data/finbench -> /dev/nvme2n1`。若某一完整实验系列明确放在该盘，P31 必须同步改为 `--device nvme2n1 --data-mount /data/finbench`；不得在同一张图中混用两种存储设备。
- 2026-07-22 01:10 快照：load=`1.16/1.65/1.63`、CPU idle≈99%、`nvme1n1` 空闲、`/data` 可用约 960 GiB，但 `MemAvailable≈356 GiB`；另有约 118 GiB RSS（124 GB）的其他用户进程及 GPStore/TuGraph 常驻服务。因此当前**不是**正式性能窗口。
- 服务器有 `taskset`、`iostat` 和 cgroup v1 CPU/memory/cpuset 控制器，但没有 `numactl`。正式 T0 前建议安装 `numactl`；若不能安装，只能使用 CPU affinity，并必须在 manifest/进度表中记录此限制。

## 2. 不可破坏的并发原则

1. 正式窗口始终只有“**1 个 benchmark + 它自己的 P31 collector**”。P31 是 sidecar，不是第二个 benchmark。
2. P02B、P10/P11、P20/P21、P32、P40/P41/P42、P51/P52/P53、P60/P61/P62，以及 P70 的正式 cost/fallback 点，所有 system、variant、arm、scale、thread point 和 repeat 均串行。
3. import/load、compaction、全量目录扫描和大文件 SHA-256 也算目标盘 I/O 任务；不得与任何正式 latency/QPS/resource run 重叠。
4. P62 的 `1/4/8/16/32` 是**单个实验内部**的客户端并发度，不代表可以并发启动多个 run。
5. 即使两个 run 位于不同 NVMe，也共享 CPU、NUMA 内存带宽、RAM 和 page cache，仍不得并发作为正式数据。

## 3. 可执行的并发分组

| 组 | 任务 | 最大并发 | CPU/内存上限 | 与正式 run 重叠 |
|---|---|---:|---|---|
| G0 工程/元数据 | P00/P30/P50 parser、P03 manifest、P31 自测、已完成小 TSV 的统计/画图 | 4 | 每任务 4 CPU、16 GiB；共用 housekeeping CPU | 否；正式窗口仅允许心跳和小文件 manifest 写入 |
| G1 正确性 | P02A、W13、P70 correctness-only，均须独立目录且 `performance_eligible=false` | 2 | 每任务 16 逻辑 CPU、64 GiB；两个任务分属两个 NUMA 节点 | 否 |
| G1-I/O | P01 full convert、全量 hash/scan、任何 import/load | 1 | 单任务；内存按数据规模预算 | 否；也不得与另一 G1-I/O 重叠 |
| G2 正式性能 | 上述全部正式 Pxx | 1 | 使用冻结的 formal cpuset 和阶段内存预算 | 只与自己的 P31 sidecar 重叠 |

T0 前允许 G0 与 G1 并行；但同一时刻最多一个 G1-I/O。若 G0/G1 总 PSS 超过 160 GiB，降为一个 G1 加一个 G0；若 `MemAvailable < 300 GiB`，只保留一个 correctness-only 任务。工程工作若可在本地或另一台机器完成，优先移出 `finbench`。

冻结关键链：

```text
P00/P01/P02A/P03 -> P02B -> P10/P11 -> P20/P21 -> P32
                                      -> P42 -> P41  (P40 同阶段但仍串行)
-> P51 -> P52；P53 满足 truth gate 后插入但单跑
-> P60 -> P61 -> P62 -> P70 formal
```

## 4. CPU affinity 与 NUMA 约定

推荐正式默认使用 48 个物理核、每核只用一个 SMT 线程，并在两 NUMA 节点上对称分配：

```bash
HOUSEKEEPING_CPUSET=0-15,64-79
FORMAL_CPUSET=16,18,20,22,24,26,28,30,32,34,36,38,40,42,44,46,48,50,52,54,56,58,60,62,80,82,84,86,88,90,92,94,96,98,100,102,104,106,108,110,112,114,116,118,120,122,124,126
FORMAL_THREADS=48
```

- `HOUSEKEEPING_CPUSET` 保留两侧各 8 个物理核给 wrapper、P31、SSH 和系统服务。
- benchmark 只在 `FORMAL_CPUSET` 上运行；同一图的所有系统使用相同 CPU 集、worker 数和 CPU quota。Rust/Rayon 同时冻结 `RAYON_NUM_THREADS=48`；JVM 记录并冻结 `-XX:ActiveProcessorCount=48`；容器冻结等价 cpuset/quota。
- 推荐调用形态（省略 provenance 参数）：

```bash
taskset -c "$HOUSEKEEPING_CPUSET" cidr-experiments/runners/p31/run_with_resources.sh \
  ... \
  -- taskset -c "$FORMAL_CPUSET" env RAYON_NUM_THREADS="$FORMAL_THREADS" COMMAND ...
```

这样 P31/外层 wrapper 留在 housekeeping 核，benchmark 子进程覆盖为 formal 核。脱离进程组的服务/容器必须在启动时设置同一 `FORMAL_CPUSET`，并传给 P31 的 `--extra-pid`/`--container`。

安装 `numactl` 后，正式命令应在相同 CPU 集上采用 `--interleave=0,1`，避免 SF100 超过单个 NUMA 节点容量，也避免不同系统的 first-touch 策略造成偏差：

```bash
numactl --physcpubind="$FORMAL_CPUSET" --interleave=0,1 COMMAND ...
```

若 P02B pilot 证明 48 核不是共同可实现的公平预算，可在 T0 前改成对称的 32 物理核；一旦 P02B 通过，整篇论文不得中途改变 cpuset/thread/quota。

## 5. 启动门控（连续满足 10 分钟）

所有条件同时为 GREEN，才能启动 P02B 或下一正式点：

| 资源 | GREEN 条件 | 说明 |
|---|---|---|
| 外部负载 | 无未知/他人 CPU、内存或目标盘重负载；外部数据库服务仅保留当前被测系统 | idle 服务也可能唤醒或占 RAM |
| CPU | load1、load5 `< 5`，host idle `> 95%` | 以 10 分钟窗口而非单点判断 |
| 内存 | `MemAvailable >= max(400 GiB, 本点冻结 PSS 预算 + 96 GiB)`；swap 必须为 0 | 初始预算见下表，先用非正式 pilot 校准 |
| `/data` I/O | `nvme1n1 util < 5%`、combined await `< 5 ms`、`aqu-sz < 0.5`、读写合计 `< 50 MiB/s` | P31 指标是整盘指标，所以同盘任务必须清空 |
| 磁盘容量 | `free >= max(300 GiB, 180 GiB + 预计剩余峰值增长)` | P32 另要求启动时 `free >= 850 GiB`；取两者较严者 |
| 稳定性 | P02B 三次：QPS CV `<= 3%` 且 P99 CV `<= 5%` | 未通过则保持 HOLD，不得放行正式链 |
| 可复现性 | clean Git、binary/dataset/config/query/truth SHA-256 冻结，cache policy 和 runner 参数冻结 | 正式 P31 不得使用 `--allow-missing-aux-tools` |

初始 PSS 预算仅用于 admission，必须由 `performance_eligible=false` pilot 校准后冻结：

| 数据级别 | 初始单 run PSS 预算 |
|---|---:|
| SF1/correctness | 64 GiB |
| SF10/外部系统 | 96 GiB |
| SF30/dynamic | 192 GiB |
| SF100/P32/P61 | 320 GiB |

若 pilot 超预算，不得边跑边放宽：先更新进度表中的冻结预算，再重新做 10 分钟 clean gate 和 P02B。

P32 的 850 GiB 只是必要条件，不是容量保证。当前约 960 GiB free，距离 180 GiB 硬停线只有约 780 GiB；若预计新增峰值达到 800 GiB，必须先冻结“任一时刻仅保留一个 active derived store”的 retention 方案。完成校验与 SHA-256 后再按既定策略归档/回收 derived store；不得临场手工删除。

## 6. 运行中动态降级

P31 的整盘 util/await 可能来自 benchmark 本身，**不能仅因 run 中 util 高就判外部干扰**。动态规则以外部进程、内存/容量硬界和跨 repeat 稳定性为主：

| 状态 | 触发条件（任一） | 动作 |
|---|---|---|
| GREEN | 未触发下列条件 | 继续当前点；仍不启动第二个 benchmark |
| YELLOW / HOLD | 新外部任务持续占用 `>2` 个核 30 s；发现目标盘外部 I/O；`MemAvailable < 96 GiB` 30 s；PSS 超冻结预算 10%；free `<300 GiB` 或按当前增长预计下一点会跌破 180 GiB；QPS/P99 CV 越界 | 当前短 repeat 可在安全余量内完成，但标记 `SUSPECT_LOAD`、不得纳入论文，也不得启动下一 repeat/variant；恢复 GREEN 10 分钟后重跑该点。发生重负载干扰后先重跑 P02B sentinel |
| RED / SAFE_STOP | `MemAvailable <64 GiB` 15 s、OOM/分配失败；free `<180 GiB`；ENOSPC/设备错误；外部任务持续 `>8` 个核或 `>32 GiB RSS` 并影响本 run；correctness mismatch；P31 collector/validator 失败 | 在 runner checkpoint 安全停止；向最外层 wrapper 发一次 TERM，让其记录 `FAILED`，不手工 `kill -9`，不伪造 `DONE`；保存 attempt 后诊断 |

所有正式点先跑 3 个独立 repeat；只有 QPS CV `>3%` 或 P99 CV `>5%` 时扩到 5 个。repeat 必须串行，且每个 repeat 都有独立 run 目录与 P31 manifest。

## 7. 进度表可直接使用的块

```markdown
### 正式窗口资源状态

| 项 | 当前值 | 门槛 | 状态 |
|---|---:|---:|---|
| load1/load5 | TBD | <5 / <5，连续10分钟 | HOLD |
| CPU idle | TBD | >95%，连续10分钟 | HOLD |
| MemAvailable | TBD | >=max(400GiB, PSS预算+96GiB) | HOLD |
| /data util/await | TBD | <5% / <5ms | HOLD |
| /data free | TBD | 通用公式；P32>=850GiB；硬停180GiB | HOLD |
| P02B sentinel | TBD | QPS CV<=3%，P99 CV<=5% | HOLD |

- 当前调度组：G0 / G1 / G1-I/O / G2
- 正式并发数：0 或 1（P31 sidecar 不计）
- CPU：HOUSEKEEPING=`0-15,64-79`；FORMAL=48个对称物理核；threads=48
- 当前动作：WAIT_CLEAN / RUN / HOLD / SAFE_STOP
- 干扰与处理：TBD
```
