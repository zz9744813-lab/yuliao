"""策略语义审查材料生成器（拍板前置件，2026-09-23，只读）。

用途：唯一拍板项=8 条 legacy 策略 status hypothesis→verified（语义审查）。
本脚本把每条策略的**审查所需材料**汇总成一份 markdown 审查卡——集霸不必
翻库即可逐条裁定：

每张卡（材料全部只读自查，不虚构）：
- 策略身份：strategy_key / version / status / observation_status / scope；
- 语义内容：abstract_operation / invariants / failure_modes / effect_hypothesis
  （legacy 聚类产物的 effect_hypothesis 多为「未定」——如实显示，这正是
  审查要裁的一部分）；
- 证据概览：strategy_stats 投影（root_works / unique_source_intervals /
  valid / rejected / missing）+ 最多 --samples 条 verified 实例原文引用
  （evidence_text 逐字切片 + observed_content + 根作品 + span）；
- 反例面：rejected 实例数（如实计，当前写入流少产 rejected 行）；
- 审查问句（固定三条，不引导结论）：①抽象操作表述是否成立且无歧义；
  ②所引证据是否真支持该抽象；③是否晋升 status=verified（或需改写/retire）。

用法（--out 已存在即拒——再生成换新文件名，不覆盖既有审查材料）：
    python scripts/strategy_review_dossier.py --out docs/策略语义审查清单_20260923.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                                    # noqa: E402
from app.models import (ExpressionStrategyV2, StrategyCondition,  # noqa: E402
                        StrategyInstance, StrategyStats)

REVIEW_QUESTIONS = (
    "1. 抽象操作（abstract_operation）的表述是否成立、无歧义、可跨作品适用？",
    "2. 所引证据是否真支持该抽象（引用≠支持，请逐条抽查）？",
    "3. 晋升判定：status → verified / 需改写后重新观察 / retired？"
)


def _root_of(s, work_id: str, cache: dict) -> str:
    from app.models import WorkSource
    if work_id not in cache:
        ws = s.query(WorkSource).filter_by(work_id=work_id).first()
        cache[work_id] = (ws.canonical_work_id if ws else None) or work_id
    return cache[work_id]


def build_dossier(s, *, samples: int = 3) -> str:
    stats_by_id = {r.strategy_id: r for r in s.query(StrategyStats).all()}
    cond_by_id: dict[str, int] = {}
    for c in s.query(StrategyCondition).all():
        cond_by_id[c.strategy_id] = cond_by_id.get(c.strategy_id, 0) + 1
    lines = ["# 策略语义审查清单（拍板材料）", "",
             "> 生成者：scripts/strategy_review_dossier.py（只读）；",
             "> 用途：status hypothesis→verified 的语义审查（集霸逐条裁定）；",
             "> 证据纪律：verified 实例均经 span 机械核对（text[span]==evidence",
             "> 逐字相符）+ sha 记录；schema 合格不等于推断成立——引用≠支持。",
             ""]
    for st in (s.query(ExpressionStrategyV2)
               .order_by(ExpressionStrategyV2.strategy_key).all()):
        stats = stats_by_id.get(st.id)
        lines += [f"## {st.strategy_key}（v{st.version}）", ""]
        lines += [f"- status: **{st.status}** | observation: "
                  f"{st.observation_status} | scope: {st.scope}", ""]
        lines += ["### 抽象操作", "", str(st.abstract_operation), ""]
        inv = st.invariants or []
        lines += ["### 保持不变项", ""] + \
                 ([f"- {x}" for x in inv] + [""]) if inv else \
                 ["### 保持不变项", "", "（未定）", ""]
        fm = st.failure_modes or []
        lines += ["### 失败模式", ""] + \
                 ([f"- {x}" for x in fm] + [""]) if fm else \
                 ["### 失败模式", "", "（未定）", ""]
        lines += ["### 效果假设", "", str(st.effect_hypothesis), ""]
        lines += [f"### 适用条件（strategy_conditions 行数："
                  f"{cond_by_id.get(st.id, 0)}——0=无显式条件，语义审查须"
                  "一并裁定是否需补）", ""]
        if stats is not None:
            lines += ["### 证据概览（strategy_stats 投影）", "",
                      f"- root_works: **{stats.root_works}** | "
                      f"unique_source_intervals: {stats.unique_source_intervals}",
                      f"- attempts: {stats.attempts} | valid: {stats.valid} | "
                      f"rejected: {stats.rejected} | missing: {stats.missing}",
                      f"- by_root_work: {stats.extras}", ""]
        else:
            lines += ["### 证据概览", "", "（无 strategy_stats 行——投影未重建）",
                      ""]
        insts = (s.query(StrategyInstance)
                 .filter_by(strategy_id=st.id, status="verified")
                 .order_by(StrategyInstance.id).limit(samples).all())
        cache: dict[str, str] = {}
        if insts:
            lines += [f"### verified 实例样本（前 {len(insts)} 条，逐字引用）", ""]
            for i, r in enumerate(insts, 1):
                root = _root_of(s, r.work_id, cache)
                lines += [f"**样本{i}**（根作品 {root}，span "
                          f"{r.span_start}–{r.span_end}，sha "
                          f"{(r.evidence_sha256 or '')[:12]}…，extractor "
                          f"{r.extractor_model}）：",
                          "",
                          f"> {r.evidence_text}",
                          "",
                          f"观察记录：{(r.observed_content or '')[:200]}",
                          ""]
        else:
            lines += ["### verified 实例样本", "", "（无 verified 实例）", ""]
        lines += ["### 审查问句", ""] + [f"- {q}" for q in REVIEW_QUESTIONS] + [""]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="策略语义审查材料生成（只读）")
    ap.add_argument("--out", required=True, help="输出 markdown（已存在即拒）")
    ap.add_argument("--samples", type=int, default=3)
    a = ap.parse_args()
    if a.samples < 1:
        raise SystemExit("--samples 须 ≥1")
    out = Path(a.out)
    if out.exists():
        raise SystemExit(f"--out 已存在：{a.out}——审查材料不许覆盖，再生成换新文件名")
    db.init_db()
    with db.session() as s:
        text = build_dossier(s, samples=a.samples)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[dossier] 已写 {out}（{len(text)} 字符）")


if __name__ == "__main__":
    main()
