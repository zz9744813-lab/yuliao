"""`scripts/stats_projection_drift.py` 的回归钉（临时 sqlite 夹具，不碰真库）。

钉死的是**判据本身**，不是某一版真库读数：
① 旧投影只有 `by_root_work` ⇒ 4 个分档键必须**逐个点名**为缺失；
② 键齐但值不同 ⇒ 进「值不一致」而不是「缺失」（两条通道不许互相冒名）；
③ `snapshot_at` 超龄 ⇒ `stale=True`，未超龄 ⇒ `False`（两个方向都钉，
   单向钉等于没钉）；
④ **零写入**：跑完夹具库文件 sha256 逐字节不变（连接层 mode=ro 的可证伪
   形式——真写一次就红）；
⑤ 真库只读冒烟：连得上就算得完，且真库文件逐字节不动；真库不在/不可读
   则 skip 并如实标注原因，**不伪造成通过**；
⑥ 反向自检：投影与现算值逐键一致 + 快照未超龄 ⇒ `drift_rows=0`——防
   「逢库必报漂移」的假阳性绿（①②⑤ 在假阳性工具下同样会全绿）。
"""
from __future__ import annotations

import datetime
import hashlib
import importlib.util as _u
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "spdr", ROOT / "scripts" / "stats_projection_drift.py")
spdr = _u.module_from_spec(_spec); _spec.loader.exec_module(spdr)

from app import db                                           # noqa: E402
from app.models import ExpressionStrategyV2, StrategyStats    # noqa: E402

# 分档文档（docs/策略统计证据分档_20260926.md）声称的四个键：库里没写进去
# ⇒ 必须逐个点名，一个不漏。
CLASS_KEYS = ("usable_evidence", "benchmark_stripped",
              "k3_eligible_instances", "k3_eligible_root_works")

NOW = datetime.datetime(2026, 9, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _consistent() -> dict:
    """与「无实例夹具」现算值逐键相等的 extras：分档键 0、by_root_work 空表。

    by_root_work 必须单独给 `{}`——现算值那里是 dict，写成 0 就成了值漂移，
    反向自检例会因此失去意义。
    """
    return {**{k: 0 for k in CLASS_KEYS}, "by_root_work": {}}


def _fresh(tmp_path: Path, name: str = "fixture.db"):
    """独立夹具库：只建表；不用 app.db 的全局 engine，也不开 WAL。"""
    path = tmp_path / name
    eng = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    db.Base.metadata.create_all(eng)
    return path, sessionmaker(bind=eng, future=True, expire_on_commit=False)


def _seed_strategy(SM, key: str) -> str:
    with SM() as s:
        st = ExpressionStrategyV2(
            strategy_key=key, abstract_operation=f"操作-{key}",
            effect_hypothesis=f"假设-{key}", status="hypothesis",
            scope="WORK", scope_ids=[])
        s.add(st)
        s.commit()
        return st.id


def _seed_stats(SM, strategy_id: str, extras: dict, snapshot_at: str,
                valid: int = 3) -> None:
    with SM() as s:
        s.add(StrategyStats(strategy_id=strategy_id, strategy_version=1,
                            snapshot_at=snapshot_at,
                            data_fingerprint="0" * 64, valid=valid,
                            attempts=valid, extras=extras))
        s.commit()


def _fixture(tmp_path: Path):
    """四个策略（现算分档键全为 0，夹具无实例）；返回路径 + 策略 id 表。

    投影行由各个用例自己按要测的形态塞，所以这里只登记策略行——未被投影
    的策略进 `unprojected_strategy_ids`，不进 `total`（total = 投影行数）。
    """
    path, SM = _fresh(tmp_path)
    ids = {k: _seed_strategy(SM, k)
           for k in ("k-old", "k-val", "k-fresh", "k-stale")}
    return path, SM, ids


def _row(rep: dict, key: str) -> dict:
    return [r for r in rep["rows"] if r["strategy_key"] == key][0]


def _sha256(path: Path) -> str:
    """分块哈希：真库可能有数百 MB，不整读进内存。"""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 23), b""):
            h.update(chunk)
    return h.hexdigest()


# ───────────────────────────────────────────────────────────── ① 缺失键
def test_only_by_root_work_projection_names_all_four_missing_keys(tmp_path):
    """真库现况的复刻：投影 extras 只有 by_root_work ⇒ 4 个分档键逐个点名。"""
    path, SM, ids = _fixture(tmp_path)
    _seed_stats(SM, ids["k-old"], {"by_root_work": {"w1": 3}},
                "2026-09-23T07:24:05Z")
    rep = spdr.build_report(path, 7.0, now=NOW)
    row = _row(rep, "k-old")
    for k in CLASS_KEYS:
        assert k in row["missing_keys"], f"缺失键未点名 {k}：{row['missing_keys']}"
    assert sorted(row["missing_keys"]) == sorted(CLASS_KEYS)
    assert "by_root_work" not in row["missing_keys"]
    # 四条通道互不冒名：缺的键不进值不一致，在场但变了（by_root_work 里
    # 还挂着旧根作品计数）才进。
    assert set(row["value_mismatch"]) == {"by_root_work"}
    assert not set(CLASS_KEYS) & set(row["value_mismatch"])
    assert row["snapshot_at"] == "2026-09-23T07:24:05Z"
    assert rep["summary"]["missing_key_rows"] == 1
    line = spdr.summary_line(rep["summary"])
    assert line == "drift_rows=1 / missing_key_rows=1 / total=1", line


# ────────────────────────────────────────────────────────── ② 值不一致
def test_present_but_stale_value_goes_to_mismatch_not_missing(tmp_path):
    """键齐、值不同 ⇒ 落在「值不一致」通道（旧值 vs 现算值并列给出）。"""
    path, SM, ids = _fixture(tmp_path)
    _seed_stats(SM, ids["k-val"], {**{k: 99 for k in CLASS_KEYS},
                                   "by_root_work": {}}, "2026-09-23T07:24:05Z")
    rep = spdr.build_report(path, 7.0, now=NOW)
    row = _row(rep, "k-val")
    assert row["missing_keys"] == [], "值漂移被误报成缺失键"
    assert set(row["value_mismatch"]) == set(CLASS_KEYS)
    for k in CLASS_KEYS:
        cell = row["value_mismatch"][k]
        assert cell["projection"] == 99
        assert cell["recomputed"] == 0        # 夹具无实例 ⇒ 现算必为 0
        assert set(cell) == {"projection", "recomputed"}
    assert rep["summary"]["missing_key_rows"] == 0
    assert rep["summary"]["value_mismatch_rows"] == 1
    assert row["drift"] is True


def test_valid_column_drift_is_reported_separately(tmp_path):
    """extras 全对但 valid 列不同 ⇒ 单独可见（valid_mismatch），不混进键差。"""
    path, SM, ids = _fixture(tmp_path)
    _seed_stats(SM, ids["k-val"], _consistent(),
                NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), valid=7)
    rep = spdr.build_report(path, 7.0, now=NOW)
    row = _row(rep, "k-val")
    assert row["missing_keys"] == [] and row["value_mismatch"] == {}
    assert (row["valid_projection"], row["valid_recomputed"]) == (7, 0)
    assert row["valid_mismatch"] is True and row["drift"] is True


# ─────────────────────────────────────────────────────────── ③ 超龄双向
def test_snapshot_age_stale_true_and_false_both_directions(tmp_path):
    path, SM, ids = _fixture(tmp_path)
    full = _consistent()
    _seed_stats(SM, ids["k-stale"], full, "2026-01-01T00:00:00Z")   # 远超 7 天
    _seed_stats(SM, ids["k-fresh"], full, "2026-09-26T00:00:00Z")   # 1.5 天
    rep = spdr.build_report(path, 7.0, now=NOW)
    assert _row(rep, "k-stale")["stale"] is True
    assert _row(rep, "k-fresh")["stale"] is False
    assert _row(rep, "k-fresh")["missing_keys"] == []
    assert rep["summary"]["stale_rows"] == 1
    # 阈值跟随 --max-age-days：放宽到 300 天后同一行不再算超龄
    rep2 = spdr.build_report(path, 300.0, now=NOW)
    assert [_row(rep2, k)["stale"] for k in ("k-stale", "k-fresh")] == \
        [False, False]
    # 收紧到 0 天：连 1.5 天前的快照也算超龄
    rep3 = spdr.build_report(path, 0.0, now=NOW)
    assert [_row(rep3, k)["stale"] for k in ("k-stale", "k-fresh")] == \
        [True, True]


def test_unparseable_snapshot_is_flagged_not_silently_fresh(tmp_path):
    """读不懂的时间戳不许判成「没超龄」——stale=None 并计入汇总。"""
    path, SM, ids = _fixture(tmp_path)
    _seed_stats(SM, ids["k-stale"], {**{k: 0 for k in CLASS_KEYS},
                                     "by_root_work": {}}, "not-a-date")
    rep = spdr.build_report(path, 7.0, now=NOW)
    assert _row(rep, "k-stale")["stale"] is None
    assert rep["summary"]["snapshot_unparseable_rows"] == 1


# ──────────────────────────────────────────────────────────── ④ 零写入
def test_cli_run_leaves_fixture_db_byte_identical(tmp_path):
    path, SM, ids = _fixture(tmp_path)
    # k-old：只有 by_root_work（且值与现算一致）⇒ 只算「缺键」行
    # k-val：五键齐但分档值全是 99 ⇒ 只算「值不一致」行
    # 两条通道各占一行，汇总计数才可分辨（missing_key_rows / value_mismatch_rows）
    _seed_stats(SM, ids["k-old"], {"by_root_work": {}},
                "2026-09-23T07:24:05Z")
    _seed_stats(SM, ids["k-val"], {**{k: 99 for k in CLASS_KEYS},
                                   "by_root_work": {}}, "2026-09-23T07:24:05Z")
    SM().bind.dispose()                       # 释放夹具写连接，只留只读句柄
    before = _sha256(path)
    bound = db.SessionLocal                   # 跑完必须还原成原绑定
    out = tmp_path / "report.json"
    rc = spdr.main(["--db", str(path), "--out", str(out), "--max-age-days", "7"])
    assert rc == 0, "对账跑完必须 rc=0（漂移是读数，不是故障）"
    assert db.SessionLocal is bound, "SessionLocal 未还原——会污染后续用例"
    assert _sha256(path) == before, "只读承诺破了：夹具库文件被写过"
    assert [p.name for p in sorted(tmp_path.iterdir())] == \
        ["fixture.db", "report.json"], "跑出了 -wal/-shm 之类副作用文件"
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["readonly"] is True
    assert rep["open_mode"] == "sqlite uri mode=ro"
    assert rep["summary"]["total"] == 2
    assert rep["summary"]["missing_key_rows"] == 1
    assert rep["summary"]["value_mismatch_rows"] == 1
    # stale_rows 不在此钉：main() 用真实时钟，夹具快照「2026-09-23T07:24:05Z」
    # 距回放当天可能已超龄——超龄判据由 ③ 用固定 now 双向钉死。
    assert len(rep["unprojected_strategy_ids"]) == 2


def test_missing_db_path_exits_2(tmp_path):
    """库不存在 ⇒ 退出码 2（跑不起来 ≠ 有漂移），且不创建任何文件。"""
    assert spdr.main(["--db", str(tmp_path / "nope.db")]) == 2
    assert list(tmp_path.iterdir()) == []


# ────────────────────────────────────────────────────────── ⑤ 真库冒烟
def test_real_db_readonly_smoke():
    """真库只读冒烟：mode=ro 连得上、`run(apply=False)` 算得完、行数对得上。

    真库不在/不可读（未挂载、被 live 独占、WAL 侧文件缺失）⇒ skip 并如实
    带原因，不伪造成通过。漂移**读数**不在此钉（重建一次就变，那是主控的
    决定）——这里只钉「跑得通 + 真库文件不被本例写过」。
    """
    db_path = Path(spdr.DEFAULT_DB)
    if not db_path.exists():
        pytest.skip(f"真库不可读：{db_path} 不存在（冒烟例如实 skip）")
    try:
        con = spdr.ro_connect(db_path)
        n = con.execute("SELECT COUNT(*) FROM strategy_stats").fetchone()[0]
        con.close()
    except Exception as exc:                  # noqa: BLE001
        pytest.skip(f"真库 mode=ro 打开失败：{type(exc).__name__}: {exc}")
    stat0 = db_path.stat()
    sha0 = _sha256(db_path)
    rep = spdr.build_report(db_path, 7.0)
    stat1 = db_path.stat()
    if (stat0.st_size, stat0.st_mtime_ns) != (stat1.st_size, stat1.st_mtime_ns):
        # 尺寸/mtime 变了 ⇒ 有**别的**进程在写（live 或人工 rebuild），本例
        # 全程只持 mode=ro 句柄，无从归因；如实 skip，不把他人写入算成我红。
        pytest.skip("真库在本例运行期间被其它进程改动（size/mtime 变了）："
                    "冒烟例只持只读句柄，无法归因，skip 不伪造")
    assert _sha256(db_path) == sha0, \
        "真库 size/mtime 未变而内容哈希变了——只读承诺破了，硬失败不许 skip"
    assert rep["summary"]["total"] == n == 8, \
        (f"真库 strategy_stats 行数不是 8（对账 {rep['summary']['total']}"
         f" / 直查 {n}）——读数已变，需同步交付报告")
    for r in rep["rows"]:
        assert {"missing_keys", "value_mismatch", "stale"} <= set(r)


# ────────────────────────────────────────────────────────── ⑥ 反向自检
def test_reverse_self_check_consistent_projection_reports_zero_drift(tmp_path):
    """把投影写成「与现算值逐键一致 + 快照未超龄」⇒ drift_rows 必为 0。

    反向自检的必要性：只测「有问题时报得出」的判据，一个逢库必报漂移的工具
    也能全绿；本例必须一例不红才算闭环。
    """
    path, SM, ids = _fixture(tmp_path)
    for k in ("k-fresh", "k-stale"):
        _seed_stats(SM, ids[k], _consistent(),
                    NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), valid=0)
    rep = spdr.build_report(path, 7.0, now=NOW)
    assert spdr.summary_line(rep["summary"]) == \
        "drift_rows=0 / missing_key_rows=0 / total=2"
    for k in ("k-fresh", "k-stale"):
        row = _row(rep, k)
        assert row["missing_keys"] == [] and row["value_mismatch"] == {}
        assert row["stale"] is False and row["drift"] is False
        assert row["foreign_extras_keys"] == []
    assert rep["summary"]["drift_rows"] == 0, "一致投影仍报漂移 ⇒ 判据假阳性"
