"""factorial_blind_multicorpus commit 守卫回归（2026-09-14 对抗审查 P1-3）。

使用仓库内真实批次文件做只读 fixture；commit 的校验失败路径不写盘。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import factorial_blind_multicorpus as fb

BATCH = Path(__file__).resolve().parent.parent / "data" / "blind" / "factorial_ba19_batch.json"


def _commit_raises(verdicts):
    with pytest.raises(SystemExit):
        fb.cmd_commit(str(BATCH), json.dumps(verdicts))


def test_commit_rejects_wrong_length():
    batch = json.loads(BATCH.read_text(encoding="utf-8"))
    _commit_raises(["A"] * (len(batch) - 1))
    _commit_raises(["A"] * (len(batch) + 1))


def test_commit_rejects_bad_verdict_value():
    batch = json.loads(BATCH.read_text(encoding="utf-8"))
    verdicts = ["A"] * len(batch)
    verdicts[0] = "X"
    _commit_raises(verdicts)
