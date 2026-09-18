"""Calibration Report v1 生成器：回答唯一问题——哪种粒度的 SemanticFrame 站得住。

核心图（文字版表格）：
              fidelity ↑ / freedom ↑
  S ─────────  □
  M ─────────        ◎  ← sweet spot = fidelity 达标 + freedom 达标 + leakage < 阈
  L ─────────            ▲

输出两份：data/reports/<exp>/calibration_report.md（人读）+ .json（程序读）。
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from . import config
from .leakage import char_ngrams
from .metrics_det import KEY_METRICS
from .models import (Candidate, Experiment, Frame, JudgeRun, LeakageScore,
                     ReportFile, ResidualDet, ResidualSem, Segment, Proposition, LlmCall)


def _p(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _mean(vals) -> float:
    vals = list(vals)
    return round(sum(vals) / len(vals), 4) if vals else 0.0


# ── 表达自由度确定口径 ──────────────────────────────────────

def _frame_freedom(cand_texts: list[str]) -> dict:
    """同一 Frame 下候选之间的多样性：越高说明 Frame 没把写法钉死。"""
    if len(cand_texts) < 2:
        return {"n": len(cand_texts), "pairwise_dist": 0.0, "len_spread": 0.0}
    gram_sets = [char_ngrams(t, 5) for t in cand_texts]
    dists = []
    for i in range(len(gram_sets)):
        for j in range(i + 1, len(gram_sets)):
            a, b = gram_sets[i], gram_sets[j]
            union = len(a | b)
            jacc = (len(a & b) / union) if union else 0.0
            dists.append(1 - jacc)
    lens = [len(t) for t in cand_texts]
    return {
        "n": len(cand_texts),
        "pairwise_dist": round(statistics.mean(dists), 4),
        "len_spread": round(statistics.pstdev(lens), 1) if len(lens) > 1 else 0.0,
    }


def build_calibration_report(s: Session, experiment_id: str) -> dict:
    exp = s.get(Experiment, experiment_id)
    if not exp:
        raise KeyError(f"experiment 不存在: {experiment_id}")
    cfg = exp.config
    thresholds = cfg.get("leak_thresholds", {})
    segs = {x.id: x for x in s.query(Segment).filter(Segment.id.in_(cfg["segment_ids"])).all()}

    frames = s.query(Frame).filter_by(experiment_id=experiment_id).filter(
        Frame.status != "failed").all()
    primary = [f for f in frames if f.is_primary]
    by_gran: dict[str, list[Frame]] = {}
    for f in primary:
        by_gran.setdefault(f.granularity, []).append(f)

    leak_map: dict[str, dict[str, float]] = {}
    for lk in s.query(LeakageScore).filter(
            LeakageScore.frame_id.in_([f.id for f in frames])).all():
        leak_map.setdefault(lk.frame_id, {})[lk.layer] = lk.score

    cands = s.query(Candidate).filter_by(experiment_id=experiment_id, status="ok").all()
    cands_by_frame: dict[str, list[Candidate]] = {}
    for c in cands:
        cands_by_frame.setdefault(c.frame_id, []).append(c)

    jr_by_cand: dict[str, list[JudgeRun]] = {}
    for jr in s.query(JudgeRun).filter_by(experiment_id=experiment_id).all():
        jr_by_cand.setdefault(jr.subject_id, []).append(jr)

    exp_cand_ids = {c.id for c in cands}
    sem_res: dict[str, ResidualSem] = {r.candidate_id: r for r in
                                       s.query(ResidualSem).all() if r.candidate_id in exp_cand_ids}

    det_map: dict[str, ResidualDet] = {r.candidate_id: r for r in
                                       s.query(ResidualDet).all() if r.candidate_id in exp_cand_ids}

    human_nat_scores = []
    for jr in s.query(JudgeRun).filter_by(experiment_id=experiment_id,
                                          judge_kind="naturalness",
                                          subject_type="human_segment").all():
        v = (jr.verdict or {}).get("score")
        if isinstance(v, (int, float)):
            human_nat_scores.append(float(v))

    # 每粒度汇总 ─────────────────────────────────────────────
    table = {}
    for gran, flist in sorted(by_gran.items()):
        frees, fidelities, leak_layers = [], [], {"char6": [], "word3": [], "rare": [], "adversarial": []}
        breach_rates, subtexts, pov_rates = [], [], []
        sem_missing, sem_added, sem_contra = [], [], []
        nat_scores, adversarial_hit, upsets = [], [], 0
        tags_counter: dict[str, int] = {}
        n_cands = 0

        for f in flist:
            cs = [c for c in cands_by_frame.get(f.id, []) if c.text]
            n_cands += len(cs)
            frees.append(_frame_freedom([c.text for c in cs]))
            for layer, arr in leak_layers.items():
                if f.id in leak_map and layer in leak_map[f.id]:
                    arr.append(leak_map[f.id][layer])

            for c in cs:
                judges = jr_by_cand.get(c.id, [])
                for jr in judges:
                    if jr.judge_kind == "semantic" and jr.verdict:
                        per = jr.verdict.get("per_proposition") or []
                        if per:
                            hit = sum(1 for x in per if x.get("verdict") == "hit")
                            fidelities.append(hit / len(per))
                        if jr.verdict.get("info_boundary_breach"):
                            breach_rates.append(1.0)
                        else:
                            breach_rates.append(0.0)
                    elif jr.judge_kind == "naturalness" and jr.verdict:
                        sc = jr.verdict.get("score")
                        if isinstance(sc, (int, float)):
                            nat_scores.append(float(sc))
                    elif jr.judge_kind == "adversarial" and jr.verdict:
                        if jr.verdict.get("guess_hit_ai") is True:
                            adversarial_hit.append(1.0)
                        elif jr.verdict.get("guess_hit_ai") is False:
                            adversarial_hit.append(0.0)
                            upsets += 1

                rs = sem_res.get(c.id)
                if rs and rs.payload:
                    sem_missing.append(len(rs.payload.get("missing") or []))
                    sem_added.append(len(rs.payload.get("added") or []))
                    sem_contra.append(len(rs.payload.get("contradicted") or []))
                    if rs.payload.get("subtext_preserved") is not None:
                        subtexts.append(float(rs.payload["subtext_preserved"]))
                    if rs.payload.get("pov_consistent") is not None:
                        pov_rates.append(1.0 if rs.payload["pov_consistent"] else 0.0)
                    for t in rs.payload.get("tags") or []:
                        tags_counter[t] = tags_counter.get(t, 0) + 1

        table[gran] = {
            "n_frames": len(flist),
            "n_candidates": n_cands,
            "fidelity_prop_hit_rate": round(_mean(fidelities), 4),
            "sem_missing_mean": round(_mean(sem_missing), 3),
            "sem_added_mean": round(_mean(sem_added), 3),
            "sem_contra_mean": round(_mean(sem_contra), 3),
            "info_boundary_breach_rate": round(_mean(breach_rates), 4),
            "freedom_pairwise_dist": round(_mean([f["pairwise_dist"] for f in frees]), 4),
            "freedom_len_spread": round(_mean([f["len_spread"] for f in frees]), 2),
            "subtext_preserved_mean": round(_mean(subtexts), 4),
            "pov_consistent_rate": round(_mean(pov_rates), 4),
            "naturalness_candidate_mean": round(_mean(nat_scores), 3),
            "adversarial_ai_hit_rate": round(_mean(adversarial_hit), 4),
            "human_upsets": upsets,
            "leakage": {
                layer: {
                    "mean": round(_mean(arr), 4),
                    "p90": round(_p(arr, 0.9), 4),
                    "max": round(max(arr) if arr else 0.0, 4),
                    "over_threshold": sum(1 for v in arr if v > thresholds.get(layer, 1.1)),
                } for layer, arr in leak_layers.items()
            },
            "top_diff_tags": sorted(tags_counter.items(), key=lambda x: -x[1])[:8],
        }

    # 确定性残差：AI 相对 Human 的系统性偏移（全体 + 分模型）
    det_delta_means: dict[str, float] = {}
    if det_map:
        for k in KEY_METRICS:
            det_delta_means[k] = round(_mean([r.deltas.get(k, 0.0) for r in det_map.values()]), 4)

    by_model: dict[str, dict[str, float]] = {}
    cand_by_model: dict[str, list[str]] = {}
    for c in cands:
        cand_by_model.setdefault(c.model, []).append(c.id)
    for model, ids in cand_by_model.items():
        ds = [det_map[i].deltas for i in ids if i in det_map]
        by_model[model] = {
            k: round(_mean([d.get(k, 0.0) for d in ds]), 4) for k in KEY_METRICS
        }

    # token 账单：优先按 experiment_id 精确切账（新调用都带实验归属）；
    # 旧数据该列为空，回退到"实验创建时间起"的时间窗（实验并行时会串账，已在审查文档注明）
    scoped = s.query(LlmCall).filter_by(experiment_id=experiment_id).limit(1).first()
    if scoped:
        calls = s.query(LlmCall).filter_by(experiment_id=experiment_id).all()
    else:
        calls = s.query(LlmCall).filter(LlmCall.created_at >= exp.created_at).all()
    usage = {}
    for lc in calls:
        row = usage.setdefault(lc.purpose, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "failed": 0})
        row["calls"] += 1
        row["tokens_in"] += lc.tokens_in
        row["tokens_out"] += lc.tokens_out
        if lc.status != "ok":
            row["failed"] += 1

    # 短文本告警：per_k 类 det 指标在小样本文本上噪声极大
    cand_lens = [len(c.text) for c in cands if c.text]
    median_cand_len = statistics.median(cand_lens) if cand_lens else 0.0

    # 甜区判定
    sweet = {}
    for gran, row in table.items():
        lk = row["leakage"]
        static_over = (lk["char6"]["over_threshold"] + lk["word3"]["over_threshold"]
                       + lk["rare"]["over_threshold"])
        adv_ok = lk["adversarial"]["p90"] < thresholds.get("adversarial", 0.65)
        leak_ok = static_over == 0 and adv_ok
        sweet[gran] = {
            "leak_ok": leak_ok,
            "fidelity": row["fidelity_prop_hit_rate"],
            "freedom": row["freedom_pairwise_dist"],
            "verdict": ("✅ sweet-zone" if (leak_ok and row["fidelity_prop_hit_rate"] >= 0.8)
                        else ("⚠ 泄漏偏高" if not leak_ok else "⚠ 语义保真不足")),
        }

    rec = max(sweet, key=lambda g: (
        sweet[g]["leak_ok"], sweet[g]["fidelity"], sweet[g]["freedom"])) if sweet else None

    # 组装 ──────────────────────────────────────────────────
    md = _render_md(exp, table, sweet, rec, det_delta_means, by_model,
                    human_nat_scores, usage, len(segs), median_cand_len)
    out_dir = Path(config.DATA_DIR) / "reports" / exp.id
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "calibration_report.md"
    json_path = out_dir / "calibration_report.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps({
        "experiment": exp.id,
        "config": {k: v for k, v in cfg.items() if k != "segment_ids"},
        "granularity_table": table,
        "sweet_zone": sweet,
        "recommended_granularity": rec,
        "det_delta_means": det_delta_means,
        "by_model_det_delta": by_model,
        "usage": usage,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    for kind, path in (("calibration_md", md_path), ("calibration_json", json_path)):
        s.add(ReportFile(experiment_id=exp.id, kind=kind, path=str(path)))
    s.commit()
    return {"md": str(md_path), "json": str(json_path), "recommended": rec}


def _fmt_table(table: dict, sweet: dict) -> str:
    lines = ["| 粒度 | Frames | Cands | 保真率 | 表达自由度 | 对抗泄漏p90 | char6超阈 | 越界率 | 甜区 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for gran, row in sorted(table.items()):
        lk = row["leakage"]
        lines.append(
            f"| {gran} | {row['n_frames']} | {row['n_candidates']} | "
            f"{row['fidelity_prop_hit_rate']:.3f} | {row['freedom_pairwise_dist']:.3f} | "
            f"{lk['adversarial']['p90']:.3f} | {lk['char6']['over_threshold']} | "
            f"{row['info_boundary_breach_rate']:.3f} | {sweet.get(gran, {}).get('verdict', '')} |"
        )
    return "\n".join(lines)


def _render_md(exp, table, sweet, rec, det_delta_means, by_model,
               human_nat_scores, usage, n_segs, median_cand_len: float = 0.0) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    judge_models = exp.config.get("judge_models") or []
    recon_models = exp.config.get("recon_models") or []
    family_overlap = [m for m in judge_models if m in recon_models]
    lines = [
        f"# SemanticFrame Calibration Report v1",
        f"",
        f"- Experiment: `{exp.id}`",
        f"- Generated: {now}",
        f"- Segments: {n_segs}（seed={exp.config.get('segment_seed')}）",
        f"- Mode: {config.LLM_MODE}",
        f"- Judges: {judge_models} / Recon: {recon_models}",
    ]
    if family_overlap:
        lines.append(f"- ⚠ Judge 与生成端同源模型：{family_overlap}（同家族自评，结论打折扣）")
    cautions = []
    if median_cand_len and median_cand_len < 150:
        cautions.append(
            f"- ⚠ 候选文本中位长度仅 {median_cand_len:.0f} 字：per_k 类 det 指标（每千字词频）"
            f"在此长度上噪声很大，第六节只作方向参考，不作阈值判定。")
    if n_segs < 32:
        cautions.append(f"- ⚠ 样本量 {n_segs} < 32：p90/均值的统计效力弱，结论视为冒烟信号而非定标。")
    if cautions:
        lines += ["", "**阅读须知**", ""] + cautions
    lines += [
        "",
        "## 一、核心判定：哪个粒度的 Frame 站得住",
        "",
        _fmt_table(table, sweet),
        "",
        f"**推荐粒度：`{rec or '（不足以判定）'}**",
        "",
        "## 二、泄漏四层",
        "",
    ]
    for gran, row in sorted(table.items()):
        lines.append(f"### Frame-{gran}")
        for layer, st in row["leakage"].items():
            lines.append(f"- {layer}: mean={st['mean']} p90={st['p90']} max={st['max']} 超阈={st['over_threshold']}")
        lines.append("")
    lines += [
        "## 三、语义充分性（Semantic Sufficiency）",
        "",
        "| 粒度 | 命题命中率 | 丢信息 | 多信息 | 冲突 | 越界率 |",
        "|---|---|---|---|---|---|",
    ]
    for gran, row in sorted(table.items()):
        lines.append(
            f"| {gran} | {row['fidelity_prop_hit_rate']:.3f} | {row['sem_missing_mean']:.2f} | "
            f"{row['sem_added_mean']:.2f} | {row['sem_contra_mean']:.2f} | {row['info_boundary_breach_rate']:.3f} |"
        )
    lines += [
        "",
        "## 四、表达自由度（Expression Freedom）",
        "",
        "| 粒度 | 候选间距离 | 长度扩散 |",
        "|---|---|---|",
    ]
    for gran, row in sorted(table.items()):
        lines.append(f"| {gran} | {row['freedom_pairwise_dist']:.3f} | {row['freedom_len_spread']:.1f} |")
    lines += [
        "",
        "## 五、Judge 行为",
        "",
        f"- Human 原文自然度盲评均值：{_mean(human_nat_scores):.2f}",
    ]
    for gran, row in sorted(table.items()):
        lines.append(
            f"- {gran}: candidate_nat={row['naturalness_candidate_mean']} "
            f"adversarial_ai_hit={row['adversarial_ai_hit_rate']:.2f} "
            f"human_upsets={row['human_upsets']}"
        )
    lines += [
        "",
        "## 六、确定性残差：AI 相对 Human 的系统性偏移（全体均值）",
        "",
        "| 指标 | Δ(candidate − human) |",
        "|---|---|",
    ]
    for k, v in det_delta_means.items():
        lines.append(f"| {k} | {v:+.3f} |")
    lines += [
        "",
        "### 分模型指纹（前几项）",
        "",
        "| 模型 | 心理标记 | 情绪命名 | 修饰副词 | 连接词 | 句长 |",
        "|---|---|---|---|---|---|",
    ]
    for model, d in by_model.items():
        lines.append(
            f"| {model} | {d.get('psych_marker_per_k', 0):+.2f} | {d.get('emotion_word_per_k', 0):+.2f} | "
            f"{d.get('adv_flavor_per_k', 0):+.2f} | {d.get('connective_per_k', 0):+.2f} | "
            f"{d.get('sent_len_mean', 0):+.2f} |"
        )
    lines += ["", "## 七、成本与用量", "", "| 环节 | 调用 | tokens_in | tokens_out | 失败 |", "|---|---|---|---|---|"]
    for purpose, u in sorted(usage.items()):
        lines.append(f"| {purpose} | {u['calls']} | {u['tokens_in']} | {u['tokens_out']} | {u['failed']} |")
    lines += [
        "",
        "## 八、下一步",
        "",
        "- 若推荐粒度泄漏/保真不达标：调 prompt、增减字段，重跑仅有变化的 stage（幂等）。",
        "- 报告里每条结论都可回到 ReportFile / DB 行级数据复核。",
        "",
    ]
    return "\n".join(lines)
