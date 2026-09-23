"""live / pytest 并发窗互斥守卫（会审 89f779e R6 待办落地，2026-09-23）。

背景（已入 HANDOVER 的待办）：2026-09-22 22:5x 一次全量 pytest 出现复现
不出的瞬态红——事后发现与主控 live 实跑同机并发时间窗重合；R6 判定=
「live 实跑与全量 pytest 禁止同机并发窗重叠，守卫机制待落」。本模块即
该守卫：DATA_DIR/live_run.lock 锁文件互斥。

**两侧看同一把锁（9e02916 会审严重项修正）**：
- live 侧（k2_extract_backfill / k4_paired_scenes / pool_probe 的 --live）：
  持锁于本进程 config.DATA_DIR（生产默认=仓库 data/）；
- pytest 侧（tests/conftest.py）：**显式盯生产锁位** prod_lock_path()
  （仓库 data/live_run.lock）——conftest 会把测试库 DATA_DIR 覆写成临时
  目录，若盯 config.DATA_DIR 则恰好看不到生产 live 的锁（守卫对要防的
  场景盲视）。两侧口径由 test_live_guard 的路径不变式钉死。
- 边界（如实声明）：本守卫覆盖**默认生产 DATA_DIR** 的 live；自定义
  LG_DATA_DIR 的 live 进程锁在别处，不在 pytest 侧观察窗内。

其他语义：
- 锁内容 = {"purpose","pid","started_at"}——诊断可读；**进程崩溃可能留
  死锁**：不自动清（误清会把真跑叠上去），拒绝信息给出锁路径，人工核
  实后清除（纪律：宁可拦，不可猜）；
- 释放保守：__exit__ 回读锁内容比对 pid+purpose，只删自己的锁——运行中
  锁被人工清除、他人接管时，前者的退出不得删掉后者的锁；
- 锁创建用 O_EXCL 原子语义——两个 live 同时起跑也只有一个能拿到；
- 损坏锁按「存在」处理（宁可拦，不可猜）。
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path


def lock_path() -> Path:
    """live 进程口径：本进程 config.DATA_DIR 下的锁位。"""
    from app import config
    return Path(config.DATA_DIR) / "live_run.lock"


def prod_lock_path() -> Path:
    """生产锁位（仓库默认 DATA_DIR=data/）——pytest 侧的观察目标。"""
    return Path(__file__).resolve().parent.parent / "data" / "live_run.lock"


def _info_at(p: Path) -> dict | None:
    """锁存在 → 内容（损坏锁也按存在处理）；不存在 → None。"""
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                       # noqa: BLE001
        return {"corrupt_lock": True, "path": str(p)}


def live_run_active() -> dict | None:
    return _info_at(lock_path())


class live_lock:
    """live 入口的互斥上下文：进入即持锁（O_EXCL 原子创建），退出即释放。

    用法：`with live_lock("k2_extract_backfill"): ...真跑...`
    锁被他人持有 → 进入时即 SystemExit（拒绝重叠，不排队——排队会造成
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
            held = _info_at(p)
            raise SystemExit(
                f"[live 互斥守卫] 已有 live 实跑持锁（{held}）——拒绝重叠"
                f"（R6：live 不与 live/pytest 并发）；锁：{p}")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        self._path = p
        return self

    def __exit__(self, *exc):
        if self._path is None:
            return False
        try:
            info = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False                    # 锁已被人工清除——无事可做
        except Exception:                   # noqa: BLE001 —— 损坏：不删（宁拦不猜）
            return False
        # 保守释放：只删**自己的**锁——运行中被人工清锁、他人接管时，
        # 本实例退出不得删掉别人的锁（9e02916 会审一般项）
        if info.get("pid") == os.getpid() and info.get("purpose") == self.purpose:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
        return False


def refuse_if_live_running(context: str, *, watch: Path | None = None) -> None:
    """pytest / 批处理入口的检锁 fail-fast。

    watch 缺省=**生产锁位** prod_lock_path()（pytest 侧口径：conftest 已把
    测试 DATA_DIR 覆写成临时目录，盯 config.DATA_DIR 会看不到生产 live）；
    live 类入口复检自身锁位时传 watch=lock_path()。"""
    p = prod_lock_path() if watch is None else watch
    held = _info_at(p)
    if held:
        raise SystemExit(
            f"[live/pytest 互斥守卫] live 实跑进行中（{held}）——拒绝 {context}"
            f"（R6：全绿结论不许被并发 live 污染）。等 live 结束；若 live "
            f"已崩溃遗留死锁，人工核实后清除：{p}")
