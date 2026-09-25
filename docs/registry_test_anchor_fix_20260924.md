# 修 main 上持久红：`test_clean_tree_passes_and_roots_only` 的 anchor_drift 假红

- 仓库：`F:/agi/language-genome`
- worktree：`F:/agi/_scratch/worktrees/registry-test-anchor`（分支 `fix/registry-test-anchor`，基线 `1642572`）
- 日期：2026-09-24

## 0. 一句话

别的测试文件建的 `WorkSource` 登记行**有内容却不带内容锚**（`text_sha256` 列为
NULL），被 `test_work_registry.py` 的 `clean_tree` 扫到后，`verify()` 的锚复核
对每条各报一次 `anchor_drift`，把 `n_mismatch` 顶到非零——这是跨文件顺序依赖
的假红，main 因此长期带一条红。修法：**只补测试夹具侧的登记锚**，判定口径
`verify()` 一字未动。

## 1. 复现命令与原始输出

> 说明：本次在受限 worktree 子会话里跑，**解释器执行被权限门拦下**（所有
> `python`/`python -m pytest` 调用都返回 "Allow Bash to run" 而未能实际执行），
> 所以下表的「改前输出」沿用**主控已独立复现**的事实数值（非本会话观测），
> 「改后 / 全量」标注为**待独立验证**，不作绿灯口头承诺。本会话交付的是
> 落地的代码与静态推演。

改前（主控独立复现）：

| 命令 | 结果 |
|---|---|
| `pytest tests/test_work_registry.py -q` | **全绿**（20 passed）——单跑无遗留行 |
| `pytest tests/test_k2_pairs_gen.py tests/test_work_registry.py -q` | **exit 1**，`test_clean_tree_passes_and_roots_only` 报 `assert 37 == 0` |
| `pytest tests/ -q`（全量） | 同用例 `assert 30 == 0` |

mismatch 全是同一种：

```
kind='anchor_drift', registered='', current='<12位sha>'   # registered 空 = 登记行没带锚
```

改后 / 全量：**待独立验证**（见 §6）。

## 2. 根因（独立复算，非盲信任务书）

判定在 `scripts/verify_work_registry.py:180`：

```python
if (r.text_sha256 or None) != (sha or None):
    mismatches.append({"work": wid, "kind": "anchor_drift", ...})
```

- `WorkSource.text_sha256` 列 `nullable=True` 且**无 server_default**（`app/models.py:96`）
  → 不显式赋值即 NULL。
- `verify()` 用当前库内容重算 `sha`（`_work_sha256`，有段则非空 hexdigest）。
- 于是「**有分段内容 + 登记行锚为 NULL**」→ `None != sha` → 判 `anchor_drift`。

`clean_tree` 的**占位登记**（`tests/test_work_registry.py:88-97`）只补
「库里有 Work 但**无登记行**」的遗留作品（`if _worksource_exists(w.id): continue`）——
**已经存在、但不带锚的登记行不在占位范围内**，原样漏过 → 被 `verify()` 判漂移。

「不带锚建 `WorkSource`」的四处在别的测试文件（其作品**都有分段内容**、且**用完
不清库**，泄漏到共享临时库被 work_registry 扫到）：

- `tests/test_k2_pairs_gen.py:110`（`seeded` 夹具，6 段）、`:252`（`_seed_src_work`，2 段）
- `tests/test_k4_registered_world_preflight.py:53`
- `tests/test_knowledge_query_cards.py:229`
- `tests/test_source_check_nonbench_scope.py:49`

排除项（自查，避免误伤或漏改）：

- `test_k2_backfill.py` / `test_k2_extract_nonbenchmark.py` /
  `test_strategy_stats_rebuild.py` / `knowledge_seed.py` 建行**已带** `text_sha256=sha`
  ——正确，不动。
- `test_freeze_fingerprint_content.py` 用假锚 `"a"*64`，但其 `s` 夹具是
  `yield + _cleanup()`（`tests/test_freeze_fingerprint_content.py:98-99`）**用完即清**，
  不泄漏 → 与假红无关，不改。
- `test_knowledge_query_v2.py` 假锚 `"0"*64`，`seeded` 同样 `yield + _purge`（模块级清）
  → 不泄漏，不改。

## 3. 改法（单一哈希来源，判定口径零改动）

新建 `tests/registry_anchor.py`——测试侧「带锚建登记行」的唯一入口，**复用生产实现**
`register_work_sources._work_sha256`（不本地另写一份哈希，避免分隔符/清洗回退/ordinal
序任一处错位造成假 PASS 或假漂移）：

- `work_sha256(s, wid)` → `(锚串, 段数)`
- `anchor(s, wid)` → 只要锚串，供 `WorkSource(text_sha256=anchor(s, wid), ...)`
- `refresh(s, wid)` → 「先建行、之后才补段」的夹具用，按当前内容重算并写回既有行的锚
  （无登记行则 no-op）

落点：

1. `tests/test_work_registry.py`：`_reg()` 的锚改走 `_anchor`（占位路径经 `_reg` 自然带锚，
   与手工登记行**同一口径**——占位登记从此不可能再漏锚）。
2. `test_k2_pairs_gen.py` 两处、`test_k4_registered_world_preflight.py`（含复用既有行的幂等
   分支）、`test_knowledge_query_cards.py` 一处：建行时补 `text_sha256=_anchor(...)`。
3. `test_source_check_nonbench_scope.py`：`_seed_work` 建行带锚；因其 `_seed_seg` 在**登记
   之后**才补段，故 `_seed_seg` 末尾 `_refresh(s, wid)` 重锚，避免「行锚早于内容」的漂移。
4. 各文件加 `sys.path.insert(0, ROOT/"tests")` 以导入 `registry_anchor`。

### 改前 / 改后（登记行锚列）

| 位置 | 改前 | 改后 |
|---|---|---|
| k2 `seeded:110` | 无 `text_sha256`（NULL），6 段 | `text_sha256=_anchor(s, w.id)` |
| k2 `_seed_src_work:252` | 无锚 | 带锚 |
| k4 `:53` / 幂等分支 | 无锚 | `text_sha256=_anchor(...)` / 复用行亦重锚 |
| cards `:229` | 无锚 | `text_sha256=_anchor(s, "WK-OT")` |
| source_check `:49` + `_seed_seg` | 无锚、段建在行之后 | 建行带锚 + 补段后 `_refresh` |
| work_registry `_reg`（占位经此） | 走 `VRW._work_sha256`（已带锚，但口径分散） | 走共享 `_anchor`（单一来源） |

## 4. 回归钉（`tests/test_registry_anchor_order.py`，新建）

正向（证明修复 + 顺序无关）：

- `test_anchored_registered_row_is_order_independent`：造「有内容 + 带锚」的遗留登记行，
  在 `clean_tree` 下 `verify()` 该作品无 `anchor_drift`，且 `n_mismatch == 0`。
- `test_zero_segment_registered_row_anchor_is_null_no_drift`：0 段作品锚=NULL 是如实，
  不报漂移（防补锚误伤空作品）。

反向钉（证明**门没被修松**，判定口径仍响亮）：

- `test_content_work_without_anchor_still_drifts`：有内容却无锚（旧 bug 形态）→
  `verify()` **仍必须**报 `anchor_drift`，`n_mismatch >= 1`。
- `test_content_churn_after_anchor_still_drifts`：带锚登记后库内容再变 →
  `verify()` **仍必须**报 `anchor_drift`（锚=登记时的事实，未被绕过）。

## 5. 禁止项遵守

- `scripts/verify_work_registry.py` 判定逻辑**未改**（未放宽锚复核、未加 `anchor_drift`
  白名单、未动 `verify_expect`）。
- `app/` 生产代码**未改**。
- 未用 `xfail`/`skip` 藏红。
- 未碰真库 `data/language_genome.db`（全程 conftest 临时 sqlite）。
- 未 commit / merge / push。

## 6. 全量套件结果与验证状态（如实，不掩盖）

- 本会话内**未能实际执行** pytest（解释器执行被权限门拦下）。以上改后/全量结论为
  静态推演 + 主控改前复现的因果闭合，需由独立验证跑以下命令确认：

  1. 顺序无关验收：`pytest tests/test_k2_pairs_gen.py tests/test_work_registry.py -q` → 期望 exit 0；
  2. 新增回归：`pytest tests/test_registry_anchor_order.py -q` → 期望全绿；
  3. 全量：`pytest tests/ -q` → `test_clean_tree_passes_and_roots_only` 期望绿；
     若仍出现任何 `anchor_drift`（`registered=''` 形态）之外的红，属**其它既有红**，
     应如实列出而非掩盖——尤其注意任何新建带 `WorkSource` 却仍不带锚、且不随夹具
     清库的**新**测试文件（当前 §2 排除项已核对，无一泄漏）。
- 单跑 `pytest tests/test_work_registry.py -q` 仍应 20 passed（改动不触碰其判定，仅换锚来源）。


## 7. 主控实跑补证（2026-09-25 08:1x，回应会审 9e2c3f8 千问席 BLOCK 的唯一实质理由）

会审 `9e2c3f8b8a` 千问 Qwen3.8-Flash 席判 **BLOCK**，实质理由只有一条：本文件 §6 自述
「未能实际执行 pytest」+ §5 写「未 commit / merge / push」，而该变更已以 merge 落进 main
⇒ 「修复有效」在合并时点无任何实测证据。中转 glm-5.3 席同批判 **PASS**（仅一般/建议级）。
现由主控在同一 main HEAD 上实跑补齐，命令与原始输出如下（**非采信执行代理回执**）：

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/ -o addopts="" -p no:warnings
tests	est_websrc_contract.py ..................                         [ 97%]
tests	est_work_registry.py ....................                         [ 99%]
tests	est_writer.py ........                                            [100%]
============ 1092 passed, 1 skipped, 3 xfailed in 89.81s (0:01:29) ============
```

- **全量绿**：`1092 passed / 1 skipped / 3 xfailed / 0 failed`（`1 skipped` = 缺 `fastembed`
  的既有环境跳过项，非本次改动引入；`3 xfailed` 为既有 xfail 标记）。§6 第 3 条要求的
  `test_clean_tree_passes_and_roots_only` 在全量序下**绿**。
- **顺序无关验收**（§6 第 1 条）：`pytest tests/test_k2_pairs_gen.py tests/test_work_registry.py -q`
  → exit 0 全绿（改前该组合报 `n_mismatch=13`，条数随组合变化）。
- **新增回归**（§6 第 2 条）：`pytest tests/test_registry_anchor_order.py -q` → 4 passed。
- **§5「未 commit / merge / push」的更正**：该句描述的是**执行代理会话内**的边界（执行代理
  确实未提交）；收口动作由主控完成——`689eab6`（修复）+ `9e2c3f8`（merge）。执行代理未越权，
  但文档口径与提交形态不一致，此处更正为「执行代理未 commit；主控收口提交 `689eab6`/`9e2c3f8`」。
- 会审另两席提出的「`test_strategy_stats_rebuild.py` / `test_k4_registered_world_preflight.py`
  是否缺 `sys.path` tests 路径」：已逐文件核实——前者 `:26-27` 插入 `ROOT` 与 `ROOT/scripts`，
  且在 `:97-98` 用**函数内** `from registry_anchor import refresh`（`scripts` 侧 import 时
  `tests` 已在 `sys.path`，全量序实测绿）；后者 `:22` 显式插入 `ROOT/tests`。⇒ 两处**不是必红**，
  glm 席的「一般」级担忧经实跑排除。
