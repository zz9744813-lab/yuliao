"""K2 复审队列驱动器回归（scripts/k2_rereview_queue.py，全离线假库）。

五钉（任务书口径）：
① 只有 reviewer_version 空/None 的实例进队列（已带 k2def-v1 的必须排除）；
② 缺策略定义正文的实例进 missing_strategy_def 桶且**不改队列总数口径**
   （桶=队列分区：n_queue = n_ready + n_missing_strategy_def）；
③ 默认档零调用零写库（只读连接断言写操作被拒 + 库文件逐字节不变）；
④ 缺库返回非零（SystemExit 非零退出码，不许静默空报告）；
⑤ --live 缺环境变量时 SystemExit 且**未发起任何调用**（网关哨兵零命中）。

复审口径单源：REVIEW_MARKER 取 app.knowledge_extract.REVIEW_MARKER_NEW_DEF，
本测试不本地重定义。
"""
from __future__ import annotations

import hashlib
import importlib.util as _u
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k2rq", ROOT / "scripts" / "k2_rereview_queue.py")
k2rq = _u.module_from_spec(_spec)
sys.modules["k2rq"] = k2rq
_spec.loader.exec_module(k2rq)

from app import knowledge_extract as KE               # noqa: E402


def _mk_db(tmp_path, *, instances, cards):
    """离线假库：raw sqlite，最小列集（驱动器只读 SELECT 所需列）。
    instances=[(id,strategy_id,work_id,segment_id,text_version,status,
                reviewer_version)]；cards={id:(key,ao,inv,eff,fm)}。"""
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    db = d / "language_genome.db"
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE strategy_instances(
      id TEXT, strategy_id TEXT, strategy_version INTEGER,
      work_id TEXT, segment_id TEXT, text_version TEXT,
      status TEXT, reviewer_version TEXT);
    CREATE TABLE expression_strategies_v2(
      id TEXT, strategy_key TEXT, version INTEGER,
      abstract_operation TEXT, invariants TEXT,
      effect_hypothesis TEXT, failure_modes TEXT);
    """)
    for (iid, sid, wid, segid, tv, status, rv) in instances:
        con.execute("INSERT INTO strategy_instances VALUES (?,?,?,?,?,?,?,?)",
                    (iid, sid, 1, wid, segid, tv, status, rv))
    for sid, (key, ao, inv, eff, fm) in cards.items():
        con.execute("INSERT INTO expression_strategies_v2 VALUES "
                    "(?,?,?,?,?,?,?)", (sid, key, 1, ao, inv, eff, fm))
    con.commit()
    con.close()
    return db


_GOOD_CARD = ("ESV2-x", ("v2:样例卡",
                         "样例抽象操作——人类直给，AI 铺陈。",
                         json.dumps(["保留短句"], ensure_ascii=False),
                         "假设正文", json.dumps(["堆修饰"], ensure_ascii=False)))
_BAD_CARD = ("ESV2-y", ("v2:缺定义卡", "", json.dumps([]),
                        "假设正文", None))     # 空抽象操作/空列表/失败模式缺


def _seed(tmp_path):
    inst = [
        ("SI-1", "ESV2-x", "WK-1", "SEG-1", "corpus-v1", "verified", None),
        ("SI-2", "ESV2-x", "WK-1", "SEG-2", "corpus-v1", "verified", ""),
        ("SI-3", "ESV2-x", "WK-1", "SEG-3", "corpus-v1", "verified",
         KE.REVIEW_MARKER_NEW_DEF),               # 已复审：必须排除
        ("SI-4", "ESV2-y", "WK-1", "SEG-4", "corpus-v1", "rejected", None),
    ]
    return _mk_db(tmp_path, instances=inst,
                  cards=dict([_GOOD_CARD, _BAD_CARD]))


def _repo(tmp_path):
    """--repo-root 口径：库在 <root>/data/language_genome.db。"""
    return tmp_path


def test_only_unreviewed_instances_enter_queue(tmp_path):
    """钉①：reviewer_version 空/None 进队列；k2def-v1 已复审的排除。"""
    db = _seed(tmp_path)
    rep = k2rq.enumerate_queue(db)
    ids = [i["instance_id"] for i in rep["queue"]]
    assert "SI-1" in ids and "SI-2" in ids
    assert "SI-3" not in ids, "已带 k2def-v1 标记的实例必须被排除"
    assert rep["ledger"]["n_reviewed_new_def"] == 1
    assert rep["ledger"]["n_total_instances"] == 4
    # 逐条字段齐（任务书口径）：
    item = rep["queue"][0]
    assert set(item) == {"instance_id", "strategy_id", "strategy_key",
                         "strategy_version", "segment_id", "work_id",
                         "text_version", "status", "reviewer_version"}
    assert item["strategy_key"] == "v2:样例卡"


def test_missing_def_bucket_partition_not_silent(tmp_path):
    """钉②：缺定义正文（四字段任一空/不可解析）→ 单列
    missing_strategy_def 桶 + fail-closed 计入报告；队列总数口径不变
    （n_queue = n_ready + n_missing_strategy_def，含缺定义条）。"""
    db = _seed(tmp_path)
    rep = k2rq.enumerate_queue(db)
    missing_ids = [i["instance_id"] for i in rep["missing_strategy_def"]]
    assert missing_ids == ["SI-4"], "缺定义条必须单列，不许静默跳过"
    assert rep["missing_strategy_def"][0]["missing_reason"] == \
        "abstract_operation_empty"
    led = rep["ledger"]
    assert led["n_queue"] == led["n_ready"] + led["n_missing_strategy_def"]
    assert led["n_queue"] == 3 and led["n_ready"] == 2 \
        and led["n_missing_strategy_def"] == 1
    # 按策略聚合含缺定义子计数（不改总数）：
    by = {b["strategy_key"]: b for b in rep["by_strategy"]}
    assert by["v2:缺定义卡"]["pending"] == 1
    assert by["v2:缺定义卡"]["missing_def"] == 1
    assert by["v2:样例卡"]["pending"] == 2


def test_default_mode_zero_calls_zero_writes(tmp_path):
    """钉③：默认档零调用零写库——(a) 只读连接对写操作明确报错；
    (b) 枚举前后库文件逐字节不变。"""
    db = _seed(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    rep = k2rq.enumerate_queue(db)
    assert rep["mode"] == "read_only" and rep["read_mode"] == "ro"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO strategy_instances VALUES "
                    "('SI-x','ESV2-x',1,'W','S','v','verified',NULL)")
    con.close()
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == after, "默认档写库=违约（文件哈希必须不变）"


def test_missing_db_exits_nonzero(tmp_path):
    """钉④：缺库 → SystemExit 非零退出码（不许静默空报告）。"""
    with pytest.raises(SystemExit) as ei:
        k2rq.enumerate_queue(tmp_path / "nope" / "language_genome.db")
    assert (ei.value.code or 0) != 0


def test_live_without_env_exits_before_any_call(tmp_path, monkeypatch):
    """钉⑤：--live 缺环境变量 → SystemExit 且未发起任何调用
    （网关哨兵零命中——app.gateway.chat 被装了断言哨兵）。"""
    import app.gateway as gw

    def _sentinel(**kw):
        raise AssertionError("发起真实调用=违约（缺环境变量必须先拒）")
    monkeypatch.setattr(gw, "chat", _sentinel)
    monkeypatch.delenv("K2_REREVIEW_ALLOW_LIVE", raising=False)
    db = _seed(tmp_path)
    monkeypatch.setattr(sys, "argv", ["k2rq", "--db", str(db), "--live",
                                      "--limit", "1",
                                      "--extractor-model", "m"])
    with pytest.raises(SystemExit, match="K2_REREVIEW_ALLOW_LIVE"):
        k2rq.main()
    # 哨兵未被触发即「未发起任何调用」（触发即 AssertionError 直接红）


def test_live_requires_limit_and_model(tmp_path, monkeypatch):
    """live 双闸补充：有环境变量但缺 --limit 或缺 --extractor-model
    → 同样 SystemExit 先拒（顺序钉：都先于任何调用构造）。"""
    monkeypatch.setenv("K2_REREVIEW_ALLOW_LIVE", "1")
    db = _seed(tmp_path)
    monkeypatch.setattr(sys, "argv", ["k2rq", "--db", str(db), "--live"])
    with pytest.raises(SystemExit, match="--limit"):
        k2rq.main()
    monkeypatch.setattr(sys, "argv", ["k2rq", "--db", str(db), "--live",
                                      "--limit", "1"])
    with pytest.raises(SystemExit, match="--extractor-model"):
        k2rq.main()
