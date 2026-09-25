"""并发闸单源去重回归（2026-09-25 会审双席：6 份逐字复制 → 单一实现 + 下界闸）。

上一轮（`07b3873` fix/script-conc-caps）把 `limits.MAX_CONCURRENCY` 做成了上限
单一真源，但 `_pool_workers` / `_check_conc` 这对 helper 本身在 6 个旁路脚本里
**逐字复制了 6 份**（glm-5.3 席与 qwen 席独立提出同一问题）；同时两席都指出
`_check_conc` 只拦上界，`--conc 0` / 负数被 CLI 放行、运行时 `max(1, …)` **静默**
归一——下界也是 clamp，且无任何打印。

本文件钉死五条不变量：

1. **只剩一份实现**：6 个脚本（clean_text / backfill_judges / dim_probe /
   extra_judges / scale_corpus / run_calibration）**不再自带** `_pool_workers` /
   `_check_conc` 定义（AST `FunctionDef` + 源码扫描双钉）；唯一实现收敛到
   `scripts/_conc_guard.py`，各脚本只 `from _conc_guard import ...` 复用；
2. **上界闸**：`check_conc` 在 `limits.MAX_CONCURRENCY` 之上 → `SystemExit` 且
   退出码 2（响亮报错，不静默 clamp）；
3. **下界新闸**：`check_conc` 对 `0` / `-1`（且脚本 `parse_args` 对 `--conc 0/-1`）
   → 同样报错退出——以前被 CLI 放行、运行时 max(1,…) 静默归一的缝隙被堵死；
4. **运行时兜底不再静默**：`pool_workers` 越界夹紧打印「越界截断」、非正值/
   非法值打印后取 1、命中单账号 CLI 模型（`gateway.is_serial_model`，
   qoder/ wb/ zcode/ agy/ 前缀）→ 恒 1 且打印「串行强制」；界内默认值零额外输出；
5. **默认路径无回归**：6 个脚本 `parse_args` 默认参数下不报错。

纯离线：只调函数与 parse_args，不建线程池、不发任何模型请求、不连网关、不读密钥。
"""
import argparse
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import _conc_guard  # noqa: E402
import backfill_judges  # noqa: E402
import clean_text  # noqa: E402
import dim_probe  # noqa: E402
import extra_judges  # noqa: E402
import run_calibration  # noqa: E402
import scale_corpus  # noqa: E402

from app import limits  # noqa: E402
from _conc_guard import MIN_CONCURRENCY, check_conc, pool_workers  # noqa: E402

_SCRIPTS_DIR = ROOT / "scripts"
SCRIPTS = ["clean_text", "backfill_judges", "dim_probe", "extra_judges",
           "scale_corpus", "run_calibration"]
MODS = {
    "clean_text": clean_text,
    "backfill_judges": backfill_judges,
    "dim_probe": dim_probe,
    "extra_judges": extra_judges,
    "scale_corpus": scale_corpus,
    "run_calibration": run_calibration,
}
# extra_judges 的 --models 是必填；其余脚本默认参数即可
DEFAULTS = {
    "clean_text": dict(flag="--conc", attr="conc", extra=[]),
    "backfill_judges": dict(flag="--conc", attr="conc", extra=[]),
    "dim_probe": dict(flag="--conc", attr="conc", extra=[]),
    "extra_judges": dict(flag="--conc", attr="conc",
                         extra=["--models", "moonshotai/kimi-k3"]),
    "scale_corpus": dict(flag="--conc", attr="conc", extra=[]),
    "run_calibration": dict(flag="--concurrency", attr="concurrency", extra=[]),
}
PLAIN_MODELS = ["moonshotai/kimi-k3", "z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash"]
SERIAL_MODELS = ["qoder/Qwen3.8-Flash", "wb/hy4-preview-f",
                 "zcode/glm-5.3-flash", "agy/gemini-3.8-flash-high"]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--conc", type=int)
    return p


# ── 1. 只剩一份实现：6 脚本不再自带 helper 定义 ─────────────────

@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_no_local_conc_guard_definition(name):
    """源码/AST 双钉：6 个脚本不再 `def _pool_workers` / `def _check_conc`
    （也不许把公共名 check_conc/pool_workers 再抄成局部定义）。"""
    src = (_SCRIPTS_DIR / f"{name}.py").read_text(encoding="utf-8")
    for bad in ("_pool_workers", "_check_conc", "pool_workers", "check_conc"):
        assert f"def {bad}" not in src, f"{name}.py 又自带了一份 {bad} 实现？"
    tree = ast.parse(src)
    names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert "_pool_workers" not in names and "_check_conc" not in names, \
        f"{name}.py 的 AST 里还有本地并发闸 helper"
    assert "pool_workers" not in names and "check_conc" not in names


def test_only_one_implementation_remains_in_conc_guard():
    """实现收敛到 scripts/_conc_guard.py 一份，且真源引用不另立常量。"""
    src = (_SCRIPTS_DIR / "_conc_guard.py").read_text(encoding="utf-8")
    assert "def check_conc" in src
    assert "def pool_workers" in src
    assert "limits.MAX_CONCURRENCY" in src, "上限真源必须仍旧只从 app/limits 读"


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_import_from_single_source(name):
    src = (_SCRIPTS_DIR / f"{name}.py").read_text(encoding="utf-8")
    assert "from _conc_guard import" in src, f"{name}.py 没走到单一实现入口"


# ── 2/3. check_conc：上界照旧、下界新闸、界内原样 ───────────────

def test_check_conc_rejects_above_max_with_exit_2():
    """上限：MAX_CONCURRENCY+1 → 响亮报错退出（SystemExit code 2），不静默 clamp。"""
    with pytest.raises(SystemExit) as ei:
        check_conc(_parser(), limits.MAX_CONCURRENCY + 1, "--conc")
    assert ei.value.code == 2


@pytest.mark.parametrize("bad", [0, -1])
def test_check_conc_rejects_below_min_with_exit_2(bad):
    """下界新闸：`--conc 0` / 负数 →同样报错退出（以前被 CLI 放行）。"""
    with pytest.raises(SystemExit) as ei:
        check_conc(_parser(), bad, "--conc")
    assert ei.value.code == 2


def test_check_conc_lower_bound_error_is_loud(capsys):
    """下界越界报错文案：明说「下界」「拒绝静默归一」，不是啥都不说静默取 1。"""
    with pytest.raises(SystemExit):
        check_conc(_parser(), 0, "--conc")
    err = capsys.readouterr().err
    assert "下界" in err and "静默" in err


def test_check_conc_returns_in_bound_value_untouched():
    assert check_conc(_parser(), 1, "--conc") == 1
    assert check_conc(_parser(), 8, "--conc") == 8
    assert check_conc(_parser(), "8", "--conc") == 8
    assert check_conc(_parser(), limits.MAX_CONCURRENCY, "--conc") == limits.MAX_CONCURRENCY
    assert check_conc(None, None, "--conc") is None


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_gate_rejects_below_min(name):
    """下界闸接进每个脚本的 parse_args：`--conc 0/-1` → exit 2。"""
    spec = DEFAULTS[name]
    for bad in (0, -1):
        with pytest.raises(SystemExit) as ei:
            MODS[name].parse_args(spec["extra"] + [spec["flag"], str(bad)])
        assert ei.value.code == 2


# ── 4. pool_workers：夹紧+打印不静默、串行强制、界内零输出 ──────

def test_pool_workers_clamps_over_cap_and_prints(capsys):
    out = pool_workers(limits.MAX_CONCURRENCY + 5, list(PLAIN_MODELS))
    assert out == limits.MAX_CONCURRENCY
    assert "越界截断" in capsys.readouterr().out, "运行时兜底也要显式打印，不许静默"


@pytest.mark.parametrize("bad", [0, -1])
def test_pool_workers_nonpositive_now_loud(bad, capsys):
    """非正值不再静默 max(1,…)：打印后取下界。"""
    assert pool_workers(bad, list(PLAIN_MODELS)) == MIN_CONCURRENCY
    assert "越界截断" in capsys.readouterr().out


def test_pool_workers_invalid_value_prints_and_returns_min(capsys):
    assert pool_workers("abc") == MIN_CONCURRENCY
    assert "非法" in capsys.readouterr().out


@pytest.mark.parametrize("m", SERIAL_MODELS)
def test_pool_workers_serial_forces_one_and_prints(m, capsys):
    assert pool_workers(8, [m]) == 1, f"{m} 是本机 CLI 单账号，必须串行"
    out = capsys.readouterr().out
    assert "串行强制" in out and m in out, "必须显式打印串行强制与命中模型名"


def test_pool_workers_in_bound_default_is_silent(capsys):
    """界内默认值原样返回、零额外输出（默认路径与改前逐字一致）。"""
    assert pool_workers(8, list(PLAIN_MODELS)) == 8
    assert pool_workers(1) == 1
    assert pool_workers(limits.MAX_CONCURRENCY) == limits.MAX_CONCURRENCY
    assert capsys.readouterr().out == "", "界内路径不该多出任何兜底日志"


def test_pool_workers_accepts_injected_serial_check():
    """与既有「注入判定函数」夹具同构：serial_check 可注入，判定跟着变。"""
    fake = lambda m: m == "weird/made-up-model"
    assert pool_workers(8, ["weird/made-up-model"], serial_check=fake) == 1
    assert pool_workers(8, list(PLAIN_MODELS), serial_check=fake) == 8


# ── 5. 默认路径无回归 ──────────────────────────────────────────

@pytest.mark.parametrize("name", SCRIPTS)
def test_parse_args_defaults_pass(name, capsys):
    """6 个脚本 parse_args 默认参数下不报错（默认 conc 全部界内）。"""
    spec = DEFAULTS[name]
    args = MODS[name].parse_args(spec["extra"])
    assert 1 <= getattr(args, spec["attr"]) <= limits.MAX_CONCURRENCY
    capsys.readouterr()