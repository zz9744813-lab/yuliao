# VERIFIER_REPAIR_ROUNDS：核验返修失败 = 可重试的轮次失败

口径文件。回答一个问题：**这次改动为什么是「预算/控制流」改动，而不是「判据放宽」。**

## 1. 事故分布（K5 十场 live，46 条真实臂失败的历史汇总）

```
verifier_contract_repair_exhausted 13     ← 最大一块
invalid_model_contract:Draft       13
rewrite_budget_exhausted:*         10
transport                           6
verifier_state_repair_exhausted     3
call_budget_exhausted               1
```

最大一块（13+3=16 条）**不是正文问题**，是核验席自己交出的工件（review JSON /
evidence 引用 / changes 清单）不合格。旧实现在这种情况**当场炸掉整批**。

现场产物（2026-10-01 07:4x 只读直读，历史产物 `out_k4_10*`）：

| 批次 | 成绩 | 失败条目 |
|---|---|---|
| `out_k4_10_live_20261001_mc12_r28` | 15/20 committed（历史最佳） | `s8/B invalid_model_contract:Draft`、`s9/A verifier_state_repair_exhausted` |
| `r18` | 14/20 | `s6/B verifier_state_repair_exhausted`、`s10/A verifier_contract_repair_exhausted` |
| `r27` | — | `s2/A rewrite_budget_exhausted:state_patch_not_authorized_by_plan,unresolved_hard_issue`、`s2/B verifier_contract_repair_exhausted` |

## 2. 改动前的两处 `raise`（逐字引用，基线 = main HEAD）

`app/scene_runtime/pipeline.py`，原文与行号：

```python
194                    review_stage = f"verifier.{round_index}.contract1"
195                    answer = self._call(job_id, review_stage, "verifier",
196                                        VERIFIER_SYSTEM, repair_input, budget)
```

（上面这段的返修只有**写死 1 次**：stage 名硬编码 `.contract1`，`i` 只有 1。）第 193 行那份
`repair_input` 里 `previous_review`/`contract_errors`/`repair_instruction` 三键，就是今天
`_repair_artifact` 里拼的同一份 payload。紧接着：

```python
197                    review = align_quotes(draft.text, parse_result(answer["text"], Review))
198                    if any(e in {"evidence_not_in_text", "duplicate_event_evidence"}
199                           for e in validate_review(plan, draft.text, review)):
200                        raise RuntimeFault("verifier_contract_repair_exhausted")   # ← 第 200 行
```

state 路径同款（第 217 行 `review_stage = f"verifier.{round_index}.state1"`），两处 `raise`：

```python
219                                        VERIFIER_SYSTEM, repair_input, budget)
220                    try:
221                        review = align_quotes(draft.text, parse_result(answer["text"], Review))
222                    except RuntimeFault:
223                        raise RuntimeFault("verifier_state_repair_exhausted")      # ← 第 223 行
224                    if "state_patch_not_authorized_by_plan" in validate_review(plan, draft.text, review):
225                        raise RuntimeFault("verifier_state_repair_exhausted")      # ← 第 225 行
```

写手轮次循环本身没变（第 163 行 `for round_index in range(budget.max_rewrites + 1):`），
但这两处 `raise` 从循环里**抛穿出去**：写手拿不到这一轮的反馈，
剩余 `max_rewrites` 轮次被白白丢弃。

`app/scene_runtime/contracts.py:128-129` 原预算域：`max_calls le=20`、`max_rewrites le=2`。

## 3. 改动后的语义

核验工件不可用 ⇒ **本轮失败**，不是整批中止：

- 错误码连同 `contract_errors` 原文拼成一条 fault 串（如
  `verifier_contract_repair_exhausted:invalid_review_json`），进写手的
  `mechanical_errors`，并在 `instruction` 里追加一句「这是核验席产物的缺陷，
  不是你的正文缺陷」的一次性说明；写手照旧用 `previous_draft` + `issues` 修稿路径接手。
- 写手剩余轮次照旧可用；`continue` 只跳过本轮的 `apply_review_decisions`。
- **只有轮次预算用尽**（`max_rewrites+1` 轮都拿不到合格核验）才失败，且失败原因
  **保留原名** `verifier_contract_repair_exhausted` / `verifier_state_repair_exhausted`
  （`RuntimeFault` 的 code = 冒号前段，可核性优先，不改写成别的名字）。
- 核验返修次数由写死 1 次改为受 `budget.max_verifier_repairs` 控制，
  stage 名 `verifier.{round}.contract{i}` / `verifier.{round}.state{i}`，
  `i` 从 1 到 `max_verifier_repairs`，每一次都在 calls 台账落账。
  `i=1` 的请求哈希与改动前那次写死的返修**逐字一致** ⇒ 旧台账重放仍可解析。

预算域（`contracts.py::Budget`）——**只动上限域与新增字段，默认值一字未改**：

| 字段 | 改动前 | 改动后 |
|---|---|---|
| `max_calls` | `default=6, ge=2, le=20` | `default=6, ge=2, le=60` |
| `max_rewrites` | `default=2, ge=0, le=2` | `default=2, ge=0, le=4` |
| `max_verifier_repairs` | —— | `default=1, ge=0, le=4`（新增） |

`default=1` ⇒ **未显式配置时行为与改动前逐字一致**（同样只返修一次、同样 stage `.contract1`/`.state1`）。
`ge`/`default` 一律不动，放宽的只有 `le`：即「允许运行期把上限配到更大」，
默认闸（`max_calls=6`）在未显式配置时照旧由 `store.reserve_call` 硬拒。

### 3.1 最坏调用数（会审 qwen 席点名的成本面，写清楚不给「不可逆额度爆炸」留口子）

一轮的调用数 ≤ `1（Writer） + 1（Verifier 首验） + max_verifier_repairs（返修）`，
总轮数 = `max_rewrites + 1` ⇒ **最坏调用数 = (max_rewrites + 1) × (2 + max_verifier_repairs)**。

| 配置 | 最坏调用数 | 实际闸 |
|---|---|---|
| 默认 `Budget()` = `(max_calls=6, max_rewrites=2, max_verifier_repairs=1)` | 3 × 3 = **9** | `max_calls=6` **先撞**，多出的 3 次根本发不出去 |
| K5/长篇 live 用 `(max_calls=40, max_rewrites=4, max_verifier_repairs=1)` | 5 × 3 = **15** | 15 ≤ 40，跑满轮次仍有余量 |
| 契约上界 `(60, 4, 4)` | 5 × 6 = **30** | 30 ≤ 60 |

⇒ **成本上界始终由 `max_calls` 单点硬控**（`store.reserve_call` 逐次原子拒绝），
上表的最坏值在任何合法配置下都不超过 `max_calls` 的**一半**，不存在「乘积爆炸绕过闸」
的路径；未显式配置时默认值 `(6,2,1)` 与改动前逐字一致。

**为什么不做 `@model_validator` 硬约束**：默认 `Budget()` 的最坏值 9 > `max_calls=6`
（这是**既有**默认预算就有的性质，改动前单轮最坏 3 次 + 2 轮 = 9 也一样），
加乘积硬约束会把**默认构造**直接判非法 ⇒ 会打红所有现有用例与 live 配置。
所以这里按会审建议的第二条（「至少在文档里写出最坏调用数」）执行，并把
`max_calls` 的硬闸性质用两个用例双向锁死。

## 4. 为什么这不是判据放宽（逐条对照）

| 可能被误解为「放宽」的地方 | 实情 |
|---|---|
| `validate_review` / `align_quotes` | **一字未改**（`git diff` 里这两个函数体零改动；用例第 5 节用同一非法输入做对照断言） |
| 非法工件会不会被「放过」成通过 | 不会。工件不合格时 `apply_review_decisions` **被跳过**，正史 revision 与 facts 零改动；轮次用尽必失败 |
| plan / 字数区间 / 计划事件校验 | 未触碰；`text_length_outside_plan`、`unresolved_hard_issue` 仍走原判据 |
| `max_calls` `le` 20→60 | 只放宽「可配置的上界的界」。默认值 6 未变，实际消费仍由 `store.reserve_call` 逐次硬拒（有用例锁死；最坏调用数见 §3.1） |
| `max_rewrites` `le` 2→4 | 同上，默认 2 未变 |
| `F:\Hermes\scripts\effect_gate_snapshot.py` / K4/K5 门 | 未触碰（不在本次 diff 内） |

一句话：**变的是「谁为核验席的错买单、买几轮」，不是「什么算合格」。**
判据函数是同一批函数、同一批阈值；改动只让写手拿回本该属于它的反馈与轮次。

## 5. 自检用例（`tests/test_verifier_repair_rounds.py`，18 个 test 函数 / 40 个用例）

全程 fixture + `ScriptedClient` 假 client：不写真库、不调真实模型。

反向自检（证明判据没放宽）：

- `test_permanently_illegal_artifact_never_becomes_a_pass`：核验席**恒**产出非法工件 ⇒
  最终仍失败，`commits == 0`、`revision == 0`、正史 fact 值一字未动。
- `test_same_illegal_review_stays_illegal_under_every_repair_budget`
  （`@pytest.mark.parametrize` × `max_verifier_repairs ∈ {0,1,2,3,4}` ×
  `evidence_not_in_text` / `state_patch_not_authorized_by_plan` / `duplicate_event_evidence`）：
  同一份非法 review 在**任意**预算取值下都判非法，且直调 `validate_review` 的探针结果
  与走管线的失败码逐字相等。
- `test_rounds_spent_keeps_the_original_failure_codes`：轮次用尽 ⇒ 失败码仍是
  `verifier_contract_repair_exhausted` / `verifier_state_repair_exhausted` 原名（`RuntimeFault` code 断言）。
- `test_contract_repair_stages_follow_max_verifier_repairs` /
  `test_state_repair_stages_follow_max_verifier_repairs`：stage 严格 `verifier.{round}.{kind}{i}`，
  `i` 到 `max_verifier_repairs` 为止；`max_verifier_repairs=0` ⇒ 一次返修都不做。
- `test_call_budget_is_still_a_hard_gate_on_retry_rounds`：重试轮次仍受 `max_calls` 硬闸
  （台账行数恰等配额、全部 `succeeded`、零提交）；**顶层失败码仍是工件根因**
  `verifier_contract_repair_exhausted`——2026-10-01 会审（qwen 席）修正：额度付不起下一轮时
  不许把归因降级成 `call_budget_exhausted`（那会让这次改动要保住的诊断信息丢失）。
  反向锁定见 `test_default_budget_reports_the_artifact_root_cause_not_the_symptom`
  （默认 `Budget()` 下收敛码必须以 `verifier_state_repair_exhausted` 开头且**不是**
  `call_budget_exhausted`）。
- `test_budget_defaults_unchanged_and_only_upper_bounds_widened`：默认值 `(6, 2, 1)` 锁死，
  越界值（`max_calls=61` / `max_rewrites=5` / `max_verifier_repairs=5` / 低于 `ge`）必 `ValidationError`。
- `test_default_budget_run_is_unchanged_two_calls`：默认预算下干净一轮 = `writer.0` + `verifier.0` 两次调用，与改动前逐字一致。

核心正向用例：

- `test_writer_round_two_recovers_from_verifier_artifact_failure`：写手第 1 轮被核验工件拖住、
  **第 2 轮**修好 ⇒ 现在 `committed`（旧实现在第 1 轮就 `raise`，整批炸掉）。
- `test_illegal_review_json_round_one_then_round_two_commits`：非法 JSON 形态的同款恢复路径。
- `test_round_failure_feedback_carries_the_verbatim_error`：回灌给写手的 `mechanical_errors`
  是错误码 + `contract_errors` **原文**；核验返修请求里 `text` 仍是同一份正文（返修不动正文）。
- `test_second_repair_attempt_can_recover_inside_one_round`：`max_verifier_repairs=2` 时
  第 2 次返修就成功 ⇒ **不烧写手轮次**（`writer_calls == 1`）。
- `test_repairable_artifact_never_burns_a_writer_round`：可修工件不占写手额度。
