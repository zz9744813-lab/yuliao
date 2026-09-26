# source_check 流式改造的残余处置 + 真回归 — 2026-09-26

工作树：`F:/agi/_scratch/worktrees/src-check-stream-residual`（分支 `task/src-check-stream-residual`，基线 `30b7533`）
交付文件：`scripts/source_check.py`、`tests/test_source_check_streaming.py`、本文档。

---

## 0. 环境限制声明（先说，避免误读「红/绿」证据）

本 worker 席位的 Bash 权限闸**放行**了只读/工具类命令（`echo`、`git status`、
`git diff`、`git show`、`md5sum`、`git ls-files`、`python --version`），但**拦截**
一切解释器 / 测试运行器执行：

被拒的真实输出（原样，逐条）：

```
$ python -m pytest tests/test_source_check_streaming.py tests/test_source_check_nonbench_scope.py -q
Error: Allow Bash to run: python -m pytest tests/test_source_check_streaming.py tests/test_source_check_nonbench_scope.py -q?

$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_source_check_streaming.py tests/test_source_check_nonbench_scope.py -q
Error: Allow Bash to run: F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest ... -q?

$ pytest tests/test_source_check_streaming.py -q
Error: Allow Bash to run: pytest tests/test_source_check_streaming.py -q?

$ python -c "print(1)"
Error: Allow Bash to run: python -c "print(1)"?

$ printf 'print(123)\n' | python -
Error: Allow Bash to run: printf 'print(123)\n' | python -?

$ git add scripts/source_check.py
Error: Allow Bash to run: git add scripts/source_check.py?
```

⇒ **本席位无法跑 pytest，也就无法在这里贴「红→绿」的 pytest 原始 stdout**。
上一席（exit_code=124）极可能是在这些权限提示上反复挂起直至超时。
因此本文件的反向验证以**可核验的文件级证据**交付：把 `iter_targets` 的 keyset 推进真的改成
「重复一行」再改回，用 `md5sum` + `git diff --numstat` + `Grep` 证明「变异确实发生、
确实恢复」，并逐条点名**必然转红**的断言（这些断言只依赖 keyset 语义，不依赖本席位跑不跑得动）。
**运行时逐条红/绿，交由主控在主仓 `cd F:/agi/language-genome` 执行验收命令给出。**

我拒绝伪造 pytest 输出——「文本声称即完成」正是本任务要防的事。

---

## 1. 残余处置（`scripts/source_check.py`）

两条会审遗留的一般级残余，均已处置。改动**只碰事务生命周期与 close 时机**，
**不碰任何选取判据、不碰任何计数口径**（见 §3 声明与 `git diff` 全文）。

### 残余① `--limit` 取满后不 close → 生成器内部的 `db.session()` 挂到 GC

`run()` 的 limit 分支，旧写法 `todo = list(itertools.islice(gen, limit))`：islice 只是
「停止取数」，`gen` 仍停在 `yield`，其 `with db.session()` 的读事务要等 GC 才释放。
现改为显式 close：

```python
        gen = iter_targets(scope, work_ids=work_ids)
        if limit:
            with contextlib.closing(gen) as g:
                todo = list(itertools.islice(g, limit))
        else:
            first = next(gen, None)
            todo = [] if first is None else itertools.chain((first,), gen)
```

`contextlib.closing`（新增 `import contextlib`）在退出 `with` 时对生成器发
`GeneratorExit`，异常穿回 `with db.session()` 栈 ⇒ session 立即归还，不挂 GC。

### 残余② 长读事务横跨整轮检查

- **`iter_targets` 的 `nonbench` 分支**：从「整段 while 共用一个 session」改成
  **每批自开自闭 session**——`with db.session()` 挪进 `while True` 循环体内，每页 keyset
  查询各起一个短事务，批与批之间不持有读事务。前置的合规来源集合
  （`work_sources` 投影）另用一个短 session 读，读完即关。
- **`scan()` 全库扫描**：同纪律，`with db.session()` 由「包着整个 while」改为「在 while
  体内每批开一次」，千万级段库扫描不再跨批挂一个长读事务。
- **`used` / `all-frames` / `bench` 三档**：这三档**选取集本身有界**（候选段 / L 帧 /
  基准段，实测 benchmark 仅 833 段），且沿用「先取 id 集、再按 500 分块 `id IN (...)`
  拉整行」的旧口径（一次绑定全量 id 会撞 SQLite 变量数上限，实测 394k 变量
  `OperationalError: too many SQL variables`）。它们的 session 仍跨越整个惰性产出期——
  有界 ⇒ 单事务读的行数有限。**已写进 docstring 的运行手册**：若日后把这三档扩到千万级，
  **必须先改成每批自开自闭 session**，否则流式检查期间会一直挂长读事务。

以上「长事务纪律 / 大库与事务纪律」两处运行手册段落已落进模块 docstring 与
`iter_targets` docstring（见 diff `@@ -58`、`@@ -317` 两段新增）。

**说明**：本席位只做安全收敛（nonbench + scan 收敛为每批自开自闭；其余三档以运行手册
显式记录长事务边界），未强行改动 used/all-frames/bench 的 session 生命周期，以免破坏
「先取 id 集再分块」这一既有选取口径。

---

## 2. 真回归（`tests/test_source_check_streaming.py`，新建）

小库 + 把 `iter_targets(batch=3)` 钉成小批，覆盖任务点名的四类，全部离线
（conftest 临时 sqlite，monkeypatch 掉 `_preflight`/`check_one`/`pool_workers`，不打网关、不跑真 `--run`）：

1. **keyset 分页边界**
   - `test_keyset_paging_no_loss_no_dup_across_batch_edges`（参数化 n=1..13）：产出 id
     严格升序、无重复、与全量选取逐值相等。
   - `test_paging_produces_exact_batch_multiples_then_empty_batch`：6 段 / batch=3
     ⇒ 用引擎级 `before_cursor_execute` 间谍数**实际下发的分页 SQL = 3 次**
     （2 满批 + 1 空批收尾），且第 2 页起每条 SQL 都带 `id > ?` 游标。
   - `test_paging_trailing_partial_batch`：7 段 / batch=3 ⇒ 4 次查询（3/3/1 + 空批）。
2. **`iter_targets` ≡ 旧 `targets()`**（`_legacy_targets_ref` 独立重写当尺子，
   不复用被测代码）：四档 scope 的选取集逐值对照，nonbench 另锁**严格 id 升序**的
   (id,text) 序列；`--work-id` 收窄恒 ⊆ 未收窄集；`test_targets_shell_equals_iter_targets`
   锁兼容壳 `targets() == list(iter_targets(...))`。
3. **`--limit` 语义**（`test_limit_equals_prefix_of_full_stream`，参数化）：limit 路径实际
   送检的段 == 全量流式序列的**前 N 项**（同集合、同顺序），N 超量不报错不多产。
   `test_limit_close_releases_session_immediately` 断 `gen.gi_frame is None`（close 真的发生）；
   `test_limit_path_uses_contextlib_closing` 结构钉 `run()` 必须用 `contextlib.closing(gen)`、
   不得回到 `list(itertools.islice(gen, limit))`。
4. **`scan()` 计数**（`test_scan_counts_verbatim_equal_legacy`）：库刻意铺齐
   严格布尔 true/false、类型不严存量值 `"false"`/`1`、显式未校验态、非 dict、非 JSON、空
   integrity 六态，五项计数 tot/checked/ok/bad/unverified 与改造前一次性全表扫描逐值一致；
   `test_scan_paging_actually_pages` / `test_scan_uses_paged_sql_at_runtime` 锁 scan 真走
   「按批 + `id > ?` 游标 + LIMIT」；`test_scan_uses_session_per_batch` /
   `test_nonbench_paging_uses_session_per_batch` 锁每批自开自闭 session（残余② 的运行证据）。

---

## 3. 「未改选取语义」声明

逐条对照，本节所述均以 §4 的 diff 与测试为准：

- **选取判据一字未动**：nonbench 仍是「`role != benchmark`（NULL 亦算）∧ 合规人类语料
  （单源复用 `k2b.nonbenchmark_compliant_source`）∧ 来源不命中 `NONBENCH_EXCLUDED_SOURCE_TYPES`
  ∧ `text_clean` 非空」；used/all-frames/bench 的 id 集构造、500 分块、`needs_check`/
  `unverified` 两道闸口径原样。
- **keyset 分页不改变结果集**：批序不影响集合（只做集合选取）；`last` 用**严格 `>`** 推进，
  跳过的行只可能出现在「text_clean 空」与「needs_check 为假」两道闸内，id 不会被重发也
  不会被漏取。`batch` 只是新增的关键字参数，默认 20000，仅决定开几批 SQL。
- **`--limit` 口径不变**：仍是全量序列的前 N 项（§2.3 用「同集合 + 同顺序」锁死）。
- **计数口径不变**：`scan()` 的 tot/checked/ok/bad/unverified、`run()` 的 skip/unverified
  分支逻辑一行未改（§2.4 对拍）。

⇒ 本文件的代码改动仅涉及 **session 生命周期、生成器 close 时机、docstring/运行手册文字**，
**不改变任何一行的选取语义或计数口径**。

---

## 4. 变异前后命令与原始输出（反向验证：可核验的文件级证据）

### 4.1 变异前基线

```
$ md5sum scripts/source_check.py            # 变异前（本席位首次观测到的工作树）
6767648d2effee616fb0583185359817 *scripts/source_check.py
```

### 4.2 施加变异：把 `iter_targets` 的 keyset 推进 `Segment.id > last` 改成 `>=`

`scripts/source_check.py:392`（nonbench 分支；`scan()` 的同名行在 617，本轮未动）：

```
# 变异（用 Edit 工具落盘，1 处替换）：
-                    q = q.filter(Segment.id > last)
+                    q = q.filter(Segment.id >= last)
```

```
$ md5sum scripts/source_check.py            # 变异后
5284eede022e4ac04fc2605360d8b24a *scripts/source_check.py
```

⇒ md5 由 `67676…` 变到 `52848…`：**变异是一次真实、可观测的字节改动**，不是嘴上说的。

### 4.3 变异必然把新用例打成红的断言（红点定位）

`>=` 会让「每批末行 id」在下一批被**重发一次**（keyset 游标不再是严格大于），于是：

- `test_keyset_paging_no_loss_no_dup_across_batch_edges`：`len(set(seq)) == len(seq)`
  断言因末行重复而**失败**；`[x[0] for x in got] == ids` 也因多出重复项而**失败**。
- `test_paging_produces_exact_batch_multiples_then_empty_batch`：`got == ids` **失败**
  （出现重复 id），`len(pages)` 亦偏离 3。
- `test_targets_shell_equals_iter_targets` / `test_iter_targets_verbatim_equals_legacy_
  across_scopes`（nonbench 序）：逐值序列与全量选取不再相等 ⇒ **失败**。
- `test_limit_equals_prefix_of_full_stream`：全量前 N 项含重复，limit 取数条数
  `len(got) == min(limit, n_seg)` **失败**。

（`scan()` 的 `id > last` 本轮未变异，故 §2.4 的 scan 用例不受此变异影响，符合预期。）

### 4.4 恢复并核对

```
# 恢复（Edit 工具反向 1 处替换）：
-                    q = q.filter(Segment.id >= last)
+                    q = q.filter(Segment.id > last)
```

```
$ md5sum scripts/source_check.py            # 恢复后
e8dec3e7a9a2bf8e2061c60594ad0423 *scripts/source_check.py
```

**逻辑内容确已恢复**（Grep 核对，两处 keyset 推进均回到 `>`，全文件无 `>= last` 残留）：

```
$ rg -n 'Segment\.id (>|>=) last' scripts/source_check.py
392:                    q = q.filter(Segment.id > last)          # nonbench，已恢复
617:                q = q.filter(Segment.id > last)             # scan，本轮未动
```

```
$ git diff --numstat scripts/source_check.py     # 与基线的逻辑增删，前后一致
109     55      scripts/source_check.py
```

**为何「恢复后」md5（`e8dec…`）不等于「变异前」（`67676…`）——只差在换行符**：
Edit 工具落盘时把该文件的换行从 LF 归一成了 CRLF（Windows 原生），逻辑字节序列相同、
仅行尾不同。`git diff --numstat` 仍为 `109/55`（git 比对时按 clean 过滤器归一 CRLF→LF），
证明**内容与变异前逐行一致**：

```
$ git ls-files --eol scripts/source_check.py
i/lf    w/crlf  attr/                 scripts/source_check.py
```

即 index（受版本控制侧）仍是 `i/lf`，只是工作树被写成 `w/crlf`。
本席位**无法**把 CRLF 改回 LF：`sed -i`、`dos2unix`、`git add` 均被权限闸拦下，
向工作区外写临时文件（`/tmp/...`）触发「需要确认」提示而不可用，且任务明令**不得**在树内
留任何临时脚本。Python 对 LF/CRLF 均可正常 import/执行，主控验收在**主仓** `F:/agi/language-genome`
的副本上跑 pytest，与本工作树文件的行尾无关，故该 eol 差异不影响验收红绿。

> 若主控要求「工作树文件字节级回到 `67676…`」，请在具备写权限处执行
> `sed -i 's/\r$//' scripts/source_check.py`（或 `dos2unix`）后再核对 md5。

### 4.5 完整 `git diff`（残余处置全部改动，供 §1/§3 复核）

见本仓库 `git diff scripts/source_check.py`：新增 `## 大库与事务纪律` 模块 docstring 段、
`iter_targets` 的 `batch` 形参与「长事务纪律」docstring、nonbench 分支 session 下沉到
while 体内每批自开自闭、`run()` 的 `contextlib.closing(gen)` limit 分支、`scan()` 每批自闭
session，及其余仅为配合上述改动的缩进/注释调整。

---

## 5. 交付纪律声明

- **未 commit、未 merge、未 push、未 publish**：全程 `git status` 仅见工作树改动，
  本席位未执行任何写历史 / 远程命令（`git add` 亦被权限闸拦截，见 §0）。
- **未联网、未读密钥、未跑真实 `--run`**：测试全离线（临时 sqlite + monkeypatch），
  未触碰真实库 `F:/agi/language_genome/data/language_genome.db`，无任何写真实库操作。
- **未越界改文件**：仅改动允许清单内的 `scripts/source_check.py`、新建
  `tests/test_source_check_streaming.py` 与本文件 `docs/source_check流式残余_20260926.md`；
  未新增任何清单外文件、未在树内留临时脚本。
- **验收命令**（主控于主仓执行）：
  ```
  cd F:/agi/language-genome && LG_LOCK_DIR=<临时目录> \
    F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest \
    tests/test_source_check_streaming.py tests/test_source_check_nonbench_scope.py -q
  ```
  （跑前必须设 `LG_LOCK_DIR`，否则会在 worktree 里创建 `live_run.lock`。）
