# live 通道模型身份诚实化 + Draft 契约失败可恢复重试（2026-10-01）

任务书：`live 通道「模型身份诚实化」+ Draft 契约失败可恢复重试`。
本文是口径文档：讲清**证据、策略、落账位置、开关、如何复核**。实现文件：
`app/scene_runtime/client.py`、`app/scene_runtime/pipeline.py`、
`app/scene_runtime/store.py`、`scripts/k4_paired_scenes.py`；
测试：`tests/test_model_identity.py`、`tests/test_contract_retry.py`。

## 0. 两条实测证据（为什么做）

- **证据 A（模型身份不实）**：只读扫描 live 世界库 `calls.response` 的
  `requested_model`/`actual_model`（探针 `F:\Hermes\cache\scratch\model_subst_scan.py`）：
  76 个世界库 / 491 次调用里 42 次（8.6%）请求模型 ≠ 实际服务模型
  （如 `mc22-flash -> deepseek-v4.1-flash` ×38、`kimi-k3 -> opencode/nemotron-3-ultra-free` ×2）。
  即 litellm 网关会静默换上游 ⇒ 收据里的模型身份可能是假的，
  K4/K5「异模型」与 K2 两席的**前提**（两个名字=两个上游）可能不成立。
- **证据 B（契约偶发不合）**：K5 十场产物 46 条真实臂失败里
  `invalid_model_contract:Draft` 占 13 条（最大块之一）。实测原文（世界库
  `G:\tmp\k4_worlds__i6hb9kc`，stage=`writer.2`）：`finish_reason=stop` 而回复是
  「JSON 再包一层 text」形态 ⇒ 模型偶发不按契约输出，不是能力不足，
  旧代码 `_parse_contract` 一步失败直接 raise、**零重试**。

## 1. A：模型身份诚实化（fail-closed，不放宽任何判据）

### 1.1 记录（client.py）
`GatewayClient.invoke` 每次都从网关响应体取 `model` 字段，回复里同时带：
- `requested_model`（请求名，语义不变）
- `actual_model`（实际服务名；网关不报/报空 ⇒ `None`）
- `model_identity_ok`（= 两者一致）
- `substituted`（= 两者不一致，含「未上报实际模型」这一情形）

**invoke 本身保持「只记录不拦截」**（既有契约，`tests/test_scene_runtime.py::
test_gateway_one_request_actual_model_and_no_hidden_retry` 钉死直接调用侧语义），
拦截在 live 通道（见 1.2）。两种口径都不许静默：真相一律进回复与台账。

### 1.2 执行位置与策略（pipeline.py `SceneRunner._call`）
live 通道（SceneRunner 的每次调用，含 writer/verifier/retry 全部 stage）拿到
回复后检查 `substituted`：

- **默认 fail-closed** ⇒ 抛 `RuntimeFault("model_identity_mismatch:<req>-><actual>")`
  （网关未报实际模型时 `<actual>` 为 `unreported`）。抛错路径把**已收到的回复**
  一并交给 `store.finish_call` ⇒ 失败的 calls 行同样落 requested/actual/
  model_substituted，审计侧看得到被拒的皮，不存在吞掉的真相。
- **显式降级开关**：环境变量 `LG_ALLOW_MODEL_SUBSTITUTION=1`
  （认 `1/true/yes/on`，调用时读取、改 env 即时生效）⇒ 不抛，
  calls 行以 `succeeded + model_substituted=1` 如实落账。
  降级只影响「是否拦」，**不影响「是否记账」**。

### 1.3 落账（store.py）
`calls` 表新增持久列 `requested_model TEXT / actual_model TEXT / model_substituted INTEGER`。
加性迁移（`ALTER TABLE ... ADD COLUMN`），不抬 `RUNTIME_VERSION`：
历史世界库（改前 9 列表）逐字可开、既有行原样保留、迁移幂等。
`reserve_call` 落 requested（dispatch 即有），`finish_call` 以 COALESCE 补
requested/actual/substituted（失败路径也补）。

### 1.4 收据（scripts/k4_paired_scenes.py，仅 live 侧加键）
逐臂收据：
- `models`：**语义不变 = 请求模型**（绝不为凑门改指实际值）；
- `models_actual`：新增，取自 calls 台账的实际服务模型；同角色逐次出现多个
  不同实际模型 ⇒ 如实列有序清单，不取其一冒充全部；
- `model_identity_ok`：新增布尔，**每一笔**带请求侧记录的调用都
  actual==requested 才 True；无请求侧记录（离线夹具通道）⇒ 无法证明身份 ⇒
  False（fail-closed，不为凑绿放宽）；
- live 侧 `verifier_attempts[*]` 逐次加 `model_actual`，`usage` 加
  `contract_retries`。离线收据键集是既有契约（`test_k4_worlds_dir_receipt.py`
  钉死），**逐字不变**。

### 1.5 门禁消费侧口径
`effect_gate_snapshot.py::_check_live_paired` 的「一臂一模型对唯一」按**请求**
模型对判——本改动不动它。异模型前提的**加强复核**（人工/下游）应看：
`model_identity_ok == true` 且 `models_actual.writer != models_actual.verifier`。
两名字被同一上游服务（换皮撞车）时 `models_actual` 会暴露之（见测试
`test_same_upstream_under_two_names_is_not_ok`）。

## 2. B：Draft/Review 契约失败的同模型可恢复重试（pipeline.py / store.py）

1. **触发**：回复不合契约（真 `json.loads` + 真 `contract.model_validate` 失败：
   多包一层/缺字段/截断/双围栏/围栏外套话等）。
2. **动作**：**同 role 再调 1 次**（模型由 `client.models[role]` 唯一决定 ⇒
   **结构性禁止换模型重试**，保住「一臂一模型对唯一」门禁前提），stage 记
   `<stage>.retry`（与既有 `verifier.retry` 落账口径一致），输入附上一次回复
   原文 `previous_reply` 与校验错误原文 `contract_error`（截 4000 字防爆输入）。
3. **不放宽契约**：重试仍不合 ⇒ 照旧 raise `invalid_model_contract:<Name>`，
   且只重试这 1 次（无 `.retry.retry`）。
4. **不白嫖预算**：重试经 `_call` 正常 `reserve_call` ⇒ 计入 calls 表与逐臂
   `usage.calls`，同样过 `call_budget_exhausted` 闸（测试
   `test_retry_consumes_call_budget` 用 `max_calls=2` 实证）。
5. **不进改写额度**：重试发生在解析处，不占 `rewrite_budget` 轮。
6. **verifier 同享**：判定 JSON 不合契约同样重试 1 次；与既有「网关无效重试」
   （`gateway_invalid_or_partial_result` ⇒ `stage+'.retry'`）叠加时 stage 续名
   基于**最终落账 stage**，两口径分开计数互不冒充：
   `usage.contract_retries`（输入带 `contract_error` 的 `.retry`）vs
   `usage.verifier_invalid_retries`（不带的那类 verifier `.retry`）。
7. 附带：`add_confirmed_issue` 扫历史 writer 回复时跳过不合契约的
   succeeded 行（契约重试会把模型原文如实记为 succeeded——它是证据不是工件）。

## 3. 如何复核（不依赖本文措辞，直接查账）

```bash
# ① 测试（20 条：身份 8 + 契约重试 12；夹具假网关，零真实调用）
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest \
  tests/test_model_identity.py tests/test_contract_retry.py -q

# ② 台账直查（任一 live 世界库 k4.sqlite）：逐臂身份一致率
sqlite3 <世界库>/k4.sqlite "SELECT requested_model, actual_model,
  model_substituted, COUNT(*) FROM calls GROUP BY 1,2,3;"
# 请求≠实际且未降级的行：status='failed' 且 error LIKE 'model_identity_mismatch:%'

# ③ 收据直查（k4_paired.json）：models=请求、models_actual=实际、
#    model_identity_ok 为真才承认该臂模型身份可证
python -c "import json;d=json.load(open('out_k4_*/k4_paired.json',encoding='utf-8'));
[print(r['arm'],r['models'],r['models_actual'],r['model_identity_ok']) for r in d['receipts']]"

# ④ 存量真库交叉核对（只读）：探针 F:\Hermes\cache\scratch\model_subst_scan.py
```

## 4. 开关与语义速查

| 键 | 位置 | 语义 |
|---|---|---|
| `LG_ALLOW_MODEL_SUBSTITUTION=1` | 环境（调用时读取） | 身份不一致从 fail-closed 降为「如实记录」；**不改记账** |
| `calls.requested_model / actual_model / model_substituted` | 世界库新列 | 每次调用的模型身份真相，SQL 直查 |
| 收据 `models` / `models_actual` / `model_identity_ok` | k4_paired.json（live） | 请求（语义不变）/ 实际 / 是否逐笔可证一致 |
| `usage.contract_retries` vs `usage.verifier_invalid_retries` | 收据 usage / store.usage | 契约重试与网关无效重试分开记，互不吞 |
| `model_identity_mismatch:<req>-><actual>` | RuntimeFault 码 | 默认模式下的身份拦截（含 `->unreported`） |

纪律核对：未改任何判据/门禁（`effect_gate_snapshot.py` 一字未动）；
未写真库；未调真实模型（全部注入式 MockTransport）；契约本身一字未放宽。
