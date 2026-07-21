# Windows → Linux 目录同步校验

校验时间：2026-07-21 23:29 CST  
Windows 源目录：`E:/文档/DGS项目复现/cidr-experiments/`  
Linux 目标目录：`/data/WorkSpace/lsmgraph-rs/cidr-experiments/`  
远端分支：`codex/sf100-basegraph-bench`  
同步前远端 HEAD：`3d09ecb98b066e77839f9b09995f4b9e0e196e7c`

## 第一遍完整校验

| 项目 | Windows | Linux | 结果 |
|---|---:|---:|---|
| manifest 内 payload 文件 | 144 | 144 个 `sha256sum -c` OK | PASS |
| 含 `SHA256SUMS` 的目录文件总数 | 145 | 145 | PASS |
| `SHA256SUMS` SHA-256 | `58eb9d1273d10df863ea81116ef833355294c975455d2c31538fe3eb4ccb2ea9` | 同左 | PASS |
| 传输归档条目 | 145 | 145 | PASS |
| 传输归档 SHA-256 | `653046da5ce0c2c1493a79fd7e75a21a5cd4651f2e093bdf9fcfa1213c5ef764` | 同左 | PASS |

初次生成的 manifest 使用 CRLF，Linux 校验将 `\r` 识别为文件名的一部分并拒绝；该版本未提交。随后 manifest 固定为 UTF-8/LF，Linux 返回 144/144 `OK`。这一失败记录保留，用于说明校验 gate 实际生效。

本报告加入目录后，提交前必须重新生成一次 manifest、同步并执行最终全量校验；最终 manifest 的 SHA-256 和 commit SHA 由提交记录与交付摘要给出，避免在被 hash 的文件中形成自引用。

## 复现命令

```bash
cd /data/WorkSpace/lsmgraph-rs/cidr-experiments
sha256sum -c SHA256SUMS
find . -type f | wc -l
sha256sum SHA256SUMS
```

判定规则：所有 manifest 条目必须为 `OK`，目录文件总数必须等于 manifest 行数加一；任一缺失、额外文件或 hash mismatch 均不得 commit。
