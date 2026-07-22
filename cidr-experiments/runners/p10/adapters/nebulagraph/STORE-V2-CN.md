# NebulaGraph formal store-v2 证据链

`cidr-p10-nebulagraph-store-v2` 只承认下面这条完整链：

1. `build_import_receipt.py` 是执行器，不再消费人工准备的 PASS JSON。它要求 import target 与所有输出均事先不存在，校验 clean Git HEAD、固定 importer/SHA 和 argv JSON，然后以 repo root 为 cwd、在硬超时内实际执行 importer，并自行捕获 stdout/stderr。
2. importer argv 必须逐项绑定 canonical raw manifest、dense dataset、truth、runtime manifest、唯一 target store、importer report 和 correctness observations；每个 flag 恰好出现一次，路径必须绝对且与 wrapper 参数完全一致。
3. importer 只能输出无 PASS 声明的 loaded-count report 和逐查询 observations。wrapper 根据退出码、实际 report 和 1700 行 observations 自己生成 execution/correctness v2；任何 mismatch、timeout、顺序漂移、输入/目标漂移或非零退出都不会生成 formal import receipt。
4. wrapper 在服务离线状态下两次核对 mount，占用稳定 tree-v2 冻结 source store，并把 wrapper、importer、argv source、输入、输出、report、observations、计数和 store tree 全部 SHA-256 绑定到 `cidr-p10-nebulagraph-import-receipt-v2`。
5. `clone_store.py` 从该 source 创建新的、互不重叠的 repeat store。默认要求 reflink 成功；不支持 reflink 时必须显式选择 `--copy-mode copy`，不会静默退化成 hardlink。工具验证 source/target tree 相同、普通文件 inode 不共享，并用 `renameat2(RENAME_NOREPLACE)` 发布目标。
6. `build_store_manifest.py --mode formal` 再次要求 source import receipt、target clone receipt、当前 offline target tree、P02B raw dataset manifest、runtime manifest、run ID 和 repeat index 全部一致，才派生 `formal_eligible=true`。

旧的 import-receipt v1、`historical-sf10-store-copy-v1`、手写 execution/correctness PASS 只能作为历史参考，不能升级成 formal 数据；正式运行必须由 strict wrapper 实际执行 importer 后重新生成 v2 receipt。

正式 target 的 Docker 容器名每次运行都必须唯一；Nebula 持久 peer identity 固定为：

- `seml0-nebula-meta-sf10`
- `seml0-nebula-storage-sf10`
- `seml0-nebula-graph-sf10`

launcher 必须把这三个名字配置为唯一网络中的 aliases，并把它们用于 `--local_ip` / `--meta_server_addrs`，不能把每次唯一的容器名写入持久 peer identity。

所有 wrapper/freezer/clone 输出必须位于 store 之外；已存在的输出或 target 一律拒绝。符号链接只允许指向同一 store 内已存在目标的相对链接。运行中任一容器 host mount 与 source/target 相等或存在祖先/后代关系时，冻结与克隆都 fail closed。
