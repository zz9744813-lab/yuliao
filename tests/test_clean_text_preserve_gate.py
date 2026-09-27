"""清洗正文保留门回归（任务 lg-clean-text-preserve-gate，2026-09-26）。

盘查结论（详见 docs/清洗正文保留门_20260926.md）：`scripts/clean_text.py` 的三条
text_clean 写路径里——

- 规则首写路径（run_rules）只写**空** text_clean（非空 continue 跳过），不构成覆写；
- `polish`（规则再扫）与 `run_llm`（LLM 还原）的每一次落笔都可能**覆写非空正文**，
  重跑即丢旧值且旧值原本无留痕——本文件钉住新加的「清洗正文保留门」：

锁定的不变量：

1. **默认拒写**：目标是**非空** text_clean 且新值不同 → 不写、旧值原地保留，
   该段 integrity 记「待人工裁决」（`text_clean_preserve_gate` 键），审计日志落
   pending_review 事件；
2. **显式越门才覆写且留痕**：`overwrite=True` / `--overwrite-text-clean` 才覆写，
   **旧值全文 + sha256** 落审计日志（JSONL）overwritten 事件，待裁决标记清除；
3. **空 text_clean 首写不拦**：run_rules / run_llm 对空段的写入与加门前逐字一致
   （不触发门、不留痕）——正常路径不许被拦死；
4. 越门后重跑幂等：值已干净 → 无新事件；
5. 内容质量门（guard_verdict）先于保留门：被 guard 拒的结果走 llm_rejected，
   不进保留门、不产生覆写裁决；
6. integrity 解析不出 JSON 对象时**不重写**别人的留痕（事件仍落审计日志）。

全程离线：llm_repair_batch 被 monkeypatch；库是 conftest 的临时 SQLite；
审计日志经 CLEAN_TEXT_GATE_LOG 隔离到 tmp_path，不碰真库、不碰真 DATA_DIR。
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import clean_text as ct  # noqa: E402
from app import config, db  # noqa: E402
from app.models import Segment, Work  # noqa: E402

SEED_SOURCE = "test:seed-preserve"

# 干净的非空正文（polish/llm 造「已有正文」用）
CLEAN_OLD = "他推门进来，屋里没人，只有炉火还在低低地烧着，映得他半边脸发红。"
# 带空括号残留的 text_clean —— polish 一定尝试「覆写」它（规则可削）
RESIDUAL = CLEAN_OLD + "()"
POLISHED = ct.clean_rules(RESIDUAL)
# 带调拼音粘连段（needs_llm 认得出；guard 放行、保留门拦得住的合法修复对）
DIRTY = "一股股白sè雾气在虚空中浮现而出，把那点残存的暖意也一并吹散了。"
FIXED = DIRTY.replace("白sè", "白色")
RULES_DIRTY = "(手打中文网7*24小时不间断更新纯txt手打小说m)他推门进来。"


def _db_path() -> str:
    url = config.DATABASE_URL
    assert url.startswith("sqlite:///"), f"本文件只适配临时 SQLite 库：{url}"
    return url[len("sqlite:///"):]


def _raw_row(seg_id: str) -> tuple[str | None, str | None]:
    """绕过 ORM 直读已提交行：(text_clean, integrity)。"""
    con = sqlite3.connect(_db_path())
    try:
        row = con.execute(
            "SELECT text_clean, integrity FROM segments WHERE id=?", (seg_id,)).fetchone()
        return (row[0], row[1]) if row else (None, None)
    finally:
        con.close()


def _events() -> list[dict]:
    p = ct.gate_log_path()
    if not p.exists():
        return []
    return [json.loads(line) for line in
            p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _events_for(seg_id: str) -> list[dict]:
    return [e for e in _events() if e.get("segment_id") == seg_id]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _reset_stat():
    """_stat 是模块级累加器，每个用例从零计。"""
    for k in ct._stat:
        ct._stat[k] = 0
    yield


@pytest.fixture(autouse=True)
def _isolated_gate_log(tmp_path, monkeypatch):
    """审计日志隔离到本用例 tmp_path——不写进共享 DATA_DIR，更不碰真库数据目录。"""
    monkeypatch.setenv(ct.GATE_LOG_ENV, str(tmp_path / "gate_audit.jsonl"))
    yield


@pytest.fixture(autouse=True)
def _fresh_seed():
    """每个用例前后清掉本文件种下的段，跨用例事件/行断言才可绝对计数。"""
    def _wipe():
        db.init_db()
        with db.session() as s:
            ids = [w.id for w in s.query(Work).filter(Work.source == SEED_SOURCE)]
            if ids:
                s.query(Segment).filter(Segment.work_id.in_(ids)).delete(
                    synchronize_session=False)
                s.query(Work).filter(Work.source == SEED_SOURCE).delete(
                    synchronize_session=False)
                s.commit()
    _wipe()
    yield
    _wipe()


def _seed(text: str, clean: str | None = None, integrity: str | None = None) -> str:
    db.init_db()
    with db.session() as s:
        w = Work(title="t-preserve", source=SEED_SOURCE)
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text=text, text_clean=clean,
                      n_sentences=1, n_chars=len(text), integrity=integrity)
        s.add(seg)
        s.commit()
        return str(seg.id)


def _install_llm(monkeypatch, mapping: dict[str, str | None]) -> None:
    """把 llm_repair_batch 换成查表假函数；未列入表的输入原样返回（幂等）。"""
    monkeypatch.setattr(ct, "llm_repair_batch",
                        lambda texts: [mapping.get(t, t) for t in texts])


def _integrity_of(seg_id: str) -> dict:
    raw = _raw_row(seg_id)[1]
    d = json.loads(raw) if (raw or "").strip() else {}
    assert isinstance(d, dict)
    return d


def test_preconditions_pinned():
    """前提自检（不成立则整个文件的构造失效，必须先红在这里）：
    RESIDUAL 确实是 polish 会改的对象、DIRTY/FIXED 确实走 guard accept + 保留门。"""
    assert POLISHED == CLEAN_OLD and POLISHED != RESIDUAL      # polish 必尝试改写非空正文
    assert ct.needs_llm(DIRTY) and not ct.needs_llm(FIXED)
    assert ct.guard_verdict(DIRTY, FIXED) == "accept"          # guard 放行 → 轮到保留门


# ── ① 非空 text_clean 不被默认覆写 ─────────────────────────────

def test_polish_default_blocks_nonempty_overwrite():
    sid = _seed(CLEAN_OLD, clean=RESIDUAL, integrity='{"quote_integrity": 0.9}')
    r = ct.polish()
    assert r["gate_pending"] >= 1
    # 旧值原地保留（直读已提交行），且既有 integrity 留痕不被吞
    assert _raw_row(sid)[0] == RESIDUAL
    flags = _integrity_of(sid)
    assert flags["quote_integrity"] == 0.9                     # 别人的键原样在
    mark = flags[ct.PRESERVE_KEY]
    assert mark["status"] == "pending_review" and mark["via"] == "polish"
    assert mark["old_sha256"] == _sha(RESIDUAL)
    assert mark["proposed_sha256"] == _sha(POLISHED)
    ev = _events_for(sid)
    assert len(ev) == 1 and ev[0]["action"] == "pending_review"
    assert ev[0]["via"] == "polish" and ev[0]["old_sha256"] == _sha(RESIDUAL)


def test_llm_default_blocks_overwrite_of_nonempty(monkeypatch):
    """text_clean 已有非空正文时，guard 放行的合法修复也**不得默认覆写**。"""
    sid = _seed(FIXED, clean=DIRTY)
    _install_llm(monkeypatch, {DIRTY: FIXED})
    before = dict(ct._stat)
    ct.run_llm(conc=1)
    d = {k: ct._stat[k] - before[k] for k in ct._stat}
    assert d["gate_blocked"] >= 1
    assert _raw_row(sid)[0] == DIRTY                           # 旧值一字未动
    flags = _integrity_of(sid)
    assert flags[ct.PRESERVE_KEY]["via"] == "llm"
    assert flags[ct.PRESERVE_KEY]["proposed_sha256"] == _sha(FIXED)
    ev = _events_for(sid)
    assert len(ev) == 1 and ev[0]["action"] == "pending_review" and ev[0]["via"] == "llm"


def test_guard_rejection_never_reaches_preserve_gate(monkeypatch):
    """两门分工：被内容质量门（guard_verdict）拒的结果根本不进保留门——
    计 llm_rejected、不产生覆写裁决（无待人工裁决标记、无审计事件）。"""
    sid = _seed(FIXED, clean=DIRTY)
    _install_llm(monkeypatch, {DIRTY: "已修复"})
    before = dict(ct._stat)
    ct.run_llm(conc=1)
    d = {k: ct._stat[k] - before[k] for k in ct._stat}
    assert d["llm_rejected"] >= 1 and d["gate_blocked"] == 0
    assert _raw_row(sid)[0] == DIRTY
    assert ct.PRESERVE_KEY not in _integrity_of(sid)
    assert _events_for(sid) == []


# ── ② 显式越门才覆写，且旧值留痕 ───────────────────────────────

def test_polish_overwrite_flag_writes_and_traces_old_value():
    sid = _seed(CLEAN_OLD, clean=RESIDUAL)
    r = ct.polish(overwrite=True)
    assert r["polished"] >= 1 and r["gate_overwritten"] >= 1
    assert _raw_row(sid)[0] == POLISHED                        # 越门才覆写
    ev = _events_for(sid)
    assert len(ev) == 1 and ev[0]["action"] == "overwritten"
    assert ev[0]["old_value"] == RESIDUAL                      # 旧值**全文**留痕
    assert ev[0]["old_sha256"] == _sha(RESIDUAL)
    assert ev[0]["new_sha256"] == _sha(POLISHED)
    # 越门重跑幂等：值已干净，不再产生新事件
    ct.polish(overwrite=True)
    assert len(_events_for(sid)) == 1


def test_blocked_then_overwrite_clears_pending_marker():
    """默认被拒 → 记待人工裁决；随后显式越门 → 落新值并**清除**待裁决标记。"""
    sid = _seed(CLEAN_OLD, clean=RESIDUAL)
    ct.polish()
    assert _integrity_of(sid)[ct.PRESERVE_KEY]["status"] == "pending_review"
    ct.polish(overwrite=True)
    assert _raw_row(sid)[0] == POLISHED
    assert ct.PRESERVE_KEY not in _integrity_of(sid)
    actions = [e["action"] for e in _events_for(sid)]
    assert actions == ["pending_review", "overwritten"]


def test_llm_overwrite_flag_overwrites_with_old_value_trace(monkeypatch):
    sid = _seed(FIXED, clean=DIRTY)
    _install_llm(monkeypatch, {DIRTY: FIXED})
    before = dict(ct._stat)
    ct.run_llm(conc=1, overwrite=True)
    d = {k: ct._stat[k] - before[k] for k in ct._stat}
    assert d["llm_ok"] >= 1 and d["gate_overwritten"] >= 1
    assert _raw_row(sid)[0] == FIXED
    ev = _events_for(sid)
    assert len(ev) == 1 and ev[0]["action"] == "overwritten" and ev[0]["via"] == "llm"
    assert ev[0]["old_value"] == DIRTY and ev[0]["old_sha256"] == _sha(DIRTY)


def test_cli_flag_parse_default_off():
    assert ct.parse_args(["--llm"]).overwrite_text_clean is False
    assert ct.parse_args(["--polish"]).overwrite_text_clean is False
    assert ct.parse_args(["--llm", "--overwrite-text-clean"]).overwrite_text_clean is True


def test_main_polish_cli_default_blocks_and_flag_overwrites(capsys):
    """CLI 端到端：`--polish` 默认拒写留标记；带越门 flag 才改写并留旧值痕。"""
    sid = _seed(CLEAN_OLD, clean=RESIDUAL)
    ct.main(["--polish"])
    out = json.loads(capsys.readouterr().out.splitlines()[0])
    assert out["gate_pending"] >= 1
    assert _raw_row(sid)[0] == RESIDUAL
    ct.main(["--polish", "--overwrite-text-clean"])
    out2 = json.loads(capsys.readouterr().out.splitlines()[0])
    assert out2["gate_overwritten"] >= 1
    assert _raw_row(sid)[0] == POLISHED
    assert [e["action"] for e in _events_for(sid)] == ["pending_review", "overwritten"]


# ── ③ 空 text_clean 仍按原口径写入（正常路径不拦死）─────────────

def test_run_rules_first_write_unchanged_and_never_overwrites():
    sid_new = _seed(RULES_DIRTY)                                # 空：照常首写
    sid_kept = _seed(RULES_DIRTY.replace("他推门进来。", "既有正文一个字都不该动。"),
                     clean="既有正文一个字都不该动。")
    r = ct.run_rules()
    assert r["rule_cleaned"] >= 1
    assert _raw_row(sid_new)[0] == ct.clean_rules(RULES_DIRTY)
    assert ct.PRESERVE_KEY not in _integrity_of(sid_new)       # 首写不触发门
    assert _raw_row(sid_kept)[0] == "既有正文一个字都不该动。"  # :289 continue 依旧挡非空
    assert _events() == []                                     # 空写入不留痕（原口径）


def test_llm_first_write_to_empty_not_blocked(monkeypatch):
    sid = _seed(DIRTY)                                          # text_clean=None
    _install_llm(monkeypatch, {DIRTY: FIXED})
    before = dict(ct._stat)
    ct.run_llm(conc=1)
    d = {k: ct._stat[k] - before[k] for k in ct._stat}
    assert d["llm_ok"] >= 1 and d["gate_blocked"] == 0
    assert _raw_row(sid)[0] == FIXED                            # 首写按原口径落库
    assert _events_for(sid) == []


def test_preserve_gate_verdicts_unit():
    """preserve_gate_write 四态穷尽：write / identical / blocked / overwritten。"""
    sid_ws = _seed(CLEAN_OLD, clean="   ")                     # 空白视同空：不拦
    sid_full = _seed(CLEAN_OLD, clean=RESIDUAL)
    with db.session() as s:
        seg_ws = s.get(Segment, sid_ws)
        seg_full = s.get(Segment, sid_full)
        assert ct.preserve_gate_write(seg_ws, FIXED, overwrite=False, via="unit") == "write"
        assert ct.preserve_gate_write(seg_full, RESIDUAL, overwrite=False, via="unit") \
            == "identical"                                     # 同值幂等：不算事件
        assert ct.preserve_gate_write(seg_full, FIXED, overwrite=False, via="unit") \
            == "blocked"
        assert ct.preserve_gate_write(seg_full, FIXED, overwrite=True, via="unit") \
            == "overwritten"
        s.commit()
    assert [e["action"] for e in _events_for(sid_full)] == ["pending_review", "overwritten"]
    assert _events_for(sid_ws) == []


def test_unparseable_integrity_never_rewritten(monkeypatch):
    """integrity 不是 JSON 对象 → 保留门**不重写**它（不猜别人的留痕），
    但「待人工裁决」事件必须仍全量落审计日志。"""
    sid = _seed(CLEAN_OLD, clean=RESIDUAL, integrity="历史遗留的自由文本")
    ct.polish()
    assert _raw_row(sid)[0] == RESIDUAL
    assert _raw_row(sid)[1] == "历史遗留的自由文本"             # 逐字未动
    ev = _events_for(sid)
    assert len(ev) == 1 and ev[0]["action"] == "pending_review"


def test_gate_log_default_path_follows_data_dir(monkeypatch):
    monkeypatch.delenv(ct.GATE_LOG_ENV, raising=False)
    assert ct.gate_log_path() == config.DATA_DIR / ct.GATE_LOG_NAME
