"""片段级「AI 味」检测器（2026-09-18 建成，AUC≈0.79）。

## 为什么是片段级，不是段落级

同一批标注，两种切法的结果完全相反（都是按题分组 5 折 CV）：

| 粒度 | AUC | 说明 |
|---|---|---|
| 段落级 | **0.443（低于随机）** | 他标的是一处用词，整段向量把它淹没了 |
| 片段级（他标的片段 vs 同段等长随机片段） | **0.794** | 长度已严格配平；去掉长度成分后 0.789 |

反证检查（先找能推翻它的证据）：
- 长度基线 **0.500**（无长度混淆）
- 表面特征基线（标点/的-地/汉字比）0.664（有信号但明显低于向量）

## 它到底是什么（说清楚边界，别过度声称）

他的批注是**他自己挑出来标的**，所以这个判别器学到的是
**"这段文字里哪一处最像会被他圈出来"**——是**显著性/别扭度**检测，
不能直接等同于"整段是 AI 写的"。它的正确用法是：
- 圈出可疑片段给人看（他本来就靠圈词工作）；
- 作为 writer 输出的**局部体检**，而不是整段打分。

## 用法

    python scripts/flavor_span.py --train            # 训练 + 交叉验证 + 存模型
    python scripts/flavor_span.py --eval-pairs       # 外部验证：构造劣化对
    python scripts/flavor_span.py --text "要检查的一段话"
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

from flavor_train import DB, EMB_MODEL, auc, embed, fit_lr, group_folds, predict  # noqa: E402

PV = "flavor_span_v1"
MODEL_DIR = ROOT / "data" / "models"
WIN_MIN, WIN_MAX, WIN_STEP = 6, 16, 3


def build_dataset(seed: int = 7, neg_per_pos: int = 2) -> tuple[list[str], np.ndarray, list[str]]:
    """正例 = 他标注的片段；负例 = 同一段里**严格等长**的随机片段。"""
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = con.execute("""select ri.id rid, ri.human_verdict hv, c.text ctext,
                                 s.text stext, s.text_clean sc
                          from review_items ri join candidates c on c.id = ri.subject_id
                          join segments s on s.id = c.segment_id
                          where ri.status='done'""").fetchall()
    con.close()
    rng = random.Random(seed)
    pos, neg, groups = [], [], []
    for r in rows:
        hv = r["hv"]
        if isinstance(hv, str):
            try:
                hv = json.loads(hv)
            except Exception:
                hv = {}
        for a in ((hv or {}).get("annotations") or []):
            side = (a.get("target") or "").lower()
            src = r["ctext"] if side == "candidate" else (r["sc"] or r["stext"])
            if not src:
                continue
            s0, s1 = int(a.get("start") or 0), int(a.get("end") or 0)
            span = (src[s0:s1] or "").strip()
            if not (2 <= len(span) <= 30):
                continue
            pos.append(span)
            groups.append(r["rid"])
            L, made = len(span), 0
            while made < neg_per_pos and len(src) > L:
                k = rng.randrange(0, len(src) - L)
                if not (k + L < s0 or k > s1):
                    continue
                piece = src[k:k + L]
                # 不取到标注本身的近似重复
                if piece.strip() == span:
                    continue
                neg.append(piece)
                groups.append(r["rid"])
                made += 1
    texts = pos + neg
    y = np.array([1] * len(pos) + [0] * len(neg))
    return texts, y, groups


def train(save: bool = True) -> dict:
    texts, y, groups = build_dataset()
    print(f"片段样本 {len(texts)}（正 {int(y.sum())} / 负 {len(y) - int(y.sum())}）")
    X = embed(texts)
    folds = group_folds(groups, 5, 7)
    aucs, accs = [], []
    for te in folds:
        tr = np.array([i for i in range(len(y)) if i not in set(te.tolist())])
        if len(set(y[tr])) < 2 or len(set(y[te])) < 2:
            continue
        w, b = fit_lr(X[tr], y[tr])
        s = predict(X[te], w, b)
        aucs.append(auc(y[te], s))
        accs.append(float(((s >= 0.5) == y[te]).mean()))
    # 基线（长度）——同一折法
    Lb = np.array([[len(t)] for t in texts], dtype=float)
    base = []
    for te in folds:
        tr = np.array([i for i in range(len(y)) if i not in set(te.tolist())])
        if len(set(y[tr])) < 2 or len(set(y[te])) < 2:
            continue
        w, b = fit_lr(Lb[tr], y[tr])
        base.append(auc(y[te], predict(Lb[te], w, b)))
    out = {"n": len(texts), "n_pos": int(y.sum()),
           "auc_mean": float(np.nanmean(aucs)), "auc_folds": [round(a, 3) for a in aucs],
           "acc_mean": float(np.mean(accs)), "length_baseline_auc": float(np.nanmean(base))}
    print(f'按题分组 CV：AUC {out["auc_mean"]:.3f} 各折 {out["auc_folds"]}；'
          f'acc {out["acc_mean"]:.3f}；长度基线 {out["length_baseline_auc"]:.3f}')
    if save:
        w, b = fit_lr(X, y)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        np.savez(MODEL_DIR / f"{PV}.npz", w=w, b=b, emb_model=EMB_MODEL)
        (MODEL_DIR / f"{PV}.json").write_text(json.dumps(
            {"pv": PV, "emb_model": EMB_MODEL, "cv": out,
             "note": "片段级 AI 味/别扭度检测；用法是圈可疑片段，不是给整段打分"},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"模型已存 → {MODEL_DIR / (PV + '.npz')}")
    return out


_MODEL: dict = {}


def _load():
    if not _MODEL:
        f = MODEL_DIR / f"{PV}.npz"
        if not f.exists():
            raise SystemExit("模型不存在，先跑 --train")
        d = np.load(f, allow_pickle=True)
        _MODEL.update(w=d["w"], b=float(d["b"]))
    return _MODEL


def windows(text: str) -> list[tuple[int, int, str]]:
    """滑窗切片（固定长度、避开标点边界太碎的片段）。"""
    out = []
    t = text or ""
    for L in range(WIN_MIN, WIN_MAX + 1, 3):
        for i in range(0, max(1, len(t) - L + 1), WIN_STEP):
            piece = t[i:i + L]
            if "，" in piece.strip("，") or "。" in piece:
                continue
            if len(re.findall(r"[\u4e00-\u9fff]", piece)) < L * 0.7:
                continue
            out.append((i, i + L, piece))
    return out


def score_spans(text: str, top_k: int = 3) -> dict:
    """返回该文本里**最像他会圈出来**的 top-k 片段与分数。"""
    m = _load()
    ws = windows(text)
    if not ws:
        return {"max": 0.0, "mean_top": 0.0, "spans": []}
    X = embed([w[2] for w in ws])
    s = predict(X, m["w"], m["b"])
    order = np.argsort(-s)
    picked, used = [], []
    for idx in order:                      # 去重叠，取前 top_k
        a, b = ws[idx][0], ws[idx][1]
        if any(not (b <= x or a >= y) for x, y in used):
            continue
        used.append((a, b))
        picked.append({"start": a, "end": b, "text": ws[idx][2],
                       "score": round(float(s[idx]), 3)})
        if len(picked) >= top_k:
            break
    return {"max": round(float(s.max()), 3),
            "mean_top": round(float(np.mean([p["score"] for p in picked])), 3),
            "spans": picked}


def eval_pairs() -> dict:
    """外部验证：构造的劣化对上，检测器是否给"改坏版"更高分。

    这一测**独立于他的批注**（标签来自构造方式），是"它到底测没测到 AI 味"的关键证据。
    """
    import ai_flavor_eval as AF
    pairs = AF._usable_pairs()
    print(f"构造劣化对 {len(pairs)} 对（源完好、非基准）")
    hi = lo = tie = 0
    for h, v, ct in pairs:
        sh = score_spans(h, top_k=3)["mean_top"]
        sv = score_spans(v, top_k=3)["mean_top"]
        if sv > sh:
            hi += 1
        elif sv < sh:
            lo += 1
        else:
            tie += 1
    n = len(pairs)
    print(f"  劣化版分数更高 {hi}/{n} = {hi / n:.1%}；原文更高 {lo}；持平 {tie}")
    return {"n": n, "higher": hi, "lower": lo, "tie": tie}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--eval-pairs", action="store_true")
    ap.add_argument("--text", default="")
    args = ap.parse_args()
    if args.train:
        train()
    if args.eval_pairs:
        eval_pairs()
    if args.text:
        r = score_spans(args.text)
        print(json.dumps(r, ensure_ascii=False, indent=1))
    if not (args.train or args.eval_pairs or args.text):
        train()
        print()
        eval_pairs()


if __name__ == "__main__":
    main()
