"""基准集构建 —— 总方案 §14（Hidden Benchmark / Hidden Set）。

## 为什么需要它

§53 的六条成功标准（Human Preference↑ / Implicitness↑ / Hidden Benchmark↑ …）里，
**没有一条现在测得了**，因为没有固定的、带答案的、不进训练的题集。
此前所有数字都来自"当时那批候选"，换了语料/换了题就不可比。

§14 明确把 **Controlled Corruption Detection** 列为基准的必含项——而劣化数据集
**自带答案**（哪边是人类原文），是整套体系里唯一不需要集霸再判一次的题源。

## §14 十二项子基准的现状（2026-09-19，T5 扩展后）

只有**答案键来自构造或已冻结数据**的子基准才建——没有答案键的"基准"是假仪器：

| §14 子基准 | 状态 | 载体 |
|---|---|---|
| Controlled Corruption Detection | ✅ cc-v1（171 题） | kind=corruption_detection |
| （按类型拆分） | ✅ cct-<类型>-v1 ×18 | kind=corruption_type |
| Naturalness | ✅ nat-v1（控制臂已排除） | kind=naturalness_pair，--task naturalness |
| Human-vs-AI Discrimination | ✅ hvai-v1（545 题，BS-95651478c6dd）——2026-09-19 已解锁（此前「待解锁」行已过期） | kind=human_vs_ai |
| Implicitness | ✅ 构题器就位（2026-09-22 离线预置：白名单 9 类「定义即显式化」劣化类型，答案键=构造，零 LLM 判读）；真库构建 --live 双闸（BENCH_ALLOW_LIVE=1）待 402 拍板 | kind=implicitness_pair |
| Semantic Fidelity / Pragmatics / Dialogue / Rhythm / Style | ⛔ 待解锁：需专门构题器 + 可验证答案键；可建性评估（Pragmatics/Rhythm）见 docs/benchmark-subs-20260919.md |
| Human Preference Prediction | ⛔ 待解锁：需要基准段上的集霸裁定（他没有判过基准段） |
| Reconstruction Quality | ⛔ 待解锁：同上，且需要排名口径 |
| Hard Case | ✅ 不在此建：hard_cases 表 + hard_case_mining.py 已是独立仪器 |

## 冻结与隔离（两条都是硬要求）

1. **冻结文本**：条目里存 A/B 原文，不只是 segment_id。语料清洗、切分器升级都会
   改变 segment 的内容；冻结后同一版基准的分数才能跨时间比较（§14 Regression）。
2. **隔离**：只取 `Segment.role='benchmark'` 的段，而这些段的文本已经
   被 `export_training.py` 排除在训练导出之外（§14：不得被训练读取）。

## 用法

    python scripts/benchmark_build.py --name cc-v1 --kind corruption_detection
    python scripts/benchmark_build.py --scan
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import db  # noqa: E402
from app.models import (BenchmarkItem, BenchmarkSet, ControlledCorruption, Segment,  # noqa: E402
                        exclude_corpus_v2_segments)
from app.ids import new_id  # noqa: E402


def _eligible_pairs(s) -> list[tuple[ControlledCorruption, Segment]]:
    """基准段上合格的劣化对（三个构建器共用同一套闸门，闸门不一致=子基准不可比）：
    status=ok + 有候选 + 只收 role='benchmark' 段 + 源文本 src_ok + 未被判病句。
    """
    bm = {x.id: x for x in s.query(Segment).filter(
        Segment.role == "benchmark", exclude_corpus_v2_segments()).all()}
    rows = []
    for cc in (s.query(ControlledCorruption)
               .filter(ControlledCorruption.status == "ok")
               .filter(ControlledCorruption.candidate_id.isnot(None)).all()):
        seg = bm.get(cc.segment_id)
        if seg is None:                       # 只收基准段
            continue
        try:
            integ = json.loads(seg.integrity or "{}")
        except Exception:
            integ = {}
        if integ.get("src_ok") is not True:   # 源文本必须干净
            continue
        dr = cc.drift or {}
        if isinstance(dr, str):
            try:
                dr = json.loads(dr)
            except Exception:
                dr = {}
        if dr.get("ungrammatical"):
            continue
        rows.append((cc, seg))
    return rows


def _frozen_items(s, st, rows, seed: int, kind: str) -> None:
    """落条目：位置用固定种子随机化（可复现），答案冻结在 item 上。"""
    from app.context_ablation import scene_context
    rng = random.Random(seed)
    for cc, seg in rows:
        human = seg.text_clean or seg.text
        if rng.random() < 0.5:
            a, b, ans = human, cc.text, "A"
        else:
            a, b, ans = cc.text, human, "B"
        # 上文与评审台默认口径一致（near1）：基准测的必须是"人判/机判"同一个任务
        ctxs, _ = scene_context(s, seg)
        s.add(BenchmarkItem(set_id=st.id, segment_id=seg.id,
                            kind=kind,
                            context=(ctxs or [""])[-1] if ctxs else "",
                            text_a=a, text_b=b, answer=ans,
                            meta={"corruption_type": cc.corruption_type,
                                  "variable": cc.variable, "drift": cc.drift_score}))


def build_corruption_detection(name: str, version: int = 1, seed: int = 20260918,
                               dry_run: bool = False) -> dict:
    """把 `role='benchmark'` 段上的劣化对做成"哪边是原文"的判别题。

    位置用固定种子随机化（可复现），答案记在 item 上。
    只收：源文本完好（src_ok）+ 变体未被判病句/机械劣化的。
    """
    with db.session() as s:
        rows = _eligible_pairs(s)
        if dry_run:
            return {"would_build": len(rows), "segments": len({x[1].id for x in rows})}

        st = BenchmarkSet(id=new_id("BS"), name=name, version=version,
                          kind="corruption_detection", n_items=len(rows),
                          spec={"source": "controlled_corruptions",
                                "segment_role": "benchmark",
                                "require_src_ok": True,
                                "require_not_ungrammatical": True,
                                "position_seed": seed,
                                "ctx": "near1"},
                          note="判别题：A/B 哪一边是**人类原文**（另一边是按单一变量劣化的版本）")
        s.add(st)
        s.flush()
        _frozen_items(s, st, rows, seed, "corruption_detection")
        s.commit()
        return {"set_id": st.id, "items": len(rows),
                "segments": len({x[1].id for x in rows})}


def build_corruption_type_sets(name_prefix: str, version: int = 1, seed: int = 20260918,
                               dry_run: bool = False) -> dict:
    """corruption_type 子基准（任务 11 扩展，T5）：**每个劣化类型一个冻结集合**。

    为什么拆：cc-v1 的 by_type 只是运行后明细，类型不是一等公民——想单独跑某个
    类型、做类型间回归对比、或给某类型加题，都得动整表。拆开后每个类型有自己的
    set_id / 排行榜，条目口径与 cc-v1 完全同闸门（_eligible_pairs）+ 同位置种子，
    与 cc-v1 同对同序，可交叉核对。
    """
    with db.session() as s:
        rows = _eligible_pairs(s)
        by_type: dict[str, list] = {}
        for cc, seg in rows:
            by_type.setdefault(cc.corruption_type or "∅", []).append((cc, seg))
        if dry_run:
            return {"would_build": {t: len(v) for t, v in sorted(by_type.items())}}
        created = []
        for t, trs in sorted(by_type.items()):
            st = BenchmarkSet(id=new_id("BS"), name=f"{name_prefix}-{t}", version=version,
                              kind="corruption_type", n_items=len(trs),
                              spec={"source": "controlled_corruptions",
                                    "segment_role": "benchmark",
                                    "corruption_type": t,
                                    "require_src_ok": True,
                                    "require_not_ungrammatical": True,
                                    "position_seed": seed,
                                    "ctx": "near1"},
                              note=f"按类型子基准：只含 {t} 的判别题（哪边是原文）")
            s.add(st)
            s.flush()
            _frozen_items(s, st, trs, seed, "corruption_type")
            created.append({"set_id": st.id, "type": t, "items": len(trs)})
        s.commit()
        return {"sets": created, "n_sets": len(created)}


# 控制臂不进 naturalness_pair：中性改写**不声称**劣化自然度，把它算进"答案=人类侧"
# 的题里是往答案键里掺噪声（§7.5 纪律 2 的镜像：控制臂不进 DPO，同理不进自然度基准）。
NATURALNESS_EXCLUDED_TYPES = frozenset({"NEUTRAL_PARAPHRASE"})


def build_naturalness_pairs(name: str, version: int = 1, seed: int = 20260918,
                            dry_run: bool = False) -> dict:
    """naturalness_pair 子基准（任务 11 扩展，T5）：**自然度**轴的成对题。

    与 corruption_detection 的差别在**问的问题**：不问"哪边是原文"（身份），
    问"哪边更自然"（质量轴）——身份检测会混淆"认出原文"与"哪边通顺"两件事
    （交接 §0.5③：评委认得出原文却偏好 AI 措辞，两个轴是分开的）。
    答案键的来源是**构造**：人类侧=出版网文原文，劣化类型按定义拉低自然度；
    控制臂（NEUTRAL_PARAPHRASE）不声称拉低自然度 → 排除（见上方常量）。
    判题走 benchmark_run.py --task naturalness（NAT 模板）。
    """
    with db.session() as s:
        rows = [(cc, seg) for cc, seg in _eligible_pairs(s)
                if cc.corruption_type not in NATURALNESS_EXCLUDED_TYPES]
        if dry_run:
            return {"would_build": len(rows), "segments": len({x[1].id for x in rows})}
        st = BenchmarkSet(id=new_id("BS"), name=name, version=version,
                          kind="naturalness_pair", n_items=len(rows),
                          spec={"source": "controlled_corruptions",
                                "segment_role": "benchmark",
                                "require_src_ok": True,
                                "require_not_ungrammatical": True,
                                "excluded_types": sorted(NATURALNESS_EXCLUDED_TYPES),
                                "position_seed": seed,
                                "ctx": "near1",
                                "answer_semantics": "answer=人类原文侧（构造性答案：劣化按定义拉低自然度）"},
                          note="自然度成对题：A/B 哪一边**更自然**（不问身份；task=naturalness 判题）")
        s.add(st)
        s.flush()
        _frozen_items(s, st, rows, seed, "naturalness_pair")
        s.commit()
        return {"set_id": st.id, "items": len(rows),
                "segments": len({x[1].id for x in rows})}


# Implicitness 收录白名单：只收**按类型定义即「含蓄→直白」**的劣化类型
# （每条的依据=controlled_corruption.TYPES 里该类型 variable 的原文定义）。
# 方向与 naturalness 的排除表相反：显式化是少数派轴——凡定义不声称
# 「把暗含之意写明」的类型混入，都会把「答案=原文侧（更隐含）」的构造性
# 答案键掺噪（同 §7.5 纪律 2 镜像：控制臂 NEUTRAL_PARAPHRASE 永不入）。
IMPLICITNESS_INCLUDED_TYPES = frozenset({
    "EXPLICITIZE",                # 把留给读者推断的暗含之意直接写出来
    "SUBTEXT_ERASE",              # 把整段所有暗含之意一并摊到明面（含蓄→直白）
    "OVER_EXPLAIN",               # 追加动机/因果解释
    "EMOTION_LABEL",              # 把靠动作表现的内心情绪直接命名
    "PSYCHOLOGY_LABEL",           # 插入内心活动的解说性句子
    "LOGIC_CONNECTOR_INFLATION",  # 句间逻辑关系全部写明
    "ABSTRACT_SUMMARY",           # 追加抽象概括/点题（把暗示的主题说破）
    "SEMANTIC_OVERCOMPLETION",    # 把没写的结果/因果链末端说尽
    "NARRATOR_JUDGMENT",          # 叙述者直接下评价判断
})


def build_implicitness_pairs(name: str, version: int = 1, seed: int = 20260918,
                             dry_run: bool = False, *, live: bool = False,
                             out_dir: str = "") -> dict:
    """implicitness_pair 子基准（§14 Implicitness，2026-09-22 监管指令离线预置）。

    问的问题：A/B 哪一边**更含蓄**（更少把暗含之意写明）。
    答案键=构造：人类侧=出版网文原文（按定义含蓄）；劣化侧=白名单类型
    变体，类型定义即显式化 → 隐含度低于原文 → answer=人类原文侧。
    **零 LLM 判读参与答案键**（与 naturalness_pair 同一构造性模式）。
    闸门与三个既有构建器**同一套**（_eligible_pairs：status=ok + 有候选 +
    role='benchmark' + src_ok + 未判病句）——闸门不一致=子基准不可比。

    默认离线**零库写**：只读库构造条目，落盘走 out_dir（已存在即拒）；
    live=True 才写 BenchmarkSet+Items 进库——main 层 --live 双闸
    （BENCH_ALLOW_LIVE=1），402 拍板后再接。同名守卫：live 下同名同
    kind 已存在 → 拒（不覆盖既有集合）；离线模式仅报告 name_conflict。
    同种子重建 → 同位置同答案（与既有构建器同 rng 口径，可跨集核对）。
    """
    with db.session() as s:
        existed = (s.query(BenchmarkSet)
                   .filter_by(name=name, kind="implicitness_pair").first())
        conflict = None if existed is None else {"set_id": existed.id,
                                                 "n_items": existed.n_items}
        rows = [(cc, seg) for cc, seg in _eligible_pairs(s)
                if cc.corruption_type in IMPLICITNESS_INCLUDED_TYPES]
        by_type = {t: sum(1 for cc, _ in rows if cc.corruption_type == t)
                   for t in sorted(IMPLICITNESS_INCLUDED_TYPES)}
        if dry_run:
            return {"would_build": len(rows),
                    "segments": len({x[1].id for x in rows}),
                    "by_type": by_type, "name_conflict": conflict}
        # 构造条目（内存，与 _frozen_items 同 rng 口径：每行一次 random()）
        rng = random.Random(seed)
        from app.context_ablation import scene_context
        items = []
        for cc, seg in rows:
            human = seg.text_clean or seg.text
            if rng.random() < 0.5:
                a, b, ans = human, cc.text, "A"
            else:
                a, b, ans = cc.text, human, "B"
            ctxs, _ = scene_context(s, seg)
            items.append({"segment_id": seg.id, "kind": "implicitness_pair",
                         "context": (ctxs or [""])[-1] if ctxs else "",
                         "text_a": a, "text_b": b, "answer": ans,
                         "meta": {"corruption_type": cc.corruption_type,
                                  "variable": cc.variable,
                                  "drift": cc.drift_score}})
        spec = {"source": "controlled_corruptions",
                "segment_role": "benchmark",
                "included_types": sorted(IMPLICITNESS_INCLUDED_TYPES),
                "require_src_ok": True,
                "require_not_ungrammatical": True,
                "position_seed": seed,
                "ctx": "near1",
                "answer_semantics": ("answer=人类原文侧（构造性答案：白名单类型"
                                     "按定义显式化 → 劣化侧隐含度低于原文）")}
        if not live:
            out = {"live": False, "name": name, "n_items": len(items),
                   "by_type": {k: v for k, v in by_type.items() if v},
                   "name_conflict": conflict,
                   "fixture_note": "零库写——真库构建须 --live + BENCH_ALLOW_LIVE=1"
                                   "（402 拍板后）"}
            if out_dir:
                d = Path(out_dir)
                if d.exists():
                    raise SystemExit(f"--out 已存在：{out_dir}——不静默覆盖"
                                     "（离线产物也不许覆盖，同 --out 拒既存纪律）")
                d.mkdir(parents=True)
                (d / "implicitness_items.json").write_text(
                    json.dumps({"kind": "implicitness_pair", "name": name,
                                "version": version, "spec": spec,
                                "items": items}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                out["fixture_path"] = str(d / "implicitness_items.json")
            return out
        if existed is not None:
            raise SystemExit(
                f"同名 implicitness_pair 集已存在：{name}（{existed.id}，"
                f"{existed.n_items} 题）——拒覆盖（既有集合不可静默重建）。")
        st = BenchmarkSet(id=new_id("BS"), name=name, version=version,
                          kind="implicitness_pair", n_items=len(rows),
                          spec=spec,
                          note="隐含度成对题：A/B 哪一边**更含蓄**（更少把暗含之意写明；"
                               "answer=原文侧，构造性答案键）")
        s.add(st)
        s.flush()
        for it in items:
            s.add(BenchmarkItem(set_id=st.id, segment_id=it["segment_id"],
                                kind="implicitness_pair",
                                context=it["context"], text_a=it["text_a"],
                                text_b=it["text_b"], answer=it["answer"],
                                meta=it["meta"]))
        s.commit()
        return {"live": True, "set_id": st.id, "name": name,
                "items": len(rows), "by_type": by_type}


def build_human_vs_ai(name: str, version: int = 1, seed: int = 20260918,
                      dry_run: bool = False) -> dict:
    """human_vs_ai 子基准（§14 Human-vs-AI Discrimination，2026-09-19 解锁）。

    题源：基准段上的**自由重建**候选（prompt_version 在盲评白名单里的
    reconstruct_v1 / recon_ctx_v1，EXP-BENCH-RECON 的产物）——不是劣化变体。
    与 corruption_detection 的差别：劣化版是"按单一变量改坏的原文"，
    重建版是"模型从语义帧自由写出来的"——这才是真正的「人 vs AI」判别。
    答案键来自构造：answer=人类原文侧。隔离：候选在基准段上，
    训练导出与盲评池都已被 role='benchmark' 闸住。
    """
    from app.config import BLIND_REVIEW_PROMPT_VERSIONS
    from app.models import Candidate
    with db.session() as s:
        bm = {x.id: x for x in s.query(Segment).filter(
            Segment.role == "benchmark", exclude_corpus_v2_segments()).all()}
        rows = []
        for cand in (s.query(Candidate)
                     .filter(Candidate.status == "ok").all()):
            seg = bm.get(cand.segment_id)
            if seg is None or not cand.text:
                continue
            if cand.prompt_version not in BLIND_REVIEW_PROMPT_VERSIONS:
                continue
            try:
                integ = json.loads(seg.integrity or "{}")
            except Exception:
                integ = {}
            if integ.get("src_ok") is not True:
                continue
            rows.append((cand, seg))
        if dry_run:
            return {"would_build": len(rows), "segments": len({x[1].id for x in rows})}
        st = BenchmarkSet(id=new_id("BS"), name=name, version=version,
                          kind="human_vs_ai", n_items=len(rows),
                          spec={"source": "candidates_reconstruction",
                                "segment_role": "benchmark",
                                "prompt_versions": list(BLIND_REVIEW_PROMPT_VERSIONS),
                                "require_src_ok": True,
                                "position_seed": seed,
                                "ctx": "near1",
                                "answer_semantics": "answer=人类原文侧（构造性答案）"},
                          note="人 vs AI 判别题：A/B 哪一边是**人类原文**（另一边是帧重建候选）")
        s.add(st)
        s.flush()
        rng = random.Random(seed)
        from app.context_ablation import scene_context
        for cand, seg in rows:
            human = seg.text_clean or seg.text
            if rng.random() < 0.5:
                a, b, ans = human, cand.text, "A"
            else:
                a, b, ans = cand.text, human, "B"
            ctxs, _ = scene_context(s, seg)
            s.add(BenchmarkItem(set_id=st.id, segment_id=seg.id,
                                kind="human_vs_ai",
                                context=(ctxs or [""])[-1] if ctxs else "",
                                text_a=a, text_b=b, answer=ans,
                                meta={"prompt_version": cand.prompt_version,
                                      "recon_model": cand.model,
                                      "temperature": cand.temperature}))
        s.commit()
        return {"set_id": st.id, "items": len(rows),
                "segments": len({x[1].id for x in rows})}


def build_length_balanced(name: str, version: int = 1, seed: int = 20260919,
                          per_side: int = 19, dry_run: bool = False,
                          l_experiments: tuple[str, ...] | None = None,
                          s_experiments: tuple[str, ...] | None = None,
                          replace: bool = False) -> dict:
    """长度平衡基准（指标硬化收口，2026-09-20）：S（human 更短）与 L（human 更长）
    两侧各取一半——长度基线在平衡集上按构造 = 0.5，评委读数无法搭长度便车
    （军师 P1-3：nat-v1 0.944 / hvai 0.872 的读数全部带着长度混淆）。

    **按侧宇宙过滤**（2026-09-20 监督整改）：role='benchmark' 是持久单调标记，
    历次 split 留下的旧标记段永远在 _eligible_pairs 池里——不滤会混入
    旧代生成器的劣化行（bal-v2 首建实测 59.5% 行来自旧实验）。
    l_experiments / s_experiments 按**行**的 experiment_id 过滤各自一侧；
    None=不过滤（旧口径）。结构事实：窗口化产线只产 L 方向，PROD 无 S 库存——
    干净口径是 L 侧纯化到指定实验、S 侧用 legacy 库存并在 spec 显式声明。

    **同名守卫**：同名同 kind 已存在 → 默认拒绝（防重复集）；replace=True
    删旧建新并在返回与 spec 里留 replaced 痕迹（删了哪个集、多少题）——
    role='benchmark' 不回收（持久单调标记，段可能被别的集合共享；行级
    宇宙过滤是兜底层）。dry-run 也报 name_conflict，预演就能看出真跑
    会被拒绝还是要删哪个集。spec 记录实测宇宙与真实两侧库存。
    控制臂（NEUTRAL_PARAPHRASE）保留：判别题（哪边是原文）里它是合法题。
    """
    with db.session() as s:
        existed = (s.query(BenchmarkSet)
                   .filter_by(name=name, kind="length_balanced").first())
        conflict = None if existed is None else {"set_id": existed.id,
                                                 "n_items": existed.n_items}
        # 拒绝语义先于一切副作用（glm 席严重项：删旧不得早于建新成功的把握）
        if existed is not None and not dry_run and not replace:
            raise SystemExit(
                f"同名长度平衡集已存在：{name}（{existed.id}，{existed.n_items} 题）。"
                f"要重建用 --replace（旧集及条目将被删除）。")

        rows = _eligible_pairs(s)
        s_side, l_side = [], []
        for cc, seg in rows:
            human = (seg.text_clean or seg.text or "")
            var = cc.text or ""
            if l_experiments is not None and len(human) > len(var) \
                    and cc.experiment_id not in l_experiments:
                continue
            if s_experiments is not None and len(human) < len(var) \
                    and cc.experiment_id not in s_experiments:
                continue
            (s_side if len(human) < len(var) else
             l_side if len(human) > len(var) else []).append((cc, seg))
        universe = {
            "l_universe": list(l_experiments) if l_experiments else "all",
            "s_universe": list(s_experiments) if s_experiments else "all",
            "l_stock_measured": len(l_side), "s_stock_measured": len(s_side),
            "structural_note": (None if s_experiments or not l_experiments else
                                "S 侧未限定（窗口化产线只产 L 方向，PROD 无 S 库存）；"
                                "L 侧已纯化，S 侧 era 混杂见 spec 声明"),
        }
        if dry_run:
            return {"would_build": {"S": len(s_side), "L": len(l_side),
                                     "per_side": min(per_side, len(l_side), len(s_side))},
                    "universe": universe, "replaced": None,
                    "name_conflict": conflict,
                    "on_conflict": (None if conflict is None else
                                    ("replace（删旧建新，旧集见 name_conflict）"
                                     if replace else "refuse（SystemExit）"))}
        rng = random.Random(seed)
        rng.shuffle(s_side)
        rng.shuffle(l_side)
        k = min(per_side, len(s_side), len(l_side))
        if k == 0:
            raise SystemExit(
                f"建不出配平集：per_side={per_side}，实测库存 S={len(s_side)} / "
                f"L={len(l_side)}——一侧为 0 两侧都配不平；先 --dry-run 看库存，"
                "别建空集占名（同名守卫会挡住后续重建）。")
        replaced = None
        if existed is not None and replace:
            # 事务内删旧、不提前 commit：建新任一步失败（异常/SystemExit）→
            # 回滚 → 旧集幸存（glm 席严重项）。n_items 用 delete() 的
            # rowcount——计数列与实际行数脱钩时以真删数为准。
            del_n = s.query(BenchmarkItem).filter_by(set_id=existed.id).delete()
            s.query(BenchmarkSet).filter_by(id=existed.id).delete()
            replaced = {"set_id": existed.id, "n_items": del_n,
                        "prior": (existed.spec or {}).get("replaced")}
        picked = s_side[:k] + l_side[:k]
        st = BenchmarkSet(id=new_id("BS"), name=name, version=version,
                          kind="length_balanced", n_items=len(picked),
                          spec={"source": "controlled_corruptions",
                                "segment_role": "benchmark",
                                "balanced": "S/L 各半（长度基线按构造=0.5）",
                                "require_src_ok": True,
                                "require_not_ungrammatical": True,
                                "position_seed": seed,
                                "ctx": "near1",
                                "split": 2,
                                **universe,
                                **({"replaced": replaced} if replaced else {})},
                          note="长度平衡判别题：S/L 各半，读数不被长度先验污染。"
                               "实测宇宙见 spec（l_universe/s_universe/两侧实测库存）。")
        s.add(st)
        s.flush()
        _frozen_items(s, st, picked, seed, "length_balanced")
        s.commit()
        return {"set_id": st.id, "items": len(picked),
                "S": k, "L": k, "universe": universe, "replaced": replaced}


def scan() -> dict:
    with db.session() as s:
        sets = s.query(BenchmarkSet).all()
        out = []
        for st in sets:
            n = s.query(BenchmarkItem).filter_by(set_id=st.id).count()
            out.append((st.id, st.name, st.version, st.kind, st.n_items, n))
    con = sqlite3.connect(str(ROOT / "data" / "language_genome.db"))
    runs = {}
    for r in con.execute("select set_id, model, n, accuracy from benchmark_runs"):
        runs.setdefault(r[0], []).append((r[1], r[2], r[3]))
    con.close()
    print(f"{'集合':<34}{'名称':<16}{'版本':>4}{'口径':<24}{'条目':>6}{'实存':>6}")
    for sid, name, ver, kind, ni, n in out:
        print(f"{sid:<34}{name:<16}{ver:>4}{kind:<24}{ni:>6}{n:>6}")
    if runs:
        print("\n已有评测:")
        for sid, rs in runs.items():
            for model, n, acc in rs:
                print(f"   {sid}  {model[:30]:32} n={n} acc={acc:.3f}")
    return {"sets": len(out)}


def main() -> None:
    ap = argparse.ArgumentParser(description="基准集构建（§14 子基准，T5 扩展）")
    ap.add_argument("--name", default="cc-v1", help="集合名（corruption_type 时作为前缀）")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--kind", default="corruption_detection",
                    choices=("corruption_detection", "corruption_type", "naturalness_pair",
                             "human_vs_ai", "length_balanced", "implicitness_pair"))
    ap.add_argument("--live", action="store_true",
                    help="implicitness_pair 真库构建（拍板后）：需要环境变量 "
                         "BENCH_ALLOW_LIVE=1（双闸：402 拍板前防真库写）")
    ap.add_argument("--out", default="",
                    help="implicitness_pair 离线产物目录（已存在即拒；零库写）")
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--per-side", type=int, default=19, dest="per_side",
                    help="length_balanced 每侧题数（上限受 L 侧库存约束）")
    ap.add_argument("--l-experiments", default="", dest="l_experiments",
                    help="length_balanced L 侧行宇宙：逗号分隔 experiment_id（空=不过滤）")
    ap.add_argument("--s-experiments", default="", dest="s_experiments",
                    help="length_balanced S 侧行宇宙：同上")
    ap.add_argument("--replace", action="store_true",
                    help="length_balanced 同名重建：删旧建新（返回报 replaced）")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db.init_db()
    if args.scan:
        scan()
        return
    if args.kind == "corruption_type":
        out = build_corruption_type_sets(args.name, args.version, args.seed, args.dry_run)
    elif args.kind == "implicitness_pair":
        if args.live and os.environ.get("BENCH_ALLOW_LIVE") != "1":
            raise SystemExit("--live 需要环境变量 BENCH_ALLOW_LIVE=1（双闸："
                             "402 拍板前防真库写）")
        out = build_implicitness_pairs(args.name, args.version, args.seed,
                                       args.dry_run, live=args.live,
                                       out_dir=args.out)
    elif args.kind == "naturalness_pair":
        out = build_naturalness_pairs(args.name, args.version, args.seed, args.dry_run)
    elif args.kind == "human_vs_ai":
        out = build_human_vs_ai(args.name, args.version, args.seed, args.dry_run)
    elif args.kind == "length_balanced":
        lex = tuple(x for x in args.l_experiments.split(",") if x) or None
        sex = tuple(x for x in args.s_experiments.split(",") if x) or None
        out = build_length_balanced(args.name, args.version, args.seed,
                                    per_side=args.per_side, dry_run=args.dry_run,
                                    l_experiments=lex, s_experiments=sex,
                                    replace=args.replace)
    else:
        out = build_corruption_detection(args.name, args.version, args.seed, args.dry_run)
    print(json.dumps(out, ensure_ascii=False))
    scan()


if __name__ == "__main__":
    main()
