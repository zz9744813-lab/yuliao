"""旁路脚本并发上限回归（第二批，2026-09-25）。

上一轮（`d3f4954`，tests/test_experiment_concurrency_cap.py）把执行侧
`app/experiments._pool_map` 收口到 `app/limits.MAX_CONCURRENCY` 并落实串行纪律，
但**旁路脚本自己开线程池**，`--conc` 命令行参数绕过全部护栏：
`python scripts/clean_text.py --llm --conc 200` 会开 200 个线程打网关；
`scale_corpus`/`run_calibration` 还会把越界值**写进实验 config 落库**。

本文件对六个脚本（clean_text / backfill_judges / dim_probe / extra_judges /
scale_corpus / run_calibration）钉死五条不变量：

1. **上限单一真源**：每个脚本的 `--conc`/`--concurrency` 上界动态取
   `limits.MAX_CONCURRENCY`（monkeypatch 改真源，脚本闸值必须跟着动）；
   源码里不许再出现一份 `MAX_CONCURRENCY = <字面量>` 赋值；
2. **CLI 越界响亮报错退出**（SystemExit(2)，不是静默 clamp）；
3. **运行时兜底**：绕过 argparse 直接调函数传越界值 → 实际传给
   `ThreadPoolExecutor` 的 `max_workers`（或写进 config / 下传给
   source_check 的并发）≤ `limits.MAX_CONCURRENCY`，且显式打印、不静默；
4. **串行纪律**：命中单账号 CLI 模型（复用 `gateway.is_serial_model`，
   不许自比前缀）→ workers 恒 1，stdout 打印「串行强制」与命中模型名；
5. **默认路径不回退**：各自默认 conc（8/6/3/4/8/4）界内时 worker 数与改前
   逐字一致、零新增输出。

纯离线：`LG_LLM_MODE=mock`（conftest），线程池一律用替身捕获 `max_workers`，
**不真开线程、不发任何模型请求、不连网关、不读密钥**；scale_corpus 的整链
测试全部打在桩上。
"""
import ast
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import gateway, limits  # noqa: E402
from app.models import Experiment  # noqa: E402

import backfill_judges  # noqa: E402
import clean_text  # noqa: E402
import dim_probe  # noqa: E402
import extra_judges  # noqa: E402
import run_calibration  # noqa: E402
import scale_corpus  # noqa: E402

PLAIN_MODELS = ["moonshotai/kimi-k3", "z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash"]
SERIAL_MODELS = ["agy/gemini-3.8-flash-high", "qoder/Qwen3.8-Flash",
                 "wb/hy4-preview-f", "zcode/glm-5.3-flash"]

# 每个脚本的闸入口 / 越界后兜底入口 / 默认值（改前口径，不许动）。
# `parse` 调脚本的 parse_args(argv)；`extra` 是必填参数前缀；`attr` 是参数 dest。
SPECS = {
    "clean_text": dict(mod=clean_text, flag="--conc", attr="conc", default=8, extra=[]),
    "backfill_judges": dict(mod=backfill_judges, flag="--conc", attr="conc",
                            default=6, extra=[]),
    "dim_probe": dict(mod=dim_probe, flag="--conc", attr="conc", default=3, extra=[]),
    "extra_judges": dict(mod=extra_judges, flag="--conc", attr="conc", default=4,
                         extra=["--models", "moonshotai/kimi-k3"]),
    "scale_corpus": dict(mod=scale_corpus, flag="--conc", attr="conc", default=8,
                         extra=[]),
    "run_calibration": dict(mod=run_calibration, flag="--concurrency",
                            attr="concurrency", default=4, extra=[]),
}
SPEC_IDS = sorted(SPECS)


def _spec(name):
    return SPECS[name]


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
    for spec in SPECS.values():
        if hasattr(spec["mod"], "ThreadPoolExecutor"):
            monkeypatch.setattr(spec["mod"], "ThreadPoolExecutor", _NoRunExecutor)
    return _NoRunExecutor


# ── 1. 上限单一真源 ───────────────────────────────────────────

@pytest.mark.parametrize("name", SPEC_IDS)
def test_cap_reads_limits_dynamically(name):
    """脚本上界必须 == limits.MAX_CONCURRENCY 且实时取值：
    改真源为 3，脚本的闸必须跟着收紧——两边不许各写一份。"""
    spec = _spec(name)
    for probe in (limits.MAX_CONCURRENCY + 1, 200):
        with pytest.raises(SystemExit):
            spec["mod"].parse_args(spec["extra"] + [spec["flag"], str(probe)])
    ok = spec["mod"].parse_args(spec["extra"] + [spec["flag"], str(limits.MAX_CONCURRENCY)])
    assert getattr(ok, spec["attr"]) == limits.MAX_CONCURRENCY
    try:
        monkey_value = 3
        import app.limits as lim
        old = lim.MAX_CONCURRENCY
        lim.MAX_CONCURRENCY = monkey_value
        with pytest.raises(SystemExit):
            spec["mod"].parse_args(spec["extra"] + [spec["flag"], str(monkey_value + 1)])
        spec["mod"].parse_args(spec["extra"] + [spec["flag"], str(monkey_value)])
    finally:
        lim.MAX_CONCURRENCY = old


@pytest.mark.parametrize("name", SPEC_IDS)
def test_no_second_literal_cap(name):
    """脚本源码不许复刻上限常量：有 `MAX_CONCURRENCY = <字面量>` 赋值即红，
    且必须通过 `limits.MAX_CONCURRENCY` 引用真源。"""
    src = (_SCRIPTS_DIR / f"{name}.py").read_text(encoding="utf-8")
    assert re.search(r"^MAX_CONCURRENCY\s*=\s*\d", src, re.M) is None, \
        f"{name}.py 里又出现了一份 MAX_CONCURRENCY 字面量赋值"
    assert "limits.MAX_CONCURRENCY" in src, f"{name}.py 没有从 app/limits 取上限"


_SCRIPTS_DIR = ROOT / "scripts"


# ── 2. CLI 越界：响亮报错退出，不是静默降级 ────────────────────

@pytest.mark.parametrize("name", SPEC_IDS)
def test_cli_over_cap_errors_loudly(name, capsys):
    spec = _spec(name)
    with pytest.raises(SystemExit) as ei:
        spec["mod"].parse_args(spec["extra"] + [spec["flag"], "200"])
    assert ei.value.code == 2, "parser.error 的非零退出码（响亮，不静默）"
    err = capsys.readouterr().err
    assert "超过上限" in err and str(limits.MAX_CONCURRENCY) in err
    assert "静默" in err, "报错里要明确说是不静默 clamp"


@pytest.mark.parametrize("name", SPEC_IDS)
def test_gate_blocks_before_any_side_effect(name, monkeypatch):
    """闸在 db.init_db() **之前**：越界命令行不会先碰库/网关再报错。

    各 main() 一律先 parse_args（读 sys.argv），越界即 SystemExit；db 被替换成
    「碰一下就炸」的桩——若哪条路径在闸前碰了库，这里就会红。
    """
    spec = _spec(name)
    mod = spec["mod"]

    def _boom(*a, **kw):
        raise AssertionError("越界 --conc 必须在任何库/网络副作用之前 SystemExit")

    monkeypatch.setattr(mod, "db", SimpleNamespace(init_db=_boom, session=_boom))
    monkeypatch.setattr(sys, "argv", ["prog"] + spec["extra"] + [spec["flag"], "200"])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 2


# ── 3. 运行时兜底：绕过 argparse 也开不出越界池 ────────────────

def _fake_clean_text_db(rows):
    class _Q:
        def all(self_inner):
            return rows

    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def query(self, *m):
            return _Q()

        def commit(self):
            pass

    return SimpleNamespace(session=lambda: _S())


def _dirty_seg():
    return SimpleNamespace(id="seg-cap-1",
                           text="一股股白sè雾气在虚空中浮现而出。",
                           text_clean="一股股白sè雾气在虚空中浮现而出。")


def _plain_judges(monkeypatch):
    """钉死评委名单为普通模型：测试不依赖宿主环境 LG_LLM_MODEL 的取值。"""
    monkeypatch.setattr(sys.modules["heldout_eval"], "JUDGES",
                        ("moonshotai/kimi-k3", "deepseek/deepseek-v4.1-flash"))


def test_clean_text_runtime_cap(spy, monkeypatch):
    monkeypatch.setattr(clean_text, "db", _fake_clean_text_db([_dirty_seg()]))
    clean_text.run_llm(conc=200, limit=0)
    assert spy.seen == [limits.MAX_CONCURRENCY], \
        f"run_llm(conc=200) 实际开池 {spy.seen}，应被兜底到上限"


def test_backfill_runtime_cap(spy, monkeypatch):
    _plain_judges(monkeypatch)
    backfill_judges._run_pool([], 200)
    assert spy.seen == [limits.MAX_CONCURRENCY]


def test_dim_probe_runtime_cap(spy):
    dim_probe._run_pool([], 200, PLAIN_MODELS)
    assert spy.seen == [limits.MAX_CONCURRENCY]


def test_extra_judges_runtime_cap(spy):
    extra_judges._run_pool([], 200, PLAIN_MODELS)
    assert spy.seen == [limits.MAX_CONCURRENCY]


def _scale_corpus_stubs(monkeypatch, sc_model="moonshotai/kimi-k3"):
    """把 scale_corpus.run() 整链打在桩上：零库写入、零线程、零模型调用。"""
    rec = {"sc_conc": [], "stage_conc": [], "exp": None}

    class _Q:
        def filter(self, *c):
            return self

        def count(self):
            return 1

    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self_inner, model, pk):
            if model is Experiment:
                return rec["exp"]
            return SimpleNamespace(id=pk, text="正常的中文段落，够长够干净。",
                                   text_clean="正常的中文段落，够长够干净。",
                                   integrity='{"src_ok": true}')

        def add(self, obj):
            if hasattr(obj, "config"):
                rec["exp"] = obj

        def commit(self):
            pass

        def query(self, *m):
            return _Q()

    class _SC:
        MODEL = sc_model

        @staticmethod
        def rule_defects(text):
            return []

        @staticmethod
        def run(conc, ids):
            rec["sc_conc"].append(conc)
            return {"ok": len(ids), "failed": 0}

    session = _S()
    # 钉死缺省模型名：不依赖宿主环境 LG_LLM_MODEL 的取值
    monkeypatch.setattr(scale_corpus.config, "DEFAULT_LLM_MODEL",
                        "deepseek/deepseek-v4.1-flash")
    monkeypatch.setattr(scale_corpus, "pf",
                        SimpleNamespace(preflight_block=lambda models, source="": None,
                                        redact=lambda s, n: s))
    monkeypatch.setattr(scale_corpus, "pick", lambda n, seed, min_chars: list(ids_pool))
    monkeypatch.setattr(scale_corpus, "db", SimpleNamespace(session=lambda: session))
    monkeypatch.setattr(scale_corpus, "source_check", _SC)
    monkeypatch.setattr(scale_corpus, "stage_extract_frames",
                        lambda s, e: rec["stage_conc"].append(e.config["concurrency"]))
    return rec


ids_pool = ["seg-cap-1"]


def test_scale_corpus_runtime_cap_before_config_and_downstream(monkeypatch, capsys):
    """绕过 CLI 直接调 run(conc=200)：写进 exp.config 与下传 source_check 的值
    都必须 ≤ 上限，且打印越界截断（不静默）。"""
    rec = _scale_corpus_stubs(monkeypatch)
    out = scale_corpus.run(300, 1, 200, "EXP-CAPTEST")
    assert out["picked"] == 1 and not out.get("aborted")
    assert rec["sc_conc"] == [limits.MAX_CONCURRENCY], "source_check 拿到越界值=护栏失守"
    assert rec["exp"].config["concurrency"] == limits.MAX_CONCURRENCY, \
        "越界值被写进实验 config 落库=本批事故的复发路径"
    assert rec["stage_conc"] == [limits.MAX_CONCURRENCY]
    assert "越界截断" in capsys.readouterr().out, "兜底截断必须显式打印，不许静默"


def test_run_calibration_overrides_capped_before_config_write(capsys):
    """build_overrides 是 `--concurrency` → 实验 config 的写入口：越界值不许原样落库。"""
    args = SimpleNamespace(segments=24, granularities="S,M,L", temps="0.5,0.9",
                           samples=2, adversarial_k=8, concurrency=64,
                           models=None, judge_models=None, extract_models=None)
    ov = run_calibration.build_overrides(args)
    assert ov["concurrency"] == limits.MAX_CONCURRENCY
    assert "越界截断" in capsys.readouterr().out


# ── 4. 串行纪律：命中 is_serial_model → workers 恒 1 且声明 ────

@pytest.mark.parametrize("name", SPEC_IDS)
def test_serial_check_reuses_gateway(name):
    """判定必须复用 app.gateway.is_serial_model（口径源唯一），不许自比前缀。"""
    spec = _spec(name)
    mod = spec["mod"]
    assert mod.is_serial_model is gateway.is_serial_model, f"{name} 没复用 gateway 判定"
    assert not hasattr(mod, "SERIAL_MODEL_PREFIXES"), f"{name} 复刻了前缀表"
    src = (_SCRIPTS_DIR / f"{name}.py").read_text(encoding="utf-8")
    assert not re.search(r"startswith\(\s*[\"'](agy|qoder|wb|zcode)/", src), \
        f"{name} 在自己比字符串前缀"


def test_serial_injection_flips_worker_decision(spy, monkeypatch):
    """替换复用的判定函数，各脚本 worker 决策必须跟着变——
    若是写死前缀比较，注入不会生效（对齐 test_experiment_concurrency_cap 的钉子）。"""
    fake = lambda m: m == "weird/made-up-model"
    he = sys.modules["heldout_eval"]
    # 评委名单里放一个「假串行」名：真 is_serial_model 不认，注入的 fake 才认
    monkeypatch.setattr(he, "JUDGES", ("moonshotai/kimi-k3", "weird/made-up-model"))
    for name in ("backfill_judges", "dim_probe", "extra_judges"):
        mod = _spec(name)["mod"]
        monkeypatch.setattr(mod, "is_serial_model", fake)
        spy.seen.clear()
        if name == "backfill_judges":
            mod._run_pool([], 8)          # backfill 内部取 he.JUDGES：fake 命中 weird
            assert spy.seen == [1], "注入后 fake 命中名单 → 必须串行（走的是复用函数）"
            spy.seen.clear()
            monkeypatch.setattr(mod, "is_serial_model", gateway.is_serial_model)
            mod._run_pool([], 8)          # 真判定不认 weird/也不认名单里的普通名
            assert spy.seen == [8], "换回真判定后不该继续串行"
        else:
            mod._run_pool([], 8, ["weird/made-up-model"])
            assert spy.seen == [1], f"{name} 的串行判定没走可注入的复用函数"
            spy.seen.clear()
            mod._run_pool([], 8, PLAIN_MODELS)
            assert spy.seen == [8]


@pytest.mark.parametrize("m", SERIAL_MODELS)
def test_dim_probe_serial_forces_one(m, spy, capsys):
    dim_probe._run_pool([], 8, [m])
    assert spy.seen == [1], f"{m} 是本机 CLI 单账号，必须串行"
    out = capsys.readouterr().out
    assert "串行强制" in out and m in out, "必须显式打印串行强制与命中模型名"


@pytest.mark.parametrize("m", SERIAL_MODELS)
def test_extra_judges_serial_forces_one(m, spy, capsys):
    extra_judges._run_pool([], 6, ["moonshotai/kimi-k3", m])
    assert spy.seen == [1], "混合池按最保守处理"
    out = capsys.readouterr().out
    assert "串行强制" in out and m in out


def test_backfill_serial_judges_force_one(spy, capsys, monkeypatch):
    he = sys.modules["heldout_eval"]
    monkeypatch.setattr(he, "JUDGES", ("moonshotai/kimi-k3", "wb/hy4-preview-f"))
    backfill_judges._run_pool([], 6)
    assert spy.seen == [1]
    out = capsys.readouterr().out
    assert "串行强制" in out and "wb/hy4-preview-f" in out


def test_clean_text_serial_llm_model_forces_one(spy, capsys, monkeypatch):
    monkeypatch.setattr(clean_text, "db", _fake_clean_text_db([_dirty_seg()]))
    monkeypatch.setattr(clean_text, "LLM_MODEL", "qoder/Qwen3.8-Flash")
    clean_text.run_llm(conc=8, limit=0)
    assert spy.seen == [1]
    out = capsys.readouterr().out
    assert "串行强制" in out and "qoder/Qwen3.8-Flash" in out


def test_scale_corpus_serial_downstream_model_forces_one(monkeypatch, capsys):
    """LG_SOURCE_MODEL 指到 zcode/ 通道时，scale_corpus 下传给 source_check 的
    并发与写进 config 的并发都必须压成 1。"""
    rec = _scale_corpus_stubs(monkeypatch, sc_model="zcode/glm-5.3-flash")
    scale_corpus.run(300, 1, 8, "EXP-CAPTEST2")
    assert rec["sc_conc"] == [1]
    assert rec["exp"].config["concurrency"] == 1
    out = capsys.readouterr().out
    assert "串行强制" in out and "zcode/glm-5.3-flash" in out


def test_run_calibration_serial_cli_model_forces_one(capsys):
    args = SimpleNamespace(segments=24, granularities="S,M,L", temps="0.5,0.9",
                           samples=2, adversarial_k=8, concurrency=8,
                           models="qoder/Qwen3.8-Flash", judge_models=None,
                           extract_models=None)
    ov = run_calibration.build_overrides(args)
    assert ov["concurrency"] == 1
    out = capsys.readouterr().out
    assert "串行强制" in out and "qoder/Qwen3.8-Flash" in out


# ── 5. 默认值路径行为不变 ──────────────────────────────────────

@pytest.mark.parametrize("name", SPEC_IDS)
def test_defaults_unchanged(name, capsys):
    spec = _spec(name)
    args = spec["mod"].parse_args(spec["extra"])
    assert getattr(args, spec["attr"]) == spec["default"], \
        f"{name} 默认 {spec['flag']} 被改了——默认行为必须与改前逐字一致"
    capsys.readouterr()


def test_default_worker_counts_identical_to_pre_fix(spy, capsys, monkeypatch):
    """界内值（各脚本默认 8/6/3/4/8/4）：worker 数原样、零新增输出。"""
    _plain_judges(monkeypatch)
    monkeypatch.setattr(clean_text, "db", _fake_clean_text_db([_dirty_seg()]))
    clean_text.run_llm(conc=8, limit=0)
    backfill_judges._run_pool([], 6)
    dim_probe._run_pool([], 3, PLAIN_MODELS)
    extra_judges._run_pool([], 4, PLAIN_MODELS)
    assert spy.seen == [8, 6, 3, 4], f"默认 worker 数与改前不一致：{spy.seen}"
    out = capsys.readouterr().out
    assert "[conc]" not in out, "界内路径不该多出兜底日志"


def test_scale_corpus_default_path_unchanged(monkeypatch, capsys):
    rec = _scale_corpus_stubs(monkeypatch)
    out = scale_corpus.run(300, 1, 8, "EXP-CAPTEST3")
    assert out["picked"] == 1
    assert rec["sc_conc"] == [8] and rec["exp"].config["concurrency"] == 8
    o = capsys.readouterr().out
    assert "[conc]" not in o, "默认路径不许多出任何兜底输出（与改前逐字一致）"


def test_run_calibration_default_override_unchanged(capsys):
    args = SimpleNamespace(segments=24, granularities="S,M,L", temps="0.5,0.9",
                           samples=2, adversarial_k=8, concurrency=4,
                           models=None, judge_models=None, extract_models=None)
    ov = run_calibration.build_overrides(args)
    assert ov["concurrency"] == 4
    assert capsys.readouterr().out == "", "正常路径不该打日志"


# ── 附加钉子：线程池调用点全部走兜底 ───────────────────────────

@pytest.mark.parametrize("name", ["clean_text", "backfill_judges", "dim_probe",
                                  "extra_judges"])
def test_every_executor_site_uses_pool_workers(name):
    """静态钉死：脚本里每个 ThreadPoolExecutor(max_workers=X) 的 X 只能是
    `_pool_workers(...)` 的直接结果——新增池忘了夹就是红。"""
    src = (_SCRIPTS_DIR / f"{name}.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fname == "ThreadPoolExecutor":
                kw = next(k for k in node.keywords if k.arg == "max_workers")
                assert ast.unparse(kw.value) == "workers", \
                    f"{name}.py:{node.lineno} 线程池没有用 _pool_workers 兜底后的值"
                n += 1
    assert n == 1, f"{name}.py 的线程池调用点数与预期不符（{n}）"
    # workers 必须来自 _pool_workers
    assert re.search(r"workers\s*=\s*_pool_workers\(", src)
