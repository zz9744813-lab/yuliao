"""可观测聚合层（总方案 §50 任务 14）——纯函数，无副作用。

只做聚合：读 llm_calls，返回 dict；不写库、不 commit、不发网络调用、
不碰全局状态（Session 由调用方传入并负责关闭）。

口径与坑（都是实测过的）：
· llm_calls.created_at 是 **ISO 字符串**（2026-09-18T23:20:10Z），不是毫秒时间戳。
  时间窗过滤必须按**字符串比较**（同格式定长，字典序即时间序），在 SQL 层做，
  不许在 Python 里当成数字解析——当成毫秒解析会让整个窗口判断退化成全表或全空。
· status 只有 ok / failed 两种（app/gateway.py 落库口径，全库 22667 行实测）。
· cost 恒为 None：中转站单价未知（见 gateway.py 头注），本层只如实汇报 None，
  不做任何价格假设。
· provider 没有单独列，model 即 provider 代理（中转站按 model 路由）。
· purpose 即工作流阶段（plan/extract/judge_*/benchmark/...）。

硬规则（总方案验收 + 纪律④）：任何聚合在**窗口内 0 条**时必须显式标注
n=0（n_zero=True），不许把全表数当窗口数、不许静默返回空；
experiment_id 在 llm_calls 里一条都查不到时直接 raise（调用方转 404/exit 1）。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import LlmCall

DEFAULT_HOURS = 24
DEFAULT_TOP_ERRORS = 10
DEFAULT_TOP_EXPERIMENTS = 20
_ERROR_HEAD = 120  # 失败原因归一化截断长度（error 是自由文本，长堆栈会淹没 Top-N）
_NO_EXP_LABEL = "(未归属)"  # experiment_id 为 NULL 的调用（脚本/手搓调用不走实验）


def _now_iso() -> str:
    """与 models.LlmCall 落库格式一致：UTC 秒精度 + Z 后缀。"""
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _parse_iso(ts: str) -> datetime:
    """只认本项目落库格式 YYYY-MM-DDTHH:MM:SSZ；别的格式直接报错，不许猜。"""
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def _pct(sorted_vals: list[int], p: float) -> float | None:
    """线性插值百分位（与 numpy 默认 linear 法一致）。

    [10,20,30,40] → p50=25.0、p95=38.5；空表返回 None（n=0 时成功率和
    延迟分位一律 None，不用 0 冒充）。
    """
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _block(rows: list[LlmCall]) -> dict[str, Any]:
    """一个分组键的统计块。所有块都带 n；窗口内 0 条时 n_zero=True 显式标出。"""
    n = len(rows)
    ok = sum(1 for r in rows if r.status == "ok")
    lats = sorted(r.latency_ms or 0 for r in rows)
    b: dict[str, Any] = {
        "n": n,
        "ok": ok,
        "failed": n - ok,
        "success_rate": round(ok / n, 4) if n else None,
        "tokens_in": sum(r.tokens_in or 0 for r in rows),
        "tokens_out": sum(r.tokens_out or 0 for r in rows),
        "latency_ms": {
            "n": n,
            "p50": round(_pct(lats, 50), 1) if n else None,
            "p95": round(_pct(lats, 95), 1) if n else None,
            "max": lats[-1] if n else None,
        },
    }
    b["tokens_total"] = b["tokens_in"] + b["tokens_out"]
    if n == 0:
        b["n_zero"] = True  # 硬规则：窗口内 0 条必须显式标注，不许静默
    return b


def _norm_error(err: str | None) -> str:
    """失败原因归一化：去空白、截前 120 字符；空 error 也不许静默归零。"""
    e = (err or "").strip()
    return e[:_ERROR_HEAD] if e else "(空 error 文本)"


def _hour_keys(since: str, now: str) -> list[str]:
    """窗口覆盖的小时桶（YYYY-MM-DDTHH），含两端，供零填充。"""
    start = _parse_iso(since).replace(minute=0, second=0)
    end = _parse_iso(now)
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%dT%H"))
        cur = cur + timedelta(hours=1)
    return out


def experiment_known(s: Session, exp: str) -> bool:
    """llm_calls 全表（无时间窗）里是否存在该 experiment_id。"""
    return s.query(func.count(LlmCall.id)) \
            .filter(LlmCall.experiment_id == exp).scalar() > 0


def snapshot(s: Session, hours: int = DEFAULT_HOURS, exp: str | None = None,
             now: str | None = None, top_errors: int = DEFAULT_TOP_ERRORS,
             top_experiments: int = DEFAULT_TOP_EXPERIMENTS) -> dict[str, Any]:
    """近 hours 小时的 llm_calls 全景聚合。exp 给定时只看该实验。

    纯函数：不改库、不 commit。now 参数留给测试固定时间窗，生产走真实 UTC。
    """
    if hours < 1:
        raise ValueError(f"hours 必须 ≥ 1，收到 {hours}")
    if exp is not None and not experiment_known(s, exp):
        # 纪律④：查无此实验直接报错；「实验存在但窗口内 0 条」才是合法的 n=0
        raise ValueError(f"llm_calls 里查无 experiment_id={exp}（全表无此实验的调用）")

    now = now or _now_iso()
    since = (_parse_iso(now) - timedelta(hours=hours)) \
        .isoformat(timespec="seconds") + "Z"

    # 时间窗在 SQL 层按字符串比较过滤（见模块头注），不许拉全表到 Python 再筛
    q = s.query(LlmCall).filter(LlmCall.created_at >= since,
                                LlmCall.created_at <= now)
    if exp is not None:
        q = q.filter(LlmCall.experiment_id == exp)
    rows: list[LlmCall] = q.all()

    # 对照组：同样 exp 过滤但**无时间窗**的全表行数。窗口数与全表数并列展示，
    # 谁想把全表数当窗口数，一眼就能对出来。
    q_full = s.query(func.count(LlmCall.id))
    if exp is not None:
        q_full = q_full.filter(LlmCall.experiment_id == exp)
    n_full_table = q_full.scalar() or 0

    out: dict[str, Any] = {
        "hours": hours,
        "now": now,
        "since": since,
        "exp": exp,
        "n_calls": len(rows),
        "n_full_table": n_full_table,
        **_block(rows),
    }
    if len(rows) == 0 and n_full_table > 0:
        # 全表有数、窗口没数：明说，不给全表数冒充窗口数留任何空间
        out["window_empty"] = True
        out["note"] = f"窗口内 0 条（n=0）；全表（无时间窗）共 {n_full_table} 条"

    by_model: dict[str, dict] = {}
    by_purpose: dict[str, dict] = {}
    by_status: dict[str, dict] = {}
    by_hour: dict[str, dict] = {h: [] for h in _hour_keys(since, now)}  # 先铺零桶
    err_groups: dict[str, list[LlmCall]] = {}
    exp_groups: dict[str, list[LlmCall]] = {}
    for r in rows:
        by_model[r.model] = by_model.get(r.model, []) + [r]
        by_purpose[r.purpose] = by_purpose.get(r.purpose, []) + [r]
        by_status[r.status] = by_status.get(r.status, []) + [r]
        hkey = (r.created_at or "")[:13]
        if hkey in by_hour:
            by_hour[hkey].append(r)
        if r.status != "ok":
            err_groups.setdefault(_norm_error(r.error), []).append(r)
        exp_groups[r.experiment_id or _NO_EXP_LABEL] = \
            exp_groups.get(r.experiment_id or _NO_EXP_LABEL, []) + [r]

    out["by_model"] = {k: _block(v) for k, v in sorted(by_model.items())}
    out["by_purpose"] = {k: _block(v) for k, v in sorted(by_purpose.items())}
    out["by_status"] = {k: _block(v) for k, v in sorted(by_status.items())}
    out["by_hour"] = [
        {"hour": h, "n": len(by_hour[h]),
         "calls": len(by_hour[h]),
         "failed": sum(1 for r in by_hour[h] if r.status != "ok"),
         "tokens": sum((r.tokens_in or 0) + (r.tokens_out or 0) for r in by_hour[h]),
         **({"n_zero": True} if not by_hour[h] else {})}
        for h in sorted(by_hour)  # 零桶也输出：0 条的小时显式 n=0，不留空窗错觉
    ]

    err_items = sorted(
        ({"error": e, "n": len(rs),
          "purposes": sorted({r.purpose for r in rs})[:5],
          "models": sorted({r.model for r in rs})[:5]}
         for e, rs in err_groups.items()),
        key=lambda d: (-d["n"], d["error"]))
    out["errors_top"] = {
        "top_n": top_errors,
        "n_failed_rows": sum(1 for r in rows if r.status != "ok"),
        "n_distinct_errors": len(err_items),
        "items": err_items[:top_errors],
        **({"truncated": True} if len(err_items) > top_errors else {}),
    }

    exp_items = sorted(
        ({"experiment_id": k, **_block(v)}
         for k, v in exp_groups.items()),
        key=lambda d: (-d["tokens_total"], -d["n"], d["experiment_id"]))
    out["experiments"] = {
        "n_experiments": len(exp_items),
        "top_n": top_experiments,
        "top": exp_items[:top_experiments],
        **({"truncated": True} if len(exp_items) > top_experiments else {}),
    }
    return out
