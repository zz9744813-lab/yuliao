"""A01 回归：盲评呈现绑定（审查 2026-09-20，project-audit-20260920-1810）。

事故复现（审查实测口径）：同一题出两次——第一页 A=原文，第二页 A=候选；
第一页提交 A，旧实现按**全局最新映射**解读 → 记成 candidate，最贵的用户
偏好标签被静默污染。修复契约：
1. 每次端题落一行不可变 ReviewPresentation（排列/上下文口径/两侧文本指纹），
   响应带 presentation_id；
2. 提交绑定 presentation_id → 按呈现**当时**的排列解读，重出题不再改语义；
3. pid 缺失（旧客户端）→ 回退该题最近一次持久化呈现（跨重启/多 worker）；
4. pid 与题目不匹配 → 400；pid 不存在 → 404；
5. 批注 target 同样按绑定呈现翻译。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api as api_mod
from app import db
from app.main import app
from app.models import Candidate, Experiment, Frame, ReviewItem, ReviewPresentation, Segment, Work

client = TestClient(app)

HUMAN_TEXT = "他推门进来，屋里没人，桌上那盏灯还亮着。"


def _seed(exp_id, batch, n=1):
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp_id):
            s.add(Experiment(id=exp_id, name="t", status="created", config={}, stats={}))
        w = Work(title="t-" + exp_id, source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text=HUMAN_TEXT, n_sentences=1,
                      n_chars=len(HUMAN_TEXT))
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
                          text="他推门进来，屋里没有人，桌上的灯依然亮着，一切如旧。")
            s.add(c)
            s.flush()
            r = ReviewItem(experiment_id=exp_id, subject_type="candidate",
                           subject_id=c.id, priority=1.0, reasons=[f"batch_{batch}"],
                           status="pending")
            s.add(r)
            s.flush()
            ids.append(r.id)
        s.commit()
    return ids


def _serve_once(exp_id, batch, monkeypatch, rand_value):
    """端一次题并把 human_first 钉死为 rand_value（0.0→A=人类，1.0→B=人类）。"""
    monkeypatch.setattr(api_mod, "random",
                        SimpleNamespace(random=lambda: rand_value))
    api_mod._SERVE_CURSOR.clear()
    item = client.get(f"/experiments/{exp_id}/review/next?batch={batch}").json()
    assert item["presentation_id"], "端题响应必须带 presentation_id（A01 契约）"
    # 人类原文在哪一侧按响应文本自证（不信钉子，信端出去的东西）
    hf = item["text_a"].startswith(HUMAN_TEXT)
    return item, hf


def test_two_tabs_same_item_each_bound(monkeypatch):
    """审查复现的主案：同题两页、排列相反，第一页提交必须按第一页解读。"""
    rid = _seed("EXP-PR1", "pr1")[0]
    page1, hf1 = _serve_once("EXP-PR1", "pr1", monkeypatch, 0.0)   # A=人类
    page2, hf2 = _serve_once("EXP-PR1", "pr1", monkeypatch, 1.0)   # B=人类
    assert hf1 and not hf2, "两页排列必须相反（钉住 rand 才有判别力）"
    # 旧页面（page1 的 pid）提交 A —— 必须按 page1 的排列解读成 human，
    # 不许被 page2 的最新映射翻转（旧实现的污染路径）。
    resp = client.post(f"/review/{rid}/verdict", json={
        "winner": "A", "reasons": [], "annotations": [],
        "presentation_id": page1["presentation_id"]})
    assert resp.status_code == 200, resp.text
    with db.session() as s:
        hv = s.get(ReviewItem, rid).human_verdict
    assert hv["winner_resolved"] == "human", \
        "第一页提交 A 被第二页的映射翻转——偏好标签污染（A01 原事故）"
    assert hv["presentation_id"] == page1["presentation_id"]
    assert hv["human_was_a"] is True


def test_pid_mismatch_and_unknown_rejected(monkeypatch):
    rid_a = _seed("EXP-PR2A", "pr2")[0]
    rid_b = _seed("EXP-PR2B", "pr2")[0]
    item_a, _ = _serve_once("EXP-PR2A", "pr2", monkeypatch, 0.0)
    item_b, _ = _serve_once("EXP-PR2B", "pr2", monkeypatch, 0.0)
    # 拿 B 题的呈现提交 A 题 → 400（呈现与题目不匹配）
    resp = client.post(f"/review/{rid_a}/verdict", json={
        "winner": "A", "reasons": [], "annotations": [],
        "presentation_id": item_b["presentation_id"]})
    assert resp.status_code == 400, "别题的呈现必须拒收"
    # 不存在的 pid → 404
    resp = client.post(f"/review/{rid_a}/verdict", json={
        "winner": "A", "reasons": [], "annotations": [],
        "presentation_id": "PR-nope"})
    assert resp.status_code == 404


def test_legacy_no_pid_falls_back_latest_db_presentation(monkeypatch):
    """旧客户端/重启：进程内映射清空后，按最近一次持久化呈现解读。"""
    import time
    rid = _seed("EXP-PR3", "pr3")[0]
    _serve_once("EXP-PR3", "pr3", monkeypatch, 0.0)                 # A=人类
    time.sleep(1.05)   # created_at 秒级精度——同秒双呈现无法定序，隔开才可测「最近」
    page2, hf2 = _serve_once("EXP-PR3", "pr3", monkeypatch, 1.0)   # B=人类（最新）
    api_mod._BLIND_MAP.clear()   # 模拟重启：进程内映射全丢
    resp = client.post(f"/review/{rid}/verdict", json={
        "winner": "A", "reasons": [], "annotations": []})           # 不带 pid
    assert resp.status_code == 200, resp.text
    with db.session() as s:
        hv = s.get(ReviewItem, rid).human_verdict
    assert hv["winner_resolved"] == ("human" if hf2 else "candidate"), \
        "回退必须按最近一次持久化呈现（page2: B=人类 → A=candidate）"
    assert hv["presentation_id"] == page2["presentation_id"], \
        "回退解读也要留呈现审计指针"


def test_annotations_follow_bound_presentation(monkeypatch):
    """批注 target 必须按绑定呈现翻译，不按最新映射。"""
    rid = _seed("EXP-PR4", "pr4")[0]
    page1, _ = _serve_once("EXP-PR4", "pr4", monkeypatch, 0.0)      # A=人类
    _serve_once("EXP-PR4", "pr4", monkeypatch, 1.0)                 # B=人类（最新）
    resp = client.post(f"/review/{rid}/verdict", json={
        "winner": "A", "reasons": [],
        "annotations": [{"side": "A", "start": 0, "end": 2, "text": "他推",
                          "kind": "用词", "note": ""}],
        "presentation_id": page1["presentation_id"]})
    assert resp.status_code == 200, resp.text
    with db.session() as s:
        hv = s.get(ReviewItem, rid).human_verdict
    a = hv["annotations"][0]
    assert a["target"] == "human", "按绑定呈现 A=人类；按最新映射翻就是反的"


def test_presentation_row_freezes_texts_and_order(monkeypatch):
    """落库的呈现行 = 端出瞬间的冻结快照（排列 + 两侧文本指纹）。"""
    rid = _seed("EXP-PR5", "pr5")[0]
    item, hf = _serve_once("EXP-PR5", "pr5", monkeypatch, 0.0)
    with db.session() as s:
        pr = s.get(ReviewPresentation, item["presentation_id"])
        assert pr is not None and pr.review_id == rid
        assert pr.human_first == hf
        assert pr.text_a_sha == api_mod._sha16(item["text_a"])
        assert pr.text_b_sha == api_mod._sha16(item["text_b"])
