"""goldpick 候选清单构建器回归（P2-7）：夹具作品过滤 + 分层完整性。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goldpick_build as GP  # noqa: E402
from app import db  # noqa: E402
from app.models import Segment, Work  # noqa: E402


def test_goldpick_excludes_fixture_works_and_reports(tmp_path):
    db.init_db()
    with db.session() as s:
        for title in ("fixture_leak", "凡人修仙传（忘语）-gp"):
            w = Work(title=title, source="test:gp")
            s.add(w)
            s.flush()
            seg = Segment(work_id=w.id, ordinal=0,
                          text="这是一段足够长的正文文本，用来通过最小字数与来源质量闸门检查，"
                               "它继续讲述人物在夜色里的动作与对话，长度超过六十个汉字的门槛。",
                          integrity='{"src_ok": true}', n_sentences=1, n_chars=34)
            s.add(seg)
        s.commit()
    out = GP.build("gptest", n=6, seed=1, out_dir=tmp_path)
    works = out["works"]
    assert all(not x.startswith("fixture") for x in works), "夹具作品混进指认清单"
    assert out["n"] >= 1
