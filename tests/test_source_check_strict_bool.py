"""source_check 严格布尔口径回归（审计 P1：模型字段类型不严）。

缺陷实证：scripts/source_check.py 旧写入用 `bool(d.get("src_ok"))`——
模型返回 `{"src_ok": "false"}` / `1` / `null` 这类合法 JSON 但类型不严的值时，
bool("false") == True ⇒ 落库成 src_ok=true，被下游（`is True` 严格口径）
当成"源文本完好"的合格证据。

钉死三条：
1. parse_src_ok 三态 + merge_integrity 未校验态绝不写布尔 src_ok（含临时 sqlite 落库值）；
2. needs_check 幂等判定：存量字符串 "false" 必须被重查（targets 的 todo 集合包含它）；
3. engine/console 统计严格化：布尔与非布尔混合输入 → 未校验计数正确。

自包含：临时目录/临时 sqlite（conftest 的 LG_DATABASE_URL），零网络、零真实库。
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import source_check as sc  # noqa: E402
from app import db  # noqa: E402
from app import console as console_mod  # noqa: E402
from app.engine import _integrity_state  # noqa: E402
from app.models import Segment, Work  # noqa: E402


# ── 1. parse_src_ok 三态 ────────────────────────────────────

def test_parse_src_ok_tri_state():
    assert sc.parse_src_ok(True) is True
    assert sc.parse_src_ok(False) is False
    for bad in ("true", "false", 1, 0, None, [], {}, "1"):
        assert sc.parse_src_ok(bad) is None, f"{bad!r} 不该被当成已校验"


# ── 2. merge_integrity ──────────────────────────────────────

def test_merge_strict_bool_true_false():
    out = sc.merge_integrity({}, True, "low", [], "pv1")
    assert out["src_ok"] is True
    out = sc.merge_integrity({}, False, "high", ["缺字"], "pv1")
    assert out["src_ok"] is False
    assert out["defects"] == ["缺字"]
    assert out["severity"] == "high"
    assert out["checked_pv"] == "pv1"


def test_merge_unverified_never_writes_bool_src_ok():
    """类型不严的模型输出 ⇒ 显式未校验态，且历史残留的 src_ok 被清掉。"""
    for raw_value in ("false", "true", 1, 0, None):
        prev = {"src_ok": raw_value}   # 模拟已落库的脏值
        out = sc.merge_integrity(prev, None, "low", [], "pv1",
                                 raw=json.dumps({"src_ok": raw_value},
                                                ensure_ascii=False))
        assert "src_ok" not in out, f"src_ok={raw_value!r} 未校验时不该保留 src_ok 键"
        assert out["src_ok_unverified"] is True
        assert isinstance(out["src_ok_raw"], str)
        assert out["checked_pv"] == "pv1"


def test_merge_unverified_truncates_raw_to_200():
    raw = "x" * 500
    out = sc.merge_integrity({}, None, None, None, "pv1", raw=raw)
    assert len(out["src_ok_raw"]) == 200


def test_merge_strict_overwrites_stale_unverified_marker():
    """之后真查出来了 ⇒ 未校验标记被清掉，src_ok 是严格布尔。"""
    prev = {"src_ok_unverified": True, "src_ok_raw": "…"}
    out = sc.merge_integrity(prev, True, "low", [], "pv1")
    assert out["src_ok"] is True
    assert "src_ok_unverified" not in out
    assert "src_ok_raw" not in out


# ── 3. needs_check 幂等判定 ─────────────────────────────────

def test_needs_check_strict():
    assert sc.needs_check({"src_ok": True}) is False    # 已查完好 → 跳过
    assert sc.needs_check({"src_ok": False}) is False   # 已查判坏 → 跳过
    for dirty in ("false", "true", 1, 0, None):
        assert sc.needs_check({"src_ok": dirty}) is True, f"{dirty!r} 必须重查"
    assert sc.needs_check({}) is True
    assert sc.needs_check(None) is True


# ── 4. 临时 sqlite 落库值 ───────────────────────────────────

def _seed_segment(integrity: str) -> str:
    db.init_db()
    with db.session() as s:
        w = Work(title="t-strict-bool", source="test:strict-bool")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="他把茶喝完才起身，屋外风声很紧。",
                      text_clean="他把茶喝完才起身，屋外风声很紧。",
                      role="train", n_sentences=1, n_chars=17,
                      integrity=integrity)
        s.add(seg)
        s.commit()
        return seg.id


def test_persisted_value_is_never_loose_bool_true():
    """落库值断言：模型返回 "false"/1/null ⇒ 临时 sqlite 里读回来的值不是布尔 true。"""
    for raw_value in ("false", "true", 1, 0, None):
        # json.dumps(None) → "null"，即落库 {"src_ok": null} 的存量脏值
        sid = _seed_segment('{"src_ok": %s}' % json.dumps(raw_value))
        with db.session() as s:
            seg = s.get(Segment, sid)
            prev = json.loads(seg.integrity or "{}")
            # 模拟 run() 的 LLM 返回分支（verdict 经 parse_src_ok 严格化）
            verdict = sc.parse_src_ok(prev.get("src_ok"))
            new_integ = sc.merge_integrity(prev, verdict, "low", [], "pv-test",
                                           raw=json.dumps(prev, ensure_ascii=False))
            seg.integrity = json.dumps(new_integ, ensure_ascii=False)
            s.commit()
        with db.session() as s:
            stored = json.loads(s.get(Segment, sid).integrity)
        if verdict is None:
            # 类型不严：绝不落库成布尔 true，且不留 src_ok 键（老判断不再误认"已查过"）
            assert stored.get("src_ok") is not True
            assert "src_ok" not in stored
            assert stored["src_ok_unverified"] is True
        else:
            assert stored["src_ok"] is verdict


def test_targets_rechecks_string_false_row():
    """库里存字符串 "false" 的行必须被 targets 判为需重查（todo 集合包含它）。"""
    sid = _seed_segment('{"src_ok": "false"}')       # 脏值 → 必须重查
    sid_ok = _seed_segment('{"src_ok": true}')       # 严格布尔 → 跳过
    with db.session() as s:
        rows = [(x.id, x.integrity) for x in
                s.query(Segment).filter(Segment.id.in_([sid, sid_ok])).all()]
    todo = []
    for xid, integ in rows:
        have = json.loads(integ or "{}")
        if sc.needs_check(have):
            todo.append(xid)
    assert sid in todo, "存量字符串 'false' 必须被重查"
    assert sid_ok not in todo, "严格布尔已查过的行不该重查"


def test_targets_scope_bench_includes_dirty_row():
    """targets(scope=bench) 端到端：脏值行进 todo，严格布尔行不进。"""
    sid = _seed_segment('{"src_ok": "false"}')
    sid_ok = _seed_segment('{"src_ok": false}')
    with db.session() as s:
        for xid, role in ((sid, "benchmark"), (sid_ok, "benchmark")):
            seg = s.get(Segment, xid)
            seg.role = role
        s.commit()
    todo_ids = {x[0] for x in sc.targets("bench")}
    assert sid in todo_ids
    assert sid_ok not in todo_ids


# ── 5. engine / console 统计严格化 ──────────────────────────

def test_engine_integrity_state_mixed():
    rows = [
        SimpleNamespace(integrity='{"src_ok": true}'),
        SimpleNamespace(integrity='{"src_ok": false}'),
        SimpleNamespace(integrity='{"src_ok": "false"}'),   # 脏值
        SimpleNamespace(integrity='{"src_ok": null}'),
        SimpleNamespace(integrity='{"src_ok_unverified": true}'),
        SimpleNamespace(integrity="{}"),
        SimpleNamespace(integrity=None),
    ]
    st = _integrity_state(rows)
    assert st["src_ok"] == 1
    assert st["src_bad"] == 1
    assert st["checked"] == 2          # 只认严格布尔
    assert st["src_unverified"] == 3   # "false" 字符串 + null + 显式未校验态


def test_console_corpus_integrity_mixed():
    _seed_segment('{"src_ok": true}')
    _seed_segment('{"src_ok": false}')
    _seed_segment('{"src_ok": "false"}')
    _seed_segment('{"src_ok_unverified": true}')
    _seed_segment("{}")
    with db.session() as s:
        integ = console_mod._corpus(s)["integrity"]
    assert integ["src_ok"] >= 1 and integ["src_bad"] >= 1
    # 我们这几行：1 ok + 1 bad + 2 unverified + 1 unchecked
    assert integ["src_unverified"] >= 2
    assert integ["checked"] == integ["src_ok"] + integ["src_bad"]
    assert "unchecked" in integ and "src_unverified" in integ  # 旧键保留 + 新键向后兼容
