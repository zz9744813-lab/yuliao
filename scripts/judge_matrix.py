"""Human Prediction Matrix（Phase 1.5 §十）：集霸盲评 vs 各 LLM Judge 的一致率。

前置：用户已在前端判完某批。
用法：
    python scripts/judge_matrix.py EXP-0911-B82D            # 该实验全部已判条目
    python scripts/judge_matrix.py EXP-0911-B82D r15        # 只看 batch_r15
输出：每 Judge 的 pairwise agreement / abstain 数（对 human 胜负的置信校准）。

口径警告（沿用 Phase 1.5 记录）：adversarial judge 判的是"哪边是 AI"，
用户判的是"哪边更好"。候选行文更光滑时评委答对 AI 却与用户偏好相反，
0.5 附近有一部分是口径错位而非评委无能。v1 计划改为让 Judge 做与用户完全相同的
A/B preference 任务再算 agreement——那之前，本表的绝对值只能当方向看。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.models import Candidate, JudgeRun, ReviewItem, Segment


def _metrics(pairs: list[tuple[bool, bool]]) -> dict:
    """pairs = [(user 判 human 胜, judge 判 human 胜), ...]，双方都已表态。

    agreement 单独看会骗人：当评委有强响应偏差（几乎总挑 candidate）时，
    agreement 会被用户的基础率主导，0.5 附近不代表"接近随机"而可能代表"零信息"。
    因此同时给 κ（chance-corrected）与响应偏差指标。
    """
    n = len(pairs)
    if not n:
        return {}
    a = sum(1 for u, j in pairs if u and j)
    b = sum(1 for u, j in pairs if u and not j)
    c = sum(1 for u, j in pairs if not u and j)
    d = sum(1 for u, j in pairs if not u and not j)
    po = (a + d) / n
    p_judge_h = (a + c) / n
    p_user_h = (a + b) / n
    pe = p_judge_h * p_user_h + (1 - p_judge_h) * (1 - p_user_h)
    kappa = (po - pe) / (1 - pe) if pe < 1 else None
    return {
        "agreement": round(po, 3),
        "kappa": round(kappa, 3) if kappa is not None else None,
        "judge_picks_candidate": round(1 - p_judge_h, 3),
        "user_picks_human": round(p_user_h, 3),
        "n": n,
    }


def _adversarial_rows(s, exp_id: str, user_call: dict, judges) -> list[dict]:
    """口径 A：adversarial judge 判"哪边是 AI"，映射为"评委站哪边"。"""
    rows = []
    for judge in judges:
        pairs, abstain = [], 0
        for cid, human_won in user_call.items():
            j = (s.query(JudgeRun)
                 .filter_by(experiment_id=exp_id, subject_type="candidate",
                            subject_id=cid, judge_kind="adversarial", model=judge)
                 .order_by(JudgeRun.created_at.desc()).first())
            if not j or not j.verdict:
                continue
            if j.abstain:
                abstain += 1
                continue
            hit_ai = j.verdict.get("guess_hit_ai")   # 评委是否认出了 AI
            if hit_ai is None:
                continue
            # 语义：认出 AI ⇒ 认为另一边（human）更像人 ⇒ 评委站 human。
            # 2026-09-14 修正：原实现写的是 `(guess_hit_ai is False)`，方向反了。
            # 反证：一个 100% 认出 AI 的完美评委，在用户 93% 判 human 胜时
            # 原实现只给 7% agreement，显然错误；本口径给 93%。
            pairs.append((bool(human_won), bool(hit_ai)))
        if pairs:
            rows.append({"judge": judge, "abstain": abstain, **_metrics(pairs)})
    return rows


def preference_rows(s, exp_id: str, user_call: dict, judges) -> list[dict]:
    """口径 B：preference judge 做与用户**完全相同**的 A/B 偏好任务，直接对齐胜负。

    只有双方都给出二选一（human / candidate）的样本进分母；
    评委 equal / both_bad / cant_judge / 无记录 → 计入 non_decisive / missing，不进分母。
    """
    rows = []
    for judge in judges:
        pairs, non_decisive, missing = [], 0, 0
        for cid, human_won in user_call.items():
            j = (s.query(JudgeRun)
                 .filter_by(experiment_id=exp_id, subject_type="candidate",
                            subject_id=cid, judge_kind="preference", model=judge)
                 .order_by(JudgeRun.created_at.desc()).first())
            if not j or not j.verdict:
                missing += 1
                continue
            resolved = j.verdict.get("winner_resolved")
            if resolved not in ("human", "candidate"):
                non_decisive += 1
                continue
            pairs.append((bool(human_won), resolved == "human"))
        if pairs or non_decisive or missing:
            rows.append({"judge": judge, "non_decisive": non_decisive, "missing": missing,
                         **_metrics(pairs)})
    return rows


def main(exp_id: str, batch: str | None = None) -> list[dict]:
    """打印两套口径的 agreement；返回 adversarial 行（保持既有调用方/测试兼容）。"""
    tag = f"batch_{batch}" if batch else None
    with db.session() as s:
        done = s.query(ReviewItem).filter_by(experiment_id=exp_id, status="done").all()
        if tag:
            done = [r for r in done if tag in (r.reasons or [])]
        if not done:
            print(f"没有已判条目（batch={tag or '全部'}）——前端盲评后重跑本脚本")
            return []
        # 用户立场：每题 human 是否胜出（从 human_verdict 解码）
        user_call = {}   # candidate_id -> True（user 判 human 胜）/ False（判 candidate 胜）
        n_both_bad = n_cant = 0
        for r in done:
            v = r.human_verdict or {}
            if v.get("winner_resolved") == "human":
                user_call[r.subject_id] = True
            elif v.get("winner_resolved") == "candidate":
                user_call[r.subject_id] = False
            elif v.get("winner_resolved") == "both_bad":
                n_both_bad += 1
            elif v.get("winner_resolved") == "cant_judge":
                n_cant += 1
        scope = tag or "全部已判"
        print(f"[{exp_id} / {scope}] 已判 {len(done)} 条："
              f"可计入胜负 {len(user_call)}（human {sum(user_call.values())} / "
              f"candidate {len(user_call) - sum(user_call.values())}），"
              f"both_bad {n_both_bad}、cant_judge {n_cant}（不计入 agreement 分母）")
        if not user_call:
            return []

        judges = ("moonshotai/kimi-k3", "deepseek/deepseek-v4.1-flash")
        rows = _adversarial_rows(s, exp_id, user_call, judges)
        print("\n── 口径 A：adversarial（评委猜「哪边是 AI」→ 映射为站边）──")
        print(json.dumps(rows, ensure_ascii=False, indent=1))

        pref = preference_rows(s, exp_id, user_call, judges)
        print("\n── 口径 B：preference（评委做与用户完全相同的 A/B 偏好任务）──")
        if pref:
            print(json.dumps(pref, ensure_ascii=False, indent=1))
        else:
            print("无 preference 记录——先跑 scripts/pref_judge.py 生成")
        print("\n说明：口径 B 才是 Gate 该用的数（同任务才可比）。"
              "口径 A 仅作历史对照：它衡量「识别 AI 的能力」，与「偏好判断」是两种能力。")
        print("⚠ 看 agreement 会骗人：评委若几乎总挑 candidate（judge_picks_candidate 高），"
              "agreement 会被用户基础率主导，0.5 附近可能其实是「零信息」。"
              "判 Gate 请以 kappa（chance-corrected）为准，≥0.70 agreement 的门应换算成 κ 再谈。")
        print("窗口评委（会话内，不调 API）的批次结果见 data/blind/*_result.json（人工并表）。")
        return rows


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "EXP-0911-B82D",
         sys.argv[2] if len(sys.argv) > 2 else None)
