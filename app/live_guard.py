"""live / pytest 并发窗互斥守卫（会审 89f779e R6 待办落地，2026-09-23；
同日残留窗口收口：pytest 侧从「单向瞬时检查」升级为「收集期取锁、整轮
持有」，审计 P1 余项）。

背景（已入 HANDOVER 的待办）：2026-09-22 22:5x 一次全量 pytest 出现复现
不出的瞬态红——事后发现与主控 live 实跑同机并发时间窗重合；R6 判定=
「live 实跑与全量 pytest 禁止同机并发窗重叠，守卫机制待落」。本模块即
该守卫：live_run.lock 锁文件互斥。

**两侧看同一把锁（9e02916 会审严重项修正；2026-09-23 审计 P1 收口：路径
由 app/config.py 单点导出）**：
- 锁路径协议唯一定义在 app/config.py（LOCK_FILE_NAME / prod_lock_path() /
  live_lock_path()，LG_LOCK_DIR 为两侧同时跟随的隔离覆盖旋钮）——本模块与
  conftest 只引用，不各自拼路径；
- live 侧（k2_extract_backfill / k4_paired_scenes / pool_probe 的 --live）：
  持锁于 config.live_lock_path()（生产默认=仓库 data/）；
- pytest 侧（tests/conftest.py）：**盯生产锁位** prod_lock_path()——conftest
  会把测试库 DATA_DIR 覆写成临时目录，若盯 config.DATA_DIR 则恰好看不到
  生产 live 的锁（守卫对要防的场景盲视，即 P1 错位）。两侧口径由
  test_live_guard / test_live_guard_mutex 的路径不变式钉死。
- **pytest 侧整轮持锁（2026-09-23 审计 P1 收口）**：conftest 在
  pytest_configure 先检锁（refuse_if_live_running，live 持锁则本次拒跑且
  不持锁），取锁成功则在生产锁位持有 purpose="pytest" 的锁**整轮**，
  pytest_sessionfinish 释放（atexit 兜底异常路径；释放回读比对 pid+
  purpose，只删自己的锁）。此前只查不持——检查通过后、全量 pytest 跑完前，
  另一个 --live 仍可启动并与之重叠，无人拦截——已修。

边界（如实声明，逐条与代码对应）：
- 已覆盖：live↔live（同一锁位 O_EXCL 原子互斥）；live↔pytest **两方
  向**跨进程互斥（2026-09-23 由 tests/test_live_guard_mutex.py 子进程钉
  死）——pytest 持锁期间 live 进入 O_EXCL 必失败被拒；live 持锁期间
  pytest 在 pytest_configure fail-fast 拒跑。
- 残留窄窗 1（pytest 启动前）：pytest 进程启动到 pytest_configure 持锁之
  间存在窄窗。互斥本身不破——谁先到谁拿锁、后到方被拒；但若 live 先拿
  锁，pytest 本次启动作废（configure 期 SystemExit），需重排。
- 残留窄窗 2：自定义 LG_DATA_DIR 的 live 进程锁在别处，不在 pytest 侧
  观察窗内（pytest 盯生产锁位）。
- 释放边界：atexit 覆盖正常/异常退出与键盘中断；**进程被硬杀**
  （SIGKILL/断电）锁无法自释放，留下含 pid 的完整死锁，不自动清（误清
  会把真跑叠上去），拒绝信息给出锁路径，人工核实后清除（纪律：宁可
  拦，不可猜）。
- 损坏锁处置口径（2026-09-23 收口选定）：锁文件存在但内容不可解析
  （空文件/半截 JSON，典型于 O_EXCL 建文件成功与 json.dump 写完之间进
  程被杀）——宽限期 CORRUPT_LOCK_GRACE_SECONDS（15s）内**按「存在」
  拦**（宁可拦，不可猜：可能是有进程正在写内容）；超过宽限仍不可解析
  → 判定为崩溃残留，记 warning 并**按「不存在」处理**、可被接管（接
  管方 unlink 后 O_EXCL 重建）。理由：健康进程建文件后毫秒级写完内
  容，15s 宽限远大于之，误接管概率可忽略；而损坏锁无 pid 可核，若一
  律硬拦，一个空文件就能锁死所有 live 与整个 pytest 套件且只能人工
  清——两害相权取「可自愈的边界」。

其他语义：
- 锁内容 = {"purpose","pid","started_at"}——诊断可读；purpose 区分
  各 live 入口名与 "pytest"；
- 释放保守：回读锁内容比对 pid+purpose，只删自己的锁——运行中锁被人
  工清除、他人接管时，前者的退出不得删掉后者的锁（9e02916 会审一般项）；
- 锁创建用 O_EXCL 原子语义——两个进入者同时起跑也只有一个能拿到。
"""
from __future__ import annotations

import datetime
import json
import os
import time
import warnings
from contextlib import contextmanager
from pathlib import Path

# 损坏锁自愈宽限（秒）：健康进程 O_EXCL 建文件后毫秒级写完内容；超过
# 宽限仍不可解析 → 判定为「建文件后被杀」的崩溃残留，可自愈接管。
CORRUPT_LOCK_GRACE_SECONDS = 15.0

# pytest 侧持锁的 purpose 标识（conftest 整轮持锁；live 侧拒绝信息可见）
PYTEST_LOCK_PURPOSE = "pytest"


def lock_path() -> Path:
    """live 进程口径：锁位由 app.config 单点导出（live_lock_path()——本进程
    config.DATA_DIR 下；LG_LOCK_DIR 显式覆盖时优先）——本模块不再自拼路径。"""
    from app import config
    return config.live_lock_path()


def prod_lock_path() -> Path:
    """生产锁位（pytest 侧观察/整轮持锁目标）——由 app.config.prod_lock_path()
    单点导出：默认 repo/data/live_run.lock，不随测试进程覆写的 LG_DATA_DIR
    漂移（2026-09-23 审计 P1「路径错位」的钉法）；LG_LOCK_DIR 显式覆盖时
    两侧（本函数与 lock_path()）同时跟随，协议测试据此隔离到临时目录。"""
    from app import config
    return config.prod_lock_path()


def _info_at(p: Path) -> dict | None:
    """锁存在 → 内容（损坏锁也按存在处理）；不存在 → None。"""
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                       # noqa: BLE001
        return {"corrupt_lock": True, "path": str(p)}


def _corrupt_and_stale(p: Path, info: dict | None) -> bool:
    """损坏锁（不可解析、无 pid 可核）且 mtime 超过自愈宽限 → 崩溃残留。"""
    if not info or not info.get("corrupt_lock"):
        return False
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return False
    return age > CORRUPT_LOCK_GRACE_SECONDS


def _warn_corrupt_stale(p: Path, held: dict) -> None:
    warnings.warn(
        f"[live/pytest 互斥守卫] 损坏锁超自愈宽限"
        f"{CORRUPT_LOCK_GRACE_SECONDS:g}s 仍不可解析（无 pid 可核）——"
        f"按崩溃残留处理（原内容：{held}）：{p}", stacklevel=2)


def _acquire_lock(p: Path, info: dict) -> None:
    """在 p 处 O_EXCL 原子取锁；「损坏且超宽限」的残留锁自愈接管一次。

    被他人持有（含宽限内的损坏锁）→ SystemExit 拒绝，不排队。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = _info_at(p)
            if attempt == 0 and _corrupt_and_stale(p, held):
                _warn_corrupt_stale(p, held)   # 自愈接管：unlink 后重试一次
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise SystemExit(
                f"[live 互斥守卫] 已有 live 实跑/pytest 持锁（{held}）——拒绝重叠"
                f"（R6：live 不与 live/pytest 并发）；锁：{p}")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        return


def _release_lock(p: Path, purpose: str) -> None:
    """保守释放：回读锁内容比对 pid+purpose，只删自己的锁——运行中锁被
    人工清除、他人接管时，本实例退出不得删掉别人的锁。"""
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return                                # 锁已被人工清除——无事可做
    except Exception:                         # noqa: BLE001 —— 损坏：不删（宁拦不猜）
        return
    if info.get("pid") == os.getpid() and info.get("purpose") == purpose:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def live_run_active() -> dict | None:
    return _info_at(lock_path())


class live_lock:
    """live 入口的互斥上下文：进入即持锁（O_EXCL 原子创建），退出即释放。

    用法：`with live_lock("k2_extract_backfill"): ...真跑...`
    锁被他人持有（live 实跑或 pytest 整轮持锁）→ 进入时即 SystemExit
    （拒绝重叠，不排队——排队会造成预算/超时语义不可控，宁可拒绝让调
    用方重排）。"""

    def __init__(self, purpose: str):
        self.purpose = purpose
        self._path: Path | None = None

    def __enter__(self):
        p = lock_path()
        info = {"purpose": self.purpose, "pid": os.getpid(),
                "started_at": datetime.datetime.now().isoformat(timespec="seconds")}
        _acquire_lock(p, info)
        self._path = p
        return self

    def __exit__(self, *exc):
        if self._path is None:
            return False
        _release_lock(self._path, self.purpose)
        return False


@contextmanager
def whole_run_lock(context: str = "全量 pytest", *, watch: Path | None = None):
    """pytest 侧的整轮持锁窗（tests/conftest.py 用）。

    命名注意：conftest 命名空间里所有 `pytest_*` 开头的名字会被 pluggy
    当作钩子校验（非注册钩子 → PluginValidationError，整轮 pytest 直接
    INTERNALERROR）——故本函数**不得**以 `pytest_` 开头命名（上一版命名
    `pytest_running_lock` 即踩此坑，exit 3）。

    先检 live 锁（fail-fast 拒跑），再在同一锁位获取 purpose="pytest"
    的锁持有到 yield 结束——live 侧 O_EXCL 同一锁位必失败被拒，两方向
    互斥闭环。释放口径与 live 相同：回读 pid+purpose 只删自己的锁。
    conftest 在 pytest_configure 进入、pytest_sessionfinish 退出（atexit
    兜底）。watch 缺省=**生产锁位** prod_lock_path()（app.config 单点导出，
    不随 conftest 覆写的测试 DATA_DIR 漂移）；测试隔离时显式传 watch 或
    设 LG_LOCK_DIR，绝不写真实生产锁位。"""
    p = prod_lock_path() if watch is None else watch
    refuse_if_live_running(context, watch=p)
    info = {"purpose": PYTEST_LOCK_PURPOSE, "pid": os.getpid(),
            "started_at": datetime.datetime.now().isoformat(timespec="seconds")}
    _acquire_lock(p, info)
    try:
        yield p
    finally:
        _release_lock(p, PYTEST_LOCK_PURPOSE)


def refuse_if_live_running(context: str, *, watch: Path | None = None) -> None:
    """pytest / 批处理入口的检锁 fail-fast。

    watch 缺省=**生产锁位** prod_lock_path()（pytest 侧口径：conftest 已把
    测试 DATA_DIR 覆写成临时目录，盯 config.DATA_DIR 会看不到生产 live）；
    live 类入口复检自身锁位时传 watch=lock_path()。
    损坏锁：宽限内按「存在」拦（宁可拦不可猜）；超宽限判定为崩溃残留，
    记 warning 并按「不存在」放行（自愈接管由随后的取锁完成）。"""
    p = prod_lock_path() if watch is None else watch
    held = _info_at(p)
    if held is None:
        return
    if _corrupt_and_stale(p, held):
        _warn_corrupt_stale(p, held)
        return
    raise SystemExit(
        f"[live/pytest 互斥守卫] live 实跑进行中（{held}）——拒绝 {context}"
        f"（R6：全绿结论不许被并发 live 污染）。等 live 结束；若 live "
        f"已崩溃遗留死锁，人工核实后清除：{p}")
