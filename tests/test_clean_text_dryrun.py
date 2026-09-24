"""clean_text --dry-run 回归（2026-09-24 主控误伤实录：--rules --dry-run 静默改生产库）。

锁定的不变量：

1. **--rules --dry-run 零写入**：跑完库内 text_clean 仍全为 NULL，且文件层面
   无 commit（绕过 ORM 直读 SQLite 文件，只看得见已提交数据）；
2. **对照组**：同一批数据不带 --dry-run 时确实写入——证明 dry 门不是恒真放行；
3. **--polish --dry-run 零写入**：既有 text_clean 值逐字不变；
4. **dry 输出键固定**：dry_run / would_clean / checked；且 would_clean 与随后
   真跑的 rule_cleaned / polished **数值一致**（预报必须准，不许估）；
5. **--llm --dry-run 既有行为回归**：打印需 LLM 段数，零写库。

全离线：conftest 已把库指到临时 SQLite（LG_DATA_DIR/LG_DATABASE_URL 临时值），
不碰 data/language_genome.db 分毫。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import clean_text as ct  # noqa: E402
from app import config, db  # noqa: E402
from app.models import Segment, Work  # noqa: E402

SEED_SOURCE = "test:seed-dryrun"

# 规则能洗掉的脏段（口径来自 test_clean_text.py 的实测样例，不新造清洗规则）
DIRTY_TEXTS = [
    "(手打中文网7*24小时不间断更新纯txt手打小说m)他推门进来。",
    "他推门进来。()",
    "[]小.说.t.xt.天.堂 / 虽然帝国民风朴素，可是百姓家家充足",
]
# 已清洗但仍有规则可削残留的 text_clean（polish 用）
POLISH_DIRTY = "他推门进来。()"
POLISH_CLEAN = "虽然帝国民风朴素，可是百姓家家充足，市场里挤满了人。"


def _db_path() -> str:
    url = config.DATABASE_URL
    assert url.startswith("sqlite:///"), f"本文件只适配临时 SQLite 库：{url}"
    return url[len("sqlite:///"):]


def _raw_text_clean() -> dict[str, str | None]:
    """绕过 ORM 直读库文件：只看得见**已提交**的 text_clean（无 commit 即证零写入）。"""
    con = sqlite3.connect(_db_path())
    try:
        return {str(r[0]): r[1] for r in con.execute("SELECT id, text_clean FROM segments")}
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _fresh_seed():
    """每个用例前后清掉本文件种下的段，跨用例计数才可绝对断言。"""
    def _wipe():
        db.init_db()
        with db.session() as s:
            ids = [w.id for w in s.query(Work).filter(Work.source == SEED_SOURCE)]
            if ids:
                s.query(Segment).filter(Segment.work_id.in_(ids)).delete(
                    synchronize_session=False)
                s.query(Work).filter(Work.source == SEED_SOURCE).delete(
                    synchronize_session=False)
                s.commit()
    _wipe()
    yield
    _wipe()


def _seed(texts: list[str], clean: list[str | None] | None = None) -> list[int]:
    db.init_db()
    cleans = clean if clean is not None else [None] * len(texts)
    assert len(cleans) == len(texts)
    with db.session() as s:
        w = Work(title="t-dryrun", source=SEED_SOURCE)
        s.add(w)
        s.flush()
        ids = []
        for i, (t, c) in enumerate(zip(texts, cleans)):
            seg = Segment(work_id=w.id, ordinal=i, text=t, text_clean=c,
                          n_sentences=1, n_chars=len(t))
            s.add(seg)
            s.flush()
            ids.append(str(seg.id))   # 与 _raw_text_clean 的 str 键同口径
        s.commit()
        return ids


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"输出里没有 JSON 行：\n{stdout}")


def test_rules_dry_run_writes_nothing(capsys):
    """① --rules --dry-run 走 CLI 入口：跑完造种的脏段 text_clean 全为 NULL（文件层面无 commit）。"""
    ids = _seed(DIRTY_TEXTS)
    assert any(ct.clean_rules(t) != t for t in DIRTY_TEXTS)   # 前提：数据确实"脏"
    ct.main(["--rules", "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY-RUN" in out, "必须明确打印这是 dry-run，不许再被当成真跑结果"
    r = _last_json(out)
    assert r["dry_run"] is True
    assert r["would_clean"] >= len(ids)
    raw = _raw_text_clean()
    for sid in ids:
        assert raw[sid] is None, f"dry-run 竟写入了段 {sid}：{raw[sid]!r}"


def test_rules_without_dry_run_does_write(capsys):
    """② 对照组：同一批数据不带 --dry-run 时确实写入（证明门不是恒真放行）。"""
    ids = _seed(DIRTY_TEXTS)
    assert all(_raw_text_clean()[sid] is None for sid in ids)
    ct.main(["--rules"])
    out = capsys.readouterr().out
    assert '"dry_run"' not in out
    r = _last_json(out)
    assert r["rule_cleaned"] >= len(ids)
    raw = _raw_text_clean()
    for sid, t in zip(ids, DIRTY_TEXTS):
        assert raw[sid] == ct.clean_rules(t)


def test_polish_dry_run_keeps_existing_clean(capsys):
    """③ --polish --dry-run：既有 text_clean 值逐字不变。"""
    ids = _seed([ct.clean_rules(POLISH_DIRTY), POLISH_CLEAN],
                clean=[POLISH_DIRTY, POLISH_CLEAN])
    before = _raw_text_clean()
    assert before[ids[0]] == POLISH_DIRTY
    ct.main(["--polish", "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    r = _last_json(out)
    assert r["dry_run"] is True
    assert r["would_clean"] >= 1, "polish dry 必须预报出那条残留"
    after = _raw_text_clean()
    for sid in ids:
        assert after[sid] == before[sid], f"dry-run 改动了既有 text_clean：{sid}"


def test_dry_forecast_matches_real_run_rules(capsys):
    """④ dry 的预报必须准：--rules 的 would_clean 与真跑后的 rule_cleaned 数值一致。"""
    _seed(DIRTY_TEXTS)
    ct.main(["--rules", "--dry-run"])
    dry = _last_json(capsys.readouterr().out)
    assert set(dry) == {"dry_run", "would_clean", "checked"}   # 键名钉死
    ct.main(["--rules"])
    real = _last_json(capsys.readouterr().out)
    assert dry["would_clean"] == real["rule_cleaned"]
    assert dry["checked"] >= len(DIRTY_TEXTS)


def test_dry_forecast_matches_real_run_polish(capsys):
    """④' 同口径对 --polish 也成立：would_clean == 真跑的 polished。"""
    ids = _seed([ct.clean_rules(POLISH_DIRTY) + "这是一段足够长的正文补足长度门槛。"] * 2,
                clean=[POLISH_DIRTY, POLISH_DIRTY])
    ct.main(["--polish", "--dry-run"])
    dry = _last_json(capsys.readouterr().out)
    assert dry["dry_run"] is True
    ct.main(["--polish"])
    real = _last_json(capsys.readouterr().out)
    assert dry["would_clean"] == real["polished"] >= 2
    raw = _raw_text_clean()
    for sid in ids:
        assert raw[sid] is not None and "()" not in raw[sid]


def test_llm_dry_run_regression(capsys, monkeypatch):
    """⑤ --llm --dry-run 既有行为回归：打印需 LLM 段数，零写库，且不得触网。"""
    def _boom(*a, **k):
        raise AssertionError("dry-run 不许调用 LLM")
    monkeypatch.setattr(ct, "llm_repair_batch", _boom)
    pinyin = "一股股白sè雾气在虚空中浮现而出，把那点残存的暖意也一并吹散了。"
    ids = _seed([pinyin])
    assert ct.needs_llm(pinyin)
    ct.main(["--llm", "--dry-run"])
    out = capsys.readouterr().out
    assert "dry-run" in out and "需 LLM 的段" in out
    raw = _raw_text_clean()
    for sid in ids:
        assert raw[sid] is None


def test_run_rules_and_polish_default_dry_run_false():
    """函数级：显式 dry_run 参数（默认 False 保持既有语义），dry 返回固定键。"""
    ids = _seed(DIRTY_TEXTS)
    r = ct.run_rules(dry_run=True)
    assert r == {"dry_run": True, "would_clean": r["would_clean"],
                 "checked": r["checked"]}
    assert r["would_clean"] >= len(ids)
    assert all(_raw_text_clean()[sid] is None for sid in ids)
    p = ct.polish(dry_run=True)
    assert p["dry_run"] is True and set(p) == {"dry_run", "would_clean", "checked"}
    assert all(_raw_text_clean()[sid] is None for sid in ids)  # polish dry 也不写
