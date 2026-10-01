# 写手侧「网关结果无效」同角色可恢复重试（2026-10-01）

任务书：`写手侧「网关结果无效」同角色可恢复重试（不放宽任何判据）`。
本文是口径文档：讲清**现场事实、口径、与校验席重试的异同、落账位置、如何复核**。
实现文件：`app/scene_runtime/pipeline.py`（**只此一个**）；
测试：`tests/test_writer_invalid_retry.py`（11 项，夹具假网关，零真实调用）。

## 0. 现场事实（为什么做）

- 判据在 `app/scene_runtime/client.py::GatewayClient.invoke`（**本次一个字未改**）：
  空 content / `finish_reason != "stop"` / 响应解析异常
  ⇒ `RuntimeFault("gateway_invalid_or_partial_result")`。
- 该类在**校验席**早已有同口径重试（`_call_verified`）；在**写手**席此前是
  `run()` 里直调 `_call`，**当场上抛、零重试** ⇒ 一条早场空响应直接炸掉该臂
  在该场景的全部后续场（`skipped_after_failure`）。K5 十场产物 46 条真实臂失败
  里该类占 4 条，近轮 r32/r35/r38 各 1 条。
- 本次改动**只增加重试次数**，不动任何判据、不动任何门禁、不动契约。

## 1. 口径（`SceneRunner._call_writer`，写手路径唯一改动点）

1. **触发**：`writer.{round}` 的 `_call` 抛
   `RuntimeFault("gateway_invalid_or_partial_result")`。判定是**精确相等**
   （`str(e) != INVALID_RESULT_FAULT` ⇒ 原样抛），不是前缀匹配 ⇒ 带后缀的
   `invalid_model_contract:Draft`、`model_identity_mismatch:<req>-><actual>`、
   `call_budget_exhausted` 等一律**不触发**、原样上抛。
2. **动作**：同一 role（`"writer"`）在 `stage + ".retry"` 再调 **1 次**，输入
   逐字不变（与校验席无效重试同一约定：网关无效是传输层形态，重发即可；
   不像契约重试那样附 `previous_reply`/`contract_error`）。
3. **结构性禁止换模型**：role 不变 ⇒ 模型由 `client.models["writer"]` 唯一决定
   （`_call` 里 `frozen["model"] = self.client.models[role]`），代码里不存在
   换模型路径 ⇒ 门要求的逐臂 `len(model_pairs) == 1` 前提不被破坏。
4. **不白嫖预算**：重试经真 `_call` ⇒ `reserve_call` 正常预约 ⇒ 进 `calls`
   表与逐臂 `usage.calls`，同样受 `call_budget_exhausted` /
   `context_budget_exceeded` 闸约束（预算耗尽即抛，不静默放宽）。
5. **次数上限 1 次**：无 `.retry.retry`（网关无效口径下）；第二次仍无效 ⇒
   原样抛**同一失败原因名** `gateway_invalid_or_partial_result`（fail-closed，
   失败原因名一字不变，K5 的失败类聚合口径不变）。
6. **不进改写额度**：重试发生在调用处，不占 `max_rewrites` 轮。
7. **stage 续名**：`_call_writer` 返回 `(reply, 实际落账 stage)`，后续契约重试
   （`_parse_or_retry`）基于**最终** stage 续名 ⇒ 网关无效重试与契约重试叠加时
   是 `writer.N.retry` + `writer.N.retry.retry`，两个 stage 名不撞
   （否则 `reserve_call` 会以 `call_input_conflict` 报一个假故障）。
   这与校验席 `_call_verified` → `_verified_answer_or_retry` 的既有约定一致。

## 2. 与校验席重试的异同（逐条对齐，差异只有「作用角色」）

| 维度 | 写手 `_call_writer`（本次） | 校验席 `_call_verified`（既有） |
|---|---|---|
| 触发字面量 | `INVALID_RESULT_FAULT`，精确相等 | 同一常量，同一判定 |
| 角色 / 模型 | `"writer"`，模型由 `client.models["writer"]` 定 | `"verifier"`，模型由 `client.models["verifier"]` 定 |
| 重试 stage | `writer.{N}.retry` | `verifier.{N}.retry` |
| 重试输入 | 逐字不变 | 逐字不变 |
| 次数上限 | 1 | 1 |
| 预算 | 经 `_call` 计入 calls / `usage.calls`，同闸 | 同 |
| 失败后 | 原样抛同一原因名 | 同 |
| 返回值 | `(reply, stage)` 供契约重试续名 | `(reply, stage)` |

**差异/未对称之处（如实记录，不是本任务的实现缺口）**：
- 计数口径不同：`usage.verifier_invalid_retries` 只统计 `verifier.` 前缀的
  `.retry` 行，写手侧这次重试**不进任何计数器**（`store.py` 不在允许编辑清单
  内）⇒ 只能从 `calls` 台账按 stage 名识别，见 §4。
- 契约重试（`usage.contract_retries`）与本重试是**两个口径**，互不吞、
  互不冒充；测试 `test_writer_invalid_and_contract_retry_do_not_collide`
  钉住两者叠加时的 stage 续名。

## 3. 未动的文件（一律逐字未改）

`app/scene_runtime/client.py`（无效判据）、`align_quotes`、
`validate_review`、`contracts.py` 的域、`effect_gate_snapshot.py`、
`store.py`、`scripts/k4_paired_scenes.py`、任何既有 `tests/*`。

## 4. 如何复核（不依赖本文措辞，直接查账）

```bash
# ① 测试（11 项：夹具 httpx MockTransport 假网关，零额度消耗）
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_writer_invalid_retry.py -q

# ② 台账直查（任一 live 世界库 k4.sqlite）：写手侧无效判定与它的重试
sqlite3 <世界库>/k4.sqlite "SELECT stage,status,error,requested_model FROM calls
  WHERE stage LIKE 'writer.%' ORDER BY rowid;"
# 判读：某个 writer.N 行的 status='failed' 且 error='gateway_invalid_or_partial_result'
#       且**紧邻**一条 writer.N.retry 的 succeeded ⇒ 本次口径的重试发生过并成功。
#       没有 writer.N.retry 行 ⇒ 该臂没吃到这条机制（或两次都无效，如实炸臂）。

# ③ 逐臂聚合复核（k4_paired.json）：本口径**不**体现在收据 retried 标志上
#    （retried 只看 verifier_invalid_retries / contract_retries），
#    故以 ② 的 calls 台账为准，不要拿收据 retried 当「没重试过」的证据。
```

## 5. 交付与纪律

- 交付物：`app/scene_runtime/pipeline.py`（改）、
  `tests/test_writer_invalid_retry.py`（新）、本文件、`DELIVERY_writer_invalid_retry.md`。
- 未写真库（`D:\language-genome-data\language_genome.db` 未触碰，测试全在
  `tmp_path` 副本库）；未调真实模型（全部注入式假 transport）；未改判据、
  未放宽门禁；未 commit、未 push。