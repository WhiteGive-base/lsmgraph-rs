# External Baselines 中文版

## LiveGraph SF10

LiveGraph 是当前包中唯一真实外部图系统 baseline。

| metric | value |
|---|---:|
| edges | 355,185,382 |
| vertices | 29,987,835 |
| load time | 1,502.59s |
| peak RSS | 约 45.7 GiB |
| block store | 37.58 GB |
| WAL | 1.0 GB |
| positive core edge types scan weighted avg | 2.742us |
| edge_type=1 avg / p99 | 3.377us / 15.197us |

边界：External comparison 是 SF10-only。LiveGraph SF100 load 预计 17-21 days，不可行。

## RocksDB-style KV-LSM

这是 internal “external-style” baseline，不是官方 RocksDB。可写 RocksDB-*style* / KV-LSM，不可写 “beats RocksDB”。

## KV-style encoding simulation

这是 simulation，不是 measured system result。必须标注 simulated。

