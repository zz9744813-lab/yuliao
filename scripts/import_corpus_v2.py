"""Phase 1.5 语料导入（v2 切分器 + integrity + 语料角色标注）。用法：
python scripts/import_corpus_v2.py <绝对路径> <标题> <角色说明>"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, segment_integrity as si, segmenter_v2
from app.corpus import _read_text_loose
from app.ids import new_id
from app.models import Segment, Work

path, title, role = sys.argv[1], sys.argv[2], sys.argv[3]
db.init_db()
text = _read_text_loose(Path(path))
with db.session() as s:
    src = f"file:{path}"
    if s.query(Work).filter_by(source=src).first():
        print("已导入过:", title); sys.exit(0)
    w = Work(id=new_id("WK"), title=title, author=None, source=src,
             note=f"corpus_role: {role}; segmenter=v2")
    s.add(w); s.flush()
    chunks = segmenter_v2.make_segments_v2(text)
    elig = 0
    for i, ch in enumerate(chunks):
        flags = si.analyze(ch, ordinal=i)
        if flags["eligible"]: elig += 1
        s.add(Segment(id=new_id("SEG"), work_id=w.id, ordinal=i, text=ch,
                      n_sentences=0, n_chars=len(ch), seg_version=2,
                      integrity=json.dumps(flags, ensure_ascii=False)))
        if (i + 1) % 2000 == 0:   # 大书分批提交：SQLite 变量数上限 + WAL 锁窗口
            s.commit()
    anchors = {"corpus_role": role, "segmenter": 2,
               "eligible_rate": round(elig / max(1, len(chunks)), 3)}
    w.anchors = json.dumps(anchors, ensure_ascii=False)
    s.commit()
    print(f"{title}: v2 段 {len(chunks)}，合格 {elig}（{elig/max(1,len(chunks)):.0%}），字数 {len(text)}")
