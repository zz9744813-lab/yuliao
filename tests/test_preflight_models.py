"""批量防呆三件套回归（T-GUARD 2026-09-19）。

起因见 `data/_dbg/DIAG_rescore_387.md`：模型名写成池里不存在的
`deepseek/deepseek-v4.1` → 网关逐个 503 → 387 段白跑 7.4 分钟、ok=0。

锁定的不变量：
1. `check_model`：池内命中放行；`vendor/model` 与裸名混写都算命中（配置里在用两种写法）。
2. 池外给出 difflib 最近候选，且**排序确定**（同分按名字，不随池顺序漂移）。
3. 本机桥接通道（agy/ qoder/ wb/ zcode/）不发网关请求就放行；前缀表与
   `app.gateway.SERIAL_MODEL_PREFIXES` 同源。
4. 拿不到池 = 未知 = 抛 `GatewayUnreachable`（宁可不跑，绝不静默放行）；
   CLI：池内 0 / 池外 2 / 不可达 3。
5. API key 只进请求头，任何输出与异常文案里都不许出现。
6. `source_check.run()` 开跑前预检：名字不可用就一个段都不碰；mock 模式不联网。
7. 失败率熔断：满 20 次调用后 failed/total **超过** 0.3 才中止；未达 20 次不判。
8. 入口闸门统一（`preflight_block`/`require_models`）与"失败日报自带原因"：
   熔断行、完成行、`controlled_corruption` 的 DB 侧首条异常都必须能自己说出挂的原因
   ——2026-09-20 第五批扩产就是因为只报计数不报原因，把 100% 的 503 误判成了"池子耗尽"。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import preflight_models as pf  # noqa: E402
import source_check as sc  # noqa: E402
from app import config, gateway  # noqa: E402
from app import db  # noqa: E402
from app.models import Segment, Work  # noqa: E402

# 与中转池实测形状一致：`vendor/model` 与裸名混写
POOL = ["deepseek-flash", "deepseek-v4.1-flash", "moonshotai/kimi-k3",
        "z-ai/glm-5.3", "agnes-3.0-flash", "glm-5.3-flash"]
FAKE_KEY = "sk-NEVER-PRINT-9e2f"
TEXT = "他推门进来，屋里没人，桌上的茶还温着，窗外雪落得极轻。"   # 不含 KNOWN_TYPOS


@pytest.fixture(autouse=True)
def _fresh_stat():
    """_stat 是模块全局（跨趟累计），每例前后归零，避免互相污染。"""
    sc._stat.update(ok=0, failed=0, skip=0, bad=0, first_error="")
    yield
    sc._stat.update(ok=0, failed=0, skip=0, bad=0, first_error="")


@pytest.fixture
def gw(monkeypatch):
    """假网关：不联网，但走真实的 header/url 拼装与解析路径。返回可变状态字典。"""
    state = {"mode": "ok", "url": None, "headers": None}

    class Resp:
        def __init__(self, code, payload):
            self.status_code, self._payload = code, payload

        def json(self):
            return self._payload

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            state["url"], state["headers"] = url, dict(headers or {})
            if state["mode"] == "raise":
                raise httpx.ConnectError("connection refused")
            if state["mode"] == "http503":
                return Resp(503, {"error": "upstream unavailable"})
            if state["mode"] == "empty":
                return Resp(200, {"data": []})
            return Resp(200, {"data": [{"id": m} for m in POOL]})

    monkeypatch.setattr(pf, "httpx", SimpleNamespace(Client=Client,
                                                     HTTPError=httpx.HTTPError))
    monkeypatch.setattr(pf.config, "GATEWAY_BASE_URL", "http://gw.test:3000/v1")
    monkeypatch.setattr(pf.config, "GATEWAY_API_KEY", FAKE_KEY)
    return state


def _mk_segments(n: int, tag: str) -> list[str]:
    db.init_db()
    ids = []
    with db.session() as s:
        w = Work(title=f"斗罗大陆（唐家三少）-guard-{tag}", source="test:guard")
        s.add(w)
        s.flush()
        for i in range(n):
            seg = Segment(work_id=w.id, ordinal=i, text=TEXT + f"（{i}）",
                          n_sentences=1, n_chars=len(TEXT))
            s.add(seg)
            s.flush()
            ids.append(seg.id)
        s.commit()
    return ids


def _fake_llm(fail_first: int, calls: list):
    """前 fail_first 次调用当作网关 503 失败，其后返回正常结论（并发=1 时确定性）。"""
    def fake(text, exp_id=None):
        calls.append(text)
        if len(calls) <= fail_first:
            raise RuntimeError("HTTP 503: model_not_found")
        return {"src_ok": True, "defects": [], "severity": "low"}
    return fake


# ── 1/2/5：池内命中、池外候选、key 不外泄 ───────────────────────

def test_pool_hit_ok(gw):
    c = pf.check_model("moonshotai/kimi-k3")
    assert c.ok and c.pool_size == len(POOL) and not c.candidates
    assert gw["url"].endswith("/models")
    assert gw["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"     # key 只进 header
    assert FAKE_KEY not in pf.describe(c)


@pytest.mark.parametrize("name", ["z-ai/glm-5.3",             # 与池内完全一致
                                  f"deepseek/{config.DEFAULT_LLM_MODEL}",  # 池内裸名多加了 vendor 前缀
                                  "kimi-k3"])                 # 少写 vendor 前缀
def test_vendor_prefix_normalization_hits(gw, name):
    assert pf.check_model(name).ok, f"{name} 该按归一化命中，否则会误杀在用的默认名"


def test_pool_miss_gives_nearest_candidates(gw):
    """事故本故：`deepseek/deepseek-v4.1`（少了 -flash）必须被拦下并给出正确名字。"""
    c = pf.check_model("deepseek/deepseek-v4.1")
    assert not c.ok and c.pool_size == len(POOL)
    assert c.candidates[0] == "deepseek-v4.1-flash", c.candidates
    assert len(c.candidates) == pf.N_CANDIDATES
    line = pf.describe(c)
    assert "deepseek-v4.1-flash" in line and FAKE_KEY not in line


def test_candidates_are_deterministic():
    """同分时按名字排：候选顺序不许随池顺序/字典实现漂移（复现要一致）。"""
    a = pf.nearest("glm-5.3", ["z-ai/glm-5.3", "glm-5.3-flash", "agnes-3.0-flash",
                               "moonshotai/kimi-k3"])
    b = pf.nearest("glm-5.3", list(reversed(
        ["z-ai/glm-5.3", "glm-5.3-flash", "agnes-3.0-flash", "moonshotai/kimi-k3"])))
    assert a == b


def test_empty_model_name_is_not_ok(gw):
    assert not pf.check_model("   ").ok


# ── 3：本机桥接通道 ────────────────────────────────────────────

def test_local_bridge_prefixes_match_gateway():
    """前缀表与 gateway 的串行通道表同源，漂移会让预检误杀/漏放。"""
    assert pf.LOCAL_BRIDGE_PREFIXES == gateway.SERIAL_MODEL_PREFIXES


@pytest.mark.parametrize("name", ["zcode/glm-5.3-flash", "agy/gemini-3.8-flash-high",
                                  "qoder/Qwen3.8-Flash", "wb/hy4-preview-f"])
def test_local_bridge_skips_network(gw, name):
    """夜间把 LG_SOURCE_MODEL 指向 zcode/… 是合法覆盖点：不该被池校验拦，也不该联网。"""
    assert pf.check_model(name).ok
    assert gw["url"] is None, "本机桥接通道不该发 /models 请求"


# ── 4：拿不到池 = 不放行 ───────────────────────────────────────

@pytest.mark.parametrize("mode", ["raise", "http503", "empty"])
def test_unreachable_modes_raise(gw, mode):
    gw["mode"] = mode
    with pytest.raises(pf.GatewayUnreachable):
        pf.check_model("moonshotai/kimi-k3")


def test_missing_config_raises(monkeypatch):
    monkeypatch.setattr(pf.config, "GATEWAY_API_KEY", "")
    with pytest.raises(pf.GatewayUnreachable):
        pf.check_model("moonshotai/kimi-k3")


# ── 4：CLI 退出码 ──────────────────────────────────────────────

def test_cli_exit_codes(gw, capsys):
    assert pf.main(["moonshotai/kimi-k3"]) == pf.EXIT_OK == 0
    assert pf.main(["deepseek/deepseek-v4.1"]) == pf.EXIT_NOT_IN_POOL == 2
    out = capsys.readouterr().out
    assert "deepseek-v4.1-flash" in out and FAKE_KEY not in out
    gw["mode"] = "raise"
    assert pf.main(["moonshotai/kimi-k3"]) == pf.EXIT_UNREACHABLE == 3
    assert pf.main([]) == 1                       # 用法错误


def test_cli_multi_models_reports_all_then_fails(gw, capsys):
    """多个名字：好的照常说 OK，坏的照样点名，最终退出码取坏的那个。"""
    assert pf.main(["moonshotai/kimi-k3", "deepseek/deepseek-v4.1"]) == pf.EXIT_NOT_IN_POOL
    out = capsys.readouterr().out
    assert out.count("OK：") == 1 and "不可用：" in out


# ── 6：source_check 开跑前预检 ─────────────────────────────────

def test_run_fails_fast_when_model_name_not_in_pool(monkeypatch, capsys):
    ids = _mk_segments(3, "pre")
    monkeypatch.setattr(sc.config, "LLM_MODE", "real")
    monkeypatch.setattr(pf, "check_model", lambda name: pf.ModelCheck(
        name, False, reason="网关模型池里没有", candidates=("deepseek-v4.1-flash",)))
    calls: list = []
    monkeypatch.setattr(sc, "check_one", lambda *a, **k: calls.append(a))
    res = sc.run(ids=ids, conc=1)
    assert res["aborted"] is True
    assert calls == [], "预检没过就不该发一次 LLM 调用"
    out = capsys.readouterr().out
    assert "预检失败" in out and "deepseek-v4.1-flash" in out
    with db.session() as s:
        assert all(s.get(Segment, i).integrity is None for i in ids)
    assert {"ok", "failed", "skip", "bad"} <= set(res)   # 既有字段名不破


def test_run_aborts_when_pool_unreachable(monkeypatch, capsys):
    def boom(name):
        raise pf.GatewayUnreachable("连接被拒绝")
    monkeypatch.setattr(sc.config, "LLM_MODE", "real")
    monkeypatch.setattr(pf, "check_model", boom)
    assert sc.run(ids=_mk_segments(2, "unreach"), conc=1)["aborted"] is True
    assert "拿不到网关模型池" in capsys.readouterr().out


def test_preflight_checks_configured_model_name(monkeypatch):
    seen: list = []

    def fake(name):
        seen.append(name)
        return pf.ModelCheck(name, True)

    monkeypatch.setattr(sc.config, "LLM_MODE", "real")
    monkeypatch.setattr(sc, "MODEL", "deepseek-v4.1-flash")
    monkeypatch.setattr(pf, "check_model", fake)
    assert sc.run(ids=[], conc=1)["aborted"] is False
    assert seen == ["deepseek-v4.1-flash"]


def test_mock_mode_never_touches_preflight(monkeypatch):
    """测试/dry-run 走 mock：预检必须整个跳过（否则单测会联网、且池校验无意义）。"""
    def never(name):
        raise AssertionError("mock 模式不该做池校验")
    monkeypatch.setattr(pf, "check_model", never)
    monkeypatch.setattr(sc, "check_one",
                        lambda text, exp_id=None: {"src_ok": True, "defects": [],
                                                   "severity": "low"})
    res = sc.run(ids=_mk_segments(2, "mock"), conc=1)
    assert res["aborted"] is False and res["ok"] == 2


# ── 7：失败率熔断 ──────────────────────────────────────────────

def test_breaker_trips_and_stops_early(monkeypatch, capsys):
    ids = _mk_segments(sc.BREAKER_MIN_CALLS + 5, "brk")
    calls: list = []
    monkeypatch.setattr(sc, "check_one", _fake_llm(99, calls))
    res = sc.run(ids=ids, conc=1)
    assert res["aborted"] is True
    assert res["failed"] == sc.BREAKER_MIN_CALLS < len(ids), "满 20 次即停，别把 25 段跑完"
    assert "熔断：失败率 100.0%，已中止" in capsys.readouterr().out
    with db.session() as s:
        assert all(s.get(Segment, i).integrity is None for i in ids)


def test_breaker_not_tripped_by_low_failure_rate(monkeypatch, capsys):
    ids = _mk_segments(sc.BREAKER_MIN_CALLS + 5, "low")
    calls: list = []
    monkeypatch.setattr(sc, "check_one", _fake_llm(2, calls))
    res = sc.run(ids=ids, conc=1)
    assert res["aborted"] is False
    assert (res["failed"], res["ok"]) == (2, sc.BREAKER_MIN_CALLS + 3)
    assert "熔断" not in capsys.readouterr().out


def test_breaker_needs_min_calls_and_is_strict(monkeypatch):
    """前 6 次全失败：第 20 次调用时失败率恰为 0.3（不**超**线）→ 不许熔断。"""
    n = sc.BREAKER_MIN_CALLS + sc.BREAKER_MIN_CALLS // 4     # 25
    ids = _mk_segments(n, "edge")
    calls: list = []
    monkeypatch.setattr(sc, "check_one", _fake_llm(6, calls))
    res = sc.run(ids=ids, conc=1)
    assert res["aborted"] is False, "6/20 = 0.30 未超阈值；且未达 20 次前根本不判"
    assert (res["failed"], res["ok"]) == (6, n - 6)
    assert len(calls) == n


def test_breaker_threshold_is_module_constant():
    """阈值可调度：改常量即改口径，不许埋进函数里的魔数。"""
    assert sc.BREAKER_MIN_CALLS == 20
    assert sc.BREAKER_FAIL_RATE == 0.3


# ── 8：入口闸门 + 失败日报必须自带原因 ─────────────────────────

def test_preflight_block_returns_none_when_every_name_is_in_pool(gw, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "real")
    assert pf.preflight_block(["moonshotai/kimi-k3",
                               f"deepseek/{config.DEFAULT_LLM_MODEL}"]) is None


def test_preflight_block_lists_every_bad_name_with_the_right_one(gw, monkeypatch):
    """一条命令里多个模型：不许只报第一个就停（否则修一个、再撞一个）。"""
    monkeypatch.setattr(config, "LLM_MODE", "real")
    msg = pf.preflight_block(["deepseek/deepseek-v4.1", "nope-not-a-model"],
                             source="scale_corpus")
    assert msg.startswith("[scale_corpus] ")
    for bad in ("deepseek/deepseek-v4.1", "nope-not-a-model"):
        assert bad in msg, f"池外名单里缺 `{bad}`：{msg}"
    assert "deepseek-v4.1-flash" in msg, "必须把池内正确的名字递到眼前"


def test_preflight_block_skips_network_in_mock_mode(monkeypatch):
    def never(name):
        raise AssertionError("mock 模式不发调用，也就无从校验")
    monkeypatch.setattr(pf, "check_model", never)
    monkeypatch.setattr(pf.config, "LLM_MODE", "mock")
    assert pf.preflight_block(["deepseek/deepseek-v4.1"]) is None


def test_require_models_exits_2_before_any_call(gw, monkeypatch, capsys):
    monkeypatch.setattr(config, "LLM_MODE", "real")
    with pytest.raises(SystemExit) as e:
        pf.require_models(["deepseek/deepseek-v4.1"], source="controlled_corruption")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "[预检失败] 未开跑" in err and "deepseek-v4.1-flash" in err


def test_redact_flattens_truncates_and_hides_key(monkeypatch):
    """错误原文要能整行贴进汇报，但网关 key 绝不能跟着出来。"""
    monkeypatch.setattr(pf.config, "GATEWAY_API_KEY", FAKE_KEY)
    raw = f'HTTP 503 {{"error": "No available channel for model x  key={FAKE_KEY}"}}\n' + "长" * 400
    line = pf.redact(raw, limit=120)
    assert "\n" not in line and FAKE_KEY not in line and "key=***" in line
    assert len(line) == 121, "截断处必须留省略号，且不许超限"


def test_scale_run_aborts_without_picking_a_single_segment(monkeypatch):
    """第五批的形状：挑 300 → 源校勘通过 0 → 0 帧，看着像池子耗尽。
    闸门必须让它一个段都不碰就退出，并把原因写进返回值（供上层继续上抛）。"""
    import scale_corpus as SCP
    monkeypatch.setattr(pf.config, "LLM_MODE", "real")
    monkeypatch.setattr(pf, "check_model", lambda name: pf.ModelCheck(
        name, False, reason="网关模型池里没有", candidates=("deepseek-v4.1-flash",)))
    calls: list = []
    monkeypatch.setattr(SCP, "pick", lambda *a, **k: calls.append(a) or [])
    res = SCP.run(300, 1, 8, "EXP-GATE")
    assert res["aborted"] is True and calls == [], "预检没过就不该挑段、更不该抽帧"
    assert "deepseek-v4.1-flash" in res["reason"]


def test_breaker_line_carries_first_error_text(monkeypatch, capsys):
    ids = _mk_segments(sc.BREAKER_MIN_CALLS + 5, "err1")
    monkeypatch.setattr(sc, "check_one", _fake_llm(99, []))
    res = sc.run(ids=ids, conc=1)
    assert res["aborted"] is True
    assert "RuntimeError: HTTP 503: model_not_found" in res["first_error"]
    out = capsys.readouterr().out
    assert "熔断：失败率 100.0%，已中止" in out
    assert "model_not_found" in out, "熔断行必须自己说出原因，不许留给人工翻 DB"


def test_done_line_carries_first_error_even_when_breaker_stays_quiet(monkeypatch, capsys):
    """只挂 1 条远够不上熔断，但日报仍然要带原文——死一个模型名时失败率是慢慢涨上去的。"""
    ids = _mk_segments(sc.BREAKER_MIN_CALLS + 5, "err2")
    monkeypatch.setattr(sc, "check_one", _fake_llm(1, []))
    res = sc.run(ids=ids, conc=1)
    assert res["aborted"] is False and res["failed"] == 1
    out = capsys.readouterr().out
    assert "首条错误原文：RuntimeError: HTTP 503" in out


def test_controlled_corruption_gate_only_asks_models_that_branch_uses():
    """预检只问本分支真正会调的模型：把生成模型算进 --recheck，
    一个挂掉的生成 id 就会挡住"救回已生成数据"的通路。"""
    import controlled_corruption as CC
    base = dict(report=False, build_batch="", split_benchmark=-1, judge=False,
                recheck=False, reverify=False, gen_model="g", verify_model="v",
                judge_models="j1, j2")
    mk = lambda **kw: SimpleNamespace(**{**base, **kw})
    assert CC._llm_models(mk()) == ["g", "v"]
    assert CC._llm_models(mk(recheck=True)) == ["v"]
    assert CC._llm_models(mk(reverify=True)) == ["v"]
    assert CC._llm_models(mk(judge=True)) == ["j1", "j2"]
    for quiet in ({"report": True}, {"split_benchmark": 5}, {"build_batch": "corr16"}):
        assert CC._llm_models(mk(**quiet)) == [], f"确定性分支不该为预检碰网关：{quiet}"


def test_controlled_corruption_first_error_reads_persisted_exception():
    """劣化批量把异常原文落在 controlled_corruptions.error 里；完成行要能把首条捞出来。
    同时锁定"只取异常前缀"：rejected_* 存的是语义拒收理由，不是故障，不许混进来。"""
    import controlled_corruption as CC
    from app.models import ControlledCorruption, Experiment
    db.init_db()
    exp, seg = "EXP-GATE-CC", _mk_segments(2, "ccerr")[0]
    with db.session() as s:
        if s.get(Experiment, exp) is None:
            s.add(Experiment(id=exp, name="t-gate", status="created", config={}, stats={}))
            s.flush()      # 外键：实验必须先落库，否则整批 CC 行一起撞 FK
        s.add(ControlledCorruption(
            experiment_id=exp, segment_id=seg, corruption_type="EXPLICITIZE",
            generator_model="g", verify_model="v", text="", status="failed",
            error="gen: HTTP 503 Service Unavailable\n No available channel for model x"))
        s.add(ControlledCorruption(
            experiment_id=exp, segment_id=seg, corruption_type="RHYTHM_FLATTEN",
            generator_model="g", verify_model="v", text="略", status="rejected_length",
            error="长度比 1.9 超线"))
        s.commit()
    line = CC._first_error(exp)
    assert "\n" not in line and "503" in line
    assert "长度比" not in line, "把语义拒收理由当故障报出来，等于又一次误导归因"
    assert CC._first_error("EXP-GATE-NONE") == ""
