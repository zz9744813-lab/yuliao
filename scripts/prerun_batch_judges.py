"""批次评委分预跑：在集霸批改**之前**，把留出集评测要用的评委分先算好。

动机（2026-09-16）：`heldout_eval.py` 只取 `status='done'` 的题，而 preference
评委分是在评测流程里**现跑**的。于是「集霸判完 → 出报告」这一步把两件事串在一起：
评委侧一旦出问题（provider 抽风、候选被纳入条件挡掉、上下文与用户所见不一致），
问题要等集霸花掉半小时批改之后才暴露。本脚本把评委侧前移并独立验证，判完即出报告。

口径保证：**直接复用 `heldout_eval.run_one`**，不重写 prompt 与调用逻辑。
上下文同样走 `context_ablation.scene_context`（§6⑥：评委与用户必须吃同一份上文）。
随机种子 `heldout:{cid}` 只由 cid 决定、与批次无关 → 预跑结果与评测当场跑**逐位一致**。

用法：
    python scripts/prerun_batch_judges.py --batch h30 --dry-run
    python scripts/prerun_batch_judges.py --batch h30 --limit 1    # 冒烟：先跑 1 题
    python scripts/prerun_batch_judges.py --batch h30

退出码非 0 = 覆盖不全（此时评测会缺数，**不要**当作成功）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import heldout_eval as he  # noqa: E402
from app import db  # noqa: E402
from app.context_ablation import scene_context  # noqa: E402
from app.models import Candidate, Segment  # noqa: E402


def load_cids(batch: str, db_path: Path | str | None = None,
              exp: str | None = None) -> list[str]:
    """取该批次的候选 id。纳入条件与 `heldout_eval.load_items` 保持一致，
    唯一差别是**不要求 status='done'**（预跑就是为了赶在标注之前）。

    `order by` 是为了让 `--limit N` 的冒烟可复现（`select distinct` 本身无序）。
    db_path 显式可传：不要靠 monkeypatch 模块全局 `DB`（见 `anchor_check` 的同款注释）。
    exp：实验号。None = 不限实验（跨语料混合批：同批题分属多个实验，
    这里只负责取题，落库时按**候选自己的实验**写，见 heldout_eval.run_one）。
    """
    con = sqlite3.connect(db_path or he.DB)
    sql = ("""select distinct ri.subject_id cid
              from review_items ri
              join candidates c on c.id = ri.subject_id
              where ri.reasons like ?
                and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""")
    args: list = [f"%batch_{batch}%"]
    if exp:
        sql += " and ri.experiment_id = ?"
        args.append(exp)
    rows = con.execute(sql + " order by ri.subject_id", args).fetchall()
    con.close()
    return [r[0] for r in rows]


def verify(cids: list[str], models: tuple[str, ...], variants: list[str],
           db_path: Path | str | None = None) -> list[str]:
    """回读数据库核对覆盖。**不信任内存计数器**——本项目已因静默失败白跑过全量。

    不按 experiment_id 过滤：候选 id 全局唯一，且混合批里各题实验不同。
    """
    con = sqlite3.connect(db_path or he.DB)
    gaps: list[str] = []
    for cid in cids:
        for m in models:
            for v in variants:
                pv = he.PROMPT_VARIANTS[v][1]
                r = con.execute(
                    """select status from judge_runs
                       where subject_type='candidate' and subject_id=?
                         and judge_kind='preference' and model=? and prompt_version=?
                       order by created_at desc limit 1""",
                    (cid, m, pv)).fetchone()
                if not r or r[0] != "ok":
                    gaps.append(f"{cid} / {m} / {v} -> {r[0] if r else 'MISSING'}")
    con.close()
    return gaps


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True, help="批次名（h30 / h31 …）")
    ap.add_argument("--variants", default="v3,v4",
                    help="要跑的口径，逗号分隔。默认 v3,v4 —— 与 heldout_eval 默认一致。")
    ap.add_argument("--models", default=",".join(he.JUDGES))
    ap.add_argument("--conc", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0,
                    help="只跑前 N 题（冒烟用）。>0 时跳过最终覆盖断言。")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--exp", default=None,
                    help="实验号；默认由批次标签反查（跨语料批次属于别的实验）")
    args = ap.parse_args()

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    for v in variants:
        if v not in he.PROMPT_VARIANTS:
            raise SystemExit(f"未知口径 {v}；可选 {list(he.PROMPT_VARIANTS)}")
    models = tuple(m.strip() for m in args.models.split(",") if m.strip())

    db.init_db()
    if args.exp:
        exp = args.exp
    else:
        exps = he.experiments_of_batch(args.batch)
        if not exps:
            raise SystemExit(f"批次 {args.batch} 不存在（没有任何 review_item 带此标签）")
        exp = exps[0] if len(exps) == 1 else None
        if exp is None:
            print(f"批次 {args.batch} 跨 {len(exps)} 个实验（跨语料混合批）"
                  f"→ 按候选各自的实验写入评委记录")
        elif exp != he.EXP:
            print(f"实验号 = {exp}（由批次反查）")
    cids = load_cids(args.batch, exp=exp)
    if not cids:
        raise SystemExit(f"批次 {args.batch} 没有任何符合条件的候选 —— 空数据不继续。")
    print(f"批次 {args.batch}：{len(cids)} 题 × {len(models)} 评委 × "
          f"{len(variants)} 口径 = {len(cids)*len(models)*len(variants)} 次调用")

    if args.limit:
        cids = cids[:args.limit]
        print(f"· limit={args.limit} → 只跑 {len(cids)} 题（冒烟）")

    ctx_by_cid: dict[str, str] = {}
    with db.session() as s:
        for cid in cids:
            cand = s.get(Candidate, cid)
            human = s.get(Segment, cand.segment_id)
            texts, _ = scene_context(s, human)
            ctx_by_cid[cid] = ("\n\n").join(texts)
    med = sorted(len(v) for v in ctx_by_cid.values())[len(ctx_by_cid) // 2]
    print(f"上文中位 {med} 字")

    if args.dry_run:
        print("dry-run：未发起")
        return

    jobs = [(cid, ctx_by_cid[cid], m, v)
            for m in models for v in variants for cid in cids]
    with ThreadPoolExecutor(max_workers=args.conc) as pool:
        for _ in pool.map(lambda j: he.run_one(*j, exp=exp), jobs):
            pass
    print(f"完成：ok={he._counter['ok']} failed={he._counter['failed']} "
          f"skip={he._counter['skip']}")

    if args.limit:
        print("（冒烟模式，跳过覆盖断言）")
        return
    gaps = verify(cids, models, variants)
    if gaps:
        print(f"\n覆盖不全：{len(gaps)} 项缺失 ——")
        for g in gaps[:20]:
            print("  ", g)
        raise SystemExit(1)
    print(f"覆盖核对通过：{len(cids)} 题 × {len(models)} 评委 × "
          f"{len(variants)} 口径 全部 status=ok")


if __name__ == "__main__":
    main()
