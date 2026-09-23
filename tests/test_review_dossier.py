"""策略语义审查材料生成器回归（scripts/strategy_review_dossier.py，只读）。

钉住的事：
1. 每条策略一张卡：身份行（status/observation/scope）+ 语义内容 + 证据
   概览（与 strategy_stats 一致）+ verified 样本逐字引用 + 固定三条审查问句；
2. 样本引用必须逐字等于库内 evidence_text（引用即证据，不转写）；
3. 只读且确定：生成前后库内行数不变、两次生成逐字相同；
4. --out 拒既存（审查材料不许覆盖）；--samples≥1 校验。
"""
from __future__ import annotations

import importlib.util as _u
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_k2_backfill import _FxOK, _seed          # noqa: E402
import k2_extract_backfill as k2b                  # noqa: E402

_spec = _u.spec_from_file_location(
    "srd", ROOT / "scripts" / "strategy_review_dossier.py")
srd = _u.module_from_spec(_spec); _spec.loader.exec_module(srd)

from app import db                                 # noqa: E402
from app.models import (ExpressionStrategyV2,       # noqa: E402
                        StrategyInstance)
import strategy_stats_rebuild as SS                 # noqa: E402

TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，慢慢开口说了正事。"


def test_dossier_cards_samples_stats_readonly():
    key = _seed(n_seg=2)
    with db.session() as s:
        k2b.run_backfill(s, _FxOK(), limit=48, strategy_keys=(key,))
    SS.run(apply=True)                            # 投影先重建
    with db.session() as s:
        st = (s.query(ExpressionStrategyV2)
              .filter_by(strategy_key=key).one())
        text = srd.build_dossier(s, samples=3)
        n_inst = s.query(StrategyInstance).count()
        n_st = s.query(ExpressionStrategyV2).count()
        text2 = srd.build_dossier(s, samples=3)   # 二次生成
        assert s.query(StrategyInstance).count() == n_inst, \
            "生成器写库=违约（只读承诺）"
        assert s.query(ExpressionStrategyV2).count() == n_st
    assert text == text2, "两次生成必须逐字相同（确定性，无隐藏时戳）"
    card = text.split(f"## {key}（v1）", 1)[1].split("\n## ", 1)[0]
    assert f"- status: **{st.status}**" in card
    assert f"observation: {st.observation_status}" in card
    assert str(st.abstract_operation) in card, "语义内容必须入卡"
    assert "root_works: **1**" in card and "valid: 2" in card, \
        "证据概览必须与本策略 strategy_stats 一致（卡内断言，不跨卡）"
    assert TEXT[0:8] in card, "样本引用必须逐字等于库内 evidence_text"
    for q in srd.REVIEW_QUESTIONS:                # 固定三条审查问句
        assert q in card, "每张卡都带审查问句"


def test_dossier_main_refuses_existing_and_bad_samples(tmp_path, monkeypatch):
    out = tmp_path / "dossier.md"
    out.write_text("已存在", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["srd", "--out", str(out)])
    with pytest.raises(SystemExit, match="已存在"):
        srd.main()
    monkeypatch.setattr(sys, "argv",
                        ["srd", "--out", str(tmp_path / "d2.md"),
                         "--samples", "0"])
    with pytest.raises(SystemExit, match="1"):
        srd.main()
