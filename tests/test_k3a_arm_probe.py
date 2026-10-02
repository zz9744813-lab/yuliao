"""K3-A 臂对照探针回归（scripts/k3a_arm_probe.py）。

红线（任务书钉死，逐条对号）：
① **默认门 + 有效 book 时 `selected==0`**（生产口径原样，不碰任何常量）；
② **同一调用 `rejected==0`，但「被 status 门静默丢弃」名单非空** ⇒ 台账
   漏报这条事实被钉死（`ledger_under_reports`）；
③ 只读放宽 status 门后 `selected==8`（离线夹具逐条钉 8 / 真库钉「等于该
   库真实策略数」）且每条 `evidence_count>0`；
④ **放宽是局部且会恢复**：跑完探针后 `ELIGIBLE_STATUS` 与
   `eligible_statuses()` 逐字回到原值（且**作用域内**确实放宽过——防空跑）；
⑤ **零写入**：探针跑完库文件 mtime/内容指纹（sha256）逐字不变，目录不冒出
   `-wal`/`-shm` 新文件；连接层 `mode=ro` + `PRAGMA query_only=ON`。

两层夹具（互不替代）：
- **离线夹具**（`tmp_path_factory`，常跑）：按真库形态造 8 条 WORK 范围
  hypothesis 策略 + 真实证据实例（两条根作品、跨作品），把 ①~⑤ 全部**逐条
  确定钉死**——真库缺席/换库时回归仍然红得住。
- **真库**（`resolve_db` 候选，缺则 `pytest.skip`）：只读复跑
  `WK-6e5d2623`，按当前 status 实况解释两臂；历史 hypothesis 阶段的
  `selected=0` 断言只属于离线夹具，不能在卡升为 verified 后继续硬套。

另钉：缺 `--book` 时**如实报错退出**（rc=2，绝不猜一个作品）；`--out` 是唯一
的写；收据 JSON 可序列化且键集稳定；脚本源码里没有写库 API 的调用点。
"""
from __future__ import annotations

import hashlib
import importlib.util as _u
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app import knowledge_query as KQ          # noqa: E402
from app.db import Base                       # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,  # noqa: E402
                        StrategyInstance, Work, WorkSource)

_spec = _u.spec_from_file_location(
    "k3ap", ROOT / "scripts" / "k3a_arm_probe.py")
k3ap = _u.module_from_spec(_spec)
sys.modules["k3ap"] = k3ap
_spec.loader.exec_module(k3ap)

# ---------------------------------------------------------------- 契约
QUERY_BOOK = "WK-probe-query"        # 查询作品（有效 book policy 的 book_id）
OTHER_BOOK = "WK-probe-other"        # 第二根作品（供跨作品证据）
N_STRATEGIES = 8                      # 与真库当前行数同形（离线钉「==8」）
TEXT = "灯花轻轻跳了一下，他终于开口。"
SHA256 = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
# 顶层契约键（键缺失即红）
TOP_KEYS = {"probe", "schema", "task", "generated_at", "repo_root", "db_path",
            "db_available", "db_error", "policy", "policy_sha256", "gates",
            "strategy_status_vocab", "readonly", "side_a_default_gate",
            "side_b_status_gate_relaxed", "candidate_filter_audit",
            "ledger_gap", "relaxation", "side_b_rows", "evidence_supply",
            "comparison", "discipline"}
SIDE_KEYS = {"side", "status", "reason", "n_selected", "n_rejected",
             "selected_keys", "rejected", "budget", "snapshot_fingerprint"}
LEDGER_KEYS = {"side_a_n_rejected", "n_silently_dropped_by_status_gate",
               "silently_dropped_keys", "rejected_keys",
               "status_drops_visible_in_rejected",
               "ledger_covers_status_drops", "verdict",
               "status_gate_isolation", "note"}


def _sha256(path: Path) -> str:
    """分块流式哈希。真库已长到数十 GB，``read_bytes()`` 整读会 MemoryError
    （2026-10-02 实测 OOM），而本例的承诺是「内容指纹逐字不变」——
    流式分块既保住这条承诺，又不在内存里放整库。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> tuple:
    """零写入对账用指纹：内容 sha256 + 字节数 + mtime_ns。"""
    st = os.stat(path)
    return (_sha256(path), st.st_size, st.st_mtime_ns)


def _listing(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


# ------------------------------------------------------- 离线夹具（真库同形）
def _add_work(session, work_id: str) -> str:
    session.add(Work(id=work_id, title=work_id, source="test:k3a-arm-probe"))
    session.flush()
    segment = Segment(
        work_id=work_id, ordinal=0, text=TEXT, text_clean=TEXT,
        n_sentences=1, n_chars=len(TEXT), role="train")
    session.add(segment)
    session.flush()
    session.add(WorkSource(
        work_id=work_id, canonical_work_id=work_id,
        source_type="human_fiction", text_version="corpus-v1",
        text_sha256=SHA256, purpose_basis="test fixture",
        identity_purposes=["research"], license_purposes=[],
        license_basis=None, metadata_status="verified",
        metadata_basis="test fixture"))
    session.flush()
    return segment.id


def _add_instance(session, instance_id: str, strategy_id: str,
                  work_id: str, segment_id: str, *, index: int) -> None:
    start, end = index * 16, index * 16 + 8
    evidence = TEXT[start % 16:end % 16] or TEXT[:8]
    session.add(StrategyInstance(
        id=instance_id, strategy_id=strategy_id, strategy_version=1,
        work_id=work_id, segment_id=segment_id, frame_id=None,
        text_version="corpus-v1", span_start=start, span_end=end,
        evidence_text=evidence,
        evidence_sha256=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
        conditions_observed={}, observed_content="test observation",
        extractor_model="test", reviewer_version="test", status="verified"))


def _seed_probe_db(db_path: Path) -> None:
    """按真库形态造离线库：N 条 hypothesis + WORK 范围 + replicated 观察 +
    真实证据实例（每条 1..N 个区间，横跨两根作品）。

    这批行在**默认门**下必然全被 status 门静默丢弃（status=hypothesis 不在
    合格集），在**只读放宽**后必然全进 selected 且 `evidence_count>0`——两侧
    对照读数的地基。"""
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        query_seg = _add_work(session, QUERY_BOOK)
        other_seg = _add_work(session, OTHER_BOOK)
        for i in range(N_STRATEGIES):
            sid = f"ESV2-probe-{i:02d}"
            key = f"probe:对照卡{i:02d}"
            session.add(ExpressionStrategyV2(
                id=sid, strategy_key=key, version=1,
                abstract_operation=f"test operation {i}", invariants=[],
                effect_hypothesis=f"test hypothesis {i}", failure_modes=[],
                status="hypothesis",          # 默认门挡掉的那一档
                source="test:k3a-arm-probe", scope="WORK",
                scope_ids=[QUERY_BOOK], scope_basis="test fixture",
                observation_status="replicated", effect_status="untested"))
            # 第 1 条区间落在查询作品，其余落在第二根作品 ⇒ 跨作品证据
            _add_instance(session, f"SI-probe-{i:02d}-a", sid,
                          QUERY_BOOK, query_seg, index=0)
            for j in range(1, i + 1):
                _add_instance(session, f"SI-probe-{i:02d}-b{j}", sid,
                              OTHER_BOOK, other_seg, index=j)
        session.commit()
    engine.dispose()


@pytest.fixture(scope="module")
def offline_db(tmp_path_factory) -> Path:
    """离线夹具库（建完即关闭；探针只以 mode=ro 打开它）。"""
    db_path = tmp_path_factory.mktemp("k3a") / "language_genome.db"
    _seed_probe_db(db_path)
    return db_path


@pytest.fixture(scope="module")
def offline_report(offline_db) -> dict:
    return k3ap.run_probe(offline_db, QUERY_BOOK)


@pytest.fixture(scope="module")
def real_db() -> Path:
    """真库（缺则 skip，不猜任何读数——沿用 tests/test_brief_cost_numbers.py
    的 pytest.skip 口径）。"""
    db_path, _cands = k3ap.resolve_db("", ROOT)
    if not db_path.is_file():
        pytest.skip(f"真库缺失或不可读：{db_path}")
    return db_path


@pytest.fixture(scope="module")
def real_report(real_db) -> dict:
    rep = k3ap.run_probe(real_db, "WK-6e5d2623")
    if not rep["db_available"]:
        pytest.skip(f"真库不可读：{rep['db_error']}")
    return rep


# ------------------------------------------- ① 默认门 + 有效 book → selected=0
def test_side_a_default_gate_selects_nothing(offline_report):
    """①离线：默认门（生产口径）下有效 book policy → selected=0。"""
    side = offline_report["side_a_default_gate"]
    assert offline_report["policy"] == {"book_id": QUERY_BOOK}
    assert side["status"] == "empty"
    assert side["n_selected"] == 0
    assert side["selected_keys"] == []
    assert side["budget"]["considered"] == 0      # 候选集在筛子处就空了


def test_side_a_default_gate_tracks_current_real_db(real_report):
    """真库：默认门的候选数须对应当下可查询的 status/观察行。"""
    side = real_report["side_a_default_gate"]
    audit = real_report["candidate_filter_audit"]
    assert real_report["policy"] == {"book_id": "WK-6e5d2623"}
    candidates = [r for r in audit["all_rows"]
                  if r["status_gate"] and r["observation_gate"]]
    assert side["budget"]["considered"] == len(candidates)
    assert len(side["selected_keys"]) == side["n_selected"]
    assert side["status"] == ("matched" if side["n_selected"] else "empty")
    assert set(side["selected_keys"]) <= {
        r["strategy_key"] for r in candidates}


# ------------------------- ② rejected==0 但 status 门丢弃名单非空（台账漏报）
def test_side_a_ledger_reports_zero_rejected_while_status_gate_drops_rows(
        offline_report):
    """②离线：**台账漏报**这条事实被钉死——同一调用 rejected=0，而被 status
    门静默丢弃的名单非空。两侧读数不能互相顶替。"""
    side = offline_report["side_a_default_gate"]
    gap = offline_report["ledger_gap"]
    audit = offline_report["candidate_filter_audit"]
    assert side["n_rejected"] == 0                 # 台账说「0 条被拒」
    assert side["rejected"] == []
    assert gap["n_silently_dropped_by_status_gate"] == N_STRATEGIES
    assert gap["silently_dropped_keys"]              # 名单非空
    assert len(gap["silently_dropped_keys"]) == N_STRATEGIES
    assert gap["ledger_covers_status_drops"] is False
    assert gap["status_drops_visible_in_rejected"] == []
    assert gap["verdict"] == "ledger_under_reports"
    # 名单就是库里那批 hypothesis 行（逐条报 status 门判词，不只给计数）
    assert audit["status_gate"]["keys"] == gap["silently_dropped_keys"]
    assert all(r["status"] == "hypothesis" and r["status_gate"] is False
               and r["observation_gate"] is True
               for r in audit["status_gate"]["rows"])


def test_side_a_ledger_gap_on_real_db(real_report):
    """真库：status 门若已无丢弃项，不能继续报历史漏报。"""
    gap = real_report["ledger_gap"]
    gate = real_report["candidate_filter_audit"]["status_gate"]
    assert gap["n_silently_dropped_by_status_gate"] == gate[
        "n_silently_dropped"]
    assert gap["silently_dropped_keys"] == gate["keys"]
    assert all(r["status_gate"] is False for r in gate["rows"])
    assert gap["verdict"] == (
        "ledger_under_reports" if gate["n_silently_dropped"] else
        "ledger_consistent")


# ------------------------------------------- ③ 只读放宽后 selected==8 且有证据
def test_relaxed_status_gate_selects_every_strategy_with_evidence(
        offline_report):
    """③离线：只读放宽 status 门 → selected==8，且每条 evidence_count>0。"""
    side = offline_report["side_b_status_gate_relaxed"]
    assert side["status"] == "matched"
    assert side["n_selected"] == N_STRATEGIES == 8
    assert offline_report["candidate_filter_audit"]["n_rows_total"] == 8
    selected = [r for r in offline_report["side_b_rows"]
                if r["landing"] == "selected"]
    assert len(selected) == N_STRATEGIES
    assert all(r["evidence_count"] > 0 for r in selected)
    assert offline_report["evidence_supply"]["all_have_evidence"] is True
    # 每条都带着被默认门丢弃的标记 + 真实根作品
    assert all(r["dropped_by_status_gate_in_side_a"] is True
               for r in selected)
    assert all(r["evidence_root_works"] for r in selected)
    # 跨作品如实报告：只有落在第二根作品的证据才把 cross_work 置真
    # （第 0 条只落在查询作品 ⇒ 1 条 selected 但 cross_work=False）
    assert [r["evidence_cross_work"] for r in selected] == \
        [r["evidence_count"] > 1 for r in selected]
    assert sum(1 for r in selected if r["evidence_cross_work"]) == \
        N_STRATEGIES - 1
    assert offline_report["evidence_supply"]["root_works_union"] == \
        sorted([QUERY_BOOK, OTHER_BOOK])


def test_relaxed_status_gate_on_real_db(real_report):
    """真库：放宽不得减少选中数；无被挡行时两侧应完全一致。"""
    side = real_report["side_b_status_gate_relaxed"]
    n_rows = real_report["candidate_filter_audit"]["n_rows_total"]
    assert n_rows > 0
    assert side["status"] == "matched"
    assert side["n_selected"] > 0
    assert side["n_selected"] == real_report["evidence_supply"]["n_rows"]
    selected = [r for r in real_report["side_b_rows"]
                if r["landing"] == "selected"]
    assert len(selected) == side["n_selected"]
    assert all(r["evidence_count"] > 0 for r in selected)
    assert all(r["evidence_root_works"] for r in selected)
    assert real_report["evidence_supply"]["all_have_evidence"] is True
    # 同一份数据、只差 status 门 ⇒ 两侧 snapshot 指纹逐字相同
    assert (side["snapshot_fingerprint"]
            == real_report["side_a_default_gate"]["snapshot_fingerprint"])
    assert side["n_selected"] >= real_report["side_a_default_gate"][
        "n_selected"]
    assert real_report["comparison"]["delta_selected"] == (
        side["n_selected"] - real_report["side_a_default_gate"]["n_selected"])
    if real_report["candidate_filter_audit"]["status_gate"][
            "n_silently_dropped"] == 0:
        assert real_report["comparison"]["readings_identical"] is True
        assert real_report["comparison"]["delta_selected"] == 0


# ----------------------------------- ④ 放宽是局部且会恢复（含「确实放宽过」）
def test_relaxation_is_local_and_restores_constants(offline_db):
    """④：跑完探针后 ELIGIBLE_STATUS 与 eligible_statuses() **逐字**回到原值；
    入口函数对象复原；模块级常量在放宽作用域内也一个字节都没动。"""
    before_const = KQ.ELIGIBLE_STATUS
    before_fn = KQ.eligible_statuses
    before_snap = k3ap.gate_snapshot()
    rep = k3ap.run_probe(offline_db, QUERY_BOOK)
    assert KQ.ELIGIBLE_STATUS == before_const
    assert KQ.eligible_statuses is before_fn
    assert k3ap.gate_snapshot() == before_snap
    # 收据自证：三份常量快照逐字相同 + 入口复原 + 输出口径复原
    relax = rep["relaxation"]
    assert relax["constants_untouched"] is True
    assert relax["constants_before"] == relax["constants_during"] \
        == relax["constants_after"]
    assert relax["entry_restored"] is True
    assert relax["entry_output_restored"] is True
    # 防「空跑」：放宽必须**真的**在作用域内生效过（否则 ③ 会假红）
    assert relax["patch_live_during_side_b"] is True
    assert relax["extra_status"] == ["hypothesis"]
    for widened in relax["widened_sets_by_version"].values():
        assert "hypothesis" in widened and "verified" in widened
    # 加法放宽：原来过的仍过（只加不减）
    assert relax["constants_after"]["ELIGIBLE_STATUS"] == ["verified"]


def test_relaxation_does_not_touch_production_defaults(offline_report):
    """④补：放宽只活在一侧；侧 A 用的就是生产默认口径。"""
    assert offline_report["gates"]["ELIGIBLE_STATUS"] == ["verified"]
    assert offline_report["gates"]["ELIGIBLE_STATUS_BY_VERSION"] == {
        "1": ["verified"], "2": ["verified"]}
    assert offline_report["relaxation"]["constants_after"][
        "ELIGIBLE_STATUS_BY_VERSION"] == {"1": ["verified"], "2": ["verified"]}
    assert offline_report["discipline"]["production_default_changed"] is False
    assert offline_report["discipline"]["status_column"] == \
        "只读：未写 expression_strategies_v2.status"
    # 侧 A 与侧 B 唯一差别是 status 门：同一 policy、同一判据源
    cmp_ = offline_report["comparison"]
    assert cmp_["side_a"] == {"status": "empty", "selected": 0, "rejected": 0}
    assert cmp_["side_b"]["selected"] == N_STRATEGIES


# ----------------------------------------- ⑤ 零写入（sha256 / mtime / 无新文件）
def test_probe_writes_nothing_to_db(offline_db):
    """⑤离线：只读连接跑完，库文件 sha256 / 字节数 / mtime_ns 三项逐字不变，
    且目录不冒出新文件（-wal/-shm 之类）。"""
    before = _fingerprint(offline_db)
    before_listing = _listing(offline_db.parent)
    k3ap.run_probe(offline_db, QUERY_BOOK)
    assert _fingerprint(offline_db) == before
    assert _listing(offline_db.parent) == before_listing


def test_probe_leaves_real_db_untouched(real_db):
    """⑤真库：同样三项指纹不变 + 目录无新文件（只读承诺在连接层成立）。
    注：真库 8.4GB，本例跑两遍全量 sha256（约 20–30s）——这是「内容逐字
    未变」最硬的证据，不拿 mtime 糊弄。指纹若真的变了，先查是不是有别的
    进程在写这个库（-wal 增长），别急着判探针写库。"""
    before = _fingerprint(real_db)
    before_listing = _listing(real_db.parent)
    rep = k3ap.run_probe(real_db, "WK-6e5d2623")
    assert rep["db_available"] is True
    assert rep["readonly"]["db_mode"] == "ro"
    assert "mode=ro" in rep["readonly"]["dbapi_uri"]
    assert rep["readonly"]["query_only"] == 1
    assert rep["readonly"]["query_only_after_probe"] == 1
    after = _fingerprint(real_db)
    assert after == before, (
        f"真库指纹变了：before={before} after={after}"
        "（-wal 是否被别的进程推着长？）")
    assert _listing(real_db.parent) == before_listing


def test_readonly_session_rejects_writes(offline_db):
    """⑤补：探针用的那个只读 session 自己就写不动（query_only=ON 的闸）。"""
    from sqlalchemy import text
    session, ro = k3ap.open_ro_session(offline_db)
    try:
        assert ro["query_only"] == 1
        with pytest.raises(Exception) as exc:
            session.execute(text(
                "UPDATE expression_strategies_v2 SET status='verified'"))
        assert "readonly" in str(exc.value).lower() or "query_only" in \
            str(exc.value).lower()
    finally:
        session.close()
    # 该 session 的 query_only 只挂在它自己的连接上，不污染其他 session
    with sqlite3.connect(offline_db) as con:      # 可写探针（事后还原）
        con.execute("UPDATE expression_strategies_v2 SET status=status"
                    " WHERE 0")
        con.commit()


# ---------------------------------------------------------- CLI 契约与纪律
def test_missing_book_reports_error_and_guesses_nothing(capsys):
    """缺 --book ⇒ 如实报错退出（rc=2），**不猜一个作品**。
    空白串与「压根没这个参数」两种缺法都拒。"""
    for argv in (["--db", str(ROOT), "--book", "   "], ["--db", str(ROOT)]):
        rc = k3ap.main(argv)
        assert rc == 2, argv
        err = capsys.readouterr().err
        assert "--book" in err and "绝不猜一个作品" in err


def test_run_probe_refuses_empty_book_id(offline_db):
    """run_probe 层同样拒空 book（值错误，不落到「随便挑一本」）。"""
    with pytest.raises(ValueError) as exc:
        k3ap.run_probe(offline_db, "")
    assert "不猜一个作品" in str(exc.value)


def test_cli_writes_only_out_file(offline_db, tmp_path, capsys):
    """`--out` 是唯一的写；跑完库指纹不变，JSON 可回读。"""
    out = tmp_path / "nested" / "report.json"
    before = _fingerprint(offline_db)
    rc = k3ap.main(["--db", str(offline_db), "--book", QUERY_BOOK,
                    "--out", str(out)])
    assert rc == 0
    assert _fingerprint(offline_db) == before
    assert out.is_file()
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["db_available"] is True
    assert rep["side_a_default_gate"]["n_selected"] == 0
    assert rep["side_b_status_gate_relaxed"]["n_selected"] == N_STRATEGIES
    assert "JSON 已写" in capsys.readouterr().err


def test_cli_rejects_status_outside_vocab(offline_db, capsys):
    """侧 B 只接受知识侧词表里的 status（不自造词）。"""
    rc = k3ap.main(["--db", str(offline_db), "--book", QUERY_BOOK,
                    "--extra-status", "not_a_status"])
    assert rc == 2
    assert "词表外取值" in capsys.readouterr().err


def test_unreadable_db_reports_instead_of_guessing(tmp_path, capsys):
    """库不可读 ⇒ 如实报 db_error + rc=3，**不猜任何读数**。"""
    missing = tmp_path / "no-such-dir" / "absent.db"
    rc = k3ap.main(["--db", str(missing), "--book", QUERY_BOOK,
                    "--out", str(tmp_path / "r.json")])
    assert rc == 3
    assert "库不可读" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


def test_report_json_serializable_and_key_contract(offline_report):
    """收据全 JSON 可序列化 + 顶层/侧/台账键集稳定（缺一即红）。"""
    assert json.loads(json.dumps(offline_report, ensure_ascii=False))
    assert TOP_KEYS <= set(offline_report)
    for side in ("side_a_default_gate", "side_b_status_gate_relaxed"):
        assert SIDE_KEYS <= set(offline_report[side]), side
    assert LEDGER_KEYS <= set(offline_report["ledger_gap"])
    assert {"status_gate", "observation_gate", "n_rows_total", "all_rows"} \
        <= set(offline_report["candidate_filter_audit"])
    assert {"constants_untouched", "entry_restored", "entry_output_restored",
            "patch_live_during_side_b", "extra_status"} \
        <= set(offline_report["relaxation"])
    row = offline_report["side_b_rows"][0]
    assert {"strategy_key", "evidence_count", "evidence_root_works",
            "landing", "dropped_by_status_gate_in_side_a"} <= set(row)
    # 两道候选筛门分别记账，不混算（observation 门不是本轮结论）
    audit = offline_report["candidate_filter_audit"]
    assert audit["status_gate"]["n_silently_dropped"] + \
        audit["observation_gate"]["n_silently_dropped"] \
        + sum(1 for r in audit["all_rows"]
              if r["status_gate"] and r["observation_gate"]) \
        == audit["n_rows_total"]


def test_status_gate_is_sole_blocker_reported(offline_report):
    """侧 B 落点对账：被 status 门丢弃的这批行**全部**在侧 B 有落点
    ⇒ 阻断本臂的只有 status 门，证据供给本身够。"""
    iso = offline_report["ledger_gap"]["status_gate_isolation"]
    assert iso["sole_blocker_is_status_gate"] is True
    assert iso["n_appear_in_side_b_landing"] == N_STRATEGIES
    assert set(iso["side_b_landings"].values()) == {"selected"}


def test_script_has_no_db_write_call_sites():
    """纪律静态段：脚本源码里**没有**写库 API 调用点（commit/add/flush/
    DELETE/INSERT/UPDATE/DDL），也不出现 engine 之外的写连接。"""
    src = (ROOT / "scripts" / "k3a_arm_probe.py").read_text(encoding="utf-8")
    for pat in (r"\.(commit|add|add_all|delete|merge|flush)\s*\(",
                r"\.execute\(\s*[\"']\s*(INSERT|UPDATE|DELETE|DROP|ALTER"
                r"|CREATE|REPLACE)",
                r"exec_driver_sql\(\s*[\"']\s*(INSERT|UPDATE|DELETE|DROP"
                r"|ALTER|CREATE|REPLACE)"):
        assert re.search(pat, src, re.IGNORECASE) is None, f"命中写路径：{pat}"
    # 只读承诺必须逐字落在连接层（不是靠自觉）
    assert "mode=ro" in src
    assert "PRAGMA query_only=ON" in src
