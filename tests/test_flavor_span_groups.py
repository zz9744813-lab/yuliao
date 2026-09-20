"""A02 回归：flavor_span 数据集三列对齐 + 按题折隔离（审查 20260920-1810）。

事故（隔离复现）：build_dataset 的 groups 按每题「正例、负例」交错追加，
texts 却按 pos+neg 重排——两组错位后按题分组折失效（3 题 9 样本中 5 个
组别错误、3 题跨了验证折），该路径的历史 AUC 不可再当效果门。

锁死两件事：
1. 三列 (texts, y, groups) 按 (text, y, rid) 记录一次成行，逐题正/负例
   计数精确对齐（正例数 = 该题批注数，负例数 = 正例数 × neg_per_pos）；
2. group_folds + _assert_folds_isolated：一轮划分中同一题只在测试侧出现
   一次，绝不同时跨训练/测试两侧；错位数据必须被断言逮住（红出来）。
"""
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import flavor_span as FS                     # noqa: E402
from flavor_train import group_folds        # noqa: E402

SEG = ("夜风把窗纸吹得微微鼓起，屋里静得能听见灯芯燃烧的声音，"
       "他坐在桌前想了很久的话，最后只写下了一句问候。")
CAND = ("夜风让窗纸轻轻鼓起来，屋里安静得连灯芯燃烧的声响都听得见，"
        "他坐在桌前思考许久，最终只写下一句问候的话语。")


def _make_db(tmp_path, spec: dict[str, int]) -> Path:
    """裸 SQLite：每 rid 一题（review_item + candidate + segment）。spec={rid: 批注数}。"""
    db_path = tmp_path / "fs.db"
    con = sqlite3.connect(str(db_path))
    con.executescript("""
        create table review_items(id text primary key, subject_id text,
                                  human_verdict text, status text);
        create table candidates(id text primary key, text text, segment_id text);
        create table segments(id text primary key, text text, text_clean text);
    """)
    for rid, n in spec.items():
        hv = json.dumps({"annotations": [
            {"target": "candidate", "start": 0, "end": 6, "text": CAND[0:6]}
            for _ in range(n)]}, ensure_ascii=False)
        con.execute("insert into review_items values (?,?,?,?)", (rid, f"c-{rid}", hv, "done"))
        con.execute("insert into candidates values (?,?,?)", (f"c-{rid}", CAND, f"s-{rid}"))
        con.execute("insert into segments values (?,?,?)", (f"s-{rid}", SEG, SEG))
    con.commit()
    con.close()
    return db_path


def test_columns_aligned_per_review(monkeypatch, tmp_path):
    """三列一次成行：每题正例数=批注数、负例数=正例数×neg_per_pos，不许错位。"""
    monkeypatch.setattr(FS, "DB",
                        _make_db(tmp_path, {"RV-A": 2, "RV-B": 1}))
    texts, y, groups = FS.build_dataset(seed=7)
    assert len(texts) == len(y) == len(groups)
    pos = [g for g, yy in zip(groups, y) if yy == 1]
    neg = [g for g, yy in zip(groups, y) if yy == 0]
    assert pos.count("RV-A") == 2 and pos.count("RV-B") == 1, \
        "正例组别必须各归其题（旧交错错位会让某题凭空多正例、另一题归零）"
    assert neg.count("RV-A") == 2 * 2 and neg.count("RV-B") == 1 * 2, \
        "负例组别随正例走：neg_per_pos=2"


def test_folds_keep_each_review_on_one_side(monkeypatch, tmp_path):
    """按题折隔离：修好的数据必须整体进折；错位数据必须被断言红出来。"""
    monkeypatch.setattr(FS, "DB",
                        _make_db(tmp_path, {f"RV-{i}": 2 for i in range(6)}))
    texts, y, groups = FS.build_dataset(seed=7)
    folds = group_folds(groups, 3, 7)
    FS._assert_folds_isolated(groups, folds)      # 对齐数据：必须通过
    # 每题只在一个折的测试侧出现
    home: dict[str, set] = {}
    for fi, te in enumerate(folds):
        for i in np.asarray(te).tolist():
            home.setdefault(groups[i], set()).add(fi)
    assert all(len(s) == 1 for s in home.values()), "一题跨了多个测试折"

    # 反证：把一个样本的组别换成别题的 rid → 该题跨两侧，断言必须 SystemExit
    other = next(g for g in groups if g != groups[0])
    bad = list(groups)
    bad[0] = other
    with pytest.raises(SystemExit, match="按题分组被破坏"):
        FS._assert_folds_isolated(bad, folds)
