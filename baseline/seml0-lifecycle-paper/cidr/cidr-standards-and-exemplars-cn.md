# CIDR 标准与参考论文写法

## CIDR 更看重什么

CIDR 的定位是 innovative data systems research。它通常更接受：

- 有风险但清晰的系统思想。
- 对数据系统架构的重新组织。
- 来自系统构建的经验和反例。
- 有说服力但不一定完整工业部署的实验。
- 解释为什么现有系统/benchmark 不完全适合比较。

CIDR 不适合把论文写成“我们在标准 benchmark 上全面打败所有系统”。如果外部 baseline 不完备，CIDR 可以接受，但前提是作者必须诚实说明不可比原因，并用设计矩阵、artifact attempt 和原型结果支撑论点。

官方参考：CIDR 2025 CFP 强调 systems architecture、risky ideas、systems-building experience、resourceful experiments、provocative positions，并接受 research / visionary / ongoing-work papers。

## 可参考的 CIDR 论文模式

### Runtime-Extensible Parsers

参考：`https://vldb.org/cidrdb/papers/2025/p18-muhleisen.pdf`

写法模式：

- 找一个数据库系统中长期被当成底层细节的组件。
- 说明传统做法为什么限制了未来架构。
- 提出一个新的系统接口或抽象。
- 用 prototype 证明功能可行，并用少量实验说明 overhead/benefit。

SemL0 可借鉴：

- Graph query signature 过去属于 query layer；SemL0 把它变成 storage control plane。
- 论文不需要证明完整图数据库优于所有系统，而要证明这个抽象值得成为系统设计原则。

### Functional Decomposition of Storage Formats

参考：`https://vldb.org/cidrdb/papers/2025/p19-prammer.pdf`

写法模式：

- 反对 one-size-fits-all。
- 把已有耦合设计拆成几个可独立组合的功能。
- 提出 design principle，而不是只报一个实现。

SemL0 可借鉴：

- 不要把 SemL0 写成某个固定 layout。
- 写成：graph storage layout 和 query-semantic pruning surface 应该解耦，但由 control plane 协调。

### SalesforceDB

参考：`https://vldb.org/cidrdb/papers/2026/p28-arora.pdf`

写法模式：

- 从真实 LSM 系统痛点出发。
- 讲几项具体系统优化。
- 用 workload evidence 和部署经验支撑。

SemL0 可借鉴：

- 从 LSM 图存储里的 selective graph read / compaction dilution 出发。
- 用 W6/W9/C2/W13 和远端原型部署证明设计有效。
- 注意：SemL0 目前不是生产部署，只能写 prototype deployment。

### SQL Server Bitmap Filters

参考：`https://vldb.org/cidrdb/papers/2026/p29-zhao.pdf`

写法模式：

- 从成熟系统中的一个机制抽象出更一般的系统原则。
- 强调设计取舍，而不是只强调性能数字。

SemL0 可借鉴：

- 从 query semantic pruning 抽象出 storage-level semantic control plane。
- 主张：动态图存储不应只按 key/range/level 组织，也应让 graph query semantics 参与物理生命周期。

## SemL0 应采用的 CIDR 写法

1. 第一页必须讲清楚 provocative thesis：query signatures should become a storage control plane。
2. “生命周期管理”降级为机制，不做标题中心。
3. Evaluation 只保留最能证明设计原则的数字：W6、W9、C2、W13。
4. External baseline 不完成时，不硬凑；写成 design-space comparison 和 artifact attempt。
5. Conclusion 给 future research agenda：semantic control planes for mutable graph stores，而不是只总结实验。
