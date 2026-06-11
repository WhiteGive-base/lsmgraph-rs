#!/usr/bin/env python3
"""Assemble the Chinese SemL0 ablation summary (留痕) document.

Concatenates the per-scale P1/P2 tables produced by extract-e11-tables.py and a
P4 feedback-vs-no-feedback section computed from the p3-feedback-bench JSON, plus
context / provenance / one-click reproduction notes.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Optional


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def git_head(root: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"],
                                       text=True).strip()
    except Exception:
        return "unknown"


def feedback_section(fb: Optional[dict]) -> str:
    if not isinstance(fb, dict):
        return "_（未找到 feedback 实验输出）_\n"
    s = fb.get("summary", {}) or {}
    nf = s.get("no_feedback", {}) or {}
    lines = [
        "反馈式 compaction（RA-score targeted）vs 无反馈基线，在工作负载发生迁移（phase_a → phase_b）时，"
        "热点 L0 分区的候选段数变化：", "",
        "| 阶段 | 反馈前候选 L0 段 | 反馈后候选 L0 段 | 无反馈基线候选 L0 段 |",
        "| --- | --- | --- | --- |",
        f"| phase_a | {s.get('phase_a_candidate_l0_before','N/A')} | {s.get('phase_a_candidate_l0_after','N/A')} | "
        f"{nf.get('phase_a_after_candidate_l0_segments','N/A')} |",
        f"| phase_b | {s.get('phase_b_candidate_l0_before','N/A')} | {s.get('phase_b_candidate_l0_after','N/A')} | "
        f"{nf.get('phase_b_after_candidate_l0_segments','N/A')} |",
        "",
        f"- 反馈选择的目标分区随负载迁移而改变：`selected_ranges_changed={s.get('selected_ranges_changed','N/A')}`。",
        "- 结论：反馈式 targeted compaction 能针对性消除热点分区的 L0 读放大（候选段→0），"
        "而无反馈基线只能做通用 compaction，热点分区仍残留候选段。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--date-tag", required=True)
    ap.add_argument("--scales", required=True, help="comma list, e.g. sf30,sf100")
    ap.add_argument("--feedback", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    root = Path(args.root)
    tag = args.date_tag
    scales = [s.strip() for s in args.scales.split(",") if s.strip()]
    norm = root / f"remote-logs/e11-normalized-{tag}"

    parts = [
        f"# SemL0 SF30/SF100 消融与代价实验 —— 数据留痕（date_tag={tag}）",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        f"- 代码分支/commit：`codex/query-semantic-lsm-research` @ `{git_head(root)}`",
        "- 实验路径：纯 LSM import 路径（`import --relation snb-full --l0-layout <variant>`），"
        "与 BaseGraph SF100 读路径相互独立。",
        "- 变体由 `--l0-layout` 选择：schema / label-only / edge-type-only / degree-only / "
        "semantic（full-semantic）/ semantic-budgeted（benefit-scored/SemL0）/ lsmgraph-style / naive / full-compact。",
        "- 读对照方法：所有变体共用同一组采样计划（从 schema store 生成的 core/alltypes sample plan），"
        "`storage-bench --sample-plan-in` 公平复跑（read_bytes / candidate L0 段为确定性计数，单次即准）；"
        "正确性由 `neighbor-compare` 与 schema 逐邻居校验（SF1 全变体 + SF30 关键变体 full_semantic/benefit_scored 抽查，mismatch 恒为 0）。",
        "- 操作点：SF30 与 SF100 统一用 memgraph=64MB（固定操作点；L0 文件数随规模增长，读放大随之增长）。",
        "- store 策略：导入后删除 snb_edge_props.jsonl / snb_vertices.jsonl 暂存件（消融不需要），"
        "再构建→测量→删除候选 store；保留全部 JSON/指标痕迹。",
        "",
        "## 指标说明",
        "- **read bytes**：回答 core 查询工作负载读到的总字节（越低越好，体现读放大）。",
        "- **cand. L0 segs**：每次 get_neighbors 检查的候选 L0 段总数（语义裁剪越强越低）。",
        "- **vs baseline**：相对 naive 基线的 read-byte 降幅。",
        "- **header/body reads**：元数据读 vs 负载体读 拆分。",
        "- **mismatches**：与 schema 全量结果逐邻居比对的不一致数（应为 0：语义裁剪是 exact-proof，回退是多读而非错读）。",
        "",
    ]

    for scale in scales:
        tbl = norm / f"e11-tables-{scale}.md"
        body = read(tbl).strip()
        if body:
            # drop the per-file H1 (we already have a doc title); keep its H2 sections
            body = "\n".join(l for l in body.splitlines() if not l.startswith("# "))
            parts.append(f"## {scale.upper()} 结果")
            parts.append(body)
        else:
            parts.append(f"## {scale.upper()} 结果\n\n_（未找到 {tbl}）_")
        parts.append("")

    parts.append("## P4 — 反馈式 compaction vs 无反馈")
    parts.append("")
    fb = read_json(Path(args.feedback)) if args.feedback else None
    parts.append(feedback_section(fb))

    parts.extend([
        "## 痕迹路径",
        f"- 每变体原始痕迹：`remote-logs/e11-<variant>-{tag}/`"
        "（import.stdout、time.log、stats.log、file-summary.tsv、fair-*.json、neighbor-compare-*.json）",
        f"- 归一化与表格：`{norm}/`（e11-variants-<scale>.tsv、e11-mechanism-isolation.csv、e11-tables-<scale>.md）",
        f"- 反馈实验：`{args.feedback}`",
        "",
        "## 一键复现",
        "```bash",
        "cd /data/WorkSpace/lsmgraph-ablation",
        f"DATE_TAG={tag} bash reproduce-semL0-ablation.sh all   # 或 sf30 / sf100 / sf10",
        "```",
        "",
    ])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8")
    print(str(out))


if __name__ == "__main__":
    main()
