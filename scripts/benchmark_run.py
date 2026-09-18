"""基准评测 Runner —— 总方案 §14（Benchmark Runner / Regression / Leaderboard）。

## 它解决什么

§53 的成功标准要靠**固定的、带答案的、不进训练的题集**来测。本脚本把
`benchmark_sets` 里的题喂给任意模型，算准确率 + Wilson 区间，写 `benchmark_runs`，
并支持两次运行对比（Regression：升级之后有没有变差）。

## 两种任务口径（`--task`）

- `preference`（默认）：问"哪一边写得更好"——**与集霸在评审台上被问的是同一个问题**，
  所以它的"答对率"可以直接和集霸的判定率、评委的 0.857 放在一起比。
- `detection`：直接问"哪一边是人类作者原文"——更容易，用于 §14 的
  Human-vs-AI Discrimination 一项。

## 与评委判定的关系（别混）

评委判定（`judge_runs`）是**参与系统决策**的产物；基准运行（`benchmark_runs`）
是**对系统打分**的产物。二者读同一类文本，但后者**不得**回流去调 prompt / 选策略
（§14：Hidden Benchmark 不得被 Prompt 优化读取）。要调 prompt 就用别的批次调。

用法：
    python scripts/benchmark_run.py --scan
    python scripts/benchmark_run.py --set BS-xxxx --models "moonshotai/kimi-k3" --conc 6
    python scripts/benchmark_run.py --compare BS-xxxx --model moonshotai/kimi-k3
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import config, db  # noqa: E402
from app.gateway import chat  # noqa: E402
from app.models import BenchmarkItem, BenchmarkRun, BenchmarkSet  # noqa: E402

PV = "bench_task_v1"

SYS = "你是中文小说评审。只回答 A 或 B，不解释。"
PREF = """下面是同一段中文小说的两个版本（A / B），上文只帮你进入场景。

【上文】
{ctx}

【A】
{a}

【B】
{b}

哪一边**写得更好**？只输出一行 JSON：{{"pick": "A"|"B", "confidence": 0.0-1.0}}"""

DET = """下面是同一段中文小说的两个版本（A / B），其中**一边是人类作者的原文**，
另一边是被改写过（可能是改差）的版本。

【上文】
{ctx}

【A】
{a}

【B】
{b}

哪一边是**人类作者的原文**？只输出一行 JSON：{{"pick": "A"|"B", "confidence": 0.0-1.0}}"""


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


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


_lock = threading.Lock()
_stat = {"ok": 0, "failed": 0}


def run_set(*, set_id: str, models: list[str], task: str = "preference",
            conc: int = 6, limit: int = 0, dry_run: bool = False) -> dict:
    with db.session() as s:
        st = s.get(BenchmarkSet, set_id)
        if st is None:
            raise SystemExit(f"基准集不存在：{set_id}")
        items = s.query(BenchmarkItem).filter_by(set_id=set_id).all()
        rows = [{"id": x.id, "ctx": x.context or "", "a": x.text_a, "b": x.text_b,
                 "ans": x.answer, "meta": x.meta or {}, "kind": x.kind}
                for x in items]
    if limit:
        rows = rows[:limit]
    print(f"基准集 {set_id}（{st.name} v{st.version}）：{len(rows)} 题 × {len(models)} 模型"
          f"（task={task}）")
    if dry_run:
        return {"items": len(rows), "models": models}

    def one(job):
        r, m = job
        tmpl = DET if task == "detection" else PREF
        from app.prompt_render import render
        try:
            res = chat(model=m, system=SYS,
                       user=render(tmpl, ctx=r["ctx"] or "（无）", a=r["a"], b=r["b"]),
                       purpose="benchmark", prompt_version=PV,
                       temperature=0.0, max_tokens=800)
            pick = parse_pick(res.text)
        except Exception:                            # noqa: BLE001
            pick = None
        with _lock:
            _stat["ok" if pick else "failed"] += 1
        return {"id": r["id"], "pick": pick, "ans": r["ans"] if task == "detection"
                else ("A" if r["ans"] == "A" else "B"),
                "type": r["meta"].get("corruption_type", ""), "model": m}

    from app.gateway import split_models
    par, serial = split_models(models)
    if serial:
        print(f"串行模型（单账号共享额度，禁止并发）：{serial}")
    t0 = time.time()
    results = []
    if par:
        with ThreadPoolExecutor(max_workers=max(1, conc)) as ex:
            results += list(ex.map(one, [(r, m) for m in par for r in rows]))
    for m in serial:
        results += [one((r, m)) for r in rows]
    dt = time.time() - t0

    out = {}
    for m in models:
        rs = [x for x in results if x["model"] == m and x["pick"]]
        n = len(rs)
        # preference 口径下"答对" = 选了人类原文那一侧；位置已随机化，故需按 ans 判定
        # detection 口径答案是"哪边是原文"，两者在本题集里是同一个位置。
        k = sum(1 for x in rs if x["pick"] == x["ans"])
        lo, hi = wilson(k, n)
        by_type: dict[str, list[int]] = {}
        for x in rs:
            a = by_type.setdefault(x["type"], [0, 0])
            a[0] += 1
            a[1] += int(x["pick"] == x["ans"])
        detail = {"by_type": {t: {"n": v[0], "correct": v[1]} for t, v in by_type.items()},
                  "picks": {x["id"]: x["pick"] for x in rs}}
        with db.session() as s:
            s.add(BenchmarkRun(set_id=set_id, model=m, n=n, n_correct=k,
                               accuracy=(k / n if n else 0.0), detail=detail,
                               note=f"task={task} pv={PV}"))
            s.commit()
        out[m] = {"n": n, "correct": k, "acc": (k / n if n else 0.0), "ci": [lo, hi],
                  "by_type": detail["by_type"]}
    print(f"完成（{dt / 60:.1f} 分钟）；ok={_stat['ok']} failed={_stat['failed']}")
    return out


def leaderboard(set_id: str | None = None) -> None:
    con = sqlite3.connect(config.DATABASE_URL.replace("sqlite:///", ""))
    con.row_factory = sqlite3.Row
    q = """select set_id, model, n, n_correct, accuracy, created_at, note
           from benchmark_runs"""
    args: tuple = ()
    if set_id:
        q += " where set_id=?"
        args = (set_id,)
    q += " order by set_id, created_at desc"
    rows = con.execute(q, args).fetchall()
    con.close()
    if not rows:
        print("（还没有基准运行记录）")
        return
    print(f"{'基准集':<22}{'模型':<34}{'n':>5}{'答对':>6}{'准确率':>8}  时间")
    seen = set()
    for r in rows:
        key = (r["set_id"], r["model"], r["note"])
        if key in seen:          # 同一口径只显示最新一次（旧记录仍在库里，供 Regression）
            continue
        seen.add(key)
        print(f'{r["set_id"]:<22}{r["model"][:32]:<34}{r["n"]:>5}{r["n_correct"]:>6}'
              f'{r["accuracy"]:>8.3f}  {r["created_at"][:16]}')


def compare(set_id: str, model: str) -> None:
    """回归对比：同一模型在本基准集上的最近两次运行。"""
    con = sqlite3.connect(config.DATABASE_URL.replace("sqlite:///", ""))
    con.row_factory = sqlite3.Row
    rows = con.execute("""select id, n, n_correct, accuracy, detail, created_at
                          from benchmark_runs where set_id=? and model=?
                          order by created_at desc limit 2""", (set_id, model)).fetchall()
    con.close()
    if len(rows) < 2:
        print("不足两次运行，无法对比")
        return
    new, old = rows[0], rows[1]
    print(f'{model} @ {set_id}')
    print(f'  旧 {old["created_at"][:16]}  n={old["n"]}  acc={old["accuracy"]:.3f}')
    print(f'  新 {new["created_at"][:16]}  n={new["n"]}  acc={new["accuracy"]:.3f}')
    d = new["accuracy"] - old["accuracy"]
    print(f'  Δ = {d:+.3f}  {"退步 ⚠" if d < -0.02 else ("进步" if d > 0.02 else "无显著变化")}')
    try:
        o = json.loads(old["detail"] or "{}").get("picks", {})
        nw = json.loads(new["detail"] or "{}").get("picks", {})
        flips = [k for k in nw if k in o and o[k] != nw[k]]
        print(f'  翻转 {len(flips)} 题（旧/新判定不同的题）')
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="")
    ap.add_argument("--models", default="")
    ap.add_argument("--task", default="preference", choices=("preference", "detection"))
    ap.add_argument("--conc", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--model", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db.init_db()
    if args.scan:
        leaderboard(args.set or None)
        return
    if args.compare:
        if not (args.set and args.model):
            raise SystemExit("--compare 需要 --set 与 --model")
        compare(args.set, args.model)
        return
    if not (args.set and args.models):
        raise SystemExit("需要 --set 与 --models")
    out = run_set(set_id=args.set, models=[m.strip() for m in args.models.split(",") if m.strip()],
                  task=args.task, conc=args.conc, limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    leaderboard(args.set)


if __name__ == "__main__":
    main()
