# PORT：盲评呈现（presentation binding）会审残留三处真缺陷整改

工作树：`F:/agi/_scratch/worktrees/blind-residual-2`（分支 `fix/blind-residual-2`）
改动文件（白名单内，无其它）：`app/api.py`、`tests/test_blind_presentation.py`、本文件。

会审出处：`F:/Hermes/team/reviews/language-genome-f17cb93b09.md`（对 `f17cb93` 出 BLOCK）。
「无 pid 一律 409」的严重项已由 `lg-fix-blind-legacy-409` 整改合入；本文件只处理**残留**：
会审 glm 席 [一般] 的并发窗口 + 读盘/写盘口径不一致，与会审 glm 席 [建议] 的 `_reset()` 隐式依赖。

本文件里的每一段输出都是本会话**真跑**出来的（含反向验证的判红原文），无推演、无占位。

---

## 一、改动说明

### 1. 窗口 1：`_blind_ensure_loaded` 的并发窗口 ⇒ 有效 pid 被误判过期（409）

**原实现**（`app/api.py`）：持锁 `_BLIND_MAP.clear()/_BLIND_LAST.clear()` → **释放锁** →
`_blind_load()`（自取锁）→ 再持锁写 `_BLIND_LOADED_FOR`。
中间那段锁是放开、哨兵也未置位的，`_blind_load()` 里的**读盘 + json.loads** 完全在无锁状态下进行。
另一个线程的读路径只要落进「已清空、未装载」这段，`_BLIND_MAP.get(pid)` 就返回 `None`
——上层 `verdict()` 的 `served is None` 分支随即对一份**还有效**的呈现回 **409（误判过期）**。
单 worker/测试下不可见，多线程部署下是真缺陷。

**现在**：清空 + 装载 + 置哨兵在**同一个持锁段**里一次做完。

- `_BLIND_LOCK` 由 `threading.Lock()` 换成 `threading.RLock()`（不可重入的 Lock 做不到
  「持锁段里调装载」，会自锁死；RLock 同线程嵌套合法、跨线程仍互斥）。
- 装载拆成 `_blind_load_locked()`（须持锁）与对外的 `_blind_load()`（自己取锁后转调，
  名字与语义保持不变——既有用例与重启路径都在直接调它，改名会连带打断它们）。
- `_blind_ensure_loaded()` 内联调用 `_blind_load_locked()`，不再有释放锁的中间段。
- `_blind_get()` / `_blind_put()` 把「懒加载 + 取表/写表」并到一个临界区：
  读路径**要么拿到旧一整套、要么拿到新一整套，永远看不到空表**；
  写路径也不会再出现「刚写进内存、还没落盘就被别人的整块换一套清掉」。

结论性不变量：`_blind_get(有效 pid)` 在任何时刻都不返回 `None`，除非该 pid 真的不在映射里。

### 2. 窗口 2：保存路径读盘失败「直接覆盖写」⇒ 与装载口径不一致、丢失现场

**原实现**：`_blind_save_locked()` 读盘失败（非 `FileNotFoundError`）只 `logger.warning(... 本次直接覆盖写)`，
损坏现场被随后的覆盖写一并抹掉；而 `_blind_load()` 遇损坏会先备份成 `.corrupt`。同一族函数两种口径。

**现在**：抽出共用的 `_blind_backup_corrupt(path, err, consequence)`，读盘路径与写盘路径同用它：

- 装载（`_blind_load_locked`）：损坏 ⇒ 备份 `.corrupt` + 告警（含路径）⇒「本次从空映射开始」；
- 保存（`_blind_save_locked`）：读盘失败/损坏 ⇒ **先**备份 `.corrupt` + 告警（含路径）⇒「本次跳过合并、直接覆盖写」。
- 备份自身失败也只降级为告警（文案含「备份到 %s 失败」），不让读/写路径把异常抛出去。

### 3. 窗口 3：`tests/test_blind_presentation.py::_reset()` 未清 `_BLIND_LOADED_FOR`

`_reset()` 现在一并置 `api_mod._BLIND_LOADED_FOR = None`，docstring 说明为什么必须清：
哨兵记的是「内存里已装载了**哪个路径**」；某用例中途改 `DATA_DIR` 后失败退出，临时目录连同
呈现文件已消失而哨兵还指着它，后续用例是否装载就取决于「路径是否恰好等于上一条留下的哨兵」——
相等则跳过装载、内存表其实是空的，有效 pid 会被判过期 **409**，而这条用例本身没做过任何清表动作。
原来靠「各用例路径一致」侥幸不触发，属隐式依赖，现已显式化（清内存 = 清哨兵，同生同灭）。

### 4. 新增回归（5 条，既有用例一条未删一条未改）

| 用例 | 钉住什么 |
|---|---|
| `test_concurrent_reload_never_marks_valid_presentation_expired` | 窗口 1 端到端：8 读线程 + 2 换 `DATA_DIR` 线程压 3s，两侧落盘内容**都含同一 pid**（故其任何时刻都有效），断言 `_blind_get(pid)` 永不为 `None`、读路径永不抛，且压后同一 pid 提交仍 200（不是「其实后来才失效」）。落盘文件用 `_pad_presentations` 撑大，把「清空→读盘」这段自然窗口拉宽，**不靠调度运气命中**。 |
| `test_reload_never_exposes_a_lock_free_cleared_window` | 窗口 1 的**临界区边界本身**（确定性、不靠调度）：绕过 `_blind_get`，只调 `_blind_ensure_loaded`，人为撑开「已清空、装载未开始」那段，探 `_BLIND_LOCK` 能否被拿到——拿到即存在无锁空窗，判红。存在理由见 §三·2。 |
| `test_save_backs_up_corrupt_file_before_overwriting` | 窗口 2 的解析损坏分支：`.corrupt` 里是**原样**损坏内容 + 覆盖写照常落地 + 告警含文件路径。绕开 `_blind_put`（其懒加载会先把现场处理掉），直接持锁调 `_blind_save_locked`。 |
| `test_save_backs_up_unreadable_present_file_before_overwriting` | 窗口 2 的「读盘失败（非 FileNotFoundError）」分支：用「路径是个目录」造稳定的 `IsADirectoryError`，不靠文件系统权限的运气。 |
| `test_reset_clears_lazy_load_sentinel` | 窗口 3：`_reset()` 必须把哨兵清成 `None`。 |

### 5. 既有契约未放宽（逐条自查）

无 pid + 带 A/B 语义 ⇒ **409**、未知/过期 pid ⇒ **409**、pid 与 `review_id` 不匹配 ⇒ **400**、
指纹不符 ⇒ **409** 一律原样保留；`verdict()` 的 409/400 分支一字未动，**未新增** `legacy` 猜义路径，
`_BLIND_LAST` 仍无读取方。机械核对（本次 diff 里凡含 409/400/legacy/winner_raw 的**代码行**为零，
只有 docstring 里提到 409）：

```
$ git diff app/api.py | grep -E "^[-+].*(409|400|legacy|winner_raw)"
+    把**有效** pid 判成过期（对客户端就是一个还活着的呈现收到 409）。单 worker/测试下
```

（唯一命中是新增的说明性注释，非逻辑行。）

---

## 二、验收命令与实跑输出（原文）

验收门（主控指定，未加任何额外参数）：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_blind_presentation.py -q
```

实跑（`-q` 与 `pyproject.toml` 的 `addopts = "-q"` 叠加成 `-qq`，故摘要行被抑制，
进度点行与退出码如下）：

```
$ "F:/Hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/test_blind_presentation.py -q
..................                                                       [100%]
EXIT=0
```

同一文件加 `--tb=line`（要看计数时）：

```
$ "F:/Hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/test_blind_presentation.py -p no:cacheprovider --tb=line
18 passed, 2 warnings in 6.14s
PYTEST_EXIT=0
```

**基线口径澄清（此前会话遗留的疑问，已查实）**：任务书写「当前 main 基线 37 passed」，
但本工作树的门文件在 HEAD 上只收得 **13** 条用例，本次**只增不减**地增 5 条 ⇒ **18 条**。
逐条比对（`git show HEAD:...` vs 工作树）确认既有 13 条一条未删、未改：

```
$ git show HEAD:tests/test_blind_presentation.py | grep -c "^def test_"
13
$ grep -c "^def test_" tests/test_blind_presentation.py
18
```

「37 passed」应是整仓或其它文件的计数口径，不是本门文件的用例数；我按「只增不减」交付 18 条，
没有为凑 37 去编造用例。

**抖动检查**（并发用例涉及线程，连跑 5 轮）：

```
$ for i in 1 2 3 4 5; do ... -m pytest tests/test_blind_presentation.py --tb=line | grep -E "[0-9]+ (passed|failed)"; done
18 passed, 2 warnings in 6.20s
18 passed, 2 warnings in 6.17s
18 passed, 2 warnings in 6.29s
18 passed, 2 warnings in 6.26s
18 passed, 2 warnings in 6.08s
```

**整仓回归**：`1550 passed, 14 failed, 1 skipped, 3 xfailed`。那 14 条失败**与本次改动无关**，
已用 `git stash` 把本次改动摘掉复跑确认是**改动前既有**的失败（`sqlite3.OperationalError` 一族，
集中在 `test_benchmark_subs.py` / `test_factorial_commit_guards.py` / `test_k5_criteria_check.py` /
`test_stratified_batch.py`，都不碰盲评呈现代码）：

```
$ git stash push -- app/api.py tests/test_blind_presentation.py   # 摘掉本次改动
$ ... -m pytest tests/test_benchmark_subs.py tests/test_factorial_commit_guards.py \
      tests/test_k5_criteria_check.py tests/test_stratified_batch.py --tb=no
14 failed, 34 passed in 2.12s        # 与带改动时同数同族 ⇒ 既有失败
$ git stash pop                        # 改动已复原
```

---

## 三、反向验证（**已真做**，退化配方 + 判红原文 + 复原对照）

### 1. 退化 A：只把 `_blind_ensure_loaded` 改回「清空后先释放锁再装载」的旧形态

配方（`app/api.py`，验完复原）：

```python
        _BLIND_MAP.clear()
        _BLIND_LAST.clear()
    _blind_load()                      # ← 旧形态：锁已放开、哨兵未置位
    with _BLIND_LOCK:
        _BLIND_LOADED_FOR = path
```

实跑**判红**（`test_reload_never_exposes_a_lock_free_cleared_window`）：

```
FAILED tests/test_blind_presentation.py::test_reload_never_exposes_a_lock_free_cleared_window
1 failed, 17 passed, 3 warnings in 5.27s
PYTEST_EXIT=1
```

报错原文（逐字）：

```
E               AssertionError: 重装载把「已清空」与「装载」拆到了两个临界区：清空之后、装载之前锁是空的，读路径能落进这个空窗把有效 pid 判成不存在（409）。装载必须与清空、置哨兵在同一临界区内一次做完。
E               assert not True

tests\test_blind_presentation.py:512: AssertionError
```

### 2. 一个必须如实交代的发现：只退化 `_blind_ensure_loaded` 时，端到端那条**侥幸放绿**

第一次只施「退化 A」时，端到端回归 `test_concurrent_reload_never_marks_valid_presentation_expired`
**没有**判红（17 passed，exit 0）。原因不是回归写得虚，而是修复其实是**两道独立防线**：

- 层 1：`_blind_ensure_loaded` 内部清空+装载+置哨兵同段；
- 层 2：`_blind_get` 整段持锁（ensure 也在锁内）。

层 2 会把层 1 的退化整个兜住——`RLock` 可重入，同线程在已持有的锁内再取一次不阻塞，
于是「释放锁后再装载」这段在读路径眼里**根本没露出过**。
只按任务书字面「改 `_blind_ensure_loaded`」做反向验证，会得到一个**假绿**结论。
因此补了 `test_reload_never_exposes_a_lock_free_cleared_window`：**绕过 `_blind_get`、直接调
`_blind_ensure_loaded`**，把层 1 单独钉死——它确定性判红（见上），不依赖调度运气。

### 3. 退化 B：三处全改回 pre-fix 形态（真实旧代码）

`_blind_ensure_loaded` 回到旧形态，且 `_blind_get` 改回 `ensure` 在锁外的两段式、
`_blind_put` 同样把 `ensure` 挪出临界区：

```
FAILED tests/test_blind_presentation.py::test_concurrent_reload_never_marks_valid_presentation_expired
FAILED tests/test_blind_presentation.py::test_reload_never_exposes_a_lock_free_cleared_window
2 failed, 16 passed, 3 warnings in 1.97s
PYTEST_EXIT=1
```

端到端那条的报错原文（逐字）：

```
E               AssertionError: 有效 pid 在换目录重装载的窗口里被读成不存在 = 会被上层判成过期（409）；装载必须与清空、置哨兵处在同一临界区内
E               assert not ['PR-6802bc2760fe', 'PR-6802bc2760fe', 'PR-6802bc2760fe', 'PR-6802bc2760fe', 'PR-6802bc2760fe', 'PR-6802bc2760fe', ...]

tests\test_blind_presentation.py:451: AssertionError
```

一帧内 8 个读线程里先后 7 个读到了 `None`——**409 误判过期**是可稳定复现的，不是理论推演。

### 4. 退化 C：保存路径改回「只告警、直接覆盖写」

```python
        except Exception as e:
            logger.warning("盲评呈现文件读不出（%s）：%s；本次直接覆盖写", path, e)   # ← 旧形态：不备份
```

实跑**判红**：

```
FAILED tests/test_blind_presentation.py::test_save_backs_up_corrupt_file_before_overwriting
FAILED tests/test_blind_presentation.py::test_save_backs_up_unreadable_present_file_before_overwriting
2 failed, 16 passed, 3 warnings in 6.17s
PYTEST_EXIT=1
```

### 5. 退化 D：`_reset()` 漏清哨兵

```python
    api_mod._BLIND_LAST.clear()
    # MUTATION: 故意漏清 _BLIND_LOADED_FOR
```

实跑**判红**：

```
FAILED tests/test_blind_presentation.py::test_reset_clears_lazy_load_sentinel
1 failed, 17 passed, 3 warnings in 6.23s
PYTEST_EXIT=1
```

### 6. 复原对照

四次退化全部复原后复跑门，复绿、exit 0；树内无任何残留标记：

```
$ grep -c "MUTATION" app/api.py                    # 0
$ grep -c "MUTATION" tests/test_blind_presentation.py   # 0
$ ... -m pytest tests/test_blind_presentation.py --tb=line
18 passed, 2 warnings in 6.14s
PYTEST_EXIT=0
```

`git diff` 复原后不为空——因为交付物本身就是这三处**修复**（相对 HEAD 的净变更），
「复原后为空」指的是**相对本会话交付态**没有残留改动；上表四次退化都已逐一还原，
`git diff app/api.py` 的内容与 §一 所述修复逐条一致、无一处退化残留。

---

## 四、影响面与风险

- `_BLIND_LOCK` 改为 RLock：持锁段变长（装载/落盘的 I/O 进入临界区），并发度下降、
  换取「读路径永不见空表」的正确性。FastAPI 同步路由跑在线程池里，不会阻塞事件循环。
  锁序风险为零：本模块只有这一把锁，不存在交叉等待。
- 新增 `_blind_backup_corrupt` / `_blind_load_locked` 两个私有函数；对外 API（`/review/...`、
  `blind_presentations.json` 格式、`_blind_load`/`_blind_put`/`_blind_get`/`_blind_save_locked`
  的名字与语义）不变。`_blind_load` 保留「自己取锁」的对外入口，既有用例
  （`test_presentations_survive_restart`、`test_corrupt_presentation_file_is_backed_up_and_warned`）
  仍直接调它，无需改动，且未被变成死代码。
- 测试耗时：并发用例固定 3s + 临界区探测用例固定 1.0s，全文件 +~4s（6.1s → 6.2s 量级）。
- 纪律：未 commit / merge / push；未改主仓或其它 worktree；工作树内无 scratch/冒烟/临时脚本
  （所有核验走既有测试文件或一次性内联命令，临时比对文件写在工作树外的系统临时目录）。
