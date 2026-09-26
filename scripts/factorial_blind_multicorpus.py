"""跨语料 2×2 盲批评次构建 + 窗口评委结果落盘。

四语料各生成一个盲评批（默认 9 段 × {HB,HC,HD,CD} = 36 项）：
  prepare --corpus douluo --n 9
  # 评委（当前窗口，不看 .map.json）按顺序给判定
  commit --batch data/blind/factorial_douluo_batch.json --verdicts '["A","B","tie",...]'

段入选条件：v2 切分、naturalness-eligible、有 primary L-frame、
且该 L-frame 在至少一个模型下 B0/C/D 三组候选齐全（同模型内比，CD 才同源）。
模型在段间轮流配平。A/B 位置随机，映射只进 .map.json（评委纪律：构建者判完前不读）。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import config, db
from app.context_ablation import neighbors
from app.models import Candidate, Frame, Segment
from app.reconstruct import RECON_PROMPT_VERSION as PV_B0
from k2_extract_backfill import integrity_flag_state  # noqa: E402  严格布尔解析层同源

BLIND_DIR = Path(__file__).resolve().parent.parent / "data" / "blind"

CORPORA = {
    "qiongming": "EXP-0914-C812",
    "jiangye": "EXP-0914-FF7B",
    "fanren": "EXP-0914-EE18",
    "douluo": "EXP-0914-6DD3",
    "ba19": "EXP-0913-BA19",   # 琼明旧实验：候选齐全，作 builder 冒烟基准
}
MODELS = [config.DEFAULT_LLM_MODEL, "meta/muse-spark-1.3-contributor"]
PV_C, PV_D = "recon_ctx_v1", "recon_ctxonly_v1"


def _eligible(seg: Segment) -> bool:
    """naturalness-eligible 闸：**严格布尔**（JSON true 才过），非字典/类型不严/
    解析失败=未校验→不过闸，绝不抛异常。写入侧 app/segment_integrity.py 恒落
    严格 bool（bool(eligible)），收紧 bool(...) 松判口径对真库无行为差。"""
    return integrity_flag_state(seg, "eligible") is True


def _groups_by_model(s, eid: str, frame_ids: set) -> dict:
    """{frame_id: {model: {pv: text}}} 只收 status=ok 且非空文本。"""
    out: dict = {}
    if not frame_ids:
        return out
    q = (s.query(Candidate)
         .filter(Candidate.experiment_id == eid,
                 Candidate.frame_id.in_(frame_ids),
                 Candidate.status == "ok",
                 Candidate.prompt_version.in_([PV_B0, PV_C, PV_D])))
    for c in q:
        if not c.text or len(c.text.strip()) < 20:
            continue
        out.setdefault(c.frame_id, {}).setdefault(c.model, {})[c.prompt_version] = c.text
    return out


def _complete(groups: dict) -> list:
    """该 frame 下三组齐全的模型列表。"""
    return [m for m, pvs in groups.items()
            if all(pv in pvs for pv in (PV_B0, PV_C, PV_D))]


def collect(s, eid: str, n: int):
    frames = (s.query(Frame)
              .filter_by(experiment_id=eid, granularity="L",
                         is_primary=True)
              .filter(Frame.status != "failed").all())
    seg_ids = {f.segment_id for f in frames}
    segs = {x.id: x for x in s.query(Segment)
            .filter(Segment.id.in_(seg_ids),
                    Segment.seg_version == 2).all()}
    groups = _groups_by_model(s, eid, {f.id for f in frames})

    # frame → segment 映射（同段多 frame 取第一个）
    cand_rows = []
    for f in frames:
        seg = segs.get(f.segment_id)
        if not seg or not _eligible(seg) or not f.payload:
            continue
        models = _complete(groups.get(f.id, {}))
        if not models:
            continue
        cand_rows.append((f, seg, models))
    # 章节序排序后均匀抽样，保证跨章多样性
    cand_rows.sort(key=lambda r: (r[1].chapter or 0, r[1].ordinal or 0))
    step = max(1, len(cand_rows) // n) if n else 1
    picked, used = [], set()
    for i in range(0, len(cand_rows), step):
        f, seg, models = cand_rows[i]
        if seg.id in used:
            continue
        picked.append((f, seg, models))
        used.add(seg.id)
        if len(picked) >= n:
            break
    return picked, groups, len(cand_rows)


def _context(s, seg: Segment) -> str:
    nb = neighbors(s, seg)
    parts = [nb["prev2"].text if nb["prev2"] else None,
             nb["prev1"].text if nb["prev1"] else None]
    return "\n\n".join(p for p in parts if p) or "（无上文）"


def cmd_prepare(corpus: str, n: int, seed: int = 20260914) -> None:
    eid = CORPORA[corpus]
    rng = random.Random(seed)
    BLIND_DIR.mkdir(parents=True, exist_ok=True)
    with db.session() as s:
        picked, groups, pool = collect(s, eid, n)
        print(f"[{corpus}] 池：{pool} 段齐全，入选 {len(picked)}")
        batch, mapping = [], []
        for idx, (f, seg, models) in enumerate(picked):
            model = models[idx % len(models)] if len(models) > 1 else models[0]
            pvs = groups[f.id][model]
            sources = {"H": seg.text, "B": pvs[PV_B0],
                       "C": pvs[PV_C], "D": pvs[PV_D]}
            ctx = _context(s, seg)
            for comp in ("HB", "HC", "HD", "CD"):
                g1, g2 = comp[0], comp[1]
                first = rng.random() < 0.5
                a, b = (sources[g1], sources[g2]) if first else (sources[g2], sources[g1])
                item_id = f"Q{len(batch):02d}"
                batch.append({"item_id": item_id, "comp": comp,
                              "ctx_mode": "prev2", "context": ctx,
                              "text_a": a, "text_b": b})
                mapping.append({"item_id": item_id, "comp": comp,
                                "segment_id": seg.id, "frame_id": f.id,
                                "model": model, "first_group": g1 if first else g2})
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bpath = BLIND_DIR / f"factorial_{corpus}_batch.json"
    mpath = BLIND_DIR / f"factorial_{corpus}_batch.map.json"
    bpath.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    mpath.write_text(json.dumps({"eid": eid, "stamp": stamp, "map": mapping},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    n_models = {}
    for m in mapping:
        n_models[m["model"]] = n_models.get(m["model"], 0) + 1
    print(f"  批次: {bpath}（{len(batch)} 项）  模型配平: {n_models}")
    print(f"  映射: {mpath}（评委判完前禁止读取）")


def cmd_commit(batch_path: str, verdicts: str) -> None:
    bp = Path(batch_path)
    batch = json.loads(bp.read_text(encoding="utf-8"))
    mp = bp.with_suffix("").with_suffix("")  # strip .json → .map.json rebuild below
    mp = bp.parent / (bp.stem + ".map.json")
    verdict_list = json.loads(verdicts)
    if len(verdict_list) != len(batch):
        raise SystemExit(f"verdicts {len(verdict_list)} != batch {len(batch)}")
    mapping = {m["item_id"]: m for m in json.loads(mp.read_text(encoding="utf-8"))["map"]}
    results = []
    for item, v in zip(batch, verdict_list):
        if v not in ("A", "B", "tie"):
            raise SystemExit(f"bad verdict {v!r}")
        m = mapping[item["item_id"]]
        # first_group 是 A 位置所属组；B 位置是另一组
        groups_pair = {"HB": ("H", "B"), "HC": ("H", "C"),
                       "HD": ("H", "D"), "CD": ("C", "D")}[m["comp"]]
        other = [g for g in groups_pair if g != m["first_group"]][0]
        winner = None if v == "tie" else (m["first_group"] if v == "A" else other)
        results.append({**{k: m[k] for k in
                           ("item_id", "comp", "segment_id", "frame_id", "model")},
                        "verdict": v, "winner_group": winner})
    rpath = bp.parent / (bp.stem.replace("_batch", "_result") + ".json")
    old = json.loads(rpath.read_text(encoding="utf-8")) if rpath.exists() else []
    old = [r for r in old if r["item_id"] not in {x["item_id"] for x in results}]
    rpath.write_text(json.dumps(old + results, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    print(f"  结果: {rpath}（{len(old) + len(results)} 条）")


def cmd_dry() -> None:
    with db.session() as s:
        for name, eid in CORPORA.items():
            picked, _, pool = collect(s, eid, 9)
            models = [m for _, _, ms in picked for m in ms]
            print(f"[{name}] 池 {pool} / 可选段 {len(picked)} / 模型覆盖 {len(models)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "commit", "dry"])
    ap.add_argument("--corpus", choices=list(CORPORA))
    ap.add_argument("--n", type=int, default=9)
    ap.add_argument("--batch")
    ap.add_argument("--verdicts")
    a = ap.parse_args()
    if a.cmd == "dry":
        cmd_dry()
    elif a.cmd == "prepare":
        cmd_prepare(a.corpus, a.n)
    else:
        cmd_commit(a.batch, a.verdicts)


if __name__ == "__main__":
    main()
