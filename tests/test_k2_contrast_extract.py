"""K2 成对对照抽取器回归（scripts/k2_contrast_extract.py，主控派工 2026-09-23）。

钉住的事：
1. 既有三道机械门（关键词/场景/长度）各自的接受与拒绝（拒绝理由可读、
   含既定形态文本）；
2. gate_pair 收全理由不短路；summarize 给通过/拒绝计数与理由分类；
3. --dry-run 零库写（临时 sqlite，strategy_instances 行数不变）；
4. 落库幂等：同 (策略, 版本, human_sha256) 重跑不重复落库；
5. --live 缺环境变量 K2CONTRAST_ALLOW_LIVE=1 ⇒ 拒绝执行（fail-closed）；
6. 破形对永不落库；span 对登记段核不上不落（既有门禁 verify_instance_span
   照用，不放宽）。
7. 按构造标注（独立审查 REVISE 修法）：op 必填，S1/S2 归属由 label_of(op)
   直接决定（唯一映射、判定确定）；四个 op 各有**对着自己**的构造断言；
   混合 op（跨标签证据形态混入同一对）必须被拒。
8. 门4 anti-copy / 门5 cross-strategy（二次独立审查 REVISE 修法）：human 侧
   全文被 ai 侧**连续包含**（照抄+贴标签）必须被拒且理由含 `anti-copy` 与
   命中片段长度；两条判据（**剔标归一后** ai 以 human 全文为前缀/后缀、
   归一包含且 ≥ min_copy_len）各有一条对应用例；本方与对方策略特征词表
   同时命中必须被拒且理由含 `cross-strategy`。审查席两个反例（照抄+贴标签
   / 逐字引用后接无关延展）逐字入回归，必须被拦。
9. 旁路账本：live 落库时完整配对（含 AI 侧原文、pair_id、op 两个标签、
   逐门结果与拒绝理由）逐对追加写入 JSONL（路径参数，默认 k2_pairs.jsonl）；
   dry-run 不写账本；不改任何既有表结构。
10. 门0 场景锚定（REVISE_k2_contrast_v2 修法）：S1 新增命中句必须自身可
    锚定到场景（句内含 scene_keys 指称或 她/他/它/自己 回指）——C 型
    （不提名跑题）必须被拒；D 型（复述人名跑题）在机械口径下可锚定、
    拦不住，xfail 钉成显式残余；E 型真摊开必须仍放行。
11. 门4 R1 复审（REVIEW_k2_anticopy_v2）：判据①原用**原文** startswith，
    一个前导空格/全角空格或"标签在前、照抄在后"即逃逸（B2/B3/B4 假阴性，
    漏洞范围=剔标后 <40 字的短 human）。修法：判据①改**归一前/后缀**
    （不设长度容错）+ 剔标用 str.isspace()（堵 U+3000/NBSP）。B1/B6/B7
    保持 REJECT；B5（归一中部一字扰动）为连续包含口径固有残余，xfail 钉住。

纪律：测试**从不**执行 CLI --live 开放路径（那是真落库）；库函数
run_contrast(live=True) 只对 conftest 的临时 sqlite 用。
"""
from __future__ import annotations

import importlib.util as _u
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k2c", ROOT / "scripts" / "k2_contrast_extract.py")
k2c = _u.module_from_spec(_spec)
# 必须先登记进 sys.modules 再 exec_module：模块里的 @dataclass 在 3.11 会回查
# sys.modules[cls.__module__] 解析字符串注解，未登记 ⇒ AttributeError 收集期即炸。
sys.modules["k2c"] = k2c
_spec.loader.exec_module(k2c)

from app import db                                        # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,   # noqa: E402
                        StrategyInstance, Work)

_n = [0]

HUMAN_S1 = "林昭把杯子放下，没接话。窗外有人喊了一嗓子，她朝那边看了一眼，还是没说。"
AI_S1 = ("林昭把杯子放下，没有接话。其实她心里明白，这件事再争也没有用，"
         "因为说了他也不会改。窗外有人喊了一嗓子，她朝那边看了一眼，"
         "还是没说，说到底不过是懒得再提。")
HUMAN_S2 = "林昭推门进来，把伞收了，靠在门边喘气。"
AI_S2 = ("林昭推门进来，把伞收了，靠在门边喘气。忽然她像是想起了什么，"
         "缓缓直起身，然后一步一步走回桌边，仿佛屋里的一切都慢了下来。")
HUMAN_PSYCH = "林昭把杯子放下，没接话。"
# 好对纪律（门4 新口径）：AI 侧不得逐字包含 human 全文——首句改写（没→没有），
# AI 侧不再以 human 全文为前缀，是合理的扩写而非"照抄+贴标签"。
AI_PSYCH = "林昭把杯子放下，没有接话。她心里明白，这件事再争也不会有结果。"
SCENES = {"林昭", "临江城"}

# 旁路账本用例的**独立证据文本**：与 persist 用例的 (HUMAN_S1, AI_S1) 刻意
# 不同。原因：同一 pytest 会话共用一条临时库，_seed 每调一次就多一张同
# (strategy_key, version=1) 的卡，而 resolve_strategy 的 id 兜底序排的是
# `ESV2-<随机hex>` 的**字典序**（app/ids.py），不是创建序——若账本用例复用
# 同一证据文本，它可能解到 persist 用例**已写过行**的旧卡上：幂等 dup 命中
# ⇒ skip_dup_sha ⇒ written=0（随机翻脸，本用例曾因此在主控复跑变红）。
# 换独有证据文本后，落库结果与「解到哪张卡」无关，判定确定。
# 构造仍是 S1 合法形态：首句改写（没→没有 ⇒ AI 侧不以 human 全文为前缀、
# 剔标后也不构成连续包含，门4 新口径放行）、新增句命中解释标记
# （其实/因为/说到底）且逐句锚定到场景（他/她 回指）、两侧共指 沈默、
# 长度比约 2.1。（原「因为讲了于事无补」句无任何锚定特征，门0 追加锚定
# 断言后改写为「因为再讲他也不会改」，构造与判定语义不变。）
HUMAN_LEDGER = "沈默把茶碗推过去，没再多问。廊下有人挑着担子走过，他侧耳听了一阵，终究没开口。"
AI_LEDGER = ("沈默把茶碗推过去，没有再多问。其实他心里清楚，就算问了她也未必肯讲，"
             "因为再讲他也不会改。廊下有人挑着担子走过，他侧耳听了一阵，"
             "终究没开口，说到底不过是怕惹麻烦。")
SCENES_LEDGER = {"沈默", "临江城"}

# —— 二次独立审查 REVISE 反例（文本逐字取自审查席反例描述）——
# 反例1（贴标签式假对照）：ai 侧 = human 侧逐字全文 + 尾缀一句万能标签。
# human 侧取 林昭把杯子放下…（36 字、剔标 31）——短于 min_copy_len=40，
# 仍须因"归一后 ai 以 human 全文为前缀（R1 复审后判据①口径）"被判拒。
CE1_TAG = "然后她忽然笑了一下，其实她心里明白，说到底不过是懒得再提。"
CE1_AI = HUMAN_S1 + CE1_TAG
# 反例2（引用后跑题式）：逐字引用后接无关延展。延展不含对方策略词表，
# 使拦截只能来自 anti-copy（归因干净），且既有四道门确实放行它。
CE2_EXT = ("其实说到底，院里的猫又上了墙，风把晾衣绳吹得直晃，"
           "远处的火车过了桥才响起来。")
CE2_AI = HUMAN_S1 + CE2_EXT

# 判据②（剔标点后归一包含）的专用夹具：human 侧剔标后 48 字 ≥ min_copy_len
# 40，使其能走"归一包含"分支（而非 36 字反例1 走的前缀分支）。
HUMAN_LONG = ("林昭把杯子放下，没接话。窗外有人喊了一嗓子，她朝那边看了一眼，"
              "还是没说。廊下的灯笼晃了两晃，她把袖口拢紧了些。")

# —— R1 复审（REVIEW_k2_anticopy_v2，主控实跑复现）：判据①原文 startswith
# 假阴性。缺陷构造 B1–B7 共用标签句 T（含 其实/因为/说到底，锚定含 她，
# 不碰 S2 词表——拦截归因干净，只有门4 能动它）：
#   B1  H+T                 原文前缀即拦（基线，改前后均 REJECT）
#   B2  " "+H+T             一个半角空格打断原文前缀 ⇒ 改前 ACCEPT（假阴性）
#   B3  "\u3000"+H+T        全角空格：旧 _PUNCT_CHARS 不含 U+3000 ⇒ 同漏
#   B4  T+H                 标签在前、照抄在后：非前缀且剔标 31<40 ⇒ 同漏
#   B5  AI 侧一字扰动+T     归一中部被打断 ⇒ 口径固有残余，xfail 钉住
#       （主控校正：扰动必须在 ai 侧；放在 human 侧时 ai 仍是精确前缀，
#        判据①本就拦得住，属假残余）
#   B6  " "+HUMAN_LONG+T    剔标 48≥40 由判据②兜住（改前后均 REJECT）
#   B7  human=" "+H / ai=" "+H+T   改前原文前缀仍命中 ⇒ REJECT 保持
# 修法：判据①改归一前/后缀（不设长度容错）+ _strip_punct 用 isspace。
ANTICOPY_T = "其实她累了，因为再说也没用，说到底不过是懒得提。"

# —— 三型探针（主控实跑 2026-09-23，k2_probe3：C/D 在旧门0 下均 ACCEPT，
# 即旧门0 只验形态不验所指）——门0 追加「新增句场景锚定」断言后的归属：
#   C 型（首句改写+跑题尾缀，不提名）→ 拒（锚定判据拦，其余门本就放行）；
#   D 型（同 C 但尾缀复述人名）→ 仍 ACCEPT：人名即 scene_keys 指称，机械
#     口径下与合法正例不可区分 ⇒ 已知残余，xfail 钉住；
#   E 型（真摊开：尾缀是对同场景的 genuine 铺陈，含回指字）→ 仍放行。
HUMAN_C = HUMAN_S1      # human 侧同基线；AI 侧首句改写（没→没有）
AI_C = ("林昭把杯子放下，没有接话。窗外有人喊了一嗓子，她朝那边看了一眼，"
        "还是没说。其实说到底，院里的猫又上了墙，风把晾衣绳吹得直晃。")
AI_D = ("林昭把杯子放下，没有接话。窗外有人喊了一嗓子，她朝那边看了一眼，"
        "还是没说。其实说到底，林昭没再理会院里的猫，风把晾衣绳吹得直晃。")
AI_E = ("林昭把杯子放下，没有接话。窗外有人喊了一嗓子，她朝那边看了一眼，"
        "还是没说。其实说到底，她不是不想争，只是这话传出去只会让她更难做。")

# 既有用例沿用 strategy_key 口径时的缺省 op（按构造标注：每对必须声明一个 op）
_DEFAULT_OP = {k2c.S1_KEY: k2c.OP_ADD_INTERPRETATION,
               k2c.S2_KEY: k2c.OP_SPLIT_BEATS}


def _pair(strategy_key, human, ai, *, op=None, scene_keys=SCENES, span=(0, None),
          meta=None) -> k2c.ContrastPair:
    start, end = span
    return k2c.ContrastPair(
        human_text=human, ai_text=ai, scene_keys=scene_keys,
        op=op or _DEFAULT_OP[strategy_key], span_start=start,
        span_end=len(human) if end is None else end, meta=meta or {})


def _seed(*, human_text: str = HUMAN_S1, strategy_key: str = k2c.S1_KEY):
    """独立夹具：1 作品 + 1 段（text_clean=human_text）+ 1 条假设策略卡。
    返回 (segment_id, text_version, strategy_key)。"""
    _n[0] += 1
    tag = f"k2c-{_n[0]}"
    db.init_db()
    with db.session() as s:
        w = Work(title=f"t-{tag}", source="test:k2c")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text=human_text,
                      text_clean=human_text, role="pilot",
                      n_chars=len(human_text), n_sentences=1,
                      integrity='{"src_ok": true}')
        s.add(seg)
        s.flush()
        st = ExpressionStrategyV2(
            strategy_key=strategy_key, abstract_operation=f"操作-{tag}",
            effect_hypothesis=f"假设-{tag}", status="hypothesis",
            scope="UNCERTAIN", scope_ids=[])
        s.add(st)
        s.commit()
        return seg.id, f"tv-{tag}", strategy_key


# ------------------------------------------------------- 门 1：特征共现
def test_gate_keyword_accept_and_reject():
    ok, why = k2c.gate_keyword_cooccurrence(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert ok and why == [], "AI 命中且 human 不命中 ⇒ 接受"

    ok, why = k2c.gate_keyword_cooccurrence(
        _pair(k2c.S1_KEY, HUMAN_S1 + "其实她早就想走了。", AI_S1))
    assert not ok and any("S1 双向命中" in r and "其实" in r for r in why), why

    ok, why = k2c.gate_keyword_cooccurrence(
        _pair(k2c.S1_KEY, HUMAN_S1, "林昭把杯子放下，没有接话，看了一眼窗外。"))
    assert not ok and any("S1 双向未命中" in r for r in why), why

    ok, why = k2c.gate_keyword_cooccurrence(_pair(k2c.S2_KEY, HUMAN_S2, AI_S2))
    assert ok, why
    ok, why = k2c.gate_keyword_cooccurrence(
        _pair(k2c.S2_KEY, HUMAN_S2 + "她忽然有点想笑。", AI_S2))
    assert not ok and any("S2 双向命中" in r for r in why), why


# ------------------------------------------------------- 门 2：场景指称
def test_gate_scene_reference_accept_and_reject():
    ok, why = k2c.gate_scene_reference(
        _pair(k2c.S1_KEY, HUMAN_S1, AI_S1, scene_keys=SCENES))
    assert ok and why == [], "两侧都引用 林昭 ⇒ 同一场景"

    ai_other = AI_S1.replace("林昭", "陈默").replace("她", "他")
    ok, why = k2c.gate_scene_reference(
        _pair(k2c.S1_KEY, HUMAN_S1, ai_other, scene_keys=SCENES))
    assert not ok and any("场景指称交集为空" in r for r in why), why

    ok, why = k2c.gate_scene_reference(
        _pair(k2c.S1_KEY, HUMAN_S1, AI_S1, scene_keys=set()))
    assert not ok and any("场景指称缺失" in r for r in why), why


# ------------------------------------------------------- 门 3：长度比
def test_gate_length_ratio_accept_and_reject():
    ok, why = k2c.gate_length_ratio(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert ok and why == []

    ok, why = k2c.gate_length_ratio(
        _pair(k2c.S1_KEY, HUMAN_S1, HUMAN_S1))          # 等长：ratio=1.0
    assert not ok and any("长度比超界" in r for r in why), why

    ok, why = k2c.gate_length_ratio(
        _pair(k2c.S1_KEY, HUMAN_S1, AI_S1 * 12))        # ratio > 6.0
    assert not ok and any("长度比超界" in r for r in why), why


# ------------------------------------------------- 汇总：计数+理由分类
def test_gate_pair_collects_all_and_summarize_classes():
    bad = _pair(k2c.S1_KEY, HUMAN_S1, HUMAN_S1, scene_keys=set())
    ok, reasons = k2c.gate_pair(bad)
    assert not ok and len(reasons) >= 5, \
        f"六道门全破时理由须收全不短路：{reasons}"
    pairs = [_pair(k2c.S1_KEY, HUMAN_S1, AI_S1),
             _pair(k2c.S2_KEY, HUMAN_S2, AI_S2), bad]
    rep = k2c.summarize(pairs)
    assert rep["n_pairs"] == 3 and rep["passed"] == 2 and rep["rejected"] == 1
    assert set(rep["reject_reason_classes"]) == {
        "add_interpretation 构造不符", "S1 双向未命中",
        "场景指称缺失", "长度比超界", "anti-copy 照抄+贴标签"}, \
        f"拒绝理由必须分类可读（含新门 anti-copy）：{rep['reject_reason_classes']}"
    assert rep["by_op"] == {"add_interpretation": {"passed": 1, "rejected": 1},
                            "add_psych_narration": {"passed": 0, "rejected": 0},
                            "split_beats": {"passed": 1, "rejected": 0},
                            "dilute_modifiers": {"passed": 0, "rejected": 0}}, \
        f"summarize 必须给按 op 的通过/拒绝计数：{rep['by_op']}"
    assert rep["reject_samples"] and rep["reject_samples"][0]["reasons"] \
        and rep["reject_samples"][0]["op"] == "add_interpretation"


# ------------------------------------------------------- build_pairs
def test_build_pairs_injects_ai_side_and_hashes():
    specs = [{"human_text": HUMAN_S1, "ai_text": AI_S1,
              "op": k2c.OP_ADD_INTERPRETATION,
              "scene_keys": list(SCENES), "span_start": 0,
              "span_end": len(HUMAN_S1), "segment_id": "seg-1",
              "text_version": "tv-1"}]
    pairs = k2c.build_pairs(specs)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.op == k2c.OP_ADD_INTERPRETATION
    assert p.strategy_key == k2c.S1_KEY, "strategy_key 须由 op 推导"
    assert p.scene_keys == SCENES
    assert p.meta["segment_id"] == "seg-1" and p.meta["text_version"] == "tv-1"
    import hashlib
    assert p.human_sha256 == hashlib.sha256(
        HUMAN_S1.encode("utf-8")).hexdigest(), "human_sha256 必须现算可对账"
    # fail-closed：规格缺 op（事后归类口径）直接拒
    with pytest.raises(ValueError, match="op"):
        k2c.build_pairs([{k: v for k, v in specs[0].items() if k != "op"}])


# ---------------------------------------------- dry-run 零库写（CLI）
def test_dry_run_zero_writes(tmp_path, monkeypatch):
    _seed()
    pf = tmp_path / "pairs.json"
    pf.write_text(json.dumps(
        {"pairs": [{"human_text": HUMAN_S1, "ai_text": AI_S1,
                    "op": k2c.OP_ADD_INTERPRETATION,
                    "scene_keys": list(SCENES), "span_start": 0,
                    "span_end": len(HUMAN_S1)}]},
        ensure_ascii=False), encoding="utf-8")
    with db.session() as s:
        before = s.query(StrategyInstance).count()

    def _boom(*_a, **_k):               # pragma: no cover
        raise AssertionError("dry-run 不许写旁路账本")

    monkeypatch.setattr(k2c, "write_pairs_ledger", _boom)
    monkeypatch.setattr(sys, "argv",
                        ["k2c", "--dry-run", "--pairs-file", str(pf)])
    k2c.main()                          # 默认即 dry-run，不应碰库
    with db.session() as s:
        after = s.query(StrategyInstance).count()
    assert after == before, "dry-run 写了库——违反零库写承诺"


# ----------------------------------------------- live 缺环境变量 ⇒ 拒
def test_live_without_env_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("K2CONTRAST_ALLOW_LIVE", raising=False)
    pf = tmp_path / "pairs.json"
    pf.write_text(json.dumps({"pairs": []}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["k2c", "--live", "--pairs-file", str(pf)])
    with pytest.raises(SystemExit, match="K2CONTRAST_ALLOW_LIVE"):
        k2c.main()
    monkeypatch.setenv("K2CONTRAST_ALLOW_LIVE", "1")
    monkeypatch.setattr(sys, "argv", ["k2c", "--live"])
    with pytest.raises(SystemExit, match="pairs-file"):
        k2c.main()


# --------------------------------------------- 落库：幂等 + 破形不落
def test_persist_idempotent_and_gated_pairs_never_land(tmp_path):
    seg_id, tv, key = _seed()
    led = str(tmp_path / "k2_pairs.jsonl")
    good = _pair(key, HUMAN_S1, AI_S1,
                 meta={"segment_id": seg_id, "text_version": tv})
    with db.session() as s:
        rep1 = k2c.run_contrast(s, [good], live=True, ledger_path=led)
    assert rep1["written"] == 1 and rep1["passed"] == 1
    assert rep1["ledger"]["entries"] == 1, "live 落库必须逐对写旁路账本"
    with db.session() as s:
        rows = (s.query(StrategyInstance)
                .filter_by(evidence_sha256=good.human_sha256).all())
        assert len(rows) == 1 and rows[0].status == "proposed"
        cond = rows[0].conditions_observed
        assert cond["protocol"] == "paired_contrast_v2"
        assert cond["op"] == k2c.OP_ADD_INTERPRETATION
        assert cond["op_label"] == "S1"
        assert cond["ai_side_sha256"] and cond["ai_side_chars"] == len(AI_S1)
        assert sorted(cond["gates"]) == ["anti_copy", "cross_strategy",
                                         "keyword_cooccurrence",
                                         "length_ratio",
                                         "op_construction",
                                         "scene_reference"]
    with db.session() as s:             # 同输入重跑：同 sha 不重复落库
        rep2 = k2c.run_contrast(s, [good], live=True, ledger_path=led)
    assert rep2["written"] == 0 and rep2["skipped"].get("skip_dup_sha") == 1
    with db.session() as s:
        assert (s.query(StrategyInstance)
                .filter_by(evidence_sha256=good.human_sha256).count()) == 1
    bad = _pair(key, HUMAN_S2, HUMAN_S2, scene_keys=SCENES,
                meta={"segment_id": seg_id, "text_version": tv})
    with db.session() as s:             # 破形对：门内拦掉，永不落库
        rep3 = k2c.run_contrast(s, [bad], live=True, ledger_path=led)
    assert rep3["written"] == 0 and rep3["rejected"] == 1
    with db.session() as s:
        assert (s.query(StrategyInstance)
                .filter_by(evidence_sha256=bad.human_sha256).count()) == 0, \
            "破形对绝不许留库行"


# ------------------------------- 既有门禁照用：span 核不上 / 缺定位不落
def test_persist_span_and_meta_gates(tmp_path):
    seg_id, tv, key = _seed()
    led = str(tmp_path / "k2_pairs.jsonl")
    off = _pair(key, HUMAN_S1, AI_S1, span=(3, len(HUMAN_S1) + 5),
                meta={"segment_id": seg_id, "text_version": tv})
    with db.session() as s:             # span 对登记段核不上 ⇒ 不落（不放宽）
        rep = k2c.run_contrast(s, [off], live=True, ledger_path=led)
    assert rep["written"] == 0 and rep["skipped"].get("skip_span_mismatch") == 1

    no_seg = _pair(key, HUMAN_S1, AI_S1, meta={"text_version": tv})
    with db.session() as s:
        rep2 = k2c.run_contrast(s, [no_seg], live=True, ledger_path=led)
    assert rep2["skipped"].get("skip_no_segment") == 1

    no_strategy = _pair(k2c.S2_KEY, HUMAN_S2, AI_S2, span=(0, len(HUMAN_S2)),
                        meta={"segment_id": seg_id, "text_version": tv})
    with db.session() as s:             # 门全过但库内无该卡 ⇒ 跳过，不自动建卡
        rep3 = k2c.run_contrast(s, [no_strategy], live=True, ledger_path=led)
    assert rep3["passed"] == 1           # 六道门过了（拦点在库侧，不在门侧）
    assert rep3["skipped"].get("skip_no_strategy") == 1
    with db.session() as s:
        assert (s.query(StrategyInstance)
                .filter_by(segment_id=seg_id).count()) == 0, \
            "不许自动建卡/落行（本夹具段上零实例）"


# ------------------------------------- 按构造标注：label_of 唯一映射
def test_label_of_mapping_deterministic_and_illegal():
    assert k2c.label_of(k2c.OP_ADD_INTERPRETATION) == "S1"
    assert k2c.label_of(k2c.OP_ADD_PSYCH_NARRATION) == "S1"
    assert k2c.label_of(k2c.OP_SPLIT_BEATS) == "S2"
    assert k2c.label_of(k2c.OP_DILUTE_MODIFIERS) == "S2"
    # 判定确定性：同 op 重复调用必得同一标签
    for op in k2c.OPS:
        assert k2c.label_of(op) == k2c.label_of(op), op
    # 非法 op fail-closed：映射抛错，构造对时也直接抛
    for bad_op in ("add_interpretation+split_beats", "", "bogus_op", None):
        with pytest.raises(ValueError, match="非法操作标签"):
            k2c.label_of(bad_op)
    with pytest.raises(ValueError, match="非法操作标签"):
        _pair(k2c.S1_KEY, HUMAN_S1, AI_S1, op="bogus_op")


# ------------------------------------- 判定确定性：同输入两次同标签同判定
def test_labeling_determinism_same_input_same_label():
    for key, human, ai, want in ((k2c.S1_KEY, HUMAN_S1, AI_S1, "S1"),
                                 (k2c.S2_KEY, HUMAN_S2, AI_S2, "S2")):
        p = _pair(key, human, ai)
        assert k2c.label_of(p.op) == k2c.label_of(p.op) == want
        assert k2c.gate_pair(p) == k2c.gate_pair(p), \
            "同一对输入两次，判定必须完全一致"
        assert k2c.gate_pair(p)[0] is True


# ------------------------------- 门0：四个 op 各自的接受与拒绝（理由可读）
def test_op_add_interpretation_accept_and_reject():
    ok, why = k2c.gate_op_construction(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert ok and why == [], why
    # 拒：句数未增（无新增陈述）
    ok, why = k2c.gate_op_construction(_pair(k2c.S1_KEY, HUMAN_S1, HUMAN_S1))
    assert not ok and any("句数未增" in r for r in why), why
    # 拒：新增句未命中解释标记词表
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S1_KEY, HUMAN_S1, HUMAN_S1 + "窗外又起了风。"))
    assert not ok and any("未命中" in r and "解释" in r for r in why), why
    # 拒：人类侧命中词表（不构成对照）
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S1_KEY, HUMAN_S1 + "其实她早想走了。", AI_S1))
    assert not ok and any("人类侧命中" in r for r in why), why


def test_op_add_psych_narration_accept_and_reject():
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S1_KEY, HUMAN_PSYCH, AI_PSYCH,
              op=k2c.OP_ADD_PSYCH_NARRATION))
    assert ok and why == [], why
    # 拒：新增句未命中心理标记词表
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S1_KEY, HUMAN_PSYCH, HUMAN_PSYCH + "窗外起了风。",
              op=k2c.OP_ADD_PSYCH_NARRATION))
    assert not ok and any("未命中" in r and "心理" in r for r in why), why
    # 拒：人类侧本就命中心理标记（不构成对照）
    human2 = HUMAN_PSYCH + "她心里明白，再争也无益。"
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S1_KEY, human2, human2 + "窗外起了风。",
              op=k2c.OP_ADD_PSYCH_NARRATION))
    assert not ok and any("人类侧命中" in r for r in why), why


def test_op_split_beats_accept_and_reject():
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S2_KEY, HUMAN_S2, AI_S2, op=k2c.OP_SPLIT_BEATS))
    assert ok and why == [], why
    # 拒：实词集合 Jaccard 过低（命题/实词集合被改写，不是拆拍）
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S2_KEY, HUMAN_S2, "她忽然放慢脚步，像是想起了什么，缓缓回头。",
        op=k2c.OP_SPLIT_BEATS))
    assert not ok and any("Jaccard" in r for r in why), why
    # 拒：节拍标记数未上升（密度变化不可测）
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S2_KEY, HUMAN_S2, HUMAN_S2 + "桌边有杯凉茶。",
        op=k2c.OP_SPLIT_BEATS))
    assert not ok and any("节拍" in r and "未上升" in r for r in why), why


def test_op_dilute_modifiers_accept_and_reject():
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S2_KEY, HUMAN_S2, AI_S2, op=k2c.OP_DILUTE_MODIFIERS))
    assert ok and why == [], why
    # 拒：修饰标记数未上升
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S2_KEY, HUMAN_S2, HUMAN_S2 + "她走了两步。",
        op=k2c.OP_DILUTE_MODIFIERS))
    assert not ok and any("修饰" in r and "未上升" in r for r in why), why
    # 拒：实词集合 Jaccard 过低（命题被换掉，不是注水）
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S2_KEY, HUMAN_S2, "屋外雨声很密，她仿佛没听见，只淡淡应了一声。",
        op=k2c.OP_DILUTE_MODIFIERS))
    assert not ok and any("Jaccard" in r for r in why), why


# ------------------------------- 混合 op：跨标签证据形态混入同一对 ⇒ 拒
def test_mixed_ops_rejected():
    # S2 对里混入 S1 形态：新增解释/心理句
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S2_KEY, HUMAN_S2, AI_S2 + "其实她心里明白，再留也无益。",
        op=k2c.OP_SPLIT_BEATS))
    assert not ok and any("混合操作" in r for r in why), why
    # S1 对里混入 S2 形态：节拍标记上升
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S1_KEY, HUMAN_S1, AI_S1 + "然后她缓缓起身。",
        op=k2c.OP_ADD_INTERPRETATION))
    assert not ok and any("混合操作" in r and "节拍" in r for r in why), why


# ------------------------------- 标记词表是模块常量，测试可整体覆写
def test_marker_vocab_overridable(monkeypatch):
    monkeypatch.setattr(k2c, "INTERPRET_MARKERS", ())
    ok, why = k2c.gate_op_construction(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert not ok and any("未命中" in r for r in why), \
        "词表清空后同一对必须被拒（证明门读的是模块常量）"
    monkeypatch.setattr(k2c, "INTERPRET_MARKERS", ("争",))
    ok, why = k2c.gate_op_construction(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert ok and why == [], "换成自定义词表命中后同一对必须通过"


# ------------------------------- 门4：反抄写（anti-copy）接受与拒绝
def test_gate_anti_copy_accept_and_reject():
    # 正例：摊开会复述大半原文，但第 1 句被改写（没接话→没有接话）——
    # ai 侧不以 human 全文为前缀、剔标后全文也不连续包含 ⇒ 放行
    ok, why = k2c.gate_anti_copy(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert ok and why == [], why
    # S2 类对不适用本门（按构造保留原句，human 本就整段在 ai 侧）
    ok, why = k2c.gate_anti_copy(_pair(k2c.S2_KEY, HUMAN_S2, AI_S2))
    assert ok and why == [], why
    # human 整段被逐字包含（ai 以 human 全文为前缀，判据①）⇒ 拒
    ok, why = k2c.gate_anti_copy(
        _pair(k2c.S1_KEY, HUMAN_S1, HUMAN_S1 + "其实她累了。"))
    assert not ok and any("anti-copy" in r for r in why), why
    # fail-closed：human 剔标后无内容 ⇒ 不可判即拒
    ok, why = k2c.gate_anti_copy(
        _pair(k2c.S1_KEY, "……！！", "其实她累了。再者说，也没用。"))
    assert not ok and any("anti-copy 不可判" in r for r in why), why


def test_anti_copy_min_copy_len_overridable(monkeypatch):
    # ai 侧把 human 全文嵌在中部（非前缀）：剔标后 31 字 < 默认 40 ⇒ 放行
    ai_embed = "屋外起了风。" + HUMAN_S1 + "她终于抬脚往回走。"
    p = _pair(k2c.S1_KEY, HUMAN_S1, ai_embed)
    ok, why = k2c.gate_anti_copy(p)
    assert ok and why == [], "短于 min_copy_len 的偶合不判照抄"
    monkeypatch.setattr(k2c, "MIN_COPY_LEN", 30)
    ok, why = k2c.gate_anti_copy(p)
    assert not ok and any("anti-copy" in r for r in why), \
        "min_copy_len 调低后同一对必须被拒（证明门读的是模块常量）"
    monkeypatch.setattr(k2c, "MIN_COPY_LEN", 40)
    ok, why = k2c.gate_anti_copy(p)
    assert ok and why == [], why


# ------------------------- 门4 R1 复审：归一前/后缀（B1–B7 判定归属）
def test_anticopy_b1_raw_prefix_still_rejected():
    # B1 基线：逐字照抄+尾缀标签。改前判据①（原文前缀）即拦，改后归一口径
    # 必须保持 REJECT（gate_anti_copy 与 gate_pair 两级都验）。
    p = _pair(k2c.S1_KEY, HUMAN_S1, HUMAN_S1 + ANTICOPY_T)
    ok, why = k2c.gate_anti_copy(p)
    assert not ok and any("anti-copy" in r for r in why), f"B1 gate_anti_copy: {why}"
    ok2, why2 = k2c.gate_pair(p)
    assert not ok2 and any("anti-copy" in r for r in why2), f"B1 gate_pair: {why2}"


def test_anticopy_b2_b3_leading_whitespace_rejected():
    # B2 头部半角空格 / B3 头部全角空格（U+3000）：原文 startswith 不命中
    # （=改前逃逸口），归一（isspace 剔尽 Unicode 空白）后 a_norm 仍以
    # h_norm 为前缀 ⇒ 必须 REJECT。
    h_norm = k2c._strip_punct(HUMAN_S1)
    assert k2c._strip_punct("\u3000x\u00a0y\t") == "xy", \
        "剔标口径必须按 isspace 覆盖全角空格/NBSP 等全部 Unicode 空白"
    for label, lead in (("B2", " "), ("B3", "\u3000")):
        ai = lead + HUMAN_S1 + ANTICOPY_T
        assert not ai.startswith(HUMAN_S1), f"{label} 原文前缀不命中（逃逸口）"
        assert k2c._strip_punct(ai).startswith(h_norm), f"{label} 归一前缀应命中"
        p = _pair(k2c.S1_KEY, HUMAN_S1, ai)
        ok, why = k2c.gate_anti_copy(p)
        assert not ok and any("anti-copy" in r for r in why), f"{label} gate_anti_copy: {why}"
        ok2, why2 = k2c.gate_pair(p)
        assert not ok2 and any("anti-copy" in r for r in why2), f"{label} gate_pair: {why2}"


def test_anticopy_b4_label_first_copy_suffix_rejected():
    # B4 标签在前、照抄在后：判据①非原文前缀、判据②剔标 31<40 兜不住
    # （改前 gate_pair ACCEPT）；归一**后缀**口径必须拦，理由点名后缀。
    ai = ANTICOPY_T + HUMAN_S1
    p = _pair(k2c.S1_KEY, HUMAN_S1, ai)
    assert k2c._strip_punct(ai).endswith(k2c._strip_punct(HUMAN_S1))
    ok, why = k2c.gate_anti_copy(p)
    assert not ok and any("anti-copy" in r and "后缀" in r for r in why), why
    ok2, why2 = k2c.gate_pair(p)
    assert not ok2 and any("anti-copy" in r for r in why2), f"B4 gate_pair: {why2}"


@pytest.mark.xfail(reason="已知残余（R1 复审 B5）：**AI 侧**归一全文中部一字扰动"
                          "（没→没有）打断归一连续包含——前/后缀与子串两条判据"
                          "同时落空即逃逸。作者明确拒绝 LCS 模糊匹配（会误杀合法"
                          "扩写），钉为显式残余，不许为此上容错。"
                          "主控 2026-09-23 校正：本用例原构造把扰动放在 **human 侧**"
                          "（human=HUMAN_LONG 改「把袖口→将袖口」、ai=human+T），"
                          "此时 ai 相对 human 仍是**精确前缀**，判据①本就拦得住 ⇒ "
                          "恒不逃逸、改后 XPASS，是**假残余**（构造错误，非口径极限）。"
                          "真实 B5 必须把扰动放在 **ai 侧**。")
def test_anticopy_b5_single_char_perturbation_is_known_residual():
    # B5（主控校正后的真实构造）：human 剔标 48≥40；**ai 侧**在 human 归一
    # 全文中部换一字（没接话→没有接话，与合法 S1 正例同形）再贴标签句。
    # 理想口径应拒（逐字照抄只换一字仍是贴标签），但连续包含口径拦不住
    # ⇒ xfail 钉住。此形与合法 S1 正例（AI_S1 同款首句改写）在一切机械
    # 口径下不可分，故属口径固有极限，不得为此上模糊容错。
    human = HUMAN_LONG
    ai = human.replace("没接话", "没有接话", 1) + ANTICOPY_T
    # 钉死构造：ai 不以 human 归一全文为前缀，也不含 human 归一全文
    assert not k2c._strip_punct(ai).startswith(k2c._strip_punct(human))
    assert k2c._strip_punct(human) not in k2c._strip_punct(ai)
    p = _pair(k2c.S1_KEY, human, ai, op=k2c.OP_ADD_INTERPRETATION)
    ok, why = k2c.gate_pair(p)
    assert not ok and any("anti-copy" in r for r in why),         "B5（AI 侧中部一字扰动+贴标签）理想上应被 anti-copy 拒——当前口径拦不住"


def test_anticopy_b6_b7_long_and_padded_still_rejected():
    # B6 长 human（剔标 48≥40）带前导空格：改前判据②兜住、改后归一前缀亦
    # 拦（双保险，必须保持 REJECT）；B7 两侧同带前导空格：照抄+贴标签同拒。
    p6 = _pair(k2c.S1_KEY, HUMAN_LONG, " " + HUMAN_LONG + ANTICOPY_T)
    ok6, why6 = k2c.gate_pair(p6)
    assert not ok6 and any("anti-copy" in r for r in why6), f"B6: {why6}"
    p7 = _pair(k2c.S1_KEY, " " + HUMAN_S1, " " + HUMAN_S1 + ANTICOPY_T)
    ok7, why7 = k2c.gate_pair(p7)
    assert not ok7 and any("anti-copy" in r for r in why7), f"B7: {why7}"


# ------------------------------- 门5：跨策略互斥（cross-strategy）
def test_gate_cross_strategy_accept_and_reject():
    ok, why = k2c.gate_cross_strategy(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert ok and why == [], why
    ok, why = k2c.gate_cross_strategy(_pair(k2c.S2_KEY, HUMAN_S2, AI_S2))
    assert ok and why == [], why
    # S1 对的 ai 侧同时命中对方（S2）词表 ⇒ 拒，理由带双方命中的词
    ok, why = k2c.gate_cross_strategy(
        _pair(k2c.S1_KEY, HUMAN_S1, AI_S1 + "然后她缓缓转身。"))
    assert not ok and any("cross-strategy" in r for r in why), why
    cross = next(r for r in why if "cross-strategy" in r)
    assert "然后" in cross and "其实" in cross, cross
    # S2 对的 ai 侧同时命中对方（S1）词表 ⇒ 拒
    ok, why = k2c.gate_cross_strategy(
        _pair(k2c.S2_KEY, HUMAN_S2, AI_S2 + "其实她心里明白。"))
    assert not ok and any("cross-strategy" in r for r in why), why


# ------- 审查席反例1：照抄+贴标签（同一对 S1/S2 双卡双计）⇒ 两面都拦
def test_review_counterexample1_copy_plus_label():
    # 同一对文本：先以 S1 提交，再以 S2 提交（审查席指认的双卡双计形态）
    p_s1 = _pair(k2c.S1_KEY, HUMAN_S1, CE1_AI, op=k2c.OP_ADD_INTERPRETATION)
    ok, why = k2c.gate_pair(p_s1)
    assert not ok, "反例1（照抄+贴标签）以 S1 提交必须被拒"
    assert any("anti-copy" in r for r in why), why
    ac = next(r for r in why if "anti-copy" in r)
    assert "前缀" in ac and str(len(HUMAN_S1)) in ac, \
        f"anti-copy 理由必须点名前缀照抄与命中片段长度: {ac}"
    p_s2 = _pair(k2c.S1_KEY, HUMAN_S1, CE1_AI, op=k2c.OP_SPLIT_BEATS)
    ok2, why2 = k2c.gate_pair(p_s2)
    assert not ok2, "同一对换 S2 标签重交仍必须被拒（不许双卡双计）"
    assert any("cross-strategy" in r for r in why2), why2
    cross = next(r for r in why2 if "cross-strategy" in r)
    assert "其实" in cross and "忽然" in cross, \
        f"cross-strategy 理由必须带上双方命中的词: {cross}"


# ------- 反例1 变体（判据②）：human 全文嵌在 ai 中部、标点被改动
# ⇒ 原文 `in` 不命中、剔标点归一后连续包含且 ≥ min_copy_len ⇒ 仍拒
def test_anti_copy_embedded_containment_variant():
    ai_embed = ("屋外起了风。" + HUMAN_LONG.replace("。", "！")
                + "她终于抬脚往回走。")
    p = _pair(k2c.S1_KEY, HUMAN_LONG, ai_embed,
              op=k2c.OP_ADD_INTERPRETATION)
    # 钉住形态：非前缀开头、原文逐字不包含——拦截只能来自归一包含分支
    assert not (ai_embed.startswith(HUMAN_LONG))
    assert HUMAN_LONG not in ai_embed
    ok, why = k2c.gate_anti_copy(p)
    assert not ok, "human 全文剔标后被连续包含（≥ min_copy_len）必须被拒"
    assert any("anti-copy" in r for r in why), why
    ac = next(r for r in why if "anti-copy" in r)
    assert "连续子串" in ac and str(len(k2c._strip_punct(HUMAN_LONG))) in ac \
        and str(k2c.MIN_COPY_LEN) in ac, \
        f"anti-copy 理由必须点名归一包含与命中片段长度: {ac}"


# ------- 审查席反例2：逐字引用后接无关延展 ⇒ anti-copy 拦
# （门0 追加锚定断言后，op_construction 亦拦：跑题句未锚定到场景——
#   拦截不再唯一，但 anti-copy 判据保持不动，两条理由并列可读）
def test_review_counterexample2_quote_then_drift():
    p = _pair(k2c.S1_KEY, HUMAN_S1, CE2_AI, op=k2c.OP_ADD_INTERPRETATION)
    # 钉住形态：其余既有门确实放行它
    for name, gate in k2c.GATES:
        if name in ("op_construction", "anti_copy", "cross_strategy"):
            continue
        g_ok, g_why = gate(p)
        assert g_ok, f"反例2 应过既有门 {name}（否则反例不成立）: {g_why}"
    # 门0 锚定断言现在也拦它：跑题延展句（其实/说到底…）不含指称与回指字
    g_ok, g_why = k2c.gate_op_construction(p)
    assert not g_ok and any("未锚定" in r for r in g_why), g_why
    ok, why = k2c.gate_pair(p)
    assert not ok, "反例2（逐字引用后跑题）必须被拒"
    assert any("anti-copy" in r for r in why), why


# ------- 门0 场景锚定：三型探针（C 拒 / D 残余 xfail / E 放行）
def test_c_type_drift_without_name_rejected_by_anchor():
    p = _pair(k2c.S1_KEY, HUMAN_C, AI_C, op=k2c.OP_ADD_INTERPRETATION)
    # 归因干净：除门0 外的既有门确实放行它——拦截只能来自新锚定判据
    for name, gate in k2c.GATES:
        if name == "op_construction":
            continue
        g_ok, g_why = gate(p)
        assert g_ok, f"C 型应过既有门 {name}（否则归因不干净）: {g_why}"
    ok, why = k2c.gate_pair(p)
    assert not ok, "C 型（首句改写+不提名跑题尾缀）必须被拒"
    assert any("未锚定" in r for r in why), why
    anchor = next(r for r in why if "未锚定" in r)
    assert "跑题尾缀" in anchor and "猫又上了墙" in anchor, \
        f"锚定理由必须点名未锚定的新增句: {anchor}"


@pytest.mark.xfail(reason="已知残余：D 型跑题句复述人名（林昭）后，"
                          "句内即含 scene_keys 指称，机械锚定口径下与合法"
                          "正例不可区分——不误伤 E 型的代价，如实钉住")
def test_d_type_drift_with_name_is_known_residual():
    p = _pair(k2c.S1_KEY, HUMAN_C, AI_D, op=k2c.OP_ADD_INTERPRETATION)
    # 归因：门0 的锚定判据对 D 型放行（尾缀句含 林昭）——残余根因在此
    g_ok, g_why = k2c.gate_op_construction(p)
    assert g_ok, f"锚定判据应放行复述人名的跑题句（残余根因）: {g_why}"
    ok, why = k2c.gate_pair(p)
    assert not ok, "D 型（复述人名跑题）理想上应被拒——当前机械判据拦不住"


def test_e_type_genuine_spread_still_accepted():
    p = _pair(k2c.S1_KEY, HUMAN_C, AI_E, op=k2c.OP_ADD_INTERPRETATION)
    ok, why = k2c.gate_pair(p)
    assert ok and why == [], \
        f"真摊开正对照（新增句含回指字，锚定成立）必须仍放行: {why}"


# ------------------------------- 旁路账本：AI 侧可复核 + 拒绝理由入账
def test_pairs_ledger_records_full_pair(tmp_path):
    # 独立证据文本（HUMAN_LEDGER/AI_LEDGER，理由见其定义处注释）：
    # 落库结果必须与「resolve 解到哪张同键卡」无关，判定确定。
    seg_id, tv, key = _seed(human_text=HUMAN_LEDGER)
    good = _pair(key, HUMAN_LEDGER, AI_LEDGER, scene_keys=SCENES_LEDGER,
                 meta={"segment_id": seg_id, "text_version": tv})
    assert k2c.gate_pair(good)[0] is True, \
        f"账本用例的 good 对必须先过六道门：{k2c.gate_pair(good)[1]}"
    bad = _pair(key, HUMAN_S2, HUMAN_S2, scene_keys=SCENES,
                meta={"segment_id": seg_id, "text_version": tv})
    led = tmp_path / "k2_pairs.jsonl"
    with db.session() as s:
        rep = k2c.run_contrast(s, [good, bad], live=True, ledger_path=str(led))
    assert rep["written"] == 1 and rep["skipped"] == {}, \
        f"过门对必须落库（skipped 报告拦点）：{rep}"
    assert rep["ledger"] == {"path": str(led), "entries": 2}
    recs = [json.loads(line) for line in
            led.read_text(encoding="utf-8").strip().splitlines()]
    assert len(recs) == 2
    by_outcome = {r["persist_outcome"]: r for r in recs}
    w = by_outcome["written"]
    assert w["ai_text"] == AI_LEDGER and w["human_text"] == HUMAN_LEDGER, \
        "旁路账本必须存 AI 侧原文与完整配对（事后可复核）"
    assert w["op"] == k2c.OP_ADD_INTERPRETATION and w["op_label"] == "S1"
    assert w["human_sha256"] == good.human_sha256 and w["ai_sha256"]
    assert set(w["gate_results"].values()) == {"pass"}
    assert w["pair_id"] == k2c.pair_id(good), "pair_id 必须确定性、可跨 run 对账"
    g = by_outcome["gated_out"]
    assert g["gates_ok"] is False and g["reject_reasons"], "拒绝理由必须入账"
    assert g["ai_text"] == HUMAN_S2, "被拒对的完整配对同样留档"


# ============================ --ledger-only 离线门账本（lg-fix-gate-ledger-offline）
# 钉住的事（派工 2026-09-26）：
# ① --ledger-only 与 --dry-run 同为只读：零库写、绝不触 _persist_one/
#    run_contrast（写库路径放断言炸弹）；
# ② 账本行 schema 与探针口径逐字对齐：每行含 GATE_LEDGER_MARKERS
#    （pair_id/gates_ok/persist_outcome），形状 = ledger_entry()；
# ③ persist_outcome 如实标注：过门对 ="not_persisted"、被拒对 ="gated_out"，
#    绝不伪写 "written" ⇒ 探针读出 n_written=0 是真值；
# ④ 探针闭环：_parse_ledger 判 kind="gate"，n_gates_ok=过门数、n_written=0；
# ⑤ 反向验证：探针分类只看 schema 标记、不看行数——把门账本标记
#    （pair_id/gates_ok/persist_outcome）从行里拿掉后 _classify_ledger_rows
#    不再返回 "gate"。注：探针 _hit 为**任一模记命中即算**（any 口径，
#    见 k5_promotion_wire_probe._classify_ledger_rows），只删 gates_ok 而
#    留着 pair_id/persist_outcome 时 kind 仍判 "gate"、但 n_gates_ok 必掉到
#    0——两条都钉成断言，反向验证不许口头化。
# ⑥ --dry-run 既有输出格式一字不动（回归 test_dry_run_zero_writes 已在）。

def _probe_mod():
    """独立名加载探针模块（与 test_k5_promotion_wire_probe_ledger 同法）。"""
    name = "k5pl_k2_ledger_only"
    if name in sys.modules:
        return sys.modules[name]
    spec = _u.spec_from_file_location(name, ROOT / "scripts" /
                                      "k5_promotion_wire_probe.py")
    mod = _u.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_pairs_file(tmp_path) -> Path:
    """1 过门对 + 1 被拒对（与 test_gate_pair_collects_all 同款构造）。"""
    pf = tmp_path / "pairs.json"
    pf.write_text(json.dumps({"pairs": [
        {"human_text": HUMAN_S1, "ai_text": AI_S1,
         "op": k2c.OP_ADD_INTERPRETATION, "scene_keys": list(SCENES),
         "span_start": 0, "span_end": len(HUMAN_S1)},
        {"human_text": HUMAN_S2, "ai_text": HUMAN_S2,
         "op": k2c.OP_SPLIT_BEATS, "scene_keys": list(SCENES),
         "span_start": 0, "span_end": len(HUMAN_S2)},
    ]}, ensure_ascii=False), encoding="utf-8")
    return pf


def _run_ledger_only_cli(tmp_path, monkeypatch):
    """真走 CLI --ledger-only；写库入口全部放断言炸弹。返回账本行列表。"""
    def _boom(*_a, **_k):               # pragma: no cover
        raise AssertionError("--ledger-only 不许写库/走 live 路径")

    monkeypatch.setattr(k2c, "_persist_one", _boom)
    monkeypatch.setattr(k2c, "run_contrast", _boom)
    pf = _write_pairs_file(tmp_path)
    led = tmp_path / "gate_ledger.jsonl"
    monkeypatch.setattr(sys, "argv", ["k2c", "--ledger-only",
                                      "--pairs-file", str(pf),
                                      "--pairs-ledger", str(led)])
    k2c.main()
    return [json.loads(l) for l in
            led.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_ledger_only_zero_db_writes_and_truthful_outcomes(tmp_path, monkeypatch):
    _seed()
    with db.session() as s:
        before = s.query(StrategyInstance).count()
    recs = _run_ledger_only_cli(tmp_path, monkeypatch)
    with db.session() as s:
        assert s.query(StrategyInstance).count() == before, \
            "--ledger-only 写了库——违反只读承诺"
    assert len(recs) == 2
    assert {r["persist_outcome"] for r in recs} == {"not_persisted", "gated_out"}, \
        "persist_outcome 必须如实标注本次没有落库，绝不伪写 written"
    assert all(r["persist_outcome"] != "written" for r in recs)
    ok_row = next(r for r in recs if r["gates_ok"] is True)
    bad_row = next(r for r in recs if r["gates_ok"] is False)
    assert ok_row["persist_outcome"] == "not_persisted"
    assert ok_row["reject_reasons"] == [] and \
        set(ok_row["gate_results"].values()) == {"pass"}
    assert bad_row["persist_outcome"] == "gated_out" and bad_row["reject_reasons"]
    # schema 与 ledger_entry() 逐字对齐（同键集，探针认账本的唯一依据）
    probe = _probe_mod()
    for r in recs:
        assert set(r) == set(k2c.ledger_entry(
            k2c.ContrastPair(human_text=r["human_text"], ai_text=r["ai_text"],
                             scene_keys=set(r["scene_keys"]), op=r["op"],
                             span_start=r["span_start"],
                             span_end=r["span_end"],
                             human_sha256=r["human_sha256"]),
            gates_ok=r["gates_ok"], gate_results=r["gate_results"],
            reasons=r["reject_reasons"], outcome=r["persist_outcome"])), \
            "账本行键集必须与 live 路径 ledger_entry() 完全一致"
        assert set(probe.GATE_LEDGER_MARKERS) <= set(r), \
            f"探针门账本标记缺位：{probe.GATE_LEDGER_MARKERS}"
        assert isinstance(r["gates_ok"], bool) and r["pair_id"].startswith("k2pair-")


def test_ledger_only_ledger_accepted_by_probe(tmp_path, monkeypatch, capsys):
    """探针闭环：--ledger-only 产物喂 _parse_ledger ⇒ kind=gate、
    n_gates_ok=过门真值、n_written=0（没落库，0 是真值不是错）。"""
    _run_ledger_only_cli(tmp_path, monkeypatch)
    rep = json.loads(capsys.readouterr().out)
    assert rep["mode"] == "ledger_only" and rep["written"] == 0
    assert rep["n_pairs"] == 2 and rep["passed"] == 1 and rep["rejected"] == 1
    probe = _probe_mod()
    led = probe._parse_ledger(Path(rep["ledger"]["path"]))
    assert led["kind"] == "gate", led
    assert led["n_gates_ok"] == 1 and led["n_written"] == 0
    assert led["n_rows"] == 2 and led["error"] is None


def test_ledger_only_reverse_verification_gate_markers_required(tmp_path,
                                                                monkeypatch):
    """反向验证（真做）：探针按 schema 标记认门账本，不看行数。
    ① 拿掉全部三个 GATE_LEDGER_MARKERS ⇒ _classify_ledger_rows 不再判
       "gate"（退化版「有行就算门账本」在此必红）；
    ② 只拿掉 gates_ok（探针 any 口径下 pair_id/persist_outcome 仍命中）⇒
       kind 仍 "gate" 但 n_gates_ok 掉到 0——gates_ok 缺失即无一行可计为
       过门，同样钉死，不许口头化。"""
    recs = _run_ledger_only_cli(tmp_path, monkeypatch)
    probe = _probe_mod()
    assert probe._classify_ledger_rows(recs) == "gate"   # 基线：本产物是门账本

    stripped = [{k: v for k, v in r.items()
                 if k not in probe.GATE_LEDGER_MARKERS} for r in recs]
    assert probe._classify_ledger_rows(stripped) != "gate", \
        "拿掉门账本标记后仍判 gate ⇒ 分类器退化成了「有行就算门账本」"

    no_gates_ok = [{k: v for k, v in r.items() if k != "gates_ok"}
                   for r in recs]
    f = tmp_path / "no_gates_ok.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                           for r in no_gates_ok), encoding="utf-8")
    led = probe._parse_ledger(f)
    assert led["kind"] == "gate" and led["n_gates_ok"] == 0, \
        "只缺 gates_ok：any 口径下仍判 gate（探针现实现如此，如实钉住），" \
        "但绝不能再报出任何过门数"
    assert led["n_written"] == 0


def test_ledger_only_requires_pairs_file_and_keeps_dry_run_format(tmp_path,
                                                                  monkeypatch):
    """fail-closed：--ledger-only 空输入不出账本；--dry-run 既有输出格式
    一字不动（mode=dry_run、无 ledger 键、不落任何文件）。"""
    monkeypatch.setattr(sys, "argv", ["k2c", "--ledger-only"])
    with pytest.raises(SystemExit, match="pairs-file"):
        k2c.main()
    pf = _write_pairs_file(tmp_path)
    led = tmp_path / "should_not_exist.jsonl"
    monkeypatch.setattr(sys, "argv", ["k2c", "--dry-run",
                                      "--pairs-file", str(pf),
                                      "--pairs-ledger", str(led)])
    out = _capture_stdout_json(k2c)
    assert out["mode"] == "dry_run" and "ledger" not in out
    assert out["n_pairs"] == 2 and out["passed"] == 1 and out["rejected"] == 1
    assert not led.exists(), "--dry-run 写了账本文件——既有零写承诺被改"


def _capture_stdout_json(mod) -> dict:
    """跑 mod.main()（argv 已由 monkeypatch 设好）并回收其 stdout JSON。"""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main()
    return json.loads(buf.getvalue())
