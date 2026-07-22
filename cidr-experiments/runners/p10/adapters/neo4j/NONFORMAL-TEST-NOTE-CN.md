# Neo4j 复审期间的非正式测试记录

2026-07-22 曾误触发旧版真实容器测试入口：

```text
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3.8 -m unittest -v \
  cidr-experiments/runners/p10/tests/test_neo4j_adapter.py
```

旧 `setUpClass` 只在系统临时目录生成 tiny CSV 和 tiny Neo4j store；它先运行带
`--rm` 的 tiny import 容器，再运行随后已 `stop`/`rm` 的 tiny index 容器，之后在
调用 freezer 时因已删除的旧 CLI 参数而失败。该次运行没有进入测试用例，也没有
发布 P10/P31 结果，必须视为 `NONFORMAL_INVALID`，不得作为 correctness 或性能证据。

失败后只做了只读残留检查：按测试容器名前缀查询 `docker ps -a` 结果为空。该入口
没有引用或挂载历史 SF10 store，没有执行 image pull/build/tag/remove，也没有改动
任何正式 store manifest、import receipt 或历史实验结果。后续复审仅运行纯 mock、
语法检查和 no-Docker fixture；真实容器测试必须在明确授权的干净窗口重新运行。
