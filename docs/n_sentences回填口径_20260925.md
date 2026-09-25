# `n_sentences` 回填口径：`backfill_v2_sentences` 的声明-实现口径差（缺 `seg_version` 谓词）+ 回填后一致性校验 —— 2026-09-25

任务号：lg-nsent-version-pred｜worktree：`F:/agi/_scratch/worktrees/nsent-version`
（分支 `fix/nsent-version-pred`，基线 `07b3873`）｜本席：受限工席（只改 3 个文件，不 commit）
上游件：`docs/v2_句数口径_20260925.md`（补数脚本的出生文档）

## 0. 一句话结论

审查席（`lg-review-round-20260925` §1.6）读出的口径差属实：`scan()` 的查询**没有
`seg_version` 谓词**，「只补 v2」是叙述、全库扫 0 值非空段是实现。功能安全，但声明
与实现不吻合。本席把口径做实：①`scan(seg_versions=None)` **默认保持全库既有行为**
并在 docstring/CLI 帮助里把「全库」讲透，新增 `--seg-version 2` 显式版本作用域；
②新增**只读** `--verify` 一致性校验，逐 `seg_version` 报
`count(*) / sum(n_sentences>0) / avg(n_sentences) / 残留（n_sentences=0 且文本非空）`，
残留必须为 0 才 exit 0；③回归 `tests/test_backfill_v2_sentences_scope.py` 钉死
「默认＝全库、限定 v2 时 v1 的 0 值行不动、dry-run/verify 零写入、口径单源无第二套
切句器」。安全过滤任何模式下不放宽：只碰 `n_sentences == 0` 且 text 非空的段，只写
`n_sentences` 一列，`text`/`text_clean`/`n_chars` 一律不动。

## 1. 口径差原文（审查报告 §1.6，经主控任务书转述）

> `scripts/backfill_v2_sentences.py::scan()` 的过滤条件是 `Segment.n_sentences == 0`
> AND text 非空，**查询里并没有 `seg_version == 2` 谓词**；而该脚本的叙述/文档/背景
> 一律以「v2 段（seg_version=2）424,294 行」为口径。
> ——功能上仍安全（v1 非空段 `n_sentences` 本就 ≥1，不会被 `==0` 命中；命中行只会是
> 那个 bug 造出来的，且用的是同源口径、幂等）；但**声明与实现不完全吻合**：说「只补
> v2」，实际「全库补 0 值非空段」。这是审计口径卫生项。

本席独立读码复核：属实。基线 `07b3873` 的 `scan()` 查询原文（改动前）：

```python
q = s.query(Segment).filter(Segment.n_sentences == 0, Segment.id > last_id)
if work_ids:
    q = q.filter(Segment.work_id.in_(work_ids))
```

——只有 `n_sentences == 0` + 主键分页 + 可选 `work_id` 三件，**无任何 `seg_version`
条件**；而模块 docstring 通篇以「v2 段」叙述。声明-实现差坐实。

## 2. 主控实测现状（本席复现状态：未能亲跑，见 §5 验收自证）

任务书给出的只读真库现状（转述照抄，本席**未独立复跑**）：

```
segments: seg_version=1 → 14124 行, sum(n_sentences>0)=14124, avg=2.737
          seg_version=2 → 424294 行, sum(n_sentences>0)=424294, avg=2.431
```

⇒ 回填已跑过，v2 无 0 值残留 ⇒ 本任务不是再跑回填，而是把口径做实 + 给一致性校验出口。
方向与本席读码自洽：`import_corpus_v2.py` 现在写 `n_sentences=n_sents(ch)`（非空段恒
≥1），补齐后 `n_sentences == 0` 的非空 v2 段应为 0 行。

## 3. 改前 / 改后对照（代码级）

### 3.1 `scan()`：新增版本作用域，默认全库不变

改动后（`scripts/backfill_v2_sentences.py`）：

```python
def scan(*, batch=BATCH, sample=5, apply=False, work_ids=None,
         seg_versions: list[int] | None = None) -> dict:
    ...
    q = s.query(Segment).filter(Segment.n_sentences == 0, Segment.id > last_id)
    if vs is not None:
        q = q.filter(Segment.seg_version.in_(vs))
    if work_ids:
        q = q.filter(Segment.work_id.in_(work_ids))
```

| | 改前 | 改后 |
|---|---|---|
| 默认（不传 `seg_versions` / 不加 `--seg-version`） | 全库扫 `n_sentences == 0` 非空段（叙述却写「补 v2」） | **行为逐字不变＝全库**；docstring/打印/帮助如实写明「全库（查询不含 seg_version 谓词）」，口径差消除靠讲清楚 |
| 限定版本 | 无此旋钮 | `--seg-version 2`（可重复）⇒ 查询加 `Segment.seg_version.in_([2])`，v1 的 0 值行**不动**（回归 `test_seg_version_2_leaves_v1_zero_rows_untouched` 钉死） |
| 空列表 `seg_versions=[]` | （无此参数） | 直接 `ValueError`：[] 是「谁都不碰」的笔误，不许被误读成全库（`test_scan_rejects_empty_version_list`） |
| 可观测性 | 只有总数 | 返回值新增 `versions={seg_version: {matched, blank, updated}}`，打印「版本分布 seg_version=N｜…」，一眼看出本次实际覆盖了哪些版本；抽样行也带 `seg_version=` |
| 安全过滤 | 只碰 `n_sentences == 0` 且 text 非空；只写 `n_sentences` 一列 | **任何模式下逐字不变**；`text`/`text_clean`/`n_chars` 不动，空文本保持 0，已有非 0 值不动 |

### 3.2 新增 `verify()` / `--verify`：回填后一致性校验（纯只读）

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe scripts/backfill_v2_sentences.py --verify
```

- 逐 `seg_version` 输出一行：`count(*)`、`sum(n_sentences>0)`、`avg(n_sentences)`、
  `残留(n_sentences=0 且文本非空)`；
- 残留判据与 `scan()` 的安全过滤**同一实现口径**：SQL 取 `n_sentences == 0` 的候选，
  文本按 Python `strip()` 判空复核（不走 SQL `trim` 近似——SQLite `trim` 默认只去空格，
  与 `str.strip()` 对 `\n`/`\t` 的行为不吻合，两个出口判据必须逐字节一致）；
- 残留行数 `> 0` ⇒ 打印「验证明不合格：仍有 N 行…」并列出样本 id，**exit 1**；
  为 0 ⇒ exit 0；
- `--verify --seg-version 2`：统计与残留门都只看该版本（v1 有残留不拦 v2 的绿门，
  反向亦然）；默认＝全库逐版本；
- 与 `--apply`、`--work` 参数层互斥（argparse 直接 exit 2）——校验没有「顺手写一把」
  的口子；
- **零写入**：全程只有 SELECT（`db.session()` 的 `Session` 是 `autoflush=False`，
  context 退出不 commit）；回归把 `Session.commit`/`Session.flush` 钉成「一调用就抛」
  再跑 `--verify`，并比对全库快照分毫不变
  （`test_verify_flags_residual_and_writes_nothing`、
  `test_cli_verify_apply_verify_full_loop` 末段）。

## 4. 真库只读 `--verify`：命令与本席执行状态

**本席未取得真库原始输出**——两条路都被工席权限门拦下（原文错误见 §5），本席不绕过、
不伪造读数。交由主控执行的只读命令（零写入；若嫌 WAL 起见文件锁可加 `?mode=ro` 直觉
等价，本命令本身不含任何 UPDATE/commit 路径）：

```
cd /d F:\agi\language-genome
F:/Hermes/hermes-agent/venv/Scripts/python.exe scripts/backfill_v2_sentences.py --verify
```

按 §2 主控实测（两版本 `sum(n_sentences>0)` 均等于 `count(*)`），预期输出形状
（**这是从代码推出的预期格式，不是实跑样本**）：

```
目标库: sqlite:///F:/agi/language-genome/data/language_genome.db
模式: VERIFY（只读一致性校验，零写入、零 commit）｜范围 全库（查询不含 seg_version 谓词）
  seg_version=1｜count=14124 ｜n_sentences>0 14124 ｜avg=2.737 ｜残留(n_sentences=0 且文本非空) 0
  seg_version=2｜count=424294 ｜n_sentences>0 424294 ｜avg=2.431 ｜残留(n_sentences=0 且文本非空) 0
验证合格：非空文本且 n_sentences=0 的残留为 0 行 ⇒ 回填口径闭合        （exit 0）
```

复核对账也可用裸 SQL（与 §2 数字同形）：

```
sqlite3 "file:F:/agi/language-genome/data/language_genome.db?mode=ro" \
  "select seg_version,count(*),sum(n_sentences>0),round(avg(n_sentences),3),\
          sum(n_sentences=0 and trim(text)!='') from segments group by 1"
```

（注意裸 SQL 的 `trim` 与本脚本 `strip` 判据在 `\n`/`\t` 上有微差；以 `--verify` 的
Python 侧复核为准。）

## 5. 回归与验收门自证状态（如实）

新增 `tests/test_backfill_v2_sentences_scope.py`（10 个用例，进程内 7 + 子进程独立临时
库 3；零网络、零真实库、零真实模型请求；不改动 `app/` 与任何既有测试）。覆盖：
默认＝全库且 v1 既有非 0 值不动（versions 分布含 {1,2}）；`seg_versions=[2]` 时 v1 的
0 值行原样不动、v2 补齐、空文本/非 0 边界不放宽；限定作用域的 dry-run 零 commit
（`Session.commit` 一调用就抛）；`--verify` 有残留 exit 1 + 样本 id + 零写入快照比对；
`--verify` 与 `--apply`/`--work` 互斥 exit 2；AST 钉死无第二套切句器（不 import `re`、
不直调 `_sentences`/`_count_sents`）+ 行为钉死 `n_sents` 走 `segmenter_v2._count_sents`
唯一入口且函数体住在 `import_corpus_v2.py`；CLI 全链路：全库 verify 红 → 限定 v2 apply
→ v2 门绿而全库门仍红（v1 残留）→ 全库 apply → 全库 verify 绿且反复 verify 零写入。

| 验收项 | 状态 |
|---|---|
| `F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_backfill_v2_sentences_scope.py tests/test_import_v2_sentcount.py -q` → exit 0 | **未跑**：本席权限门把所有 Python 解释器调用拦下（见下），文件已交付，待主控放行执行 |
| 交付文档含 `seg_version`、`n_sentences=0`、`全库`、`--verify`、`口径` | 已含（本文 §0–§5） |
| 真库只读 `--verify` 原始输出 | **未取得**（同上拦阻 + 真库文件在工席允许目录之外）；命令与预期形状已给（§4），不伪造读数 |
| 默认行为不变 | 代码级保证：`seg_versions=None` 时查询逐字等价于基线（无 `seg_version` 谓词）；既有测试 `tests/test_import_v2_sentcount.py` 一字未动，其断言（`would_update 3`/`updated 4`/幂等/CLI 闭环）与改后打印格式兼容 |

阻塞详情（供主控决定给权限还是代跑；本席未绕过、未变通规避）——以下形态全部返回
`Error: Allow Bash to run: …?`：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest … -q
"F:/Hermes/hermes-agent/venv/Scripts/python.exe" -m pytest … -q
python -m pytest … -q          # 注：which python 恰指向同一 venv，命令名形式也被拒
py -3 -m pytest … -q
python -m py_compile scripts/backfill_v2_sentences.py tests/test_backfill_v2_sentences_scope.py
```

（`ls`、`which`、`python --version` 可用；解释器带参执行一律被拒。与上一席
`docs/v2_句数口径_20260925.md` §1 记录的拦阻同款。）

## 6. 残余风险（如实列）

1. **验证门未经本席亲跑**：正确性论证是代码级 + 断言级，非执行级。主控放行解释器后，
   验收门若红，第一嫌疑是打印措辞/统计形状类断言，而非口径逻辑本身。
2. `--verify` 与裸 SQL `trim` 判据微差：脚本以 Python `strip()` 复核为准；换 Postgres
   后端时 `btrim` 行为也不同——一致性判据在应用侧，不依赖 SQL 方言，风险已收在判据
   单点（`verify()` 与 `scan()` 同一表达式 `(seg.text or "").strip()`）。
3. 默认仍是全库口径：这是**有意的兼容选择**（既有文档、主控操作史都以「默认全库」为
   行为基线）。要严格「只碰 v2」必须显式 `--seg-version 2`；若主控希望反过来（默认即
   v2），那是行为变更，需另立任务并同步改既有回归。
4. v1 的 0 值行在真库现应为 0（§2）；若未来某旁路脚本再造 v1 0 值行，默认全库口径会
   把它们一并补掉——这正是 §1.6 论证过的「功能安全」面，`--verify` 会先于任何人发现。
5. 本席未 commit/merge/push；未写真库（未跑任何 `--apply`，真库连只读 `--verify` 都
   未获执行）；未动 `app/`、既有测试与 `data/`。
6. `--verify` 全库残留扫描是 keyset 分页读 `n_sentences == 0` 候选行：现库该集合为空
   （一批即停）；若未来 0 值行海量（如 v2 全库未补状态），verify 会读穿 42 万行 id+text
   ——只读但耗时，预期分钟级；需要更快可 `--seg-version` 分批验。

## 7. 交付物清单

| 文件 | 动作 |
|---|---|
| `scripts/backfill_v2_sentences.py` | 改：`scan(seg_versions=…)` + 版本分布打印 + `verify()`/`--verify`/`--seg-version`；docstring 把「默认＝全库」讲清 |
| `tests/test_backfill_v2_sentences_scope.py` | 新建：10 用例（见 §5） |
| `docs/n_sentences回填口径_20260925.md` | 新建：本文 |
