"""文档-代码对账机械回归（docs-code-reconcile，2026-09-27）。

把 `docs/文档与代码对账台账_20260927.md` 里**可机械判定**的对账条目钉成
断言：每条 = 「某文档声称某文件存在某实现/某用例」→ 在当前代码里真的存在。
代码后来漂移（函数改名/用例删除/常量变值）时，对应断言转红——
文档与现状的失配从此有哨兵，不再靠人肉发现。

条目来源与判定依据见台账 §对账方法；每条 (doc, path, needle) 都经过
HEAD b856b63 预检（42/42 命中）后才入表，零臆造。

三类特殊条目：
- reverse：断言**不存在**（声称过度的实现——若日后有人补实现，本断言转红
  提醒同步回改文档）。dropped_foreign_extras 本条 2026-09-27 已补实现，
  改判在位（见 REVERSED_TO_PRESENT_CLAIMS）；
- min_count：文档声称「N 个用例」→ 断言实际数量 ≥ N（新增不红，缩水才红）；
- moved：文档所指文件已迁移（api.py→corpus_routes.py）→ 断言新位置仍在
  （旧位置缺失即代码已变，台账已记录）。
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_ssr_spec = _ilu.spec_from_file_location(
    "ssr_fen", ROOT / "scripts" / "strategy_stats_rebuild.py")
ssr = _ilu.module_from_spec(_ssr_spec)
_ssr_spec.loader.exec_module(ssr)

from app import config, db                                # noqa: E402
from app.models import StrategyStats                      # noqa: E402

# (文档, 代码文件, 必须存在的精确串)
PRESENT_CLAIMS = [
    # ── 策略统计证据分档_20260926 ──
    ("策略统计证据分档_20260926.md", "app/knowledge_query.py",
     'ELIGIBLE_INSTANCE_STATUS = frozenset({"verified"})'),
    ("策略统计证据分档_20260926.md", "scripts/strategy_stats_rebuild.py",
     "refs, ev_count, stripped = kq._evidence_for(s, st.id, {})"),
    ("策略统计证据分档_20260926.md", "tests/test_strategy_stats_evidence_class.py",
     "def test_excluded_source_type_zeroes_k3_eligible_but_not_valid"),
    # ── 策略裁定简报_20260925（反向口径锚：判定不读 allowed_purposes）──
    ("策略裁定简报_20260925.md", "app/knowledge_query.py",
     'FINGERPRINT_EXCLUDED_FIELDS = frozenset({"created_at", "allowed_purposes"})'),
    # ── 证据跨作品口径_20260925 ──
    ("证据跨作品口径_20260925.md", "app/knowledge_query.py",
     "require_same_book_evidence"),
    ("证据跨作品口径_20260925.md", "app/knowledge_query.py",
     '"evidence_cross_work": ('),
    ("证据跨作品口径_20260925.md", "tests/test_evidence_cross_work.py",
     "def test_fuhan_s3_shape_selected_zero_when_same_book_required"),
    # ── v2_句数口径_20260925 ──
    ("v2_句数口径_20260925.md", "scripts/import_corpus_v2.py",
     "n_sentences=n_sents(ch), n_chars=len(ch), seg_version=2,"),
    ("v2_句数口径_20260925.md", "tests/test_import_v2_sentcount.py",
     "def test_backfill_dry_run_writes_nothing"),
    # ── K3_可达预演口径_20260924 ──
    ("K3_可达预演口径_20260924.md", "scripts/k2_extract_backfill.py",
     "def k3_would_pass(s, strategy, segment, text_version, *,"),
    ("K3_可达预演口径_20260924.md", "tests/test_k3_preview_reach.py",
     "def test_true_preview_pins_current_truth_eight_hypothesis_uncertain"),
    # ── K4验收定性_20260925 ──
    ("K4验收定性_20260925.md", "tests/test_k4_accept_report.py",
     "def test_budget_refusal_is_honest_failure"),
    ("K4验收定性_20260925.md", "k4_accept_report_20260925.json", '"total": 6'),
    # ── K4收据世界目录_20260926 ──
    ("K4收据世界目录_20260926.md", "scripts/k4_paired_scenes.py",
     'rec["worlds_dir_cleaned"] = False'),
    ("K4收据世界目录_20260926.md", "tests/test_k4_worlds_dir_receipt.py",
     "def test_offline_receipts_and_output_keys_unchanged"),
    # ── K2门账本离线产出_20260926 ──
    ("K2门账本离线产出_20260926.md", "scripts/k2_contrast_extract.py",
     "def run_ledger_only(pairs: list[ContrastPair], *,"),
    ("K2门账本离线产出_20260926.md（反向口径锚）", "scripts/k5_promotion_wire_probe.py",
     'GATE_LEDGER_MARKERS = ("pair_id", "gates_ok", "persist_outcome")'),
    # ── 编辑台脚本加固_20260926 ──
    ("编辑台脚本加固_20260926.md", "websrc/_shared/editorial.js",
     "if (!target) return;"),
    ("编辑台脚本加固_20260926.md", "websrc/_shared/editorial.js",
     "banner.querySelector('.lg-banner-retry')"),
    ("编辑台脚本加固_20260926.md", "tests/test_editorial_js.py",
     "def test_retry_button_deduplicated_after_two_failures"),
    # ── 编辑台会审残留收口_20260926 ──
    ("编辑台会审残留收口_20260926.md", "app/static/index.html",
     "getComputedStyle($('#rv-q-wrap')).display === 'none'"),
    ("编辑台会审残留收口_20260926.md", "tests/test_editorial_residual.py",
     "def test_open_done_records_expands_hidden_queue_and_never_collapses"),
    # ── K2_成对对照来源门_20260924（六道门 + 照抄门常量）──
    ("K2_成对对照来源门_20260924.md", "scripts/k2_contrast_extract.py",
     'GATES = (("op_construction", gate_op_construction),'),
    ("K2_成对对照来源门_20260924.md", "scripts/k2_contrast_extract.py",
     "MIN_COPY_LEN = 40"),
    # ── 清洗正文保留门_20260926 ──
    ("清洗正文保留门_20260926.md", "scripts/clean_text.py",
     "def preserve_gate_write(seg: Segment, new_value: str, *, overwrite: bool, "
     "via: str) -> str:"),
    ("清洗正文保留门_20260926.md", "tests/test_clean_text_dryrun.py",
     'assert blocked["gate_pending"] == 2'),
    # ── 正文保留门补强_20260926 ──
    ("正文保留门补强_20260926.md", "scripts/clean_text.py", "GUARD_MAX_RATIO = 1.2"),
    ("正文保留门补强_20260926.md", "tests/test_clean_text_guard.py",
     "def test_half_pinyin_output_not_written"),
    # ── 来源类型列宽_20260925 ──
    ("来源类型列宽_20260925.md", "app/models.py",
     "source_type: Mapped[str] = mapped_column(String(64), index=True)"),
    ("来源类型列宽_20260925.md", "tests/test_source_type_width.py",
     "def test_register_rejects_overwide_source_type"),
    # ── src_ok严格布尔统一_20260926 ──
    ("src_ok严格布尔统一_20260926.md", "scripts/normalize_typos.py",
     "return src_ok_strict(raw)"),
    ("src_ok严格布尔统一_20260926.md", "tests/test_src_ok_strict.py",
     "def test_normalize_typos_json_ok_is_same_source"),
    # ── source_check流式残余_20260926 ──
    ("source_check流式残余_20260926.md", "scripts/source_check.py",
     "with contextlib.closing(gen) as g:"),
    # ── 并发闸单源_20260925 ──
    ("并发闸单源_20260925.md", "scripts/clean_text.py",
     "from _conc_guard import check_conc as _check_conc, pool_workers as _pool_workers"),
    ("并发闸单源_20260925.md", "tests/test_conc_guard_shared.py",
     "def test_check_conc_rejects_below_min_with_exit_2"),
    # ── 令牌两档分离_20260925 ──
    ("令牌两档分离_20260925.md", "app/access.py", "def load_admin_token"),
    ("令牌两档分离_20260925.md", "app/access.py", "LG_ADMIN_LEGACY_SHARED"),
    # ── 评审令牌调用量上限_20260926 ──
    ("评审令牌调用量上限_20260926.md", "app/api.py",
     "return max(_ENTRY_QUOTA_FLOOR, int(raw))"),
    ("评审令牌调用量上限_20260926.md", "tests/test_api_experiment_quota.py",
     "def test_check_and_increment_share_one_lock"),
    # ── 旁路脚本并发上限_第二批_20260925 ──
    ("旁路脚本并发上限_第二批_20260925.md", "scripts/ai_ranking_build.py",
     "workers = pool_workers(6, JUDGES, serial_check=is_serial_model)"),
]

# 曾按「声称过度、实现缺失」以 reverse 口径入册的条目（台账 2026-09-27）：
# dropped_foreign_extras 一条已于 2026-09-27 补齐实现（覆盖前清点并打印），
# 据此**改判在位断言**——实现字面量必须存在，再消失即红，提醒同步回改
# docs/策略统计证据分档_20260926.md §6⑪ 与台账口径。
REVERSED_TO_PRESENT_CLAIMS = [
    ("策略统计证据分档_20260926.md（会审处置⑪）", "scripts/strategy_stats_rebuild.py",
     "dropped_foreign_extras"),
]

# 文档声称的用例数量下限（新增用例不红，缩水才红）
MIN_COUNT_CLAIMS = [
    ("正文保留门补强_20260926.md（10→22 用例）", "tests/test_clean_text_guard.py",
     22),
    ("评审令牌调用量上限_20260926.md（16 例）", "tests/test_api_experiment_quota.py",
     16),
    ("令牌两档分离_20260925.md（28 例）", "tests/test_token_privilege_split.py", 28),
]

# 文档所指文件已迁移（代码已变类）——新位置必须仍在
MOVED_CLAIMS = [
    ("v2_句数口径_20260925.md（api.py 路由已拆出）", "app/corpus_routes.py",
     '"sents": x.n_sentences,'),
]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("doc,path,needle", PRESENT_CLAIMS,
                         ids=[f"{c[0]}::{c[2][:36]}" for c in PRESENT_CLAIMS])
def test_doc_claim_present_in_current_code(doc: str, path: str, needle: str):
    """文档声称的实现/用例，在当前代码里必须真的存在。"""
    assert needle in _read(path), (
        f"文档 {doc} 声称的实现已从 {path} 消失或变形：{needle[:60]!r}——"
        "代码已漂移，请对账并回改文档（或恢复实现）")


@pytest.mark.parametrize("doc,path,needle", REVERSED_TO_PRESENT_CLAIMS,
                         ids=[f"{c[0]}::{c[2][:36]}"
                              for c in REVERSED_TO_PRESENT_CLAIMS])
def test_overclaim_reversed_to_present(doc: str, path: str, needle: str):
    """reverse 条目改判在位（2026-09-27）：该声称曾判「实现缺失」，实现补齐
    后断言字面量必须存在于实现文件；实现再丢失即红。"""
    assert needle in _read(path), (
        f"{path} 不再包含 {needle!r}——条目已于 2026-09-27 改判在位，"
        "实现不应回缩；若确要回缩请同步回改台账与文档口径")


@pytest.mark.parametrize("doc,path,minimum", MIN_COUNT_CLAIMS,
                         ids=[f"{c[0]}::>={c[2]}" for c in MIN_COUNT_CLAIMS])
def test_doc_claimed_case_count_not_shrunk(doc: str, path: str, minimum: int):
    src = _read(path)
    n = src.count("def test_")
    assert n >= minimum, f"文档 {doc} 声称 {minimum} 个用例，当前 {path} 只有 {n}"


@pytest.mark.parametrize("doc,path,needle", MOVED_CLAIMS,
                         ids=[f"{c[0]}::{c[2][:36]}" for c in MOVED_CLAIMS])
def test_moved_claim_lands_at_new_location(doc: str, path: str, needle: str):
    """文档所指文件已迁移（代码已变）——功能必须落在新位置。"""
    assert needle in _read(path), (
        f"文档 {doc} 所指功能迁移后的新位置 {path} 里找不到 {needle[:60]!r}")


def test_reconcile_ledger_exists():
    """对账台账本体在位（本文件的断言来源与判定依据）。"""
    ledger = ROOT / "docs" / "文档与代码对账台账_20260927.md"
    assert ledger.exists(), "对账台账缺失"
    text = ledger.read_text(encoding="utf-8")
    assert "STATUS:" in text, "台账缺 STATUS 行"
    assert "对账方法" in text, "台账缺「对账方法与可复现命令」节"


# ══ dropped_foreign_extras 功能回归（foreign-extras-notice，2026-09-27）══
# 分档文档 §6⑪ 的声称（run(apply=True) 覆盖前清点并打印被丢弃的第三方
# extras 键）由「声称过度」补成真实实现——以下钉行为：清点计数正确、打印
# 一行汇总（stdout）、汇总进 run() 返回 dict、apply 后库里外来键确实没了、
# dry_run 恒 {} 且零库写。

# 重建写入 extras 的键全集（与脚本 CANONICAL_EXTRAS_KEYS 同步，漂移即红）
CANON = {"by_root_work", "usable_evidence", "benchmark_stripped",
         "k3_eligible_instances", "k3_eligible_root_works"}


def _assert_temp_db() -> None:
    """写库护栏（同 test_strategy_stats_evidence_class 的双道断言）：
    apply=True 只许打 conftest 临时 SQLite，绝不可能是真库。顺带建表
    （run() 不自带 init_db，本文件单跑时表可能尚未创建）。"""
    db.init_db()
    _url = str(db.engine.url)
    assert _url.startswith("sqlite:///") and "lg_test_" in _url, \
        f"engine 不是 conftest 临时库：{_url}"
    assert config.DATABASE_URL.startswith("sqlite:///") and \
        "lg_test_" in config.DATABASE_URL, \
        f"config 与实际 engine 不一致：config={config.DATABASE_URL} engine={_url}"


def _seed_stats(strategy_id: str, extras: dict) -> None:
    db.init_db()
    with db.session() as s:
        s.add(StrategyStats(strategy_id=strategy_id, strategy_version=1,
                            snapshot_at="seed", data_fingerprint="fen",
                            extras=dict(extras)))
        s.commit()


def _all_stats_rows() -> list[tuple]:
    """全表逐行快照（含 extras），供 dry_run 前后一致性对照。"""
    with db.session() as s:
        return sorted(
            (r.id, r.strategy_id, r.strategy_version, r.data_fingerprint,
             r.attempts,
             json.dumps(r.extras, sort_keys=True, ensure_ascii=False)
             if r.extras is not None else None)
            for r in s.query(StrategyStats).all())


def test_apply_counts_drops_and_reports_foreign_extras(capsys):
    """①造 2 个外来键（k_fen_a 现于 2 行、k_fen_b 现于 1 行）：apply 后
    汇总计数正确、打印行在位、库里外来键确实没了。"""
    _assert_temp_db()
    assert CANON == set(ssr.CANONICAL_EXTRAS_KEYS), \
        "测试端键全集与脚本 CANONICAL_EXTRAS_KEYS 漂移"
    ssr.run(apply=True)      # 归一：清掉会话内可能残留的它处 stats 外来键
    _seed_stats("SS-fen-x1", {"by_root_work": {}, "k_fen_a": 1})
    _seed_stats("SS-fen-x2", {"k_fen_a": 2, "k_fen_b": 3})
    capsys.readouterr()
    rep = ssr.run(apply=True)
    assert rep["dropped_foreign_extras"] == {"k_fen_a": 2, "k_fen_b": 1}
    printed = capsys.readouterr().out
    assert "dropped_foreign_extras=3" in printed
    assert "'k_fen_a': 2" in printed and "'k_fen_b': 1" in printed
    with db.session() as s:
        assert s.query(StrategyStats).filter(
            StrategyStats.strategy_id.in_(["SS-fen-x1", "SS-fen-x2"])
        ).count() == 0, "被覆盖的旧行（外来键载体）必须已不在库"
        for r in s.query(StrategyStats).all():
            assert set(r.extras or {}) <= CANON, \
                f"apply 覆盖后 extras 仍含外来键：{r.extras}"


def test_apply_without_foreign_keys_reports_zero(capsys):
    """②无外来键：汇总恒空、打印 `dropped_foreign_extras=0 {}`、不报错。"""
    _assert_temp_db()
    ssr.run(apply=True)      # 归一（同上）
    capsys.readouterr()
    rep = ssr.run(apply=True)
    assert rep["dropped_foreign_extras"] == {}
    assert "dropped_foreign_extras=0 {}" in capsys.readouterr().out


def test_dry_run_reports_empty_and_writes_nothing(capsys):
    """③dry_run：即便库里现存外来键，汇总恒 {}、不打印清点行、库零改动
    （前后逐行一致，沿用零库写纪律）。"""
    _seed_stats("SS-fen-dry", {"k_fen_dry": 1})
    before = _all_stats_rows()
    rep = ssr.run(apply=False)
    assert rep["dropped_foreign_extras"] == {}
    assert _all_stats_rows() == before, "dry_run 必须零库写"
    assert "dropped_foreign_extras" not in capsys.readouterr().out
    with db.session() as s:   # 收尾清掉本用例自造的行，不留污染
        s.query(StrategyStats).filter_by(strategy_id="SS-fen-dry").delete()
        s.commit()
