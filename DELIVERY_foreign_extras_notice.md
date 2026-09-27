# strategy_stats_rebuild 外来键清点（dropped_foreign_extras）

派工：foreign-extras-notice（工作树 `F:/agi/_scratch/worktrees/foreign-extras-notice`，
分支 `task/foreign-extras-notice`，基点 `ead47a7`）。

## 0. 交付形态说明（与任务书的差异，先说清楚）

- 任务书允许新增 `tests/test_foreign_extras_notice.py`，但主控给的**允许编辑
  白名单**只含 `scripts/strategy_stats_rebuild.py`、`tests/test_docs_code_reconcile.py`、
  `DELIVERY_foreign_extras_notice.md` 三处（白名单是硬约束：新建未列文件即
  越界、整轮判负）。任务书自身给出等价路径：「若你没新增独立测试文件，就把
  新用例放进 `tests/test_docs_code_reconcile.py`，验收命令相应改成只跑该文件」
  ——本次按该路径交付。
- 相应地，验收命令为（工作树内实跑）：

  ```
  F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_docs_code_reconcile.py -q -p no:warnings
  ```

## 1. 改动点（文件:行号，行号为交付后现状）

### `scripts/strategy_stats_rebuild.py`

| 位置 | 内容 |
| --- | --- |
| `:33-40` | 模块 docstring「extras 写入方式」条：由「apply=True 会静默丢弃」改为**非静默**口径——delete 前清点外来键、打印 `dropped_foreign_extras=<总次数> {键名: 出现行数}`、汇总同进 run() 返回 dict、dry_run 恒 {} 且整条路径不插/不删/不改数据行 |
| `:76-82` | 新增常量 `CANONICAL_EXTRAS_KEYS = frozenset({...})`（`:79`）：本次重建写入 extras 的键全集 = `by_root_work / usable_evidence / benchmark_stripped / k3_eligible_instances / k3_eligible_root_works`（外来键判据；追加自有键须同步扩充） |
| `:156-165` | 新增 `_count_dropped_foreign_extras(s)`：遍历现库 `strategy_stats` 各行 extras，统计不在 `CANONICAL_EXTRAS_KEYS` 内的键 →「键名→出现行数」 |
| `:168-181` | `run()`：`out` 初始化即含 `"dropped_foreign_extras": {}`（dry_run 恒 `{}` 的来源，`:172-174`）；`apply=True` 分支在 **`s.query(StrategyStats).delete()` 之前**先清点（`:176`）、`print(f"dropped_foreign_extras={sum(dropped.values())} {dropped}", flush=True)`（`:177-178`，走 stdout 既有口径）、汇总进返回 dict（`:179`），随后才是原有 delete+全量 add（零新增写语义） |

CLI（`main()`）参数与语义未动；`--apply` 的 delete+add 覆盖语义未动；dry_run
路径除只读清点返回值外无任何库写（实际 dry_run 连清点都不做，恒 `{}`）。

### `tests/test_docs_code_reconcile.py`

| 位置 | 内容 |
| --- | --- |
| `:11-14` | 模块 docstring `reverse` 说明处按要求补句：「dropped_foreign_extras 本条 2026-09-27 已补实现，改判在位」 |
| `:21-38` | 新增导入：`importlib.util`/`json`/`sys`、以 `ssr_fen` 名装载 `scripts/strategy_stats_rebuild.py`、`from app import config, db`、`from app.models import StrategyStats`（写库护栏与功能用例所需） |
| `:141-148` | `ABSENT_CLAIMS` → `REVERSED_TO_PRESENT_CLAIMS`（同条目：分档 §6⑪ / `scripts/strategy_stats_rebuild.py` / 字面量 `dropped_foreign_extras`） |
| `:179-187` | 哨兵由**缺失断言**改判**在位断言** `test_overclaim_reversed_to_present`：字面量必须存在于实现文件，实现再消失即红 |
| `:215-304` | 新增 dropped_foreign_extras 功能回归一节：护栏 `_assert_temp_db`（双道临时库断言，仿 `test_strategy_stats_evidence_class`）、`_seed_stats`、`_all_stats_rows`，及三个用例（详见 §3） |

## 2. 验收命令与逐字输出原文 —— **本环境未能实跑（阻断，见 §5）**

主控要求逐字实跑并贴原文。本会话的 Bash 权限门**只放行只读命令**
（`pwd`、`ls` 通过），凡涉及解释器/pytest 的执行一律被拦，被拦原文如下
（每条即一次真实尝试，未做任何绕过）：

```
Error: Allow Bash to run: cd /f/agi/_scratch/worktrees/foreign-extras-notice && F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_docs_code_reconcile.py -q -p no:warnings?
Error: Allow Bash to run: F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_docs_code_reconcile.py -q -p no:warnings?
Error: Allow Bash to run: "F:/Hermes/hermes-agent/venv/Scripts/python.exe" -m pytest "tests/test_docs_code_reconcile.py" -q -p no:warnings?
Error: Allow Bash to run: F:/Hermes/hermes-agent/venv/Scripts/python.exe --version?
Error: Allow Bash to run: python -c "import pytest, sqlalchemy; print(pytest.__version__, sqlalchemy.__version__)"?
Error: Allow Bash to run: python -m pytest --version?
Error: Allow Bash to run: pytest --version?
```

（PATH 上的 `python --version` 可得 `Python 3.14.7`，但该解释器同样无法带参
执行，且依赖是否齐备未知——不以此冒充验收。）

因此**pytest 输出原文无法由本席贴出**；代码经静态复核（§4 的替代验证），
实跑请主控在有权限的环境执行：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_docs_code_reconcile.py -q -p no:warnings
```

## 3. 用例数前后对比（静态核算，非实测）

基点 46 例 = PRESENT_CLAIMS 40 + reverse 条目 1 + MIN_COUNT 3 + MOVED 1 +
`test_reconcile_ledger_exists` 1。

交付后 46 + 3 = **49 例**（只增不减）：

1. `test_apply_counts_drops_and_reports_foreign_extras`（①）：造 2 个外来键
   （`k_fen_a` 现于 2 行、`k_fen_b` 现于 1 行）→ 断言返回 dict 汇总
   `{"k_fen_a": 2, "k_fen_b": 1}`、stdout 出现 `dropped_foreign_extras=3`
   及两键计数、apply 后载体行已从库中消失且全表 extras ⊆ 重建键集（含
   `CANON == ssr.CANONICAL_EXTRAS_KEYS` 漂移哨兵）；
2. `test_apply_without_foreign_keys_reports_zero`（②）：无外来键时汇总恒
   `{}`、打印 `dropped_foreign_extras=0 {}`、不报错；
3. `test_dry_run_reports_empty_and_writes_nothing`（③）：库里现存外来键时
   dry_run 汇总仍 `{}`、不打印清点行、库**前后逐行一致**（零改动），收尾自清。

用例数改动不影响他文件：`test_overclaim_still_absent` 旧名仅存在于 docs
叙述（docs 只读，台账回改归主控，见 §5-3）。

## 4. 反向自检 —— 同样受 §2 的执行阻断，未能实跑贴红

任务书要求的「注释掉打印/短路清点 → 看哪条转红」需要跑 pytest，本席无执行
权限，**如实报告未做**。替代验证为逐文件静态推演，结论如下（供主控复核）：

- 注释 `:177-178` 的 `print(...)` → ①（打印断言 `dropped_foreign_extras=3`）
  与②（`dropped_foreign_extras=0 {}`）转红；③不受影响（本就不该有打印）。
- 把 `_count_dropped_foreign_extras` 短路成 `return {}` → ① 的
  `rep["dropped_foreign_extras"] == {"k_fen_a": 2, "k_fen_b": 1}` 转红。
- 从实现里删净 `dropped_foreign_extras` 字面量 → 改判后的哨兵
  `test_overclaim_reversed_to_present` 转红（即台账设计：回缩须先回改文档）。

静态阶段实际发现并修复的一个**真实回归**：`test_strategy_stats_rebuild.py::
test_docstring_honest_wording` 断言 `"零库写" not in
scripts/strategy_stats_rebuild.py`（钉「零库写」是过度声明）。初稿 docstring
写了「dry_run 恒为 {} 且零库写」会把它打红——已改写为「不插/不删/不改数据行
」（`:38-39`），现全文无该字面量（grep 复核零命中）。另核对
`test_k2_backfill.py:338-356`、`test_review_dossier.py:42`、
`test_strategy_stats_rebuild.py:177-197`、`test_strategy_stats_evidence_class.py`
全部按松散键访问 `run()` 结果/不解析本脚本 stdout，新增 dict 键与打印行不
触碰它们的既有断言。

## 5. 未做项与阻断（如实清单）

1. **未实跑验收命令**（含反向自检的红测输出）：本席 Bash 权限门拒绝一切
   解释器执行（§2 贴了全部被拦原文）。代码为静态复核后交付，请主控实跑确认
   rc=0、49 passed。
2. **未新建 `tests/test_foreign_extras_notice.py`**：白名单硬约束（§0），
   新用例并入 `tests/test_docs_code_reconcile.py`，验收命令相应只跑该文件。
3. **未回改 `docs/`**（§6⑪ 对账注记、台账 `ABSENT_CLAIMS` 表述、
   `test_overclaim_still_absent` 旧名引用）：docs 只读非我权限；实现落地后
   台账第 17/53/131 行与分档 236 行注记的口径更新归主控/回改席处理。
4. 未 commit、未 push、未合并；未触碰 `expression_strategies_v2` 数据、
   `app/`、其他工作树；除交付白名单三文件外零新增文件。

## 6. 变更文件清单

- `scripts/strategy_stats_rebuild.py`（修改）
- `tests/test_docs_code_reconcile.py`（修改）
- `DELIVERY_foreign_extras_notice.md`（本报告，新增，属白名单）
