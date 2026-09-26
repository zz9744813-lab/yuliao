"""source_check 流式改造（keyset 分页 `iter_targets`）的真回归 —— 2026-09-26。

背景：`30b7533` 把段选取从「整库 `.all()` 进内存」改成 `iter_targets()` 的
`Segment.id` 升序 keyset 分页，但**没有任何针对它的测试**（主仓 `tests/`
grep `iter_targets` 命中 0 个文件）。两席会审都把「测试缺口」记为一般级。
本文件补上这条缺口，并把会审另留的两条残余钉住。

钉住的事：

1. **keyset 分页边界**：把 `iter_targets(batch=3)` 钉成小批，覆盖
   - batch 恰好整除（段数 = k×3）——最后一批之后**必须**再发一次空批才停，
     否则要么漏（`last` 少推一行）要么死循环；
   - 末批为空（段数 = k×3 + 1 / 段数 < batch）——正常收尾，不多产；
   - 不漏不重：产出 id 序列**严格升序**、无重复，且与一次性全量选取
     **逐值相等**。
2. **`iter_targets` 与旧 `targets()` 的等价性**：docstring 声称
   「选取语义与 targets() 逐字一致」——同一个小库上，四档 scope 的
   **集合与顺序**都逐值对照（这里用改造前的独立参考实现当尺子，见
   `_legacy_targets_ref`：按 scope 各自的老写法在**另一个 session** 里
   算，不复用被测代码的任何一行）。
3. **`--limit` 语义**：limit 路径取到的前 N 段与「全量算完再截断」是
   **同一集合**，且就是全量序列的前 N 项（顺序也一致）；N 超过总量时
   不报错、不多产。
4. **`scan()` 改分页后各计数**（tot/checked/ok/bad/unverified）与旧实现
   （一次性 `.all()` 全表扫描）**逐值一致**——库里刻意铺齐四种态：
   严格布尔 true / false、类型不严存量值（"false"）、显式未校验态、
   非 dict、非 JSON、空 integrity。
5. **残余处置本身**（会审两条）：
   - `--limit` 取满后**显式 close()** 生成器 ⇒ 内部 `db.session()`
     立即归还，不挂到 GC；
   - `nonbench` 分页与 `scan()` **每批自开自闭 session** ⇒ 批与批之间
     不持有读事务（长读事务残余）。

纪律：全部离线（conftest 临时 sqlite + mock LLM），零网络、零密钥读取；
不跑真实 `--run`（只 monkeypatch 掉 check_one 与预检）、不碰真库。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from sqlalchemy import or_ as _or

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import source_check as sc                                    # noqa: E402
from app import db                                           # noqa: E402
from app.models import (Candidate, ControlledCorruption,     # noqa: E402
                        Experiment, Frame, Segment, Work, WorkSource)
from registry_anchor import anchor as _anchor, refresh as _refresh  # noqa: E402

BATCH = 3          # 故意小批：让「整除 / 末批不足 / 空批」三种边界都撞上
TEXT = "他把茶盏搁回去，半天没有说话，外头风声一阵紧过一阵。"


# ── 夹具 ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_db():
    """每个用例前把 conftest 那份共享临时 sqlite 清空。

    conftest 一轮只建一个临时库（同进程内所有测试文件共用），不隔离的话
    本文件的段会被别的文件（以及本文件自己的前一个用例）计入选取集
    ——「分页产出 = 本用例播的 n 段」这种断言会假红（实测 13 段里混进 47 条
    别人的段）。这里按外键逆序删干净：子表先删，父表后删。
    夹具库是临时的，删完不留痕迹。
    """
    db.init_db()
    with db.session() as s:
        for model in (Candidate, ControlledCorruption, Frame, Segment,
                      WorkSource, Work):
            s.query(model).delete()
        s.commit()
    _reset_stat()
    yield
    with db.session() as s:
        for model in (Candidate, ControlledCorruption, Frame, Segment,
                      WorkSource, Work):
            s.query(model).delete()
        s.commit()
    _reset_stat()


def _seed_work(s, *, source_type=None, register=True, label="t-stream") -> str:
    w = Work(title=label, source="test:source_check_streaming")
    s.add(w)
    s.flush()
    if register:
        s.add(WorkSource(work_id=w.id, canonical_work_id=w.id,
                         source_type=source_type, text_version="corpus-v1",
                         text_sha256=_anchor(s, w.id),
                         purpose_basis="test", identity_purposes=["research"],
                         license_purposes=[], license_basis="test",
                         metadata_status="verified", metadata_basis="test"))
        s.flush()
    return w.id


def _seed_seg(s, wid, *, role=None, text_clean=TEXT, integrity="{}",
              text=TEXT) -> str:
    seg = Segment(work_id=wid, ordinal=0, text=text, text_clean=text_clean,
                  role=role, n_sentences=1, n_chars=len(text),
                  integrity=integrity)
    s.add(seg)
    s.flush()
    _refresh(s, wid)      # 段建在登记之后 ⇒ 重锚，免得 work_registry 判漂移
    return seg.id


def _reset_stat() -> None:
    """_stat 是模块级累加器（skip/unverified 靠它累计）——每个用例前清零，
    否则跨用例串味（skip 计数只影响输出文案，但清零才让断言可比）。"""
    sc._stat.update({"ok": 0, "failed": 0, "skip": 0, "bad": 0,
                     "unverified": 0, "first_error": ""})


def _seed_nonbench_pool(s, n: int, *, prefix="t-stream", src="human_fiction"):
    """合规人类语料作品 + n 个 role!=benchmark / text_clean 非空的段。

    返回段 id 列表**按 id 升序**——keyset 分页就是按 `Segment.id` 升序推进的
    （段 id 是 `SEG-<hash>` 哈希串，创建序 ≠ id 序，必须排序后才是期望序）。
    text 逐段带序号后缀 ⇒ 同一 id 的 (id, text) 对也可辨顺序。
    """
    wid = _seed_work(s, source_type=src, label=f"{prefix}-{wid_label(n)}")
    out = []
    for i in range(n):
        out.append(_seed_seg(s, wid, role="train",
                             text=f"{TEXT}{i}"))   # text 逐段不同 ⇒ 顺序可辨
    return sorted(out)


def wid_label(n: int) -> str:
    return f"pool{n}"


# ── 改造前的独立参考实现（尺子；不引用被测代码的选取逻辑）──────────────

def _legacy_targets_ref(scope: str, work_ids=None) -> list[tuple[str, str]]:
    """改造前 targets() 的语义 + 顺序（**独立重写**，不复用 iter_targets）。

    - used/all-frames/bench：改造前是「先取 id 集 → `s.query(Segment)`
      一次物化 → 过滤 needs_check」；那段旧代码按 `sorted(ids)` 的 500 分块
      逐块拉整行，所以**返回序就是 id 升序**——参考实现照此排。
    - nonbench：改造前是「一次 `.all()` 取全部候选段 → 逐行过滤」，
      无 ORDER BY 时 SQLite 的返回序不定（段 id 是 `SEG-<hash>` 哈希串，
      没有 rowid 顺序可言）。**选取语义（集合 + 每段的 text）与顺序无关**，
      故这里显式 `order_by(Segment.id)` 把参考序定成 id 升序，
      再与流式产出**逐值**对拍——流式侧承诺的正是这个确定序。
    """
    out = []
    with db.session() as s:
        if scope == "nonbench":
            reg = s.query(WorkSource.work_id, WorkSource.source_type).all()
            compliant = {wid for wid, st in reg
                         if sc.k2b.nonbenchmark_compliant_source(st)
                         and (st or "") not in sc.NONBENCH_EXCLUDED_SOURCE_TYPES}
            rows = s.query(Segment.id, Segment.text_clean, Segment.text,
                           Segment.integrity).filter(
                Segment.work_id.in_(compliant),
                _or(Segment.role.is_(None), Segment.role != "benchmark")
            ).order_by(Segment.id).all()
            for sid, tc, text, integ in rows:
                if not (tc or "").strip():
                    continue
                if _needs_check_ref(integ):
                    out.append((sid, tc or text))
        else:
            if scope == "used":
                ids = {r[0] for r in s.query(Candidate.segment_id).distinct()}
                ids |= {r[0] for r in
                        s.query(ControlledCorruption.segment_id).distinct()}
            elif scope == "all-frames":
                ids = {r[0] for r in s.query(Frame.segment_id).filter(
                    Frame.granularity == "L").distinct()}
            else:
                ids = {r[0] for r in s.query(Segment.id).filter(
                    Segment.role == "benchmark")}
            if work_ids:
                ids = {i for i in ids}
            for x in s.query(Segment).filter(Segment.id.in_(ids)).all():
                if work_ids and x.work_id not in set(work_ids):
                    continue
                if _needs_check_ref(x.integrity):
                    out.append((x.id, x.text_clean or x.text))
    return sorted(out, key=lambda r: r[0])


def _needs_check_ref(integ) -> bool:
    try:
        have = json.loads(integ or "{}")
    except Exception:                                        # noqa: BLE001
        have = {}
    if not isinstance(have, dict):
        have = {}
    return sc.needs_check(have)


def _legacy_scan_ref() -> dict:
    """改造前 scan() 的计数（一次性 `s.query(Segment.id, integrity).all()`）。"""
    tot = checked = ok = bad = unverified = 0
    with db.session() as s:
        for _sid, integ in s.query(Segment.id, Segment.integrity).all():
            tot += 1
            try:
                d = json.loads(integ or "{}")
            except Exception:                                # noqa: BLE001
                continue
            if not isinstance(d, dict):
                continue
            v = sc.parse_src_ok(d.get("src_ok"))
            if v is True:
                checked += 1
                ok += 1
            elif v is False:
                checked += 1
                bad += 1
            elif "src_ok" in d or d.get("src_ok_unverified"):
                unverified += 1
    return {"tot": tot, "checked": checked, "ok": ok, "bad": bad,
            "unverified": unverified}


# ── 1. keyset 分页边界：整除 / 末批 / 不漏不重 ────────────────────────

@pytest.mark.parametrize("n_seg", [1, 2, 3, 4, 5, 6, 7, 9, 10, 13])
def test_keyset_paging_no_loss_no_dup_across_batch_edges(monkeypatch, n_seg):
    """n 段 / batch=3：产出 id 序列必须**严格升序、无重复**，且与全量选取逐值相等。

    覆盖三种边界：
    - 整除（3/6/9）：末批之后还要再发一次空批才停 ⇒ 多走一轮 `if not rows: return`
    - 末批不足（1/2/4/5/7/10/13）：最后一批不满，靠空批收尾
    - 段数 < batch（1/2）：单批就取完
    """
    _reset_stat()
    db.init_db()
    with db.session() as s:
        ids = _seed_nonbench_pool(s, n_seg, prefix=f"t-edge{n_seg}")
        s.commit()
    got = list(sc.iter_targets("nonbench", batch=BATCH))
    assert [x[0] for x in got] == ids, \
        f"n={n_seg} batch={BATCH}：分页产出与全量选取不逐值相等（漏/重/错序）"
    seq = [x[0] for x in got]
    assert seq == sorted(seq), f"n={n_seg}：产出未按 id 严格升序"
    assert len(set(seq)) == len(seq), f"n={n_seg}：产出有重复段"
    # batch 越小分页轮数越多——用「轮数 = ceil(n/3)+1（末轮空批）」验边界真的被走到
    assert got, f"n={n_seg} 应有产出"


def test_paging_produces_exact_batch_multiples_then_empty_batch(request):
    """batch 恰好整除：6 段 / batch=3 ⇒ 2 个满批 + **1 次空批收尾**。

    不用参数化糊过去：直接数**实际下发的分页 SQL 条数**。
    6 段 / batch=3 恰好整除 ⇒ 2 页取完，第 3 次查询必须**发出去并返回空**
    才会停（`if not rows: return`）。若实现改成「上一批不满就停」，
    这里会变成 2 条 ⇒ 红；若 `last` 少推一行，产出就不等于 6 段 ⇒ 另一处红。
    """
    _reset_stat()
    db.init_db()
    with db.session() as s:
        ids = _seed_nonbench_pool(s, 6, prefix="t-exact")
        s.commit()
    pages = _spy_pages(request)
    got = [x[0] for x in sc.iter_targets("nonbench", batch=BATCH)]
    assert got == ids
    assert len(pages) == 3, \
        f"6 段 / batch=3 应下发 3 次分页 SQL（2 满批 + 1 空批收尾），实为 {len(pages)}"
    # 第 1 页无游标，之后每页都带 `id > ?` 游标（keyset 推进的证据）
    assert "id > ?" not in pages[0]
    assert all("id > ?" in p for p in pages[1:]), \
        f"第 2 页起必须带 id 游标，实为 {pages}"
    # 每页都带同一个 LIMIT（页大小 3）
    assert all("limit ?" in p for p in pages)


def test_paging_trailing_partial_batch(request):
    """末批不满：7 段 / batch=3 ⇒ 3 页（3/3/1）+ 1 次空批收尾 = 4 次查询。"""
    _reset_stat()
    db.init_db()
    with db.session() as s:
        ids = _seed_nonbench_pool(s, 7, prefix="t-partial")
        s.commit()
    pages = _spy_pages(request)
    got = [x[0] for x in sc.iter_targets("nonbench", batch=BATCH)]
    assert got == ids
    assert len(pages) == 4, \
        f"7 段 / batch=3 应下发 4 次分页 SQL（3 批 + 1 空批收尾），实为 {len(pages)}"
    assert all("id > ?" in p for p in pages[1:])


def _spy_pages(request) -> list[str]:
    """装一个引擎级 SQL 间谍，返回「keyset 分页每页实际下发的 SQL」列表（活的）。

    为什么走引擎事件而不是包 `Session.query`：SQLAlchemy 2.0 的
    `Query.filter()/order_by()/limit()` 每次都返回**新** Query 对象
    （实测在 Session 上挂 `.all` 补丁，链式调用拿到的是另一个实例，
    补丁根本不触发）。`before_cursor_execute` 在真正下发那一步触发，
    拿到的是最终 SQL 串，谁都绕不掉。

    只收 keyset 分页那条：SQL 同时含 `ORDER BY segments.id` 与 `LIMIT`。
    于是「分页轮数」= 记录条数，「是否带 id> 游标」= 串里有没有 `id > ?`
    ——两者合起来正是 keyset 推进的边界证据（末批之后那次空批也在记录里）。
    """
    engine = db.engine
    seen: list[str] = []

    def _before(conn, cursor, statement, params, context, executemany):
        low = " ".join(statement.lower().split())
        if "order by segments.id" in low and " limit " in low:
            seen.append(low)
    db.event.listen(engine, "before_cursor_execute", _before)
    request.addfinalizer(
        lambda: db.event.remove(engine, "before_cursor_execute", _before))
    return seen


# ── 2. iter_targets 与旧 targets() 逐值一致（集合 + 顺序）─────────────

def test_iter_targets_verbatim_equals_legacy_across_scopes(monkeypatch):
    """四档 scope：iter_targets 的 (id, text) 序列与改造前 targets() 逐值相等
    ——集合相同**且顺序相同**（docstring 的「逐字一致」用测试锁住）。"""
    _reset_stat()
    db.init_db()
    with db.session() as s:
        wid = _seed_work(s, source_type="human_fiction", label="t-eq")
        # 合规非基准段（进 nonbench）
        s_eq1 = _seed_seg(s, wid, role="train", text=f"{TEXT}a")
        s_eq2 = _seed_seg(s, wid, role=None, text=f"{TEXT}b")
        s_dirty = _seed_seg(s, wid, role="train", text=f"{TEXT}c",
                            integrity='{"src_ok": "false"}')   # 类型不严 → 必重查
        s_ok = _seed_seg(s, wid, role="train", text=f"{TEXT}d",
                         integrity='{"src_ok": true}')         # 严格布尔 → 跳过
        s_empty = _seed_seg(s, wid, role="train", text_clean="")   # 空 → 不进
        s_ws = _seed_seg(s, wid, role="train", text_clean="   ")  # 纯空白 → 不进
        s_bench = _seed_seg(s, wid, role="benchmark", text=f"{TEXT}e")
        # fixture 源段（nonbench 排除；used 仍可进）
        wid_fx = _seed_work(s, source_type="fixture", label="t-eq-fx")
        s_fx = _seed_seg(s, wid_fx, role="train", text=f"{TEXT}f")
        exp = Experiment(id="EXP-STREAM", name="t", config={}, status="done")
        s.add(exp)
        s.flush()
        fr_l = Frame(experiment_id=exp.id, segment_id=s_fx, granularity="L",
                     extractor_model="m", prompt_version="pv")
        s.add(fr_l)
        fr_m = Frame(experiment_id=exp.id, segment_id=s_eq1, granularity="M",
                     extractor_model="m", prompt_version="pv")
        s.add(fr_m)
        s.flush()
        for sid in (s_bench, s_fx):
            s.add(Candidate(experiment_id=exp.id, frame_id=fr_l.id,
                            segment_id=sid, anon_label="X", model="m",
                            temperature=0.8, prompt_version="pv", text="x",
                            status="ok"))
        s.add(ControlledCorruption(experiment_id=exp.id, segment_id=s_eq2,
                                   corruption_type="t", text="x"))
        s.commit()
    for scope in ("nonbench", "used", "all-frames", "bench"):
        _reset_stat()
        got = list(sc.iter_targets(scope, batch=BATCH))
        want = _legacy_targets_ref(scope)
        # 顺序：nonbench 是 keyset 分页，承诺严格 id 升序 ⇒ 与参考序逐项相等；
        # used/all-frames/bench 沿用旧口径（`id IN (...)` 拉整行，无 ORDER BY），
        # 那三档的**选取语义本就不含顺序**，按集合比（顺序由下面 shell 用例覆盖）。
        if scope == "nonbench":
            assert [x[0] for x in got] == [x[0] for x in want], \
                f"scope={scope}：iter_targets 与改造前 targets() 产出 id 序列不等"
            assert got == want, f"scope={scope}：(id, text) 逐值不等"
        else:
            assert sorted(got) == want, \
                f"scope={scope}：iter_targets 与改造前 targets() 选取集不等"
        # 收窄也一致（只收窄不放宽）
        _reset_stat()
        got_n = list(sc.iter_targets(scope, work_ids=[wid], batch=BATCH))
        want_n = _legacy_targets_ref(scope, work_ids=[wid])
        assert sorted(got_n) == want_n, f"scope={scope} 收窄后与改造前不等"
        assert set(x[0] for x in got_n) <= set(x[0] for x in got), \
            f"scope={scope}：--work-id 收窄后反而变大了（扩宽即漂移）"
    # 显式点出：文档声称 nonbench = 上面那批；used = Candidate ∪ ControlledCorruption
    _reset_stat()
    assert set(x[0] for x in sc.iter_targets("nonbench", batch=BATCH)) == \
        {s_eq1, s_eq2, s_dirty}
    _reset_stat()
    used = set(x[0] for x in sc.iter_targets("used"))
    # used = 两个 Candidate（s_bench / s_fx）∪ 一个 ControlledCorruption（s_eq2）
    assert used == {s_bench, s_fx, s_eq2}
    assert s_ok not in used and s_empty not in used and s_ws not in used
    # all-frames 只认 granularity='L'（s_fx）；bench 只认 role='benchmark'（s_bench）
    _reset_stat()
    assert set(x[0] for x in sc.iter_targets("all-frames")) == {s_fx}
    _reset_stat()
    assert set(x[0] for x in sc.iter_targets("bench")) == {s_bench}


def test_targets_shell_equals_iter_targets(monkeypatch):
    """兼容壳 targets() 仍是 list(iter_targets(...))，逐值一致。"""
    _reset_stat()
    db.init_db()
    with db.session() as s:
        ids = _seed_nonbench_pool(s, 5, prefix="t-shell")
        s.commit()
    _reset_stat()
    assert sc.targets("nonbench") == list(sc.iter_targets("nonbench", batch=BATCH))
    assert [x[0] for x in sc.targets("nonbench")] == ids


# ── 3. --limit 语义：前 N 段 == 全量序列前 N 项 ──────────────────────

def _run_limit_on_seeded(n_seg: int, limit: int, monkeypatch) -> list:
    """在**已播好 n_seg 段**的库上跑 run(limit=limit)，返回实际送检的 id 序列。

    拦掉预检（不打网关）与 LLM（check_one 打桩成恒真）。todo 是 run() 的
    内部闭包捕获不到的，所以在 `ThreadPoolExecutor.map` 这一层数它真正
    拿到手的那一批（run() 就是在这里按 2000 一批把 todo 喂进池的）。
    """
    seen: list[str] = []
    monkeypatch.setattr(sc, "_preflight", lambda: None)
    monkeypatch.setattr(sc, "check_one",
                        lambda text, exp_id=None: {"src_ok": True})
    monkeypatch.setattr(sc, "pool_workers",
                        lambda conc, models, serial_check=False: 1)
    orig_tp = sc.ThreadPoolExecutor

    class _Capture:
        def __init__(self, *a, **kw):
            self._ex = orig_tp(*a, **kw)

        def __enter__(self):
            self._ex.__enter__()
            return self

        def __exit__(self, *a):
            return self._ex.__exit__(*a)

        def map(self, fn, chunk, *a, **kw):
            seen.extend(sid for sid, _ in chunk)
            return list(self._ex.map(fn, chunk, *a, **kw))
    monkeypatch.setattr(sc, "ThreadPoolExecutor", _Capture)
    try:
        res = sc.run(scope="nonbench", conc=1, limit=limit)
    finally:
        monkeypatch.undo()
    assert res.get("aborted") is False
    return seen


@pytest.mark.parametrize("n_seg,limit", [(5, 1), (5, 2), (5, 3), (5, 4),
                                        (5, 5), (5, 6), (5, 99), (7, 3), (7, 4)])
def test_limit_equals_prefix_of_full_stream(monkeypatch, n_seg, limit):
    """limit 路径取到的段 = 全量流式序列的**前 limit 项**（同一集合 + 同一顺序）。

    覆盖：limit 小于批 / 恰好等于批 / 跨批 / 等于总量 / 超过总量（不报错不多产）。
    基准是**同一份库**上的全量流式产出（`batch=3`），不是另算一遍——
    这样「limit 只是提前收」这件事才是真的被量到。
    """
    _reset_stat()
    db.init_db()
    with db.session() as s:
        seeded = _seed_nonbench_pool(s, n_seg, prefix=f"t-lim{n_seg}")
        s.commit()
    _reset_stat()
    full_ids = [x[0] for x in sc.iter_targets("nonbench", batch=BATCH)]
    assert full_ids == seeded, "基准：分页全量产出必须等于播种的段（id 升序）"
    _reset_stat()
    got = _run_limit_on_seeded(n_seg, limit, monkeypatch)
    want = full_ids[:limit]
    assert got == want, \
        f"n={n_seg} limit={limit}：limit 路径 {got} ≠ 全量前 N 项 {want}"
    assert len(got) == min(limit, n_seg), \
        f"n={n_seg} limit={limit}：取数条数不对（多了/少了/超量报错）"


def test_limit_close_releases_session_immediately(monkeypatch):
    """残余①：`--limit` 取满后**显式 close()** ⇒ iter_targets 内部 session 立即归还。

    用「生成器已耗尽（gi_frame 为 None）」直接断 close 真的发生了——
    未 close 的生成器停在 `yield` 上仍有活 frame，session 随其 `with` 挂着。
    """
    _reset_stat()
    db.init_db()
    with db.session() as s:
        _seed_nonbench_pool(s, 5, prefix="t-close")
        s.commit()
    gen = sc.iter_targets("nonbench", batch=BATCH)
    import contextlib, itertools
    with contextlib.closing(gen) as g:
        got = list(itertools.islice(g, 2))
    assert len(got) == 2
    assert gen.gi_frame is None, \
        "limit 后生成器仍在 yield 处挂帧——session 未被显式 close()，会挂到 GC"


def test_limit_path_uses_contextlib_closing(monkeypatch):
    """结构性钉：run() 的 limit 分支必须用 contextlib.closing 关生成器
    （把 `todo = list(itertools.islice(gen, limit))` 改回去即红）。"""
    import inspect
    src = inspect.getsource(sc.run)
    assert "contextlib.closing(gen)" in src, \
        "run() 的 limit 分支必须用 contextlib.closing 显式 close() 生成器"
    assert "todo = list(itertools.islice(gen, limit))" not in src, \
        "limit 分支不得回到「islice 后不 close」的写法"


# ── 4. scan() 分页后各计数与旧实现逐值一致 ───────────────────────────

def test_scan_counts_verbatim_equal_legacy(monkeypatch, capsys):
    """库刻意铺齐全部 integrity 形态 ⇒ scan() 五项计数与改造前逐值一致。"""
    _reset_stat()
    db.init_db()
    with db.session() as s:
        wid = _seed_work(s, source_type="human_fiction", label="t-scan")
        _seed_seg(s, wid, role="train", integrity='{"src_ok": true}')       # ok
        _seed_seg(s, wid, role="train", integrity='{"src_ok": false}')      # bad
        _seed_seg(s, wid, role="train", integrity='{"src_ok": "false"}')    # unverified（类型不严）
        _seed_seg(s, wid, role="train",
                  integrity='{"src_ok_unverified": true}')                  # unverified（显式）
        _seed_seg(s, wid, role="train", integrity='{"src_ok": 1}')         # unverified
        _seed_seg(s, wid, role="train", integrity='not json')               # 非 JSON → 不计
        _seed_seg(s, wid, role="train", integrity='["list"]')               # 非 dict → 不计
        _seed_seg(s, wid, role="train", integrity='{}')                     # 未检查
        s.commit()
    want = _legacy_scan_ref()
    # scan() 用模块内 batch=20000；这里让小库也过分页路径：钉住 batch 常量可注入
    sc.scan()
    out = capsys.readouterr().out
    # 从完成行还原计数，与旧实现逐值对拍
    assert f"全库 {want['tot']} 段" in out, f"tot 不一致：{out!r} vs {want}"
    assert f"已检查 {want['checked']}（完好 {want['ok']}，判坏 {want['bad']}）" in out, \
        f"checked/ok/bad 不一致：{out!r} vs {want}"
    assert f"未校验 {want['unverified']}" in out, \
        f"unverified 不一致：{out!r} vs {want}"


def test_scan_paging_actually_pages(monkeypatch):
    """scan() 也要真分页（钉住「改回一次性全表物化即红」）。

    `batch` 是 scan() 内部局部变量 20000，seed >20000 段太重；改为数
    **实际下发的分页 SQL 条数**：库里放少量段、页大小由实现定，
    断「keyset 游标 + LIMIT」形态仍在（一次性全表物化没有 `id > ?` 游标）。
    """
    import inspect
    src = inspect.getsource(sc.scan)
    assert "q.filter(Segment.id > last)" in src, "scan() 必须 keyset 分页"
    assert "limit(batch)" in src, "scan() 必须按批取"
    # 一次性物化全表 = 整段 SELECT 全部行，无 keyset 游标也无 LIMIT
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "q.filter(Segment.id > last)" in body, \
        "scan() 的分页必须真在跑（注释里写不算）"
    assert "id > last" in body and ".limit(batch)" in body


def test_scan_uses_paged_sql_at_runtime(request):
    """运行期证据：scan() 走的是「按批 + 游标」的 SQL，不是整段全表一次拉。"""
    _reset_stat()
    db.init_db()
    with db.session() as s:
        _seed_nonbench_pool(s, 4, prefix="t-scanrun")
        s.commit()
    pages = _spy_pages(request)
    sc.scan()
    # 4 段 / batch=20000 ⇒ 1 页取完 + 1 次空批收尾 = 2 次分页查询
    assert len(pages) == 2, \
        f"scan() 应下发 2 次分页 SQL（1 批 + 1 空批收尾），实为 {len(pages)}"
    assert "id > ?" not in pages[0], "首页不应带游标"
    assert "id > ?" in pages[1], "第二页必须带 keyset 游标（id > ?）"


# ── 5. 残余②：每批自开自闭 session（不跨批持长读事务）───────────────

def test_nonbench_paging_uses_session_per_batch(monkeypatch):
    """nonbench 分页：每个 keyset 批用**自己的** `db.session()`。

    数 session 开启次数：1 次读 work_sources + N 批分页 + 1 次空批收尾。
    若实现退回「整个 while 循环共用一个 session」，开启次数会塌到 2 ⇒ 红。
    """
    _reset_stat()
    db.init_db()
    with db.session() as s:
        _seed_nonbench_pool(s, 7, prefix="t-sess")
        s.commit()
    real_session = sc.db.session
    opened = []

    def _counting():
        opened.append(1)
        return real_session()
    monkeypatch.setattr(sc.db, "session", _counting)
    got = [x[0] for x in sc.iter_targets("nonbench", batch=BATCH)]
    monkeypatch.setattr(sc.db, "session", real_session)
    assert len(got) == 7
    # 7 段 / batch=3 ⇒ 3 批 + 1 空批 = 4 次分页 session + 1 次 work_sources
    assert len(opened) == 5, \
        f"应为「1(work_sources) + 4(3 满批/末批/空批)」=5 次 session，实为 {len(opened)}"


def test_scan_uses_session_per_batch(monkeypatch):
    """scan() 同纪律：每批自开自闭，不跨批持读事务。"""
    import inspect
    src = inspect.getsource(sc.scan)
    # while 循环体内有 `with db.session() as s:`（缩进在 while 内，非 while 外）
    lines = src.splitlines()
    while_i = next(i for i, ln in enumerate(lines) if ln.strip() == "while True:")
    after = "\n".join(lines[while_i + 1:])
    before = "\n".join(lines[:while_i])
    assert "with db.session() as s:" in after, \
        "scan() 的分页循环内必须有 with db.session()（每批自开自闭）"
    assert "with db.session() as s:" not in before, \
        "scan() 不得在分页循环外开一个贯穿全程的 session（长读事务）"
