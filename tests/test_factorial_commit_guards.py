"""factorial_blind_multicorpus commit 守卫回归（2026-09-14 对抗审查 P1-3）。

使用临时批次和映射 fixture；commit 的校验失败路径不写盘。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import factorial_blind_multicorpus as fb

@pytest.fixture
def batch_path(tmp_path):
    batch = [{"item_id": f"Q{i:02d}", "comp": "HB"} for i in range(2)]
    mapping = [{"item_id": item["item_id"], "comp": "HB", "segment_id": "S",
                "frame_id": "F", "model": "test", "first_group": "H"}
               for item in batch]
    path = tmp_path / "factorial_test_batch.json"
    path.write_text(json.dumps(batch), encoding="utf-8")
    (tmp_path / "factorial_test_batch.map.json").write_text(
        json.dumps({"map": mapping}), encoding="utf-8")
    return path


def _commit_raises(batch_path, verdicts):
    with pytest.raises(SystemExit):
        fb.cmd_commit(str(batch_path), json.dumps(verdicts))
    assert not (batch_path.parent / "factorial_test_result.json").exists()


def test_commit_rejects_wrong_length(batch_path):
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    _commit_raises(batch_path, ["A"] * (len(batch) - 1))
    _commit_raises(batch_path, ["A"] * (len(batch) + 1))


def test_commit_rejects_bad_verdict_value(batch_path):
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    verdicts = ["A"] * len(batch)
    verdicts[0] = "X"
    _commit_raises(batch_path, verdicts)
