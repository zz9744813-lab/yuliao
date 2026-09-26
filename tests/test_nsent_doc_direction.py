"""`n_sentences` 回填文档的**方向门**：文档结论词 / 残留数 ⇄ `--verify` 实跑方向。

背景（仓库外独立复核 `F:/Hermes/team/reviews/REVIEW_audit_residuals_round2_20260925.md`，
判词 BLOCK，唯一依据是「验收门无方向」）：同型残留两条至今未收口——

1. **同型未推广**（该报告 §4.4 建议 4 / §5 R4）：`docs/n_sentences回填口径_20260925.md`
   与本轮回填结论同样只被「报告里写了这些数字」间接覆盖。本门改为让门**直接消费**
   `scripts/backfill_v2_sentences.py --verify` 的 exit 0 与「残留（n_sentences=0 且
   文本非空）=0」，不再靠报告转述。
2. **生成器自印假证据**（该报告 §3 注记 2）：`scripts/k5_supply_recount.py` 的
   `render_markdown` 把两行 `→ exit 0` 硬编码进文档 §0，无论实跑成败都会印出来。
   该项的收口在 `tests/test_k5_supply_recount.py`（rc 一致性回归），
   `scripts/k5_supply_recount.py` 本体（`render_markdown(res, run_rc=...)`）。

门怎么做（**零真库、纯文本 + 子进程**）：

- 文档侧：严格锚定 §4（`## 4.` 标题、到下一个 `## ` 为止）里**承载 `--verify`
  输出的那个围栏块**，用**与解析真实 stdout 同一个正则**解析逐版本
  `seg_version / count / n_sentences>0 / avg / 残留(...)` 五元组，并取该块的
  结论行（`验证合格` / `验证明不合格` + `（exit N）`）。另解析 §2「主控实测现状」
  块做**文档内两处对账**（count − sum(>0) 必须等于 §4 的残留列）。
- 实跑侧：子进程真跑 `--verify`（脚本本体取仓库内 `scripts/backfill_v2_sentences.py`，
  库取 `tmp_path` 下的**临时夹具库**，绝不碰真库、零真实模型请求），断言 exit 0、
  「残留」逐版本与合计均为 0；另一夹具留一行残留 ⇒ 同一脚本 exit 1、残留 1。
- 方向判据（**唯一变异点**＝下方 `conclusion_agrees` / `residual_agrees`）：
  `结论词比对`（结论词 + exit 标记同向）与 `残留数比对`（文档残留列同向），
  二者皆要求与实跑残留**同向**（双射：实测残留=0 ⇔ 文档给肯定结论）。
  ⇒ 只改文档结论词为反向说法、或只把残留 0 改成非 0、或真跑真出现残留而文档仍
  说「合格」，三条都**判红**。
- 缺文档 / 缺脚本 / 锚定小节漂移 / 围栏块找不到 ⇒ 一律 `assert` 失败（**fail-closed**，
  不许 skip、不许静默放行）。

**反向验证**（本门「有方向」的机械证明，两条都写在本文件里）：

- 文件内自检：结论词翻转、残留数翻转、真跑出残留仍说合格，三条都断言
  `direction_ok(...) is False`；另有「实测与文档同向为假」的反向态断言
  `direction_ok(1, 反向文档) is True`（证明判据是双射而非「见残留就红」）。
- 命令行级反向验证：把下方 `conclusion_agrees` / `residual_agrees` 任一退化成
  `return True`，本文件立刻判红（原样输出见 `PORT_nsent_direction_gate.md`）；
  复原后复绿，且 `git diff` 为空。

口径边界（不夸大本门效力）：本门**只钉方向**（残留是否为 0 与结论词是否同向），
不核对文档里的 14124 / 424294 等绝对数字与真库是否逐位相符——那需要真库只读复算，
属 `scripts/k5_supply_recount.py` / 主控验收会话的职责；夹具库的段数量级与真库无关，
故**不**断言两侧 count 相等（断言了就是「拿夹具冒充真库」的假证据）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "n_sentences回填口径_20260925.md"
SCRIPT = ROOT / "scripts" / "backfill_v2_sentences.py"

# 文档侧词表：肯定方向 / 反向。先剥反向词再判肯定词——`不合格` 是
# `验证明不合格` 的子串，只查肯定词会让正反两个方向同时判绿。
PASS_WORD = "验证合格"
FAIL_WORD = "验证明不合格"

SEC4 = re.compile(r"^##\s*4[.、]")
SEC2 = re.compile(r"^##\s*2[.、]")
SEC_NEXT = re.compile(r"^##\s")
FENCE = re.compile(r"^```")
# 逐版本统计行：**文档 §4 的围栏块与真实 stdout 逐字同形**，故两侧共用一个正则
# （全角 ｜ 分隔；`残留(...)` 括号为半角，与脚本 print 一致）。
ROW = re.compile(
    r"seg_version=(?P<ver>\d+)｜count=(?P<count>[\d,]+) "
    r"｜n_sentences>0 (?P<pos>[\d,]+) "
    r"｜avg=(?P<avg>[\w.]+) "
    r"｜残留\(n_sentences=0 且文本非空\) (?P<res>[\d,]+)")
# §2「主控实测现状」块：count 与 sum(n_sentences>0) 成对给出（无残留列）。
ROW_STATUS = re.compile(
    r"seg_version=(?P<ver>\d+) → (?P<count>[\d,]+) 行, "
    r"sum\(n_sentences>0\)=(?P<pos>[\d,]+)")
EXIT_MARK = re.compile(r"（exit\s*(-?\d+)）")


def _int(raw: str) -> int:
    return int(raw.replace(",", ""))


# ── fail-closed 出口：缺文档 / 缺脚本一律红，不许静默放行 ────────────────
def _read_doc(path: Path = DOC) -> str:
    assert path.is_file(), (
        f"n_sentences 回填口径文档不在树内：{path}"
        f"（门不得因缺文档而静默放行）")
    return path.read_text(encoding="utf-8")


def _script(path: Path = SCRIPT) -> Path:
    assert path.is_file(), (
        f"被测脚本不在树内：{path}（门不得因缺脚本而静默放行）")
    return path


def _section(text: str, head: re.Pattern[str], what: str) -> str:
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    assert start is not None, f"文档里找不到 {what} ⇒ 锚定失效，本门失去解析对象"
    end = next((i for i in range(start + 1, len(lines))
                if SEC_NEXT.match(lines[i])), len(lines))
    return "\n".join(lines[start:end])


def _fences(section: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    inside = False
    for ln in section.splitlines():
        if FENCE.match(ln):
            if inside:
                out.append("\n".join(cur))
                cur, inside = [], False
            else:
                inside = True
        elif inside:
            cur.append(ln)
    return out


def _pick_fence(section: str, *needles: str, what: str) -> str:
    cands = [b for b in _fences(section) if all(n in b for n in needles)]
    assert len(cands) == 1, (
        f"{what} 锚定失败：符合条件的围栏块应恰为 1 个，实得 {len(cands)}"
        f"（文档结构漂移 ⇒ 本门必须判红，不许猜）")
    return cands[0]


def _rows(block: str, rx: re.Pattern[str], what: str) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    for m in rx.finditer(block):
        groups = m.groupdict()
        out[int(groups["ver"])] = {k: _int(v) for k, v in groups.items()
                                   if k in ("count", "pos", "res") and v is not None}
    assert out, f"{what} 解析不到任何逐版本统计行 ⇒ 本门失去解析对象"
    return out


@dataclass(frozen=True)
class DocClaim:
    """文档自己声称的口径（只从锚定小节解析，不复述别处叙述）。"""
    rows: dict[int, dict[str, int]]      # §4 逐版本 {count, pos, res}
    status: dict[int, dict[str, int]]    # §2 逐版本 {count, pos}
    conclusion: str                      # PASS_WORD / FAIL_WORD
    exit_marker: int | None              # 结论行上的 （exit N）

    @property
    def residuals(self) -> dict[int, int]:
        return {ver: r["res"] for ver, r in self.rows.items()}

    @property
    def versions(self) -> set[int]:
        return set(self.rows)


def _parse_claim(text: str) -> DocClaim:
    sec4 = _section(text, SEC4, "§4（真库只读 `--verify`）小节")
    block = _pick_fence(sec4, "残留(n_sentences=0 且文本非空)", "seg_version=",
                        what="§4 的 --verify 输出块")
    rows = _rows(block, ROW, "§4 逐版本统计行")
    assert all("res" in r for r in rows.values()), \
        "§4 逐版本行缺 `残留(...)` 列 ⇒ 解析口径漂移"
    # 结论行：块内唯一含结论词的行（先剥反向词，防 `不合格` 子串串味）
    hits = [ln for ln in block.splitlines()
            if PASS_WORD in ln or FAIL_WORD in ln]
    assert len(hits) == 1, \
        f"§4 输出块的结论行应恰为 1 行，实得 {len(hits)}（自相矛盾或漂移 ⇒ 判红）"
    line = hits[0]
    if FAIL_WORD in line:
        conclusion = FAIL_WORD
    elif PASS_WORD in line:
        conclusion = PASS_WORD
    else:                                   # pragma: no cover - 上面已保证二选一
        conclusion = ""
    m = EXIT_MARK.search(line)
    status = _rows(_pick_fence(_section(text, SEC2, "§2（主控实测现状）小节"),
                               "sum(n_sentences>0)=", "seg_version=",
                               what="§2 实测现状块"),
                   ROW_STATUS, "§2 实测现状行")
    return DocClaim(rows=rows, status=status, conclusion=conclusion,
                    exit_marker=int(m.group(1)) if m else None)


# ── 实跑侧：子进程真跑 --verify（临时夹具库；零真库、零模型调用）────────
_SEED = """
import sys
sys.path.insert(0, r"%(root)s")
from app import db
from app.ids import new_id
from app.models import Segment, Work
db.init_db()
rows = %(rows)s
with db.session() as s:
    for ver, txt, ns in rows:
        w = Work(id=new_id("WK"), title="nsent-dir-v%%d" %% ver,
                 source="file:nsent-dir-v%%d" %% ver, note="n")
        s.add(w)
        s.flush()
        s.add(Segment(id=new_id("SEG"), work_id=w.id, ordinal=0, text=txt,
                      n_sentences=ns, n_chars=len(txt), seg_version=ver,
                      integrity="{}"))
    s.commit()
"""

# 干净夹具：v1/v2 的 n_sentences 全 >0 ⇒ 「残留（n_sentences=0 且文本非空）=0」
CLEAN_ROWS = [(1, "甲：一句话。第二句。", 2), (1, "乙：只有一句。", 1),
              (2, "丙：三句。第四句。第五句！", 3), (2, "丁：两句。还是两句！", 2)]
# 脏夹具：v2 多留一行「非空文本 + n_sentences=0」⇒ 同一脚本必须 exit 1、残留 1
DIRTY_ROWS = CLEAN_ROWS + [(2, "戊：回填漏掉的一行。", 0)]


@dataclass(frozen=True)
class VerifyRun:
    rc: int
    rows: dict[int, dict[str, int]]
    stdout: str

    @property
    def residual_total(self) -> int:
        return sum(r["res"] for r in self.rows.values())


def _env(home: Path) -> dict:
    return dict(os.environ,
                LG_DATABASE_URL=f"sqlite:///{(home / 'nsent.db').as_posix()}",
                LG_DATA_DIR=str(home), LG_LLM_MODE="mock",
                PYTHONIOENCODING="utf-8")


def _run(home: Path, *args: str, code: str | None = None) -> subprocess.CompletedProcess:
    argv = [sys.executable, *(["-c", code] if code else []), *args]
    return subprocess.run(argv, cwd=str(ROOT), env=_env(home), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


def _build_and_verify(home: Path, rows: list) -> VerifyRun:
    seed = _run(home, code=_SEED % {"root": str(ROOT), "rows": repr(rows)})
    assert seed.returncode == 0, f"夹具建库失败：{seed.stdout}{seed.stderr}"
    r = _run(home, str(_script()), "--verify")
    assert r.stdout, f"--verify 无 stdout（stderr：{r.stderr}）"
    return VerifyRun(rc=r.returncode, rows=_rows(r.stdout, ROW, "--verify 逐版本行"),
                     stdout=r.stdout)


@pytest.fixture(scope="module")
def clean_run(tmp_path_factory) -> VerifyRun:
    return _build_and_verify(tmp_path_factory.mktemp("nsent_clean"), CLEAN_ROWS)


@pytest.fixture(scope="module")
def dirty_run(tmp_path_factory) -> VerifyRun:
    return _build_and_verify(tmp_path_factory.mktemp("nsent_dirty"), DIRTY_ROWS)


# ── 方向判据（唯一变异点：conclusion_agrees / residual_agrees）────────────
# 「同向」是**双射**而非单向蕴含：实测残留为 0 ⇔ 文档给「残留 0 + 合格 + exit 0」
# 的肯定结论。任一侧单独翻转 ⇒ False（判红）。
def conclusion_agrees(measured_residual: int, claim: DocClaim) -> bool:
    """结论词比对（反向验证变异点 A）：结论词与 `（exit N）` 标记同向于实跑残留。"""
    expected_pass = measured_residual == 0
    return ((claim.conclusion == PASS_WORD) == expected_pass
            and (claim.exit_marker == 0) == expected_pass)


def residual_agrees(measured_residual: int, claim: DocClaim) -> bool:
    """残留数比对（反向验证变异点 B）：文档 §4 残留列的「是否全 0」同向于实跑残留。"""
    return all(r == 0 for r in claim.residuals.values()) == (measured_residual == 0)


def direction_ok(measured_residual: int, claim: DocClaim) -> bool:
    return (conclusion_agrees(measured_residual, claim)
            and residual_agrees(measured_residual, claim))


def _flip_conclusion(text: str) -> str:
    """内存里把 §4 结论行翻成反向说法（沿用脚本自己的失败措辞），不落盘。"""
    return text.replace(f"{PASS_WORD}", f"{FAIL_WORD}", 1).replace(
        "（exit 0）", "（exit 1）", 1)


# ── ① fail-closed：缺文档 / 缺脚本 / 目标漂移一律红 ────────────────────
def test_gate_targets_exist_inside_repo():
    assert _script().is_file() and _read_doc()
    assert ROOT in DOC.parents and ROOT in SCRIPT.parents, \
        "门的观测对象必须都在本仓内（不得指向树外路径）"


def test_fail_closed_when_doc_missing(tmp_path):
    with pytest.raises(AssertionError):
        _read_doc(tmp_path / "absent.md")


def test_fail_closed_when_script_missing(tmp_path):
    with pytest.raises(AssertionError):
        _script(tmp_path / "absent.py")


def test_fail_closed_when_anchor_section_is_gone():
    """§4 小节被删/改名 ⇒ 解析对象漂移必须判红，不许静默放行。"""
    with pytest.raises(AssertionError):
        _parse_claim(_read_doc().replace("## 4.", "## 四、"))


# ── ② 文档侧解析：结构没漂移 + 文档内两处对账 ─────────────────────────
def test_doc_claim_parses_residual_zero_rows_and_pass_conclusion():
    claim = _parse_claim(_read_doc())
    assert claim.versions == {1, 2}, \
        f"§4 逐版本统计的 seg_version 集合已漂移：{sorted(claim.versions)}"
    for ver, r in claim.rows.items():
        assert r["res"] == 0, f"文档声称 v{ver} 有 {r['res']} 行残留 ⇒ 与实跑口径不同向"
    assert claim.conclusion == PASS_WORD, \
        f"文档结论词是 {claim.conclusion!r}，非肯定方向 {PASS_WORD!r}"
    assert claim.exit_marker == 0, f"文档结论行的 exit 标记是 {claim.exit_marker}"


def test_doc_status_block_agrees_with_verify_block():
    """文档内两处对账：§2 的 count − sum(>0) 必须等于 §4 的残留列。

    这条拦的是「只改残留数不改 count」的自相矛盾改法（数字存在性检查对此零判别力）。
    """
    claim = _parse_claim(_read_doc())
    assert set(claim.status) == claim.versions, \
        "§2 与 §4 报的 seg_version 集合不一致"
    for ver, s in claim.status.items():
        row = claim.rows[ver]
        assert s["count"] == row["count"] and s["pos"] == row["pos"], \
            f"v{ver} 的 count/sum(>0) 两处不一致：§2 {s} vs §4 {row}"
        assert row["count"] - row["pos"] == row["res"], (
            f"v{ver} 的残留列 {row['res']} ≠ count−sum(>0) = "
            f"{row['count'] - row['pos']} ⇒ 文档自相矛盾")


# ── ③ 实跑侧：--verify 的 exit 与「残留」（含只读/可复跑）──────────────
def test_verify_subprocess_run_is_exit0_with_zero_residual(clean_run):
    assert clean_run.rc == 0, f"--verify 应 exit 0：\n{clean_run.stdout}"
    assert {1, 2} <= set(clean_run.rows), \
        f"夹具的 --verify 应逐版本报 v1/v2：\n{clean_run.stdout}"
    assert clean_run.residual_total == 0, \
        f"干净夹具的残留应为 0：\n{clean_run.stdout}"
    assert PASS_WORD in clean_run.stdout, clean_run.stdout
    assert "VERIFY" in clean_run.stdout and "零写入" in clean_run.stdout


def test_verify_subprocess_run_flags_residual_with_nonzero_exit(dirty_run):
    assert dirty_run.rc == 1, f"有残留时 --verify 必须 exit 1：\n{dirty_run.stdout}"
    assert dirty_run.residual_total == 1, dirty_run.stdout
    assert FAIL_WORD in dirty_run.stdout and "仍有 1 行" in dirty_run.stdout, \
        dirty_run.stdout


def test_verify_subprocess_run_is_repeatable_and_read_only(clean_run, tmp_path_factory):
    """只读 + 幂等：同一夹具再跑一次 `--verify`，stdout 与 exit 逐字不变。"""
    home = tmp_path_factory.mktemp("nsent_repeat")
    assert _run(home, code=_SEED % {"root": str(ROOT), "rows": repr(CLEAN_ROWS)}).returncode == 0
    a = _run(home, str(_script()), "--verify")
    b = _run(home, str(_script()), "--verify")
    assert a.returncode == b.returncode == 0
    assert a.stdout == b.stdout, "--verify 反复跑输出不一致 ⇒ 疑似有写入或非确定读数"


# ── ④ 核心判据：文档结论词/残留数 与 --verify 实跑同向 ─────────────────
def test_doc_direction_matches_real_verify_run(clean_run):
    claim = _parse_claim(_read_doc())
    assert direction_ok(clean_run.residual_total, claim), (
        "文档结论方向与 `--verify` 实跑不同向：实跑残留=%d（exit %d），"
        "文档结论词=%r、exit 标记=%r、残留列=%r ⇒ 结论被手工改动，"
        "或文档数字与实跑口径脱节"
        % (clean_run.residual_total, clean_run.rc, claim.conclusion,
           claim.exit_marker, claim.residuals))


def test_direction_gate_is_green_on_matching_failure_state(dirty_run):
    """双射的另一半：实跑真出现残留、文档同步改成反向说法（且残留列非 0）⇒ 绿。

    证明判据不是「见残留就红」的万能红，而是真的在比对方向。反向文档用
    `dataclasses.replace` 从真实解析结果派生（并保持文档内自洽：count−sum(>0)=残留）。
    """
    claim = _parse_claim(_read_doc())
    failed = replace(
        claim,
        rows={v: {**r, "pos": r["count"] - 7, "res": 7} for v, r in claim.rows.items()},
        conclusion=FAIL_WORD, exit_marker=1)
    assert all(r["count"] - r["pos"] == r["res"] for r in failed.rows.values())
    assert direction_ok(dirty_run.residual_total, failed) is True, \
        "实跑残留=1、文档同步改成反向说法时判红 ⇒ 判据不是双射比对"


# ── ⑤ 反向验证：三条变异方向都判红（门「有方向」的机械证明）────────────
def test_direction_gate_rejects_flipped_conclusion_word(clean_run):
    mutated = _flip_conclusion(_read_doc())
    assert mutated != _read_doc(), "变异未生效（结论词没找到）"
    claim = _parse_claim(mutated)
    assert conclusion_agrees(clean_run.residual_total, claim) is False, \
        "结论词翻成反向说法后本门仍判绿 ⇒ 门钉不住方向"
    assert direction_ok(clean_run.residual_total, claim) is False


def test_direction_gate_rejects_flipped_residual_count(clean_run):
    """把 §4 的残留 0 手工改成非 0（与同向结论词并存）⇒ 残留数比对判红。"""
    mutated = _read_doc().replace(
        "｜残留(n_sentences=0 且文本非空) 0", "｜残留(n_sentences=0 且文本非空) 7", 1)
    assert mutated != _read_doc(), "变异未生效（残留列没找到）"
    claim = _parse_claim(mutated)
    assert claim.residuals[1] == 7
    assert residual_agrees(clean_run.residual_total, claim) is False, \
        "残留数被改成非 0 而实跑残留=0，本门仍判绿 ⇒ 门钉不住方向"
    assert direction_ok(clean_run.residual_total, claim) is False


def test_direction_gate_rejects_optimistic_doc_when_real_residual_found(dirty_run):
    """实跑真跑出残留（exit 1、残留 1），文档仍说「合格 / 残留 0」⇒ 判红。"""
    claim = _parse_claim(_read_doc())
    assert dirty_run.residual_total == 1 and dirty_run.rc == 1
    assert conclusion_agrees(dirty_run.residual_total, claim) is False, \
        "实跑有残留而文档结论词仍是肯定方向，本门仍判绿 ⇒ 门钉不住方向"
    assert direction_ok(dirty_run.residual_total, claim) is False
