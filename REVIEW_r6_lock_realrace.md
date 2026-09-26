VERDICT: PASS

# R6 live/pytest 互斥守卫 —— 真竞态覆盖与锁删除安全性 独立审查

- 任务 id：`lg-review-r6-lock-realrace`（kind=review，作者不得自审）
- 工作树：`F:/agi/_scratch/worktrees/audit-residual-3`（分支 `review/audit-residuals-3`）
- 审查对象：源检出 `F:/agi/language-genome`（**只读**）
- 本轮只审 R6 与会审侧两条残留（锁删除误删继任者 / 测试碰到真实生产锁），**未重开任何已 CLOSED 条目**。

## 0. 两边 HEAD 与差异

```
$ git -C F:/agi/language-genome rev-parse HEAD
a1fefc2a510734c57c36a29dfb45b5c04de9064d
$ git -C F:/agi/_scratch/worktrees/audit-residual-3 rev-parse HEAD
a1fefc2a510734c57c36a29dfb45b5c04de9064d
```

**两边 HEAD 完全一致**（`a1fefc2a`，`git log --oneline -3` 顶部为
`a1fefc2a merge(0b0f593): 主控独立验收后合入…`），因此本 worktree 的旧 HEAD 与源检出
**无代码差异**，读码结论对源检出直接成立。源检出 `git status --porcelain` 全程为空（干净）。

## 1. 逐条结论速览

| # | 待核命题 | 结论 | 关键证据 |
|---|---|---|---|
| 1 | conftest 用 `app/config.prod_lock_path()` 同一路径 | **成立** | 读码 `tests/conftest.py:61`→`app/live_guard.py:294`→`app/config.py:35-38`；实验 (a) 锁内 pid 与真实 pytest 进程 pid 相同 |
| 2 | pytest 整轮持锁（不只查一次） | **成立** | 实验 (a)：真实 pytest 进程在**测试阶段内**持锁，live 同刻抢锁 exit 1 |
| 3 | 释放只删自己的锁（回读 pid+purpose） | **成立** | 读码 `app/live_guard.py:231-244`；实验 (c1)/(c2) 两种继任者都保住；**反向验证**证明该校验是承重墙 |
| 4 | 残留①「释放路径可能误删继任者的锁」 | **不成立** | 同上；且**去掉 pid 回读立刻复现误删**（判红原文见 §4） |
| 5 | 残留②「跑测试会把 live 的锁删了/改了」 | **部分成立** | 「删/改 live 持有的锁」**不成立**（实验 b、c2）；「跑测试确实创建+删除真实生产锁**路径**」**成立**（实验 d，且是设计意图） |
| 6 | `tests/test_live_guard.py` 已覆盖双向**真**竞态 | **部分成立（有实测缺口）** | 方向 B 是真跨进程真 pytest（`tests/test_live_guard_mutex.py:162-193`）；方向 A 的 pytest 侧是**进程内替身**（同文件 `:145`）；跨进程**同 purpose** 继任者释放**无任何在册用例**（见 OPEN-1） |
| 7 | `prod_lock_path()` / `live_lock_path()` 在 `LG_LOCK_DIR` 有无/有覆盖下的关系 | **成立，但存在未钉死的逃逸口** | 四环境矩阵实测（§2.3）；`LG_DATA_DIR` 单独设置时两侧**分叉**并已被实验 (e) 复现出真实重叠 |

## 2. 读码核实

### 2.1 `tests/conftest.py`（源检出 HEAD a1fefc2a）

- `:15-17` 把 `LG_DATABASE_URL` / `LG_DATA_DIR` 覆写到 `tempfile.mkdtemp()` 临时目录。
- `:39` 只 `from app.live_guard import refuse_if_live_running, whole_run_lock`——**不自己拼锁路径**。
- `pytest_configure`（`:51-65`）：
  1. `refuse_if_live_running("全量 pytest")`（`:60`）→ `watch` 缺省 = `prod_lock_path()`；
  2. `_run_guard = whole_run_lock("全量 pytest")`（`:61`），`:62` 立刻 `__enter__()`；
     `whole_run_lock` 内部 `app/live_guard.py:294` 取 `p = prod_lock_path() if watch is None else watch`，
     `:298` `_acquire_lock(p, info)`（O_EXCL），`:299-302` `yield` 期间整轮持锁，`finally` 释放；
  3. 拒跑出口 `pytest.exit(..., returncode=2)`（`:64`），**不持锁**（`_run_guard` 仍为 `None`）。
- `atexit` 兜底（`:65`）只在**取锁成功后**注册；`_release_run_guard`（`:44-48`）先把
  `_run_guard` 置 `None` 再 `__exit__` ⇒ 幂等，重复调用不会二次动锁。
- `pytest_sessionfinish`（`:68-70`）正常路径释放。
- **结论：路径同源、整轮持锁、释放只删自己的锁——三条全部成立。**

### 2.2 `app/live_guard.py`

- 取锁 `_acquire_lock`（`:201-228`）：`os.open(p, O_CREAT|O_EXCL|O_WRONLY)` 原子创建；
  `FileExistsError` 时按 `_corrupt_and_stale`（`:112-120`）/ `_dead_pid_and_stale`（`:157-190`）
  各只允许**一次**自愈 unlink+重试，其余一律 `SystemExit` 拒绝（`:223-225`），不排队。
- 自愈的两条证据门：内容不可解析 + mtime 超 `CORRUPT_LOCK_GRACE_SECONDS=15.0`；
  或 pid **确定不存在**（`_pid_looks_live` `:130-154`，只把 `ProcessLookupError` /
  Windows `EINVAL|winerror 87` 判为死，其余含 `ACCESS_DENIED` 一律按「存在」拦）
  + mtime 超宽限。**任何自愈 unlink 都要求「mtime 超 15s」**，而活进程刚建的锁 mtime 是新鲜的
  ⇒ 自愈路径不会删掉活锁（实验 f 实证）。
- 释放 `_release_lock`（`:231-244`）：
  `json.loads` 回读 → `FileNotFoundError` 直接返回（锁已被人工清除）→ 内容损坏**不删**（宁拦不猜）
  → **`:240` `if info.get("pid") == os.getpid() and info.get("purpose") == purpose:` 才 unlink**。
  ⇒ 「释放只删自己的锁」在代码层成立，且 pid 与 purpose 是**与**条件（两个都过才删）。
- `live_lock.__exit__`（`:271-275`）与 `whole_run_lock` 的 `finally`（`:301-302`）都走同一条释放路径。

### 2.3 `app/config.py`

`LOCK_FILE_NAME = "live_run.lock"`（`:28`）；`_lock_dir_override()`（`:31-32`）`(os.environ.get("LG_LOCK_DIR") or "").strip()`
⇒ 空串/纯空白视同未设置；`prod_lock_path()`（`:35-38`）= `LG_LOCK_DIR or ROOT/data` / 文件名；
`live_lock_path()`（`:41-43`）= `LG_LOCK_DIR or DATA_DIR` / 文件名。

四环境矩阵实测（源检出，`python -c` 子进程逐个打印）：

```
### env overrides: (none)
  DATA_DIR     = F:\agi\language-genome\data
  prod_lock_path() = F:\agi\language-genome\data\live_run.lock
  live_lock_path() = F:\agi\language-genome\data\live_run.lock
  SAME PATH?  = True
### env overrides: {'LG_LOCK_DIR': 'C:\\...\\r6rev\\zz'}
  prod_lock_path() = C:\Users\6\AppData\Local\Temp\opencode\r6rev\zz\live_run.lock
  live_lock_path() = C:\Users\6\AppData\Local\Temp\opencode\r6rev\zz\live_run.lock
  SAME PATH?  = True
### env overrides: {'LG_DATA_DIR': 'C:\\...\\r6rev\\otherdata'}
  DATA_DIR     = C:\Users\6\AppData\Local\Temp\opencode\r6rev\otherdata
  prod_lock_path() = F:\agi\language-genome\data\live_run.lock
  live_lock_path() = C:\Users\6\AppData\Local\Temp\opencode\r6rev\otherdata\live_run.lock
  SAME PATH?  = False        <-- 两侧分叉
### env overrides: {'LG_LOCK_DIR': '   '}
  SAME PATH?  = True         <-- 纯空白视同未设置（不误分叉）
```

⇒ `LG_LOCK_DIR` 是**唯一同时跟随两侧**的旋钮，行为符合设计；`LG_DATA_DIR` 单独设置会让两侧分叉
（这正是 `app/live_guard.py:36-37` 自称的「残留窄窗 2」，代码里已如实声明，但**无任何测试钉死**，
且实验 (e) 证明它能造出真实重叠 → OPEN-3）。

## 3. 真跑竞态实验（全部原始命令 + 原始输出 + exit code）

纪律：除特别说明外**一律用 `LG_LOCK_DIR` 覆盖到 `C:\Users\6\AppData\Local\Temp\opencode\r6rev\*`
临时目录**；源检出的 `F:\agi\language-genome\data\live_run.lock` 全程**不存在也未被创建**
（实验前后各核一次，见 §6）。所有 pytest 子进程加 `PYTHONDONTWRITEBYTECODE=1 -p no:cacheprovider`。
「持锁的 pytest 进程」用 `python - <<'PY'` 里的 stdin 驱动脚本拉起，插件在
`pytest_runtest_call` 的 hookwrapper **post-yield** 处阻塞（即测试已跑完、session 尚未结束 ⇒ 整轮锁仍在握），
**没有在树内留下任何脚本文件**。

### (a) 方向 A：真实 pytest 持锁 → 真实 live 抢锁必须被拒

原始命令（工作目录 `F:/agi/language-genome`）：

```
F:\Hermes\hermes-agent\venv\Scripts\python.exe -c <PY_SRC> F:\agi\language-genome\tests\test_live_guard_mutex.py -o addopts= -q -k child_probe -p no:cacheprovider
F:\Hermes\hermes-agent\venv\Scripts\python.exe -c "import time
from app import live_guard as lg
with lg.live_lock('r6-review-live'):
    print('LIVE_STARTED', flush=True)
    time.sleep(0.5)
"
```

原始输出：

```
==================== CMD-1  (real pytest process, conftest takes whole-run lock)
PY1 ready file exists: True
PY1 pid from ready   : 39292
LOCK content while PY1 holds:
{"purpose": "pytest", "pid": 39292, "started_at": "2026-09-26T10:18:58"}
==================== CMD-2  (real live process tries to take the same lock while PY1 holds it)
CMD-2 exit code: 1
CMD-2 stdout: ''
CMD-2 stderr: [live 互斥守卫] 已有 live 实跑/pytest 持锁（{'purpose': 'pytest', 'pid': 39292, 'started_at': '2026-09-26T10:18:58'}）——拒绝重叠（R6：live 不与 live/pytest 并发）；锁：C:\Users\6\AppData\Local\Temp\opencode\r6rev\r6a_52lmil3j\live_run.lock

LOCK unchanged after live attempt: True
==================== CMD-3  (release PY1 -> its sessionfinish/atexit must delete its own lock)
PY1 exit code: 0
PY1 stdout tail: .                                                                        [100%]
1 passed, 5 deselected in 0.33s
PY1 stderr tail:
LOCK exists after PY1 exit: False
==================== VERDICT-A
A_PASS
DRIVER-EXIT=0
```

判读：锁内 `pid=39292` **等于真实 pytest 进程 pid**，`purpose=pytest`；live 进程 exit 1、
`LIVE_STARTED` 未出现、锁内容逐字节未变；pytest 正常收尾后锁位无残留。
**「整轮持锁」在跨进程真跑下成立，「live 抢锁被拒」成立。**

### (b) 方向 B：真实 live 持锁 → 真实 pytest 必须 fail-fast 拒跑且**不删 live 的锁**

原始命令：

```
F:\Hermes\hermes-agent\venv\Scripts\python.exe -c <LIVE_HOLD>
F:\Hermes\hermes-agent\venv\Scripts\python.exe -m pytest F:\agi\language-genome\tests\test_live_guard_mutex.py -o addopts= -q -k child_probe -p no:cacheprovider
```

原始输出：

```
==================== CMD-1  (real LIVE process takes the lock and holds it)
LIVE ready: True pid: 36248
LOCK content while LIVE holds: {"purpose": "r6-review-live", "pid": 36248, "started_at": "2026-09-26T10:19:07"}
==================== CMD-2  (real pytest process: conftest pytest_configure must fail-fast refuse)
CMD-2 exit code: 2 (elapsed 0.8s)
CMD-2 stdout+stderr:
Exit: [live/pytest 互斥守卫] live 实跑进行中（{'purpose': 'r6-review-live', 'pid': 36248, 'started_at': '2026-09-26T10:19:07'}）——拒绝 全量 pytest（R6：全绿结论不许被并发 live 污染）。等 live 结束；若 live 已崩溃遗留死锁，人工核实后清除：C:\Users\6\AppData\Local\Temp\opencode\r6rev\r6b__evan4ql\live_run.lock

LOCK exists after pytest refusal: True
LOCK unchanged after pytest refusal: True
lock purpose still live: True
==================== CMD-3  (release LIVE)
LIVE exit code: 0 stdout: LIVE_RELEASED stderr:
LOCK exists after LIVE exit: False
==================== VERDICT-B
B_PASS
DRIVER-EXIT=0
```

判读：pytest **exit 2**、0.8s 内退出（未进入测试阶段，输出无 `1 passed`）、拒绝原因明确指向
live 持锁；live 的锁内容与 mtime 语义未变、仍在（**pytest 拒跑时没有删 live 的锁**）。
**残留②的「删了 live 的锁」半边被实证否掉。**

### (c) 释放路径在「继任者已接管」时不得删继任者的锁（两个变体）

统一序列：PY1 取锁 → 父进程 `lockf.unlink()`（模拟人工清锁 / 崩溃残留自愈 unlink，正是
`app/live_guard.py:325` 拒绝信息里指引人工做的动作）→ 继任者取锁 → 放行 PY1 让其释放路径执行 → 检查。

变体 c1（**继任者同为 pytest ⇒ purpose 相同，只有 pid 回读能救**）原始输出：

```
CMD-1  spawn real pytest #1 (holds lock):
  PY1 pid: 36904  lock: {"purpose": "pytest", "pid": 36904, "started_at": "2026-09-26T10:19:19"}
CMD-2  simulate human clear / crash-residue unlink:  lockf.unlink()
  lock exists after unlink: False
CMD-3  spawn real pytest #2 (takes over the SAME lock, purpose='pytest')
  PY2 pid: 40896  lock: {"purpose": "pytest", "pid": 40896, "started_at": "2026-09-26T10:19:20"}
CMD-4  release PY1 -> its pytest_sessionfinish/atexit release runs now
  PY1 exit code: 0 | stdout tail: .                                                                        [100%]
1 passed, 5 deselected in 1.20s
  >>> successor lock still present: True
  >>> lock content now: {"purpose": "pytest", "pid": 40896, "started_at": "2026-09-26T10:19:20"}
  >>> successor (pid 40896) lock intact: True
CMD-5  release successor
  successor exit code: 0 | stdout:                                           [100%]
1 passed, 5 deselected in 0.29s
  lock exists after successor exit: False
```

变体 c2（**继任者为真实 live 进程 ⇒ purpose 与 pid 都不同**）原始输出：

```
CMD-1  spawn real pytest #1 (holds lock):
  PY1 pid: 41888  lock: {"purpose": "pytest", "pid": 41888, "started_at": "2026-09-26T10:19:21"}
CMD-2  simulate human clear / crash-residue unlink:  lockf.unlink()
  lock exists after unlink: False
CMD-3  spawn real LIVE process (takes over the lock, purpose='r6-review-live')
  LIVE pid: 38612  lock: {"purpose": "r6-review-live", "pid": 38612, "started_at": "2026-09-26T10:19:22"}
CMD-4  release PY1 -> its pytest_sessionfinish/atexit release runs now
  PY1 exit code: 0 | stdout tail: .                                                                        [100%]
1 passed, 5 deselected in 0.33s
  >>> successor lock still present: True
  >>> lock content now: {"purpose": "r6-review-live", "pid": 38612, "started_at": "2026-09-26T10:19:22"}
  >>> successor (pid 38612) lock intact: True
CMD-5  release successor
  successor exit code: 0 | stdout: LIVE_RELEASED
  lock exists after successor exit: False
==================== VERDICT-C
  c1-same-purpose: C_PASS
  c2-live-successor: C_PASS
```

判读：两种继任者都保住。c1 是**关键变体**——`purpose` 都是 `pytest`，唯一拦住误删的就是
`app/live_guard.py:240` 的 pid 回读；这也正是「人工清锁」指引所打开的路径。
⇒ **残留①「释放只删自己的锁」不成立。**

### (d) 残留②的另一半：裸 `pytest` 确实会创建并删除**真实生产锁路径**（非破坏性证明）

在**临时沙箱**（`<temp>/r6rev/sb`，`app/`+`scripts/`+三个 live_guard 测试文件为源检出逐字节副本，
`diff -q` 全部 `IDENTICAL`）里以 `ROOT=<沙箱>` 运行，**故意不设 `LG_LOCK_DIR` / `LG_DATA_DIR`**，
于是 `config.ROOT` = 沙箱 ⇒ `prod_lock_path()` = 沙箱 `data/live_run.lock`，**源检出零触达**：

```
sandbox ROOT (=config.ROOT for this run): C:\Users\6\AppData\Local\Temp\opencode\r6rev\sb
resolved prod_lock_path()               : C:\Users\6\AppData\Local\Temp\opencode\r6rev\sb\data\live_run.lock
sandbox data/ exists before run         : False
==================== CMD  bare pytest, NO LG_LOCK_DIR / NO LG_DATA_DIR
prod lock CREATED by the running pytest: True
prod lock content                    : {"purpose": "pytest", "pid": 26888, "started_at": "2026-09-26T10:19:36"}
sandbox data/ dir created            : True
pytest exit code: 0 | stdout tail: .                                                                        [100%]
1 passed, 5 deselected in 0.08s
prod lock DELETED by pytest release  : True
```

判读：**「跑测试会碰到真实生产锁路径」成立且是设计意图**（否则 P1 错位复发）——裸 `pytest`
会创建 `<config.ROOT>/data/live_run.lock`、跑完再删掉它，并顺带创建 `data/` 目录
（本 worktree 目前**没有** `data/` 目录，见 §6，这也是本轮所有实验都必须带 `LG_LOCK_DIR` 的原因）。
但结合 (b)(c2)：它**只会删自己写下的那把锁**，绝不删/改 live 正在持有的锁。

### (e) 逃逸口：`LG_DATA_DIR` 单独设置 ⇒ 真实重叠（文档已声明，测试未钉死）

沙箱内无 `LG_LOCK_DIR` 的真实 pytest 持生产位锁，再拉起一个**只多设了 `LG_DATA_DIR`** 的真实 live 进程：

```
sandbox prod lock path (pytest side): C:\Users\6\AppData\Local\Temp\opencode\r6rev\sb\data\live_run.lock
alt LG_DATA_DIR for live child       : C:\Users\6\AppData\Local\Temp\opencode\r6rev\r6e_altdata_5p8lol2u
pytest holds prod lock: {"purpose": "pytest", "pid": 41736, "started_at": "2026-09-26T10:21:46"}
CMD live child WITH LG_DATA_DIR set:
  exit code: 0
  stdout: LIVE_LOCK_PATH C:\Users\6\AppData\Local\Temp\opencode\r6rev\r6e_altdata_5p8lol2u\live_run.lock
LIVE_STARTED
  stderr:
  >>> OVERLACH REACHED (pytest lock + live lock both held): True
  prod lock after pytest exit: False
E_OVERLAP_TRUE
```

判读：live 进程 exit 0 且 `LIVE_STARTED` 打出——**pytest 整轮持锁期间仍被 live 叠上**，
根因即 §2.3 的路径分叉。这是 `app/live_guard.py:36-37` 已自报的「残留窄窗 2」，
在册测试无一钉死它（`tests/test_live_guard_mutex.py:129-131` 只断言 live 侧跟随 `DATA_DIR`，
不断言任何安全性质）⇒ OPEN-3。

### (f) 硬杀等价路径（`os._exit` ≡ SIGKILL / `taskkill /F`）：15s 宽限内拦、超宽限自愈

```
CMD-1 hard-killed pytest exit code: 7 (os._exit(7))
  lock left behind: {"purpose": "pytest", "pid": 29692, "started_at": "2026-09-26T10:22:02"}
CMD-2 next pytest run INSIDE the 15s grace (dead pid, fresh mtime):
  exit code: 2
  output: Exit: [live/pytest 互斥守卫] live 实跑进行中（{'purpose': 'pytest', 'pid': 29692, ...}）——拒绝 全量 pytest（R6：全绿结论不许被并发 live 污染）。等 live 结束；若 live 已崩溃遗留死锁，人工核实后清除：C:\Users\6\AppData\Local\Temp\opencode\r6rev\r6f_buse_u4n\live_run.lock
  lock still present (bricking): True
CMD-3 wait out CORRUPT_LOCK_GRACE_SECONDS then retry:
  exit code: 0 | stdout tail: .                                                                        [100%]
1 passed, 5 deselected in 0.02s
  lock exists after self-healed run: False
F_GRACE_BLOCKS=True  F_SELFHEAL=True
```

判读：与 `app/live_guard.py:38-49` 的自述完全一致（死 pid + 新鲜 mtime ⇒ 按「有人持有」拦；
超 15s ⇒ 自愈接管并正常跑完）。**自愈不会误删活锁**（活锁 pid 存在 ⇒ 恒拦）。

## 4. 反向验证（变异 → 判红 → 恢复复绿）

变异**只施加在临时沙箱副本**上（`C:\Users\6\AppData\Local\Temp\opencode\r6rev\sb\app\live_guard.py`），
**源检出与本 worktree 的产品代码一字节未改**。变异内容＝去掉释放路径的 pid 回读校验
（变异标记刻意不写连写形式，见 §7 卫生说明）：

```diff
--- F:/agi/language-genome/app/live_guard.py
+++ <sandbox>/app/live_guard.py
@@ -237,7 +237,7 @@
         return                                # 锁已被人工清除——无事可做
     except Exception:                         # noqa: BLE001 —— 损坏：不删（宁拦不猜）
         return
-    if info.get("pid") == os.getpid() and info.get("purpose") == purpose:
+    if info.get("purpose") == purpose:  # <变异标记：去掉 pid 回读>
         try:
             p.unlink()
         except FileNotFoundError:
```

### 4.1 判红：同一实验 (c1) 立刻抓到「前实例删掉继任者的锁」

```
SANDBOX ROOT: C:\Users\6\AppData\Local\Temp\opencode\r6rev\sb
CMD-1 spawn real pytest #1 (MUTATED sandbox app):
  PY1 pid: 33400  lock: {"purpose": "pytest", "pid": 33400, "started_at": "2026-09-26T10:20:53"}
CMD-2 simulate human clear: lockf.unlink()
CMD-3 spawn real pytest #2 (successor, same purpose='pytest')
  PY2 pid: 39016  lock: {"purpose": "pytest", "pid": 39016, "started_at": "2026-09-26T10:20:54"}
CMD-4 release PY1 -> PY1 release path runs with pid readback REMOVED
  PY1 exit code: 0
  >>> successor lock still present: False
  >>> lock content now: None
  >>> successor (pid 39016) lock intact: False
CMD-5 cleanup successor
==================== VERDICT-C1 on MUTATED sandbox
C1_FAIL  <-- REPRODUCED: predecessor deleted the successor's lock
```

危害不是「多删一个文件」：PY2 仍以为自己持锁（整轮 pytest 已在跑），而锁文件已消失 ⇒
**第三个 live 进程可以无阻进入并与 PY2 重叠**——互斥被无声破坏。

### 4.2 在册 25 个 live 守卫用例对该变异**全绿**（覆盖缺口实证）

```
### COMMITTED SUITE against the pid-readback-REMOVED sandbox ###
======================= 25 passed, 8 warnings in 5.02s ========================
COMMITTED-SUITE-EXIT=0
```

原因（读码可解释）：`tests/test_live_guard.py:68-80` 与 `tests/test_live_guard_deadpid.py:259-271`
两处「保守释放」用例里，**前任与继任者都在同一个进程**（`os.getpid()` 相同、只有 purpose 不同），
所以承重的是 purpose 分支，pid 分支在这些用例里是死代码。⇒ 变异杀不掉 ⇒ **OPEN-1**。

### 4.3 恢复后复绿

```
--- restore check (diff sandbox vs source, expect empty):
RESTORED: byte-identical
--- source checkout <变异标记> count:      (0)      # 原文此处是 grep -c <标记>，见 §7 卫生说明
### COMMITTED SUITE after restore ###
======================= 25 passed, 8 warnings in 5.06s ========================
RESTORED-SUITE-EXIT=0
```

同一实验 (c1) 在恢复后的沙箱复跑：

```
  PY1 pid: 38664  lock: {"purpose": "pytest", "pid": 38664, "started_at": "2026-09-26T10:21:27"}
  CMD-2 lockf.unlink() done
  PY2 pid: 41564  lock: {"purpose": "pytest", "pid": 41564, "started_at": "2026-09-26T10:21:28"}
  PY1 exit code: 0
  >>> successor lock still present: True | content: {"purpose": "pytest", "pid": 41564, "started_at": "2026-09-26T10:21:28"}
  >>> successor (pid 41564) lock intact: True
  lock exists after successor exit: False
C1_PASS (restored)
```

`C1_FAIL(变异) → C1_PASS(恢复)` 成对，**反向验证闭合**。沙箱已 `rm -rf` 清空。

## 5. 在册测试覆盖度评估（本轮独立核，结论=「部分成立」）

| 场景 | 在册用例 | 是不是真跨进程 | 缺口 |
|---|---|---|---|
| live↔live 互斥 | `tests/test_live_guard.py:48`、`:217` 双子进程赛跑 | 是 | — |
| 方向 A：pytest 持锁 ⇒ live 被拒 | `tests/test_live_guard_mutex.py:140-157` | **否**（pytest 侧是进程内 `whole_run_lock(watch=...)` 替身，`:145`） | 缺「真实 pytest 进程持锁 + 真实 live 抢锁」端到端一条；本轮实验 (a) 已补上但**未入册** |
| 方向 B：live 持锁 ⇒ pytest 拒跑 | `tests/test_live_guard_mutex.py:162-193` | **是**（真 `python -m pytest` 子进程） | — |
| 方向 C：pytest 收尾无残留 | `tests/test_live_guard_mutex.py:198-212` | 是 | — |
| 测试阶段确实在持锁 | `tests/test_live_guard_mutex.py:246-254`（child_probe 校验 pid） | 是 | — |
| 释放不删继任者锁（**跨进程**） | **无** | — | 两处在册用例都是同进程；c1/c2 两变体**未入册** |
| `LG_DATA_DIR` 分叉导致重叠 | **无** | — | 实验 (e) 已复现；未入册 |

## 6. 纪律与副作用核验

```
$ cd F:/agi/_scratch/worktrees/audit-residual-3 && git status --porcelain
(仅本报告文件)
$ test -d data && echo YES || echo "NO (bare pytest here would CREATE data/live_run.lock)"
NO (bare pytest here would CREATE data/live_run.lock)
$ cd F:/agi/language-genome && git status --porcelain
(clean)
$ test -e data/live_run.lock && echo "PROD LOCK PRESENT" || echo "PROD LOCK STILL ABSENT (never touched)"
PROD LOCK STILL ABSENT (never touched)
```

- 源检出**零改动**、生产锁文件**全程未被创建/删除/改写**（实验前后各核一次）。
- 本 worktree 唯一新增文件＝本报告；**无任何脚本/夹具/临时文件留在树内**（驱动脚本全部经
  `python - <<'PY'` 从 stdin 读取；沙箱建在 `C:\Users\6\AppData\Local\Temp\opencode\r6rev` 并已删除）。
- 未 commit / 未 merge / 未 push；零模型调用。

## 7. 卫生说明：变异标记的连写形式

本报告**刻意不写**该 10 字母变异标记的连写形式，以免污染仓库级 `grep -c` 卫生门。
基线（**本轮之前既有**，与本次审查无关，位于 5 个既有文档共 8 行）：
`docs/K5晋升接线探针_20260925.md:1`、`PORT_blind_concurrency.md:3`、
`PORT_nsent_direction_gate.md:2`、`PORT_promotion_wiring_probe.md:1`、`REVIEW_PROBE_LEDGER.md:1`。
本报告新增 **0** 处 ⇒ 全树计数仍为 **8**（与基线一致）；源检出 `app/live_guard.py` 计数为 **0**。

## 8. 未自跑（如实标注）

以下段落**未经本轮命令实跑**，结论只来自读码或引用在册用例，不得当作实测：

1. **未自跑**：`tests/test_live_guard.py:126`（k2 driver 守卫前置）——需要 `scripts/k2_extract_backfill.py`
   及其网关依赖；本轮只把它当作已入库用例引用，未独立复跑。
2. **未自跑**：除三个 live_guard 文件外的**全量 pytest 套件**（其余 ~200 个测试文件）零触达。
3. **未自跑**：`atexit` 兜底释放的**独立验证**。实验 (a)(c) 的释放走的是 `pytest_sessionfinish` 正常路径；
   我没有构造「`sessionfinish` 被跳过但 `atexit` 仍跑」的进程（`os._exit` 会连 `atexit` 一起跳过，
   见实验 f）。`tests/conftest.py:44-48,65` 的幂等性只经读码确认。
4. **未自跑**：`SIGKILL` / `taskkill /F` / 断电的真实信号路径。实验 f 用 `os._exit(7)` 做等价替身
   （同样跳过 `sessionfinish` 与 `atexit`、同样留下含死 pid 的锁），**不是**真信号。
5. **未自跑**：POSIX 行为。全部实验跑在 `win32`，`_pid_looks_live` 的 POSIX 分支
   （`ProcessLookupError`）与 `/proc` 语义未被本轮触及。
6. **未自跑**：多 worktree / 多机共享同一 `ROOT/data` 的场景，以及真实 439MB 生产库被并发
   读写的影响（守卫只管锁，不管 DB 层）。
7. **未自跑**：`LG_LOCK_DIR` 指向**不可写/不存在且无法创建**的目录时的失败形态
   （`_acquire_lock` 的 `p.parent.mkdir` 抛 `PermissionError` 会绕过 `SystemExit` 契约）。

## 9. 剩余 OPEN 清单（每条附可机械执行的收口判据）

| id | 事项 | 严重度 | 机械收口判据（可复制执行） |
|---|---|---|---|
| OPEN-1 | 释放路径的 **pid 回读分支无在册测试**；两处「保守释放」用例是同进程，删掉 pid 校验仍 25/25 全绿 | 中（承重墙无回归网） | 往 `tests/test_live_guard_mutex.py` 加一条**跨进程同 purpose** 用例：PY1 持锁 → `lockf.unlink()` → PY2 取锁 → 放行 PY1 → 断言 `lockf` 仍存在且 `pid==PY2.pid`；随后把 `_release_lock` 的 pid 条件去掉，该用例必须转红 |
| OPEN-2 | 方向 A 的 pytest 侧是进程内替身（`tests/test_live_guard_mutex.py:145`），缺「真实 pytest 持锁 + 真实 live 抢锁」端到端一条 | 低-中 | 加一条用例：真实 `python -m pytest` 子进程（阻塞在 `pytest_runtest_call` post-yield）持锁期间拉起 live 子进程，断言 live exit≠0、`互斥守卫` 在输出、锁内容未变；把 `tests/conftest.py:61` 的 `whole_run_lock` 调用去掉后该用例必须转红 |
| OPEN-3 | `LG_DATA_DIR` 单独设置 ⇒ 两侧锁位分叉 ⇒ **真实重叠**（实验 e 已复现），在册测试零覆盖，代码只在 `app/live_guard.py:36-37` 自述 | 中 | 二选一并落测试：(a) 在 `live_lock.__enter__`/`refuse_if_live_running` 里当 `LG_DATA_DIR` 被显式设置且 `LG_LOCK_DIR` 未设置时按「口径分叉」硬拦并给出指引；或 (b) 加用例断言分叉时守卫必须拒绝。判据：设 `LG_DATA_DIR=<tmp>` 后跑 `--live` 入口，exit≠0 且输出含口径分叉原因 |
| OPEN-4 | 裸 `pytest` 会在 `config.ROOT` 下**创建** `data/` 与 `data/live_run.lock` 再删除（本 worktree 无 `data/`），对「不在源检出里跑套件」的 reviewer/agent 是意外副作用 | 低 | 收口判据：要么在 `conftest.py` 顶部对「`ROOT/data` 不存在且 `LG_LOCK_DIR` 未设」打一条 warning，要么文档化「跑套件必须设 `LG_LOCK_DIR`」。判据文本：在干净 worktree 里裸跑 `pytest -k child_probe` 后 `git status --porcelain` 出现 `?? data/` 即为未收口 |
| OPEN-5 | `tests/test_live_guard.py:39-45` 的 autouse 隔离夹具在 `LG_LOCK_DIR` 被设置时**失效**：`live_lock_path()` 里覆盖优先于 `monkeypatch.setattr(config,"DATA_DIR",...)`，前置断言直接炸掉整个文件（实测 11 errors） | 低 | 收口判据：`LG_LOCK_DIR=<tmp> pytest tests/test_live_guard.py` 必须 0 error（当前 11 errors）；修法二选一——夹具里 `monkeypatch.setenv("LG_LOCK_DIR", str(tmp_path))`，或夹具前置 `monkeypatch.delenv("LG_LOCK_DIR", raising=False)` |
| OPEN-6 | 硬杀残留：死 pid + 新鲜 mtime 会让整个套件 fail-fast exit 2 **15s**（实验 f 已实证，行为符合自述）；若 pid 被系统复用给无关进程则**永久**砖化（`_pid_looks_live` 按「存在」拦） | 低（设计取舍，注释已声明） | 收口判据：留一份运维处置 runbook（哪条命令看锁内 pid、`tasklist /FI "PID eq <n>"` 怎么确认），并在守卫拒绝信息里带上该命令；判据：拒绝信息含可复制的 `tasklist` 提示 |
| OPEN-7 | 本轮全部实验在 `win32` 上完成，POSIX 分支（`ProcessLookupError` 判死、`/proc` 语义）无覆盖 | 低 | 收口判据：在 POSIX 上重跑本报告 §3 的 (a)(b)(c1)(c2) 四条，结论方向一致；或加一条平台无关的单测用 `monkeypatch` 伪造 `os.kill` 抛 `ProcessLookupError` |

## 10. 收尾判词

- 守卫本体（两侧同锁路径、整轮持锁、双向 fail-fast、O_EXCL 原子性、pid+purpose 保守释放、
  死 pid/损坏锁 mtime 宽限自愈）在**读码 + 6 组真跑实验**下均成立，**未发现需要改产品代码的缺陷**。
- 会审侧两条残留：①「释放误删继任者的锁」**不成立**（实验 c1/c2 + 反向验证承重墙确认）；
  ②「跑测试会删/改 live 的锁」**不成立**（实验 b/c2），但「跑测试确实创建并删除真实生产锁**路径**」
  **成立**且属设计意图（实验 d），并附带发现 `LG_DATA_DIR` 逃逸口（实验 e）。
- 「双向真竞态已被真覆盖」只**部分成立**：在册方向 B 是真跨进程，方向 A 的 pytest 侧是替身，
  跨进程同 purpose 的释放安全**无在册用例**。三条已用本轮可复现的实验闭合，但**尚未入册**（OPEN-1/2）。
- 故判 **VERDICT: PASS**（无阻塞项），残留以 OPEN-1…OPEN-7 形式挂账，每条带机械判据。
