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
    sc._stat.update(ok=0, failed=0, skip=0, bad=0)
    yield
    sc._stat.update(ok=0, failed=0, skip=0, bad=0)


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
