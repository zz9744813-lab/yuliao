"""盲评「噪点批注」回归（2026-09-14）。

动机（集霸）：有些段落整体其实还可以，但总有几处坏的把整段拖垮。
只给 A/B 一个胜负会丢掉这个信息——批注是**用户亲手指认的缺陷位置**。

本文件锁死两件事：
1. 批注能随判定落库，且字段完整（side_raw/target/start/end/text/kind）；
2. **盲评翻译不能错**：A/B → human/candidate 必须与 human_was_a 一致，
   否则"人类段里的噪点"会被统计成"候选段里的噪点"，整份统计就反了。
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api as api_mod
from app import db
from app.main import app
from app.models import Candidate, Experiment, Frame, ReviewItem, Segment, Work

client = TestClient(app)


def _seed(exp_id, batch, n=1):
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp_id):
            s.add(Experiment(id=exp_id, name="t", status="created", config={}, stats={}))
        w = Work(title="t-" + exp_id, source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="他推门进来，屋里没人。",
                      n_sentences=1, n_chars=12)
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=exp_id, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        ids = []
        for i in range(n):
            c = Candidate(experiment_id=exp_id, frame_id=fr.id, segment_id=seg.id,
                          anon_label=f"X{i:04d}", model="m", prompt_version="reconstruct_v1",
                          text=f"候选文本{i}，够长。")
            s.add(c)
            s.flush()
            r = ReviewItem(experiment_id=exp_id, subject_type="candidate", subject_id=c.id,
                           priority=1.0, reasons=[f"batch_{batch}"], status="pending")
            s.add(r)
            s.flush()
            ids.append(r.id)
        s.commit()
    return ids


def _take_and_judge(exp_id, batch, annotations, winner="A"):
    api_mod._SERVE_CURSOR.clear()
    _seed(exp_id, batch)
    item = client.get(f"/experiments/{exp_id}/review/next?batch={batch}").json()
    body = {"winner": winner, "reasons": [], "annotations": annotations}
    resp = client.post(f"/review/{item['review_id']}/verdict", json=body)
    assert resp.status_code == 200, resp.text
    with db.session() as s:
        r = s.get(ReviewItem, item["review_id"])
        return dict(r.human_verdict), item


def test_annotations_persisted_with_full_fields():
    hv, _ = _take_and_judge("EXP-AN1", "an1", [
        {"side": "A", "start": 3, "end": 9, "text": "屋里没人", "kind": "用词", "note": ""},
    ])
    anns = hv["annotations"]
    assert len(anns) == 1 and hv["n_annotations"] == 1
    a = anns[0]
    assert (a["side_raw"], a["start"], a["end"], a["kind"]) == ("A", 3, 9, "用词")
    assert a["text"] == "屋里没人"


def test_annotation_target_matches_blind_mapping():
    """核心不变量：target 必须由 human_was_a 推出，不能直接照抄 A/B。"""
    hv, _ = _take_and_judge("EXP-AN2", "an2", [
        {"side": "A", "start": 0, "end": 2, "text": "候选", "kind": "节奏", "note": ""},
        {"side": "B", "start": 0, "end": 2, "text": "他推", "kind": "逻辑", "note": ""},
    ])
    hwA = hv["human_was_a"]
    assert hwA is not None, "正常路径下映射不应丢失"
    by_side = {a["side_raw"]: a["target"] for a in hv["annotations"]}
    assert by_side["A"] == ("human" if hwA else "candidate")
    assert by_side["B"] == ("candidate" if hwA else "human")


def test_invalid_side_dropped():
    hv, _ = _take_and_judge("EXP-AN3", "an3", [
        {"side": "C", "start": 0, "end": 2, "text": "x", "kind": "用词"},
        {"side": "A", "start": 0, "end": 2, "text": "y", "kind": "用词"},
    ])
    assert len(hv["annotations"]) == 1
    assert hv["annotations"][0]["side_raw"] == "A"


def test_no_annotations_is_fine():
    """批注是可选的——不标也要能正常交卷（不能因为缺字段报错）。"""
    hv, _ = _take_and_judge("EXP-AN4", "an4", [])
    assert hv["annotations"] == [] and hv["n_annotations"] == 0
    assert hv["winner_raw"] == "A"


def test_annotation_text_truncated():
    """超长文本要截断，避免有人粘贴整篇进来把 JSON 撑爆。"""
    hv, _ = _take_and_judge("EXP-AN5", "an5", [
        {"side": "A", "start": 0, "end": 1, "text": "字" * 999, "kind": "用词"},
    ])
    assert len(hv["annotations"][0]["text"]) == 300
