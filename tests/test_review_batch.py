"""盲评批次回归（2026-09-14）。

覆盖当天三处后端改动：
1. `GET /review/batch/{batch}` 概览端点（新增）。
2. `review/next` 的批次轮换必须覆盖**整批**——旧实现 `cur % min(10, len(rows))` 只轮 top-10，
   只浏览不判定时第 11 题之后永远轮不到（15 题批次直接漏 5 题）。
3. 批次过滤只出本批题目；且 `review/next` 按 experiment_id 隔离 → **批次必须单实验**。
"""
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api as api_mod
from app import db
from app.main import app
from app.models import Candidate, Experiment, Frame, ReviewItem, Segment, Work

client = TestClient(app)


def _reset_cursor():
    """_SERVE_CURSOR 是进程内模块级状态，跨用例必须清零。"""
    api_mod._SERVE_CURSOR.clear()


def _seed(exp_id, batch, n_batch, n_other=0, prompt_version="reconstruct_v1"):
    """1 实验 / 1 段 / 1 frame / N 候选 + N 评审项；前 n_batch 条带 `batch_{batch}` 标签。

    注意 db 里 PRAGMA foreign_keys=ON，frame_id / segment_id 必须是真实行，不能填占位串。
    prompt_version 默认用白名单里的 reconstruct_v1——盲评只出平行文本候选（见 config）。
    返回本批 ReviewItem.id 列表。
    """
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

        ids, total = [], n_batch + n_other
        for i in range(total):
            c = Candidate(experiment_id=exp_id, frame_id=fr.id, segment_id=seg.id,
                          anon_label=f"X{i:04d}", model="m", prompt_version=prompt_version,
                          text=f"候选文本{i}")
            s.add(c)
            s.flush()
            in_batch = i < n_batch
            r = ReviewItem(experiment_id=exp_id, subject_type="candidate", subject_id=c.id,
                           priority=float(total - i),
                           reasons=["human_upset"] + ([f"batch_{batch}"] if in_batch else []))
            s.add(r)
            s.flush()
            if in_batch:
                ids.append(r.id)
        s.commit()
    return ids


def test_batch_info_reports_counts():
    _reset_cursor()
    _seed("EXP-RB1", "rb1", 4, 2)
    d = client.get("/review/batch/rb1").json()
    assert d["batch"] == "batch_rb1"
    assert d["experiment_id"] == "EXP-RB1"
    assert (d["total"], d["done"], d["pending"]) == (4, 0, 4)


def test_batch_info_unknown_batch_404():
    _reset_cursor()
    _seed("EXP-RB2", "rb2", 1)
    assert client.get("/review/batch/nope").status_code == 404


def test_batch_rotation_covers_whole_batch_not_top10():
    """核心回归：15 条批次必须全部轮得到（旧实现只轮 top-10，会漏 5 条）。"""
    _reset_cursor()
    ids = _seed("EXP-RB3", "rb3", 15, 3)
    seen = set()
    for _ in range(15):
        r = client.get("/experiments/EXP-RB3/review/next?batch=rb3")
        assert r.status_code == 200, r.text
        seen.add(r.json()["review_id"])
    assert seen == set(ids), f"批次未全覆盖，漏：{set(ids) - seen}"


def test_batch_filter_excludes_non_batch_items():
    _reset_cursor()
    ids = _seed("EXP-RB4", "rb4", 3, 5)
    for _ in range(6):
        r = client.get("/experiments/EXP-RB4/review/next?batch=rb4")
        assert r.status_code == 200
        assert r.json()["review_id"] in ids, "批次过滤漏出了非本批题目"


def test_batch_is_isolated_per_experiment():
    """批次必须单实验：同名 batch 落在两个实验时，next 只出该实验的题。"""
    _reset_cursor()
    ids5 = _seed("EXP-RB5", "rb5", 2)
    _seed("EXP-RB6", "rb5", 2)
    d = client.get("/review/batch/rb5").json()
    assert d["total"] == 4
    assert d["experiments"] == ["EXP-RB5", "EXP-RB6"]
    got = set()
    for _ in range(5):
        r = client.get("/experiments/EXP-RB5/review/next?batch=rb5")
        assert r.status_code == 200
        got.add(r.json()["review_id"])
    assert got <= set(ids5), "next 串到了另一实验的题"


def test_batch_exhausted_returns_404():
    """批次判完 → 404（前端据此显示完成态，不得静默返回别的题）。"""
    _reset_cursor()
    ids = _seed("EXP-RB7", "rb7", 1)
    with db.session() as s:
        s.get(ReviewItem, ids[0]).status = "done"
        s.commit()
    assert client.get("/experiments/EXP-RB7/review/next?batch=rb7").status_code == 404


def test_review_next_refuses_non_parallel_candidates():
    """兜底守卫：非平行（自由续写）候选绝不能被端上盲评。

    2026-09-14 事故：r25 混进 3 条 recon_ctxonly_v1，A 讲"献出妹妹换命"、
    B 讲"钟华弹俞小塘额头"，"哪边更好"这个问题根本不成立。
    即使脏条目已躺在队列里（本用例就是），review/next 也必须跳过它。
    """
    _reset_cursor()
    _seed("EXP-RB8", "rb8", 3, prompt_version="recon_ctxonly_v1")
    r = client.get("/experiments/EXP-RB8/review/next?batch=rb8")
    assert r.status_code == 404, (
        f"非平行候选必须被过滤掉（队列应视为空），实得 {r.status_code}: {r.text[:120]}")


def test_review_next_serves_whitelisted_versions():
    """白名单里的版本仍要正常出题——守卫不能误伤。"""
    _reset_cursor()
    ids = _seed("EXP-RB9", "rb9", 2, prompt_version="recon_ctx_v1")
    r = client.get("/experiments/EXP-RB9/review/next?batch=rb9")
    assert r.status_code == 200
    assert r.json()["review_id"] in ids


# ── 跨实验取题 + 近段上文（2026-09-17 集霸太折磨了之后的改动）────
def test_near_context_is_default_and_full_is_available():
    """默认只给最近一段（阅读量的 97% 都在上文），全场景仍可展开。

    实测单题 4100 字、其中上文 3970 字；near 之后降到 ~200 字。
    """
    _reset_cursor()
    _seed("EXP-RV30", "n30", 1)
    r = client.get("/experiments/EXP-RV30/review/next", params={"batch": "n30"})
    assert r.status_code == 200
    d = r.json()
    assert "context" in d and "context_full" in d
    assert len(d["context"]) <= len(d["context_full"])
    assert d["ctx_mode"].startswith("near1/"), d["ctx_mode"]
    assert d["ctx_scope"] == "near"
    # 显式要求全场景时退回旧行为
    _reset_cursor()
    r2 = client.get("/experiments/EXP-RV30/review/next",
                    params={"batch": "n30", "ctx": "scene"})
    assert r2.status_code == 200
    assert not r2.json()["ctx_mode"].startswith("near1/")


def test_batch_endpoint_serves_across_experiments():
    """❗跨语料批必须走 /review/next?batch=：单实验路径会在第一个实验判完后
    误报「本批已全部判定」（x50 实测：斗罗 3 题判完即显示完成，实际还有 39 题）。"""
    _reset_cursor()
    _seed("EXP-RV31", "m31", 2)
    _seed("EXP-RV32", "m31", 2)
    # 单实验路径只出该实验的题
    _reset_cursor()
    ids_a = {client.get("/experiments/EXP-RV31/review/next",
                        params={"batch": "m31"}).json()["review_id"] for _ in range(3)}
    _reset_cursor()
    ids_b = {client.get("/experiments/EXP-RV32/review/next",
                        params={"batch": "m31"}).json()["review_id"] for _ in range(3)}
    assert ids_a.isdisjoint(ids_b)
    # 跨实验路径能同时出两个实验的题
    _reset_cursor()
    got = {client.get("/review/next", params={"batch": "m31"}).json()["review_id"]
           for _ in range(4)}
    assert len(got) >= 3
    assert got & ids_a and got & ids_b, "跨实验路径应同时覆盖两个实验"


# ── 游标落盘：重启不得回退（2026-09-17 集霸「题在那轮来轮去」）────
def test_serve_cursor_survives_process_restart():
    """进程重启后队列**不能**从第一批题重新开始。

    根因：游标原本只在内存里，重启归零 → 集霸反复看到同一批题。
    这里模拟重启：清掉进程内缓存（不清磁盘），再取题必须是后面的题。
    """
    _reset_cursor()
    api_mod._CURSOR_FILE.unlink(missing_ok=True)
    _seed("EXP-RV40", "c40", 3)
    seen = [client.get("/experiments/EXP-RV40/review/next",
                       params={"batch": "c40"}).json()["review_id"] for _ in range(2)]
    # 模拟重启：内存缓存清空，磁盘保留
    api_mod._SERVE_CURSOR.clear()
    third = client.get("/experiments/EXP-RV40/review/next",
                       params={"batch": "c40"}).json()["review_id"]
    assert third not in seen, "重启后应从第 3 题继续，而不是回到第 1 题"
    assert len({*seen, third}) == 3


def test_serve_cursor_file_is_written():
    _reset_cursor()
    api_mod._CURSOR_FILE.unlink(missing_ok=True)
    _seed("EXP-RV41", "c41", 2)
    client.get("/experiments/EXP-RV41/review/next", params={"batch": "c41"})
    data = json.loads(api_mod._CURSOR_FILE.read_text(encoding="utf-8"))
    assert "c41" in data and data["c41"] >= 1
