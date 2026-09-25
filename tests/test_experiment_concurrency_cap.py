"""实验并发上限 + 串行纪律的执行侧收口回归（2026-09-25）。

审计事实：`_MAX_CONCURRENCY = 16` 原先**只**挂在 HTTP 入参 `ExperimentIn.concurrency`
上，执行侧 `app/experiments.py::_pool_map` 原样 `max(1, int(config["concurrency"]))`
开线程池——旁路脚本直接写 config（`scripts/scale_corpus.py:121`、
`scripts/run_calibration.py:52` 的 `--concurrency` 无上界校验）就能绕过闸；
且重建/判定 stage 完全不看「本机 CLI 单账号必须串行」这条纪律
（gateway.py:133-143），把 `qoder/Qwen3.8-Flash` 塞进 `recon_models` + `concurrency=8`
就是 8 线程打同一个账号。

本文件钉死四条不变量：
1. **单一真源**：`limits.MAX_CONCURRENCY == api._MAX_CONCURRENCY == 16`，
   且 api.py 里不许再出现自己的一份字面量赋值；
2. **执行侧有上限**：普通模型 + `concurrency=64` → 绝不出现 64 个 worker，
   按上限截断且返回值显式记 `concurrency_clamped_from=64`（口径：执行侧截断+记账，
   HTTP 入参侧仍是越界 422 直拒——见 docs/实验并发上限_20260925.md）；
3. **串行纪律落到执行侧**：模型里命中 `gateway.is_serial_model()` → worker 恒为 1，
   显式记 `serial_forced=True` 与命中模型名；判定必须复用 gateway 的函数，
   不许在 experiments 里自己比字符串前缀；
4. **正常路径不回退**：普通模型 + `concurrency=4`（及缺省值）行为与改前一致。

纯离线：只调用 `_pool_plan`/`_pool_map` 本身（`fn` 是本地 lambda），
全程不发任何模型请求、不连库、不起服务。
"""
import ast
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import api, experiments, gateway, limits  # noqa: E402

PLAIN_MODELS = ["moonshotai/kimi-k3", "z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash"]
SERIAL_MODELS = ["agy/gemini-3.8-flash-high", "qoder/Qwen3.8-Flash",
                 "wb/hy4-preview-f", "zcode/some-model"]


def _exp(concurrency=None, **extra) -> SimpleNamespace:
    """最小实验替身：_pool_plan/_pool_map 只读 config 与 id。"""
    cfg = {"recon_models": list(PLAIN_MODELS), "judge_models": list(PLAIN_MODELS[:1]),
           **extra}
    if concurrency is not None:
        cfg["concurrency"] = concurrency
    return SimpleNamespace(id="EXP-cap0000000", config=cfg)


class _SpyExecutor(ThreadPoolExecutor):
    """记录真实传给线程池的 max_workers（不改行为：照常执行）。"""
    seen: list[int | None] = []

    def __init__(self, *args, **kwargs):
        _SpyExecutor.seen.append(kwargs.get("max_workers"))
        super().__init__(*args, **kwargs)


@pytest.fixture()
def spy(monkeypatch):
    _SpyExecutor.seen = []
    monkeypatch.setattr(experiments, "ThreadPoolExecutor", _SpyExecutor)
    return _SpyExecutor


# ── 1. 单一真源 ───────────────────────────────────────────────

def test_max_concurrency_single_source():
    assert limits.MAX_CONCURRENCY == 16
    assert api._MAX_CONCURRENCY == limits.MAX_CONCURRENCY, "两边各写一份就会漂移"
    # api.py 只能"从 limits 导入并改名"，不许再自己赋字面量
    src = (ROOT / "app" / "api.py").read_text(encoding="utf-8")
    assert re.search(r"^_MAX_CONCURRENCY\s*=\s*\d", src, re.M) is None, \
        "api.py 里又出现了一份 _MAX_CONCURRENCY 字面量赋值"
    assert "from .limits import MAX_CONCURRENCY as _MAX_CONCURRENCY" in src


def test_http_entry_still_rejects_instead_of_clamping():
    """入参侧口径不变：越界 422（不 clamp），界内上界放行。"""
    api.ExperimentIn(concurrency=limits.MAX_CONCURRENCY)
    with pytest.raises(ValidationError):
        api.ExperimentIn(concurrency=limits.MAX_CONCURRENCY + 1)
    with pytest.raises(ValidationError):
        api.ExperimentIn(concurrency=64)
    with pytest.raises(ValidationError):
        api.ExperimentIn(concurrency=0)


# ── 2. 执行侧上限（截断 + 显式记账，不静默）───────────────────

def test_plan_clamps_over_cap_plain_models():
    plan = experiments._pool_plan(_exp(64), PLAIN_MODELS)
    assert plan["workers"] == limits.MAX_CONCURRENCY == 16
    assert plan["concurrency_clamped_from"] == 64, "越界必须显式记账，不许静默降级"
    assert plan["serial_forced"] is False


def test_plan_cap_boundary_matches_http_gate():
    """两边同一口径：入参侧允许的上界，执行侧不该判成越界。"""
    ok = experiments._pool_plan(_exp(api._MAX_CONCURRENCY), PLAIN_MODELS)
    assert ok["workers"] == api._MAX_CONCURRENCY
    assert ok["concurrency_clamped_from"] is None


def test_pool_map_never_opens_64_workers(spy):
    out = experiments._pool_map(_exp(64), [1, 2, 3], lambda x: {"ok": True},
                                 desc="probe", models=PLAIN_MODELS)
    assert spy.seen == [16], f"传给 ThreadPoolExecutor 的 max_workers 应为 16，实得 {spy.seen}"
    assert 64 not in spy.seen
    assert len(out) == 3 and all(r["ok"] for r in out)
    assert out.plan["concurrency_clamped_from"] == 64, "返回结构里要能查到截断来源"


def test_pool_map_extreme_concurrency_still_capped(spy):
    experiments._pool_map(_exp(10_000), [], lambda x: None, models=PLAIN_MODELS)
    assert spy.seen == [16]


# ── 3. 串行纪律：命中 is_serial_model → 恒为 1 ────────────────

@pytest.mark.parametrize("m", SERIAL_MODELS)
def test_each_serial_prefix_forces_one_worker(m):
    assert gateway.is_serial_model(m)
    plan = experiments._pool_plan(_exp(8), [m])
    assert plan["workers"] == 1, f"{m} 是本机 CLI 单账号，必须串行"
    assert plan["serial_forced"] is True
    assert plan["serial_models"] == [m], "要能查出是哪个模型触发了串行"


def test_mixed_pool_is_conservative_and_records_all_hits():
    plan = experiments._pool_plan(
        _exp(8), ["moonshotai/kimi-k3", "qoder/Qwen3.8-Flash",
                  "agy/gemini-3.8-flash-high"])
    assert plan["workers"] == 1
    assert plan["serial_models"] == ["qoder/Qwen3.8-Flash", "agy/gemini-3.8-flash-high"]


def test_serial_beats_over_cap_but_both_recorded():
    """越界 + 串行同时发生：workers 取更保守的 1，两个标记都保留（都不静默）。"""
    plan = experiments._pool_plan(_exp(64), ["qoder/Qwen3.8-Flash"])
    assert plan["workers"] == 1
    assert plan["serial_forced"] is True
    assert plan["concurrency_clamped_from"] == 64


def test_pool_map_forces_serial_workers_at_runtime(spy):
    experiments._pool_map(_exp(8), [1, 2, 3], lambda x: {"ok": True},
                          models=["qoder/Qwen3.8-Flash"])
    assert spy.seen == [1]


def test_serial_check_reuses_gateway_not_local_prefix_logic(monkeypatch):
    """口径只有一处：判定走 gateway.is_serial_model（复用），不许自己比前缀。"""
    assert experiments.is_serial_model is gateway.is_serial_model
    assert not hasattr(experiments, "SERIAL_MODEL_PREFIXES"), "前缀表不许在 experiments 里复刻"

    # 行为侧证据：替换掉复用的函数，_pool_plan 的判定跟着变
    #（若是自己写死 "qoder/" 之类的字符串比较，注入不生效）。
    monkeypatch.setattr(experiments, "is_serial_model",
                        lambda m: m == "weird/made-up-model")
    forced = experiments._pool_plan(_exp(8), ["weird/made-up-model"])
    assert forced["workers"] == 1 and forced["serial_forced"] is True
    normal = experiments._pool_plan(_exp(8), ["qoder/Qwen3.8-Flash"])
    assert normal["workers"] == 8 and normal["serial_forced"] is False


def test_decision_is_logged(capsys):
    experiments._pool_map(_exp(8), [], lambda x: None, desc="reconstruct",
                          models=["qoder/Qwen3.8-Flash"])
    log = capsys.readouterr().out
    assert "serial_forced=True" in log and "qoder/Qwen3.8-Flash" in log

    experiments._pool_map(_exp(64), [], lambda x: None, desc="judges",
                          models=PLAIN_MODELS)
    log = capsys.readouterr().out
    assert "concurrency_clamped_from=64" in log and "workers=16" in log


# ── 4. 正常路径不回退 ────────────────────────────────────────

def test_normal_path_unchanged():
    """既有实验全部 concurrency=4 + 普通模型：worker 数、标记都与改前一致。"""
    plan = experiments._pool_plan(_exp(4), PLAIN_MODELS)
    assert plan["workers"] == 4
    assert plan["serial_forced"] is False
    assert plan["concurrency_clamped_from"] is None
    # 缺省值口径不变（DEFAULT_CONFIG["concurrency"] = 4）
    assert experiments._pool_plan(_exp(), PLAIN_MODELS)["workers"] == 4
    assert experiments.DEFAULT_CONFIG["concurrency"] == 4
    # 下界与 1 也照旧
    assert experiments._pool_plan(_exp(1), PLAIN_MODELS)["workers"] == 1
    assert experiments._pool_plan(_exp(0), PLAIN_MODELS)["workers"] == 1


def test_pool_map_runtime_normal_path_no_extra_cap(spy, capsys):
    experiments._pool_map(_exp(4), list(range(6)), lambda x: {"ok": True},
                          models=PLAIN_MODELS)
    assert spy.seen == [4]
    assert capsys.readouterr().out == "", "正常路径不该多打日志"


def test_no_models_passed_means_no_serial_protection():
    """models 是可选参数：不传等于放弃串行判定——钉住调用点必须显式传。"""
    plan = experiments._pool_plan(_exp(8))
    assert plan["workers"] == 8 and plan["serial_forced"] is False


def test_every_pool_map_call_site_passes_models():
    """静态钉住：app/experiments.py 里每个 _pool_map(...) 调用都带 models=，
    新增 stage 忘了传就红（这正是「执行侧不认串行纪律」的复发路径）。"""
    tree = ast.parse((ROOT / "app" / "experiments.py").read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name == "_pool_map":
                calls.append(node)
    assert len(calls) >= 6, f"调用点数量对不上（预期 ≥6 个 stage 发模型请求），实得 {len(calls)}"
    missing = [c.lineno for c in calls
               if not any(k.arg == "models" for k in c.keywords)]
    assert not missing, f"以下 _pool_map 调用点没传 models（串行纪律会失效）：{missing}"
