"""live / pytest 并发窗互斥守卫（会审 89f779e R6 待办落地，2026-09-23）。

背景（已入 HANDOVER 的待办）：2026-09-22 22:5x 一次全量 pytest 出现复现
不出的瞬态红——事后发现与主控 live 实跑同机并发时间窗重合；R6 判定=
「live 实跑与全量 pytest 禁止同机并发窗重叠，守卫机制待落」。本模块即
该守卫：DATA_DIR/live_run.lock 锁文件互斥。

方向与语义：
- **live 入口**（k2_extract_backfill --live / k4_paired_scenes --live）：
  开跑前 acquire——锁已存在 → 拒绝（不重叠跑 live）；跑完 finally 释放；
- **pytest**（tests/conftest.py）：收集前检查——锁存在 → fail-fast 拒跑
  （全绿结论不允许被并发 live 污染）；
- 锁内容 = {"purpose","pid","started_at"}——诊断可读；**进程崩溃可能留
  死锁**：不自动清（误清会把真跑叠上去），拒绝信息给出锁路径，人工核
  实后清除（纪律：宁可拦，不可猜）。

零依赖纯文件锁：锁创建用 O_EXCL 原子语义——两个 live 同时起跑也只有一个
能拿到（竞态窗口不存在）。
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path


def lock_path() -> Path:
    from app import config
    return Path(config.DATA_DIR) / "live_run.lock"


def live_run_active() -> dict | None:
    """锁存在 → 返回锁内容（诊断用）；不存在 → None。"""
    p = lock_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                       # noqa: BLE001 —— 损坏锁也按「存在」处理
        return {"corrupt_lock": True, "path": str(p)}


class live_lock:
    """live 入口的互斥上下文：进入即持锁（O_EXCL 原子创建），退出即释放。

    用法：`with live_lock("k2_extract_backfill"): ...真跑...`
    锁被他人持有 → 进 入 时即 SystemExit（拒绝重叠，不排队——排队会造成
    预算/超时语义不可控，宁可拒绝让调用方重排）。"""

    def __init__(self, purpose: str):
        self.purpose = purpose
        self._path: Path | None = None

    def __enter__(self):
        p = lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        info = {"purpose": self.purpose, "pid": os.getpid(),
                "started_at": datetime.datetime.now().isoformat(timespec="seconds")}
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = live_run_active()
            raise SystemExit(
                f"[live 互斥守卫] 已有 live 实跑持锁（{held}）——拒绝重叠"
                f"（R6：live 不与 live/pytest 并发）；锁：{p}")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        self._path = p
        return self

    def __exit__(self, *exc):
        if self._path is not None:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass                        # 人工已清/外部清——不报错不猜
        return False


def refuse_if_live_running(context: str) -> None:
    """给 pytest / 批处理入口用的检锁 fail-fast：锁在 → SystemExit。"""
    held = live_run_active()
    if held:
        p = lock_path()
        raise SystemExit(
            f"[live/pytest 互斥守卫] live 实跑进行中（{held}）——拒绝 {context}"
            f"（R6：全绿结论不许被并发 live 污染）。等 live 结束；若 live "
            f"已崩溃遗留死锁，人工核实后清除：{p}")
