"""R6 live/pytest 互斥守卫——跨进程竞态回归（2026-09-23 审计 P1 验收项）。

钉住的事（同锁协议见 app/live_guard.py + app/config.py 单点导出）：
1. **路径单点**：live 侧与 pytest 侧的锁路径都只出自 app/config.py
   （LOCK_FILE_NAME / prod_lock_path() / live_lock_path()）；LG_LOCK_DIR
   一个旋钮让两侧同时跟随——本文件全部子进程用例用它隔离到临时目录，
   **绝不触碰真实生产锁位**（repo/data/live_run.lock）；
2. **方向 A**：pytest 整轮持锁期间，live 进程启动必须被拒且给出明确
   原因（跨进程实证，不再是「查一次不持锁」）；
3. **方向 B**：live 进程真实持锁期间，pytest 启动（其 conftest 在
   pytest_configure 取锁）必须非零退出、明确拒绝——不得静默放行；
4. **方向 C**：pytest 整轮结束（pytest_sessionfinish 释放）后锁位无
   残留，live 可正常获取；
5. **竞态**：两个 live 进程同一时刻起跑，O_EXCL 原子性保证恰好一个赢。
6. **OPEN-1（2026-09-26 入册，承重墙补网）**：**跨进程同 purpose** 的
   保守释放——test_release_only_deletes_own_lock / test_live_blocks_
   while_pytest_lock_held 两条在册用例的前任/继任都在**同一进程**里且
   purpose 不同，_release_lock 的 **pid 回读分支**在沙箱里删掉后 25 个
   在册用例仍全绿（审查判定：承重墙无在册回归）。本文件
   test_open1_crossproc_* 用两个真实子进程（同 purpose）钉死该分支。
7. **OPEN-2（2026-09-26 入册）**：方向 A 的 pytest 侧在册实现是父进程
   内的**进程内替身**（test_direction_a_* 用 whole_run_lock(watch=)）
   ——本文件 test_open2_* 改由**子进程真跑 `python -m pytest`** 持整轮
   锁（conftest 真实 pytest_configure 取锁），并在探针用例的
   pytest_runtest_call hookwrapper **post-yield** 阻塞（测试已跑完、
   session 未结束 ⇒ 锁必须仍在握）；真实 live 子进程抢锁必须被拒、锁
   内容逐字节未变、且锁内 pid == pytest 子进程 pid。
8. **OPEN-3（2026-09-26 入册，硬拦落地）**：LG_DATA_DIR 显式设置而
   LG_LOCK_DIR 未设 ⇒ live 锁位与 pytest 观察的生产锁位**分叉**（审查
   实验 e 已复现真实重叠）——app/live_guard.py 取锁入口
   `_lock_scope_divergence` 硬拦；test_open3_* 钉拒绝口径与放行对照
   （显式设 LG_LOCK_DIR＝声明隔离意图 ⇒ 放行）。
9. **OPEN-4（2026-09-26 第二批入册，测试卫生）**：干净源检出里裸跑
   （未设 LG_LOCK_DIR）会在检出目录创建 data/ 整轮锁位——tests/conftest.py
   现在响亮 warning + 收尾清除本轮自创的空目录；test_open4_* 用**仿造根
   子进程**（复制 app/ + tests/conftest.py 到临时目录跑真 pytest）钉该
   行为，不触碰本仓真实 ROOT/data。

纪律：只起子进程验证协议，不碰 DB、不跑 --live 开放路径。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as CFG                     # noqa: E402
from app import live_guard as LG                  # noqa: E402

TIMEOUT_S = 120

# ── 子进程脚本：只 import app.config / app.live_guard，不触 DB/LLM ──

# live 角色：尝试取锁并跑起来（取锁失败 → SystemExit，非零退出）
LIVE_TRY = (
    "import time\n"
    "from app import live_guard as lg\n"
    "with lg.live_lock('t-child-live'):\n"
    "    print('CHILD_LIVE_STARTED', flush=True)\n"
    "    time.sleep(0.5)\n"
)

# live 角色（握手版）：持锁直到父进程放置 RELEASE 文件才释放
LIVE_HOLD = (
    "import os, time\n"
    "from app import live_guard as lg\n"
    "ready, release = os.environ['LG_CHILD_READY'], os.environ['LG_CHILD_RELEASE']\n"
    "with lg.live_lock('t-child-live'):\n"
    "    with open(ready, 'w') as f:\n"
    "        f.write(str(os.getpid()))\n"
    "    end = time.time() + 60\n"
    "    while not os.path.exists(release) and time.time() < end:\n"
    "        time.sleep(0.05)\n"
    "print('CHILD_LIVE_RELEASED', flush=True)\n"
)

# live 角色（赛跑版）：等 GO 门文件后同时冲锁，赢家持 0.3s
LIVE_RACE = (
    "import os, time\n"
    "from app import live_guard as lg\n"
    "go = os.environ['LG_CHILD_GO']\n"
    "while not os.path.exists(go):\n"
    "    time.sleep(0.01)\n"
    "with lg.live_lock('t-race'):\n"
    "    print('CHILD_ACQUIRED', flush=True)\n"
    "    time.sleep(0.3)\n"
)

# live 角色（交棒版，OPEN-1）：PY1 与 PY2 用**同一 purpose**，中间父进程
# unlink 锁文件模拟人工清锁/崩溃残留自愈——前任（PY1）迟到退出会不会删
# 掉继任者（PY2）的锁，只剩 _release_lock 的 pid 回读分支可拦（purpose
# 两者相同，purpose 比对恒真）。
LIVE_RELAY = (
    "import os, time\n"
    "from app import live_guard as lg\n"
    "ready, release = os.environ['LG_RELAY_READY'], os.environ['LG_RELAY_RELEASE']\n"
    "with lg.live_lock('t-relay'):\n"
    "    with open(ready, 'w') as f:\n"
    "        f.write(str(os.getpid()))\n"
    "    end = time.time() + 60\n"
    "    while not os.path.exists(release) and time.time() < end:\n"
    "        time.sleep(0.05)\n"
    "print('CHILD_RELAY_EXITED', flush=True)\n"
)

# live 角色（分叉场景，OPEN-3）：LG_DATA_DIR 显式改而 LG_LOCK_DIR 未设
# （子进程内再 pop 一次，防 .env setdefault 注入）⇒ 锁位与生产锁位分叉，
# 取锁入口必须硬拦（_lock_scope_divergence）。
LIVE_DIVERGED = (
    "import os\n"
    "from app import live_guard as lg\n"
    "import app.config  # noqa: F401 —— 先完成 .env 加载，再清注入\n"
    "os.environ.pop('LG_LOCK_DIR', None)\n"
    "with lg.live_lock('t-diverged'):\n"
    "    print('CHILD_LIVE_STARTED_DIVERGED', flush=True)\n"
)


def _child_env(lock_dir: Path, **extra: object) -> dict:
    env = os.environ.copy()
    env["LG_LOCK_DIR"] = str(lock_dir)      # 锁目录单点覆盖：两侧同时跟随
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _child_env_diverged(data_dir: Path, **extra: object) -> dict:
    """OPEN-3 分叉环境：LG_DATA_DIR 显式设置、LG_LOCK_DIR **不设**——live
    锁位随 DATA_DIR 漂走、pytest 侧仍盯生产锁位（=分叉）。DB 一并指到临
    时目录（正常路径本就不该触库，双保险）。"""
    env = os.environ.copy()
    env.pop("LG_LOCK_DIR", None)
    env["LG_DATA_DIR"] = str(data_dir)
    env["LG_DATABASE_URL"] = f"sqlite:///{(data_dir / 'probe.db').as_posix()}"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _run_live_child(lock_dir: Path, snippet: str, **extra: object):
    return subprocess.run(
        [sys.executable, "-c", snippet], cwd=str(ROOT),
        env=_child_env(lock_dir, **extra),
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=TIMEOUT_S)


def _run_pytest_child(lock_dir: Path, keyword: str):
    """子进程跑一轮真 pytest（本文件、只命中无副作用的 child_probe 用例）：
    其 tests/conftest.py 会在 pytest_configure 检锁+取锁、sessionfinish 释放。"""
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)),
         # 清掉 pyproject.toml 的 addopts="-q"：否则与本处的 -q 叠成 -qq，
         # pytest 会吞掉 "1 passed" 总结行（方向 C 曾据此误判失败）。
         "-o", "addopts=",
         "-q", "-k", keyword, "-p", "no:cacheprovider"],
        cwd=str(ROOT), env=_child_env(lock_dir),
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=TIMEOUT_S)


def _wait_until(cond, what: str, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    pytest.fail(f"等待超时：{what}")


# ── 6. OPEN-1：跨进程同 purpose——_release_lock 的 pid 回读分支（承重墙）──

def test_open1_crossproc_same_purpose_release_keeps_successor_lock(tmp_path):
    """两个**真实子进程**用同一 purpose 交棒：PY1 持锁 → 父进程 unlink
    锁文件（模拟人工清锁/崩溃残留自愈）→ PY2 O_EXCL 接管 → 放行 PY1 迟
    到退出。前任退出时回读到的锁 purpose 与自己相同——拦住它误删继任者
    锁的**只剩 pid 分支**（在册两条「保守释放」用例前任/继任同进程且
    purpose 不同，删掉 pid 校验也全绿，即审查判定的无在册承重墙）。
    另钉继任者正常退出后无残留。"""
    lock_file = tmp_path / CFG.LOCK_FILE_NAME
    r1_ready, r1_release = tmp_path / "relay1.ready", tmp_path / "relay1.release"
    r2_ready, r2_release = tmp_path / "relay2.ready", tmp_path / "relay2.release"

    def _spawn(ready: Path, release: Path) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", LIVE_RELAY], cwd=str(ROOT),
            env=_child_env(tmp_path, LG_RELAY_READY=ready,
                           LG_RELAY_RELEASE=release),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")

    py1 = py2 = None
    try:
        py1 = _spawn(r1_ready, r1_release)
        _wait_until(lambda: r1_ready.exists(), "PY1 持锁就绪")
        pid1 = int(r1_ready.read_text(encoding="utf-8"))
        assert json.loads(lock_file.read_text(encoding="utf-8"))["pid"] == pid1
        lock_file.unlink()                        # 模拟人工清锁/残留自愈
        py2 = _spawn(r2_ready, r2_release)        # 只在清锁后才起跑（否则被 O_EXCL 拒）
        _wait_until(lambda: r2_ready.exists(), "PY2 同 purpose 接管持锁")
        pid2 = int(r2_ready.read_text(encoding="utf-8"))
        assert pid1 != pid2
        held = json.loads(lock_file.read_text(encoding="utf-8"))
        assert held["purpose"] == "t-relay" and held["pid"] == pid2
        r1_release.touch()                        # 放行前任 PY1 迟到退出
        out1, err1 = py1.communicate(timeout=TIMEOUT_S)
        assert "CHILD_RELAY_EXITED" in out1, f"PY1 退出异常：{err1}"
        if not lock_file.exists():
            # 失败时把「谁删的」判据一次性全贴出（2026-09-26 主控亲修：首版只
            # 报「锁没了」，分不清 pid 分支失能 / 继任者提前释放 / 外部清锁）。
            raise AssertionError(
                "前任（PY1）退出删掉了继任者（PY2）的同 purpose 锁——"
                "_release_lock pid 回读分支失能（人工清锁后继任者互斥归零）；"
                f" 诊断：pid1={pid1} pid2={pid2} PY2存活={py2.poll() is None}"
                f" PY1输出={out1.strip()!r} PY1错误={err1.strip()[-300:]!r}")
        after = json.loads(lock_file.read_text(encoding="utf-8"))
        assert after["pid"] == pid2 and after["purpose"] == "t-relay", \
            "前任退出不得动继任者的锁（只删自己的）"
        r2_release.touch()                        # 放行继任者正常收尾
        out2, err2 = py2.communicate(timeout=TIMEOUT_S)
        assert "CHILD_RELAY_EXITED" in out2, f"PY2 退出异常：{err2}"
        assert not lock_file.exists(), "继任者（真正持有者）正常退出须释放锁"
    finally:
        for pr in (py1, py2):
            if pr is not None and pr.poll() is None:
                pr.kill()


# ── 7. OPEN-2：真 pytest 子进程整轮持锁（非进程内替身）+ 真实 live 抢锁 ──

# OPEN-2 持锁窗制造器**不在本模块**：pytest 只注册 conftest.py / 插件的钩子，
# 测试模块里的 `pytest_*` 函数永不执行（2026-09-26 主控实测：子进程 pytest 跑完
# 即退、锁已释放，主用例读锁文件 FileNotFoundError）。持锁窗由
# `tests/conftest.py::pytest_runtest_makereport` 承担（仅 LG_CHILD_HOLD 设了的
# 子进程生效；`when == "call"` ⇒ 测试已结束、teardown/sessionfinish 未发生）。


def test_open2_pytest_probe_marker():
    """OPEN-2 探针（子进程 pytest 里真跑）：测试阶段内整轮锁确由**本
    pytest 进程**持有（conftest pytest_configure 取的）；写 passed 标记
    后返回——真正的持锁窗口在上面的 hookwrapper post-yield 里（测试已
    结束、session 未结束）。父轮（无 LG_CHILD_PASSED）为纯 no-op。"""
    lock_dir = os.environ.get("LG_LOCK_DIR")
    passed = os.environ.get("LG_CHILD_PASSED")
    if not lock_dir or not passed:
        return
    p = Path(lock_dir) / CFG.LOCK_FILE_NAME
    info = json.loads(p.read_text(encoding="utf-8"))
    assert info["purpose"] == LG.PYTEST_LOCK_PURPOSE, \
        "子 pytest 测试阶段未持有整轮锁"
    assert info["pid"] == os.getpid(), "整轮锁持有者须是本 pytest 进程"
    # passed 标记里写**真实解释器 pid**：venv 的 python.exe 是 shim，Popen.pid 是
    # shim 的 pid，shim 再起真解释器 ⇒ 二者不等（2026-09-26 主控实测：
    # Popen.pid=39748 / 子进程 os.getpid()=7228）。父用例只能拿这里的实测 pid
    # 比对锁内 pid，不能拿 holder.pid 比对。
    Path(passed).write_text(str(os.getpid()), encoding="utf-8")


def test_open2_real_pytest_process_holds_lock_live_refused(tmp_path):
    """OPEN-2 主用例（端到端）：子进程**真跑 `python -m pytest`**——其
    tests/conftest.py 在 pytest_configure 真实取整轮锁（在册方向 A 的
    pytest 侧是父进程内替身，钉不到这条真路径）。探针跑完、conftest 的
    makereport 包装器阻塞（测试已结束、session 未结束）→ 另起**真实 live 子
    进程**抢锁：必须 exit≠0、输出含互斥守卫字样、锁内容逐字节未变且
    pid == pytest 子进程 pid（持锁主体是那个真 pytest 进程本身）。放行
    → 子 pytest 正常收尾（1 passed）、锁无残留。"""
    lock_file = tmp_path / CFG.LOCK_FILE_NAME
    passed = tmp_path / "open2_probe.passed"
    hold = tmp_path / "open2.hold"
    holder = subprocess.Popen(
        [sys.executable, "-m", "pytest", str(Path(__file__)),
         # 清 pyproject addopts="-q"，防叠成 -qq 吞掉总结行（同 _run_pytest_child）
         "-o", "addopts=",
         "-q", "-k", "open2_pytest_probe", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        env=_child_env(tmp_path, LG_CHILD_HOLD=hold, LG_CHILD_PASSED=passed),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    try:
        try:
            _wait_until(lambda: passed.exists(), "子 pytest 探针用例跑完（锁应仍在握）")
        except BaseException:
            # 超时/失败时**必须**把子进程收尸并把它自己的输出贴进诊断：2026-09-26
            # 主控亲修——首版此路径静默吞掉子 pytest 的 stdout/stderr，失败时只
            # 有一句「等待超时」，无法判是子 pytest 被拒跑、收集失败、还是探针
            # 断言红（真红因全在被吞的输出里）。
            hold.touch()
            try:
                so, se = holder.communicate(timeout=TIMEOUT_S)
            except subprocess.TimeoutExpired:
                holder.kill()
                so, se = holder.communicate(timeout=TIMEOUT_S)
            raise AssertionError(
                f"子 pytest 未在期限内产出 passed 标记（rc={holder.returncode}）；"
                f"子进程输出：{(so + se)[-1500:]}")
        # 子进程真实解释器 pid（探针写进 passed 标记；见
        # test_open2_pytest_probe_marker）——不能拿 holder.pid：venv python.exe
        # 是 shim，Popen.pid ≠ 真解释器 pid。
        child_pid = int(passed.read_text(encoding="utf-8"))
        snapshot = lock_file.read_bytes()
        info = json.loads(snapshot.decode("utf-8"))
        assert info["purpose"] == LG.PYTEST_LOCK_PURPOSE
        assert info["pid"] == child_pid, \
            f"锁持有者 pid={info['pid']} ≠ 真 pytest 子进程 pid={child_pid}"
        r = _run_live_child(tmp_path, LIVE_TRY)
        out = r.stdout + r.stderr
        assert r.returncode != 0, \
            "真 pytest 进程整轮持锁期间 live 竟启动——conftest 真取锁路径失守" \
            "（进程内替身钉不住的那种失守）"
        assert "互斥守卫" in out, f"拒绝须给明确原因：{out[-500:]}"
        assert LG.PYTEST_LOCK_PURPOSE in out, f"原因须指明 pytest 持锁：{out[-500:]}"
        assert "CHILD_LIVE_STARTED" not in r.stdout, "live 不得在拒锁后发起真跑"
        assert lock_file.read_bytes() == snapshot, \
            "live 被拒期间整轮锁内容必须逐字节未变"
    finally:
        hold.touch()                              # 放行：让子 pytest 走 sessionfinish
        try:
            so, se = holder.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            holder.kill()
            so, se = holder.communicate(timeout=TIMEOUT_S)
        if holder.poll() is None:
            # 断言失败时上面的 communicate 根本不会执行 ⇒ 无条件兜底杀：否则
            # 持锁的子 pytest 活到 120s 兜底超时，既拖长整轮门，又让下一轮
            # 撞「live/pytest 互斥」假红。2026-09-26 主控亲修。
            holder.kill()
            holder.communicate(timeout=TIMEOUT_S)
    assert holder.returncode == 0, \
        f"放行后子 pytest 应正常收尾：{(so + se)[-800:]}"
    assert "1 passed" in so, f"探针须真正执行过：{so[-500:]}{se[-500:]}"
    assert not lock_file.exists(), \
        "子 pytest sessionfinish 后整轮锁残留——live 将被误拒"


# ── 8. OPEN-3：LG_DATA_DIR 改而 LG_LOCK_DIR 未设 ⇒ 锁位分叉，取锁入口硬拦 ──

def test_open3_live_refused_when_lock_scope_diverges(tmp_path):
    """机械判据（审查实验 e 复现的真实重叠入口）：LG_DATA_DIR 显式设置
    （live 锁位漂到临时数据目录）而 LG_LOCK_DIR 未设（pytest 侧仍盯生产
    锁位）⇒ 两侧看的不是同一个文件、互斥失明——live 取锁入口必须硬拦：
    exit≠0、输出含「分叉」原因与可执行指引（LG_LOCK_DIR 处置），且不
    在漂移位留下锁、未发起真跑。"""
    data_dir = tmp_path / "alt_data"
    r = subprocess.run(
        [sys.executable, "-c", LIVE_DIVERGED], cwd=str(ROOT),
        env=_child_env_diverged(data_dir),
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=TIMEOUT_S)
    out = r.stdout + r.stderr
    assert r.returncode != 0, \
        f"口径分叉必须硬拦，live 竟正常起跑：{out[-500:]}"
    assert "互斥守卫" in out and "分叉" in out, \
        f"拒绝须点明锁位口径分叉：{out[-500:]}"
    assert "LG_LOCK_DIR" in out, f"拒绝须给可执行处置指引：{out[-500:]}"
    assert "CHILD_LIVE_STARTED_DIVERGED" not in r.stdout
    assert not (data_dir / CFG.LOCK_FILE_NAME).exists(), \
        "硬拦发生在取锁之前，不得留下锁残留"


def test_open3_declared_lock_dir_with_data_dir_override_passes(tmp_path):
    """对照（硬拦不许过拦）：LG_DATA_DIR 改指他处**同时**显式设
    LG_LOCK_DIR——两侧同跟随这一旋钮、不再分叉（＝声明独立沙箱、明知
    不与 pytest 并发的既有用法）→ live 正常取锁-释放。"""
    data_dir = tmp_path / "alt_data2"
    lock_dir = tmp_path / "declared_locks"
    r = _run_live_child(lock_dir, LIVE_TRY, LG_DATA_DIR=data_dir)
    assert r.returncode == 0, \
        f"显式声明锁目录的隔离用法不得被分叉硬拦误伤：{r.stdout}{r.stderr}"
    assert "CHILD_LIVE_STARTED" in r.stdout
    assert not (lock_dir / CFG.LOCK_FILE_NAME).exists()
    assert not (data_dir / CFG.LOCK_FILE_NAME).exists()


# ── 1. 锁路径单点导出（P1 错位钉死）──────────────────────────────

def test_lock_path_single_source_exported_by_config(tmp_path, monkeypatch):
    """两侧锁路径只出自 app/config.py；LG_LOCK_DIR 让两侧同时跟随；
    生产锁位**不随** LG_DATA_DIR（pytest 覆写的临时目录）漂移——
    2026-09-23 P1「守卫盯错目录」的不变式。"""
    monkeypatch.delenv("LG_LOCK_DIR", raising=False)
    # pytest 侧缺省=固定生产锁位 repo/data/live_run.lock
    assert LG.prod_lock_path() == CFG.prod_lock_path()
    assert LG.prod_lock_path() == ROOT / "data" / CFG.LOCK_FILE_NAME
    monkeypatch.setenv("LG_DATA_DIR", str(tmp_path / "pytest_tmp_data"))
    assert LG.prod_lock_path() == ROOT / "data" / CFG.LOCK_FILE_NAME, \
        "生产锁位随 LG_DATA_DIR 漂移=P1 错位复发"
    # live 侧跟随本进程 DATA_DIR（conftest/测试用 monkeypatch 隔离）
    monkeypatch.setattr(CFG, "DATA_DIR", tmp_path / "live_data")
    assert LG.lock_path() == CFG.live_lock_path()
    assert LG.lock_path() == tmp_path / "live_data" / CFG.LOCK_FILE_NAME
    # LG_LOCK_DIR 一个旋钮，两侧同移（子进程协议测试的隔离通道）
    monkeypatch.setenv("LG_LOCK_DIR", str(tmp_path / "lock_dir"))
    assert LG.prod_lock_path() == LG.lock_path() \
        == tmp_path / "lock_dir" / CFG.LOCK_FILE_NAME


# ── 2. 方向 A：pytest 持锁 ⇒ live 启动被拒 ──────────────────────

def test_direction_a_live_refused_while_pytest_holds_lock(tmp_path):
    """pytest 侧整轮锁（purpose="pytest"，conftest 同款）持有期间，
    另起**真实子进程** live：必须非零退出、原因明确指向 pytest 持锁，
    live 侧未发起（无 CHILD_LIVE_STARTED）、pytest 锁原封不动。"""
    lock_file = tmp_path / CFG.LOCK_FILE_NAME
    with LG.whole_run_lock("测试内 pytest 整轮持锁", watch=lock_file):
        assert json.loads(lock_file.read_text(encoding="utf-8")) \
            ["purpose"] == LG.PYTEST_LOCK_PURPOSE
        r = _run_live_child(tmp_path, LIVE_TRY)
        out = r.stdout + r.stderr
        assert r.returncode != 0, \
            "pytest 持锁期间 live 竟启动了——P1「只查不持」竞态复发"
        assert "互斥守卫" in out, f"拒绝须给明确原因：{out}"
        assert LG.PYTEST_LOCK_PURPOSE in out, f"原因须指明 pytest 持锁：{out}"
        assert "CHILD_LIVE_STARTED" not in r.stdout
        assert json.loads(lock_file.read_text(encoding="utf-8")) \
            ["purpose"] == LG.PYTEST_LOCK_PURPOSE, "live 被拒后不得动 pytest 的锁"
    assert not lock_file.exists(), "整轮结束不得残留锁"


# ── 3. 方向 B：live 持锁 ⇒ pytest 启动明确被拒（跨进程真 pytest）──

def test_direction_b_pytest_refused_while_live_holds_lock(tmp_path):
    """真实 live 子进程持锁（握手确保已持锁）→ 子进程真跑 `python -m
    pytest`：其 conftest 在 pytest_configure 检锁拒跑——非零退出、输出
    含互斥守卫原因；live 的锁不被 pytest 侧删除。"""
    lock_file = tmp_path / CFG.LOCK_FILE_NAME
    ready, release = tmp_path / "child.ready", tmp_path / "child.release"
    holder = subprocess.Popen(
        [sys.executable, "-c", LIVE_HOLD], cwd=str(ROOT),
        env=_child_env(tmp_path, LG_CHILD_READY=ready, LG_CHILD_RELEASE=release),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    try:
        # 主控修正（2026-09-23）：原交付把 bool 当可调用传入（ready.exists()
        # 已求值）→ _wait_until 内 cond() 抛 TypeError，方向 B 必红。
        _wait_until(lambda: ready.exists(), "live 子进程持锁就绪")
        r = _run_pytest_child(tmp_path, "child_probe")
        out = r.stdout + r.stderr
        assert r.returncode != 0, \
            f"live 持锁时 pytest 静默放行——守卫失能：{out[-800:]}"
        assert "互斥守卫" in out, f"pytest 拒跑须给明确原因：{out[-800:]}"
        assert "1 passed" not in out, "被拒的 pytest 不得进入测试阶段"
        held = json.loads(lock_file.read_text(encoding="utf-8"))
        assert held["purpose"] == "t-child-live", "pytest 被拒时不得动 live 的锁"
    finally:
        release.touch()
        try:
            so, se = holder.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            holder.kill()
            so, se = holder.communicate(timeout=TIMEOUT_S)
    assert "CHILD_LIVE_RELEASED" in (so or ""), f"live 握手释放异常：{se}"
    assert not lock_file.exists(), "live 正常退出后不得残留锁"


# ── 4. 方向 C：pytest 整轮结束后锁无残留、live 可正常获取 ────────

def test_direction_c_live_can_lock_after_pytest_session(tmp_path):
    """无 live 干扰时子进程真 pytest 一轮：正常通过（证明 configure 取锁
    成功）；退出后锁位**无残留**（sessionfinish 释放）；随后 live 子进程
    可在同一锁位正常取锁-释放。"""
    lock_file = tmp_path / CFG.LOCK_FILE_NAME
    r = _run_pytest_child(tmp_path, "child_probe")
    assert r.returncode == 0, f"无并发时子进程 pytest 应正常通过：{r.stdout[-800:]}{r.stderr[-800:]}"
    assert "1 passed" in r.stdout, \
        f"探针用例须真正执行（锁应被持有过）：{r.stdout[-800:]}"
    assert not lock_file.exists(), \
        "pytest 整轮结束（pytest_sessionfinish）后锁残留——live 将被误拒"
    r2 = _run_live_child(tmp_path, LIVE_TRY)
    assert r2.returncode == 0, f"pytest 结束后 live 应可正常取锁：{r2.stdout}{r2.stderr}"
    assert "CHILD_LIVE_STARTED" in r2.stdout
    assert not lock_file.exists(), "live 取锁-释放循环后不得残留"


# ── 5. 真竞态：双 live 同刻起跑，O_EXCL 恰好一个赢 ───────────────

def test_race_two_live_children_o_excl_one_winner(tmp_path):
    """两进程同一 GO 门同时冲锁：恰一个成功（CHILD_ACQUIRED、exit 0），
    另一个被 O_EXCL 拒绝（互斥守卫、非零）——不存在双持锁窗口。"""
    go = tmp_path / "child.go"
    kids = [subprocess.Popen(
        [sys.executable, "-c", LIVE_RACE], cwd=str(ROOT),
        env=_child_env(tmp_path, LG_CHILD_GO=go),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace") for _ in range(2)]
    try:
        go.touch()
        results = [k.communicate(timeout=TIMEOUT_S) for k in kids]
    finally:
        for k in kids:
            if k.poll() is None:
                k.kill()
    codes = [k.returncode for k in kids]
    started = [o for (o, _e), c in zip(results, codes)
               if c == 0 and "CHILD_ACQUIRED" in o]
    refused = [(o + e) for (o, e), c in zip(results, codes)
               if c != 0 and "互斥守卫" in o + e]
    assert len(started) == 1 and len(refused) == 1, \
        f"O_EXCL 竞态破口：codes={codes}, 输出={results}"


# ── 探针用例：供方向 B/C 的子进程 pytest 以 -k child_probe 命中。
#    在子进程里钉住「测试阶段进行中，本 pytest 进程确实持有整轮锁」
#    （pytest_configure 取锁、尚未释放）；父轮里为纯 no-op。

def test_child_probe_noop_for_nested_pytest_runs():
    lock_dir = os.environ.get("LG_LOCK_DIR")
    if not lock_dir:
        return                                    # 父轮正常收集，无副作用
    p = Path(lock_dir) / CFG.LOCK_FILE_NAME
    info = json.loads(p.read_text(encoding="utf-8"))
    assert info["purpose"] == LG.PYTEST_LOCK_PURPOSE, \
        "测试阶段 pytest 侧未持有整轮锁——只查不持复发"
    assert info["pid"] == os.getpid(), "整轮锁持有者须是本 pytest 进程"


# ── 9. OPEN-4：裸跑套件不在源检出里留下 data/ 副作用（响亮 warning + 自清）──

def test_open4_bare_pytest_warns_and_leaves_no_data_dir(tmp_path):
    """裸跑（未设 LG_LOCK_DIR）的整轮锁位=ROOT/data，_acquire_lock 的
    mkdir 会在干净检出目录里**创建** data/（2026-09-26 主控实测：rc=5 一
    轮后检出里多出 data/）——对不在源检出跑套件的 reviewer/agent 是意外
    副作用。用**仿造根**复现（app/ + tests/conftest.py 复制进临时目录，
    config.ROOT 随复制位置漂移），全程不碰本仓真实 ROOT/data：
    1) 响亮 warning：输出含 OPEN-4 与 LG_LOCK_DIR 字面量（收口判据 a，
       warning 文本由此回归断言到）；
    2) 收尾自清：本轮创建的 data/ 在 sessionfinish 后不存在（收口判据 b
       的机理——真 worktree 里同理由 git status --porcelain 不再出现
       data/，见 docs/R6守卫覆盖缺口入册第二批_OPEN4-5_20260926.md 实跑读数）；
    3) warning 不改变结果：该轮 1 passed、rc=0。"""
    fake = tmp_path / "fake_root"
    (fake / "app").mkdir(parents=True)
    (fake / "tests").mkdir(parents=True)
    for rel in ("app/__init__.py", "app/config.py", "app/live_guard.py",
                "tests/conftest.py"):
        shutil.copyfile(ROOT / rel, fake / rel)
    (fake / "tests" / "test_probe.py").write_text(
        "def test_child_probe_marker():\n    assert True\n",
        encoding="utf-8")
    env = os.environ.copy()
    env.pop("LG_LOCK_DIR", None)                  # 伪造「裸跑」口径
    env.pop("LG_DATA_DIR", None)
    env["PYTHONPATH"] = str(fake) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_probe.py",
         "-o", "addopts=", "-q", "-p", "no:cacheprovider"],
        cwd=str(fake), env=env,
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=TIMEOUT_S)
    out = r.stdout + r.stderr
    assert "OPEN-4" in out and "LG_LOCK_DIR" in out, \
        f"裸跑必须打响亮 OPEN-4 warning（判据 a）：{out[-800:]}"
    assert "1 passed" in out and r.returncode == 0, \
        f"warning 不得改变本轮结果：rc={r.returncode} {out[-800:]}"
    assert not (fake / "data").exists(), \
        "本轮在检出目录创建的 data/ 必须收尾清除（判据 b）"
