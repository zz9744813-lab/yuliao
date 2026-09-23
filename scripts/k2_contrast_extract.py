"""K2 成对对照抽取器（paired_contrast_v2 前置机制，主控派工 2026-09-23）。

背景：8 条策略卡的"逐字证据"是单段描述，独立判断 24/24 判「证据不支持抽象
操作」0 条升格——根因：证据只说明"这一段在写什么"，与"人 X / AI Y 的对比"
无关（docs/策略语义审查_判定模型版_20260923.md）。合并方案定稿两条新卡
（docs/策略卡合并方案_20260923.md）：S1 留白摊开=语义增量、S2 节拍注水=
语义不变密度下降，互斥判据=主改动形态。

本模块换抽取协议：抽**成对对照**（同一场景的人类原文片段 ↔ 模型铺陈片段），
配三道**可机械判定**的门，让"引用≠支持"这类问题在落库前被拦掉：

- 门1 gate_keyword_cooccurrence：策略特征词表（S1=摊开/解释类信号词；
  S2=节拍/密度/修饰类信号词），要求 ai_text 命中**且** human_text 不命中；
  双向命中 / 双向未命中 / 反向命中 ⇒ 拒（理由可读，如 `S1 双向命中: [...]`）。
- 门2 gate_scene_reference：scene_keys 非空，且两侧的场景指称交集非空
  （人名/地名的字符集口径：指称的每个字符都出现在该侧文本中即视为该侧
  引用了此指称），证明两段写的是同一个场景；空集 ⇒ 拒。
- 门3 gate_length_ratio：len(ai)/len(human) ∈ [1.2, 6.0]，出界 ⇒ 拒
  （S2 的"注水"必须有可测的密度变化；S1 的"摊开"同理）。

纪律：
- **纯离线**：AI 侧片段由调用方注入（--pairs-file / run_contrast 入参），
  本工具不联网、不生成、不调模型。
- **fail-closed**：--dry-run（默认）零库写只出统计与拒绝样本；真落库需
  --live **且** 环境变量 K2CONTRAST_ALLOW_LIVE=1，缺一即拒；写库前照用既有
  R6 互斥锁（app.live_guard.live_lock），锁被持有即拒绝，不与 live/pytest 重叠。
- **落库走既有 strategy_instances 表结构**：不改任何表结构、不放宽既有
  门禁（verify_instance_span 照用——human 侧 span 对登记段逐字可核，
  核不上不落）；新实例 status=proposed（状态纪律：判定≠升格）。
- **幂等**：同 (strategy_id, strategy_version, evidence_sha256) 已有行
  即跳过——同输入重跑不重复落库。

用法：
    python scripts/k2_contrast_extract.py --dry-run --pairs-file pairs.json
    K2CONTRAST_ALLOW_LIVE=1 python scripts/k2_contrast_extract.py --live \
        --pairs-file pairs.json --extractor-model paired_contrast_v2
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PROTOCOL = "paired_contrast_v2"

# 合并方案 §1 定稿的两条新卡（strategy_key 照抄方案文档）
S1_KEY = "v2:留白摊开"
S2_KEY = "v2:节拍注水"

# 门1 特征词表（合并方案 §4 门A：S1 人类侧无解释类标记且 AI 侧有；
# S2 侧 AI 节拍/修饰标记成立且人类侧无）。机械 substring 匹配，离线确定。
S1_SPREAD_WORDS = (        # 摊开/解释类：AI 把潜台词摊成明文的信号
    "因为", "其实", "意味着", "心里明白", "说到底", "是由于",
    "换句话说", "之所以", "说白了", "归根结底",
)
S2_BEAT_WORDS = (          # 节拍/密度/修饰类：AI 拆拍注水的信号
    "忽然", "就在", "最后", "接着", "然后", "慢慢", "缓缓",
    "渐渐", "仿佛", "像是", "一连", "似乎", "轻轻", "一般",
)
FEATURE_WORDS: dict[str, tuple[str, ...]] = {
    S1_KEY: S1_SPREAD_WORDS,
    S2_KEY: S2_BEAT_WORDS,
}
_STRATEGY_LABEL = {S1_KEY: "S1", S2_KEY: "S2"}

# 门3 长度比边界（任务口径：S2 注水/S1 摊开都必须有可测的长度变化）
MIN_LEN_RATIO = 1.2
MAX_LEN_RATIO = 6.0


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class ContrastPair:
    """一条成对对照：同一场景的人类原文片段 ↔ AI 铺陈片段。

    span_start/span_end 是**人类侧**片段在其登记段原文里的区间
    （落库后由 app.knowledge.verify_instance_span 机械核对）；
    human_sha256 缺省时按 human_text 现算（幂等键的原料）；
    meta 携带落库定位（segment_id / text_version），不入门判。"""
    human_text: str
    ai_text: str
    scene_keys: set
    strategy_key: str
    span_start: int
    span_end: int
    human_sha256: str = ""
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.scene_keys = set(self.scene_keys or ())
        if not self.human_sha256:
            self.human_sha256 = _sha256(self.human_text)


# ---------------------------------------------------------------- 门 1
def gate_keyword_cooccurrence(pair: ContrastPair) -> tuple[bool, list[str]]:
    """ai_text 命中特征词 且 human_text 不命中才放行；三种破形全拒。"""
    words = FEATURE_WORDS.get(pair.strategy_key)
    if not words:
        return False, [f"未知策略 {pair.strategy_key}: 无特征词表"]
    label = _STRATEGY_LABEL.get(pair.strategy_key, pair.strategy_key)
    ai_hits = sorted(w for w in words if w in pair.ai_text)
    human_hits = sorted(w for w in words if w in pair.human_text)
    if ai_hits and not human_hits:
        return True, []
    if ai_hits and human_hits:
        return False, [f"{label} 双向命中: {human_hits}"]
    if not ai_hits and not human_hits:
        return False, [f"{label} 双向未命中: 特征词 {len(words)} 个全缺"
                       f"（AI 侧未呈现该策略改动形态）"]
    return False, [f"{label} 反向命中: human 命中 {human_hits} 而 AI 侧未命中"
                   f"（人类原文已含解释/节拍信号，不构成对照）"]


# ---------------------------------------------------------------- 门 2
def _scene_refs(scene_keys: set, text: str) -> set:
    """场景指称匹配（字符集口径）：指称的每个字符都出现在该侧文本中，
    即视为该侧引用了此指称。离线、确定、机械可核——不做分词猜名。"""
    chars = set(text or "")
    return {k for k in scene_keys if k and set(k) <= chars}


def gate_scene_reference(pair: ContrastPair) -> tuple[bool, list[str]]:
    """两侧场景指称交集非空才放行——证明两段写的是同一个场景。"""
    if not pair.scene_keys:
        return False, ["场景指称缺失: scene_keys 为空"]
    h = _scene_refs(pair.scene_keys, pair.human_text)
    a = _scene_refs(pair.scene_keys, pair.ai_text)
    if not (h & a):
        return False, [f"场景指称交集为空: human={sorted(h)} ai={sorted(a)}"
                       f"（两侧疑似不是同一场景）"]
    return True, []


# ---------------------------------------------------------------- 门 3
def gate_length_ratio(pair: ContrastPair) -> tuple[bool, list[str]]:
    """len(ai)/len(human) 必须落在 [1.2, 6.0]——两侧等长说明没有摊开/注水，
    超长说明已不是同一片段的铺陈化。"""
    n_h = len(pair.human_text or "")
    if n_h == 0:
        return False, ["长度比不可判: human_text 为空"]
    ratio = len(pair.ai_text or "") / n_h
    if not (MIN_LEN_RATIO <= ratio <= MAX_LEN_RATIO):
        return False, [f"长度比超界: {ratio:.2f} ∉ "
                       f"[{MIN_LEN_RATIO}, {MAX_LEN_RATIO}]"]
    return True, []


GATES = (("keyword_cooccurrence", gate_keyword_cooccurrence),
         ("scene_reference", gate_scene_reference),
         ("length_ratio", gate_length_ratio))


def gate_pair(pair: ContrastPair) -> tuple[bool, list[str]]:
    """三道门全过才放行；任一破形收集全部理由（不短路，拒绝样本可读）。"""
    ok, reasons = True, []
    for _name, gate in GATES:
        g_ok, g_reasons = gate(pair)
        if not g_ok:
            ok = False
            reasons.extend(g_reasons)
    return ok, reasons


# ------------------------------------------------------- 构建 / 汇总
def build_pairs(specs: list[dict], *,
                default_strategy_key: str | None = None) -> list[ContrastPair]:
    """把调用方注入的成对片段规格转成 ContrastPair 列表。

    每项规格：human_text / ai_text / scene_keys / span_start / span_end
    （必填），strategy_key（缺省用 default_strategy_key）、segment_id /
    text_version（落库定位，进 meta）可选。AI 侧片段**只能**从这里来。"""
    pairs = []
    for spec in specs:
        strategy_key = spec.get("strategy_key") or default_strategy_key
        if not strategy_key:
            raise ValueError("每条规格须有 strategy_key（或给 default_strategy_key）")
        pairs.append(ContrastPair(
            human_text=spec["human_text"], ai_text=spec["ai_text"],
            scene_keys=set(spec.get("scene_keys") or ()),
            strategy_key=strategy_key,
            span_start=int(spec["span_start"]), span_end=int(spec["span_end"]),
            human_sha256=spec.get("human_sha256") or "",
            meta={"segment_id": spec.get("segment_id"),
                  "text_version": spec.get("text_version"),
                  **(spec.get("meta") or {})},
        ))
    return pairs


def load_pairs_file(path: str) -> list[ContrastPair]:
    """读取调用方注入的成对片段 JSON：{strategy_key?, pairs: [...]}。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return build_pairs(raw.get("pairs") or [],
                       default_strategy_key=raw.get("strategy_key"))


def summarize(pairs: list[ContrastPair]) -> dict:
    """通过/拒绝计数 + 拒绝理由分类（冒号前的门+形态即类目）+ 拒绝样本。"""
    passed, reason_classes, samples = 0, {}, []
    for p in pairs:
        ok, reasons = gate_pair(p)
        if ok:
            passed += 1
            continue
        for r in reasons:
            cls = r.split(":", 1)[0]
            reason_classes[cls] = reason_classes.get(cls, 0) + 1
        if len(samples) < 5:
            samples.append({"strategy_key": p.strategy_key,
                            "reasons": reasons})
    return {"n_pairs": len(pairs), "passed": passed,
            "rejected": len(pairs) - passed,
            "reject_reason_classes": reason_classes,
            "reject_samples": samples}


# ------------------------------------------------------------- 落库
def resolve_strategy(s, strategy_key: str):
    """既有 expression_strategies_v2 表里按 key 取最新可用版本
    （hypothesis/active）；没有 → None（跳过，不自动建卡）。
    同版本并列时以 id 收尾：两次 run 必须解到**同一行**，否则幂等键
    (strategy_id, version, sha) 在跨会话重跑时会漂到别的行上、重复落库。"""
    from app.models import ExpressionStrategyV2
    return (s.query(ExpressionStrategyV2)
            .filter(ExpressionStrategyV2.strategy_key == strategy_key,
                    ExpressionStrategyV2.status.in_(("hypothesis", "active")))
            .order_by(ExpressionStrategyV2.version.desc(),
                      ExpressionStrategyV2.id.desc())
            .first())


def _persist_one(s, pair: ContrastPair, *, extractor_model: str) -> tuple[str, str]:
    """单对落库（门已过的对才进这里）。返回 (结果, 理由)：
    written / skip_no_strategy / skip_no_segment / skip_no_text_version /
    skip_span_mismatch / skip_dup_sha。"""
    from app.knowledge import verify_instance_span
    from app.models import Segment, StrategyInstance

    st = resolve_strategy(s, pair.strategy_key)
    if st is None:
        return "skip_no_strategy", f"库内无可用策略卡 {pair.strategy_key}"
    seg_id = (pair.meta or {}).get("segment_id")
    if not seg_id:
        return "skip_no_segment", "meta 缺 segment_id（人类侧须对登记段可核）"
    seg = s.query(Segment).filter_by(id=seg_id).first()
    if seg is None:
        return "skip_no_segment", f"段 {seg_id} 不存在"
    text_version = (pair.meta or {}).get("text_version") or ""
    if not text_version:
        return "skip_no_text_version", "meta 缺 text_version（K1-A 来源契约）"
    # 既有门禁照用：human 侧 span 必须对登记段逐字可核——核不上不落
    if not verify_instance_span(seg.text_clean, pair.span_start,
                                pair.span_end, pair.human_text):
        return "skip_span_mismatch", (
            f"span [{pair.span_start},{pair.span_end}) 与段 {seg_id} 原文不符")
    dup = (s.query(StrategyInstance)
           .filter(StrategyInstance.strategy_id == st.id,
                   StrategyInstance.strategy_version == st.version,
                   StrategyInstance.evidence_sha256 == pair.human_sha256)
           .first())
    if dup is not None:
        return "skip_dup_sha", "同 (策略, 版本, human_sha256) 已落行——幂等跳过"
    s.add(StrategyInstance(
        strategy_id=st.id, strategy_version=st.version, work_id=seg.work_id,
        segment_id=seg.id, text_version=text_version,
        span_start=pair.span_start, span_end=pair.span_end,
        evidence_text=pair.human_text, evidence_sha256=pair.human_sha256,
        conditions_observed={
            "protocol": PROTOCOL,
            "strategy_key": pair.strategy_key,
            "ai_side_sha256": _sha256(pair.ai_text),
            "ai_side_chars": len(pair.ai_text),
            "scene_keys": sorted(pair.scene_keys),
            "gates": {name: "pass" for name, _ in GATES},
        },
        observed_content=f"{PROTOCOL}:{pair.strategy_key}",
        extractor_model=extractor_model,
        status="proposed"))           # 状态纪律：判定≠升格，落库即 proposed
    return "written", ""


def run_contrast(s, pairs: list[ContrastPair], *, live: bool,
                 extractor_model: str = PROTOCOL) -> dict:
    """一次 run：三道门 →（非 live 即回，零库写）→ 过门对逐条落库。

    live 只表示"允许写库"（CLI --live 已过环境变量双闸才到这）；
    本模块自身不联网、不生成 AI 侧——pairs 全部来自调用方注入。"""
    rep = {"mode": "live" if live else "dry_run",
           **summarize(pairs), "written": 0, "skipped": {}}
    if not live:
        return rep                    # dry-run：零库写承诺
    skipped: dict[str, int] = {}
    for p in pairs:
        ok, _reasons = gate_pair(p)
        if not ok:
            continue                  # 破形对不落库（拒绝样本已在 summarize 里）
        outcome, _why = _persist_one(s, p, extractor_model=extractor_model)
        if outcome == "written":
            rep["written"] += 1
        else:
            skipped[outcome] = skipped.get(outcome, 0) + 1
    s.commit()
    rep["skipped"] = skipped
    return rep


# --------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(
        description="K2 成对对照抽取器（paired_contrast_v2：三道机械门+落库）")
    ap.add_argument("--dry-run", dest="mode", action="store_const",
                    const="dry_run", help="预演（默认）：零库写，只出统计与拒绝样本")
    ap.add_argument("--live", dest="mode", action="store_const", const="live",
                    help="真落库：需环境变量 K2CONTRAST_ALLOW_LIVE=1")
    ap.set_defaults(mode="dry_run")
    ap.add_argument("--pairs-file", default="",
                    help="成对片段 JSON（AI 侧由调用方注入；--live 必填）")
    ap.add_argument("--extractor-model", default=PROTOCOL)
    a = ap.parse_args()
    live = a.mode == "live"

    if live and os.environ.get("K2CONTRAST_ALLOW_LIVE") != "1":
        raise SystemExit(
            "--live 需要环境变量 K2CONTRAST_ALLOW_LIVE=1"
            "（fail-closed：未拍板不落库）")
    if live and not a.pairs_file:
        raise SystemExit(
            "--live 需要 --pairs-file（AI 侧片段由调用方注入，本工具不联网生成）")

    pairs = load_pairs_file(a.pairs_file) if a.pairs_file else []
    if not live:
        print(json.dumps({"mode": "dry_run", **summarize(pairs)},
                         ensure_ascii=False, indent=1))
        return
    # 既有 R6 互斥纪律照用（与 k2_extract_backfill 同口径）：写库前持锁，
    # 锁被持有（live 实跑 / pytest 整轮）即 SystemExit，不排队、不重叠。
    from app import db
    from app.live_guard import live_lock
    with live_lock("k2_contrast_extract"):
        db.init_db()
        with db.session() as s:
            rep = run_contrast(s, pairs, live=True,
                               extractor_model=a.extractor_model)
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
