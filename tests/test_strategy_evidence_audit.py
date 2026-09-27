"""策略证据位可信度审计器回归（scripts/strategy_evidence_audit.py）。

除 ⑦ 真库烟测（真库不存在则 skip，**不因缺库判红**）外全部离线：合成库
在 tmp_path 里自建（raw sqlite3，最小三表），真库一个字节都不碰。

任务书八例逐条对号：
① AI 极措辞计入且逐条列实例 id（非 verified 行不计——证据位口径）；
② 合规声明缺失计入（NULL 按「无声明」计；n_works=全实例 distinct work_id）；
③ verdict=insufficient 触发线：AI>0 一条、`n_no_conformance_claim ==
   n_verified/2` 边界相等一条、零证据 `0>=0` 保守口径一条；
④ verdict=reviewable 反例（2 < 5/2 且无 AI 命中）；
⑤ 两次运行逐字一致（generated_at 之外），排序键写死；
⑥ 只读硬约束：正则扫审计器源码，update/insert/delete/drop/alter 一词都不许
   出现；连接串必须 mode=ro + uri=True；审计跑完合成库字节/mtime 不变、
   目录不冒新文件；
⑦ 真库只读烟测（缺库 skip）：主控点名的 3 条 AI 极实例必须被逐条列出；
⑧ n_packages 读数正确 + 顶层汇总/孤行对账（db_totals 与逐策略求和的口径差
   如实可见，不静默对齐）。
另加 CLI 行为钉死：--out UTF-8+末尾换行、--limit 抽样窗、非法 limit、
库不可读 rc=2 不猜读数。
"""
from __future__ import annotations

import importlib.util as _u
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "sea", ROOT / "scripts" / "strategy_evidence_audit.py")
sea = _u.module_from_spec(_spec)
sys.modules["sea"] = sea
_spec.loader.exec_module(sea)

REAL_DB = Path("F:/agi/language-genome/data/language_genome.db")
# 主控 2026-09-27 只读实跑点名的 3 条 AI 极 verified 实例
AI_KNOWN = {"SI-2eda2b553ee7", "SI-2f8ba9930de2", "SI-ddf6d7173911"}

DDL = """
CREATE TABLE expression_strategies_v2(
  id TEXT PRIMARY KEY, strategy_key TEXT NOT NULL,
  version INTEGER DEFAULT 1, status TEXT DEFAULT 'hypothesis');
CREATE TABLE strategy_instances(
  id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, work_id TEXT,
  status TEXT DEFAULT 'proposed', observed_content TEXT);
CREATE TABLE knowledge_packages(id TEXT PRIMARY KEY);
"""


def _inst(iid, sid, oc, status="verified", work="WK-1"):
    return dict(id=iid, strategy_id=sid, work_id=work, status=status,
                observed_content=oc)


def _mk_db(tmp_path: Path, cards, instances, packages=(), name="synth.db"):
    """合成库：cards=[dict(id,key,version,status)]，真库一个字节都不碰。"""
    db = tmp_path / name
    con = sqlite3.connect(db)
    try:
        con.executescript(DDL)
        con.executemany(
            "INSERT INTO expression_strategies_v2"
            "(id, strategy_key, version, status) VALUES (?,?,?,?)",
            [(c["id"], c["key"], c.get("version", 1),
              c.get("status", "hypothesis")) for c in cards])
        con.executemany(
            "INSERT INTO strategy_instances"
            "(id, strategy_id, work_id, status, observed_content) "
            "VALUES (?,?,?,?,?)",
            [(i["id"], i["strategy_id"], i["work_id"], i["status"],
              i["observed_content"]) for i in instances])
        con.executemany("INSERT INTO knowledge_packages(id) VALUES (?)",
                        [(p,) for p in packages])
        con.commit()
    finally:
        con.close()
    return db


def _by_id(report, sid):
    return next(i for i in report["strategies"] if i["strategy_id"] == sid)


# ------------------------------------------------- ① AI 极措辞计入且列 id
def test_ai_pole_counted_and_ids_listed(tmp_path):
    """①命中 AI 极措辞即计入并逐条列实例 id；proposed 行不进证据位计数
    （审计对象=verified 实例）。"""
    db = _mk_db(
        tmp_path,
        [dict(id="ESV2-A", key="少动作直给疑问")],
        [
            _inst("SI-a1", "ESV2-A", "典型的AI铺陈，描述符合该策略定义"),
            _inst("SI-a2", "ESV2-A", "AI 式铺陈，全段演绎"),
            _inst("SI-a3", "ESV2-A", "本策略下的人类原文直给疑问"),
            _inst("SI-a4", "ESV2-A", "AI式但未核对，属 proposed",
                  status="proposed"),
        ])
    rep = sea.audit(db)
    a = _by_id(rep, "ESV2-A")
    assert a["n_ai_pole"] == 2
    assert a["ai_pole_instance_ids"] == ["SI-a1", "SI-a2"]   # 逐条列出、id 序
    assert a["n_instances"] == 4               # 采样窗内全状态
    assert a["n_verified"] == 3                # proposed 不进证据位口径
    assert rep["n_ai_pole_total"] == 2
    assert rep["criteria"]["ai_pole_terms"] == list(sea.AI_POLE_TERMS)
    # 任务书给定的最小表逐字在场
    for t in ("AI式", "AI 式", "典型的AI", "AI 倾向", "AI常"):
        assert t in sea.AI_POLE_TERMS


# --------------------------------------------- ② 合规声明缺失计入 + n_works
def test_no_conformance_claim_counted_and_works(tmp_path):
    """②不含任何合规声明措辞的 verified 实例计入；NULL 观察按「通篇没有
    声明」计；n_works=采样窗内全部实例（含 proposed）的 distinct work_id。"""
    db = _mk_db(
        tmp_path,
        [dict(id="ESV2-B", key="B")],
        [
            _inst("SI-b1", "ESV2-B", "该段描写符合策略定义", work="WK-1"),
            _inst("SI-b2", "ESV2-B", "体现了本策略的节奏安排", work="WK-1"),
            _inst("SI-b3", "ESV2-B", "属于少动作直给疑问一类", work="WK-2"),
            _inst("SI-b4", "ESV2-B", "言行相悖的反讽，人物说一套做一套",
                  work="WK-3"),                          # 无声明 → 计
            _inst("SI-b5", "ESV2-B", None, work="WK-3"),  # NULL → 计
            _inst("SI-b6", "ESV2-B", "纯复述无声明但是 proposed",
                  status="proposed", work="WK-9"),       # 非 verified 不计
        ])
    rep = sea.audit(db)
    b = _by_id(rep, "ESV2-B")
    assert b["n_no_conformance_claim"] == 2
    assert b["n_verified"] == 5
    assert b["n_works"] == 4                   # WK-1/2/3/9（含 proposed 行）
    assert rep["n_no_conformance_claim_total"] == 2
    for t in ("符合", "体现", "属于", "该策略", "本策略"):
        assert t in sea.CONFORMANCE_TERMS


# --------------------------------------------------- ③ insufficient 触发线
def test_verdict_insufficient_all_three_triggers(tmp_path):
    """③三条触发线各钉一例：AI>0；`n_no_conformance_claim == n_verified/2`
    （边界相等即触发，改成 > 本用例红）；零证据 `0 >= 0` 保守口径。"""
    db = _mk_db(
        tmp_path,
        [dict(id="ESV2-C", key="C"), dict(id="ESV2-D", key="D"),
         dict(id="ESV2-F", key="F")],
        [
            # C：verified=4，无声明=2 → 2 >= 2.0 恰好压线触发
            _inst("SI-c1", "ESV2-C", "符合该策略"),
            _inst("SI-c2", "ESV2-C", "体现本策略"),
            _inst("SI-c3", "ESV2-C", "只是原文复述甲"),
            _inst("SI-c4", "ESV2-C", "只是原文复述乙"),
            # D：verified=3，AI 命中 1，声明齐全 → 仅 AI 触发
            _inst("SI-d1", "ESV2-D", "AI 倾向明显的演绎，符合该策略"),
            _inst("SI-d2", "ESV2-D", "属于本策略例证"),
            _inst("SI-d3", "ESV2-D", "符合定义"),
        ])
    rep = sea.audit(db)
    c, d, f = (_by_id(rep, s) for s in ("ESV2-C", "ESV2-D", "ESV2-F"))
    assert c["n_verified"] == 4 and c["n_no_conformance_claim"] == 2
    assert c["n_ai_pole"] == 0 and c["verdict"] == "insufficient"
    assert d["n_ai_pole"] == 1 and d["verdict"] == "insufficient"
    assert d["ai_pole_instance_ids"] == ["SI-d1"]
    assert f["n_verified"] == 0 and f["verdict"] == "insufficient"  # 0>=0
    for i in (c, d, f):
        assert i["verdict_rule"] == sea.VERDICT_RULE       # 判据原文随行给出
        assert i["verdict_basis"]


# ---------------------------------------------------- ④ reviewable 反例
def test_verdict_reviewable_negative_case(tmp_path):
    """④无 AI 命中且无声明 2 < 6/2=3 → reviewable（把阈值改成 <= 会先让
    本例红；把 AI 表清空则 ①③ 红）。"""
    db = _mk_db(
        tmp_path,
        [dict(id="ESV2-E", key="E")],
        [
            _inst("SI-e1", "ESV2-E", "符合该策略的直给"),
            _inst("SI-e2", "ESV2-E", "体现了本策略"),
            _inst("SI-e3", "ESV2-E", "属于例子一类"),
            _inst("SI-e4", "ESV2-E", "该策略下的人类写法"),
            _inst("SI-e5", "ESV2-E", "只是语义复述甲"),
            _inst("SI-e6", "ESV2-E", "只是语义复述乙"),
        ])
    rep = sea.audit(db)
    e = _by_id(rep, "ESV2-E")
    assert e["n_verified"] == 6 and e["n_no_conformance_claim"] == 2
    assert e["n_ai_pole"] == 0
    assert e["verdict"] == "reviewable"
    assert "n_no_conformance_claim=2" in e["verdict_basis"]


# ------------------------------------------------------ ⑤ 两次运行确定性
def test_two_runs_identical_except_timestamp(tmp_path):
    """⑤同一库两次运行：generated_at 之外 JSON 逐字一致；排序键写死
    （strategy_key→version→id，实例 id 升序）。"""
    db = _mk_db(
        tmp_path,
        [dict(id="ESV2-Z", key="b-key", version=2),
         dict(id="ESV2-Y", key="a-key", version=1),
         dict(id="ESV2-X", key="a-key", version=2),
         dict(id="ESV2-W", key="a-key", version=1)],
        [_inst("SI-2", "ESV2-X", "AI式"), _inst("SI-1", "ESV2-X", "符合"),
         _inst("SI-3", "ESV2-X", "无声明复述")])
    r1, r2 = sea.audit(db), sea.audit(db)
    assert r1["generated_at"]  # 时间戳在场
    t1 = json.dumps({k: v for k, v in r1.items() if k != "generated_at"},
                    ensure_ascii=False, indent=1)
    t2 = json.dumps({k: v for k, v in r2.items() if k != "generated_at"},
                    ensure_ascii=False, indent=1)
    assert t1 == t2                                   # 逐字一致
    keys = [(i["strategy_key"], i["version"], i["strategy_id"])
            for i in r1["strategies"]]
    assert keys == sorted(keys)                       # 排序键写死
    x = _by_id(r1, "ESV2-X")
    assert x["ai_pole_instance_ids"] == ["SI-2"]      # 实例按 id 升序扫描


# ------------------------------------------------------- ⑥ 只读硬约束
def test_source_scan_and_db_bytes_untouched(tmp_path):
    """⑥审计器源码里 update/insert/delete/drop/alter 一个词都不许出现
    （词边界正则）；连接必须 mode=ro + uri=True；审计跑完合成库字节与
    mtime 均不变、目录不冒新文件（-wal/-shm/-journal 一并盯）。"""
    src = (ROOT / "scripts" / "strategy_evidence_audit.py").read_text(
        encoding="utf-8")
    m = re.search(r"(?i)\b(update|insert|delete|drop|alter)\b", src)
    assert m is None, f"审计器源码出现库写词：{m.group(0)!r}"
    assert "mode=ro" in src and "uri=True" in src
    db = _mk_db(
        tmp_path,
        [dict(id="ESV2-A", key="A")],
        [_inst("SI-1", "ESV2-A", "符合"), _inst("SI-2", "ESV2-A", "无声明")])
    before = db.read_bytes()
    before_mtime = os.stat(db).st_mtime_ns
    before_list = sorted(p.name for p in tmp_path.iterdir())
    sea.audit(db)
    assert db.read_bytes() == before
    assert os.stat(db).st_mtime_ns == before_mtime
    assert sorted(p.name for p in tmp_path.iterdir()) == before_list


# ------------------------------------------------------ ⑦ 真库只读烟测
def test_real_db_readonly_smoke():
    """⑦真库只读烟测：缺库 skip（**不得**因缺库判红）；在场则主控点名的
    3 条 AI 极 verified 实例必须被逐条列出，汇总与全表直读对账一致。"""
    if not REAL_DB.exists():
        pytest.skip(f"真库不在场（{REAL_DB}）——只读烟测跳过，不判红")
    rep = sea.audit(REAL_DB)
    assert rep["db_mode"] == "ro"
    flagged = {i for s in rep["strategies"]
               for i in s["ai_pole_instance_ids"]}
    assert AI_KNOWN <= flagged, f"主控点名的 AI 极实例未被全部列出：{flagged}"
    assert rep["n_ai_pole_total"] == len(flagged) >= 3
    # 逐策略求和 vs 全表直读：无孤行时逐字相等（155 的证据位全景）
    assert rep["n_instances_total"] == rep["db_totals"][
        "strategy_instances_rows"]
    assert rep["n_verified_total"] == rep["db_totals"][
        "strategy_instances_verified"]
    for i in rep["strategies"]:
        assert {"strategy_id", "strategy_key", "version", "status",
                "n_instances", "n_verified", "n_works", "n_ai_pole",
                "ai_pole_instance_ids", "n_no_conformance_claim", "verdict",
                "verdict_rule", "verdict_basis"} <= set(i)
        assert i["verdict"] in ("insufficient", "reviewable")


# ------------------------------------------------- ⑧ n_packages + 孤行对账
def test_n_packages_and_top_level_totals(tmp_path):
    """⑧n_packages 读数正确；顶层汇总=逐策略求和；不属于任何策略的孤行
    只进 db_totals（口径差如实可见），不污染逐策略计数。"""
    db = _mk_db(
        tmp_path,
        [dict(id="ESV2-A", key="A")],
        [_inst("SI-1", "ESV2-A", "符合该策略"), _inst("SI-2", "ESV2-A",
                                                      "体现本策略"),
         _inst("SI-orphan", "ESV2-NOPE", "无声明的孤行")],
        packages=["KPKG-1", "KPKG-2", "KPKG-3"])
    rep = sea.audit(db)
    assert rep["n_packages"] == 3
    assert rep["n_strategies"] == 1
    assert rep["n_instances_total"] == 2
    assert rep["n_verified_total"] == 2
    assert rep["n_no_conformance_claim_total"] == 0   # 孤行不计入
    assert rep["db_totals"]["strategy_instances_rows"] == 3
    assert rep["db_totals"]["strategy_instances_verified"] == 3
    for k in ("n_strategies", "n_instances_total", "n_verified_total",
              "n_ai_pole_total", "n_no_conformance_claim_total",
              "n_packages", "db_path", "generated_at"):
        assert k in rep                             # 顶层汇总契约键


# ----------------------------------------------------------- CLI 行为
def test_cli_out_utf8_trailing_newline(tmp_path, capsys):
    """--out 落盘：UTF-8、ensure_ascii=False（中文原样）、末尾换行；
    stdout 同一 JSON；rc=0；除 --out 外零写盘。"""
    db = _mk_db(tmp_path, [dict(id="ESV2-A", key="少动作直给疑问")],
                [_inst("SI-1", "ESV2-A", "AI式铺陈")])
    out = tmp_path / "sub" / "r.json"
    rc = sea.main(["--db", str(db), "--out", str(out)])
    assert rc == 0
    raw = out.read_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    text = raw.decode("utf-8")                      # UTF-8 可解码
    assert "少动作直给疑问" in text                  # ensure_ascii=False
    rep = json.loads(text)
    assert rep["n_ai_pole_total"] == 1
    assert rep["db_path"] == str(db.resolve().as_posix())
    assert json.loads(capsys.readouterr().out)      # stdout 同内容可解析
    assert not [p for p in tmp_path.iterdir()
                if p.name not in ("synth.db", "sub")]


def test_cli_limit_and_bad_args_and_missing_db(tmp_path, capsys):
    """--limit 抽样窗按 id 升序取前 n（所有计数随窗走、limit 如实回显）；
    --limit <1 → rc=2；库不可读 → rc=2 + stderr 如实原因（不猜读数）。"""
    db = _mk_db(
        tmp_path, [dict(id="ESV2-A", key="A")],
        [_inst(f"SI-{i}", "ESV2-A", "无声明复述") for i in range(1, 6)])
    assert sea.main(["--db", str(db), "--limit", "2"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["limit"] == 2
    a = _by_id(rep, "ESV2-A")
    assert a["n_instances"] == 2 and a["n_verified"] == 2
    assert a["n_no_conformance_claim"] == 2     # 计数随采样窗走
    assert a["n_no_conformance_claim"] == 2
    assert rep["n_instances_total"] == 2            # 顶层汇总随采样窗走
    assert sea.main(["--db", str(db), "--limit", "0"]) == 2
    assert "limit" in capsys.readouterr().err
    assert sea.main(["--db", str(tmp_path / "absent.db")]) == 2
    err = capsys.readouterr().err
    assert "只读库打开失败" in err
    capsys.readouterr()
