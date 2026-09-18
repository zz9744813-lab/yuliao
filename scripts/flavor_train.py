"""用集霸的批注训「AI 味」判别器（2026-09-18，§⑳ 之后的唯一没试过的路）。

## 为什么不能直接"拿批注当标签、跑个分类器就完事"

集霸标的是**让他不适的地方**，不是随机抽样。所以有三个必须处理的混淆，
不处理的话训出来的很可能是个没用的东西：

1. **候选/人类混淆**：批注 63 条里绝大多数标在**候选侧**（AI 写的）。
   分类器很容易学会"这是候选文本"而不是"这有 AI 味"——
   而"候选文本"这个标签在训练集外毫无意义（将来的 writer 输出全是候选侧）。
   → 处理：**分侧评估**。只在候选侧内部训/测一次，再只在人类侧内部训/测一次。
   两侧都能分开，才说明学到的是"味"而不是"身份"。
2. **同题泄漏**：同一道题的两个版本内容相同（一个是另一个的重写），
   一边进训练、一边进测试 = 泄漏。→ 处理：**按题分组做折**（GroupKFold）。
3. **长度混淆**：他标的那侧可能系统性地更长/更短。
   → 处理：拿"只用长度"的基线对比，赢了才算数。

## 口径

- 正样本：该侧文本里**至少有一条他的批注**；负样本：该侧没有任何批注。
- 特征：本机 `fastembed` bge-small-zh 向量（512 维，无需 GPU、无需联网）。
- 模型：numpy 手写逻辑回归（L2），5 折**按题分组**，报 AUC + 分侧读数。
- 无 sklearn，不引入新依赖。

用法：
    python scripts/flavor_train.py --dry-run
    python scripts/flavor_train.py --train          # 训练 + 交叉验证 + 存模型
    python scripts/flavor_train.py --score "文本"    # 用已存模型打分
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

DB = ROOT / "data" / "language_genome.db"
MODEL_DIR = ROOT / "data" / "models"
PV = "flavor_lr_v1"
EMB_MODEL = "BAAI/bge-small-zh-v1.5"


# ── 数据 ───────────────────────────────────────────────────────

def load_samples() -> list[dict]:
    """每个 (题, 侧) 一条：(text, y=有无批注, side, item_group)。"""
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = con.execute("""select ri.id rid, ri.human_verdict hv, c.text ctext, s.text stext,
                                 s.text_clean sclean
                          from review_items ri join candidates c on c.id = ri.subject_id
                          join segments s on s.id = c.segment_id
                          where ri.status='done'""").fetchall()
    con.close()
    out = []
    for r in rows:
        hv = r["hv"]
        if isinstance(hv, str):
            try:
                hv = json.loads(hv)
            except Exception:
                hv = {}
        anns = (hv or {}).get("annotations") or []
        for side, text in (("candidate", r["ctext"]),
                           ("human", r["sclean"] or r["stext"])):
            if not text or len(text) < 20:
                continue
            kinds = [a.get("kind") for a in anns
                     if (a.get("target") or "").lower() == side]
            out.append({"rid": r["rid"], "side": side, "text": text,
                        "y": 1 if kinds else 0, "kinds": kinds})
    return out


def embed(texts: list[str]) -> np.ndarray:
    from fastembed import TextEmbedding
    m = TextEmbedding(EMB_MODEL)
    v = np.array(list(m.embed(texts)), dtype=np.float64)
    # L2 归一化：余弦相似度等价于点积，逻辑回归的尺度也更稳
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.maximum(n, 1e-9)


# ── 逻辑回归（手写，L2，梯度下降）───────────────────────────────

def fit_lr(X: np.ndarray, y: np.ndarray, *, l2: float = 1e-3, lr: float = 0.5,
           epochs: int = 3000, seed: int = 7) -> tuple[np.ndarray, float]:
    rng = np.random.default_rng(seed)
    w = rng.normal(0, 0.01, X.shape[1])
    b = 0.0
    n = len(y)
    for _ in range(epochs):
        z = X @ w + b
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        g = p - y
        gw = X.T @ g / n + l2 * w
        gb = g.mean()
        w -= lr * gw
        b -= lr * gb
    return w, b


def predict(X: np.ndarray, w: np.ndarray, b: float) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(X @ w + b, -30, 30)))


def auc(y: np.ndarray, s: np.ndarray) -> float:
    """AUC（Mann-Whitney），带并列处理。"""
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(len(order), dtype=float)
    ranks[order] = np.arange(1, len(order) + 1)
    # 并列取平均秩
    allv = np.concatenate([pos, neg])
    sv = allv[order]
    i = 0
    while i < len(sv):
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        if j > i:
            avg = (ranks[order[i:j + 1]]).mean()
            ranks[order[i:j + 1]] = avg
        i = j + 1
    r_pos = ranks[:len(pos)].sum()
    return (r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def group_folds(groups: list[str], k: int, seed: int) -> list[np.ndarray]:
    """按题分组切折：同一道题的两个版本必须整体进同一折（否则泄漏）。"""
    uniq = sorted(set(groups))
    rng = np.random.default_rng(seed)
    rng.shuffle(uniq)
    buckets = [[] for _ in range(k)]
    for i, u in enumerate(uniq):
        buckets[i % k].append(u)
    return [np.array([i for i, g in enumerate(groups) if g in set(b)]) for b in buckets]


# ── 主流程 ─────────────────────────────────────────────────────

def cv_report(X: np.ndarray, y: np.ndarray, groups: list[str], k: int = 5,
              seed: int = 7) -> dict:
    folds = group_folds(groups, k, seed)
    aucs, accs = [], []
    for f in folds:
        te = f
        tr = np.array([i for i in range(len(y)) if i not in set(te.tolist())])
        if len(set(y[tr])) < 2 or len(set(y[te])) < 2:
            continue
        w, b = fit_lr(X[tr], y[tr])
        s = predict(X[te], w, b)
        aucs.append(auc(y[te], s))
        accs.append(float(((s >= 0.5) == y[te]).mean()))
    return {"auc_mean": float(np.nanmean(aucs)), "auc_folds": [round(a, 3) for a in aucs],
            "acc_mean": float(np.mean(accs)) if accs else float("nan")}


def train(seed: int = 7) -> dict:
    samples = load_samples()
    n_pos = sum(s["y"] for s in samples)
    print(f"样本 {len(samples)} 条（正 {n_pos} / 负 {len(samples) - n_pos}）"
          f"，涉及题目 {len({s['rid'] for s in samples})} 道")
    for side in ("candidate", "human"):
        sub = [s for s in samples if s["side"] == side]
        print(f"   {side}: {len(sub)} 条（正 {sum(x['y'] for x in sub)}）")
    texts = [s["text"] for s in samples]
    X = embed(texts)
    y = np.array([s["y"] for s in samples])
    groups = [s["rid"] for s in samples]
    lens = np.array([len(t) for t in texts], dtype=float)

    out = {}
    out["all"] = cv_report(X, y, groups)
    print(f'\n全体按题分组 CV：AUC {out["all"]["auc_mean"]:.3f} '
          f'（各折 {out["all"]["auc_folds"]}）acc {out["all"]["acc_mean"]:.3f}')

    # **分侧评估**：能不能分出的不是"候选/人类"这个身份
    for side in ("candidate", "human"):
        idx = np.array([i for i, s in enumerate(samples) if s["side"] == side])
        if len(set(y[idx])) < 2 or len(idx) < 12:
            print(f"   {side}: 样本不足，跳过")
            continue
        r = cv_report(X[idx], y[idx], [groups[i] for i in idx])
        out[side] = r
        print(f'   仅 {side} 侧：AUC {r["auc_mean"]:.3f}（各折 {r["auc_folds"]}）'
              f' n={len(idx)} 正={int(y[idx].sum())}')

    # 长度基线：只用长度能不能分出（若能，说明前面的 AUC 可能只是长度）
    base = cv_report(lens.reshape(-1, 1), y, groups)
    out["length_baseline"] = base
    print(f'   长度基线 AUC {base["auc_mean"]:.3f}（这是"作弊线"，必须明显低于向量模型）')

    # 全量拟合存档
    w, b = fit_lr(X, y)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    np.savez(MODEL_DIR / f"{PV}.npz", w=w, b=b, emb_model=EMB_MODEL)
    (MODEL_DIR / f"{PV}.json").write_text(json.dumps(
        {"pv": PV, "emb_model": EMB_MODEL, "n": len(samples), "n_pos": int(n_pos),
         "cv": out, "note": "用集霸 63 条批注训的 AI 味判别器；分侧 AUC 才算数"},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f'\n模型已存 → {MODEL_DIR / (PV + ".npz")}')
    print(json.dumps({k: (v["auc_mean"] if isinstance(v, dict) else v)
                      for k, v in out.items()}, ensure_ascii=False))
    return out


def score_text(text: str) -> float:
    f = MODEL_DIR / f"{PV}.npz"
    if not f.exists():
        raise SystemExit("模型不存在，先跑 --train")
    d = np.load(f, allow_pickle=True)
    v = embed([text])
    return float(predict(v, d["w"], float(d["b"]))[0])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--score", default="")
    args = ap.parse_args()
    if args.score:
        print(f"{score_text(args.score):.3f}  {args.score[:60]}")
        return
    if args.dry_run:
        s = load_samples()
        print(f"正 {sum(x['y'] for x in s)} / 负 {len(s) - sum(x['y'] for x in s)}；"
              f"题目 {len({x['rid'] for x in s})} 道")
        return
    train()


if __name__ == "__main__":
    main()
