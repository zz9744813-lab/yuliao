# DELIVERY — lg-segments-slim-fix（按会审 BLOCK 逐条修 `slim_segments`）

分支：`task/segments-slim-fix`（**未 push、未合 main**）。基线：`28c0985` 的 `lg-segments-slim`。
会审记录：`F:\Hermes\team\reviews\lg-segments-slim-28c0985862.md`（glm-5.3 BLOCK / qwen3.8-flash BLOCK）。

改动文件（均在白名单内）：
- `scripts/slim_segments.py`
- `app/console.py`
- `tests/test_slim_segments.py`
- `docs/SEGMENTS_SLIM.md`
- `DELIVERY_segments_slim_fix.md`（本文件）

**未改** `app/models.py`（不在本 worker 白名单）⇒ 严重项 1 的修法走 console 复合口径路线，不动 ORM。
**未对活库写**、**未放宽任何判据**、**`text` 列逐字节不变**（回归仍全绿钉死）。

验收：`F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_slim_segments.py -q`
→ **17 passed**（原 12 → 现 17，净新增 5 条，其中 ≥3 条为反向自检）。
`tests/test_console.py` → **20 passed**（回落分支不受影响，无回归）。

---

## 逐条对应（会审原文 → 改了什么 → 证据）

### 1. [严重] `cleaned` 列 + console 对增量数据永久漏计 ✅ 已修（实现层面消除，非仅文档）
- **改了什么**：`app/console.py` 新增可测函数 `_cleaned_expr(colnames)`，探测到 `cleaned` 列时**不再**读
  `SUM(cleaned)`，改用**等价复合口径**
  `SUM(CASE WHEN cleaned = 1 OR text_clean IS NOT NULL THEN 1 ELSE 0 END)`；无该列时仍回落 `SUM(text_clean IS NOT NULL)`。
  理由：`cleaned` 不进 ORM（`models.py` 未动）⇒ 迁移后新行 `cleaned=0` 但 `text_clean` 非 NULL。复合口径里
  被迁移归 NULL 的等值行由 `cleaned=1` 命中、增量/未迁移行由 `text_clean IS NOT NULL` 命中，并集 == 原「有清洗结果」语义，
  **不放宽判据、不改口径语义**。`_corpus` 只此一处调用该函数（grep 复核）。
- **回归用例**（新增）：`test_incremental_cleaned_not_undercounted`（迁移后 ORM 风格插入 `cleaned=0`+`text_clean` 非 NULL 新行 →
  复合口径 +1，并**反向自证** `SUM(cleaned)` 漏计这一行）。
- **真跑证据**：
  ```
  ### [E1] 模拟 ORM 新行（models.py 未改 ⇒ cleaned 走 DEFAULT 0，但 text_clean 非 NULL）
    新行 cleaned=0（ORM 默认 0）
    console 复合口径表达式 = SUM(CASE WHEN cleaned = 1 OR text_clean IS NOT NULL THEN 1 ELSE 0 END)
    复合口径读数 = 961  （应为 960+1 = 961）
    旧口径只看 SUM(cleaned) = 960  （漏计新行：少 1）
  ```

### 2. [一般] `--limit 0`/负数静默变全库；`--batch 0`/负数死循环 ✅ 已修
- **改了什么**：`run()` 最前置入参闸门（在开库/写任何东西之前）：`limit is not None and limit<1` → 打印说明并 `return 2`；
  `batch<1` → `return 2`。范围只收紧不放宽（`limit>=1`、`batch>=1`）。
- **回归用例**（新增，反向自检）：`test_reject_bad_limit_and_batch`：`limit ∈ {0,-1,-100}`（含 `--dry-run`）、`batch ∈ {0,-5}` 全部 rc=2，
  且断言**开库前即拒绝 → 无 `cleaned` 列、无行归 NULL**。
- **真跑证据**：
  ```
  ### [E2] --limit 0 / --batch 0 拒绝（退出码，且开库前拒绝）
    run(limit=0) rc = 2
    run(batch=0) rc = 2
  [slim] --limit=0 非法：必须 >= 1。（LIMIT 0 ⇒ 子查询空 ⇒ MAX(rowid)=NULL ⇒ 会被误当作全库执行，已拒绝。）
  [slim] --batch=0 非法：必须 >= 1。
  ```

### 3. [一般] `--limit` 部分迁移 + console 口径不一致 ✅ 已修（实现消除风险，并写进文档）
- **改了什么**：由第 1 项的复合口径直接消除——部分迁移时范围外已清洗行 `text_clean` 仍非 NULL，走
  `text_clean IS NOT NULL` 分支计入，**console 当场即正确，无需等全量迁移完成**。文档 §3 明写此结论，并删去
  「切换新口径只能在全量迁移完成后」这一实现可规避的前提。
- **回归用例**（新增）：`test_limit_partial_migration_console_still_correct`：`limit=500` 部分迁移后
  `_console_cleaned(db) == 迁移前全库非 NULL 计数`。
- 另：`test_limit_scope_and_vacuum` 补断言范围外已清洗行 `cleaned=0`（见第 7 项②）。

### 4. [一般] 全量活库下内存/复杂度（`int_rows` 全表 `fetchall` + 每批线性过滤）✅ 已修
- **改了什么**：删除一次性 `int_rows = ...fetchall()` 与 `[u for u in int_updates if b0<=u[1]<=b1]` 线性过滤。
  改为 **b/c 合并单趟分批循环**：每批用 `rowid BETWEEN ? AND ?` **流式**取该区间的 integrity 行
  （`_compact_integrity(b0,b1)`，常驻内存 O(batch)）、算 `pack_raw` 压缩差、`executemany` 回填，逐批 commit。
  总复杂度由 O(更新数×批数) 降到 **O(行数)**。
- **真跑证据**（合并循环单批日志）：
  ```
  [slim][预] 将归 NULL 的等值 text_clean：480 行 / 22,080 B；integrity 逐批流式紧凑化（见每批 [b/c] 行）
  [slim][b/c] rowid [1..1200]：归 NULL 480 行（累计 480）；紧凑化 400 行（累计 400），耗时 0.02s
  ```

### 5. [一般] 活库守卫用 `abspath` 而非 docstring 声称的 `realpath`（junction/符号链接可绕过）✅ 已修
- **改了什么**：`_norm` 改用 `os.path.realpath`（解析符号链接 / junction / 8.3 短路径 / subst 盘）+ `normpath` + `normcase`，
  与 `is_live_db` docstring 口径一致。守卫只加强不削弱。
- **回归用例**（新增，反向自检）：`test_symlink_to_live_db_is_refused`：临时目录 `os.symlink` 指向活库路径
  （目标文件不存在也无妨，realpath 仍解析），断言 `is_live_db(link) is True` 且 `run(link)`、`run(link, dry_run)` 均 rc=2。
  （环境不允许创建符号链接时 `pytest.skip`；本机实测可创建。）
- **真跑证据**：
  ```
  ### [E5] realpath 守卫：临时目录符号链接指向活库路径
    is_live_db(link) = True
    run(link) rc = 2  run(link,dry_run) rc = 2
  [slim] 活库需 --force-live：...\evil.db 判定为活库路径，默认拒绝对活库执行任何写操作（含 VACUUM）。
  ```

### 6. [一般] 中途失败态（`left_equal != 0` 时前批已 commit）未声明 ✅ 已修（输出 + 文档）
- **改了什么**：`left_equal != 0` 分支的 stderr 由一句扩为明示「⚠ 半途而废态：此前各批 UPDATE 已逐批 commit，
  不可自动回滚；已提交批次必须按 `docs/SEGMENTS_SLIM.md §4` 回滚节手工处理后再重跑」。文档新增 §4.4「半途而废态」一节。
- 该项为输出/文档声明（会审原文即要求「明示」，非要求实现回滚）；未放宽判据。

### 7. [建议] 测试缺口 ①/② ✅ 已补
- **①** `test_reverse_dirty_text_not_fixed` 重写：原选 `text_clean IS NULL` 的行（重跑本就不触碰，断言弱）改为
  选**等值行**（迁移后 `text_clean` 已归 NULL）→ 迁移后改脏 `text` 再重跑 → 断言 `text` 仍脏**且** `text_clean` 仍为
  `NULL`（迁移器绝不把归 NULL 的 `text_clean`「复原」）。
- **②** `test_limit_scope_and_vacuum` 补 `--limit` 场景范围外 `cleaned` 断言：范围外已清洗行（`text_clean` 非 NULL）
  `cleaned=1` 计数必须为 0（迁移器只回填范围内 `cleaned`）。
- **真跑证据**（①）：
  ```
  ### [E7] 反向：等值行迁移后改脏 text 再重跑，text_clean 不得被复原
    迁移后等值行 text_clean 归 NULL(=None)；改脏 text 重跑后 text_clean=None（应保持 None，不复原）
  ```

### 8. [建议] `scoped` 用 f-string 拼 `hi`，参数化 `_scope_clause` 是死代码 ✅ 已修
- **改了什么**：删 f-string `scoped`，统一 `scope_sql, scope_params = _scope_clause(hi)` 参数绑定；原死代码
  `_scope_clause` 现被实际调用（所有范围计数/回填/校验查询都传 `scope_params`）。`grep 'rowid <= {'`/`1=1`/`scoped` 均无残留。

### 9. [建议] `PRAGMA foreign_keys=OFF` 无必要 ✅ 已移除
- **改了什么**：删除该行，注释说明本轮只改三列列值、不新增/删除行、不动主键/外键、不重建表 ⇒ 无需关外键，
  关掉反而少一道兜底。文档 §1 同步声明。

### 10. [建议] fixture `SEG_DDL` 与 ORM 真表结构无对账 ✅ 已补护栏
- **改了什么**：新增 `test_fixture_schema_matches_orm_segment`：`SEG_DDL` 在内存库建表后 `PRAGMA table_info`
  列集合减去显式化的 `rowid`（真库为 SQLite 隐式 rowid，PRAGMA 不列出；`cleaned` 由迁移器补列、不属 ORM 声明列），
  必须 == `Segment.__table__.columns.keys()`，防止 `--limit`/分批 rowid 口径在真库失真。
- **真跑证据**：`### [E10] fixture 列集合 vs ORM Segment 列集合 → ddl-rowid == orm ? True`

---

## 反向自检（≥3 条，验收要求）
1. **守卫绕过**（`test_symlink_to_live_db_is_refused`）：符号链接指向活库必须被 realpath 判活并拒绝。
2. **`--limit 0`/`--batch 0`**（`test_reject_bad_limit_and_batch`）：非法入参开库前即拒、零写入。
3. **增量 `cleaned` 漏计**（`test_incremental_cleaned_not_undercounted`）：既证复合口径不漏，又**反证**旧 `SUM(cleaned)` 会漏。

## 硬约束复核
- 未对活库写：所有证据/测试都在临时目录小副本；守卫加强为 realpath 且只拒绝不放行（无 `--force-live` 路径未触发）。
- `text` / 有效正文 / 两读侧口径 / integrity `loads_any` 等价：原 `test_text_column_never_touched`、
  `test_effective_text_invariant_both_readers`、`test_integrity_loads_any_equivalent` 全绿未改判据。
- 未放宽判据；未改 `app/scene_runtime/**`、`scripts/k45_acceptance.py`、门脚本、`app/models.py`。

## 未做 / 如实声明
- **未在活库 `D:/language-genome-data/language_genome.db` 上跑任何写操作**（含 VACUUM）——按约束只在小副本真跑。
- 会审 [一般] 中 qwen 席额外提出的「备份存在性检查 / `--i-have-a-backup` 显式确认」属**建议**级、不在本任务 10 项清单内，
  **本轮未实现**（默认拒绝活库已是最强闸门，正式活库执行前仍应人工备份，见文档 §4.1「副本先跑、原文件整文件保留」）。
- 本 worker 无权 commit/merge/push；改动提交在本 worktree 分支由值班 agent 走会审门禁。
