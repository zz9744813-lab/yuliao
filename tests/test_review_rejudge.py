"""改判与盲评映射生命周期回归（2026-09-16）。

动机：UI 加了「改判」入口（本会话快照 + 服务端重端），后端随之改了三处，
每处都能单独把数据写脏，必须锁死：

1. 映射不再随判定弹出——否则改判时 A/B 无法翻回 human/candidate；
2. 已判题改判的**安全闸**：映射丢了（服务重启）时 A/B 投票必须 409 拒绝，
   不能让"原始 A/B"覆盖语义值（旧实现无状态校验，重启后可对任意已判题
   写入字面量 A/B，留下无法区分的脏判定）；
3. 批注偏移校验：越界夹回、文字对不上标 verified=False，不丢数据但下游可区分。

顺带回归 review_next 的空队列 bug：旧实现 `rows = [q.first()]` 把 None
包成 [None]（[None] 是真值），空队列走到 rows[cur % 1] 直接 AttributeError 500，
本该是 404（前端据此显示完成态）。
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api as api_mod
from app import db
from app.main import app
from app.models import (Candidate, Experiment, Frame, ReviewItem,
                        ReviewPresentation, Segment, Work)

client = TestClient(app)

SEG_TEXT = "他推门进来，屋里没人。"


def _reset():
    api_mod._SERVE_CURSOR.clear()
    api_mod._BLIND_MAP.clear()


def _seed(exp_id, batch, n_batch=1, n_other=0):
    """1 实验 / 1 段 / 1 frame / N 候选 + N 评审项；前 n_batch 条带批次标签。"""
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp_id):
            s.add(Experiment(id=exp_id, name="t", status="created", config={}, stats={}))
        w = Work(title="t-" + exp_id, source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text=SEG_TEXT, n_sentences=1, n_chars=12)
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=exp_id, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        ids = []
        for i in range(n_batch + n_other):
            c = Candidate(experiment_id=exp_id, frame_id=fr.id, segment_id=seg.id,
                          anon_label=f"X{i:04d}", model="m", prompt_version="reconstruct_v1",
                          text=f"候选文本{i}，够长。")
            s.add(c)
            s.flush()
            in_batch = i < n_batch
            r = ReviewItem(experiment_id=exp_id, subject_type="candidate", subject_id=c.id,
                           priority=1.0,
                           reasons=[f"batch_{batch}"] + (["human_upset"] if in_batch else []))
            s.add(r)
            s.flush()
            if in_batch:
                ids.append(r.id)
        s.commit()
    return ids


def _human_side(item) -> str:
    """题面里哪一侧是人类段（测试可看文本，真实用户看不到）。"""
    return "A" if item["text_a"] == SEG_TEXT else "B"


def _judge(item, winner, annotations=None):
    # A01：与真实前端一致——item 来自端题响应，自带 presentation_id
    return client.post(f"/review/{item['review_id']}/verdict",
                       json={"winner": winner, "reasons": [], "annotations": annotations or [],
                             "presentation_id": item.get("presentation_id", "")})


def test_empty_queue_returns_404_not_500():
    """空队列必须 404（旧实现 [None] 是真值 → AttributeError 500）。"""
    _reset()
    _seed("EXP-RJ0", "rj0", n_batch=0)
    r = client.get("/experiments/EXP-RJ0/review/next?batch=rj0")
    assert r.status_code == 404, f"空队列应 404，实得 {r.status_code}: {r.text[:200]}"


def test_mapping_survives_verdict():
    """判定后映射必须还在——改判要靠它翻译 A/B。"""
    _reset()
    _seed("EXP-RJ1", "rj1")
    item = client.get("/experiments/EXP-RJ1/review/next?batch=rj1").json()
    assert _judge(item, "A").status_code == 200
    assert api_mod._blind_get(item["presentation_id"]) is not None, "判定后呈现被弹掉了"


def test_rejudge_overwrites_with_rejudged_flag():
    _reset()
    _seed("EXP-RJ2", "rj2")
    item = client.get("/experiments/EXP-RJ2/review/next?batch=rj2").json()
    hs = _human_side(item)

    first = _judge(item, "A").json()
    assert first["rejudged"] is False

    # 改判：同一题换 B。winner_resolved 必须按**当前映射**翻译，不能照抄字面量
    second = _judge(item, "B").json()
    assert second["rejudged"] is True
    assert second["resolved"] == ("candidate" if hs == "A" else "human")

    with db.session() as s:
        hv = s.get(ReviewItem, item["review_id"]).human_verdict
    assert hv["winner_resolved"] == second["resolved"]
    assert hv["prev_winner_resolved"] == ("human" if hs == "A" else "candidate")
    assert hv["n_verdicts"] == 2


def test_rejudge_refused_when_presentation_gone():
    """已判题 + 呈现不存在（A01 落库前的历史题）+ 投 A/B → 必须 409。

    A01 后映射持久化在 review_presentations，进程重启不再是丢失场景；
    本测试把呈现行删掉，模拟「历史题从没被新代码端过」。"""
    _reset()
    _seed("EXP-RJ3", "rj3")
    item = client.get("/experiments/EXP-RJ3/review/next?batch=rj3").json()
    assert _judge(item, "A").status_code == 200
    with db.session() as s:
        s.query(ReviewPresentation).filter_by(
            review_id=item["review_id"]).delete()
        s.commit()
    api_mod._BLIND_MAP.clear()          # 模拟进程重启：进程内映射全丢
    r = _judge(item, "B")
    # A01 二轮：item 自带 pid——呈现行已删 → 绑定失效 404（不再有猜义路径）
    assert r.status_code == 404, f"呈现行已删的改判应按绑定失效拒绝，实得 {r.status_code}: {r.text[:200]}"
    assert "呈现" in r.text
    # 且原判定没被污染
    with db.session() as s:
        hv = s.get(ReviewItem, item["review_id"]).human_verdict
    assert hv["n_verdicts"] == 1 and hv["rejudged"] is False


def test_rejudge_after_restart_uses_persisted_presentation():
    """A01 二轮：重启（进程内映射清空）后改判——**pid 绑定在 DB 呈现行上
    存活**，带原 pid 提交照常解读；进程内缓存无关紧要。"""
    _reset()
    _seed("EXP-RJ3B", "rj3b")
    item = client.get("/experiments/EXP-RJ3B/review/next?batch=rj3b").json()
    hs = _human_side(item)
    human_vote = "A" if hs == "A" else "B"
    assert _judge(item, human_vote).status_code == 200
    api_mod._BLIND_MAP.clear()   # 模拟重启：进程内映射全丢
    r = _judge(item, human_vote)  # 改判仍带原 pid 投人类侧
    assert r.status_code == 200, r.text[:200]
    assert r.json()["resolved"] == "human", "pid 绑定跨重启存活，必须翻对"
    with db.session() as s:
        hv = s.get(ReviewItem, item["review_id"]).human_verdict
    assert hv["rejudged"] is True and hv["presentation_id"] == item["presentation_id"]


def test_pending_restart_rejects_unbound_ab():
    """A01 二轮（知识化方案 §1.1）：待判题有呈现行但提交无 pid → 409
    拒收（禁止猜最近一条，23:14 复现的误译路径堵死）；重取题带 pid
    提交照常翻译。"""
    _reset()
    _seed("EXP-RJ4", "rj4")
    item = client.get("/experiments/EXP-RJ4/review/next?batch=rj4").json()
    api_mod._BLIND_MAP.clear()   # 模拟重启
    r = client.post(f"/review/{item['review_id']}/verdict",
                    json={"winner": "A", "reasons": [], "annotations": []})
    assert r.status_code == 409 and "呈现绑定" in r.text, \
        "无绑定+有呈现行必须拒收，不许按最近呈现猜"
    with db.session() as s:
        assert s.get(ReviewItem, item["review_id"]).human_verdict is None, \
            "拒收的判定不许落库"
    # 重取题（带回新 pid）→ 提交照常
    item2 = client.get("/experiments/EXP-RJ4/review/next?batch=rj4").json()
    ok = _judge(item2, "A")
    assert ok.status_code == 200
    with db.session() as s:
        hv = s.get(ReviewItem, item2["review_id"]).human_verdict
    assert hv["human_was_a"] is not None
    assert hv["winner_resolved"] == ("human" if hv["human_was_a"] else "candidate")
    assert hv["presentation_id"]


def test_pending_never_served_now_rejected():
    """待判题 + 取不到呈现：**现在直接 409**，不再落"原始 A/B"。

    口径变更（审计 A01，2026-09-20，随 6da2a18 移植）：旧行为（本测试前身
    test_pending_never_served_records_raw）是照常落库 + 标 mapping_lost，
    但"原始 A/B"落库后没人知道它按哪套排列解读 —— 与已判题那条 409 规则自相矛盾，
    也正是审计要清的污染源。现在宁可让评审人重端一次，也不写语义不明的判定。

    （6da2a18 同名义的 test_pending_without_presentation_now_rejected 在本分支
    落在**从未端出**的题上：呈现行 + 进程内映射都在=可解释，DB 兜底会照常解读，
    拿不到"无从解释"这一态；从未端出的题才是 legacy 无绑定的真实拒绝场景。）"""
    _reset()
    rid = _seed("EXP-RJ4B", "rj4b")[0]   # 不走 next/serve——不产生任何呈现
    r = client.post(f"/review/{rid}/verdict",
                    json={"winner": "A", "reasons": [], "annotations": []})
    assert r.status_code == 409, f"无呈现可解释时必须 409，实得 {r.status_code}"
    with db.session() as s:
        hv = s.get(ReviewItem, rid).human_verdict
    assert not hv or not hv.get("winner_raw"), "被拒的提交不许落库"


def test_annotation_offset_verified():
    _reset()
    _seed("EXP-RJ5", "rj5")
    item = client.get("/experiments/EXP-RJ5/review/next?batch=rj5").json()
    hs = _human_side(item)
    # 人类段上 6..10 = 「屋里没人」，选中文字与原文一致 → verified
    ok = {"side": hs, "start": 6, "end": 10, "text": "屋里没人", "kind": "用词"}
    # 偏移对不上（文字乱填）→ verified=False，但数据仍落库
    bad = {"side": hs, "start": 6, "end": 10, "text": "根本不是这句", "kind": "节奏"}
    # 越界（end 超出文本长度）→ 夹回，verified=False
    over = {"side": hs, "start": 0, "end": 9999, "text": "x", "kind": "逻辑"}
    r = _judge(item, "A", [ok, bad, over])
    assert r.status_code == 200
    with db.session() as s:
        anns = s.get(ReviewItem, item["review_id"]).human_verdict["annotations"]
    by_text = {a["text"]: a for a in anns}
    assert by_text["屋里没人"]["verified"] is True
    assert by_text["根本不是这句"]["verified"] is False
    assert by_text["x"]["end"] == len(SEG_TEXT)      # 被夹回文本长度
    assert by_text["x"]["verified"] is False


def test_serve_done_item_returns_prev_consistent_with_new_ab():
    """服务端重端已判题：A/B 重洗，旧判定/批注必须按**新** A/B 位翻译。

    不依赖随机落位：原判语义侧（human/candidate）在新题面里落在哪一侧，
    由新题面文本本身推出，prev 必须与之一致。
    """
    _reset()
    _seed("EXP-RJ6", "rj6")
    item = client.get("/experiments/EXP-RJ6/review/next?batch=rj6").json()
    hs = _human_side(item)
    first = _judge(item, hs, [{"side": hs, "start": 6, "end": 10,
                               "text": "屋里没人", "kind": "用词"}]).json()
    assert first["resolved"] == "human"   # 投的就是人类段那一侧

    served = client.get(f"/experiments/EXP-RJ6/review/{item['review_id']}/serve").json()
    new_human_a = served["text_a"] == SEG_TEXT     # 重洗后人类段在哪侧
    expect_side = "A" if new_human_a else "B"
    prev = served["prev"]

    assert prev["winner_resolved"] == "human"
    assert prev["winner_side"] == expect_side
    # 旧批注的 side/偏移在新题面里必须对得上人类段原文
    assert len(prev["annotations"]) == 1
    a = prev["annotations"][0]
    assert a["side"] == expect_side
    assert served["text_" + a["side"].lower()][a["start"]:a["end"]] == "屋里没人"
    # 已判题的信号标签可以给了（判定后不再构成锚定）
    assert "human_upset" in served.get("tags", [])


def test_serve_pending_item_refused():
    _reset()
    _seed("EXP-RJ7", "rj7")
    item = client.get("/experiments/EXP-RJ7/review/next?batch=rj7").json()
    r = client.get(f"/experiments/EXP-RJ7/review/{item['review_id']}/serve")
    assert r.status_code == 400, "改判入口只接已判题"


def test_batch_done_list_and_404():
    _reset()
    ids = _seed("EXP-RJ8", "rj8", n_batch=2)
    r = client.get("/review/batch/rj8/done")
    assert r.status_code == 404, "还没有已判题时应 404"

    item = client.get("/experiments/EXP-RJ8/review/next?batch=rj8").json()
    _judge(item, "A")
    d = client.get("/review/batch/rj8/done").json()
    assert d["done"] == 1 and len(d["items"]) == 1
    row = d["items"][0]
    assert row["id"] == item["review_id"]
    assert row["winner"] in ("human", "candidate")
    assert row["rejudged"] is False and row["n_annotations"] == 0


def test_blind_map_fifo_cap(monkeypatch):
    """映射表有 FIFO 上限：超出后最旧的被逐出，不会无限膨胀。

    落盘打桩：本测试只验**内存**淘汰语义，不该真往 DATA_DIR 写呈现文件
    （原写法会把副作用渗到同目录其它测试，见会审 qwen 席意见）。
    """
    _reset()
    monkeypatch.setattr(api_mod, "_blind_save_locked", lambda: None)
    api_mod._BLIND_CAP = 2
    try:
        for i in range(3):
            api_mod._blind_put(f"RV-cap{i}", {"review_id": f"RV-cap{i}", "human_first": True,
                                              "ctx_mode": "x"})
        assert len(api_mod._BLIND_MAP) == 2
        assert "RV-cap0" not in api_mod._BLIND_MAP
        assert "RV-cap2" in api_mod._BLIND_MAP
    finally:
        api_mod._BLIND_CAP = 512
    _reset()
