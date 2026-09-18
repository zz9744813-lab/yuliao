"""表达策略发现 —— 总方案 §9 工作流 D（2026-09-17 补建）。

## 为什么现在做这个

总方案 §52 要回答的第一批问题就是：「**AI 为什么喜欢解释心理？为什么喜欢把隐含信息
说完整？句子为什么经常过于工整？**」；§53 的成功标准里明写 **Implicitness ↑**。
而项目此前建的全是**工作流 A（重建）+ 偏好判定**——工作流 D（表达策略发现）与
E（硬例挖掘）在库里连表都不存在（没有 `expression_strategies` / `hard_cases`）。

本脚本把它建起来，且**不需要集霸再判任何题**：用已有的 230 条判定。

## 流程（照方案 §9）

```
Residual Embedding → Clustering → Cluster Summarization → Strategy Candidate → 入库
```

- **单位**：一条已判的 (人类段, 候选段) 对
- **残差**：`normalize(embed(候选)) − normalize(embed(人类))`（本地 bge-small-zh-v1.5，零 API）
- **聚类**：k-means（纯 numpy、k-means++ 初始化、固定种子，可复现）
- **归纳**：每簇取离质心最近的若干对，让 LLM 起名 / 给适用条件 / 建议做法 / 该避免的写法
- **产出**：每簇的**候选胜率**（Wilson CI）+ 与池基准率对照

方案 §9 明说「不得预设只有固定策略」——策略名与内容全部由数据聚类 + LLM 归纳得出。

用法：
    python scripts/strategy_discovery.py --dry-run
    python scripts/strategy_discovery.py --k 8
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_eval as he  # noqa: E402
from app import db  # noqa: E402
from app.gateway import chat  # noqa: E402
from app.models import ExpressionStrategy  # noqa: E402

EMB_MODEL = "BAAI/bge-small-zh-v1.5"
SUMMARIZE_MODEL = "moonshotai/kimi-k3"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def load_pairs(batch: str | None = None) -> list[dict]:
    """已判二选一的 (人类段, 候选段) 对。"""
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.human_verdict hv, ri.reasons rs, c.id cid, c.text ctext,
                  c.experiment_id exp, s.text stext, w.title work
           from review_items ri
           join candidates c on c.id = ri.subject_id
           join segments s on s.id = c.segment_id
           join works w on w.id = s.work_id
           where ri.status = 'done'
             and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""").fetchall()
    con.close()
    want = {f"batch_{t.strip()}" for t in batch.split(",")} if batch else None
    out, seen = [], set()
    for r in rows:
        if want is not None and not (want & set(json.loads(r["rs"] or "[]"))):
            continue
        hv = json.loads(r["hv"]) if r["hv"] else {}
        w = hv.get("winner_resolved")
        if w not in ("human", "candidate") or r["cid"] in seen:
            continue
        if not (r["ctext"] or "").strip() or not (r["stext"] or "").strip():
            continue
        seen.add(r["cid"])
        out.append({"cid": r["cid"], "human": r["stext"], "cand": r["ctext"],
                    "cand_won": w == "candidate", "exp": r["exp"], "work": r["work"]})
    return out


def kmeans(X: np.ndarray, k: int, iters: int = 200, seed: int = 20260917) -> tuple[np.ndarray, np.ndarray]:
    """纯 numpy k-means（k-means++ 初始化）。返回 (labels, centroids)。"""
    rng = np.random.default_rng(seed)
    n = len(X)
    centers = [X[rng.integers(n)]]
    for _ in range(k - 1):
        d2 = np.min(((X[:, None, :] - np.array(centers)[None, :, :]) ** 2).sum(-1), axis=1)
        p = d2 / d2.sum() if d2.sum() > 0 else np.ones(n) / n
        centers.append(X[rng.choice(n, p=p)])
    C = np.array(centers)
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        dist = ((X[:, None, :] - C[None, :, :]) ** 2).sum(-1)
        new = dist.argmin(1)
        if (new == labels).all():
            break
        labels = new
        for j in range(k):
            m = labels == j
            if m.any():
                C[j] = X[m].mean(0)
    return labels, C


def summarize(members: list[dict], k_index: int) -> dict:
    """让 LLM 给一簇归纳策略（照 §4.4 的字段）。"""
    blocks = []
    for i, m in enumerate(members[:5], 1):
        blocks.append(f"【例 {i}】\n人类原句：{m['human'][:150]}\nAI 重建：{m['cand'][:150]}\n"
                      f"（作者判定：{'AI 更好' if m['cand_won'] else '人类更好'}）")
    prompt = (
        "下面是同一段语义的「人类原文」与「AI 重建」的成对样本，它们被聚在同一簇里"
        "（表示两者的**表达差异方式相似**）。\n\n" + "\n\n".join(blocks) +
        "\n\n请归纳这一簇的表达策略差异——人类这边在**怎么做**，AI 那边在**怎么做**。\n"
        "只输出一行 JSON：\n"
        '{"name":"策略名（≤10字，动宾或名词短语）","description":"一句话说清差异",'
        '"conditions":["什么语义/情绪条件下会出现"],"recommended":["人类侧的做法"],'
        '"avoid":["AI 侧的坏习惯"]}')
    try:
        r = chat(model=SUMMARIZE_MODEL, system="你是中文小说编辑，只做归纳，不给总体评价。",
                 user=prompt, purpose="strategy_summarize",
                 prompt_version="strategy_discovery_v1", temperature=0.2, max_tokens=800)
        raw = (r.text or "").strip()
        d = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        return {"name": str(d.get("name", f"簇{k_index}"))[:60],
                "description": str(d.get("description", ""))[:300],
                "conditions": [str(x)[:80] for x in (d.get("conditions") or [])][:6],
                "recommended": [str(x)[:80] for x in (d.get("recommended") or [])][:6],
                "avoid": [str(x)[:80] for x in (d.get("avoid") or [])][:6]}
    except Exception as e:  # noqa: BLE001
        return {"name": f"簇{k_index}（归纳失败）", "description": str(e)[:200],
                "conditions": [], "recommended": [], "avoid": []}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=8, help="簇数")
    ap.add_argument("--batch", default=None, help="只用这些批（默认全部已判）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-write", action="store_true", help="只打印不写库")
    args = ap.parse_args()

    items = load_pairs(args.batch)
    n = len(items)
    base = sum(1 for x in items if x["cand_won"]) / n if n else float("nan")
    print(f"已判二选一对 = {n}；池基准率（候选胜）= {base:.3f}")
    if args.dry_run or n < 20:
        print("dry-run 或样本不足：未发起")
        return

    from fastembed import TextEmbedding
    emb = TextEmbedding(model_name=EMB_MODEL)
    print(f"embedding：{n} 对人类段 + {n} 条候选（本地 {EMB_MODEL}，零 API）…")
    H = np.array(list(emb.embed([x["human"] for x in items])), dtype=float)
    C = np.array(list(emb.embed([x["cand"] for x in items])), dtype=float)
    H /= np.linalg.norm(H, axis=1, keepdims=True) + 1e-9
    C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-9
    R = C - H                                   # 表达残差
    R /= np.linalg.norm(R, axis=1, keepdims=True) + 1e-9

    labels, centers = kmeans(R, args.k)
    print(f"k-means k={args.k} 完成")
    print()
    print(f"{'簇':>3s}{'n':>5s}{'候选胜':>7s}{'胜率':>8s}{'Wilson 95%':>18s}{'vs 池基准':>10s}")
    rows = []
    for j in range(args.k):
        idx = np.where(labels == j)[0]
        if not len(idx):
            continue
        k_ = int(sum(1 for i in idx if items[i]["cand_won"]))
        rate = k_ / len(idx)
        lo, hi = wilson(k_, len(idx))
        d = np.linalg.norm(R[idx] - centers[j], axis=1)
        order = idx[np.argsort(d)]              # 离质心最近 = 该簇最典型
        members = [items[i] for i in order]
        flag = "" if lo <= base <= hi else ("↑" if rate > base else "↓")
        print(f"{j:>3d}{len(idx):5d}{k_:7d}{rate:8.3f}   [{lo:.3f}, {hi:.3f}] {flag:>9s}")
        rows.append({"j": j, "idx": idx, "k": k_, "rate": rate, "lo": lo, "hi": hi,
                     "members": members})

    print()
    print("（↑/↓ = 该簇的 95% CI 不含池基准率；注意这是 " + str(args.k) + " 个簇的多重比较，")
    print("  单个 ↑/↓ 不足以当结论，要看簇内样本量与 CI 宽度。）")

    print()
    print("=== 逐簇归纳（LLM）===")
    out = []
    for r in rows:
        s = summarize(r["members"], r["j"])
        dist = {}
        for m in r["members"]:
            dist[m["work"]] = dist.get(m["work"], 0) + 1
        print(f"\n[{r['j']}] n={len(r['idx'])} 候选胜率 {r['rate']:.3f} "
              f"[{r['lo']:.3f},{r['hi']:.3f}]   {s['name']}")
        print(f"     {s['description']}")
        if s["recommended"]:
            print(f"     人类侧做法: {'；'.join(s['recommended'][:3])}")
        if s["avoid"]:
            print(f"     AI 侧习惯: {'；'.join(s['avoid'][:3])}")
        out.append({"r": r, "s": s, "dist": dist, "members": r["members"]})

    if args.no_write:
        print("\n--no-write：未写库")
        return
    with db.session() as sess:
        sess.query(ExpressionStrategy).delete()      # 每次重跑覆盖（口径变了旧簇无意义）
        for o in out:
            r, s = o["r"], o["s"]
            ex = [{"human": m["human"][:200], "cand": m["cand"][:200],
                   "cand_won": m["cand_won"], "cid": m["cid"]} for m in o["members"][:4]]
            cx = [{"human": m["human"][:200], "cand": m["cand"][:200],
                   "cand_won": m["cand_won"], "cid": m["cid"]} for m in o["members"][-3:]]
            sess.add(ExpressionStrategy(
                name=s["name"], description=s["description"],
                conditions=s["conditions"], recommended=s["recommended"], avoid=s["avoid"],
                examples=ex, counter_examples=cx,
                n_items=int(len(r["idx"])), success_rate=float(r["rate"]),
                rate_lo=float(r["lo"]), rate_hi=float(r["hi"]),
                distribution=o["dist"],
                method=f"bge-small-zh residual + kmeans(k={args.k},seed=20260917)",
                version=1, source=f"{n} 条已判二选一对；SUMMARIZE={SUMMARIZE_MODEL}"))
        sess.commit()
    print(f"\n已写入 expression_strategies：{len(out)} 条策略")


if __name__ == "__main__":
    main()
