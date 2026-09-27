"""文档-代码对账机械回归（docs-code-reconcile，2026-09-27）。

把 `docs/文档与代码对账台账_20260927.md` 里**可机械判定**的对账条目钉成
断言：每条 = 「某文档声称某文件存在某实现/某用例」→ 在当前代码里真的存在。
代码后来漂移（函数改名/用例删除/常量变值）时，对应断言转红——
文档与现状的失配从此有哨兵，不再靠人肉发现。

条目来源与判定依据见台账 §对账方法；每条 (doc, path, needle) 都经过
HEAD b856b63 预检（42/42 命中）后才入表，零臆造。

三类特殊条目：
- reverse：断言**不存在**（声称过度的实现——如 dropped_foreign_extras——
  若日后有人补实现，本断言转红提醒同步回改文档）；
- min_count：文档声称「N 个用例」→ 断言实际数量 ≥ N（新增不红，缩水才红）；
- moved：文档所指文件已迁移（api.py→corpus_routes.py）→ 断言新位置仍在
  （旧位置缺失即代码已变，台账已记录）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

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

# 文档声称「已实现」但当前代码**不应存在**（会审处置⑪未落地——台账判定
# 声称过度）。若日后补实现，本断言转红，提醒同步回改文档口径。
ABSENT_CLAIMS = [
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


@pytest.mark.parametrize("doc,path,needle", ABSENT_CLAIMS,
                         ids=[f"{c[0]}::{c[2][:36]}" for c in ABSENT_CLAIMS])
def test_overclaim_still_absent(doc: str, path: str, needle: str):
    """声称过度条目：文档说有、代码当前没有——保持缺失态；
    一旦有人补实现，此断言转红提醒回改文档口径。"""
    assert needle not in _read(path), (
        f"{path} 出现了 {needle!r}——台账判定「声称过度」的前提已变化，"
        "请更新 docs/策略统计证据分档_20260926.md §6⑪ 的对账注记")


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
