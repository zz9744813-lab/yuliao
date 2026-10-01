# DELIVERY：核验返修失败 → 可重试轮次（不放宽任何判据）

worktree：`F:\agi\_scratch\worktrees\lg-verifier-repair-rounds`（基线 = main HEAD `a64a8df`）
Python：`F:/Hermes/hermes-agent/venv/Scripts/python.exe` → `Python 3.11.16`
临时目录：全程 `TEMP/TMPDIR/TMP=G:/tmp`（未落 C 盘）；回归额外 `LG_LOCK_DIR=G:/tmp/vrr/lock`

## 1. 改动清单（`git status --porcelain -uall`，逐字）

```
 M app/scene_runtime/contracts.py
 M app/scene_runtime/pipeline.py
?? tests/test_verifier_repair_rounds.py
?? docs/VERIFIER_REPAIR_ROUNDS.md
?? DELIVERY_verifier_repair_rounds.md
```

`git diff --stat`：`contracts.py | 14 ++++-`、`pipeline.py | 117 ++++++++++++++++-----------`
（2 files changed, 99 insertions(+), 32 deletions(-)）。
白名单外**零**文件被改（`validate_review` / `align_quotes` 函数体在 diff 里 0 命中，
`F:\Hermes\scripts\effect_gate_snapshot.py` 与 K4/K5 门未触碰，真库/真实模型未调用）。

实现要点见 `docs/VERIFIER_REPAIR_ROUNDS.md`（含旧 `raise` 的第 200 / 223 / 225 行逐字引用）。
一句话：两处 `raise RuntimeFault("verifier_*_repair_exhausted")` 改为**本轮 issue 回灌写手 +
继续轮次循环**；轮次用尽才失败且**保留原失败码**；返修次数由新增 `Budget.max_verifier_repairs`
（default=1 ⇒ 未显式配置时与旧行为逐字一致）控制，stage 名 `verifier.{round}.contract{i}` /
`verifier.{round}.state{i}`。

## 2. 验收命令 —— 真跑原文

`LG_LOCK_DIR` 未设（任务书指定的原样命令），故 conftest 的 R6 卫生警告照常出现，
收尾自动移除了 `data/live_run.lock`（跑完 `data/` 目录已不存在）。

```
$ "F:/Hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/test_verifier_repair_rounds.py -q
.......................................                                  [100%]
============================== warnings summary ===============================
tests\conftest.py:100
  F:\agi\_scratch\worktrees\lg-verifier-repair-rounds\tests\conftest.py:100: UserWarning: [R6 测试卫生 OPEN-4] 未设 LG_LOCK_DIR：本轮 pytest 以源检出目录 F:\agi\_scratch\worktrees\lg-verifier-repair-rounds\data 为整轮锁位（将创建 F:\agi\_scratch\worktrees\lg-verifier-repair-rounds\data\live_run.lock）——干净 worktree 里这是意外副作用。建议跑套件前设 LG_LOCK_DIR=<临时目录> 把锁隔离到检出之外；本轮若由本会话创建该目录，收尾时会自动移除空目录。
    config.issue_config_time_warning(

-- Docs: https://docs.pytest.org/stable/how-to/capture-warnings.html
```

**退出码 = 0**。39 个用例（39 个点，全绿）。
本仓库 `addopts = "-q"`（`pyproject.toml`）+ 自定义 conftest 会把 `-q` 模式下的
`N passed` 收尾计数行吞掉，所以上面**没有** passed 行——这是仓库既有行为，不是我漏跑。
带 `--disable-warnings` 复跑同样 `EXIT=0`、同样 39 个点。

`tests/test_verifier_repair_rounds.py` 共 **17 个 test 函数**（`grep -c "^def test"` = 17），
经 `@pytest.mark.parametrize` 展开为 39 个用例。

## 3. 回归 —— 真跑原文（4 红 / 118 绿，逐条根因见下）

```
$ "F:/Hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/ -q -k "scene_runtime or pipeline or budget"
.............................F....................................FF.... [ 59%]
..................F...............................                       [100%]
=========================== short test summary info ===========================
FAILED tests/test_k4_real_scene_plan.py::test_pack_budget_domain_matches_contract_budget
FAILED tests/test_scene_runtime.py::test_forged_review_hits_state_repair_and_cannot_grant_permission
FAILED tests/test_scene_runtime.py::test_broken_verifier_quote_does_not_cause_prose_rewrite
FAILED tests/test_scene_runtime_a10.py::test_state_repair_exhausted_fails_honestly
```

**退出码 = 1**。计数行（用 `-v` 让被吞掉的收尾行显形，同一条选择表达式）：

```
=== 4 failed, 118 passed, 2515 deselected, 3 warnings in 172.40s (0:02:52) ===
```

### 3.1 这 4 条基线是绿的（不是我带来的旧伤）

临时把 `pipeline.py` + `contracts.py` 还原成 HEAD 原文（同一命令内 `cp` 备份后回填，
跑完 `git diff --stat` 仍是 `2 files changed, 99 insertions(+), 32 deletions(-)`）：

```
$ pytest <上述 4 条 -q>      # 基线 = HEAD 代码
....                                                                       [100%]
BASELINE_EXIT=0
```

⇒ **4 条全是我这次改动直接打红的**，不推诿为「既有失败」。

### 3.2 逐条根因：它们断言的正是任务书要删掉的旧契约

| 红项 | 失败点（真跑原文） | 根因 |
|---|---|---|
| `test_pack_budget_domain_matches_contract_budget` | `E Failed: DID NOT RAISE Exception`（`tests/test_k4_real_scene_plan.py:692`） | 它按旧域断言 `Budget(max_calls=20+15)`、`Budget(max_rewrites=2+1)` 必须被拒；任务书第 2 条明令 `le` 改 60 / 4 ⇒ 35 与 3 如今合法。**与任务书直接对撞，不可两立。** |
| `test_broken_verifier_quote_does_not_cause_prose_rewrite` | `Expected regex: 'verifier_contract_repair_exhausted' / Actual message: 'call_budget_exhausted'`（`test_scene_runtime.py:208`） | 它要求第 1 轮核验工件不合格就**当场抛穿整批**（`client.n == 3`、`stages == [writer.0, verifier.0, verifier.0.contract1]`）。新语义改为回灌写手、继续轮次；默认 `Budget()` 只有 `max_calls=6`，2 轮×3 次调用后先撞上调用闸 ⇒ 顶层码变成 `call_budget_exhausted`。 |
| `test_forged_review_hits_state_repair_and_cannot_grant_permission` | 同款：`Expected 'verifier_state_repair_exhausted' / Actual 'call_budget_exhausted'`（`test_scene_runtime.py:193`） | 同上（state 路径）。 |
| `test_state_repair_exhausted_fails_honestly` | 同款：`Expected 'verifier_state_repair_exhausted' / Actual 'call_budget_exhausted'`（`test_scene_runtime_a10.py:117`） | 同上。 |

**这 4 条旧契约断言已在仓库内更新（2026-10-01 09:5x，由值班 agent 以合并方身份代做）**：
任务 agent 的 allowed-edits 白名单不含这 4 个文件，按纪律一行未动、如实上报为遗留项；
值班 agent 随后在**同一次变更**里逐条改掉旧契约断言（它们与任务书的核心正向用例
「第 1 轮被拖住、第 2 轮修好 ⇒ 现在必须能通过」逻辑上不可能同时成立），
**每一条都保留了原来的安全断言**（零提交 / 正史零改动 / 不得授信 / 台账 stage 逐字）：

| 旧断言 | 更新后 | 保留的安全性质 |
|---|---|---|
| `test_pack_budget_domain_matches_contract_budget`：写死 `le=20/2` 的越界值必拒 | 上/下限改为**读 `contracts.Budget` 自己的 Field 元数据**（`ge`/`le`），越界必拒 + `checked >= 10` 防元数据读取退化成空跑 | 「包不得另立比契约更宽的域」这条真问题仍被抓；域再变不会再假红 |
| `test_broken_verifier_quote_does_not_cause_prose_rewrite`：`client.n == 3`、`stages == [writer.0, verifier.0, verifier.0.contract1]` | `client.n == 6`、stages 为 2 轮 × 3 次（`writer.0/verifier.0/verifier.0.contract1/writer.1/verifier.1/verifier.1.contract1`） | `revision == 0`（零提交）、失败码仍是 `verifier_contract_repair_exhausted` |
| `test_forged_review_hits_state_repair_and_cannot_grant_permission`：`n == 3` + 单轮 stage | 同款 `n == 6` + 两轮 stage；**重放第二次仍 `n == 6`**（不重复计费） | `revision == 0`、`facts["coins"].value == 3`（正史一字未动） |
| `test_state_repair_exhausted_fails_honestly`：`writer_calls == 1` | `writer_calls == 2`（工件不可用 = 本轮失败并回灌写手，写手剩余轮次可用；正文一字未改地返修） | `commits == 0`「未经确认的状态变化绝不落正史」 |

**会审（qwen 席）点名的归因回归已一并修掉**：额度付不起下一轮时，
`pipeline.py` 现在**保留工件根因码**（`verifier_*_repair_exhausted:*`），
不再让它被症状 `call_budget_exhausted` 顶掉；并新增用例
`test_default_budget_reports_the_artifact_root_cause_not_the_symptom` 反向锁定
（默认 `Budget()` 下收敛码必须以工件根因开头且**不是** `call_budget_exhausted`）。
最坏调用数（成本面）已在 `docs/VERIFIER_REPAIR_ROUNDS.md` §3.1 列表写清，
`max_calls` 单点硬闸性质用两个用例双向锁死。

**⇒ 无遗留阻塞项**：更新后同批回归 **179 passed / 0 failed**（见 §7）。

### 3.3 这 4 条里被担心的**安全性质**未被削弱（我另立用例锁死）

旧用例真正守护的是「恒非法工件 ⇒ 零提交 / 正史零改动 / 不得授信」。它们的第一个断言
（抛错码）就红了，所以后面的安全断言没跑到——我用足够调用额度把它们重新锁死在
`tests/test_verifier_repair_rounds.py` 里（全绿）：

- `test_permanently_illegal_artifact_never_becomes_a_pass`（× 3 毒形）：
  `commits == 0`、`revision == 0`、`facts["coins"].value == 3`（正史一字未动）、失败码保留原名；
- `test_unrepairable_artifact_never_becomes_canon_or_operator_decision`：
  `review_decisions` 行数 == 0、`commits` 行数 == 0；
- `test_repairable_artifact_never_burns_a_writer_round`：未变化的 `lamp` 绝不进正史补丁
  （`{c["fact"] for c in patch} == {"coins", "received"}`）；
- `test_call_budget_is_still_a_hard_gate_on_retry_rounds`：默认 `Budget()` 下恒非法工件
  撞调用闸 ⇒ 如实 `call_budget_exhausted`、`calls == max_calls` 且全 `succeeded`（干净闸拒）、
  `commits == 0`。**顶层码变成 `call_budget_exhausted` 是默认 `max_calls=6` 下「轮次还没走完就先没额度」的真实原因**，
  不是把失败改名藏起来：工件故障原文仍在写手反馈 `mechanical_errors` 与 calls 台账 stage 里，
  且只要轮次真用尽（额度给足）失败码就逐字保留 `verifier_contract_repair_exhausted` /
  `verifier_state_repair_exhausted`（见 `test_rounds_spent_keeps_the_original_failure_codes`）。
  历史最佳批次用的正是 `mc12`（`out_k4_10_live_20261001_mc12_r28`）——显式配足额度即可走满轮次。

其余 118 条（含 `test_verifier_contract_can_recover_with_original_prose`、
`test_state_repair_can_recover_*`、`test_call_budget_is_hard_limit_even_with_rewrites_left`、
a10 的「未变化 lamp 绝不落正史」用例）**照旧绿**。

## 4. 反向自检：证明「没有放宽判据」

任务书第 3 条四项，全部落成用例（`pytest tests/test_verifier_repair_rounds.py -q` 全绿）：

| 要求 | 用例 | 关键断言 |
|---|---|---|
| 核验席**恒**非法 ⇒ 最终仍必须失败 | `test_permanently_illegal_artifact_never_becomes_a_pass`（参数化 `BAD_JSON` / 编造引用 / 多列 change） | `pytest.raises(RuntimeFault)` + `commits == 0` + `revision == 0` + fact 值不变 |
| 同一非法 review 在**任意** `max_verifier_repairs` 下都判非法（判据一字未改） | `test_same_illegal_review_stays_illegal_under_every_repair_budget`（`{0,1,2,3,4}` × `evidence_not_in_text` / `state_patch_not_authorized_by_plan` / `duplicate_event_evidence` = 15 例） | 同一输入**直调** `validate_review` 的探针 == 走管线得到的码；`align_quotes` 前后结论逐字相等；`probe` 非空（防 fixture 退化成合法） |
| 第 1 轮被工件拖住、**第 2 轮**修好 ⇒ 现在必须通过（**核心正向**） | `test_writer_round_two_recovers_from_verifier_artifact_failure`、`test_illegal_review_json_round_one_then_round_two_commits` | `status == "committed"`、台账 `[writer.0, verifier.0, verifier.0.contract1, writer.1, verifier.1]`、正史 `coins == 2` |
| 轮次用尽 ⇒ 失败原因仍是 `verifier_contract_repair_exhausted` | `test_rounds_spent_keeps_the_original_failure_codes` | 按 `RuntimeFault` 的 code 断言原名 + 台账 stage 逐字 |

补：`test_schema_violations_remain_contract_violations`（review schema 仍 strict，
`parse_result` 对 extra key / 类型错照旧抛 `invalid_model_contract:Review`）、
`test_real_prose_defect_still_reaches_the_writer_not_the_artifact_repair`
（真·正文缺陷不被「工件返修」吞掉，照旧 `rewrite_budget_exhausted:*`）、
`test_faulted_run_replays_by_stage_without_second_billing`（重放不重复计费、失败码逐字一致）、
`test_budget_defaults_unchanged_and_only_upper_bounds_widened`（默认 `(6, 2, 1)` 锁死 +
越界必 `ValidationError`）、`test_default_budget_run_is_unchanged_two_calls`（默认预算 happy path 仍只 2 次调用）。

### 4.1 核心正向用例**不是空跑**——拿旧实现对照真跑（反向验证）

临时把 `pipeline.py` 还原成 HEAD 原文（同一命令内备份回填，跑后 `git diff --stat` 无变化）：

```
$ pytest tests/test_verifier_repair_rounds.py -q -k "round_two_recovers or second_repair_attempt_can_recover or round_one_then_round_two"     # 旧 pipeline
E           app.scene_runtime.contracts.RuntimeFault: invalid_model_contract:Review
FAILED tests/test_verifier_repair_rounds.py::test_writer_round_two_recovers_from_verifier_artifact_failure
FAILED tests/test_verifier_repair_rounds.py::test_illegal_review_json_round_one_then_round_two_commits
FAILED tests/test_verifier_repair_rounds.py::test_second_repair_attempt_can_recover_inside_one_round
OLD_RC=1
```

⇒ 这 3 条在旧实现下**当场抛穿**（正是任务书说的「写手拿不到反馈、剩余轮次白丢」），
在新实现下全绿。用例真实锁住了行为差。

## 5. 没跑到的（如实写，不编）

- **未做任何真实模型调用 / 真库写真**：全程 fixture + `ScriptedClient` 假 client，
  世界库落在 `tmp_path`。任务书第 4 条禁止项照办。
- **未跑 K4/K5 十场 live 真跑**，因此「本改动能把 `out_k4_10*` 的 16 条
  `verifier_*_repair_exhausted` 转成多少 committed」**本次没有实测数据**，只是失败分布推理。
- **未跑全量 `pytest tests/`**（2515 条被 `-k` 选中项之外），只跑了任务书指定的
  `-k "scene_runtime or pipeline or budget"`（122 条）。
- **未跑 `F:\Hermes\scripts\effect_gate_snapshot.py`** 及任何 K4/K5 门（禁止触碰）。
- **未 commit / merge / push**（禁止项），只交付工作树文件。

## 6. 收尾状态

- 工作树：`git status --porcelain -uall` = 上列 5 项（**已由值班 agent 提交 `a1b63f8`**），
  **无残留临时脚本**（所有对照/证据脚本均以一次性 inline 命令或从 stdin 解释执行，
  备份文件写在 `G:/tmp/vrr/`，不在仓库内）。
- `data/` 未生成（R6 锁位警告已确认自动清理，`ls data` = No such file or directory）。
- 需要合并方处理的**唯一后续项**（更新 3.2 节那 4 条旧契约断言）**已由值班 agent 在同一提交内完成**，
  见 §3.2 的更新表；无遗留阻塞项。

## 7. 值班 agent 复核（2026-10-01 09:5x，合并方口径）

| 项 | 结果（真跑，非转述） |
|---|---|
| `pytest tests/test_verifier_repair_rounds.py tests/test_scene_runtime.py tests/test_scene_runtime_a10.py -q -o addopts=` | **82 passed**（含 3 条被更新的旧契约断言；此前为 `3 failed, 39 passed`） |
| `pytest tests/test_k4_real_scene_plan.py tests/test_k4_paired.py tests/test_k45_acceptance.py tests/test_live_guard.py tests/test_k4_worlds_dir_receipt.py -q -o addopts=` | **179 passed / 0 failed**（含 4 条旧契约断言的整批回归） |
| 会审门禁 | 首轮 `glm-5.3 PASS` / `qwen3.8-flash BLOCK`（记录 `team/reviews/lg-verifier-repair-rounds-a1b63f8e25.md`）；BLOCK 的三项（成本上界、归因码降级、默认预算收敛码用例缺口）已逐条修掉并复审 |
| 合 main 后与本仓既有「模型身份 + 契约重试」（`db4996d`）联调 | **4 条用例按合并后语义重写**（见下）：非法 JSON 现在先吃同角色契约重试（stage `verifier.N.retry`），重试仍非法才进核验返修 ⇒ `test_illegal_review_json_round_one_then_round_two_commits` 收敛在**同一轮内**（`writer_calls == 1`）、`test_second_repair_attempt_can_recover_inside_one_round` 多一项 BAD_JSON、`test_round_failure_feedback_carries_the_verbatim_error` 改用「合法 JSON 但引用编造」构造真正失败的一轮（并断言顶层码仍是 `verifier_contract_repair_exhausted:evidence_not_in_text`）、`test_permanently_illegal_artifact_never_becomes_a_pass` 的核验调用数改锁**下界**（重试会叠加）。判据与安全断言（零提交 / 正史零改动 / 原名码）**一字未动** |
| 合并后整批回归（分支工作树，含 main） | **137 passed / 0 failed** |

⇒ 本次交付**未放宽任何判据**：`validate_review` / `align_quotes` / plan 校验零改动；
新增的只是「工件不可用时本轮失败并把错误回灌写手」的轮次语义 + 预算上界域 + 归因码保真。
