"""盲评呈现绑定（审计 A01 / A04，2026-09-20）。

审计复现的实账：旧实现只按 review_id 存**一份**全局映射，两个页面打开同一题、
或旧页面还在时重新出题，旧页面的选择会按**新**映射解释 —— 第一页提交 A，服务端记成 candidate。
污染的是最贵的用户偏好标签，批注归属跟着一起错。

本文件把"呈现不可变"钉死（会审两席意见已并入，见每条的括注）：
  1. 每次出题返回唯一 presentation_id，判定记录里能查到用的是哪一份呈现；
  2. 两页同题各自提交 → 各自按**自己那一次**的排列解释；排列**用 monkeypatch 钉死成反向**，
     保证这条守卫是确定性的，不会 50% 概率放水（会审 qwen 席：原写法对旧实现也可能过）；
  3. pid 未知/过期 → 409 拒绝（不回退猜测）；
  4. pid 与 review_id 不匹配（串页提交）→ 400 拒绝；
  5. 页面回传的两侧文本指纹与冻结值不符 → 409（冻结字段必须被真的用上，不是死字段）；
  6. 不带 pid 的提交**一律不按最近呈现猜义**（会审 qwen 席 [严重] 整改，A01 二轮口径）：
     有呈现行 ⇒ 409 拒收；从无呈现行的历史题投非 A/B 仍走「存原始值」binding=none；
  7. 呈现**落盘**：清内存后重新装载（模拟重启）旧 pid 仍可提交；文件损坏则备份 .corrupt 并告警；
  8. 落盘文件走 config.DATA_DIR（测试期即 conftest 的临时目录），不碰仓库真实状态（A04），
     `_reset()` 连文件一起删，测试之间不靠"整体覆盖写"间接隔离（会审两席）。
"""
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api as api_mod
from app import config, db
from app.main import app
from app.models import Candidate, Experiment, Frame, ReviewItem, Segment, Work

client = TestClient(app)

SEG_TEXT = "他推门进来，屋里没人。"


def _reset():
    """清内存**并**删落盘呈现文件：让每个测试的呈现态从零开始。"""
    api_mod._SERVE_CURSOR.clear()
    api_mod._BLIND_MAP.clear()
    api_mod._BLIND_LAST.clear()
    try:
        api_mod._present_file().unlink()
    except FileNotFoundError:
        pass


def _seed(exp_id: str, batch: str = "bp", n: int = 1):
    """1 实验 / 1 段 / 1 frame / n 候选 + n 评审项（全部带批次标签）。"""
    db.init_db()
    ids = []
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
        for i in range(n):
            c = Candidate(experiment_id=exp_id, frame_id=fr.id, segment_id=seg.id,
                          anon_label=f"X{i:04d}", model="m", prompt_version=config.SERVABLE_PROMPT_VERSIONS[0],
                          text=f"候选文本{i}，够长。")
            s.add(c)
            s.flush()
            r = ReviewItem(experiment_id=exp_id, subject_type="candidate", subject_id=c.id,
                           priority=1.0, reasons=[f"batch_{batch}"])
            s.add(r)
            s.flush()
            ids.append(r.id)
        s.commit()
    return ids


def _present(exp_id: str, batch: str = "bp"):
    r = client.get(f"/experiments/{exp_id}/review/next?batch={batch}")
    assert r.status_code == 200, r.text[:200]
    return r.json()


def _human_side_of(item) -> str:
    """从**服务端冻结的呈现**反推人类侧，而不是拿文本相等去猜。

    会审 qwen 席：等文本一旦被展示层归一化（去空白/省略号），相等判断会静默反向成假阳性。
    """
    served = api_mod._blind_get(item["presentation_id"])
    assert served is not None
    return "A" if served["human_first"] else "B"


def _judge(item, winner, pid="__from_item__", annotations=None, a_hash=None, b_hash=None):
    body = {"winner": winner, "reasons": [], "annotations": annotations or []}
    if pid == "__from_item__":
        body["presentation_id"] = item.get("presentation_id")
    elif pid is not None:
        body["presentation_id"] = pid
    if a_hash is not None:
        body["a_hash"] = a_hash
    if b_hash is not None:
        body["b_hash"] = b_hash
    return client.post(f"/review/{item['review_id']}/verdict", json=body)


def _stored(review_id: str) -> dict:
    with db.session() as s:
        r = s.get(ReviewItem, review_id)
        return dict(r.human_verdict or {})


def test_present_returns_presentation_id_and_persists_file():
    _reset()
    _seed("EXP-A1", "bp")
    item = _present("EXP-A1")
    assert item.get("presentation_id"), "出题必须返回 presentation_id（审计 A01）"
    assert item.get("a_hash") and item.get("b_hash"), "必须回传双文本指纹供页面证伪过期呈现"
    assert api_mod._blind_get(item["presentation_id"]) is not None
    f = api_mod._present_file()
    assert f.exists(), "呈现必须落盘（重启/多 worker 后仍能解析）"
    assert item["presentation_id"] in f.read_text(encoding="utf-8")


def test_two_pages_same_question_do_not_cross_contaminate(monkeypatch):
    """审计复现场景：两页同题，各自按自己的排列解释。

    排列用 monkeypatch 钉成**反向**（第一页 human 在 A、第二页 human 在 B），
    于是"把第二页的选择按第一页的映射解释"必然得出相反答案 —— 旧实现必挂，不靠概率。
    """
    _reset()
    _seed("EXP-A2", "bp")
    monkeypatch.setattr(api_mod.random, "random", lambda: 0.1)     # human_first=True
    p1 = _present("EXP-A2")
    monkeypatch.setattr(api_mod.random, "random", lambda: 0.9)     # human_first=False
    p2 = _present("EXP-A2")
    assert p1["review_id"] == p2["review_id"]
    assert p1["presentation_id"] != p2["presentation_id"], "同题重出必须是**新**呈现"
    assert _human_side_of(p1) == "A" and _human_side_of(p2) == "B", "两侧排列必须确实相反"
    assert p1["text_a"] != p2["text_a"]

    # 各自选"自己页面上的人类侧"：两次都必须解析成 human（旧实现第二次会被第一页的映射带歪）
    assert _judge(p1, "A").status_code == 200
    assert _stored(p1["review_id"])["winner_resolved"] == "human"
    assert _stored(p1["review_id"])["presentation_id"] == p1["presentation_id"]

    assert _judge(p2, "B").status_code == 200
    assert _stored(p2["review_id"])["winner_resolved"] == "human", \
        "第二页必须按它自己的排列解释（旧实现这里会按新映射错解）"
    assert _stored(p2["review_id"])["presentation_id"] == p2["presentation_id"]
    assert _stored(p2["review_id"])["presentation_binding"] == "presentation_id"


def test_unknown_or_expired_presentation_rejected():
    _reset()
    _seed("EXP-A3", "bp")
    item = _present("EXP-A3")
    r = _judge(item, "A", pid="deadbeefdeadbeef")
    assert r.status_code == 409, f"未知/过期呈现必须拒绝，实得 {r.status_code}"
    hv = _stored(item["review_id"])
    assert not hv.get("winner_raw"), "被拒绝的提交不许落库（会审 qwen 席：原断言近乎恒真）"


def test_page_text_fingerprint_mismatch_rejected():
    """页面拿着旧文本投新题：回传指纹与冻结值不符 ⇒ 409。"""
    _reset()
    _seed("EXP-A7", "bp")
    item = _present("EXP-A7")
    r = _judge(item, "A", a_hash="0000000000000000")
    assert r.status_code == 409, f"指纹不符必须拒绝，实得 {r.status_code}"
    assert not _stored(item["review_id"]).get("winner_raw")
    # 指纹正确时不拦
    ok = _judge(item, _human_side_of(item), a_hash=item["a_hash"], b_hash=item["b_hash"])
    assert ok.status_code == 200


def test_presentation_review_mismatch_rejected():
    """串页提交：拿 A 题的呈现去提交 B 题，必须 400。"""
    _reset()
    _seed("EXP-A4", "bp", n=2)
    a = _present("EXP-A4")
    b = _present("EXP-A4")
    if a["review_id"] == b["review_id"]:      # 队列只有 1 题时游标回绕：再造一题
        _seed("EXP-A4", "bp2", n=1)
        b = _present("EXP-A4", "bp2")
    assert a["review_id"] != b["review_id"]
    r = _judge(b, "A", pid=a["presentation_id"])
    assert r.status_code == 400, f"串页提交必须 400，实得 {r.status_code}"


def test_legacy_submit_without_pid_is_never_guessed():
    """会审 qwen 席 [严重] 整改：无 pid 分支恢复 A01 二轮契约（56ddc43 口径）。

    移植版曾把无 pid 提交经 _blind_latest **接受**并只记 binding=legacy_review_id——
    「最近呈现」正是被重新端题后的**新**映射，旧页面提交会被误译并 HTTP 200 写进
    最贵的偏好标签；留痕不是防线，且内存映射存活/重启逐出后同一提交命运不同
    （客户端结局取决于服务端重启时序）。现在钉死：
      · 该题存在任何呈现行 + 无 pid + 带 A/B 语义（winner A/B 或有批注）⇒ 409，不落库；
      · 完全没有呈现行的 pre-A01 历史题 + 非 A/B 判定 ⇒ 200「存原始值」，binding=none
        （那是如实存未知，不是猜）。
    """
    _reset()
    _seed("EXP-A5", "bp")
    item = _present("EXP-A5")            # 内存映射存活——旧实现正是在这条路径上猜义放行
    assert api_mod._blind_get(item["presentation_id"]) is not None
    for side in ("A", "B"):
        r = _judge(item, side, pid=None)     # 老前端：不带 pid
        assert r.status_code == 409, \
            f"有呈现行+无 pid+winner={side} 必须 409（禁止按最近呈现猜义），实得 {r.status_code}"
        assert "呈现绑定" in r.text
    # 批注同样携带 A/B 归属语义：tie + 批注、无 pid ⇒ 一样拒收
    ann = {"side": "A", "start": 0, "end": 5, "text": SEG_TEXT[:5], "kind": "用词"}
    r = _judge(item, "tie", pid=None, annotations=[ann])
    assert r.status_code == 409, "有呈现行+无 pid+带批注 ⇒ 归属无法确定，必须 409"
    hv = _stored(item["review_id"])
    assert not hv.get("winner_raw"), "被拒收的提交不许落库（留痕≠防线，这里根本不许留）"
    # pre-A01 历史题（该题从无呈现行）：非 A/B 判定仍走「存原始值」unresolved 路径
    old_rids = _seed("EXP-A5B", "bp5b")
    r2 = client.post(f"/review/{old_rids[0]}/verdict",
                     json={"winner": "tie", "reasons": [], "annotations": []})
    assert r2.status_code == 200, f"从无呈现的历史题投 tie 应存原始值，实得 {r2.status_code}"
    hv2 = _stored(old_rids[0])
    assert hv2["presentation_binding"] == "none"
    assert hv2["presentation_id"] is None
    assert hv2["winner_raw"] == "tie" and hv2["winner_resolved"] == "tie"
    # 会审 qwen 席 [一般] 补覆盖：fallthrough 的其余两种非 A/B 判定同样走
    # 「存原始值」而不是 500/409（这两条正是「served/pid_used 未初始化会 500」
    # 那一支，必须真跑钉住）。同时钉住 `_stored()` 返回非 None（否则是 AttributeError
    # 而非契约断言，报错信息会与契约无关）。
    for w in ("both_bad", "cant_judge"):
        rid = _seed("EXP-A5C", "bp5c")[0]
        r3 = client.post(f"/review/{rid}/verdict",
                         json={"winner": w, "reasons": [], "annotations": []})
        assert r3.status_code == 200,             f"从无呈现的历史题投 {w} 应存原始值（不得 500/409），实得 {r3.status_code}"
        hv3 = _stored(rid)
        assert hv3 is not None, "_stored() 返回 None ⇒ 提交没落库"
        assert hv3["presentation_binding"] == "none" and hv3["presentation_id"] is None
        assert hv3["winner_raw"] == w and hv3["winner_resolved"] == w
    # 会审 glm 席 [一般] 补覆盖：从无呈现行 + 带 side 批注 ⇒ 走 fallthrough 存原始值
    # （批注 side 不参与盲评翻译：human_first is None ⇒ target=None、verified=False），
    # 这是**保留语义**，必须钉住；否则日后有人把它改成 409 会静默改变既有契约。
    rid = _seed("EXP-A5D", "bp5d")[0]
    ann = {"side": "A", "start": 0, "end": 5, "text": "x", "kind": "用词"}
    r4 = client.post(f"/review/{rid}/verdict",
                     json={"winner": "tie", "reasons": [], "annotations": [ann]})
    assert r4.status_code == 200, f"从无呈现 + 带批注应存原始值，实得 {r4.status_code}"
    hv4 = _stored(rid)
    assert hv4["presentation_binding"] == "none" and hv4["presentation_id"] is None
    assert hv4["winner_raw"] == "tie"


def test_presentations_survive_restart():
    """清空内存后重新装载（模拟服务重启）：旧 pid 仍能提交，且仍按原排列解释。"""
    _reset()
    _seed("EXP-A6", "bp")
    item = _present("EXP-A6")
    pid = item["presentation_id"]
    api_mod._BLIND_MAP.clear()
    api_mod._BLIND_LAST.clear()
    assert api_mod._blind_get(pid) is None
    api_mod._blind_load()
    assert api_mod._blind_get(pid) is not None, "重启后必须能从落盘恢复呈现"
    assert _judge(item, _human_side_of(item)).status_code == 200
    assert _stored(item["review_id"])["winner_resolved"] == "human"


def test_corrupt_presentation_file_is_backed_up_and_warned(caplog):
    """损坏的落盘文件：备份 .corrupt + 告警，不许静默变成"空映射"（会审两席）。"""
    _reset()
    _seed("EXP-A8", "bp")
    item = _present("EXP-A8")
    f = api_mod._present_file()
    f.write_text("{ 这不是 JSON", encoding="utf-8")
    api_mod._BLIND_MAP.clear()
    api_mod._BLIND_LAST.clear()
    with caplog.at_level("WARNING"):
        api_mod._blind_load()          # 不许抛
    assert api_mod._BLIND_MAP == {}
    assert f.with_suffix(f.suffix + ".corrupt").exists(), "损坏文件必须被备份，不能直接丢"
    assert any("损坏" in r.message or "损坏" in str(r.msg) for r in caplog.records), "必须有告警日志"


def test_serve_one_without_presentation_marks_prev(monkeypatch, caplog):
    """改判入口取不到呈现时，prev 标 no_presentation，而不是猜一个位置（会审 qwen 席：无覆盖）。"""
    _reset()
    _seed("EXP-A9", "bp")
    item = _present("EXP-A9")
    assert _judge(item, _human_side_of(item)).status_code == 200
    # 同一请求内 _serve_payload 刚写过呈现，所以这条分支正常走不到 —— 用打桩强制它，
    # 验"不猜位置 + 留日志 + 不 500"（会审 qwen 席：该分支此前无覆盖）
    monkeypatch.setattr(api_mod, "_blind_get", lambda pid: None)
    with caplog.at_level("WARNING"):
        r = client.get(f"/experiments/EXP-A9/review/{item['review_id']}/serve")
    assert r.status_code == 200, r.text[:200]
    prev = r.json()["prev"]
    assert prev["winner_side"] is None
    assert "no_presentation" in (prev.get("note") or "")
    assert any("取不到呈现" in str(rec.msg) for rec in caplog.records), "降级必须留日志"


def test_latest_pid_index_is_capped(monkeypatch):
    """老前端索引 _BLIND_LAST 也要有上限，否则长跑内存无限涨（会审两席）。

    与本文件 FIFO 用例同一标准：落盘打桩，别把副作用渗给同目录其它测试。
    """
    _reset()
    monkeypatch.setattr(api_mod, "_blind_save_locked", lambda: None)
    monkeypatch.setattr(api_mod, "_BLIND_LAST_CAP", 2)
    for i in range(4):
        api_mod._blind_put(f"p{i}", {"review_id": f"RV-{i}", "human_first": True})
    assert len(api_mod._BLIND_LAST) <= 2
    assert len(api_mod._BLIND_MAP) <= api_mod._BLIND_CAP
    assert api_mod._BLIND_LAST_CAP <= api_mod._BLIND_CAP, \
        "索引上限不得大于映射上限，否则会留下 rid→已逐出 pid 的悬挂条目（会审 qwen 席）"
    _reset()


def test_legacy_without_any_presentation_rejected():
    """从无呈现行的历史题投 A/B：A/B 依赖一个从未存在过的排列——同样 409，
    不落"原始 A/B"（整改后无 pid 分支与内存映射存活与否无关，会审两席）。"""
    _reset()
    ids = _seed("EXP-A10", "bp")
    r = client.post(f"/review/{ids[0]}/verdict",
                    json={"winner": "A", "reasons": [], "annotations": []})
    assert r.status_code == 409, f"无呈现可解释时必须 409，实得 {r.status_code}"
    assert not _stored(ids[0]).get("winner_raw")


def test_save_merges_other_workers_presentations():
    """落盘是「读盘合并写」：别的 worker 已写入的呈现不许被本进程整表覆盖（会审 glm 席）。"""
    _reset()
    f = api_mod._present_file()
    api_mod._atomic_write_json(f, {"presentations": {"otherworker": {"review_id": "RV-o",
                                                                    "human_first": True}},
                                   "last": {"RV-o": "otherworker"}})
    api_mod._BLIND_MAP.clear()
    api_mod._BLIND_LAST.clear()
    api_mod._blind_put("mine", {"review_id": "RV-m", "human_first": False})
    data = json.loads(f.read_text(encoding="utf-8"))
    assert "otherworker" in data["presentations"], "别的 worker 的条目被覆盖 = 静默丢数据"
    assert "mine" in data["presentations"]
    assert data["last"]["RV-o"] == "otherworker"


def test_data_dir_is_resolved_at_call_time(monkeypatch, tmp_path):
    """A04 回归：路径必须**每次读写时**派生；导入之后再改 DATA_DIR 也要跟着走。

    审计 A04 的验收口径是"测试不许写到真实状态文件"——只有动态派生才成立。
    """
    _reset()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)   # 注意是 Path：db.init_db 直接 .mkdir()
    assert api_mod._present_file() == tmp_path / "blind_presentations.json"
    assert api_mod._cursor_file() == tmp_path / "serve_cursor.json"
    _seed("EXP-AD", "bp")
    item = _present("EXP-AD")
    assert (tmp_path / "blind_presentations.json").exists(), "落盘必须落在新 DATA_DIR"
    assert (tmp_path / "serve_cursor.json").exists(), "游标同理"
    assert _judge(item, _human_side_of(item)).status_code == 200
