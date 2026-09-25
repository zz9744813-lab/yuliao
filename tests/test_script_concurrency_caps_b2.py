"""旁路脚本并发上限回归（第二批，2026-09-25）。

第一批（`tests/test_script_concurrency_caps.py`，收口 clean_text / backfill_judges /
dim_probe / extra_judges / scale_corpus / run_calibration）落地两层闸后，交付文档
§4 列出的 12 个旁路脚本仍是裸口：`--conc 200` 直开越界线程池打网关，
`ai_ranking_build` 甚至把 6 硬编码绕过一切命令行。

本文件对第二批 12 个脚本（source_check / benchmark_run / frame_violation /
heldout_eval / overexplain_probe / pref_judge / prerun_batch_judges / span_probe /
xcorpus_bias / run_nat_v2 / controlled_corruption / ai_ranking_build）钉死四类不变量：

1. **CLI 越界响亮报错退出**（SystemExit(2)，含「超过上限/上限值/静默」三要素），
   且闸在一切库/网络副作用之前（booby-trap 证明顺序）；下界 <1 同样响亮；
2. **上限单一真源**：闸值动态取 `app/limits.MAX_CONCURRENCY`——真源改 3，
   各脚本闸必须跟着收紧；脚本里不许再写一份字面量上限、不许复刻
   `check_conc`/`pool_workers` 本体；
3. **运行时兜底**：绕过 argparse 直调执行入口传越界值 → 实际
   `ThreadPoolExecutor.max_workers` ≤ 上限且打印「越界截断」；
4. **串行纪律**：命中单账号 CLI 通道（复用 `gateway.is_serial_model`，
   经 `scripts/_conc_guard.py` 单点）→ workers 恒 1 并打印命中模型名；
   注入替换判定函数，决策必须跟着变（防写死前缀）。

默认路径（按脚本：source_check 8、benchmark_run 6、frame_violation 4、heldout_eval 4、
overexplain_probe 4、pref_judge 4、prerun_batch_judges 4、span_probe 4、xcorpus_bias 5、
controlled_corruption 4、run_nat_v2 6、ai_ranking_build 6）worker 数与改前逐字一致、
零新增输出。

纯离线：`LG_LLM_MODE=mock`（conftest），线程池一律用替身捕获 `max_workers`，
**不真开线程、不发任何模型请求、不连网关、不读密钥、不碰生产库**。
"""
import ast
import concurrent.futures as cf
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import gateway, limits  # noqa: E402

import _conc_guard as cg  # noqa: E402
import ai_ranking_build as ab  # noqa: E402
import benchmark_run as br  # noqa: E402
import controlled_corruption as cc  # noqa: E402
import frame_violation as fv  # noqa: E402
import heldout_eval as he  # noqa: E402
import overexplain_probe as op  # noqa: E402
import pref_judge as pj  # noqa: E402
import prerun_batch_judges as pb  # noqa: E402
import run_nat_v2 as rn  # noqa: E402
import source_check as sc  # noqa: E402
import span_probe as sp  # noqa: E402
import xcorpus_bias as xb  # noqa: E402

_SCRIPTS_DIR = ROOT / "scripts"

PLAIN = ["moonshotai/kimi-k3", "z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash"]
SERIAL = ["agy/gemini-3.8-flash-high", "qoder/Qwen3.8-Flash",
          "wb/hy4-preview-f", "zcode/glm-5.3-flash"]

# 有 `--conc` 的 10 个脚本：extra 是走到闸所需的前置参数；default 为改前口径（不许动）。
GATED = {
    "source_check": dict(mod=sc, extra=["--run"], default=8),
    "benchmark_run": dict(mod=br, extra=[], default=6),
    "frame_violation": dict(mod=fv, extra=[], default=4),
    "heldout_eval": dict(mod=he, extra=[], default=4),
    "overexplain_probe": dict(mod=op, extra=[], default=4),
    "pref_judge": dict(mod=pj, extra=[], default=4),
    "prerun_batch_judges": dict(mod=pb, extra=["--batch", "capb2"], default=4),
    "span_probe": dict(mod=sp, extra=[], default=4),
    "xcorpus_bias": dict(mod=xb, extra=[], default=5),
    "controlled_corruption": dict(mod=cc, extra=[], default=4),
}
GATED_IDS = sorted(GATED)

# 无 `--conc` 的两个收口点（口径不同，单独钉）：
#   run_nat_v2 —— 第 3 位置参数（parse_cli 里过闸）；默认 6。
#   ai_ranking_build —— 原硬编码 6 改为「请求值 6 过 pool_workers」，无 CLI。
ALL12 = GATED_IDS + ["run_nat_v2", "ai_ranking_build"]

DEFAULT_CONC = {**{k: v["default"] for k, v in GATED.items()},
                "run_nat_v2": 6, "ai_ranking_build": 6}

# 每个脚本线程池调用点数（改后口径；新增池忘了报数即红）。
SITES = {"source_check": 1, "benchmark_run": 1, "frame_violation": 1,
         "heldout_eval": 2, "overexplain_probe": 1, "pref_judge": 1,
         "prerun_batch_judges": 1, "span_probe": 1, "xcorpus_bias": 1,
         "run_nat_v2": 1, "controlled_corruption": 5, "ai_ranking_build": 1}

POOL_MODULES = [sc, br, fv, he, op, pj, pb, sp, xb, rn, cc, ab]


# ── 替身：捕获 max_workers，绝不真开线程 ──────────────────────

class _NoRunExecutor:
    """记录传给线程池的 max_workers；不执行任何任务、不起任何线程。"""
    seen: list[int | None] = []

    def __init__(self, *args, **kwargs):
        _NoRunExecutor.seen.append(kwargs.get("max_workers"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, items, timeout=None):
        return iter(())

    def submit(self, fn, *a, **kw):
        raise AssertionError("测试替身不该 submit")


@pytest.fixture()
def spy(monkeypatch):
    _NoRunExecutor.seen = []
    for m in POOL_MODULES:
        if hasattr(m, "ThreadPoolExecutor"):
            monkeypatch.setattr(m, "ThreadPoolExecutor", _NoRunExecutor)
    # ai_ranking_build 在函数体内 import，模块命名空间没有既有绑定可换：
    # 换 concurrent.futures 的源头属性（monkeypatch 会还原）。
    monkeypatch.setattr(cf, "ThreadPoolExecutor", _NoRunExecutor)
    return _NoRunExecutor


# ── 执行入口统一签名：runner(monkeypatch, tmp_path, conc, models) ─
# 每个 runner 恰好开一个池（被 spy 捕获），全部打在桩上：零库写、零网络。

def _fake_session(rows=(), get_obj=None):
    class _Q:
        def filter(self, *c):
            return self

        def filter_by(self, *c, **k):
            return self

        def all(self):
            return list(rows)

    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, model, pk):
            return get_obj(pk) if callable(get_obj) else get_obj

        def query(self, *m):
            return _Q()

        def add(self, obj):
            pass

        def commit(self):
            pass

    return _S()


def _run_sc(monkeypatch, tmp_path, conc, models):
    seg = SimpleNamespace(id="SEG-CAPB2", text="测试段测试段测试段", text_clean="测试段测试段测试段",
                          integrity="{}")
    monkeypatch.setattr(sc, "MODEL", models[0])
    monkeypatch.setattr(sc, "_preflight", lambda: None)
    monkeypatch.setattr(sc, "db", SimpleNamespace(
        session=lambda: _fake_session(rows=[seg]), init_db=lambda: None))
    sc.run(scope="used", conc=conc, ids=["SEG-CAPB2"])


def _run_br(monkeypatch, tmp_path, conc, models):
    br._run_pool([], conc, models, lambda j: None)


def _run_fv(monkeypatch, tmp_path, conc, models):
    fv._run_pool([], conc, models)


def _run_he(monkeypatch, tmp_path, conc, models):
    monkeypatch.setattr(he, "JUDGES", tuple(models))
    he._run_pool([], conc, None)


def _run_op(monkeypatch, tmp_path, conc, models):
    op._run_pool([], conc, models)


def _run_pj(monkeypatch, tmp_path, conc, models):
    pj._run_pool([], conc, models)


def _run_pb(monkeypatch, tmp_path, conc, models):
    pb._run_pool([], conc, models, None)


def _run_sp(monkeypatch, tmp_path, conc, models):
    monkeypatch.setattr(sp, "pf",
                        SimpleNamespace(require_models=lambda models, source="": None))
    monkeypatch.setattr(sp, "annotated_items", lambda: [])
    sp.run(list(models), conc)


def _run_xb(monkeypatch, tmp_path, conc, models):
    xb._run_pool([], conc, models)


def _run_rn(monkeypatch, tmp_path, conc, models):
    monkeypatch.setattr(rn, "CONC", conc)
    monkeypatch.setattr(rn, "JUDGE_MODEL", models[0])
    monkeypatch.setattr(rn, "db", SimpleNamespace(
        session=lambda: _fake_session(rows=[], get_obj=lambda pk: SimpleNamespace(
            config={"segment_ids": []}, id=pk, text="x")),
        init_db=lambda: None))
    rn.main()


def _run_cc(monkeypatch, tmp_path, conc, models):
    class _Con:
        def execute(self, *a, **k):
            return self

        def fetchall(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(cc, "sqlite3",
                        SimpleNamespace(connect=lambda *a, **k: _Con(), Row=object))
    cc.recheck(verify_model=models[0], conc=conc)


def _run_ab(monkeypatch, tmp_path, conc, models):
    monkeypatch.setattr(ab, "JUDGES", tuple(models))
    monkeypatch.setattr(ab, "db", SimpleNamespace(
        session=lambda: _fake_session(), init_db=lambda: None))
    monkeypatch.setattr(ab, "sample_pairs", lambda s, n, seed: [])
    ab.build(0, 1, "capb2", out_dir=tmp_path, dry_run=False)


RUNNERS = {"source_check": _run_sc, "benchmark_run": _run_br,
           "frame_violation": _run_fv, "heldout_eval": _run_he,
           "overexplain_probe": _run_op, "pref_judge": _run_pj,
           "prerun_batch_judges": _run_pb, "span_probe": _run_sp,
           "xcorpus_bias": _run_xb, "run_nat_v2": _run_rn,
           "controlled_corruption": _run_cc, "ai_ranking_build": _run_ab}
RUNNER_IDS = sorted(RUNNERS)
# ai_ranking 的请求值是写死的 6（无 CLI 可越界），运行时夹紧对它不适用（N/A 口径：
# 钉的是「6 也过闸、串行通道命中压成 1」，见下面两个专测）。
CLAMP_IDS = [n for n in RUNNER_IDS if n != "ai_ranking_build"]


# ── 1. CLI 越界：响亮报错退出，且闸在任何副作用之前 ─────────────

def _boom(*a, **kw):
    raise AssertionError("闸未过就碰了库/网关：check_conc 必须在一切副作用之前")


def _gate_stubs(monkeypatch, mod):
    monkeypatch.setattr(mod, "db", SimpleNamespace(init_db=_boom, session=_boom))
    if hasattr(mod, "pf"):
        monkeypatch.setattr(mod, "pf", SimpleNamespace(
            require_models=_boom, preflight_block=_boom, redact=lambda s, n=None: s))
    if hasattr(mod, "sqlite3"):
        monkeypatch.setattr(mod, "sqlite3", SimpleNamespace(connect=_boom))


def _argv(monkeypatch, spec, conc):
    monkeypatch.setattr(sys, "argv",
                        ["prog"] + spec["extra"] + ["--conc", str(conc)])


@pytest.mark.parametrize("name", GATED_IDS)
def test_cli_over_cap_errors_loudly(name, monkeypatch, capsys):
    spec = GATED[name]
    _gate_stubs(monkeypatch, spec["mod"])
    _argv(monkeypatch, spec, 200)
    with pytest.raises(SystemExit) as ei:
        spec["mod"].main()
    assert ei.value.code == 2, "parser.error 的非零退出码（响亮，不静默）"
    err = capsys.readouterr().err
    assert "超过上限" in err and str(limits.MAX_CONCURRENCY) in err
    assert "静默" in err, "报错里要明确说是不静默 clamp"


@pytest.mark.parametrize("name", GATED_IDS)
def test_cli_below_floor_errors_loudly(name, monkeypatch, capsys):
    spec = GATED[name]
    _gate_stubs(monkeypatch, spec["mod"])
    _argv(monkeypatch, spec, 0)
    with pytest.raises(SystemExit) as ei:
        spec["mod"].main()
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "低于下界" in err and "静默" in err


@pytest.mark.parametrize("name", GATED_IDS)
def test_gate_passes_at_cap_then_hits_boobytrap(name, monkeypatch):
    """界内（=上限值）放行：main 走到第一个库/网关副作用才炸——
    证明闸只拦越界，不越权拦界内值，且副作用确实排在闸后。"""
    spec = GATED[name]
    _gate_stubs(monkeypatch, spec["mod"])
    _argv(monkeypatch, spec, limits.MAX_CONCURRENCY)
    with pytest.raises(AssertionError):
        spec["mod"].main()


@pytest.mark.parametrize("name", GATED_IDS)
def test_cli_cap_follows_limits_source(name, monkeypatch, capsys):
    """上限单一真源：真源收紧到 3，各脚本闸必须跟着动（不许各自记一份 16）。"""
    spec = GATED[name]
    _gate_stubs(monkeypatch, spec["mod"])
    monkeypatch.setattr(limits, "MAX_CONCURRENCY", 3)
    _argv(monkeypatch, spec, 4)
    with pytest.raises(SystemExit) as ei:
        spec["mod"].main()
    assert ei.value.code == 2
    assert "上限 3" in capsys.readouterr().err
    _argv(monkeypatch, spec, 3)
    with pytest.raises(AssertionError):
        spec["mod"].main()


def test_run_nat_v2_positional_gate(monkeypatch):
    """run_nat_v2 无 `--conc`：第 3 位置参数同样过闸（上/下越界都 exit 2）、
    默认（缺省位置参数）仍 6、闸值动态跟真源。"""
    for bad in (200, 0):
        with pytest.raises(SystemExit) as ei:
            rn.parse_cli(["run_nat_v2.py", "EXP-X", PLAIN[0], str(bad)])
        assert ei.value.code == 2
    assert rn.parse_cli(["run_nat_v2.py"]) == (rn.EXP, rn.JUDGE_MODEL, 6)
    assert rn.parse_cli(["run_nat_v2.py", "E", "m", str(limits.MAX_CONCURRENCY)])[2] \
        == limits.MAX_CONCURRENCY
    monkeypatch.setattr(limits, "MAX_CONCURRENCY", 3)
    assert rn.parse_cli(["run_nat_v2.py", "E", "m", "3"])[2] == 3
    with pytest.raises(SystemExit):
        rn.parse_cli(["run_nat_v2.py", "E", "m", "4"])


def test_run_nat_v2_positional_errors_are_loud(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        rn.parse_cli(["run_nat_v2.py", "E", "m", "200"])
    err = capsys.readouterr().err
    assert "超过上限" in err and str(limits.MAX_CONCURRENCY) in err and "静默" in err


def test_run_nat_v2_invalid_position_is_parser_error(capsys):
    with pytest.raises(SystemExit) as ei:
        rn.parse_cli(["run_nat_v2.py", "E", "m", "not-an-int"])
    assert ei.value.code == 2
    assert "不是整数" in capsys.readouterr().err


def test_run_nat_v2_import_has_no_argv_side_effect():
    """改前模块级 `int(sys.argv[3])`——import 即吞 argv；改后 sys.argv 只允许
    出现在 `__main__` 守卫块内。"""
    tree = ast.parse((_SCRIPTS_DIR / "run_nat_v2.py").read_text(encoding="utf-8"))
    guarded = [n for n in tree.body
               if isinstance(n, ast.If) and "sys.argv" in ast.unparse(n)]
    assert len(guarded) == 1 and "__name__" in ast.unparse(guarded[0].test)
    for node in tree.body:
        if node is guarded[0]:
            continue
        assert "sys.argv" not in ast.unparse(node)


def test_ai_ranking_no_silent_hardcoded_pool(spy, monkeypatch, tmp_path, capsys):
    """ai_ranking_build 原先 `ThreadPoolExecutor(max_workers=6)` 绕过一切护栏：
    现在请求值 6 也过 pool_workers——界内逐字不变（[6]、零新输出）。"""
    _run_ab(monkeypatch, tmp_path, 0, PLAIN)
    assert spy.seen == [6]
    out = capsys.readouterr().out
    assert "[conc]" not in out, "界内路径不许多出兜底日志"


def test_ai_ranking_serial_channel_forces_one(spy, monkeypatch, tmp_path, capsys):
    m = "qoder/Qwen3.8-Flash"
    _run_ab(monkeypatch, tmp_path, 0, [m, PLAIN[0]])
    assert spy.seen == [1]
    out = capsys.readouterr().out
    assert "串行强制" in out and m in out


# ── 2. 运行时兜底：绕过 CLI 也开不出越界池 ─────────────────────

@pytest.mark.parametrize("name", CLAMP_IDS)
def test_runtime_clamps_over_cap(name, spy, monkeypatch, tmp_path, capsys):
    RUNNERS[name](monkeypatch, tmp_path, 200, PLAIN)
    assert spy.seen == [limits.MAX_CONCURRENCY], \
        f"{name} 直调传 200 实际开池 {spy.seen}，应被兜底到上限"
    out = capsys.readouterr().out
    assert "越界截断" in out, "兜底截断必须显式打印，不许静默"


# ── 3. 串行纪律：命中单账号 CLI 通道 → workers 恒 1 且声明 ──────

@pytest.mark.parametrize("m", SERIAL)
def test_guard_serial_prefixes_cover_channels(m, capsys):
    assert cg.pool_workers(8, [m]) == 1, f"{m} 是本机 CLI 单账号通道，必须串行"
    assert "串行强制" in capsys.readouterr().out


def test_guard_plain_models_untouched(capsys):
    assert cg.pool_workers(8, PLAIN) == 8
    assert capsys.readouterr().out == "", "界内且非串行 → 零输出"


@pytest.mark.parametrize("bad", [0, -1])
def test_guard_runtime_lower_bound_is_loud(bad, capsys):
    assert cg.pool_workers(bad, PLAIN) == cg.MIN_CONCURRENCY
    assert "越界截断" in capsys.readouterr().out


def test_guard_invalid_runtime_value_is_loud(capsys):
    assert cg.pool_workers("not-an-int", PLAIN) == cg.MIN_CONCURRENCY
    assert "非法" in capsys.readouterr().out


def test_guard_reuses_gateway_predicate():
    """串行判定必须复用 gateway.is_serial_model（口径源唯一），不许自比前缀。"""
    assert cg.is_serial_model is gateway.is_serial_model
    assert not hasattr(cg, "SERIAL_MODEL_PREFIXES")
    guard_src = (_SCRIPTS_DIR / "_conc_guard.py").read_text(encoding="utf-8")
    assert not re.search(r"startswith\(\s*[\"'](agy|qoder|wb|zcode)/", guard_src)


def test_serial_injection_flips_worker_decision(spy, monkeypatch):
    """注入各脚本持有的同一判定绑定，worker 决策必须跟着变。"""
    fake = lambda m: m == "weird/made-up-model"
    for name in ("benchmark_run", "frame_violation", "pref_judge", "xcorpus_bias"):
        mod = GATED[name]["mod"]
        monkeypatch.setattr(mod, "is_serial_model", fake)
        RUNNERS[name](monkeypatch, Path("/tmp-unused"), 8,
                      ["weird/made-up-model", PLAIN[0]])
        assert spy.seen == [1], f"{name} 的串行判定没走可注入的复用函数"
        spy.seen.clear()
        monkeypatch.setattr(mod, "is_serial_model", gateway.is_serial_model)
        RUNNERS[name](monkeypatch, Path("/tmp-unused"), 8,
                      ["weird/made-up-model", PLAIN[0]])
        assert spy.seen == [8], f"{name} 换回真判定后不该继续串行"
        spy.seen.clear()


@pytest.mark.parametrize("name", RUNNER_IDS)
def test_serial_model_forces_one_per_script(name, spy, monkeypatch, tmp_path, capsys):
    m = "zcode/glm-5.3-flash"
    RUNNERS[name](monkeypatch, tmp_path, DEFAULT_CONC[name], [m] + PLAIN[:2])
    assert spy.seen == [1], f"{name}: {m} 是单账号 CLI 通道，必须串行"
    out = capsys.readouterr().out
    assert "串行强制" in out and m in out, "必须显式打印串行强制与命中模型名"


# ── 4. 默认路径不回退 ──────────────────────────────────────────

def test_default_worker_counts_identical_to_pre_fix(spy, monkeypatch, tmp_path, capsys):
    """12 个脚本各自的默认 conc（8/6/4/4/4/4/4/4/5/4/6/6）界内：
    worker 数原样、零新增输出（默认行为与改前逐字一致）。"""
    seen = []
    for name in RUNNER_IDS:
        RUNNERS[name](monkeypatch, tmp_path, DEFAULT_CONC[name], PLAIN[:2])
        seen.extend(spy.seen)
        spy.seen.clear()
    assert seen == [DEFAULT_CONC[n] for n in RUNNER_IDS], \
        f"默认 worker 数与改前不一致：{seen}"
    out = capsys.readouterr().out
    assert "[conc]" not in out, "界内路径不该多出兜底日志"


@pytest.mark.parametrize("name", GATED_IDS)
def test_cli_defaults_pinned_in_source(name):
    """静态钉：main() 里 `--conc` 的 default 字面值 == 改前口径。"""
    src = (_SCRIPTS_DIR / f"{name}.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    got = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "add_argument"
                and node.args and getattr(node.args[0], "value", None) == "--conc"):
            kw = next(k for k in node.keywords if k.arg == "default")
            got = kw.value.value
    assert got == GATED[name]["default"], f"{name} 默认 --conc 被改了：{got}"


# ── 静态底线：调用点 / 单源 / 零复刻 ───────────────────────────

@pytest.mark.parametrize("name", sorted(SITES))
def test_every_executor_site_uses_pool_workers(name):
    """每个 ThreadPoolExecutor(max_workers=X) 的 X 只能是 `workers`，
    且 workers 必须来自 pool_workers(...)——新增池忘了夹就是红。"""
    src = (_SCRIPTS_DIR / f"{name}.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fname == "ThreadPoolExecutor":
                kw = next(k for k in node.keywords if k.arg == "max_workers")
                assert ast.unparse(kw.value) == "workers", \
                    f"{name}.py:{node.lineno} 线程池没有用 pool_workers 兜底后的值"
                n += 1
    assert n == SITES[name], f"{name}.py 线程池调用点数与预期不符：{n} != {SITES[name]}"
    assert re.search(r"workers\s*=\s*pool_workers\(", src)


@pytest.mark.parametrize("name", ALL12)
def test_single_source_and_zero_copy(name):
    """上限只有一个真源、闸实现只有一份：
    - 脚本不许出现 `MAX_CONCURRENCY = <字面量>`；
    - 不许复刻 check_conc/pool_workers 本体（必须 import _conc_guard）；
    - 不许自带前缀表/前缀比较（串行口径复用 gateway）。"""
    src = (_SCRIPTS_DIR / f"{name}.py").read_text(encoding="utf-8")
    assert re.search(r"^MAX_CONCURRENCY\s*=\s*\d", src, re.M) is None, \
        f"{name}.py 又复刻了一份字面量上限"
    assert "from _conc_guard import" in src, f"{name}.py 没走共用并发闸入口"
    assert re.search(r"def (check_conc|pool_workers)\b", src) is None, \
        f"{name}.py 复刻了闸实现（第二批要求零拷贝单点复用）"
    assert not re.search(r"startswith\(\s*[\"'](agy|qoder|wb|zcode)/", src), \
        f"{name}.py 在自己比串行前缀"
    assert "SERIAL_MODEL_PREFIXES" not in src


def test_guard_cap_reads_limits_dynamically(monkeypatch):
    """兜底夹紧值动态取真源：limits 改 2，pool_workers 夹紧必须变 2。"""
    monkeypatch.setattr(limits, "MAX_CONCURRENCY", 2)
    assert cg.pool_workers(8, [PLAIN[0]]) == 2


def test_guard_tag_appears_in_messages(capsys):
    cg.pool_workers(200, [PLAIN[0]], tag="cap-tag")
    out = capsys.readouterr().out
    assert out.startswith("[cap-tag]") and "越界截断" in out
