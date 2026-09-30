# K4/K5 效果验收的**可核签认链**（lg-k45-acceptance-chain，2026-09-30）

## 0. 这件东西补哪一格

`F:/Hermes/scripts/effect_gate_snapshot.py` 的 `_k4_gate` / `_k5_gate` 在全部结构性
检查通过后，尾部**写死** FAIL：

- `_k4_gate` ⇒ `"FAIL(K4 六臂结构可核，但缺可信质量验收签认)"`
- `_k5_gate` ⇒ `"FAIL(K5 真跑结构可核，但缺可信质量复核签认)"`

没有任何分支能返回 PASS，所以台账里的「效果门 0/4」里有一部分是**假停滞**。但拒
得对：注释给的理由是「同目录任意 JSON 自称人工 PASS 没有身份/签认链」——只要一份
自称 PASS 的 JSON 和产物躺在同一个目录里就算数，那这个门等于没有。

本件（`scripts/k45_acceptance.py`）把那条缺的链补上，**判据一字不改、`app/` 一行
不动**。补完之后，「通过」不再是一段文本，而是一串**可回推、可重算**的关系：

```
产物文件原始字节
   └─ receipt_sha256 = sha256(产物字节)
逐臂（status=="committed"）
   └─ prose_sha256 = sha256(text)
评审输入（六键，全部确定）
   {scene, arm, prose_sha256, prose, rubric, receipt_sha256}
   └─ input_sha256 = sha256(canonical(request_bytes))
两席独立派发（每席一个进程内证明网关 tools/attestation_gateway.py）
   └─ 网关签发 x-lg-upstream-{provider,model,channel-id,request-id} + 账本一行 issued
调用收据（append-only JSONL，逐条即时落盘）
   └─ verdict / reason / 两侧哈希 / upstream_request_id
两席异模型判词
   └─ decision = 确定性重算（不是任何人写下的字）
```

**结论先说死：自填文本不构成通过。** `verify_acceptance()` 只读、逐条重算，任一环
缺失或不符即 `(False, 原因原文)`，绝不静默放行。

---

## 1. 三个角色，各管一件事

| 角色 | 文件 | 写谁 | 读谁 |
|---|---|---|---|
| 产物（K4/K5 收据 JSON） | 调用方给定，例如 `out_k4_*/k4_paired.json` | 上游 K4/K5 线 | 本件只读，且**只读字节**（算哈希） |
| 调用收据（call receipt） | `*.k45_calls.jsonl`（append-only） | `mint_acceptance` 逐条追加 | `verify_acceptance` 逐行核对 |
| 签认 artifact | `*.k45_acceptance.json` | `mint_acceptance` 末尾写一次 | `verify_acceptance` 核验对象 |
| 网关账本 | 每席 `LG_ATTEST_*_AUDIT_PATH` 指向的 JSONL | `tools/attestation_gateway.py` | `verify_acceptance` 第三方对账 |

装配（席位路由 + 每席一个进程内网关）**复用** `scripts/k2_receipt_mint.py` 的
`seat_routes_from_env()` / `start_gateways()` / `stop_gateways()`，本件不复制也不修
改它们，更不修改 `tools/attestation_gateway.py`：网关是全仓唯一签发证明头并逐条
记账之处。

---

## 2. 字段口径（逐条钉死）

### 2.1 评审输入六键（`REVIEW_INPUT_KEYS`）

| 键 | 口径 |
|---|---|
| `scene` | 产物 `artifacts.prose[i].scene` 原文 |
| `arm` | 产物 `artifacts.prose[i].arm` 原文 |
| `prose_sha256` | `sha256(text.encode("utf-8"))`，`text` 取产物里的**原样字符串** |
| `prose` | **该臂正文全文**（`text` 原样，不截断不改写）——席据此按 rubric 判词 |
| `rubric` | 调用方传入的验收标准原文（空串即在 mint 阶段被拒） |
| `receipt_sha256` | `sha256(产物文件原始字节)`——注意是**文件字节**，不是重新序列化后的 JSON |

**为什么必须有 `prose`（2026-09-30 修，结构性缺口）**：原设计只给五键（只有
`prose_sha256`，没有正文），理由是「席不得据自填内容签发」。实测拿真产物、真两席
模型跑时，**任何诚实的席都只能判 `ABSTAIN`**——输入里没有任何可据以判断的证据
（席看不到正文，也不许凭空签名）。而 `effect_gate_snapshot._k4_gate/_k5_gate` 的
唯一 PASS 入口要求 `decision == ACCEPT`，`recompute_decision` 又是
「两票全 ACCEPT 才 ACCEPT」⇒ **K4/K5 两门结构性不可翻**（与 K2/K3 那两处
「写死 FAIL」同类，见任务书 §现状）。

补 `prose` **不放宽任何判据**：正文来源只有产物（`committed_arms` 从产物读出，
artifact 里始终只有哈希、没有正文）；`prose_sha256` 由正文逐字算出；verify 侧用
产物里的正文**原样重算整个 `input_sha256`**（`_check_row_self_consistent`），改一个
字即 `k45_input_sha_mismatch`；`_check_arms` 仍逐臂核 `prose_sha256` 与当前产物相符。
即：席能读到正文，但正文与链的绑定强度一字不减。

请求体完全确定（无 uuid、无时间戳、`temperature=0`、`stream=false`），序列化口径
是 `canonical_bytes()` = `json.dumps(..., ensure_ascii=False, sort_keys=True,
separators=(",", ":"))` 再 UTF-8 编码。**这是核验能原样重算 `input_sha256` 的前
提**：任何一处不确定性都会让重算永远对不上。

### 2.2 调用收据行（`LEDGER_FIELDS`，JSONL 每行）

`seat` / `provider` / `model_id` / `model_identity` / `upstream_request_id` /
`input_sha256` / `response_sha256` / `receipt_sha256` / `scene` / `arm` /
`prose_sha256` / `verdict` / `reason`（另附一行内的 `at` 时间戳，仅供人看，
**不参与任何判据**）。

- `provider` / `model_id` / `upstream_request_id` 一律取**网关响应头**的值，不取
  本地配置：本件写下的是「网关实际签发了什么」。
- `model_identity = provider + "/" + model_id`，两席独立性就以它为准。
- `response_sha256` 是上游响应体原始字节的哈希（网关账本同一行的
  `response_sha256` 必须逐字相等）。
- 凭据（`api_key`）**绝不**进收据、绝不进 artifact、绝不进审计。

### 2.3 签认 artifact 必备字段

`artifact_version`（当前 `k45-acceptance/v1`）、`created_at`、`receipt_path`、
`receipt_sha256`、`rubric`、`seats[]`、`call_receipt_path`、`n_arms`、
`arms[]`（每臂 `scene` / `arm` / `prose_sha256` / `votes[]`，`votes` 是该臂两票的
**收据行副本**）、`decision`。

---

## 3. 两席规则

1. **必须恰好两席**（`REQUIRED_SEATS = 2`）：`mint` 阶段不是两席即拒
   （`k45_seats_require_2`），`verify` 每臂再核一遍票数恰为 2
   （`k45_arm_votes_require_2`）。
2. **必须不同 `model_identity`**：同模型两票在 `mint` 就被拒
   （`k45_seats_model_not_distinct`），`verify` 侧同样拒
   （`k45_vote_model_identity_not_distinct`）——同一个模型投两次不叫独立签认，
   只是将同一份偏见复制了一遍。
3. **两席各自一个证明网关 + 各自一份账本**：一席的签发不能拿去给另一席作证。
4. 每臂每席**恰好一次**派发：不重试。派发不出结果 ⇒ 整体拒绝、不落 artifact，
   绝不留下「半截收据冒充一次验收」。
   （注意：`mint` 不去重——它总是逐臂逐席发一次请求；「每票在收据里恰有一行」是
   **核验侧**的判据。对**已存在收据**再跑一次 mint 会追加同 `(scene, arm, seat)`
   的第二行，之后核验报 `k45_call_receipt_row_duplicated`（重放/覆盖）。要续投请换
   一个新的 `call_receipt_path`。）
5. 判词解析 fail-closed：上游正文解析不出 `{verdict, reason}`、`verdict` 不在
   `ACCEPT/BLOCK/ABSTAIN` 之内、或 `reason` 为空 ⇒ 记 **ABSTAIN**，绝不记 ACCEPT。

---

## 4. `decision` 的确定性重算（禁止自填）

```
任一席 BLOCK                    ⇒ BLOCK
两席均 ACCEPT                  ⇒ ACCEPT
含 ABSTAIN 且无 BLOCK          ⇒ ABSTAIN
空判词集（没证据）             ⇒ ABSTAIN
```

`verify_acceptance()` 不看 artifact 里写的 `decision` 是什么，而是把**从收据行取出
的全部 `verdict` 重新算一遍**，不相等即拒（`k45_decision_mismatch`，原因原文里明
写「自填文本不构成通过」）。所以：两席判词是 BLOCK/ABSTAIN 而把 `decision` 手改成
ACCEPT ⇒ 必拒；反过来把 ACCEPT 改成 BLOCK 也拒——链上说的是「两席都判了 ACCEPT」，
写 BLOCK 就是假报告。

---

## 5. 核验顺序（`verify_acceptance` 逐条检查，任一句失败即 `(False, 原文)`）

| 序 | 检查 | 拒绝码（前缀） |
|---|---|---|
| 1 | artifact 是 JSON 对象、必备字段齐、版本受支持、`arms` 非空、`seats` 恰 2 且各字段非空、`rubric` 非空 | `k45_artifact_field_missing` / `k45_artifact_version_unsupported` / `k45_artifact_arms_empty` / `k45_artifact_seats_require_2` / `k45_artifact_seat_field_missing` / `k45_artifact_rubric_empty` |
| 2 | 核的就是指定的那个文件（传了 `receipt_path_expected` 则必须相等；没传则必须等于 artifact 记录的 `receipt_path`） | `k45_receipt_path_unexpected` / `k45_receipt_path_mismatch` |
| 3 | **`receipt_sha256` 必须等于当前产物文件字节哈希**（防倒签：产物换了字节，旧签认立刻失效） | `k45_receipt_sha_mismatch` |
| 4 | 覆盖当前产物**全部** `status=="committed"` 的臂，不许多也不许少；逐臂 `prose_sha256` 与当前产物相符 | `k45_arms_missing` / `k45_arms_extra` / `k45_prose_sha_mismatch` |
| 5 | 调用收据存在、逐行可解析、字段齐 | `k45_call_receipt_missing` / `k45_call_receipt_unparseable` / `k45_call_receipt_not_object` / `k45_call_receipt_field_missing` |
| 6 | 每席网关账本文件存在（**没有签发记录就没有签认**） | `k45_gateway_audit_missing` |
| 7 | 每臂两票、席位集与 artifact 一致、`model_identity` 非空且互异 | `k45_arm_votes_require_2` / `k45_vote_model_identity_empty` / `k45_vote_model_identity_not_distinct` / `k45_vote_seat_set_mismatch` |
| 8 | 每票在收据里**恰有一行**对应（无行=漏投，多行=重放/覆盖） | `k45_call_receipt_row_missing` / `k45_call_receipt_row_duplicated` |
| 9 | artifact 判词与收据行逐字段一致 | `k45_vote_ledger_mismatch` |
| 10 | 收据行自洽：`provider`/`model_id` 与该席身份一致、`model_identity` 拼法一致、`upstream_request_id` 非空、`verdict` 合法、`reason` 非空、`prose_sha256`/`receipt_sha256` 绑定、**`input_sha256` 按六键原样重算相符**（六键含正文 `prose`，正文取自产物） | `k45_seat_identity_mismatch` / `k45_model_identity_mismatch` / `k45_upstream_request_id_empty` / `k45_verdict_not_allowed` / `k45_reason_empty` / `k45_prose_sha_mismatch` / `k45_call_receipt_sha_mismatch` / `k45_input_sha_mismatch` |
| 11 | **三方对账**：收据行能在该席网关账本里找到同一次 `issued`（`request_sha256`/`response_sha256`/`upstream_id`/路由身份四者齐等），且一次签发不得复用到多票 | `k45_gateway_audit_no_issuance` / `k45_gateway_audit_replay` / `k45_gateway_audit_seq_missing` |
| 12 | `decision` 重算相符 | `k45_decision_mismatch` |

核验器自身若抛出任何未预期异常，也一律 `(False, "k45_verifier_error:...")`——
**出错不等于通过**。

注：第 6 步的账本读取发生在逐臂循环**之前**（一次性读入），所以缺账本文件会先于
`k45_input_sha_mismatch` 报出。测试断言按这个实际顺序写。

---

## 6. mint 侧的拒绝（派发前拦下，不落任何文件）

`k45_rubric_missing`、`k45_timeout_seconds_invalid`、`k45_receipt_missing`、
`k45_receipt_not_json`、`k45_receipt_not_object`、`k45_prose_not_list`、
`k45_arm_identity_missing`、`k45_arm_text_missing`、`k45_arm_duplicated`、
`k45_no_committed_arms`、`k45_seats_require_2`、`k45_seats_model_not_distinct`、
`k45_seat_field_missing` / `k45_seat_route_missing` / `k45_seat_identity_incomplete`、
`k45_output_path_protected`（输出落在受保护只读根之下：默认
`F:\agi\language-genome\data` 与 `D:\language-genome-data`，可用
`LG_K45_PROTECTED_ROOTS` 以 `;` 分隔覆盖）。

派发期：`k45_dispatch_failed`、`k45_gateway_denied`（网关 502 拒发）、
`k45_attestation_headers_missing`、`k45_attestation_provider_mismatch`、
`k45_attestation_model_mismatch`、`k45_upstream_request_id_empty`——任一命中即整体
拒绝，不落 artifact。

---

## 7. 用法

```bash
# 席位装配沿用 K2 的那一组环境变量（第 N 席带 LG_ATTEST_<N>_ 前缀，第 1 席可省）
export LG_ATTEST_UPSTREAM_BASE_URL=... LG_ATTEST_UPSTREAM_API_KEY=...
export LG_ATTEST_ROUTE_PROVIDER=... LG_ATTEST_ROUTE_MODEL=... LG_ATTEST_ROUTE_CHANNEL_ID=...
export LG_ATTEST_AUDIT_PATH=...        # 第 1 席账本
export LG_ATTEST_2_...=...             # 第 2 席（模型必须与第 1 席不同）
```

```python
from pathlib import Path
import scripts.k2_receipt_mint as k2mint
import scripts.k45_acceptance as k45

routes = k2mint.seat_routes_from_env(env, seat_count=2)   # 复用，不改
seats  = k2mint.start_gateways(routes)                    # 每席一个真网关
try:
    art = k45.mint_acceptance("out_k4_3_mc22_v2/k4_paired.json", rubric, seats,
                              timeout_seconds=120,
                              artifact_path=Path(".../k45.acceptance.json"),
                              call_receipt_path=Path(".../k45.calls.jsonl"))
finally:
    k2mint.stop_gateways(seats)

ok, why = k45.verify_acceptance("out_k4_3_mc22_v2/k4_paired.json",
                                ".../k45.acceptance.json")
print(ok, why)          # 判据口径见 §4/§5
```

---

## 8. 信任边界与残余风险（必须诚实写明）

1. **本件不判正文好坏**。它保证的是「两席独立模型确实各自签过这份哈希」，
   rubric 的执行质量仍属模型/人审范畴。链解决的是**身份与可核**，不是品味。
2. **收据与网关账本的写保护是残余信任锚**。伪造者要同时做到：复现
   `input_sha256`（六键 + 确定序列化）**并且**在该席网关账本里放得进一条
   `issued` 行（`request_sha256`/`response_sha256`/`upstream_id` 与路由身份四者
   齐等）**并且**该签发不被复用。因此**这两个文件必须落在只有签发方写得了的位置**
   （与 K2 消费侧 `_require_private_storage` 同一思路）。若攻击者对收据/账本目录有
   写权限，本链不宣称能挡住。
3. **上游身份由网关的核准路由决定**：路由配错（`LG_ATTEST_ROUTE_MODEL` 与实际服务
   不符）⇒ 网关拒发 ⇒ 无收据。这方向的失败是 fail-closed，不会伪装成通过。
4. **不做准入**：本件只造「质量验收签认」，不替代 K5 的其它门，也不写任何库。
5. 一次 mint 对每臂每席发出 1 次真实 HTTP 请求；测试全为合成上游 + 回环网关，
   **零真实模型调用、零真库读写**。

---

## 9. 测试

`tests/test_k45_acceptance.py`（16 项，全绿）：1 条正链 + 任务书要求的 8 条反向
（① 产物字节变了而 artifact 陈旧；② 漏一臂；③ 两席同模型；④ 调用收据 sha 与网关
账本不符；⑤ `upstream_request_id` 为空；⑥ 判词为 BLOCK/ABSTAIN 而 `decision` 自填
ACCEPT；⑦ 收据里缺某席一行；⑧ 某臂正文改一个字），另加：手写自称 PASS 的逐级伪
造、换产物文件核验（`receipt_path_expected` 只放宽路径、绝不放宽字节哈希）、
mint 前置拒绝集、网关拒发不留收据/不产 artifact、一次签发复用到多票。

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k45_acceptance.py -q
```
