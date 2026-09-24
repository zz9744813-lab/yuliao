"""K2 成对片段生成器（K2 最后一道闸：成对对照重抽，2026-09-24 派工）。

抽取器 scripts/k2_contrast_extract.py 要求调用方**注入 AI 侧片段**（工具
不联网生成）——本脚本即那一环：人类侧从库里 segments 表**现成分段**取
（不编造人类文本），AI 侧调 writer 通道对该片段施加该策略的**反面行为**
（AI 化），产出能被 `k2_contrast_extract.py --pairs-file` 直接消费的
`{"pairs": [...]}`。

口径（从 k2_contrast_extract 单源继承，不臆造）：
- op 集合 / 标签映射 / 策略键：import 该模块的 OPS / label_of /
  LABEL_STRATEGY；每对 strategy_key 由 label_of(op) 唯一决定；
- **来源合规门（2026-09-24 派工「来源门」）**：人类侧逐段查 work_sources，
  只收合规人类语料——口径与 K2 试点通道/K3 证据侧**同源**：
  `source_type ∈ {human_fiction} ∪ 前缀 production_nonbenchmark_`
  （import 自 scripts/k2_extract_backfill，唯一判定入口
  nonbenchmark_compliant_source，不自创第二套口径），且
  `text_version ∈ DEFAULT_ALLOWED_TEXT_VERSIONS`（import 自
  app/knowledge_query，与 K3 证据侧同源）；fixture/synthetic/commentary
  与无登记行一律排除。被排除来源逐来源留痕（work_id/source_type/
  text_version/reason/n_segments 写进返回结构 skips.excluded_sources 与
  CLI 输出，不静默丢）——K3 硬排 fixture/synthetic/commentary，不合规
  来源的对即使过门也永远成不了 K3 可用证据，取它们纯属浪费。
- 人类侧段筛选**对着六道门反向设计**：命中任一信号词表（解释/心理/节拍/
  修饰/门1 双侧词表）的段不入池——人类原文不得自带改动信号，否则不构成
  对照；长度窗 [80, 350]（门3 比值 [1.2,6.0] × writer 指令 1.5~2.5 倍）；
  只取 role≠benchmark 的生产段（基准段是评测宇宙，不作重抽原料）；
- scene_keys 机械派生：段内出现 ≥2 次、不含虚词字符（k2c.FUNCTION_CHARS）
  的高频 2~3 字连续汉字串，确定性取前 3；writer 指令要求保留全部指称；
- AI 侧 writer 指令按 op 定向（_DIRECTIVES）：S1=**重写**原文并插入解释/
  心理句（禁节拍/修饰词——门0 混合检查不许上升；禁照抄原文——门4）；
  S2=保留全部人物事件、拆拍/堆修饰（禁解释/心理新句——门0 混合检查）；
- **幂等/续跑**：每对缓存 <cache>/<sha(op|segment|human|model)>.json，
  重跑缓存命中即不调 writer；
- writer 挂 / 空响应 / finish≠stop（截断）：**如实报错退出（非零）**，
  绝不伪造 AI 侧；
- 密钥：运行时从 F:/Hermes/secrets/litellm_master_key.txt 读（env
  K2_PAIRS_WRITER_KEY 可覆盖），不写入任何交付文件/账本/缓存；
- 不动任何既有表结构：对库只 SELECT。

用法（真实通道参数见 docs/K2_重抽_成对对照_20260924.md）：
    python scripts/k2_pairs_gen.py --n-per-op 12 \
        --on expression_strategies_v2 \
        --writer-base http://127.0.0.1:4000/v1 --writer-model mc22-flash \
        --out <path>/k2_pairs_20260924.json --ledger <path>/k2pairs_ledger.jsonl

    # 零调用预演（不调 writer、不读密钥、不写 --out）：逐来源合格/排除明细
    python scripts/k2_pairs_gen.py --dry-run --n-per-op 12 --out <path>/x.json
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util as _u
import json
import os
import sys
from pathlib import Path

from sqlalchemy import func                           # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k2c", ROOT / "scripts" / "k2_contrast_extract.py")
k2c = _u.module_from_spec(_spec)
# dataclass 回查 sys.modules（同 tests/test_k2_contrast_extract.py 的教训）：
sys.modules["k2c"] = k2c
_spec.loader.exec_module(k2c)

from app import db                                    # noqa: E402
from app.models import Segment, Work, WorkSource      # noqa: E402
# 来源合规口径**单源**：白名单常量与判定函数直接 import 自
# scripts/k2_extract_backfill.py（K2 试点通道口径），text_version 白名单
# 直接 import 自 app/knowledge_query.py（K3 证据侧 DEFAULT_ALLOWED_TEXT_VERSIONS）
# ——不自创第二套口径；tests 钉住「字面量漂移即红」。
from k2_extract_backfill import (                     # noqa: E402
    NONBENCHMARK_SOURCE_TYPES, NONBENCHMARK_SOURCE_TYPE_PREFIX,
    nonbenchmark_compliant_source)
from app.knowledge_query import DEFAULT_ALLOWED_TEXT_VERSIONS  # noqa: E402

KEY_FILE = Path("F:/Hermes/secrets/litellm_master_key.txt")

# 人类侧禁词池＝六道门全部信号词表的并集：任何命中即不入池（人类原文
# 必须是「无信号」基线，两侧才构成对照）。全部取自 k2c 常量，不本地重定义。
HUMAN_FORBIDDEN = tuple(sorted(set(
    k2c.INTERPRET_MARKERS + k2c.PSYCH_MARKERS + k2c.BEAT_MARKERS
    + k2c.MODIFIER_MARKERS
    + tuple(k2c.FEATURE_WORDS[k2c.S1_KEY])
    + tuple(k2c.FEATURE_WORDS[k2c.S2_KEY]))))

SEG_MIN_CHARS, SEG_MAX_CHARS = 80, 350
DEFAULT_SCAN = 20_000     # 每 op 最多扫描段数（43.8 万行库上的确定性上限）

# 来源合规门的排除理由码（skips.excluded_sources[].reason，可核对）：
REASON_NO_ROW = "no_work_source_row"                  # 无登记行
REASON_BAD_TYPE = "source_type_not_compliant"         # fixture/synthetic/commentary 等
REASON_BAD_TEXT_VERSION = "text_version_not_allowed"  # 空/ test-fixture 等白名单外

# text_version 白名单**单源**继承 K3 证据侧（app/knowledge_query），不本地重定义
ALLOWED_TEXT_VERSIONS = DEFAULT_ALLOWED_TEXT_VERSIONS

# 按 op 定向的 writer 指令（对着门 0/1/3/4/5 断言逐条反推）：
# S1（add_interpretation / add_psych_narration）——重写并插入「解释/心理」
# 句：句数必增、命中句必须新增且锚定（她/他/它/自己 或指称）、禁节拍/修饰
# 词（门0 混合检查不许上升）、禁照抄原文（门4）、禁对方词表（门5）。
# S2（split_beats / dilute_modifiers）——保留命题（Jaccard≥0.3）与全部
# 指称，节拍/修饰标记数必须上升；禁解释/心理新增句（门0 混合）。
_S1_ADD_INSTR = (
    "把下面这段小说**完整重写**成爱解释的AI腔：保留原有人物、事件与对话内容，"
    "但在行文中插入2-3句解释性陈述，用「其实」「因为」「意味着」「说到底」"
    "「换句话说」「说白了」这类词把人物的言外之意、动机和暗含关系写明白"
    "（新增句里要有「她/他/自己」回指）。要求：句数明显多于原文，总长度约为"
    "原文的1.5~2.5倍；不得以原文逐字开头；禁止使用这些词："
    "忽然、就在、接着、然后、慢慢、缓缓、渐渐、一连、一步一步、仿佛、像是、"
    "似乎、宛如、轻轻、淡淡、一般；禁止心理旁白词（心里、心想、暗想、暗自、"
    "寻思、琢磨、心头、心底、暗暗、默默）。只输出改写后的正文。")
_S1_PSYCH_INSTR = (
    "把下面这段小说**完整重写**成爱补心理解说的AI腔：保留原有人物、事件与"
    "对话内容，插入2-3句内心活动旁白，其中**至少一句必须用「她心里明白……」"
    "或「他心里明白……」的句式**，其余可用「心想」「暗想」「暗自」「寻思」"
    "「琢磨」写出人物内心（句中带「她/他/自己」回指）。要求：句数明显多于"
    "原文，总长度约为原文的1.5~2.5倍；不得以原文逐字开头；"
    "禁止使用：忽然、就在、接着、然后、慢慢、缓缓、渐渐、一连、一步一步、"
    "仿佛、像是、似乎、宛如、轻轻、淡淡、一般；禁止解释词（因为、其实、"
    "意味着、说到底、换句话说、说白了、归根结底、之所以）。只输出改写后的"
    "正文。")
_S2_BEAT_INSTR = (
    "把下面这段小说改写成**注水拆拍**的AI腔：必须保留全部人物名、地点、"
    "事件与信息内容（不得增删情节），把原有动作和对话用「忽然」「就在」"
    "「接着」「然后」「慢慢」「缓缓」「渐渐」这类词拆成连续多拍，节拍明显"
    "多于原文；总长度约为原文的1.5~2.5倍。禁止出现解释词（因为、其实、"
    "意味着、说到底、换句话说、说白了）和心理旁白词（心里、心想、暗想、"
    "寻思、琢磨、暗自）。只输出改写后的正文。")
_S2_MOD_INSTR = (
    "把下面这段小说改写成**修饰堆叠**的AI腔：必须保留全部人物名、地点、"
    "事件与信息内容（不得增删情节），插入比喻与情态修饰（「仿佛」「像是」"
    "「似乎」「宛如」「轻轻」「淡淡」），让修饰密度明显高于原文；总长度"
    "约为原文的1.5~2.5倍。禁止出现解释词（因为、其实、意味着、说到底、"
    "换句话说、说白了）和心理旁白词（心里、心想、暗想、寻思、琢磨、"
    "暗自）。只输出改写后的正文。")
_DIRECTIVES = {k2c.OP_ADD_INTERPRETATION: _S1_ADD_INSTR,
               k2c.OP_ADD_PSYCH_NARRATION: _S1_PSYCH_INSTR,
               k2c.OP_SPLIT_BEATS: _S2_BEAT_INSTR,
               k2c.OP_DILUTE_MODIFIERS: _S2_MOD_INSTR}


def derive_scene_keys(text: str, k: int = 3) -> list[str]:
    """机械派生场景指称：出现 ≥2 次的高频 2~3 字连续汉字串（每个字符都
    不在虚词集 k2c.FUNCTION_CHARS 里），按 (次数, 串) 确定性排序取前 k。"""
    from collections import Counter
    cnt: Counter[str] = Counter()
    for n in (2, 3):
        for i in range(len(text) - n + 1):
            g = text[i:i + n]
            if all(("\u4e00" <= ch <= "\u9fa5") and ch not in k2c.FUNCTION_CHARS
                   for ch in g):
                cnt[g] += 1
    cands = [g for g, c in cnt.items() if c >= 2]
    # 出现≥2次的串里优先长串（3字名/地点更specific），再按 (总次数, 串) 排序
    cands.sort(key=lambda g: (-len(g), -cnt[g], g))
    return cands[:k]


def _clean(seg_text: str) -> str:
    t = (seg_text or "").strip()
    return t


def _source_verdict(s, wid: str, cache: dict) -> tuple:
    """逐来源合规判定（每 work_id 只查一次 work_sources）。
    返回 (ok, reason, source_type, text_version)；口径唯一入口：
    nonbenchmark_compliant_source（K2 试点通道同源）+
    DEFAULT_ALLOWED_TEXT_VERSIONS（K3 证据侧同源）。"""
    if wid in cache:
        return cache[wid]
    reg = s.query(WorkSource).filter_by(work_id=wid).first()
    if reg is None:
        v = (False, REASON_NO_ROW, "", "")
    elif not nonbenchmark_compliant_source(reg.source_type):
        v = (False, REASON_BAD_TYPE, reg.source_type or "",
             reg.text_version or "")
    elif (reg.text_version or "") not in ALLOWED_TEXT_VERSIONS:
        v = (False, REASON_BAD_TEXT_VERSION, reg.source_type or "",
             reg.text_version or "")
    else:
        v = (True, "", reg.source_type or "", reg.text_version or "")
    cache[wid] = v
    return v


def _trace_bump(trace: dict, wid: str, verdict: tuple, *,
                pool_eligible: bool = False) -> None:
    ok, reason, st, tv = verdict
    e = trace.get(wid)
    if e is None:
        e = trace[wid] = {"work_id": wid, "source_type": st,
                          "text_version": tv, "reason": "",
                          "n_segments_scanned": 0, "n_pool_eligible": 0}
    e["n_segments_scanned"] += 1
    if not ok:
        e["reason"] = reason          # 同一 work 判定恒定
    elif pool_eligible:
        e["n_pool_eligible"] += 1


def human_pool(s, *, n: int, scan_limit: int):
    """确定性人类侧段池：**来源合规门**（work_sources 逐段查，不合规即
    跳过并留痕）+ role≠benchmark、text_clean 在长度窗内、六门信号词全零
    命中、可派生非空 scene_keys。按 (work_id, ordinal) 序扫描取前 n。
    返回 (pool, trace)——trace 为逐来源明细 dict[work_id → 计数字段]，
    排除/合格都不静默丢。"""
    out: list[dict] = []
    trace: dict[str, dict] = {}
    verdict_cache: dict[str, tuple] = {}
    rows = (s.query(Segment.id, Segment.work_id, Segment.ordinal,
                    Segment.text_clean, Segment.role)
            .filter(Segment.text_clean.isnot(None))
            .order_by(Segment.work_id, Segment.ordinal).all())
    scanned = 0
    for sid, wid, ordinal, tclean, role in rows:
        if len(out) >= n:
            break
        scanned += 1
        if scanned > scan_limit:
            break
        ok, reason, st, tv = verdict = _source_verdict(s, wid, verdict_cache)
        if not ok:
            _trace_bump(trace, wid, verdict)      # 来源不合规：留痕即跳
            continue
        t = _clean(tclean)
        if role == "benchmark":
            continue
        if not (SEG_MIN_CHARS <= len(t) <= SEG_MAX_CHARS):
            continue
        if any(w in t for w in HUMAN_FORBIDDEN):
            continue
        keys = derive_scene_keys(t)
        if not keys:
            continue
        _trace_bump(trace, wid, verdict, pool_eligible=True)
        out.append({"segment_id": sid, "work_id": wid, "text": t,
                    "text_version": tv, "scene_keys": keys})
    return out, trace


def trace_report(trace: dict) -> dict:
    """逐来源留痕 → 可核对的排除/合格明细（按 work_id 确定性排序）。
    excluded_sources 条目形态即任务书规定的
    {work_id, source_type, text_version, reason, n_segments}。"""
    entries = sorted(trace.values(), key=lambda e: e["work_id"])
    return {
        "excluded_sources": [
            {"work_id": e["work_id"], "source_type": e["source_type"],
             "text_version": e["text_version"], "reason": e["reason"],
             "n_segments": e["n_segments_scanned"]}
            for e in entries if e["reason"]],
        "eligible_sources": [
            {"work_id": e["work_id"], "source_type": e["source_type"],
             "text_version": e["text_version"],
             "n_segments_scanned": e["n_segments_scanned"],
             "n_pool_eligible": e["n_pool_eligible"]}
            for e in entries if not e["reason"]],
    }


def read_writer_key() -> str:
    key = os.environ.get("K2_PAIRS_WRITER_KEY", "")
    if key:
        return key.strip()
    if not KEY_FILE.exists():
        raise SystemExit(f"[k2_pairs_gen] 密钥不可得：env K2_PAIRS_WRITER_KEY "
                         f"未设且密钥文件不存在——如实失败，不伪造")
    return KEY_FILE.read_text(encoding="utf-8").strip()


def call_writer(base: str, model: str, key: str, instruction: str,
                human: str, *, timeout: float = 120.0,
                _post=None) -> str:
    """一次 writer 调用（OpenAI 兼容 /chat/completions）。空响应 / 非 200 /
    finish≠stop（截断）→ 如实 SystemExit，绝不伪造。
    _post：注入点（测试用假 post(url, headers=…, json=…, timeout=…) →
    response-like；生产为 None 走 httpx）。"""
    if _post is None:
        import httpx
        try:
            r = httpx.post(base.rstrip("/") + "/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": model,
                                 "messages": [{"role": "system",
                                               "content": instruction},
                                              {"role": "user",
                                               "content": human}],
                                 "max_tokens": 2000, "temperature": 0.8},
                           timeout=timeout)
        except Exception as exc:                     # noqa: BLE001
            raise SystemExit(f"[k2_pairs_gen] writer 不可达（{base}）："
                             f"{type(exc).__name__}: {exc}——如实报错，"
                             "不伪造 AI 侧数据") from exc
    else:
        r = _post(base.rstrip("/") + "/chat/completions",
                  headers={"Authorization": f"Bearer {key}"},
                  json={"model": model,
                        "messages": [{"role": "system",
                                      "content": instruction},
                                     {"role": "user",
                                      "content": human}],
                        "max_tokens": 2000, "temperature": 0.8},
                  timeout=timeout)
    if r.status_code != 200:
        raise SystemExit(f"[k2_pairs_gen] writer HTTP {r.status_code}"
                         f"（{base}）：{r.text[:200]}——如实报错退出")
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    if not isinstance(text, str) or not text.strip():
        raise SystemExit("[k2_pairs_gen] writer 返回空 content"
                         f"（finish={choice.get('finish_reason')}）——"
                         "如实报错，不伪造 AI 侧数据")
    if choice.get("finish_reason") != "stop":
        raise SystemExit(f"[k2_pairs_gen] writer 未正常收尾"
                         f"（finish={choice.get('finish_reason')}，"
                         "截断输出不可用）——如实报错退出")
    return text.strip()


def _cache_key(op: str, seg_id: str, human_sha: str, model: str,
               instruction: str) -> str:
    # 指令哈希必须入键：改指令而不换键会静默复用旧输出（幂等漏洞，
    # 2026-09-24 首轮实测后修复——psych 指令调整时旧缓存会把劣品顶回来）
    return hashlib.sha256(
        f"{op}|{seg_id}|{human_sha}|{model}|"
        f"{hashlib.sha256(instruction.encode('utf-8')).hexdigest()}"
        .encode("utf-8")).hexdigest()


def generate(s, *, n_per_op: int, writer, model: str, cache_dir: Path,
             ledger: Path | None, scan_limit: int = DEFAULT_SCAN) -> dict:
    """主流程：每 op 选 n 段人类侧 → 缓存或调 writer 生成 AI 侧 → 组对。
    writer=callable(instruction, human) -> ai_text（生产传 call_writer 封装，
    测试注入假 writer——不联网）。幂等：缓存命中不调 writer。"""
    pairs, events = [], []
    merged_trace: dict[str, dict] = {}
    for op in k2c.OPS:
        pool, trace = human_pool(s, n=n_per_op, scan_limit=scan_limit)
        # 扫描对每 op 确定性重复（同库同序），逐来源明细各来源取首份
        for wid, e in trace.items():
            merged_trace.setdefault(wid, dict(e))
        if len(pool) < n_per_op:
            excl = trace_report(merged_trace)["excluded_sources"]
            detail = ""
            if excl:
                head = "; ".join(f"{e['work_id']}={e['reason']}"
                                 for e in excl[:10])
                detail = f"：{head}{'等' if len(excl) > 10 else ''}"
            raise SystemExit(
                f"[k2_pairs_gen] 人类侧合规段池不足：op={op} 需要 "
                f"{n_per_op}，实得 {len(pool)}（扫描上限 {scan_limit}，"
                f"来源合规门排除 {len(excl)} 个不合规来源{detail}；"
                "禁词过滤后无信号段不足）——如实失败，不凑数、"
                "不降级到不合规来源")
        for item in pool:
            human = item["text"]
            h_sha = hashlib.sha256(human.encode("utf-8")).hexdigest()
            ck = _cache_key(op, item["segment_id"], h_sha, model,
                             _DIRECTIVES[op])
            cpath = cache_dir / f"{ck}.json"
            if cpath.exists():
                ai = json.loads(cpath.read_text(encoding="utf-8"))["ai_text"]
                events.append({"event": "cache_hit", "op": op,
                               "segment_id": item["segment_id"],
                               "cache": str(cpath)})
            else:
                ai = writer(_DIRECTIVES[op], human)
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_text(json.dumps(
                    {"ai_text": ai, "op": op, "writer_model": model,
                     "segment_id": item["segment_id"],
                     "human_sha256": h_sha,
                     "created_at": datetime.datetime.now().isoformat(
                         timespec="seconds")}, ensure_ascii=False),
                    encoding="utf-8")
                events.append({"event": "writer_call", "op": op,
                               "segment_id": item["segment_id"],
                               "writer_model": model,
                               "n_chars_ai": len(ai)})
            pairs.append({
                "op": op,
                "strategy_key": k2c.LABEL_STRATEGY[k2c.label_of(op)],
                "human_text": human, "ai_text": ai,
                "scene_keys": item["scene_keys"],
                "span_start": 0, "span_end": len(human),
                "human_sha256": h_sha,
                "segment_id": item["segment_id"],
                "text_version": item["text_version"],
                "meta": {"work_id": item["work_id"],
                         "generator": "k2_pairs_gen",
                         "writer_model": model},
            })
    if ledger is not None:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as f:
            for e in events:
                e["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    rep = trace_report(merged_trace)
    return {"n_pairs": len(pairs),
            "n_writer_calls": sum(1 for e in events
                                  if e["event"] == "writer_call"),
            "n_cache_hits": sum(1 for e in events
                                if e["event"] == "cache_hit"),
            "by_op": {op: sum(1 for p in pairs if p["op"] == op)
                      for op in k2c.OPS},
            "skips": {"excluded_sources": rep["excluded_sources"]},
            "eligible_sources": rep["eligible_sources"],
            "pairs": pairs}


def source_census(s) -> list[dict]:
    """逐 work_source 登记行的来源普查（dry-run 诊断用，只 SELECT）：
    合规来源里 text_clean 全空的（如尚未跑清洗的试点源）在段池查询中
    零行出现——不查这一层就看不见「合格却缺席」。"""
    out = []
    for reg in s.query(WorkSource).order_by(WorkSource.work_id).all():
        ok = (nonbenchmark_compliant_source(reg.source_type)
              and (reg.text_version or "") in ALLOWED_TEXT_VERSIONS)
        n_seg = s.query(func.count(Segment.id)).filter(
            Segment.work_id == reg.work_id).scalar()
        n_clean = s.query(func.count(Segment.id)).filter(
            Segment.work_id == reg.work_id,
            Segment.text_clean.isnot(None)).scalar()
        out.append({"work_id": reg.work_id,
                    "source_type": reg.source_type or "",
                    "text_version": reg.text_version or "",
                    "compliant": bool(ok),
                    "n_segments": int(n_seg or 0),
                    "n_text_clean": int(n_clean or 0)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n-per-op", type=int, default=12, dest="n_per_op")
    ap.add_argument("--on", default="expression_strategies_v2",
                    help="策略来源表名（当前协议仅 expression_strategies_v2）")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="零调用预演：不调 writer、不读密钥、不写 --out，"
                         "只打印逐来源合格/排除明细与人类侧池容量")
    ap.add_argument("--writer-base", default="", dest="writer_base")
    ap.add_argument("--writer-model", default="", dest="writer_model")
    ap.add_argument("--out", required=True, help="输出对 JSON（已存在即拒）")
    ap.add_argument("--ledger", default="", help="旁路账本 JSONL（追加写）")
    ap.add_argument("--cache-dir", default=str(ROOT / "_pairs" / "cache"),
                    dest="cache_dir")
    ap.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN,
                    dest="scan_limit")
    a = ap.parse_args()
    if a.on != "expression_strategies_v2":
        raise SystemExit(f"--on 只支持 expression_strategies_v2（得到 "
                         f"{a.on}）——当前协议的唯一策略来源")
    if a.n_per_op < 1:
        raise SystemExit("--n-per-op 须 ≥1")
    if a.dry_run:
        # 零调用路径：无 writer 参数、无密钥、不落对文件——来源门明细照出
        db.init_db()
        with db.session() as s:
            pool, trace = human_pool(s, n=a.n_per_op, scan_limit=a.scan_limit)
            rep = trace_report(trace)
            by_work: dict[str, int] = {}
            for item in pool:
                by_work[item["work_id"]] = by_work.get(item["work_id"], 0) + 1
            print(json.dumps(
                {"dry_run": True, "n_per_op": a.n_per_op,
                 "n_ops": len(k2c.OPS),
                 "pool_size_per_op": len(pool),
                 "pool_sufficient": len(pool) >= a.n_per_op,
                 "pool_by_work": dict(sorted(by_work.items())),
                 "skips": rep["excluded_sources"],
                 "eligible_sources": rep["eligible_sources"],
                 "source_census": source_census(s)},
                ensure_ascii=False, indent=1))
        return
    if not (a.writer_base and a.writer_model):
        raise SystemExit("须给 --writer-base 与 --writer-model（AI 侧生成"
                         "走真实 writer 通道；离线路径仅供测试注入）")
    out_path = Path(a.out)
    if out_path.exists():
        raise SystemExit(f"--out 已存在：{a.out}——对文件不覆盖，重跑先换名")
    key = read_writer_key()
    ledger = Path(a.ledger) if a.ledger else None
    db.init_db()
    with db.session() as s:
        result = generate(s, n_per_op=a.n_per_op,
                          writer=lambda ins, hum: call_writer(
                              a.writer_base, a.writer_model, key, ins, hum),
                          model=a.writer_model, cache_dir=Path(a.cache_dir),
                          ledger=ledger, scan_limit=a.scan_limit)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(
        {"n_pairs": result["n_pairs"], "by_op": result["by_op"],
         "skips": result["skips"],
         "eligible_sources": result["eligible_sources"],
         "pairs": result["pairs"]}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(json.dumps({"n_pairs": result["n_pairs"],
                      "n_writer_calls": result["n_writer_calls"],
                      "n_cache_hits": result["n_cache_hits"],
                      "by_op": result["by_op"],
                      "skips": result["skips"],
                      "eligible_sources": result["eligible_sources"],
                      "out": str(out_path),
                      "ledger": str(ledger) if ledger else ""},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
