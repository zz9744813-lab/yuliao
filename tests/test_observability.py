"""可观测聚合层（任务 14）回归测试。

验收口径：
1. 窗口过滤：窗口内/外的行算得清清楚楚，边界秒（== since / == now）行为正确；
2. p50/p95：线性插值口径下对造好的假行算出精确值；
3. 0 条窗口：显式 n_zero 标注 + window_empty 说明，成功率和分位是 None 不是 0，
   绝不拿全表数冒充窗口数；
4. ISO 字符串比较不退化成全表：未来时间戳行、窗口前 1 秒的行都必须被排除；
5. 查无此实验 → raise（纪律④），API 转 404、CLI exit 1；
6. GET /llm/stats 保持旧字段（mode + by_purpose.calls/tokens/failed），只做加法；
7. CLI 子进程直跑（--json / --md / 未知实验报错）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app import db, observability
from app.main import app
from app.models import LlmCall

# 固定「现在」：窗口 = 2026-09-18T12:00:00Z ~ 2026-09-19T12:00:00Z
NOW = "2026-09-19T12:00:00Z"
SINCE = "2026-09-18T12:00:00Z"
EXP_A = "OBS-T14-A"      # 有数据实验
EXP_B = "OBS-T14-B"      # 已知但窗口内 0 条
EXP_NOPE = "OBS-T14-NOPE"  # 全表查无


def _seed() -> None:
    """造假行：EXP_A 7 条（5 在窗内、2 在窗外），EXP_B 1 条 48h 前的。"""
    db.init_db()
    with db.session() as s:
        if observability.experiment_known(s, EXP_A):
            return  # 模块内只造一次（幂等，重跑不重复计数）

    def add(exp, purpose, model, created_at, *, tin=0, tout=0, lat=0,
            status="ok", error=None):
        with db.session() as s:
            s.add(LlmCall(experiment_id=exp, purpose=purpose, model=model,
                          prompt_version="obs_v1", tokens_in=tin, tokens_out=tout,
                          latency_ms=lat, status=status, error=error,
                          created_at=created_at))
            s.commit()

    add(EXP_A, "judge_preference", "mA", "2026-09-19T11:00:00Z",
        tin=100, tout=50, lat=100)
    add(EXP_A, "extract:L", "mB", "2026-09-19T09:00:00Z",
        tin=200, tout=100, lat=200)
    add(EXP_A, "reconstruct:L", "mB", "2026-09-19T10:30:00Z",
        lat=300, status="failed", error="timed out")
    add(EXP_A, "judge_preference", "mA", NOW, tin=10, tout=5, lat=40)
    # 窗口前 1 秒：必须被过滤（字符串比较 < since）
    add(EXP_A, "benchmark", "mC", "2026-09-18T11:59:59Z",
        lat=999, status="failed", error="HTTP 429")
    # 未来时间戳：按毫秒解析之类的错误实现会把它算进窗口，正确实现必须排除
    add(EXP_A, "extract:M", "mC", "2999-01-01T00:00:00Z", tin=999999)
    # 边界：恰好 == since，必须算进窗口（>=）
    add(EXP_A, "extract:L", "mA", SINCE, tin=7, tout=3, lat=60)

    add(EXP_B, "judge_semantic", "mA", "2026-09-17T00:00:00Z", tin=1, tout=1)


@pytest.fixture(scope="module", autouse=True)
def _seeded():
    _seed()


def _snap(**kw) -> dict:
    with db.session() as s:
        return observability.snapshot(s, **kw)


# ── ① 窗口过滤与 ISO 字符串比较 ────────────────────────────────

def test_window_filter_and_iso_string_compare():
    snap = _snap(hours=24, exp=EXP_A, now=NOW)
    # 7 条全表 → 窗口只 5 条：窗口前 1 秒的和 2999 年的都必须排除
    assert snap["n_calls"] == 5
    assert snap["n_full_table"] == 7
    # n=5 ≠ 全表 7：把全表数当窗口数会在这里立刻穿帮
    assert snap["n_calls"] != snap["n_full_table"]
    # 边界：== since 的行算进窗口（>=）；失败原因也只许来自窗口内
    assert "HTTP 429" not in json.dumps(snap["errors_top"], ensure_ascii=False)
    assert snap["errors_top"]["items"] == [
        {"error": "timed out", "n": 1,
         "purposes": ["reconstruct:L"], "models": ["mB"]}]


def test_boundary_second_is_inclusive():
    # since 与 now 两个边界秒的行都在窗口内（>= 与 <=）
    snap = _snap(hours=24, exp=EXP_A, now=NOW)
    hours = {h["hour"]: h["calls"] for h in snap["by_hour"]}
    assert hours["2026-09-18T12"] == 1   # == since 的行
    assert hours["2026-09-19T12"] == 1   # == now 的行


def test_hours_must_be_positive():
    with pytest.raises(ValueError):
        _snap(hours=0, exp=EXP_A, now=NOW)


def test_unknown_experiment_raises_not_silent():
    with pytest.raises(ValueError, match="OBS-T14-NOPE"):
        _snap(hours=24, exp=EXP_NOPE, now=NOW)


# ── ② 聚合口径：成功率 / token / 分位数 ────────────────────────

def test_success_rate_tokens_and_percentiles():
    snap = _snap(hours=24, exp=EXP_A, now=NOW)
    assert snap["ok"] == 4 and snap["failed"] == 1
    assert snap["success_rate"] == pytest.approx(0.8)
    assert snap["tokens_in"] == 317
    assert snap["tokens_out"] == 158
    assert snap["tokens_total"] == 475
    # lats = [40,60,100,200,300]：p50=100，p95 线性插值 = 200*0.2+300*0.8=280
    assert snap["latency_ms"]["p50"] == pytest.approx(100.0)
    assert snap["latency_ms"]["p95"] == pytest.approx(280.0)


def test_percentile_definitions():
    # 线性插值口径的具体数字：[10,20,30,40] → p50=25.0、p95=38.5
    assert observability._pct([10, 20, 30, 40], 50) == pytest.approx(25.0)
    assert observability._pct([10, 20, 30, 40], 95) == pytest.approx(38.5)
    assert observability._pct([100], 50) == 100.0
    assert observability._pct([], 50) is None


def test_by_model_and_by_purpose_breakdown():
    snap = _snap(hours=24, exp=EXP_A, now=NOW)
    assert snap["by_model"]["mA"]["n"] == 3
    assert snap["by_model"]["mB"]["failed"] == 1
    assert snap["by_model"]["mB"]["success_rate"] == pytest.approx(0.5)
    # mC 的两条（窗口前 + 未来）都不能出现
    assert "mC" not in snap["by_model"]
    assert snap["by_purpose"]["judge_preference"]["n"] == 2
    assert snap["by_purpose"]["judge_preference"]["tokens_total"] == 165
    assert snap["by_status"]["ok"]["n"] == 4


def test_hourly_buckets_zero_filled():
    snap = _snap(hours=24, exp=EXP_A, now=NOW)
    hours = snap["by_hour"]
    # 24h 窗口 → 25 个小时桶（首尾两小时各算一桶），缺数据的桶显式 n=0
    assert len(hours) == 25
    assert hours[0]["hour"] == "2026-09-18T12"
    zeroed = [h for h in hours if h.get("n_zero")]
    assert len(zeroed) == 20  # 25 桶里 5 桶有数
    by_h = {h["hour"]: h for h in hours}
    assert by_h["2026-09-19T10"]["calls"] == 1
    assert by_h["2026-09-19T10"]["failed"] == 1
    assert by_h["2026-09-19T11"]["tokens"] == 150


# ── ③ 窗口内 0 条：显式 n=0，不回退全表 ────────────────────────

def test_empty_window_marks_n_zero_no_full_table_fallback():
    snap = _snap(hours=24, exp=EXP_B, now=NOW)
    assert snap["n_calls"] == 0
    assert snap["n_zero"] is True            # 硬规则：显式标注
    assert snap["window_empty"] is True
    assert "n=0" in snap["note"]
    # 全表有 1 条，但窗口数必须是 0，不是全表数
    assert snap["n_full_table"] == 1 and snap["n_calls"] != snap["n_full_table"]
    # 空窗口下所有统计都是「无数据」而不是 0 冒充
    assert snap["success_rate"] is None
    assert snap["latency_ms"]["p50"] is None
    assert snap["by_purpose"] == {}
    assert snap["errors_top"]["items"] == []
    # 小时桶照常铺零，每桶显式 n=0
    assert len(snap["by_hour"]) == 25
    assert all(h["calls"] == 0 and h.get("n_zero") for h in snap["by_hour"])


def test_empty_window_md_says_n_zero():
    snap = _snap(hours=24, exp=EXP_B, now=NOW)
    # scripts 目录不是包，用 importlib 按文件路径加载 render_md 做纯渲染断言
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "obs_report", ROOT / "scripts" / "observability_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    md = mod.render_md(snap)
    assert "n=0" in md
    assert "⚠" in md


# ── ④ 每实验消耗排行 ─────────────────────────────────────────

def test_experiments_ranking():
    snap = _snap(hours=24, exp=EXP_A, now=NOW)
    top = snap["experiments"]["top"]
    assert [b["experiment_id"] for b in top] == [EXP_A]
    assert top[0]["tokens_total"] == 475 and top[0]["n"] == 5
    assert snap["experiments"]["n_experiments"] == 1


# ── ⑤ GET /llm/stats 扩展（旧字段兼容，只做加法） ─────────────

def test_api_llm_stats_keeps_old_fields_and_adds_window():
    client = TestClient(app)
    r = client.get("/llm/stats")
    assert r.status_code == 200
    body = r.json()
    # 旧契约：mode + by_purpose[].{calls,tokens,failed}（控制台 loadUsage 只读它）
    assert "mode" in body
    assert isinstance(body["by_purpose"], dict)
    assert body["by_purpose"], "全表 by_purpose 不应为空（库里有其他测试的调用）"
    some = next(iter(body["by_purpose"].values()))
    assert {"calls", "tokens", "failed"} <= set(some)
    # 新增：window 块
    w = body["window"]
    assert w["hours"] == 24 and "n_calls" in w and "n_full_table" in w
    assert "by_model" in w and "by_hour" in w and "errors_top" in w


def test_api_llm_stats_unknown_exp_is_404():
    client = TestClient(app)
    r = client.get("/llm/stats", params={"exp": EXP_NOPE})
    assert r.status_code == 404
    assert "OBS-T14-NOPE" in r.json()["detail"]


def test_api_llm_stats_bad_hours_is_400():
    client = TestClient(app)
    assert client.get("/llm/stats", params={"hours": 0}).status_code == 400
    assert client.get("/llm/stats", params={"hours": 10000}).status_code == 400


# ── ⑥ CLI 子进程直跑 ─────────────────────────────────────────

def _cli(*extra: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "observability_report.py"),
         *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, env=env, cwd=str(ROOT))


def test_cli_json_and_md_smoke():
    r = _cli("--hours", "168", "--exp", EXP_A, "--json")
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout)
    assert body["exp"] == EXP_A and body["hours"] == 168
    assert "by_model" in body and "errors_top" in body
    # 全表计数只跟实验走（确定性）：EXP_A 恒 7 条；2999 年那行永远进不了窗口，
    # 所以窗口数必须严格小于全表数——ISO 字符串比较在 CLI 里也成立
    assert body["n_full_table"] == 7
    assert 1 <= body["n_calls"] < body["n_full_table"]
    # --md：markdown 表里有真实行
    r = _cli("--hours", "168", "--exp", EXP_A, "--md")
    assert r.returncode == 0, r.stderr
    assert "| 模型 |" in r.stdout and "mA" in r.stdout
    assert "n=" in r.stdout


def test_cli_unknown_exp_exits_1():
    r = _cli("--hours", "24", "--exp", EXP_NOPE)
    assert r.returncode == 1
    assert "OBS-T14-NOPE" in r.stderr
