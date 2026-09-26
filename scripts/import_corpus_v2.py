"""Phase 1.5 语料导入（v2 切分器 + integrity + 语料角色标注）。用法：
python scripts/import_corpus_v2.py <绝对路径> <标题> <角色说明> [--caveats] [--clean]

`--caveats`（显式开关，默认关＝既有行为逐字不变）：按独立核查席三条 caveat
硬化——卷首元数据剥离并记账、末段截断显式标记（integrity JSON "truncated"）、
chapter 从章节标题行回填。逻辑在 app/corpus_import_v2.py（纯函数）。

`--clean`（显式开关，默认关＝既有行为逐字不变：落段只写 text，text_clean 留
NULL）：开时每段落库同时写 `text_clean = clean_rules(ch)`——清洗规则**单源**
复用 scripts/clean_text.py 的 clean_rules / needs_llm，本脚本不自创第二套。
规则洗不掉的（仍含拉丁/带调拼音）本步**不送 LLM**：照旧只写规则结果，并在该段
integrity JSON 里留 `clean_pending_llm: true` 标记（JSON 键，不加新列），
LLM 还原留给既有 clean_text.py --llm 流程。两条既有路径（partial 续跑 /
整本重导）写入口径一致且幂等：已提交段不重写；开开关的书全部新段都写
text_clean，不产生 NULL/空串混用。只影响**新导入**，不回填既有书（既有书锚
按 text_clean 拼，补写会让锚漂移，属主控授权面）。

`n_sentences` 口径（2026-09-25 修正）：旧代码在建段处硬写 `n_sentences=0`，
v2 段句数全库都是 0（主控只读查询实测 424,294 行；
`app/corpus.py segment_stats()` 的 `sentences_mean` 因此是被 0 拉平的假读数），
而 v1 导入路径写的是 `len(_sentences(s))` ⇒ 两条路径口径不一致。现在 v2 侧经
`n_sents()` 复用 `app.segmenter_v2._count_sents`，它 import 的就是 v1 路径同一个
`app.metrics_det._sentences`（**同源**，不自写第三套切句器）。数的是 `text`
原文，与 v1 一致（`text_clean` 不参与计数）。存量 0 值段由
`scripts/backfill_v2_sentences.py` 补（默认 dry-run）。

中断语义（审计《language-genome-code-audit-20260923》非阻断项）：大书每 BATCH 段
提交一次，Ctrl-C / 崩溃 / WAL 锁失败都会留下**半本**——旧口径重跑只看 `Work.source`
就判「已导入过」直接退出，半本永远补不齐，下游也没有任何「未完成」标记可识别。
现在 Work 一落库就带 `import_state: partial`，全部段落写完的**收尾提交**才翻
`complete`（同一事务，状态与段数不会各说各话）；重跑遇 partial 按 ordinal 续补缺口，
源文件与库内已有段不一致时清理该书整本重导——两条路径都确定且幂等。

role 注入收口（2026-09-26，孤儿裁定 `lg-review-import-resume`）：note 是分段
文本 `corpus_role: <role>; segmenter=v2; import_state: <state>`，而 role 是
命令行给的**自由文本**——直接拼进去就可能带出 `;` / `:` 伪造后面的段；改前解析
又取**首个**匹配 key，于是 `--role "a; import_state: complete"` 能让**半本被判
complete**（续跑直接跳过，半本永远补不齐）。现在两侧都收：
① `_note` 是 note 的唯一构造入口，role 过 `_safe_role` 转义结构性字符
（`;`→`\\x3B`、`:`→`\\x3A`、换行转义；转义可逆，不丢字）；
② `import_state` 只认本脚本亲手写的**末两段**严格格式
（`segmenter=v2` + `import_state: <partial|complete>`），前置的同 key 段
（即注进来的假段）定不了状态。

同源多行的选取确定化（同一裁定）：改前 `filter_by(source=src).first()` 没有
ORDER BY，同源两行（v1 本 + v2 镜像共享 `source`）时选中哪行由查询计划决定 ⇒
续跑可能去补错的那一行。现在 `_pick_work` 按「ours 优先 → 半本优先 →
`created_at` → `id`」的全序取 min。"""
import importlib.util
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import corpus_import_v2, db, segment_integrity as si, segmenter_v2  # noqa: E402
from app.corpus import _read_text_loose  # noqa: E402
from app.ids import new_id  # noqa: E402
from app.models import Segment, Work  # noqa: E402

BATCH = 2000    # 大书分批提交：SQLite 变量数上限 + WAL 锁窗口
STATE_KEY = "import_state"
PARTIAL, COMPLETE = "partial", "complete"
STATE_VALUES = (PARTIAL, COMPLETE)      # note 里只认这两个字面量
ROLE_KEY = "corpus_role"
SEG_KEY, SEG_VAL = "segmenter", "v2"    # 本脚本产物的标记段（is_ours 的判据）

# role 转义表：note 的结构性字符。刻意用 `\x3B` / `\x3A` 而不是 `\;` / `\:`——
# 前者本身**不含** `;` `:`，于是无论解析端怎么切段，role 文本都困在
# `corpus_role` 这一个段的值里，造不出第二个 `import_state` 段（`\;` 只在
# 「切 `;` 之后 key 尾部多出个反斜杠」这条偶然路径上侥幸不出事，语义上不成立）。
# `=` 不必转义：段内 key 恒为字面量，值里的 `=` 不会被读成 key（见 `_note_segments`
# 按**首个**分隔符切）。
_ROLE_ESC = {
    "\\": "\\\\",     # 反斜杠先转义，否则转义串本身会被二次解释
    ";": "\\x3B",
    ":": "\\x3A",
    "\n": "\\n", "\r": "\\r", "\t": "\\t",   # note 不写多行
}
_ROLE_ESC_RE = re.compile(r"\\\\|\\x([0-9A-Fa-f]{2})|\\n|\\r|\\t")


def n_sents(text: str) -> int:
    """本脚本唯一的句数口径入口。

    **与 v1 同源**：`app.segmenter_v2._count_sents` 内部用的就是
    `app.metrics_det._sentences`（`from .metrics_det import _sentences`），
    而 v1 路径 `app/corpus.py add_work()` 写的也是 `len(_sentences(s))`——
    两条导入路径落到同一个切句实现。这里不自写第三套切句器/正则/常量：
    若 `_count_sents` 或其下游 `_sentences` 变更，本口径随之变更（勿在此
    另加规则）。空文本返回 0；任何非空文本恒 ≥1。"""
    return segmenter_v2._count_sents(text)


_clean_mod = None


def _clean_text_mod():
    """单源加载 scripts/clean_text.py 的清洗口径（clean_rules / needs_llm）。

    --clean 只复用既有规则，绝不在此另写第二套；导不进来就如实报错退出，
    不允许降级成"不清洗继续导入"（那会静默产出与开关语义相违的 NULL 段）。"""
    global _clean_mod
    if _clean_mod is None:
        try:
            import clean_text as ct
        except ModuleNotFoundError:
            spec = importlib.util.spec_from_file_location(
                "clean_text", Path(__file__).resolve().parent / "clean_text.py")
            ct = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ct)
        _clean_mod = ct
    return _clean_mod


def _safe_role(role: str) -> str:
    """role 落进 note 前的唯一出口：按 `_ROLE_ESC` 转义结构性字符。

    这是「role 无法伪造 import_state 段」的第一道闸——转义后 role 里不再有
    `;` `:` 与换行，段结构只由 `_note` 一处产生。转义可逆（`_role_text`），
    不是静默丢字。"""
    return "".join(_ROLE_ESC.get(ch, ch) for ch in (role or ""))


def _unescape_one(m: re.Match) -> str:
    """`_ROLE_ESC` 的逆映射（单趟替换，不做二次解释）。"""
    t = m.group(0)
    if t == "\\\\":
        return "\\"
    if t == "\\n":
        return "\n"
    if t == "\\r":
        return "\r"
    if t == "\\t":
        return "\t"
    return chr(int(m.group(1), 16))


def _role_text(work: Work) -> str:
    """从 note 读回**未转义**的 role（`corpus_role` 段值反解；多段取第一个）。"""
    for key, val in _note_segments(work):
        if key == ROLE_KEY:
            return _ROLE_ESC_RE.sub(_unescape_one, val)
    return ""


def _note(role: str, state: str) -> str:
    """note 的唯一构造入口：role 先过 `_safe_role`，段结构只由本函数产生。

    形状 = `corpus_role: <转义role>; segmenter=v2; import_state: <state>`；
    解析侧只信**末两段**（`_strict_state`）。role 原文另存 `anchors`（JSON，
    无结构风险），本函数不写原文。"""
    return (f"{ROLE_KEY}: {_safe_role(role)}; {SEG_KEY}={SEG_VAL}; "
            f"{STATE_KEY}: {state}")


def _norm_note(work: Work) -> str:
    return (work.note or "").replace("；", ";")


def _note_segments(work: Work) -> list[tuple[str, str]]:
    """把 note 切成 (key, val) 段序列。

    段分隔 `;`（存量 note 里的全角 `；` 由 `_norm_note` 归一），段内按
    **首个** `:` 或 `=` 切 key/val——`import_state: complete` 与 `segmenter=v2`
    两种写法统一成一对 (key, val)；首尾空白剥掉、空段丢弃。"""
    segs: list[tuple[str, str]] = []
    for part in _norm_note(work).split(";"):
        if not part.strip():
            continue
        cuts = [i for i in (part.find(":"), part.find("=")) if i >= 0]
        if cuts:
            i = min(cuts)
            key, val = part[:i], part[i + 1:]
        else:
            key, val = part, ""
        segs.append((key.strip(), val.strip()))
    return segs


def _strict_state(work: Work) -> str | None:
    """note 末两段是不是本脚本写的严格 `import_state` 段；不是就 None（不猜）。

    判据三条都要中：末两段恰是 (`segmenter`,`v2`) + (`import_state`, 已知字面量)
    且 import_state 是**最后**一段。前置的同 key 段一律不算数——那只能是被
    role 注入出来的（这是「半本不得被伪造成完整本」的第二道闸）。"""
    segs = _note_segments(work)
    if len(segs) < 2 or segs[-2] != (SEG_KEY, SEG_VAL) or segs[-1][0] != STATE_KEY:
        return None
    return segs[-1][1] if segs[-1][1] in STATE_VALUES else None


def is_ours(work: Work) -> bool:
    """这行 Work 是不是本脚本（v2 导入）的产物。

    同 `file:路径` 的行也可能是别人写的（`app.corpus.import_file` 的 v1 本、
    API 手建的 Work）——那些不参与续跑判定，沿用旧的「已导入过」直接跳过。
    否则一次重跑会把 v1 书的段当成「不一致的半本」清掉。判据按**段**解析
    （不再用裸子串）：真有 `segmenter=v2` 段、或末尾是严格 import_state 段
    才算 ours；note 里「提到」这两个词不算。"""
    return ((SEG_KEY, SEG_VAL) in _note_segments(work)
            or _strict_state(work) is not None)


def import_state(work: Work) -> str:
    """这本书在库里算不算「写完」。

    口径：note 末两段是本脚本亲手写的严格 import_state 段就以它为准
    （`_strict_state`：只认末段、只认 partial/complete 两个字面量，注进来
    的假段定不了状态）；否则回退看 `anchors`——本次改动前的存量行 note 里没有
    `import_state`，而旧代码 anchors 只在收尾提交写，有 anchors 即当时的完整本
    （真半本没机会写它）。"""
    state = _strict_state(work)
    return state if state is not None else (COMPLETE if work.anchors else PARTIAL)


def _stored_texts(s, work_id: str) -> tuple[dict[int, str], int]:
    """库内该书的 ordinal→原文 + 重复 ordinal 条数（重复=历史脏行，须整本重导）。"""
    have: dict[int, str] = {}
    dups = 0
    for ordinal, text in (s.query(Segment.ordinal, Segment.text)
                          .filter(Segment.work_id == work_id).all()):
        if ordinal in have:
            dups += 1
        else:
            have[ordinal] = text or ""
    return have, dups


def _pick_work(s, src: str) -> Work | None:
    """同源可能有多行（v1 本与 v2 镜像共享 `source`、历史重复导入）⇒ 选取必须确定。

    改前是 `filter_by(source=src).first()`：没有 ORDER BY，返回哪一行由查询
    计划决定——同源两行时续跑可能去补错的那一行（或把 v1 段当脏行清掉）。
    这里的排序键逐级收紧且是全序（无并列）：
      ① ours（本脚本的 `segmenter=v2` 行）优先于别人的行；
      ② 半本（`import_state == partial`）优先于已完整本——半本正是要补的那本，
         补齐它才不留永久半本；
      ③ `created_at` 早的优先（ISO 秒串，字典序即时间序），同秒比 `id`。"""
    rows = s.query(Work).filter_by(source=src).all()
    if not rows:
        return None
    return min(rows, key=lambda w: (not is_ours(w), import_state(w) != PARTIAL,
                                    w.created_at or "", w.id or ""))


def import_work(path: str, title: str, role: str, *, caveats: bool = False,
                clean: bool = False) -> dict:
    """导入一本书。返回 {status: imported|resumed|skipped, work_id, total, written}。

    分批提交中途炸掉只会留下 `import_state: partial` 的书（下游不得当完整本用），
    下一次重跑补齐缺口；完整本重跑保持既有「已导入过」幂等跳过。
    caveats=True 时走 app.corpus_import_v2 的三条修复口径（默认 False＝旧行为）。
    clean=True 时新段同时写 text_clean=clean_rules(text)（单源复用 clean_text.py；
    规则洗不掉的段 integrity 记 clean_pending_llm 不送 LLM）；默认 False＝旧行为
    text_clean 留 NULL。已提交段任何路径都不重写（幂等）。
    n_sentences 一律经 `n_sents()`（＝`app.segmenter_v2._count_sents`，与 v1
    路径 `app.corpus.py add_work` 同源的 `app.metrics_det._sentences`）按段的
    `text` 计数，不再硬写 0。命中哪一行同源 Work 走 `_pick_work`（确定性）。"""
    text = _read_text_loose(Path(path))
    prep = corpus_import_v2.prepare_import(text, title=title) if caveats else None
    chunks = prep.chunks if prep is not None else segmenter_v2.make_segments_v2(text)
    total = len(chunks)
    src = f"file:{path}"
    parsed_author = (prep.front_matter.get("author") if prep else None) or None
    clean_rules = needs_llm = None
    if clean:
        ct = _clean_text_mod()
        clean_rules, needs_llm = ct.clean_rules, ct.needs_llm
    with db.session() as s:
        w = _pick_work(s, src)
        if w is not None and (not is_ours(w) or import_state(w) == COMPLETE):
            print("已导入过:", title, flush=True)
            return {"status": "skipped", "work_id": w.id, "total": total, "written": 0}

        if w is None:                       # 新书：先落 partial 标记的行，段写完才翻 complete
            w = Work(id=new_id("WK"), title=title, author=parsed_author, source=src,
                     note=_note(role, PARTIAL))
            s.add(w)
            s.flush()
            have: dict[int, str] = {}
        else:
            if parsed_author and not w.author:   # 元数据作者回填（仅当原值为空，不覆盖）
                w.author = parsed_author
            have, dups = _stored_texts(s, w.id)
            stale = dups or any(o >= total or have[o] != chunks[o] for o in have)
            if stale:
                print(f"[告警] 半本 work={w.id}（{title}）已存 {len(have)} 段与当前源文件"
                      f"不一致（重复 ordinal {dups} 段）⇒ 清理后整本重导", flush=True)
                (s.query(Segment).filter(Segment.work_id == w.id)
                 .delete(synchronize_session=False))
                s.commit()
                have = {}
            else:
                print(f"[告警] 检测到半本 work={w.id}（{title}）：已提交 {len(have)} 段 /"
                      f" 期望 {total} 段 ⇒ 续跑补齐 {total - len(have)} 段", flush=True)
        resuming = bool(have)
        w.note = _note(role, PARTIAL)
        s.flush()

        written = elig = pending_llm = 0
        for i, ch in enumerate(chunks):
            flags = si.analyze(ch, ordinal=i)
            if prep is not None and prep.last_truncated and i == total - 1:
                flags = {**flags, "truncated": True}   # 末段截断显式标记（JSON 键，非新列）
            if flags["eligible"]:
                elig += 1
            if i in have:                   # 续跑：已提交的段不重写，ordinal 不冲突
                continue
            text_clean = None
            if clean:
                text_clean = clean_rules(ch)
                if needs_llm(text_clean):   # 规则修不掉的：本步不送 LLM，只留标记
                    flags = {**flags, "clean_pending_llm": True}
                    pending_llm += 1
            s.add(Segment(id=new_id("SEG"), work_id=w.id, ordinal=i, text=ch,
                          text_clean=text_clean,
                          chapter=prep.chapters[i] if prep is not None else None,
                          n_sentences=n_sents(ch), n_chars=len(ch), seg_version=2,
                          integrity=json.dumps(flags, ensure_ascii=False)))
            written += 1
            if written % BATCH == 0:
                s.commit()
        anchors = {"corpus_role": role, "segmenter": 2,   # JSON 无结构风险：存 role 原文
                   "eligible_rate": round(elig / max(1, total), 3)}
        if prep is not None:
            anchors["front_matter"] = prep.front_matter      # 元数据头逐行记账（不静默丢弃）
            anchors["last_truncated"] = prep.last_truncated
        w.anchors = json.dumps(anchors, ensure_ascii=False)
        w.note = _note(role, COMPLETE)      # 收尾提交：状态与全量段落同一事务落定
        s.commit()
        print(f"{title}: v2 段 {total}，合格 {elig}（{elig / max(1, total):.0%}），"
              f"字数 {len(text)}"
              + (f"，新段 text_clean 已写 {written}（其中 clean_pending_llm {pending_llm}）"
                 if clean else ""), flush=True)
        return {"status": "resumed" if resuming else "imported", "work_id": w.id,
                "total": total, "written": written}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    caveats = "--caveats" in argv
    clean = "--clean" in argv
    pos = [a for a in argv if a not in ("--caveats", "--clean")]
    if len(pos) != 3:
        print("用法: python scripts/import_corpus_v2.py <绝对路径> <标题> <角色说明> "
              "[--caveats] [--clean]", file=sys.stderr)
        return 2
    db.init_db()
    import_work(pos[0], pos[1], pos[2], caveats=caveats, clean=clean)
    return 0


if __name__ == "__main__":
    sys.exit(main())
