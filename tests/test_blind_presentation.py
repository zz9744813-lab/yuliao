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
     `_reset()` 连文件一起删，测试之间不靠"整体覆盖写"间接隔离（会审两席）；
  9. **重装载原子性**（会审 glm 席 [一般] 残留，2026-09-26）：换 DATA_DIR 触发的整块换一套
     必须在同一临界区内做完——并发读路径**永不可见**"已清空、未装载"，否则内存表会暴露空窗（真实后果见 PORT §一.1 更正框）；
 10. 读盘/写盘口径一致：保存路径读盘失败也先备份 .corrupt 再覆盖写，不许顺手抹掉损坏现场。
"""
import json
import sys
import threading
import time
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
    """清内存**并**删落盘呈现文件、**并**清懒加载哨兵：让每个测试的呈现态从零开始。

    为什么 `_BLIND_LOADED_FOR` 必须一起清（会审 glm 席 [建议]，2026-09-26 显式化）：
    哨兵记的是"内存里已经装载了**哪个路径**"，而这条文件的落盘动作只按它做比较。
    某用例中途改过 `DATA_DIR`（`test_data_dir_is_resolved_at_call_time` 就是这么干）
    后失败退出，那个临时目录连同呈现文件已经没了，哨兵却还指着它；后续用例的懒加载
    于是取决于"这条用例的路径是否恰好等于上一条留下的哨兵"——不相等就整块换一套、
    相等就跳过装载而内存表其实是空的（有效 pid 被判过期 ⇒ 409）。
    现在靠"各用例路径一致"侥幸不触发，那是隐式依赖：清内存 = 清哨兵，二者同生同灭，
    每条用例的装载行为才可独立推理。
    """
    api_mod._SERVE_CURSOR.clear()
    api_mod._BLIND_MAP.clear()
    api_mod._BLIND_LAST.clear()
    api_mod._BLIND_LOADED_FOR = None
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


def _pad_presentations(blob: str, n: int = 400) -> str:
    """把落盘文件撑大（同 pid 仍在），顺带把"清空 → 读盘装载"这段自然窗口拉宽：
    并发回归不许靠调度运气命中，否则改回旧实现也可能侥幸判绿。"""
    data = json.loads(blob)
    pres = data.setdefault("presentations", {})
    for i in range(n):
        pres[f"filler{i}"] = {"review_id": f"RV-filler{i}", "human_first": True,
                              "created_at": "2026-01-01T00:00:00Z", "pad": "x" * 128}
    return json.dumps(data, ensure_ascii=False)


def test_concurrent_reload_never_marks_valid_presentation_expired(tmp_path):
    """会审 glm 席 [一般] 残留：重装载必须在**同一临界区**内完成（清空 + 装载 + 置哨兵）。

    旧形态是"持锁清空 → **释放锁** → `_blind_load()` → 再持锁置哨兵"。中间锁是放开的、
    哨兵也没置位，另一个线程的读路径正好落进"已清空、未装载"就拿回 None。

    后果更正（2026-09-26 独立审查席 [一般 1]，主控复核成立）：**不是**上层判 409 ——
    有效 pid 的 ReviewPresentation 行端题时已 commit，提交路径的 DB 兜底会重建 served 并 200。
    真实后果是指纹证伪被静默跳过、改判入口旧判定翻不回 A/B（详见 PORT §一.1 的更正框）。
    本用例钉的是"内存表不得暴露空窗"这条不变量本身；下面那条 200 断言属**装饰性**——
    即使窗口开着，DB 兜底也会把它救成 200，故它证伪不了本用例声称要防的东西。
    真正的判别力来自 reader 线程里直接钉内存表的 `api_mod._blind_get(pid) is None`。
    单 worker/本文件既有用例都看不见这条，多线程部署下是真缺陷。

    构造要点：两个 DATA_DIR 的落盘内容**都含同一个 pid**，所以它任何时刻都算有效；
    一个线程反复换 DATA_DIR 触发整块换一套，多个线程反复 `_blind_get(pid)`。
    修复后读路径要么看到旧一整套、要么看到新一整套，永远看不到空表。
    """
    _reset()
    stop = threading.Event()
    orig_dir = config.DATA_DIR
    missing: list[str] = []
    errors: list[str] = []
    dir_a, dir_b = tmp_path / "da", tmp_path / "db"
    dir_a.mkdir()
    dir_b.mkdir()
    try:
        config.DATA_DIR = dir_a
        _seed("EXP-CC", "bpcc")
        item = _present("EXP-CC", "bpcc")   # 批次标签必须与 _seed 的一致，否则队列是空的
        pid = item["presentation_id"]
        assert api_mod._blind_get(pid) is not None
        blob = _pad_presentations((dir_a / "blind_presentations.json").read_text(encoding="utf-8"))
        (dir_a / "blind_presentations.json").write_text(blob, encoding="utf-8")
        (dir_b / "blind_presentations.json").write_text(blob, encoding="utf-8")
        api_mod._BLIND_MAP.clear()
        api_mod._BLIND_LOADED_FOR = None          # 强制第一次读就走重装载

        def churn():
            i = 0
            while not stop.is_set():
                config.DATA_DIR = dir_a if i % 2 else dir_b
                i += 1

        def reader():
            while not stop.is_set():
                try:
                    if api_mod._blind_get(pid) is None:
                        missing.append(pid)
                        return
                except Exception as e:            # 读路径也不许崩成异常（等价于 500）
                    errors.append(repr(e))
                    return

        threads = ([threading.Thread(target=reader, daemon=True) for _ in range(8)]
                   + [threading.Thread(target=churn, daemon=True) for _ in range(2)])
        for t in threads:
            t.start()
        deadline = time.time() + 3.0
        while time.time() < deadline and not missing and not errors:
            time.sleep(0.02)
        stop.set()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"并发读路径抛异常：{errors[:3]}"
        assert not missing, (
            "有效 pid 在换目录重装载的窗口里被读成不存在 = 内存表暴露了空窗；"
            "装载必须与清空、置哨兵处在同一临界区内")
        # 契约不动：压力过后同一份呈现仍可提交。**判别力边界**（2026-09-26 独立审查席 [一般 1]）：
        # 这条 200 断言是**装饰性**的——提交路径有 DB 兜底（api.py:1101-1109），即使内存表处于
        # 空窗也会被救成 200，故它证伪不了"空窗存在"；真正的判别力在上面 reader 线程
        # （直接钉 `_blind_get` 返回 None）。保留它是为了钉"DB 兜底这条契约本身没被顺手改掉"。
        config.DATA_DIR = dir_a
        r = _judge(item, _human_side_of(item))
        assert r.status_code == 200, \
            f"并发重装载后有效呈现必须仍可提交（DB 兜底契约），实得 {r.status_code}"
    finally:
        stop.set()
        config.DATA_DIR = orig_dir
        _reset()


def test_reload_never_exposes_a_lock_free_cleared_window(monkeypatch, tmp_path):
    """把"同一临界区"这条不变量**单独**钉死（`_blind_ensure_loaded` 这一层，不靠读路径兜）。

    为什么还要这条：修复是两道独立防线——(1) `_blind_ensure_loaded` 内部清空+装载+置哨兵
    在同一临界区，(2) `_blind_get` 整段持锁。只退化 (1) 时，端到端那条
    `test_concurrent_reload_never_marks_valid_presentation_expired` 会被 (2) 兜住而侥幸放绿，
    钉不住这一层。所以本条**绕过 `_blind_get`**，只调 `_blind_ensure_loaded`。

    做法（确定性，不靠调度运气）：把"清空已完成、装载还没开始"这段人为撑开——退化形态正是
    在这段里调了会自己取锁的装载函数。此时若锁是**空的**，就说明存在"已清空 + 无锁"的空窗，
    读路径能落进来把有效 pid 判成不存在（409）；修复后装载内联在持锁段里，这段空窗不存在。
    """
    _reset()
    dir_a, dir_b = tmp_path / "gap_a", tmp_path / "gap_b"
    dir_a.mkdir()
    dir_b.mkdir()
    orig_dir = config.DATA_DIR
    target = dir_b / "blind_presentations.json"
    target.write_text(json.dumps(
        {"presentations": {"held-pid": {"review_id": "RV-h", "human_first": True}},
         "last": {"RV-h": "held-pid"}}, ensure_ascii=False), encoding="utf-8")

    in_gap = threading.Event()
    resume = threading.Event()
    real_load = api_mod._blind_load

    def pausing_load():
        # 退化形态在"锁已释放、装载未开始"处走到这里；修复形态不走本函数（在锁内直接装载）。
        in_gap.set()
        resume.wait(10)
        real_load()

    monkeypatch.setattr(api_mod, "_blind_load", pausing_load)
    t = None
    try:
        config.DATA_DIR = dir_b
        api_mod._BLIND_MAP["stale-pid"] = {"review_id": "RV-s", "human_first": False}
        api_mod._BLIND_LOADED_FOR = str(dir_a / "blind_presentations.json")  # 强制重装载
        t = threading.Thread(target=api_mod._blind_ensure_loaded, daemon=True)
        t.start()
        in_gap.wait(1.0)          # 修复形态：这段空窗不存在，等满 1.0s 也不置位
        lock_free_in_gap = False
        if in_gap.is_set():
            got = api_mod._BLIND_LOCK.acquire(timeout=1.0)
            if got:
                lock_free_in_gap = True
                api_mod._BLIND_LOCK.release()
        assert not lock_free_in_gap, (
            "重装载把「已清空」与「装载」拆到了两个临界区：清空之后、装载之前锁是空的，"
            "读路径能落进这个空窗把有效 pid 判成不存在（409）。"
            "装载必须与清空、置哨兵在同一临界区内一次做完。")
        resume.set()
        t.join(timeout=10)
        assert not t.is_alive(), "装载线程没退出"
        # 两种形态最终都该装载成功（本条只钉"有没有空窗"，不钉别的）
        assert api_mod._BLIND_LOADED_FOR == str(target)
        assert "held-pid" in api_mod._BLIND_MAP, "重装载后新目录的呈现必须在位"
        assert "stale-pid" not in api_mod._BLIND_MAP, "旧目录的残留必须被整块换掉"
    finally:
        resume.set()
        if t is not None:
            t.join(timeout=10)
        monkeypatch.undo()
        config.DATA_DIR = orig_dir
        _reset()


def test_save_backs_up_corrupt_file_before_overwriting(caplog):
    """口径统一（会审 glm 席 [一般] 残留）：保存路径遇到损坏文件，先备份 `.corrupt` 再覆盖写。

    旧写法只告警一句"本次直接覆盖写"，把损坏现场一并抹掉——与 `_blind_load`
    （备份 + 告警）两种口径。`test_save_merges_other_workers_presentations`
    只覆盖正常合并路径，这里补损坏分支。
    绕开 `_blind_put`（它的懒加载会先把现场处理掉），直接持锁调用保存路径。
    """
    _reset()
    f = api_mod._present_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    corrupt = "{ 这不是 JSON"
    f.write_text(corrupt, encoding="utf-8")
    api_mod._BLIND_MAP["mine"] = {"review_id": "RV-c", "human_first": True}
    api_mod._BLIND_LAST["RV-c"] = "mine"
    with caplog.at_level("WARNING"):
        with api_mod._BLIND_LOCK:
            api_mod._blind_save_locked()
    backup = f.with_suffix(f.suffix + ".corrupt")
    assert backup.exists(), "损坏现场必须先备份：直接覆盖写 = 丢失审计现场"
    assert backup.read_text(encoding="utf-8") == corrupt, "备份里要原样留着损坏内容"
    assert f.exists(), "覆盖写照常发生（本进程呈现不许丢）"
    data = json.loads(f.read_text(encoding="utf-8"))
    assert "mine" in data["presentations"]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("损坏" in m and str(f) in m for m in msgs), \
        f"损坏分支必须告警且含文件路径，实得 {msgs}"


def test_save_backs_up_unreadable_present_file_before_overwriting(monkeypatch, tmp_path, caplog):
    """另一半口径：读盘**失败**（非 FileNotFoundError）同样先备份再覆盖写。

    用"路径是个目录"造出稳定的 IsADirectoryError，不靠文件系统权限的运气。
    """
    _reset()
    bad = tmp_path / "blind_presentations.json"
    bad.mkdir()
    monkeypatch.setattr(api_mod, "_present_file", lambda: bad)
    api_mod._BLIND_MAP["mine"] = {"review_id": "RV-u", "human_first": True}
    with caplog.at_level("WARNING"):
        with api_mod._BLIND_LOCK:
            api_mod._blind_save_locked()
    assert bad.with_suffix(bad.suffix + ".corrupt").exists(), \
        "读盘失败的现场也要保住（备份）而不是直接被覆盖"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("损坏" in m and str(bad) in m for m in msgs), \
        f"必须告警且含文件路径（会审两席口径：只给批次键定位不到数据目录），实得 {msgs}"


def test_reset_clears_lazy_load_sentinel():
    """`_reset()` 必须连 `_BLIND_LOADED_FOR` 一起清（会审 glm 席 [建议]：隐式依赖显式化）。

    哨兵漏清 ⇒ 下一条用例是否装载取决于"路径是否恰好等于上一条留下的哨兵"，
    有效 pid 可能被空表判成过期 409，而这条用例本身没做过任何清表动作。
    """
    _reset()
    assert api_mod._BLIND_LOADED_FOR is None
    api_mod._BLIND_LOADED_FOR = str(api_mod._present_file())   # 模拟用例中途改过 DATA_DIR
    _reset()
    assert api_mod._BLIND_LOADED_FOR is None, \
        "_reset() 漏清哨兵 ⇒ 后续用例的懒加载行为不可推理"
