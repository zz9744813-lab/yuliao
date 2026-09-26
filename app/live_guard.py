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
- 残留窄窗 2（2026-09-26 OPEN-3 收口，审查实验 e 已复现真实重叠）：自
  定义 LG_DATA_DIR 的 live 进程——若不同时显式设 LG_LOCK_DIR，其锁位与
  pytest 侧观察的生产锁位**分叉**，取锁入口 `_lock_scope_divergence`
  硬拦拒绝并给可执行处置指引（设 LG_LOCK_DIR 同口径 / 不设
  LG_DATA_DIR）；仅当显式设了 LG_LOCK_DIR（＝声明独立沙箱、两侧同跟随
  同一旋钮）才放行，此时不在 pytest 观察窗内属明示声明，不再是隐性分
  叉。pytest 进程本身豁免该判据（conftest 覆写 LG_DATA_DIR 是设计行为，
  该口径由路径不变式回归钉死）。
- 释放边界：atexit 覆盖正常/异常退出与键盘中断；**进程被硬杀**
  （SIGKILL/断电/任务管理器结束/taskkill /F）锁无法自释放，留下含
  pid 的完整死锁。处置（2026-09-25 事故收口，死 pid 自愈，见
  docs/live锁死pid自愈_20260925.md）：
  - 锁内容合法 + 锁内 pid 在本机进程表中**确定不存在** + 锁文件 mtime
    超过自愈宽限 CORRUPT_LOCK_GRACE_SECONDS → 判定为崩溃残留，记
    warning 并**自愈接管**（unlink 后 O_EXCL 重建/放行，与损坏锁超宽
    限同口径）——硬杀残留不再需要人工删锁；
  - pid 查询失败/权限不足/平台无法核验/pid 仍存在（含被系统复用给无
    关进程）→ 一律按「有人持有」硬拦，拒绝信息给出锁路径 **+ 可复制的
    `tasklist` 核查/清锁命令**（`_ops_dispose_hint`，2026-09-26 OPEN-6
    入册），人工核实后清除。纪律：宁可拦，不可猜——**只有 pid 确定不存
    在才自愈**，绝不放宽成“看着像残留就删”。
- 残留（2026-09-26 OPEN-6 入册，明写不掩盖）：硬杀 + 新鲜 mtime ⇒ 整套件
  fail-fast exit 2，持续到 mtime 超 15s 宽限（约 15s，自愈，**非永久**）；
  **pid 被系统复用给无关进程 ⇒ 永久砖化**（`_pid_looks_live` 只按「存在」
  判，不核验进程身份；取舍理由见 `_dead_pid_and_stale` docstring 的
  「pid 复用风险」段）。两条路径的唯一出口都是**人工处置**，故拒绝信息必
  须自带可执行命令而不只是锁路径——见 `_ops_dispose_hint` 与固定路径的运
  维 runbook `docs/R6守卫覆盖缺口入册第二批_20260926.md`。
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
  工清除、他人接管时，前者的退出不得删掉后者的锁（9e02916 会审一般项；
  pid 分支是**跨进程同 purpose** 场景的唯一防线——既有两条保守释放用例
  前任/继任同进程且 purpose 不同，承重验证走不到 pid 分支，2026-09-26
  OPEN-1 由 tests/test_live_guard_mutex.py test_open1_crossproc_* 跨进
  程钉死）；
- 锁创建用 O_EXCL 原子语义——两个进入者同时起跑也只有一个能拿到。
"""
from __future__ import annotations

import datetime
import errno
import json
import os
import sys
import time
import warnings
from contextlib import contextmanager
from pathlib import Path

# 损坏锁自愈宽限（秒）：健康进程 O_EXCL 建文件后毫秒级写完内容；超过
# 宽限仍不可解析 → 判定为「建文件后被杀」的崩溃残留，可自愈接管。
CORRUPT_LOCK_GRACE_SECONDS = 15.0

# pytest 侧持锁的 purpose 标识（conftest 整轮持锁；live 侧拒绝信息可见）
PYTEST_LOCK_PURPOSE = "pytest"

# 运维处置 runbook 的**固定文档路径**（2026-09-26 OPEN-6 入册）。拒绝信息里
# 的运维段直接指向它——「锁被拒了怎么办」不能只存在于某个 issue/PR 里。
OPS_RUNBOOK_DOC = "docs/R6守卫覆盖缺口入册第二批_20260926.md"


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


def _pid_looks_live(pid: int) -> bool:
    """pid 在本机进程表中是否（可能）存活。

    用标准库 os.kill(pid, 0) 做存在性核验（零信号不发信号，纯查进程表；
    POSIX 与 Windows 均支持，**不新增 psutil 依赖**）。只把**确定不存在**
    的 pid 判为死亡，其余一律按「存在」拦：
    - ProcessLookupError / Python 0 号信号查无进程 → 确定不存在（POSIX）；
    - Windows（OpenProcess 查 pid）：不存在的 pid → OSError errno=EINVAL
      (22) / winerror=87（ERROR_INVALID_PARAMETER）——实测本机死 pid 与
      越界 pid 均此表现，故按「确定不存在」处理；存在但受保护的系统 pid
      → ACCESS_DENIED(5)/EACCES → 按「存在」；
    - 查询失败/权限不足/平台不支持/pid 超长不可转译（OverflowError）/
      其它意外 → 一律 True（按存在拦，宁可拦，不可猜）。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as e:
        if e.errno == errno.EINVAL or getattr(e, "winerror", None) == 87:
            return False                       # Windows 死 pid（表外/已回收）
        return True                            # ACCESS_DENIED 等 → 存在/不可知
    except Exception:                          # noqa: BLE001 —— OverflowError 等
        return True
    return True


def _dead_pid_and_stale(p: Path, info: dict | None) -> bool:
    """合法 JSON 锁 + 锁内 pid **确定不存在** + 锁文件 mtime 超自愈宽限
    → 硬杀/断电崩溃残留（2026-09-25 事故收口）。

    防误判条件（与损坏锁超宽限**同口径**，二选一取 mtime 宽限）：
    - 条件一：pid 在本机进程表中确定不存在——「pid 不存在就不可能有持
      有者」，这是比损坏锁（无 pid 可核、只能靠 mtime 猜）更强的证据；
    - 条件二：锁文件 mtime 距现在超过 CORRUPT_LOCK_GRACE_SECONDS。新落
      盘的锁文件（宽限内）一律按「存在」拦——宁可拦不可猜，避免把刚写
      完的锁、或刚被人为重现的锁立刻当残留清掉；该宽限与损坏锁共用同
      一常量，判定行为完全同构。
    宽限取 mtime 而非 started_at：started_at 是写入方自报时间戳，可随意
    编造（如锁内写远古时间）且依赖解析；mtime 是文件系统事实，与损坏锁
    口径的取证完全一致。
    pid 复用风险（取舍后接受）：pid 被系统复用给无关进程 → 判定偏
    「存在」→ 只会更保守地拦（不删），绝不误删活锁；「pid 存在但不是
    我方进程」不另做核验（进程创建时间等需额外系统接口且各有精度问
    题，宁可不猜）。
    """
    if not info or info.get("corrupt_lock"):
        return False
    pid = info.get("pid")
    if not isinstance(pid, int):
        return False                           # 无 pid 可核 → 交给既有损坏锁口径
    try:
        if _pid_looks_live(pid):
            return False                       # pid 仍存在（含复用）→ 有人持有
    except Exception:                          # noqa: BLE001 —— 查询抛错 → 按存在
        return False
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return False                           # 文件消失/不可 stat → 不删
    return age > CORRUPT_LOCK_GRACE_SECONDS


def _warn_dead_pid_stale(p: Path, held: dict) -> None:
    warnings.warn(
        f"[live/pytest 互斥守卫] 死 pid 残留：锁内容合法但 pid "
        f"{held.get('pid')} 在本机进程表中已不存在、且锁文件超自愈宽限"
        f"{CORRUPT_LOCK_GRACE_SECONDS:g}s——判定为硬杀/断电崩溃残留，"
        f"可自愈接管（原内容：{held}）：{p}", stacklevel=2)


def _ops_dispose_hint(held: dict | None, p: Path) -> str:
    """拒绝信息里的**可复制**运维处置段（2026-09-26 OPEN-6 入册）。

    本函数**只产文案**，不读进程表、不动锁文件——互斥/取锁/释放语义零改动
    （OPEN-6 的收口判据只要求拒绝信息里出现可复制的 `tasklist` 提示）。

    为什么必须有它（两种「拒了但活不下去」的形态，都只能人工处置）：
    - 形态一（有限砖化）：硬杀 + mtime 尚在
      CORRUPT_LOCK_GRACE_SECONDS 内 → 整套件 fail-fast exit 2 约 15s，
      宽限一过自动自愈。行为符合设计，**不需要**人工干预，但运维在 15s
      内只会看到一条红色退出信息。
    - 形态二（**永久**砖化）：锁内 pid 已被系统复用给无关进程 ⇒
      `_pid_looks_live` 永远返回「存在」⇒ 永不自动自愈。既有文案只给锁路
      径，运维既不知道要看 pid、也不知道用什么命令确认，只能靠猜。
    故此处把「①怎么确认锁内 pid ② `tasklist` 怎么查该 pid ③确认无实跑后
    怎么清锁」三步直接写进拒绝信息，pid 与锁路径均已按本例实参代入——运维
    复制即可执行，不需要自己拼模板。

    纪律不改：文案只给**核查与清锁**步骤，「删前必须确认那不是实跑」写进
    ②③，宁可拦不可猜；本函数绝不替人删锁（真删除仍只在
    `_corrupt_and_stale`/`_dead_pid_and_stale` 双条件自愈路径里发生）。

    损坏锁（无 pid 可核）走另一条 ②——按 pid 过滤的命令在无 pid 时不可复
    制，给的是「列出候选进程人工确认」；两条分支都带字面量 `tasklist`。
    """
    pid = (held or {}).get("pid")
    if isinstance(pid, int):
        step12 = (
            f"\n ① 锁内 pid 就是本条消息里 held 的 pid 字段：{pid}。"
            f"\n ② 确认该 pid 此刻有没有被占用（Windows 首选，可直接复制）："
            f'tasklist /FI "PID eq {pid}" /NH'
            f"\n    （POSIX 备选：ps -p {pid} -o pid,cmd）"
            f"——无输出＝pid 确定不存在＝崩溃残留，"
            f"等 {CORRUPT_LOCK_GRACE_SECONDS:g}s 自愈宽限过后守卫会自愈接管、"
            f"一般无需手清；有输出但确认不是实跑（如 pid 被复用的无关进程，"
            f"此时**永不**自动自愈）→ 走 ③。"
        )
    else:
        step12 = (
            f"\n ① 锁内容不可解析（corrupt_lock），**无 pid 可核**——不满足"
            f"「死 pid」判据，只能人工判定；先用 ② 确认本机确无实跑在跑。"
            f"\n ② 列出候选进程人工确认（Windows，可直接复制）："
            f'tasklist /FI "IMAGENAME eq python.exe" /NH'
            f"\n    （POSIX 备选：ps -ef | grep python）"
            f"——确认无实跑进程后走 ③。"
        )
    step3 = (
        f"\n ③ 确认无人持锁后人工清除该锁文件再重跑（跨平台，可直接复制）："
        f'\n    "{sys.executable}" -c "import os;os.remove(r\'{p}\')"'
        f"\n    删前务必确认 ② 的结果里没有实跑进程——宁可拦，不可猜；"
        f"只删这一把锁文件，勿用任何递归/批量删除命令。"
    )
    return f"\n 运维处置（runbook：{OPS_RUNBOOK_DOC}）：{step12}{step3}"


def _acquire_lock(p: Path, info: dict) -> None:
    """在 p 处 O_EXCL 原子取锁；「损坏且超宽限」或「死 pid 且超宽限」的
    残留锁自愈接管一次。

    被他人持有（含宽限内的损坏/死 pid 锁）→ SystemExit 拒绝，不排队。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = _info_at(p)
            if attempt == 0 and (_corrupt_and_stale(p, held)
                                 or _dead_pid_and_stale(p, held)):
                if _corrupt_and_stale(p, held):
                    _warn_corrupt_stale(p, held)   # 损坏残留：unlink 后重试一次
                else:
                    _warn_dead_pid_stale(p, held)  # 死 pid 残留：unlink 后重试一次
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise SystemExit(
                f"[live 互斥守卫] 已有 live 实跑/pytest 持锁（{held}）——拒绝重叠"
                f"（R6：live 不与 live/pytest 并发）；锁：{p}"
                + _ops_dispose_hint(held, p))
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


def _lock_scope_divergence() -> str | None:
    """OPEN-3 硬拦判据（2026-09-26 入册；独立审查实验 e 已复现真实重叠）：
    检出「LG_DATA_DIR 显式设置而 LG_LOCK_DIR 未设」造成的锁位口径分叉。

    分叉机制：live 侧锁位=本进程 DATA_DIR（跟随 LG_DATA_DIR 漂移），
    pytest 侧观察/持锁位=生产锁位 ROOT/data（**不**随 LG_DATA_DIR 漂移，
    P1 钉法）——只拨前者这一个旋钮时两侧看的不是同一个文件，O_EXCL
    互斥对彼此失明，live 与全量 pytest 可真实并发（R6 违规）。

    处置：live 取锁入口（live_lock.__enter__）硬拦，给可执行指引（二
    选一）：设 LG_LOCK_DIR=<生产 data 目录>与生产锁位同口径恢复互斥；
    或不设 LG_DATA_DIR（数据与锁都回 ROOT/data）。确需整库独立沙箱
    （明知不与 pytest 并发）时显式设 LG_LOCK_DIR 到自己的目录＝声明隔
    离意图，两侧同跟随同一旋钮，不再分叉、放行。

    pytest 进程（"pytest"/"_pytest" 已加载）跳过：conftest 把
    LG_DATA_DIR 覆写成临时目录是**设计行为**（pytest 侧固定盯生产锁
    位），该口径的分叉已由 test_live_guard/test_live_guard_mutex 的
    路径不变式钉死，若在这里拦会把整个测试套件 lockout——危险场景只
    发生在真实 live 进程，驱动脚本从不 import pytest。
    """
    if "pytest" in sys.modules or "_pytest" in sys.modules:
        return None
    data_dir = (os.environ.get("LG_DATA_DIR") or "").strip()
    if not data_dir:
        return None                                   # 未设：锁位=ROOT/data，两侧同位
    if (os.environ.get("LG_LOCK_DIR") or "").strip():
        return None                                   # 显式声明锁目录：两侧同随同一旋钮
    from app import config
    prod_dir = os.path.join(str(config.ROOT), "data")
    if os.path.normcase(os.path.abspath(data_dir)) \
            == os.path.normcase(os.path.abspath(prod_dir)):
        return None                                   # LG_DATA_DIR 正指生产数据目录：不分叉
    return (
        f"[live 互斥守卫] 锁位口径分叉：LG_DATA_DIR={data_dir} 显式设置而 "
        f"LG_LOCK_DIR 未设——live 锁会落在 {Path(data_dir) / config.LOCK_FILE_NAME}，"
        f"而 pytest 侧观察/持有的是生产锁位 {Path(prod_dir) / config.LOCK_FILE_NAME}，"
        f"两把不是同一个文件，R6 互斥对分叉失明（审查实验 e 已复现真实重叠）。"
        f"处置（二选一）：设 LG_LOCK_DIR={prod_dir} 与生产锁位同口径；"
        f"或不设 LG_DATA_DIR（数据与锁都回 ROOT/data）。确需整库隔离独立"
        f"跑（明知不与 pytest 并发）时，显式设 LG_LOCK_DIR 到你的目录即声"
        f"明隔离意图、放行。")


class live_lock:
    """live 入口的互斥上下文：进入即持锁（O_EXCL 原子创建），退出即释放。

    用法：`with live_lock("k2_extract_backfill"): ...真跑...`
    锁被他人持有（live 实跑或 pytest 整轮持锁）→ 进入时即 SystemExit
    （拒绝重叠，不排队——排队会造成预算/超时语义不可控，宁可拒绝让调
    用方重排）。口径分叉（LG_DATA_DIR 显式设置而 LG_LOCK_DIR 未设，
    _lock_scope_divergence）同样进入即拒——2026-09-26 OPEN-3 硬拦。"""

    def __init__(self, purpose: str):
        self.purpose = purpose
        self._path: Path | None = None

    def __enter__(self):
        diverged = _lock_scope_divergence()
        if diverged:
            raise SystemExit(diverged)
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
    记 warning 并按「不存在」放行（自愈接管由随后的取锁完成）。
    死 pid 锁（2026-09-25 收口）：内容合法 + pid 确定不存在 + 超宽限 → 同
    样判为崩溃残留放行（硬杀残留不再 brick 套件）；pid 查询失败/权限不
    足/pid 仍存在 → 一律按「存在」拦。

    拒绝出口（2026-09-26 OPEN-6 入册）除锁路径外还带 `_ops_dispose_hint`
    的可复制 `tasklist` 核查/清锁命令——pid 被复用给无关进程时本条永不
    自动自愈，运维必须有一条不含猜测的出路（**仅文案，判定逻辑未变**）。"""
    p = prod_lock_path() if watch is None else watch
    held = _info_at(p)
    if held is None:
        return
    if _corrupt_and_stale(p, held):
        _warn_corrupt_stale(p, held)
        return
    if _dead_pid_and_stale(p, held):
        _warn_dead_pid_stale(p, held)
        return
    raise SystemExit(
        f"[live/pytest 互斥守卫] live 实跑进行中（{held}）——拒绝 {context}"
        f"（R6：全绿结论不许被并发 live 污染）。等 live 结束；若 live "
        f"已崩溃遗留死锁，人工核实后清除：{p}"
        + _ops_dispose_hint(held, p))
