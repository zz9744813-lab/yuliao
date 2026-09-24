"""K2 成对片段生成器回归（scripts/k2_pairs_gen.py，纯离线、假 writer）。

钉住的事（派工任务书 2 的五点）：
① 生成记录 schema 合法且 op 全覆盖（4 op 各至少一对，字段齐、
   strategy_key 与 span/sha/scene_keys 机械一致）；
② 每个 op 的标签由 k2_contrast_extract.label_of 唯一决定（本模块不另设
   映射——单源继承，任何漂移即红）；
③ 缓存/续跑幂等：重跑 0 次 writer 调用、缓存全命中、产物逐字相同；
④ happy path 生成的对能过抽取器**门0**（且本夹具实际过全部六道门——
   更强的口径，不放宽任何门）；
⑤ writer 不可达时如实报错退出（SystemExit 非零），绝不伪造 AI 侧数据。

来源合规门增钉（派工任务书「来源门」2026-09-24，同样全离线假 writer）：
⑥ fixture/synthetic/commentary/无登记行四种不合规来源全部被排除，
   excluded_sources 留痕带可核对 reason；
⑦ 合规来源（human_fiction 与 production_nonbenchmark_*）正常进池；
⑧ text_version 不在 K3 白名单（test-fixture / 空值）被排除；
⑨ 合规常量与 scripts/k2_extract_backfill.py 源码字面量单源一致，漂移即红；
⑩ 合规段池不足 n 时 fail-closed（SystemExit），绝不回退到不合规来源。

纪律：测试从不联网、从不执行 CLI --live；人类侧文本来自种子段（模拟库内
现成分段），AI 侧来自假 writer（不编造"人类文本"、不伪造 writer 故障行为）。
"""
from __future__ import annotations

import hashlib
import importlib.util as _u
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

# 与 tests/test_k2_contrast_extract.py 相同的模块加载序（dataclass 回查
# sys.modules 的坑），先登记再 exec。
_spec_g = _u.spec_from_file_location(
    "k2g", ROOT / "scripts" / "k2_pairs_gen.py")
k2g = _u.module_from_spec(_spec_g)
sys.modules["k2g"] = k2g
_spec_g.loader.exec_module(k2g)

_spec_c = _u.spec_from_file_location(
    "k2c", ROOT / "scripts" / "k2_contrast_extract.py")
k2c = _u.module_from_spec(_spec_c)
sys.modules["k2c"] = k2c
_spec_c.loader.exec_module(k2c)

from app import db                                        # noqa: E402
from app.models import Segment, Work, WorkSource          # noqa: E402
from registry_anchor import anchor as _anchor             # noqa: E402  登记行内容锚同源

# 种子人类侧段：≥80 字（长度窗）、六门信号词全零命中（无信号基线）、
# 「林昭」出现 3 次（scene_keys 可机械派生）。
HUMAN_TEXT = ("林昭把伞收在门边，往灶上添了把柴。林昭坐回桌前，翻着那本"
              "旧账，一页一页看得很慢。屋外的雨声小了下去，林昭听见院里"
              "有人踩水过来，脚步停在门口，门环响了一声，又停了。")
# 假 writer 的四个 op 答文（对着 k2c 六道门断言手工构造的合法形态：
# S1=重写+新增命中句且锚定/无节拍修饰词/不含原文全文；S2=保命题+标记数
# 上升/无解释心理新句）：
AI_ADD_INTERPRETATION = (
    "林昭把伞收在门边，又往灶膛里添了一把柴。其实她心里清楚，这账再翻也"
    "翻不出新的名堂，因为父亲当年记下的每一个数目都不肯多写半个字。林昭"
    "坐回桌前，还是把那本旧账摊开了，她明白，翻下去是因为要给母亲一个"
    "交代。屋外的雨声小了下去，院里有人踩水过来，脚步停在门口——说到底，"
    "她知道，来的人总是为同一样东西。")
AI_ADD_PSYCH_NARRATION = (
    "林昭把伞收在门边，往灶上添了把柴。林昭坐回桌前，翻着那本旧账。她"
    "心里明白，这账翻到天亮也翻不出名堂，她心里想，父亲留下的数目一个都"
    "不能少，她暗自记下了每一页的数字。屋外的雨声小了下去，院里有人踩"
    "水过来，她寻思着，脚步停在门口的人，多半又是来讨那笔旧债的。")
AI_SPLIT_BEATS = (
    "林昭把伞收在门边，往灶上添了把柴。接着她坐回桌前，翻着那本旧账，"
    "一页一页看得很慢。屋外的雨声渐渐小了下去，林昭听见院里有人踩水"
    "过来，然后脚步停在门口，门环响了一声，又停了。她又把门外的动静"
    "听了一遍，才把账本合上。")
AI_DILUTE_MODIFIERS = (
    "林昭把伞收在门边，往灶上添了把柴，火光轻轻晃了晃。林昭坐回桌前，"
    "翻着那本旧账，一页一页看得很慢，纸页像是旧年的水面。屋外的雨声"
    "缓缓停了，院里有人踩水过来，脚步停在门口，门环响了一声，又停了，"
    "仿佛谁在门外犹豫。")


def _fake_writer(instruction: str, human: str) -> str:
    if "解释性陈述" in instruction:
        return AI_ADD_INTERPRETATION
    if "内心活动旁白" in instruction:
        return AI_ADD_PSYCH_NARRATION
    if "注水拆拍" in instruction:
        return AI_SPLIT_BEATS
    if "修饰堆叠" in instruction:
        return AI_DILUTE_MODIFIERS
    raise AssertionError(f"未知指令形态：{instruction[:40]}")


@pytest.fixture()
def seeded(tmp_path):
    db.init_db()
    with db.session() as s:
        w = Work(title="t-k2pairs", source="test:k2pairs")
        s.add(w)
        s.flush()
        for i in range(6):
            s.add(Segment(work_id=w.id, ordinal=i, text=HUMAN_TEXT,
                          text_clean=HUMAN_TEXT, role=None,
                          n_sentences=3, n_chars=len(HUMAN_TEXT)))
        s.flush()
        s.add(WorkSource(work_id=w.id, canonical_work_id=w.id,
                         source_type="human_fiction", text_version="corpus-v1",
                         text_sha256=_anchor(s, w.id),
                         purpose_basis="test", identity_purposes=["research"],
                         license_purposes=[], license_basis="test",
                         metadata_status="verified", metadata_basis="test"))
        s.commit()
        return w.id


def _gen(tmp_path, *, writer=None, n_per_op=1):
    writer = writer or _fake_writer
    with db.session() as s:
        return k2g.generate(s, n_per_op=n_per_op, writer=writer,
                            model="fake-writer",
                            cache_dir=tmp_path / "cache",
                            ledger=tmp_path / "ledger.jsonl")


def test_schema_valid_and_op_coverage(seeded, tmp_path):
    """① schema 合法且 op 全覆盖：4 op 各 n 对；strategy_key 由 label_of
    唯一决定；span/sha/scene_keys/text_version 机械一致。"""
    res = _gen(tmp_path, n_per_op=2)
    assert res["n_pairs"] == 8
    by_op = {p["op"] for p in res["pairs"]}
    assert by_op == set(k2c.OPS)
    for p in res["pairs"]:
        assert p["strategy_key"] == \
            k2c.LABEL_STRATEGY[k2c.label_of(p["op"])]
        assert p["human_text"] == HUMAN_TEXT
        assert p["span_start"] == 0 and p["span_end"] == len(HUMAN_TEXT)
        assert p["human_sha256"] == hashlib.sha256(
            HUMAN_TEXT.encode("utf-8")).hexdigest()
        assert p["scene_keys"] and p["text_version"] == "corpus-v1"
        assert p["meta"]["generator"] == "k2_pairs_gen"
        assert p["segment_id"] and p["meta"]["work_id"] == seeded
    # spec 级合法：抽取器的 build_pairs 直接可吃（op/strategy 一致性由它验）
    pairs = k2c.build_pairs(res["pairs"])
    assert len(pairs) == 8


def test_label_of_single_source_no_drift(seeded, tmp_path):
    """② 标签单源：本模块不含第二份 op→标签映射（直接用 k2c.label_of）；
    任何对 k2c.OP_LABEL 的漂移都会让本用例红。"""
    assert k2g.HUMAN_FORBIDDEN and \
        set(k2g.HUMAN_FORBIDDEN) == set(k2c.INTERPRET_MARKERS) \
        | set(k2c.PSYCH_MARKERS) | set(k2c.BEAT_MARKERS) \
        | set(k2c.MODIFIER_MARKERS) | set(k2c.FEATURE_WORDS[k2c.S1_KEY]) \
        | set(k2c.FEATURE_WORDS[k2c.S2_KEY])
    for op, label in k2c.OP_LABEL.items():
        assert k2c.label_of(op) == label
    with pytest.raises(ValueError):
        k2c.label_of("not_an_op")


def test_cache_idempotent_resume(seeded, tmp_path):
    """③ 缓存/续跑幂等：第二次生成 0 次 writer 调用、全命中缓存、
    产物对逐字相同。"""
    calls = {"n": 0}

    def counting_writer(ins, hum):
        calls["n"] += 1
        return _fake_writer(ins, hum)
    r1 = _gen(tmp_path, writer=counting_writer)
    assert calls["n"] == 4
    r2 = _gen(tmp_path, writer=counting_writer)   # 续跑：全部走缓存
    assert calls["n"] == 4, "缓存命中不许再调 writer"
    assert r2["n_writer_calls"] == 0 and r2["n_cache_hits"] == 4
    assert [(p["op"], p["ai_text"]) for p in r1["pairs"]] == \
        [(p["op"], p["ai_text"]) for p in r2["pairs"]]


def test_happy_path_passes_extractor_gates(seeded, tmp_path):
    """④ happy path 生成的对过抽取器门0——本夹具按全六道门构造，实际
    断言 gate_pair 全过（更强口径，未放宽任何门）。"""
    res = _gen(tmp_path)
    pairs = k2c.build_pairs(res["pairs"])
    for p in pairs:
        ok, reasons = k2c.gate_pair(p)
        assert ok, f"{p.op}: {reasons}"


def test_writer_unreachable_fails_closed(seeded, tmp_path):
    """⑤ writer 不可达=如实报错退出而非伪造数据：网络异常 / HTTP 非 200 /
    空 content / 截断（finish≠stop）各报各的错；主流程抛出后不写 out、
    不落假 ai_text。"""
    # 网络层异常（死端口）
    with pytest.raises(SystemExit, match="writer 不可达"):
        k2g.call_writer("http://127.0.0.1:9/v1", "m", "k", "ins", "hum",
                        timeout=0.5)

    class _Resp:
        def __init__(self, status, content, finish):
            self.status_code = status
            self.text = "{}"
            self._content, self._finish = content, finish

        def json(self):
            return {"choices": [{"message": {"content": self._content},
                                 "finish_reason": self._finish}]}

    def _post(status=200, content="x", finish="stop"):
        return lambda url, **kw: _Resp(status, content, finish)

    with pytest.raises(SystemExit, match="HTTP 500"):
        k2g.call_writer("http://ph/v1", "m", "k", "ins", "hum",
                        _post=_post(status=500))
    with pytest.raises(SystemExit, match="空 content"):
        k2g.call_writer("http://ph/v1", "m", "k", "ins", "hum",
                        _post=_post(content=""))
    with pytest.raises(SystemExit, match="未正常收尾"):
        k2g.call_writer("http://ph/v1", "m", "k", "ins", "hum",
                        _post=_post(finish="length"))
    # 主流程：writer 抛错 → 不写 out、不伪造
    def broken_writer(ins, hum):
        raise SystemExit("writer 不可达（注入）")
    out = tmp_path / "never.json"
    with db.session() as s:
        with pytest.raises(SystemExit, match="不可达"):
            k2g.generate(s, n_per_op=1, writer=broken_writer,
                         model="fake", cache_dir=tmp_path / "c2",
                         ledger=None)
    assert not out.exists(), "writer 挂后绝不许产出对文件"


# ── 来源合规门增钉（⑥~⑩，任务书「来源门」2026-09-24）─────────────────

BIG = 10 ** 6   # 测试库行数量级远小于它：pool 即全库合格段（计数精确）


def _seed_src_work(s, *, source_type=None, text_version=None,
                   register=True, n_seg=2, title="t-k2src"):
    """造一个来源登记形态指定的作品（段文本用无信号基线 HUMAN_TEXT），
    返回 work_id。source_type=None 且 register=True ⇒ 登记行 source_type
    列 NOT NULL，故 register=False 才表达「无登记行」。"""
    w = Work(title=title, source="test:k2src")
    s.add(w)
    s.flush()
    for i in range(n_seg):
        s.add(Segment(work_id=w.id, ordinal=i, text=HUMAN_TEXT,
                      text_clean=HUMAN_TEXT, role=None,
                      n_sentences=3, n_chars=len(HUMAN_TEXT)))
    # 锚必须在段可见之后算：SessionLocal 是 autoflush=False，未 flush 的
    # pending 段不会被 _work_sha256 的查询看到 ⇒ 锚恒 None，登记行带着空锚
    # 落库，而 clean_tree 的占位登记只补「无登记行」的作品 ⇒ 每条各报一次
    # anchor_drift（跨文件假红根因的另一半）。
    s.flush()
    if register:
        s.add(WorkSource(work_id=w.id, canonical_work_id=w.id,
                         source_type=source_type, text_version=text_version,
                         text_sha256=_anchor(s, w.id),
                         purpose_basis="test", identity_purposes=["research"],
                         license_purposes=[], license_basis="test",
                         metadata_status="verified", metadata_basis="test"))
    s.commit()
    return w.id


def _pool_and_excluded(s):
    pool, trace = k2g.human_pool(s, n=BIG, scan_limit=BIG)
    rep = k2g.trace_report(trace)
    return pool, rep["excluded_sources"], rep["eligible_sources"]


def test_source_gate_excludes_noncompliant_four_forms(tmp_path):
    """⑥ fixture / synthetic / commentary / 无登记行四种不合规来源全部
    被排除，且 excluded_sources 留痕 work_id/source_type/reason/n_segments
    可核对（不静默丢）。"""
    db.init_db()
    with db.session() as s:
        w_fix = _seed_src_work(s, source_type="fixture",
                               text_version="test-fixture")
        w_syn = _seed_src_work(s, source_type="synthetic",
                               text_version="corpus-v1")
        w_com = _seed_src_work(s, source_type="commentary",
                               text_version="corpus-v2-mirror")
        w_noreg = _seed_src_work(s, register=False)
        pool, excluded, _eligible = _pool_and_excluded(s)
    by_wid = {e["work_id"]: e for e in excluded}
    # 四种来源都留痕，理由码逐一对得上
    assert by_wid[w_fix]["reason"] == k2g.REASON_BAD_TYPE
    assert by_wid[w_fix]["source_type"] == "fixture"
    assert by_wid[w_syn]["reason"] == k2g.REASON_BAD_TYPE
    assert by_wid[w_syn]["source_type"] == "synthetic"
    assert by_wid[w_com]["reason"] == k2g.REASON_BAD_TYPE
    assert by_wid[w_com]["source_type"] == "commentary"
    assert by_wid[w_noreg]["reason"] == k2g.REASON_NO_ROW
    # 逐来源段数留痕（各 2 段全部被排除）
    for wid in (w_fix, w_syn, w_com, w_noreg):
        assert by_wid[wid]["n_segments"] == 2
    # 池内零命中：不合规来源的 segment_id 一个都不许进池
    assert not ({it["work_id"] for it in pool}
                & {w_fix, w_syn, w_com, w_noreg})


def test_source_gate_admits_both_compliant_forms(tmp_path):
    """⑦ 合规来源两种形态（精确值 human_fiction 与前缀
    production_nonbenchmark_*）正常进池，合格明细带 text_version。"""
    db.init_db()
    with db.session() as s:
        w_hf = _seed_src_work(s, source_type="human_fiction",
                              text_version="corpus-v1")
        w_pb = _seed_src_work(s, source_type="production_nonbenchmark_k2v2",
                              text_version="corpus-v2-mirror")
        pool, _excluded, eligible = _pool_and_excluded(s)
    assert (pool and eligible), "前置：本用例的合规段必须在池里"
    got_hf = [it for it in pool if it["work_id"] == w_hf]
    got_pb = [it for it in pool if it["work_id"] == w_pb]
    assert len(got_hf) == 2 and len(got_pb) == 2
    assert all(it["text_version"] == "corpus-v1" for it in got_hf)
    assert all(it["text_version"] == "corpus-v2-mirror" for it in got_pb)
    by_wid = {e["work_id"]: e for e in eligible}
    assert by_wid[w_hf]["n_pool_eligible"] == 2
    assert by_wid[w_pb]["source_type"] == "production_nonbenchmark_k2v2"
    assert by_wid[w_pb]["n_pool_eligible"] == 2


def test_source_gate_text_version_whitelist(tmp_path):
    """⑧ source_type 合规但 text_version 不在 K3 白名单（test-fixture /
    空值）一律排除，留痕 reason=text_version_not_allowed。"""
    db.init_db()
    with db.session() as s:
        w_tvx = _seed_src_work(s, source_type="human_fiction",
                               text_version="test-fixture")
        w_tve = _seed_src_work(s, source_type="human_fiction",
                               text_version="")
        pool, excluded, _eligible = _pool_and_excluded(s)
    by_wid = {e["work_id"]: e for e in excluded}
    assert by_wid[w_tvx]["reason"] == k2g.REASON_BAD_TEXT_VERSION
    assert by_wid[w_tvx]["text_version"] == "test-fixture"
    assert by_wid[w_tve]["reason"] == k2g.REASON_BAD_TEXT_VERSION
    assert not ({it["work_id"] for it in pool} & {w_tvx, w_tve})


def test_source_constants_single_source_no_drift():
    """⑨ 常量单源漂移即红：本模块的合规口径必须逐字等于
    scripts/k2_extract_backfill.py 源码里的字面量（直接读文件比对，
    不 import 后自比自），且判定入口就是 backfill 的那个函数；
    text_version 白名单就是 K3 的 DEFAULT_ALLOWED_TEXT_VERSIONS。"""
    import re
    import ast
    src = (ROOT / "scripts" / "k2_extract_backfill.py").read_text(
        encoding="utf-8")
    m_t = re.search(
        r"^NONBENCHMARK_SOURCE_TYPES\s*=\s*frozenset\((\{[^}]*\})\)",
        src, re.M)
    m_p = re.search(
        r'^NONBENCHMARK_SOURCE_TYPE_PREFIX\s*=\s*["\']([^"\']*)["\']',
        src, re.M)
    assert m_t and m_p, "k2_extract_backfill 常量声明形态变了——两边同步核查"
    assert set(ast.literal_eval(m_t.group(1))) == \
        set(k2g.NONBENCHMARK_SOURCE_TYPES), "来源类型白名单漂移"
    assert m_p.group(1) == k2g.NONBENCHMARK_SOURCE_TYPE_PREFIX, \
        "合规前缀漂移"
    # 判定入口唯一：k2_pairs_gen 不另写第二套判定（函数就住在 backfill）
    assert k2g.nonbenchmark_compliant_source.__module__ == \
        "k2_extract_backfill"
    assert k2g.nonbenchmark_compliant_source("fixture") is False
    assert k2g.nonbenchmark_compliant_source("human_fiction") is True
    assert k2g.nonbenchmark_compliant_source(
        "production_nonbenchmark_x") is True
    # text_version 白名单与 K3 证据侧同一对象（同源，不复制字面量）
    from app import knowledge_query as kq
    assert k2g.ALLOWED_TEXT_VERSIONS is kq.DEFAULT_ALLOWED_TEXT_VERSIONS


def test_compliant_pool_short_fails_closed_no_fallback(tmp_path):
    """⑩ 合规源不足 n 时 fail-closed：SystemExit 非零、writer 一次都不调、
    绝不回退到不合规来源凑数（fixture 段就在库里也不取）。"""
    db.init_db()
    with db.session() as s:
        w_fix = _seed_src_work(s, source_type="fixture",
                               text_version="test-fixture", n_seg=4)
    calls = {"n": 0}

    def counting_writer(ins, hum):
        calls["n"] += 1
        return _fake_writer(ins, hum)
    with db.session() as s:
        with pytest.raises(SystemExit, match="不足") as ei:
            k2g.generate(s, n_per_op=BIG, writer=counting_writer,
                         model="fake", cache_dir=tmp_path / "c3",
                         ledger=None)
    assert calls["n"] == 0, "池不足发生在调 writer 之前——一次都不许调"
    msg = str(ei.value)
    assert "来源合规门排除" in msg, "失败信息必须留痕不合规来源被排除"
    assert not (tmp_path / "c3").exists(), "失败路径不留缓存半成品"
    # 不回退的正面核对：同一库上逐段验池——fixture 段零进池且留痕在案
    with db.session() as s:
        pool, excluded, _eligible = _pool_and_excluded(s)
    assert not [it for it in pool if it["work_id"] == w_fix], \
        "失败重试也休想从 fixture 源取段"
    assert any(e["work_id"] == w_fix
               and e["reason"] == k2g.REASON_BAD_TYPE for e in excluded)
