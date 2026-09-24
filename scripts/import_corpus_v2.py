"""Phase 1.5 语料导入（v2 切分器 + integrity + 语料角色标注）。用法：
python scripts/import_corpus_v2.py <绝对路径> <标题> <角色说明> [--caveats]

`--caveats`（显式开关，默认关＝既有行为逐字不变）：按独立核查席三条 caveat
硬化——卷首元数据剥离并记账、末段截断显式标记（integrity JSON "truncated"）、
chapter 从章节标题行回填。逻辑在 app/corpus_import_v2.py（纯函数）。

中断语义（审计《language-genome-code-audit-20260923》非阻断项）：大书每 BATCH 段
提交一次，Ctrl-C / 崩溃 / WAL 锁失败都会留下**半本**——旧口径重跑只看 `Work.source`
就判「已导入过」直接退出，半本永远补不齐，下游也没有任何「未完成」标记可识别。
现在 Work 一落库就带 `import_state: partial`，全部段落写完的**收尾提交**才翻
`complete`（同一事务，状态与段数不会各说各话）；重跑遇 partial 按 ordinal 续补缺口，
源文件与库内已有段不一致时清理该书整本重导——两条路径都确定且幂等。"""
import json
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


def _note(role: str, state: str) -> str:
    return f"corpus_role: {role}; segmenter=v2; {STATE_KEY}: {state}"


def _norm_note(work: Work) -> str:
    return (work.note or "").replace("；", ";")


def is_ours(work: Work) -> bool:
    """这行 Work 是不是本脚本（v2 导入）的产物。

    同 `file:路径` 的行也可能是别人写的（`app.corpus.import_file` 的 v1 本、
    API 手建的 Work）——那些不参与续跑判定，沿用旧的「已导入过」直接跳过。
    否则一次重跑会把 v1 书的段当成「不一致的半本」清掉。"""
    note = _norm_note(work)
    return f"{STATE_KEY}:" in note or "segmenter=v2" in note


def import_state(work: Work) -> str:
    """这本书在库里算不算「写完」。

    本次改动前的存量行 note 里没有 `import_state`：回退看 `anchors`——旧代码
    anchors 只在收尾提交写，有 anchors 即当时的完整本（真半本没机会写它）。"""
    for part in _norm_note(work).split(";"):
        key, _, val = part.partition(":")
        if key.strip() == STATE_KEY and val.strip():
            return val.strip()
    return COMPLETE if work.anchors else PARTIAL


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


def import_work(path: str, title: str, role: str, *, caveats: bool = False) -> dict:
    """导入一本书。返回 {status: imported|resumed|skipped, work_id, total, written}。

    分批提交中途炸掉只会留下 `import_state: partial` 的书（下游不得当完整本用），
    下一次重跑补齐缺口；完整本重跑保持既有「已导入过」幂等跳过。
    caveats=True 时走 app.corpus_import_v2 的三条修复口径（默认 False＝旧行为）。"""
    text = _read_text_loose(Path(path))
    prep = corpus_import_v2.prepare_import(text, title=title) if caveats else None
    chunks = prep.chunks if prep is not None else segmenter_v2.make_segments_v2(text)
    total = len(chunks)
    src = f"file:{path}"
    parsed_author = (prep.front_matter.get("author") if prep else None) or None
    with db.session() as s:
        w = s.query(Work).filter_by(source=src).first()
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

        written = elig = 0
        for i, ch in enumerate(chunks):
            flags = si.analyze(ch, ordinal=i)
            if prep is not None and prep.last_truncated and i == total - 1:
                flags = {**flags, "truncated": True}   # 末段截断显式标记（JSON 键，非新列）
            if flags["eligible"]:
                elig += 1
            if i in have:                   # 续跑：已提交的段不重写，ordinal 不冲突
                continue
            s.add(Segment(id=new_id("SEG"), work_id=w.id, ordinal=i, text=ch,
                          chapter=prep.chapters[i] if prep is not None else None,
                          n_sentences=0, n_chars=len(ch), seg_version=2,
                          integrity=json.dumps(flags, ensure_ascii=False)))
            written += 1
            if written % BATCH == 0:
                s.commit()
        anchors = {"corpus_role": role, "segmenter": 2,
                   "eligible_rate": round(elig / max(1, total), 3)}
        if prep is not None:
            anchors["front_matter"] = prep.front_matter      # 元数据头逐行记账（不静默丢弃）
            anchors["last_truncated"] = prep.last_truncated
        w.anchors = json.dumps(anchors, ensure_ascii=False)
        w.note = _note(role, COMPLETE)      # 收尾提交：状态与全量段落同一事务落定
        s.commit()
        print(f"{title}: v2 段 {total}，合格 {elig}（{elig / max(1, total):.0%}），"
              f"字数 {len(text)}", flush=True)
        return {"status": "resumed" if resuming else "imported", "work_id": w.id,
                "total": total, "written": written}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    caveats = "--caveats" in argv
    pos = [a for a in argv if a != "--caveats"]
    if len(pos) != 3:
        print("用法: python scripts/import_corpus_v2.py <绝对路径> <标题> <角色说明> "
              "[--caveats]", file=sys.stderr)
        return 2
    db.init_db()
    import_work(pos[0], pos[1], pos[2], caveats=caveats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
