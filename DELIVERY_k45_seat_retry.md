# DELIVERY lg-k45-seat-retry —— 签认链席位调用的**同席同输入**可恢复重试

工作树：`F:\agi\_scratch\worktrees\lg-k45-seat-retry`（分支 `task/k45-seat-retry`）
改动文件（只动了允许清单内的两处 + 本件交付说明）：

| 路径 | 状态 |
|---|---|
| `scripts/k45_acceptance.py` | 修改（`git status`：`M`） |
| `tests/test_k45_seat_retry.py` | 新建（14 项用例：13 passed + 1 skipped） |
| `DELIVERY_k45_seat_retry.md` | 本文件 |

`git status --short` 原文（收尾时全树只有这三项，`data/` 未留下、`.pytest_cache/` 被 gitignore）：

```
 M scripts/k45_acceptance.py
?? tests/test_k45_seat_retry.py
```

## 1. 改了什么

`mint_acceptance()` 的逐 (臂, 席) 循环体里，原来的两行

```python
outcome = _dispatch(seat, str(source.api_key), payload, float(timeout_seconds))
verdict, reason = _parse_verdict(outcome["body"])
```

包成 `_dispatch_and_parse(seat, api_key, payload, timeout_seconds, 1 + extra)`
（`scripts/k45_acceptance.py:389-411`）。`payload` 仍是**同一个 bytes 对象**逐次重发，
seat 与 `requested_model` 一律不变。新增模块级常量与三个小函数：

- `SEAT_RETRY_EXTRA_ENV = "LG_K45_SEAT_RETRY_EXTRA"`、`DEFAULT_SEAT_RETRY_EXTRA = 1`
  （`scripts/k45_acceptance.py:113-114`）；`_seat_retry_extra()`（`:355`）只接受非负整数，
  别的值 ⇒ 前置拒 `k45_seat_retry_extra_invalid` / `k45_seat_retry_extra_negative`，
  一次上游都不打。
- `_retryable_dispatch_error()`：重试验证**只有**这三类派发故障 ——
  `k45_dispatch_failed:*`（超时/连接失败/响应中断）、
  `k45_gateway_denied:<席>:http_5xx`（仅 500–599）、
  `k45_attestation_headers_missing:*`。**其余一律不重试**（4xx、
  `k45_attestation_*_mismatch`、任何未知错误码：默认不重试，宁可少打不多打）。
- `_unparseable_verdict()`：只有 `_parse_verdict` 给出的
  `malformed_review_response:*` 这一族才算「连判词对象都没解析出来」⇒ 可重试；
  可读判词（ACCEPT/BLOCK/ABSTAIN）一字不改地落行、绝不再打。
- 账本行新增**可选**字段 `attempts`（`OPTIONAL_LEDGER_FIELDS`，**只在 >1 时写入**）：
  没重试的行、以及所有已铸现网产物都没有这个键 ⇒ 旧行旧 artifact 不可能因此变红。

未改的东西（逐条对着硬约束）：`_parse_verdict` 的严格性、`recompute_decision`、
`verify_acceptance`/`_check_*` 的判定强度一字未减；`app/**`、门脚本、
`tools/attestation_gateway.py`、`scripts/k2_receipt_mint.py` 都没碰；没有写真库、
没有调真实模型（全部走本地合成上游 + 回环证明网关）。

## 2. 验收命令（值班会亲跑的那条）

`F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k45_acceptance.py tests/test_k45_seat_retry.py -q -o addopts=`

```
..........................s...                                           [100%]
29 passed, 1 skipped, 1 warning in 40.89s
```

（1 skipped = `test_9[team_k4_out_retry5]`：`F:\Hermes\team\k4_out\retry5_deepseek-v4.1-flash\`
下只有 `k4_paired.json`，**没有已铸 artifact** ⇒ 用例主动 skip，不谎报核过。该目录下
唯一已铸产物是 `k4_v2_WK-6e5d2623`，见第 ⑨/⑩ 条。）

回归（第 7 条）：`-m pytest tests/test_k45_acceptance.py -q -o addopts=` ⇒ **16 passed**
（改动前基线也是 16 passed，一条没变红）：

```
................                                                         [100%]
16 passed, 1 warning in 21.59s
```

顺带核了相邻线没被带坏：`tests/test_attestation_gateway.py tests/test_k2_receipt_mint.py
tests/test_compile_all.py` ⇒ `122 passed, 2 warnings`。

## 3. 逐条对应任务书要求的 7 项用例

`tests/test_k45_seat_retry.py -o addopts= -v` 原文：

```
tests/test_k45_seat_retry.py::test_1_unchangeably_unparsable_seat_stays_abstain_never_accept PASSED [  7%]
tests/test_k45_seat_retry.py::test_2_readable_block_verdict_is_dispatched_exactly_once PASSED [ 14%]
tests/test_k45_seat_retry.py::test_3_retry_reuses_identical_payload_bytes_and_model PASSED [ 21%]
tests/test_k45_seat_retry.py::test_4_retry_still_writes_exactly_one_ledger_row_per_arm_seat PASSED [ 28%]
tests/test_k45_seat_retry.py::test_5_retry_disabled_reproduces_todays_behaviour_verbatim PASSED [ 35%]
tests/test_k45_seat_retry.py::test_6_first_502_second_verdict_completes_the_whole_batch PASSED [ 42%]
tests/test_k45_seat_retry.py::test_7_http_4xx_and_identity_mismatch_are_never_retried PASSED [ 50%]
tests/test_k45_seat_retry.py::test_8_invalid_retry_env_is_a_preflight_refusal[abc-k45_seat_retry_extra_invalid] PASSED [ 57%]
tests/test_k45_seat_retry.py::test_8_invalid_retry_env_is_a_preflight_refusal[-1-k45_seat_retry_extra_negative] PASSED [ 64%]
tests/test_k45_seat_retry.py::test_9_existing_live_artifacts_still_verify_true[out_k4_3_live_20261001] PASSED [ 71%]
tests/test_k45_seat_retry.py::test_9_existing_live_artifacts_still_verify_true[team_k4_out_retry5] SKIPPED [ 78%]
tests/test_k45_seat_retry.py::test_10_previously_red_live_product_fails_for_the_same_reason PASSED [ 85%]
tests/test_k45_seat_retry.py::test_11_transport_layer_failures_are_retried_then_still_fail_closed[drop-k45_dispatch_failed:seat-1:] PASSED [ 92%]
tests/test_k45_seat_retry.py::test_11_transport_layer_failures_are_retried_then_still_fail_closed[no_headers-k45_attestation_headers_missing:seat-1:] PASSED [100%]
================== 13 passed, 1 skipped, 1 warning in 18.97s ==================
```

### ①（反向）恒不可解析体 ⇒ 重试后**仍**是 ABSTAIN，绝不变 ACCEPT
`test_1_...`：seat-1 每臂两次都吃到围栏包裹的伪 JSON（今天 deepseek-flash 探针那类
`JSONDecodeError`）。断言逐字为 `verdict=="ABSTAIN"`、
`reason.startswith("malformed_review_response:JSONDecodeError")`、`attempts==2`、
`decision == recompute_decision(行判词) == "ABSTAIN"`、且 seat-1 没有任何一行是 ACCEPT。
真跑输出（下面「第 4 节 demo」同场景原文）：

```
   s1/A seat-1  verdict=ABSTAIN attempts=2 input_sha=b5444b93ac74... reason=malformed_review_response:JSONDecodeError
   s1/A seat-2  verdict=ACCEPT  attempts=1 input_sha=3b13450c5c02... reason=合成席按 rubric 逐句核过：ACCEPT
   s1/B seat-1  verdict=ABSTAIN attempts=2 input_sha=1c884a6873fb... reason=malformed_review_response:JSONDecodeError
   s1/B seat-2  verdict=ACCEPT  attempts=1 input_sha=4a360c37f716... reason=合成席按 rubric 逐句核过：ACCEPT
   decision: ABSTAIN  verify: (True, 'OK(k45-acceptance/v1) 2 臂 × 2 席 = 4 票，decision=ABSTAIN，全部哈希/证明头/网关账本/两席异模型逐条重算相符')
```

### ②（反向）BLOCK ⇒ 只派发一次，判词逐字保留
`test_2_...`：断言 `counts["synthetic-seat-a"] == 2`（2 臂各 1 次，**零重试**）、
`row["reason"] == "视角越权：B 臂第二段直接写进老人内心，rubric 明令禁止。"`（逐字）、
`"attempts" not in row`、`decision == "BLOCK"`。
同场景 demo 原文（demo 里用的是短句 reason，用例断言的是上面那句完整原文）：

```
### block-once  error=None
   上游逐席调用次数: {'synthetic-seat-a': 2, 'synthetic-seat-b': 2}  逐 (席,臂) 尝试次数: {('synthetic-seat-a', 's1', 'A'): 1, ('synthetic-seat-b', 's1', 'A'): 1, ('synthetic-seat-a', 's1', 'B'): 1, ('synthetic-seat-b', 's1', 'B'): 1}
   s1/A seat-1  verdict=BLOCK   attempts=1 input_sha=b5444b93ac74... reason=视角越权：第二段写进老人内心。
   s1/A seat-2  verdict=ACCEPT  attempts=1 input_sha=3b13450c5c02... reason=合成席按 rubric 逐句核过：ACCEPT
   s1/B seat-1  verdict=BLOCK   attempts=1 input_sha=1c884a6873fb... reason=视角越权：第二段写进老人内心。
   s1/B seat-2  verdict=ACCEPT  attempts=1 input_sha=4a360c37f716... reason=合成席按 rubric 逐句核过：ACCEPT
   decision: BLOCK  verify: (True, 'OK(k45-acceptance/v1) 2 臂 × 2 席 = 4 票，decision=BLOCK，全部哈希/证明头/网关账本/两席异模型逐条重算相符')
```

### ③（反向）重试前后 `input_sha256` 完全相同、payload 字节未变、model 未变
`test_3_...`：三层取证 —— (a) 上游收到的**原始请求字节**按臂分组后每臂只有 1 个 distinct
值（`len(variants)==1`）；(b) 网关侧 seat-1 账本里 `denied` 与 `issued` 两批的
`request_sha256` **集合相等**（同一输入先拒后签），且 `row["input_sha256"] in denied`；
(c) 所有请求的 `model` 只落在 `SEAT_MODELS` 内、`row["model_id"]=="synthetic-seat-a"`。
demo 原文（seat-1 网关账本，denied 与 issued 的 request_sha 一模一样）：

```
   seat-1 网关账本: [('denied', 'upstream_status_502', 'b5444b93ac'), ('issued', None, 'b5444b93ac'),
                     ('denied', 'upstream_status_502', '1c884a6873'), ('issued', None, '1c884a6873')]
```

### ④（反向）发生重试时该 (臂, 席) 仍只有 1 行，`verify_acceptance` 仍 True
`test_4_...`（`LG_K45_SEAT_RETRY_EXTRA=2`：TypeError → KeyError → ACCEPT 三次尝试）：
`row_for()` 本身就是「该 (scene, arm, seat) 恰好一行，多一行即断言失败」的取行器；
`len(rows)==4`（2 臂 × 2 席）、键集无重复、`verify(...) is True`、
`"k45_call_receipt_row_duplicated" not in why`；artifact 每臂 `len(votes)==2` 且
`len({v["model_identity"]})==2`（两席异模型这个门要求没被重试破坏）。

### ⑤（反向）`重试次数=0` 时行为与今天逐字一致（同 stub 序列 ⇒ 同结果、同异常类型）
`test_5_...`：`LG_K45_SEAT_RETRY_EXTRA=0` + 与正向同一条 stub 序列（502 然后正常判词）⇒
`isinstance(error, k45.AcceptanceMintError)` 且
`str(error) == "k45_gateway_denied:seat-1:http_502"`（异常类型与消息原文都对得上）、
`counts==1`（一次都不许多打）、artifact 与账本**都不存在**。
不可解析体在 extra=0 时：每席只调一次、行里**没有** `attempts` 键、`decision=ABSTAIN`、
`verify` 仍 True。demo 原文：

```
### retry-off-502  error=AcceptanceMintError('k45_gateway_denied:seat-1:http_502')
   上游逐席调用次数: {'synthetic-seat-a': 1}  逐 (席,臂) 尝试次数: {('synthetic-seat-a', 's1', 'A'): 1}
   账本未落盘: True  artifact 未产出: True
```

同一条用例另断言 `k45.DEFAULT_SEAT_RETRY_EXTRA == 1` 且 `environ={}`（完全不配置）时
seat-1 走 2 次尝试而整批成功。

### ⑥（正向）第一次 502、第二次正常判词 ⇒ 整批成功 + 账本 1 行 + decision 按判词重算
`test_6_...` + demo 原文：

```
### 502-then-accept  error=None
   上游逐席调用次数: {'synthetic-seat-a': 4, 'synthetic-seat-b': 2}  逐 (席,臂) 尝试次数: {('synthetic-seat-a', 's1', 'A'): 2, ('synthetic-seat-b', 's1', 'A'): 1, ('synthetic-seat-a', 's1', 'B'): 2, ('synthetic-seat-b', 's1', 'B'): 1}
   s1/A seat-1  verdict=ACCEPT  attempts=2 input_sha=b5444b93ac74... reason=合成席按 rubric 逐句核过：ACCEPT
   s1/A seat-2  verdict=ACCEPT  attempts=1 input_sha=3b13450c5c02... reason=合成席按 rubric 逐句核过：ACCEPT
   s1/B seat-1  verdict=ACCEPT  attempts=2 input_sha=1c884a6873fb... reason=合成席按 rubric 逐句核过：ACCEPT
   s1/B seat-2  verdict=ACCEPT  attempts=1 input_sha=4a360c37f716... reason=合成席按 rubric 逐句核过：ACCEPT
   decision: ACCEPT  verify: (True, 'OK(k45-acceptance/v1) 2 臂 × 2 席 = 4 票，decision=ACCEPT，全部哈希/证明头/网关账本/两席异模型逐条重算相符')
```

断言还包括 `artifact["decision"] == recompute_decision(全部行判词) == "ACCEPT"`、
`row["upstream_request_id"].startswith("chatcmpl-k45sr-")`（拿到的是**第二次**真签发）。

### ⑦（回归）现网校验全绿
见第 2 节：`tests/test_k45_acceptance.py` **16 passed**，与改动前基线一致。

### 追加（任务书硬约束里没列进 7 条、但属于「越线」的部分）
- `test_7_...`：**4xx 与身份不符绝不重试** —— 用本地假网关复现
  `k45_gateway_denied:seat-1:http_404` 与 `k45_attestation_model_mismatch:seat-1`，
  各断言 stub 侧**恰好收到 1 个请求**、不产 artifact、不落账本。
- `test_8_...`：`LG_K45_SEAT_RETRY_EXTRA=abc` / `-1` ⇒ 前置拒
  （`k45_seat_retry_extra_invalid` / `k45_seat_retry_extra_negative`），`counts == {}`
  即一次上游都不打。
- `test_11_...`：另两类**该重试**的传输故障（连上就断 ⇒ `k45_dispatch_failed:seat-1:*`、
  200 但零证明头 ⇒ `k45_attestation_headers_missing:seat-1:*`）在 extra=1 时打 2 次、
  extra=0 时打 1 次；救不回来照样抛原错、不产 artifact（fail-closed 没松动）。

## 4. 向后兼容：已铸现网产物回归（真跑原文）

改动**后**跑（与改动**前**基线逐字相同）：

```
(True, 'OK(k45-acceptance/v1) 6 臂 × 2 席 = 12 票，decision=ACCEPT，全部哈希/证明头/网关账本/两席异模型逐条重算相符')
(False, 'k45_call_receipt_row_duplicated:zhl-c1-s1/A/seat-1 共 3 行（重放/覆盖）')
旧产物里是否存在 attempts 键: False
```

- 第 1 行 = `F:\agi\language-genome\out_k4_3_live_20261001\k4_paired.k45_acceptance.json`
  （+ 同目录 `.k45_calls.jsonl` + 对应 receipt / 两席 live 网关账本）：**True**，
  由 `test_9[out_k4_3_live_20261001]` 常驻断言。
- 第 2 行 = `F:\Hermes\team\k4_out\k4_v2_WK-6e5d2623\k4_paired.k45_acceptance.json`：
  这件**在本改动之前就已经是红的**（上面第 2 节之前我在未改动的代码上跑过同一条命令，
  返回原文一字不差），红因正是任务书点出的那条——整批重跑把行追加进同一本 append-only
  收据 ⇒ 同一 (scene, arm, seat) 出现 3 行 ⇒ `k45_call_receipt_row_duplicated`。
  本件没让它更红、也没有救它（`test_10_...` 把这条基线钉住：若它仍 False，原因必须是这条
  原文且不得出现任何与 `attempts` 相关的新拒）。
- `F:\Hermes\team\k4_out\` 下没有别的已铸 artifact（`retry1..retry5_deepseek-v4.1-flash`
  各目录只有 `k4_paired.json`）⇒ 那一档按 skip 如实标出。

## 5. 没做到 / 边界（不粉饰）

1. **默认值 1 的含义**：任务书写「默认额外 1 次……默认值写死为 1 ⇒ 不显式配置时行为与今天一致」。
   我的实现是 `DEFAULT_SEAT_RETRY_EXTRA = 1`（代码里写死、无需配置），但**不配置时确实会多打一次**：
   只有 `LG_K45_SEAT_RETRY_EXTRA=0` 才逐字回到今天的行为（⑤ 号用例钉的就是这一条）。
   若值班会审认为「默认必须为 0」，改 `scripts/k45_acceptance.py:114` 一个常量即可，用例 ①③④⑥ 会
   按预期转红，不会静默通过。
2. **真实 token 成本**：每次重试都是**真调同一个上游**，12 臂 × 2 席最坏 24 → 48 次调用。
   本件只做「同席同输入」重试，不做预算闸；调用方（任务书提到的
   `scripts/longform_acceptance.py`）**不在这份 worktree 里**（`find` 无此文件），
   我没有改它，也没法在这里把 `attempts` 计入它的调用预算。值班合入 live 工作树时若那条线有
   call-budget 核算，需要自行按 `attempts` 记账。
3. **`verdict_not_allowed:*` 与 `reason_missing` 不重试**：它们出现在 `_parse_verdict` 的
   fail-closed 分支里，但那是席**已经给出**的判词对象（只是值越界/依据为空），按
   「判词一旦可读一律不重试」的口径归到「不重试」。今天实测的 24 条失败全部是
   `malformed_review_response:*` 与 `http_502`，所以这一条不影响本次要救的场景。
   temperature=0 下重打也大概率得到同一个越界值，救它是另一件事（放宽判据），本件不做。
4. **未知错误码默认不重试**：`_retryable_dispatch_error` 是白名单，新增错误码若忘了登记 ⇒
   不重试（宁可白跑也不多打）。这是刻意的 fail-closed 选择。
5. **`docs/K45_ACCEPTANCE_CHAIN.md` 没更新**：不在允许清单内 ⇒ 文档 §4 的检查顺序里目前还
   没写重试这件事（代码注释与本文件写了）。需要补的话由值班会在合入时一并补。
6. **没有 commit / 没有 push / 没有合 main**（按 worker 指令）：任务书「交付」里那句
   "提交在本 worktree 分支" 我**没有做**，改动全部留在工作树（`git status` 见第 1 节），
   交值班走会审门禁后提交。
7. 用例里的 502 是**网关侧统一 502**（`deny_reason=upstream_status_502`），与今日实测
   `k45_gateway_denied:seat-2:http_502` 同一条码；我没有真的制造过上游超时（`timeout_seconds`
   走的是 `_dispatch` 既有的 httpx 超时 ⇒ 同一族 `k45_dispatch_failed`，由 ⑪ 号用例的
   「连上就断」覆盖）。
