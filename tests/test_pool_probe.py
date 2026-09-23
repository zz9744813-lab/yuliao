"""模型池存活探针回归（scripts/pool_probe.py，会审 89f779e R8 待办落地）。

钉住的事：
1. 分类口径判定表：ok（status="ok" 且 error 空且未超时）/ slow（>slow_ms）/
   dead（调用异常 **或软失败**——status != "ok" / error 非空，探针不炸）；
2. 默认档零调用（preflight_block 池名单口径，mock 模式不发网络）；
3. 双闸：--live 无 POOL_PROBE_ALLOW_LIVE=1 拒；有闸但 LLM_MODE≠real 拒；
4. 预检闸真 exit 2（excinfo.value.code == 2，非 SystemExit(str) 的退 1）；
5. 测试不真跑 live 开放路径（真实调用纪律）。
"""
from __future__ import annotations

import importlib.util as _u
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location("pp", ROOT / "scripts" / "pool_probe.py")
PP = _u.module_from_spec(_spec); _spec.loader.exec_module(PP)


def test_probe_classifies_ok_slow_dead(monkeypatch):
    class _R:
        latency_ms = 0
        tokens_in = 1
        tokens_out = 1
        status = "ok"
        error = None

    def fake_chat(**kw):
        m = kw["model"]
        if m == "dead-model":
            raise PP.gateway.LLMError("HTTP 410 已下架")
        r = _R()
        r.latency_ms = 100 if m == "fast-model" else 99_000
        return r
    monkeypatch.setattr(PP.gateway, "chat", fake_chat)
    rep = PP.run_probe(["fast-model", "slow-model", "dead-model"], slow_ms=30_000)
    assert rep["verdicts"] == {"fast-model": "ok", "slow-model": "slow",
                               "dead-model": "dead"}
    assert rep["rows"][2]["error"].startswith("LLMError")
    ok_row = rep["rows"][0]
    assert ok_row["ok"] is True and ok_row["latency_ms"] == 100


def test_main_default_preflight_only_zero_calls(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pp", "--models", "m1, m2,"])
    PP.main()                     # mock 模式：preflight_block 放行、零网络
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "preflight_only" and out["probed"] == 0
    assert out["models"] == ["m1", "m2"] and out["blocked"] is None


def test_main_live_double_gate(monkeypatch):
    monkeypatch.delenv("POOL_PROBE_ALLOW_LIVE", raising=False)
    monkeypatch.setattr(sys, "argv", ["pp", "--models", "m1", "--live"])
    with pytest.raises(SystemExit, match="POOL_PROBE_ALLOW_LIVE"):
        PP.main()
    monkeypatch.setenv("POOL_PROBE_ALLOW_LIVE", "1")
    monkeypatch.setattr(PP, "LLM_MODE", "mock")   # conftest 已 mock，显式钉前提
    with pytest.raises(SystemExit, match="LG_LLM_MODE=real"):
        PP.main()


def test_main_empty_models_refused(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pp", "--models", ","])
    with pytest.raises(SystemExit, match="为空"):
        PP.main()


def test_main_default_exits_nonzero_when_blocked(monkeypatch, capsys):
    """预检闸语义（9e02916 会审建议项 + 794521f 整改）：默认档池外名非空
    → **真** exit 2（SystemExit(str) 实际退 1，调用方按 2 判会判错），
    拦截原因打印到 stderr——调用方不必解析 stdout 才能拦死名。"""
    import preflight_models as PF
    monkeypatch.setattr(PF, "preflight_block",
                        lambda models, source="": "m1 不在池内（最接近：mx）")
    monkeypatch.setattr(sys, "argv", ["pp", "--models", "m1"])
    with pytest.raises(SystemExit) as excinfo:
        PP.main()
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["mode"] == "preflight_only" and out["blocked"]
    assert "预检失败" in captured.err and "m1 不在池内" in captured.err


def test_soft_fail_response_verdicts_dead(monkeypatch):
    """软失败=dead 契约（会审 794521f）：gateway.chat 不抛异常但返回
    status != "ok" 或 error 非空的响应 ⇒ 该模型不得判 ok，判 dead。"""
    class _Resp:
        def __init__(self, status, error):
            self.latency_ms = 5
            self.tokens_in = 0
            self.tokens_out = 0
            self.status = status
            self.error = error

    responses = {
        "err-status": _Resp("error", "upstream 503"),
        "err-field": _Resp("ok", "silent failure payload"),
    }
    monkeypatch.setattr(PP.gateway, "chat",
                        lambda **kw: responses[kw["model"]])
    rep = PP.run_probe(["err-status", "err-field"], slow_ms=30_000)
    assert rep["verdicts"] == {"err-status": "dead", "err-field": "dead"}
    for row in rep["rows"]:
        assert row["ok"] is False


def test_normal_and_slow_responses_keep_verdicts(monkeypatch):
    """判定表非软失败侧不回归：status="ok" 且 error 空——快响应 ok、
    超阈值响应 slow（既有阈值口径不变）。"""
    class _Resp:
        def __init__(self, latency):
            self.latency_ms = latency
            self.tokens_in = 1
            self.tokens_out = 1
            self.status = "ok"
            self.error = ""

    monkeypatch.setattr(
        PP.gateway, "chat",
        lambda **kw: _Resp(1_000 if kw["model"] == "fast-m" else 60_000))
    rep = PP.run_probe(["fast-m", "slow-m"], slow_ms=30_000)
    assert rep["verdicts"] == {"fast-m": "ok", "slow-m": "slow"}
