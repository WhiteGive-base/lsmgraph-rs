# P20 artifact preparation staging

这是独立于正式 runner tree 的 Python 3.8-compatible staging：

- `build_pristine_inventory.py`：生成/复验完整 `p20-store-inventory-v1`。
- `build_workload_truth.py`：从受 SHA 保护的 trusted storage-bench output 生成一个 workload truth。
- `build_property_stratified_plan.py`：从 300,000-source property candidate pool 确定性选择 500 mixed + 500 zero 的平衡 diagnostic plan，并生成完整 provenance receipt。
- `generate_property_candidate_pool.py`：以固定 exact argv 调用 `storage-bench`，在前后完整校验 store inventory，并把 binary/inventory/typed plan/plan/result/exit code 绑定为 generation receipt。
- `prepare_correctness.py`：冻结 plan/reference 副本，生成三类 truth，逐 cell 新 clone，执行 SF10 A0--A6 correctness，并调用 integrated correctness PASS builder。
- `test_*.py`：tiny fixture、digest/P20-tool tamper、vacuous property、symlink cleanup、pre-existing output 与完整 21-cell mock 回归。
- `INVENTORY-AND-DIGEST-AUDIT-CN.md`：精确 schema/算法和当前 artifact 审计。
- `REMOTE-PREP-PLAN-CN.md`：远端命令、stop gate 与 ETA。
- `PROPERTY-STRATIFIED-PLAN-CN.md`：正式 property plan 的冻结阈值、SF1 calibration 命令和 v2 config 契约。
- `config-sf10.template.json`：带 fail-closed 非法占位符的配置模板。

Engine/CSR property materializer 已在独立 candidate 通过 SF1 smoke；旧 W8 property-presence raw 仍是全零退化结果，不能生成 truth/PASS。当前正式输入仍缺 SF10 property pristine store、受 inventory 保护的 generation receipt 与 balanced diagnostic plan；本 staging 不会把 SF1 calibration 提升为正式证据。

正式 `property-presence` 必须使用独立于 typed/degree 的 sample-plan path 和 SHA；该 plan 的每个 `entries[].edge_type` 必须显式为 `null`，防止 property 曲线退化为某个 typed one-hop 子集。

输入契约额外固定三点：P20 profiles/validator/builder 都必须带预期 SHA-256；plan/reference 只从同一次稳定且 SHA 匹配的 bytes 解析；pristine store 在 21-cell 前后各做一次 full-content verification。所有正式输出路径必须预先不存在。

本地回归：

```bash
python -m unittest discover -s p20-prep-staging -p 'test_*.py' -v
```
