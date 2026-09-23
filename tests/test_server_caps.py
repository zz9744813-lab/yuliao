"""实验入参枚举 + 上限回归（审计 P1 2026-09-23 余项，服务端收口）。

锁死 POST /experiments（app/api.ExperimentIn）四条口径：
1. granularities 只允许 S/M/L 无重复子集；非法值/注入串 → 422（不静默过滤，
   含注入串 `"><img src=x onerror=alert(1)>`）；
2. 列表字段（recon_models/judge_models/work_ids/temperatures）有元素个数、
   单元素长度与字符集上限；数值字段（n_segments/samples_per_pair/
   adversarial_k/concurrency）有区间上下限——越界必拒、界内放行、**不 clamp**；
3. 拒绝 = 零副作用：不建 experiment 行、不建 job 行；
4. 空列表 [] 视同未提供（刻意约定，与既有前端空表单 falsy 落默认一致）：
   granularities=[] → 落回默认 ["S","M","L"]，而不是零粒度空实验。

另钉独立审查 2026-09-23 BLOCK 判出的两个 [严重] 残余项（lg-fix-server-caps-residual）：
F1 批注 kind 服务端强枚举（口径源 = index.html:1108 MARK_KINDS，越界 422 零写入）；
F2 GET /segments 翻页上界（limit ≤200、offset+limit ≤5000 总量闸，越界 422 不 clamp，
   [:80] 预览硬上限不变式不回退）。

纯离线：TestClient + conftest 的临时 SQLite（LG_LLM_MODE=mock），
不连真网关、不写真库；上限只走 pydantic 校验，不触发任何 stage 执行。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import api, corpus, db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Experiment, Job, ReviewItem, Work  # noqa: E402

SEED_TEXT = (
    "天擦黑的时候他进了院子。院门没闩，他一推就开。屋里点着灯，人影晃了一下。\n"
    "\"回来了？\"她问。他没有答，把斗笠摘下来挂在门后。\n"
    "桌上摆着饭菜，还冒着热气。他坐下来，先喝了一口汤。汤是温的，早就放了好一阵了。\n"
    "她坐在对面看他的动作，始终没有再开口。窗外有虫鸣，一声长一声短。\n"
    "他放下碗，说我明早走。她点了点头，转身去收拾灶膛里的火。\n"
    "火光映着她的侧脸，一闪一闪。他忽然觉得喉咙有些发紧，却什么也没说。\n"
)

# 注入串（审计点名的验收用例）：前端收口另派 lg-fix-frontend-escape，
# 这里钉的是**服务端**不落库、不透出。
INJECT_GRAN = "\"><img src=x onerror=alert(1)>"


@pytest.fixture()
def client():
    db.init_db()
    with db.session() as s:
        if s.query(Work).count() == 0:  # 合法用例要能建成实验：先备语料
            corpus.add_work(s, title="上限测试书", text=SEED_TEXT * 4,
                            source="test:caps")
            s.commit()
    return TestClient(app)


def _counts() -> tuple[int, int]:
    with db.session() as s:
        return s.query(Experiment).count(), s.query(Job).count()


def _assert_rejected(client, body: dict):
    """越界 → 422 且零副作用（不建 experiment 行、不建 job 行）。"""
    before = _counts()
    r = client.post("/experiments", json=body)
    assert r.status_code == 422, f"应 422，实得 {r.status_code}: {r.text[:400]}"
    assert _counts() == before, f"越界拒绝产生了副作用（experiment/job 行数变化）: {body}"


def _assert_ok(client, body: dict) -> dict:
    before_exp, _ = _counts()
    r = client.post("/experiments", json=body)
    assert r.status_code == 200, f"合法值被误拒 {r.status_code}: {r.text[:400]}"
    with db.session() as s:
        assert s.query(Experiment).count() == before_exp + 1
    return r.json()


# ── granularities：枚举白名单 ─────────────────────────────────

def test_granularities_injection_string_422(client):
    """验收点名用例：注入串必须 422 且零副作用。"""
    _assert_rejected(client, {"granularities": ["S", INJECT_GRAN]})
    _assert_rejected(client, {"granularities": [INJECT_GRAN]})


def test_granularities_illegal_value_422_not_dropped(client):
    """非法粒度整单拒（不静默丢弃坏值留下好值）。"""
    _assert_rejected(client, {"granularities": ["S", "XL"]})
    _assert_rejected(client, {"granularities": ["s"]})       # 大小写不豁免
    _assert_rejected(client, {"granularities": ["S", "S"]})  # 重复 = 同粒度多跑


def test_granularities_legal_subset_passes(client):
    out = _assert_ok(client, {"n_segments": 2, "granularities": ["S", "M"]})
    assert out["config"]["granularities"] == ["S", "M"]


def test_empty_list_means_unset_not_zero_experiment(client):
    """刻意约定：[] 视同未提供 → 落 DEFAULT_CONFIG 的 S/M/L，
    而不是零粒度零帧的废实验（口径写进 ExperimentIn docstring）。"""
    out = _assert_ok(client, {"granularities": [], "recon_models": [],
                              "temperatures": [], "work_ids": []})
    assert out["config"]["granularities"] == ["S", "M", "L"]


# ── 列表字段：个数 / 长度 / 字符集上限 ────────────────────────

def test_recon_models_count_cap(client):
    ok = ["mA", "mB", "mC", "mD", "mE", "mF", "mG", "mH"]
    assert api._MAX_MODEL_LIST == 8
    _assert_ok(client, {"n_segments": 1, "recon_models": ok})
    _assert_rejected(client, {"recon_models": ok + ["mI"]})


def test_judge_models_count_and_charset_cap(client):
    _assert_ok(client, {"n_segments": 1, "judge_models": ["jA"]})
    _assert_rejected(client, {"judge_models": [f"j{i}" for i in range(9)]})
    # 注入字符（引号/尖括号/空白/换行）进不了模型名
    _assert_rejected(client, {"judge_models": ['"><script>alert(1)</script>']})
    _assert_rejected(client, {"judge_models": ["bad model;rm -rf /"]})
    _assert_rejected(client, {"judge_models": ["model\nwith-newline"]})


def test_model_name_length_cap(client):
    _assert_ok(client, {"n_segments": 1,
                        "recon_models": ["real.model/name:v1-x"]})
    _assert_rejected(client, {"recon_models": ["m" * 65]})


def test_work_ids_cap_and_charset(client):
    with db.session() as s:
        wid = s.query(Work).first().id
    _assert_ok(client, {"n_segments": 1, "work_ids": [wid]})
    _assert_rejected(client, {"work_ids": [f"WK-{i:012d}" for i in range(65)]})
    _assert_rejected(client, {"work_ids": ["W" * 33]})
    _assert_rejected(client, {"work_ids": ["../etc/passwd"]})
    _assert_rejected(client, {"work_ids": ["img{x}"]})


# ── temperatures：个数 + 区间 ─────────────────────────────────

def test_temperatures_range_and_count(client):
    _assert_ok(client, {"n_segments": 1, "temperatures": [0.0, 0.5, 2.0]})
    _assert_rejected(client, {"temperatures": [2.1]})     # 越界拒，不夹到 2.0
    _assert_rejected(client, {"temperatures": [-0.1]})
    _assert_rejected(client, {"temperatures": [0.5] * 9})


# ── 数值字段：区间上下限（不 clamp） ──────────────────────────

@pytest.mark.parametrize("field,lo_ok,hi_ok,lo_bad,hi_bad", [
    ("n_segments", 1, api._MAX_SEGMENTS, 0, api._MAX_SEGMENTS + 1),
    ("samples_per_pair", 1, api._MAX_SAMPLES_PER_PAIR, 0,
     api._MAX_SAMPLES_PER_PAIR + 1),
    ("adversarial_k", 0, api._MAX_ADVERSARIAL_K, -1,
     api._MAX_ADVERSARIAL_K + 1),
    ("concurrency", 1, api._MAX_CONCURRENCY, 0, api._MAX_CONCURRENCY + 1),
])
def test_numeric_bounds(client, field, lo_ok, hi_ok, lo_bad, hi_bad):
    """界内两端放行、越界两端必拒（422）——钉死"不静默 clamp"。"""
    _assert_ok(client, {"n_segments": 1, field: lo_ok})
    _assert_ok(client, {"n_segments": 1, field: hi_ok})
    _assert_rejected(client, {field: lo_bad})
    _assert_rejected(client, {field: hi_bad})


def test_concurrency_over_cap_no_side_effect(client):
    """审计动机用例：把并发拉满放大费用 → 必须整单拒、零建行。"""
    _assert_rejected(client, {"concurrency": 9999, "samples_per_pair": 999,
                              "n_segments": 10 ** 6,
                              "granularities": ["S", "M", "L"]})


# ── 既有安全口径不放宽（回归护栏） ────────────────────────────

def test_no_corpus_still_400(client):
    """语料域校验（experiments.create_experiment 的 ValueError→400）不受影响：
    合法入参但 work_ids 指向不存在的 Work → 400「没有语料」，仍零副作用。"""
    r = client.post("/experiments", json={"work_ids": ["WK-deadbeef0000"]})
    assert r.status_code == 400, r.text[:300]


# ── F1：批注 kind 强枚举（POST /review/{id}/verdict，独立审查 2026-09-23
#     BLOCK 严重项：066b754 声称收口但代码不存在，注入串可直通落库）──────

# 口径源 = app/static/index.html:1108 MARK_KINDS，两边必须同步。
LEGAL_KINDS = list(api._MARK_KINDS)
# 审查点名形态：[:40] 截断挡不住它（33 字符），只有枚举能挡。
INJECT_ANN = '"><img src=x onerror=alert(1)>'


_PROBE_EXP = "EXP-capsprobe0000"


def _mk_pending_review() -> str:
    """直插一条 pending 题（无任何呈现行）→ verdict 走无绑定路径，
    批注按原样校验落库，真打路由而不只测 pydantic 模型。
    FK 提示：db.py 开了 PRAGMA foreign_keys=ON，experiment_id 必须真行。"""
    with db.session() as s:
        if not s.get(Experiment, _PROBE_EXP):
            s.add(Experiment(id=_PROBE_EXP, name="caps-probe", status="created",
                             config={}, stats={}))
        r = ReviewItem(experiment_id=_PROBE_EXP,
                       subject_type="candidate", subject_id="CX-capsprobe0000",
                       status="pending", reasons=[])
        s.add(r)
        s.commit()
        return r.id


def _ann(**kw):
    base = {"side": "A", "start": 0, "end": 2, "text": "天擦",
            "kind": "用词", "note": ""}
    base.update(kw)
    return base


def _review_state(rid: str) -> tuple[str, dict | None]:
    with db.session() as s:
        r = s.get(ReviewItem, rid)
        return r.status, r.human_verdict


def test_annotation_kind_seven_legal_values_all_pass(client):
    """合法 7 值各一次通过，且按提交顺序原样落库。"""
    assert LEGAL_KINDS == ['用词', '解释过度', '情绪直给', '节奏', '逻辑', '意象', '其他'], \
        "服务端枚举与 index.html MARK_KINDS 口径漂移了"
    rid = _mk_pending_review()
    anns = [_ann(kind=k, side="A" if i % 2 == 0 else "B")
            for i, k in enumerate(LEGAL_KINDS)]
    r = client.post(f"/review/{rid}/verdict",
                    json={"winner": "tie", "reasons": [], "annotations": anns})
    assert r.status_code == 200, r.text[:400]
    assert r.json()["n_annotations"] == 7
    _, hv = _review_state(rid)
    assert [a["kind"] for a in hv["annotations"]] == LEGAL_KINDS


def test_annotation_kind_omitted_falls_to_qita(client):
    """缺省新口径：不带 kind → 落「其他」（旧默认 other 的直译，在口径内）。"""
    rid = _mk_pending_review()
    ann = {"side": "A", "start": 0, "end": 2, "text": "天擦"}
    r = client.post(f"/review/{rid}/verdict",
                    json={"winner": "tie", "reasons": [], "annotations": [ann]})
    assert r.status_code == 200, r.text[:400]
    _, hv = _review_state(rid)
    assert hv["annotations"][0]["kind"] == "其他"


def test_annotation_kind_illegal_422_zero_write(client):
    """注入串（含 < " ' 反引号）、空串、超长串、大小写变体、旧默认 other →
    一律 422，且**该题零写入**（status 仍 pending、human_verdict 仍 NULL）。"""
    bad = [INJECT_ANN, "<script>x</script>", 'a"b', "a'b", "a`b", "",
           "x" * 200, "other", "Other", "用词 ", "用词<img>"]
    for k in bad:
        rid = _mk_pending_review()
        r = client.post(f"/review/{rid}/verdict",
                        json={"winner": "tie", "reasons": [],
                              "annotations": [_ann(kind=k)]})
        assert r.status_code == 422, \
            f"kind={k!r} 应 422，实得 {r.status_code}: {r.text[:300]}"
        status, hv = _review_state(rid)
        assert status == "pending" and hv is None, f"拒绝后该题仍被写入: {k!r}"


def test_annotation_kind_mixed_batch_rejected_wholesale(client):
    """混合提交整单拒（不「丢坏留好」）：合法+非法同单 → 422 且零写入。"""
    rid = _mk_pending_review()
    r = client.post(f"/review/{rid}/verdict",
                    json={"winner": "tie", "reasons": [],
                          "annotations": [_ann(kind="用词"),
                                          _ann(kind=INJECT_ANN, side="B")]})
    assert r.status_code == 422, r.text[:400]
    status, hv = _review_state(rid)
    assert status == "pending" and hv is None


# ── F2：GET /segments 翻页上界（独立审查 2026-09-23 BLOCK 严重项：
#     limit/offset 无上界，配合 [:80] 预览可对整库正文无界翻页外流）──────

def test_segments_limit_cap_boundary(client):
    """limit 上界 200：界内放行、越界 422（**不 clamp**，与 ExperimentIn 同纪律）；
    0/负数同拒（旧实现可传负数/超大数，一并钉死）。"""
    assert api._MAX_SEGMENT_LIMIT == 200
    assert client.get("/segments?limit=200").status_code == 200
    assert client.get("/segments?limit=201").status_code == 422
    assert client.get("/segments?limit=1000000").status_code == 422
    assert client.get("/segments?limit=0").status_code == 422
    assert client.get("/segments?limit=-5").status_code == 422
    assert client.get("/segments?offset=-1").status_code == 422


def test_segments_offset_scan_gate(client):
    """总量闸 offset+limit ≤ 5000：恰好 5000 放行（offset=上界 4999@limit=1）、
    5001 拒；天文数字 offset 必拒。空结果页仍是 200（闸只卡请求形状）。"""
    assert api._MAX_SEGMENT_SCAN == 5000
    assert client.get("/segments?offset=4999&limit=1").status_code == 200
    assert client.get("/segments?offset=5000&limit=1").status_code == 422
    assert client.get("/segments?offset=4900&limit=100").status_code == 200
    assert client.get("/segments?offset=4901&limit=100").status_code == 422
    assert client.get(f"/segments?offset={10**12}&limit=20").status_code == 422


def test_segments_preview_80_cap_unchanged(client):
    """[:80] 预览硬上限不变式（审计 P1 注释语义不放宽）：单条响应文本
    ≤ 80 + 1 个省略号；>80 字的段必须恰好截到 81 并以 … 结尾。"""
    long_text = "雾" * 300 + "。"  # 单句 301 字（切分器 max_chars=300 即到即断）
    with db.session() as s:
        w = corpus.add_work(s, title="预览上限测试书", text=long_text,
                            source="test:caps-seg")
        s.commit()
        wid = w.id
    rows = client.get(f"/segments?work_id={wid}").json()
    assert len(rows) == 1 and rows[0]["chars"] > 80
    for item in client.get("/segments?limit=200").json():
        assert len(item["text"]) <= 81, f"预览超 80+… 上限: {item['id']}"
    assert rows[0]["text"] == "雾" * 80 + "…"
    assert len(rows[0]["text"]) == 81
