# 交付说明 — lg-supply-whitelist-col

worktree：`F:/agi/_scratch/worktrees/supply-whitelist-col`（分支 `task/supply-whitelist-col`）
仓库口径列修订：把供给白名单判据从语义废弃的 `allowed_purposes` 换成 `identity_purposes`，
旧列保留为对照列 `wl_legacy_len`（并列输出、不得删除）。真库只读、零模型调用、未 commit。

## 改了哪些文件（均在允许清单内）

1. `scripts/k5_supply_recount.py`
   - 上一手 worker 已把 `_WHITELIST_LEN` 改为 `identity_purposes`、加了
     `_WHITELIST_LEN_LEGACY=allowed_purposes` 与 `wl_compare`/`wl_diff_evidence`，
     但**没接线**：`build_result` 里 `compare`/`diff` 算了却没进返回 dict、
     `render_markdown`/`render_stdout` 从不渲染、§2 标题还写 `allowed_purposes`。
     本手把这些补齐：
   - `build_result` 返回 `wl_compare` / `wl_diff`（:309）。
   - 新增 `_render_whitelist_sections()`（:322）：§2.5 两口径数字并列（A/B 各给
     新列合计与旧列合计 `wl_legacy_len`）+ 逐作品两列对照表；§2.6 成员资格翻转
     作品的 `work_sources` 只读原文归因（identity/license/allowed＋source_type＋
     created_at）＋ `license_purposes 非空不算白名单` 的理由。
   - §2 口径 A/B 标题：`json_array_length(allowed_purposes)>0` → 语义白名单列
     `json_array_length(identity_purposes)>0`（既有数字表数值**未动**）。
   - `render_stdout` 末尾并列打印「口径 A/B：新列合计｜旧列合计」与翻转作品一行。
   - SQL 判据片段处注释写明：新列 `identity_purposes`；旧 `allowed_purposes` 系
     建表史遗留纯插行列（`app/knowledge_query.py:130-134`、`app/models.py:109-114`、
     `FINGERPRINT_EXCLUDED_FIELDS`）；`license_purposes` 非空**不算**通过授权
     （用途授权位、非白名单成员资格，理由内联于 :87-91）。
2. `tests/test_k5_supply_recount.py`
   - 夹具 `work_sources` 表补齐真实 schema 列（`identity_purposes`/`license_purposes`/
     `source_type`/`created_at`/`allowed_purposes`）——原夹具只有 `allowed_purposes`，
     换列后脚本会因「no such column」全线报错。令 identity 列成员资格与旧 allowed
     同构（W1/W6 非空、W2 空、W4 非法、W5 非数组、W3 无登记行），**既有 A/B/C 期望值逐条不变**。
   - 新增第二套夹具 `make_db_covhan`（覆汉形态：identity=["research"]、allowed=[]；
     仅授权 LIC：identity=[]、license=["training_source"]；空 identity EMP）。
   - 新增 8 条回归（≥6）：① 空 identity fail-closed（W2）；② 覆汉形态纳入新列、
     被旧列排除；③ 无登记行排除（wl_identity_len=-1，W3）；④ 非法 JSON / 非数组
     fail-closed（W4/W5）；④b license 非空不充白名单（LIC 排除）；⑤ 新旧两列合计
     并列输出（A_total=2 vs A_legacy_total=0、B 同）；⑤b wl_compare 两列齐全＋
     wl_diff 翻转归因原文；⑤c render_markdown 落出 §2.5/§2.6 新列判据与对照。
3. `docs/K5供给口径真计数_20260925.md`
   - 在 §2 与 §3 之间**追加** §2.5（列选错依据＋两口径数字对照）与 §2.6（覆汉治理
     归因＋纳入/不纳入两个数字）。既有 §1/§2/§3 数字表**数值未改**（§2.3 B 旧列
     401,611 等原样保留）。

## 覆汉归因结论（口径 A 三数字）

`WK-dc90993434e9`（覆汉，production_nonbenchmark，35,974 段）：`allowed_purposes=[]`
被旧口径 fail-closed 排除；`identity_purposes=["research"]` 非空 ⇒ 按 identity 口径
**被纳入白名单成员资格**。但其段无一 `src_ok=true`（未过源校勘、属「未校验」），
对口径 A **零贡献** ⇒ 纳入后口径 A 三数字仍为 **合计 4,220 / 最大单作品 876 / ≥1 万段作品数 0**
（与不纳入时相同；此三数与主控实测「换 identity 列口径 A 合计同样 4,220」一致）。
覆汉 35,974 段只体现在口径 B（不看 src_ok）：B 合计在旧列 401,611 基础上并入其非
benchmark 段（精确并列数由脚本真跑打印，见验收命令 2）。登记代码位置：
`scripts/register_work_sources.py::register()` root 分支（:192-205 造 row、:237
`WorkSource(**row)`，row 不写 allowed → ORM 默认 `[]`，`app/models.py:114`）；
交叉核对 `scripts/k2_unlock_sim.py:87,371-373`。

## 验收命令（须由主控执行）

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k5_supply_recount.py -q
F:/Hermes/hermes-agent/venv/Scripts/python.exe scripts/k5_supply_recount.py
```

## ⚠ 阻塞：本地无法执行（权限门拒绝一切 python 调用）

本 worker 会话的 Bash 权限门**放行** `python --version`、`git status/diff`、`echo`、`which`，
但**拒绝**（返回 `Allow Bash to run: ...?` 且未被批准）以下全部执行形态，各已重试多次：
`python -m pytest ...`、`python scripts/k5_supply_recount.py`、`python -c "..."`、
`py -m pytest ...`、全路径 `F:/Hermes/.../python.exe -m pytest ...`；无 `sqlite3` CLI。
按纪律「不得绕过权限提示」，**未**尝试规避。故：
- 两项验收命令的**原始 stdout 未能由本 worker 粘贴**（未能执行，非未通过）；
- 覆汉 B 精确并列数、真库逐作品 `wl_identity_len/wl_legacy_len` 需脚本真跑打印
  （脚本与夹具已就绪，跑即产出）。
以上为**唯一未竟项**；三项文件交付已完成并经静态逐例核（期望值手算对齐）。

## 反向验证（静态推演；待 python 权限放行后按此复核）

把 `_WHITELIST_LEN = _wl_case("identity_purposes")` 临时改回 `_wl_case("allowed_purposes")`
（`WL_OK`/`COND_A`/`COND_B`/`measured` 随之改用旧列），预期**立即变红**的新回归：
- `test_covhan_form_included_in_new_excluded_in_legacy`：covhan 全部 `allowed=[]`，
  `COND_A` 命中集变 `set()`，`assert {"c1","c2"} <= a_new`（该函数首个断言）**红**。
- `test_two_caliber_totals_output_in_parallel`：`measured["A_total"]` 由 2 变 0，
  `assert m["A_total"] == 2` **红**。
- `test_illegal_or_nonarray_identity_fail_closed`：W4/W5 的 `allowed=["train"]` 非空，
  `s_w4`/`s_w5` 漏进 `COND_A`，`assert not a_ids & {"s_w4","s_w5"}` **红**。
（`wl_compare`/`wl_diff_evidence` 用 `_wl_case("identity_purposes")` 直取，不走
`_WHITELIST_LEN`，故其两列对照仍恒为 identity——正是「数字并列、判据列须正确」的落点。）
验证后须还原为 `identity_purposes` 并复跑全绿。本 worker **未落盘该临时改动**（无法跑，
不留下未还原文本，避免污染正确版本）。

## 纪律自检

- 真库只读（`mode=ro`）、零写库、零模型调用：脚本逻辑未改；`data/` mtime 未触碰
  （本会话未执行任何写/格式化/git 提交动作）。
- 未 commit/merge/push；未改 `app/knowledge_query.py` 或任何非清单文件；未新建清单外文件。
