"""路线 (b)：AI 输出之间的相对排序 —— 学「哪些写法更不像 AI」（2026-09-19 集霸授权）。

Gold standard 双轨之一（交接 §8 修订版）：不跟人类比（human=chosen 已被裁定否掉），
只在 **AI 输出之间**排序。排序仪器 = nat-v1 上刚通过否掉检验的**自然度轴**
（四模型 0.879~0.915，`scripts/benchmark_falsify.py` pass）——用同一个 NAT 问题
问「哪边更自然」，多数决作为「更不像 AI」的弱标签。

数据池：非基准段上 ≥2 个白名单口径的自由重建候选（reconstruct_v1 / recon_ctx_v1）。
每段抽**一对跨模型**候选（同模型不同温度的差别信息量小）。位置种子随机化。

标签纪律：
· 每行带 `weak: true` + 三评委逐票 picks（§52：评委不是真理源，只当弱标签）；
· 三票分裂（1-1-1）的段直接丢弃，不许硬标；
· `_excluded_reason` 式隔离：role='benchmark' 段永不进（§14）。

用法：
    python scripts/ai_ranking_build.py --pairs 150 --run     # 真跑（默认 dry-run）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import config, db  # noqa: E402
from app.config import BLIND_REVIEW_PROMPT_VERSIONS  # noqa: E402
from app.gateway import chat  # noqa: E402
from app.models import Candidate, Segment, exclude_corpus_v2_segments  # noqa: E402
from app.ids import new_id  # noqa: E402
import preflight_models as pf  # noqa: E402  # 批量防呆①：开跑前校验模型名在网关池内

# 评委名单可被调度覆盖（LG_RANKING_JUDGES=逗号分隔）；通道挂掉时降级
# （如 2026-09-19 晚 deepseek 网关连败 → kimi+agnes 双评委，n_valid=2 需一致票）。
JUDGES = tuple((os.environ.get("LG_RANKING_JUDGES")
                or f"{config.DEFAULT_LLM_MODEL},moonshotai/kimi-k3,agnes-3.0-flash").split(","))
PV = "ai_ranking_v1"

NAT_SYS = "你是中文小说评审。只回答 A 或 B，不解释。"
NAT_Q = """下面是同一段中文小说的两个 AI 改写版本（A / B）。

【A】
{a}

【B】
{b}

哪一边读起来**更自然**（更像人自然写出的中文，不别扭、不堆砌）？
只输出一行 JSON：{{"pick": "A"|"B", "confidence": 0.0-1.0}}"""


def parse_pick(text: str) -> str | None:
    t = (text or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            d = json.loads(t[i:j + 1])
            p = str(d.get("pick", "")).strip().upper()[:1]
            if p in ("A", "B"):
                return p
        except Exception:
            pass
    for ch in t:
        if ch in "AB":
            return ch
    return None


def _pool(s) -> dict[str, list[Candidate]]:
    """非基准段 → 该段的白名单 ok 候选（≥2 个才入池）。

    corpus v2 镜像段一并排除：v2 与 v1 同文，双份入池等于同一对排序证据计两次
    （会审①复发路径，与 role='benchmark' 侧同闸）。
    """
    rows = (s.query(Candidate)
            .filter(Candidate.status == "ok")
            .filter(Candidate.prompt_version.in_(BLIND_REVIEW_PROMPT_VERSIONS))
            .filter(Candidate.text.isnot(None)).all())
    segs = {x.id: x for x in
            s.query(Segment).filter(Segment.role.is_(None) | (Segment.role == "train"),
                                    exclude_corpus_v2_segments()).all()}
    pool: dict[str, list[Candidate]] = {}
    for c in rows:
        seg = segs.get(c.segment_id)
        if seg is None or len(c.text or "") < 30:
            continue
        pool.setdefault(c.segment_id, []).append(c)
    return {k: v for k, v in pool.items() if len(v) >= 2}


def sample_pairs(s, n_pairs: int, seed: int) -> list[dict]:
    """每段最多 1 对，跨模型优先；位置种子随机化（可复现）。"""
    pool = _pool(s)
    rng = random.Random(seed)
    seg_ids = sorted(pool)
    rng.shuffle(seg_ids)
    pairs = []
    for sid in seg_ids:
        if len(pairs) >= n_pairs:
            break
        cands = pool[sid]
        def _distinct(i, j):
            return (cands[i].text or "") != (cands[j].text or "")
        cross = [(i, j) for i in range(len(cands)) for j in range(i + 1, len(cands))
                 if cands[i].model != cands[j].model and _distinct(i, j)]
        fallback = [(i, j) for i in range(len(cands)) for j in range(i + 1, len(cands))
                    if _distinct(i, j)]
        choice = rng.choice(cross if cross else fallback) if (cross or fallback) else None
        if choice is None:
            continue          # 该段所有候选文本相同（mock/重复产物）→ 无法构成排序对
        ci, cj = cands[choice[0]], cands[choice[1]]
        if rng.random() < 0.5:
            a, b, ai, bj = ci.text, cj.text, ci.model, cj.model
        else:
            a, b, ai, bj = cj.text, ci.text, cj.model, ci.model
        pairs.append({"segment_id": sid, "text_a": a, "text_b": b,
                      "model_a": ai, "model_b": bj})
    return pairs


def judge_pair(pair: dict, conc_model: str | None = None) -> dict | None:
    """三评委按自然度投票；多数决定 chosen（更自然=更不像 AI）。三票分裂 → None。"""
    votes = {}
    for m in JUDGES:
        try:
            r = chat(model=m, system=NAT_SYS,
                     user=NAT_Q.format(a=pair["text_a"], b=pair["text_b"]),
                     purpose="ai_ranking", prompt_version=PV,
                     temperature=0.0, max_tokens=200)
            votes[m] = parse_pick(r.text)
        except Exception:                       # noqa: BLE001
            votes[m] = None
    valid = [v for v in votes.values() if v in ("A", "B")]
    if len(valid) < 2:
        return None
    top, n = Counter(valid).most_common(1)[0]
    if n < 2:                                   # 1-1-1 分裂（或票型不明）
        return None
    chosen_is_a = (top == "A")
    return {"votes": votes, "n_valid": len(valid),
            "chosen": pair["text_a"] if chosen_is_a else pair["text_b"],
            "rejected": pair["text_b"] if chosen_is_a else pair["text_a"],
            "chosen_model": pair["model_a"] if chosen_is_a else pair["model_b"],
            "rejected_model": pair["model_b"] if chosen_is_a else pair["model_a"]}


def build(n_pairs: int, seed: int, ver: str, out_dir: Path | None = None,
          dry_run: bool = True) -> dict:
    dest = out_dir or (ROOT / "data" / "exports")
    dest.mkdir(parents=True, exist_ok=True)
    with db.session() as s:
        pairs = sample_pairs(s, n_pairs, seed)
    if dry_run:
        return {"would_judge_pairs": len(pairs),
                "segments_in_pool": len(_pool_dbg())}
    def one(k_pair):
        k, pair = k_pair
        res = judge_pair(pair)          # 组内 3 评委并行（judge_pair 内部线程）
        if res is None:
            return None
        row = {
            "id": new_id("AR"),
            "segment_id": pair["segment_id"],
            "chosen": res["chosen"], "rejected": res["rejected"],
            "chosen_model": res["chosen_model"], "rejected_model": res["rejected_model"],
            "label_source": "naturalness_ensemble_v1（nat-v1 轴，四模型 0.879~0.915 pass）",
            "weak": True,
            "votes": res["votes"], "n_valid_judges": res["n_valid"],
            "pair_seed": seed, "pv": PV,
        }
        print(f"[{k + 1}/{len(pairs)}] ok", flush=True)
        return row

    # 评委是网关模型（非串行通道）→ 对间并行 + 组内并行，落盘随跑随写（防崩丢账）
    path = dest / f"ai_ranking_{ver}.jsonl"
    sum_path = dest / f"ai_ranking_{ver}_summary.json"
    rows = []
    n_dropped = 0
    done_segs: set = set()
    if path.exists():                       # 断点续跑：已判段跳过，追加写
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r0 = json.loads(line)
                rows.append(r0)
                done_segs.add(r0["segment_id"])
    todo = [pr for pr in pairs if pr["segment_id"] not in done_segs]
    with path.open("a", encoding="utf-8") as f:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            for row in ex.map(one, list(enumerate(todo))):
                if row is None:
                    n_dropped += 1
                    continue
                rows.append(row)
                f.write(json.dumps(row, ensure_ascii=False) + chr(10))
                f.flush()
    summary = {"path": str(path), "summary_path": str(sum_path), "n": len(rows),
               "n_pairs_attempted": len(todo),
               "n_tie_dropped": n_dropped,
               "judges": list(JUDGES),
               "chosen_model_dist": dict(Counter(r["chosen_model"] for r in rows)),
               "by_model_pair": dict(Counter(
                   "/".join(sorted((r["chosen_model"], r["rejected_model"]))) for r in rows)),
               "pv": PV}
    sum_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def _pool_dbg() -> int:
    with db.session() as s:
        return len(_pool(s))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=150)
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--ver", default="v1")
    ap.add_argument("--run", action="store_true", help="真跑（默认 dry-run）")
    args = ap.parse_args()
    if args.run:
        # 批量防呆①（P0 死 id 事故）：评委整串不在池内的表现是 n_valid=0，
        # 看上去像"没有可判的 pair"，实际全在 503 —— 开跑前先问一遍网关。
        pf.require_models([m.strip() for m in JUDGES], source="ai_ranking_build")
    db.init_db()
    out = build(args.pairs, args.seed, args.ver, dry_run=not args.run)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
