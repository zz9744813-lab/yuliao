"""给**全部已判条目**补跑额外评委模型（2026-09-17）。

## 为什么

终报里写明的一条局限是：**结论只基于 2 个模型**（kimi-k3 / deepseek-v4.1-flash）。
"评委复现不了集霸偏好" 到底是**任务**的问题，还是**这两个模型**的问题？
—— 换模型是唯一还没试过的轴。本脚本把可比的口径（`judge_preference_v4_heldout`，
与既有评委同源）补跑到更多模型上。

复用 `heldout_eval.run_one`：prompt / 上下文 / 位置随机种子（`heldout:{cid}`）与评测
**逐位一致**，所以新旧模型的结果可直接放到同一张表里比。幂等，可反复跑。

用法：
    python scripts/extra_judges.py --models meta/muse-spark-1.3-contributor --variants v4 --dry-run
    python scripts/extra_judges.py --models meta/muse-spark-1.3-contributor,agnes-3.0-flash --variants v4
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_eval as he  # noqa: E402
from app import db, limits  # noqa: E402
from app.context_ablation import scene_context  # noqa: E402
from app.gateway import is_serial_model  # noqa: E402
from app.models import Candidate, JudgeRun, Segment  # noqa: E402


def _pool_workers(conc: int, models=()) -> int:
    """线程池 worker 数的运行时兜底（上限真源：app/limits.py::MAX_CONCURRENCY）。

    CLI 闸（_check_conc）已对越界**报错退出**；本函数管绕过命令行直接调函数的
    路径：越界按上限截断并打印（不静默）；命中单账号 CLI 模型
    （app.gateway.is_serial_model，判定口径唯一，不许本脚本自比前缀）→ workers
    恒 1 并打印「串行强制」。界内正常值原样返回、零额外输出。
    """
    requested = max(1, int(conc))
    workers = min(requested, limits.MAX_CONCURRENCY)
    hits = [m for m in models if is_serial_model(m)]
    if hits:
        print(f"[conc] 串行强制：命中单账号 CLI 模型 {hits} → workers=1（请求 conc={conc}）",
              flush=True)
        return 1
    if workers != requested:
        print(f"[conc] 越界截断：conc={conc} → workers={workers}"
              f"（上限 app/limits.MAX_CONCURRENCY={limits.MAX_CONCURRENCY}）", flush=True)
    return workers


def _check_conc(ap, value: int, flag: str) -> None:
    """`--conc` 硬上界闸：超界响亮报错退出（parser.error → SystemExit(2)），不静默 clamp。"""
    if value > limits.MAX_CONCURRENCY:
        ap.error(f"{flag}={value} 超过上限 {limits.MAX_CONCURRENCY}"
                 f"（单一真源 app/limits.py::MAX_CONCURRENCY）；"
                 f"本脚本拒绝静默 clamp，越界即报错退出")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True, help="逗号分隔的模型 id")
    ap.add_argument("--variants", default="v4", help="默认只跑 v4（口径与既有评委同源）")
    ap.add_argument("--conc", type=int, default=4,
                    help=f"线程池并发（上界 app/limits.MAX_CONCURRENCY="
                         f"{limits.MAX_CONCURRENCY}，越界报错退出）；agy 等单账号 CLI 通道"
                         f"（gateway.is_serial_model 命中）会被强制串行 workers=1")
    ap.add_argument("--batch", default=None,
                    help="只跑这些批次（逗号分隔），如 h30,mix30,nq50")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    _check_conc(ap, args.conc, "--conc")
    return args


def _run_pool(jobs: list, conc: int, models: list[str]) -> None:
    """执行本脚本全部评委补跑；worker 数由 _pool_workers 兜底（上限+串行）。"""
    workers = _pool_workers(conc, models)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(lambda j: he.run_one(*j), jobs):
            pass


def load_cids(batch: str | None = None) -> list[str]:
    """已判二选一条目（所有实验）。batch= 逗号分隔的批次标签则只取那些批。"""
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.human_verdict hv, ri.reasons rs, c.id cid
           from review_items ri join candidates c on c.id = ri.subject_id
           where ri.status = 'done'
             and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""").fetchall()
    con.close()
    want = {f"batch_{t.strip()}" for t in batch.split(",")} if batch else None
    out, seen = [], set()
    for r in rows:
        if want is not None:
            tags = json.loads(r["rs"] or "[]")
            if not (want & set(tags)):
                continue
        hv = json.loads(r["hv"]) if r["hv"] else {}
        if hv.get("winner_resolved") not in ("human", "candidate"):
            continue
        if r["cid"] in seen:
            continue
        seen.add(r["cid"])
        out.append(r["cid"])
    return out


def verify(cids, models, variants) -> list[str]:
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row      # 少了这行 _read_verdict 内部会按元组取键而崩
    gaps = []
    for cid in cids:
        for m in models:
            for v in variants:
                if he._read_verdict(cid, m, he.PROMPT_VARIANTS[v][1], con) is None:
                    gaps.append((cid[:14], m.split("/")[-1][:16], v))
    con.close()
    return gaps


def main() -> None:
    args = parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    for v in variants:
        if v not in he.PROMPT_VARIANTS:
            raise SystemExit(f"未知口径 {v}；可选 {list(he.PROMPT_VARIANTS)}")

    db.init_db()
    cids = load_cids(args.batch)
    need = len(cids) * len(models) * len(variants)
    print(f"已判二选一条目 {len(cids)} × {len(models)} 模型 × {len(variants)} 口径 = {need} 次调用")
    print(f"  → 已缓存的部分会自动跳过（幂等）")
    if args.dry_run:
        print("dry-run：未发起")
        return

    ctx_by_cid: dict[str, str] = {}
    with db.session() as s:
        for cid in cids:
            cand = s.get(Candidate, cid)
            human = s.get(Segment, cand.segment_id)
            texts, _ = scene_context(s, human)
            ctx_by_cid[cid] = ("\n\n").join(texts)
    med = sorted(len(v) for v in ctx_by_cid.values())[len(ctx_by_cid) // 2]
    print(f"上文中位 {med} 字")

    jobs = [(cid, ctx_by_cid[cid], m, v) for m in models for v in variants for cid in cids]
    _run_pool(jobs, args.conc, models)
    print(f"完成：ok={he._counter['ok']} failed={he._counter['failed']} skip={he._counter['skip']}")

    gaps = verify(cids, models, variants)
    if gaps:
        print(f"\n仍有缺口 {len(gaps)} 项（多为解析失败，重跑本脚本即可）")
        for g in gaps[:8]:
            print("  ", g)
        raise SystemExit(1)
    print("覆盖核对通过")


if __name__ == "__main__":
    main()
