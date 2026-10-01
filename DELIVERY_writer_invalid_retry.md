# DELIVERY：写手侧「网关结果无效」同角色可恢复重试（2026-10-01）

worktree：`F:\agi\_scratch\worktrees\lg-writer-invalid-retry`（基线 `db4996d`）。
**已 commit（`0beb81a`），未 push、未写真库、未调真实模型**（全程注入式 httpx
MockTransport 假网关，零额度消耗）。值班 agent 接手后并入 main（`1de37fe`）
⇒ 合并提交 `f83d3d8`，合并后同批回归 **148 passed**。口径文档：`docs/WRITER_INVALID_RETRY.md`。

### 覆盖边界（如实声明，勿当成「唯一改动点」）
`_call_writer` 只覆盖 `run()` 循环里写手的**主调**一次。以下调用点仍是裸
`_call`、收到 `gateway_invalid_or_partial_result` 仍当场抛（未覆盖，已知遗留）：
① `_parse_or_retry`（写手契约重试的第二次调用，`writer.N.retry`）；
② `_verified_answer_or_retry`（校验席契约重试）；
③ `_repair_artifact` 内的校验席返修调用。
三者命中率远低于主调（K5 实测该类 4/46 全部出在写手主调），故本轮只修主调；
残留点位已在此登记，下一轮如需可同口径扩展。

### 已知口径变化（预算紧张时的失败码迁移）
写手重试经真 `reserve_call` 扣预算 ⇒ 预算只剩 1 位时，重试会占掉该位、随后
校验席被闸拦，最终失败码从 `gateway_invalid_or_partial_result` 变为
`call_budget_exhausted`（§2.3 探针 `budget-only-1-left` 即此形态，用例
`test_writer_retry_blocked_when_last_call_slot_is_left` 已固化）。
即「两次都无效 ⇒ 失败原因名一字不变」只在**预算够**的分支成立；下游若按
`error` 字段聚合 K5，需知这条迁移。

## 1. 交付清单（全部在允许编辑白名单内）

| 文件 | 状态 | 内容 |
|---|---|---|
| `app/scene_runtime/pipeline.py` | 改 | 新增模块常量 `INVALID_RESULT_FAULT`（引用 `client.py` 既有字面量，**判据本身未动**）；`_call_verified` 改用该常量（判定逐字不变）；新增 `SceneRunner._call_writer`（写手侧同角色重试 1 次，返回 `(reply, stage)`）；`run()` 的 `writer.{round}` 调用点改走 `_call_writer` 并接住返回的最终 stage 供契约重试续名 |
| `tests/test_writer_invalid_retry.py` | 新 | 11 项（8 个用例函数，其中 1 个 parametrize 三形态）：正向 3 形态（空 content / `finish_reason=length` / 响应缺 content ⇒ 重试后提交成功且落账正文=重试那次真实回复）、反向①两次都无效仍抛同名故障、反向②重试计入 `usage.calls`（`max_calls=2` ⇒ 恰好 2 次且 verifier 被闸拦）、反向③a `model_identity_mismatch` 不触发、反向③b `invalid_model_contract` 走自己的口径、反向③c `gateway_http_500` 不触发、反向④两次请求/台账模型逐字相同、反向⑤只剩 1 次预算时重试被 `call_budget_exhausted` 拦、叠加用例（无效重试 + 契约重试 stage 不撞） |
| `docs/WRITER_INVALID_RETRY.md` | 新 | 口径、与校验席重试的逐条异同表、落账位置、复核 SQL |
| `DELIVERY_writer_invalid_retry.md` | 新 | 本文件 |

任务书条目核对：1（写手侧同角色重试、只重试这一类）✓ 2（经真 `_call` 计入
`calls` 表与 `usage.calls`）✓ 3（role 不变 ⇒ 模型唯一，结构性禁止换模型）✓
4（判据/`align_quotes`/`validate_review`/`contracts.py`/`effect_gate_snapshot.py`
逐字未改）✓ 5（上限 1 次、失败原因名不变）✓。

## 2. 真跑命令与真跑输出原文

venv python：`F:/Hermes/hermes-agent/venv/Scripts/python.exe`；
工作目录均为 `F:\agi\_scratch\worktrees\lg-writer-invalid-retry`。
（`LG_LOCK_DIR` 只是把 pytest 的整轮互斥锁隔离到检出之外的 conftest 卫生项，
不参与任何判据；§2.1 的验收命令**未设**它，输出里的 OPEN-4 warning 就是
如实记录，conftest 收尾已自删临时 `data/` 目录。）

### 2.1 验收命令（逐字为派工给的命令，未加任何环境变量）

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_writer_invalid_retry.py -q
...........                                                              [100%]
============================== warnings summary ===============================
tests\conftest.py:100
  F:\agi\_scratch\worktrees\lg-writer-invalid-retry\tests\conftest.py:100: UserWarning: [R6 测试卫生 OPEN-4] 未设 LG_LOCK_DIR：本轮 pytest 以源检出目录 F:\agi\_scratch\worktrees\lg-writer-invalid-retry\data 为整轮锁位（将创建 F:\agi\_scratch\worktrees\lg-writer-invalid-retry\data\live_run.lock）——干净 worktree 里这是意外副作用。建议跑套件前设 LG_LOCK_DIR=<临时目录> 把锁隔离到检出之外；本轮若由本会话创建该目录，收尾时会自动移除空目录。
    config.issue_config_time_warning(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
exit=0
```

收集数（`--collect-only` 尾行原文）：

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_writer_invalid_retry.py --collect-only
...
11 tests collected in 0.55s
```

（本仓 conftest 把 `-q` 的 passed 汇总行吞掉，故以「11 个点 + exit=0 +
collect 数」三证。）

### 2.2 全量相关回归（18 个测试文件，264 项，含本次新文件）

```
$ LG_LOCK_DIR=/tmp/lg_lock_reg2 F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest \
    $(grep -l "scene_runtime\|k4_paired_scenes\|effect_gate" tests/*.py) tests/test_compile_all.py -q
........................................................................ [ 27%]
........................................................................ [ 54%]
........................................................................ [ 81%]
................................................                         [100%]
============================== warnings summary ===============================
tests/test_compile_all.py::test_all_python_files_compile
  F:\agi\_scratch\worktrees\lg-writer-invalid-retry\app\api.py:1009: DeprecationWarning: invalid escape sequence '\_'
    Candidate.prompt_version.like("corrupt\_%", escape="\\")))

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
exit=0
```

`--collect-only` 尾行原文：`264 tests collected in 3.36s`。含既有钉死项：
`test_scene_runtime.py`（校验席无效重试、`invoke` 只记录不拦截的旧契约）、
`test_contract_retry.py`（契约重试口径、`.retry` 落账、预算闸）、
`test_model_identity.py`、`test_k4_worlds_dir_receipt.py`（离线收据键集不漂移）、
`test_docs_code_reconcile.py`（文档-代码对账）、`test_compile_all.py`（全库编译）。

### 2.3 台账直查探针（stdin 一次性命令，不落任何脚本文件）

场景：写手首次返回空 content（`INVALID_EMPTY`），重试返回合规 Draft。

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe - <<'PYEOF'
...（用 stdin 喂的解释器：真 GatewayClient + 真 SceneRunner，假 transport 注入）
PYEOF
status: committed | requests: ['kimi-k3', 'kimi-k3', 'mc22-flash']
| stage | status | error | requested_model |
|---|---|---|---|
| writer.0 | failed | gateway_invalid_or_partial_result | kimi-k3 |
| writer.0.retry | succeeded | - | kimi-k3 |
| verifier.0 | succeeded | - | mc22-flash |
usage: calls=3 contract_retries=0 verifier_invalid_retries=0
```

四类反向场景的台账/失败原因名（同一 stdin 探针，一次跑完）：

```
retry-still-invalid    fault=gateway_invalid_or_partial_result    calls=2
                         ledger=['writer.0:failed', 'writer.0.retry:failed']
budget-only-1-left     fault=call_budget_exhausted                calls=2
                         ledger=['writer.0:failed', 'writer.0.retry:succeeded']
identity-mismatch      fault=model_identity_mismatch:kimi-k3->deepseek-v4.1-flash calls=1
                         ledger=['writer.0:failed']
last-slot-left         fault=call_budget_exhausted                calls=3
                         ledger=['writer.0:succeeded', 'verifier.0:succeeded', 'writer.1:failed']
```

读法：`retry-still-invalid` 两行都 failed 且**失败原因名一字未变**；
`identity-mismatch` 只调了 1 次（无 `.retry` 行）；两个预算场景的重试请求
分别被预算闸拦住（`budget-only-1-left` 的重试占掉了第 2 个位导致 verifier
被拦；`last-slot-left` 的 `writer.1` 用掉最后一位后其重试根本发不出去）。

## 3. 反向自检（证明测试不是空转）

把重试逻辑**临时改坏**两次，跑同一验收命令，必须转红；改完立即恢复并用
`diff` 与备份逐字比对（`IDENTICAL to pre-mutation`），最终文件与自检前一致。

### 3.1 变异 M1：「重试后返回假 Draft」（任务书点名的变异）

改法（`_call_writer` 内）：仍真调一次网关（预算照扣），但**丢掉真实回复**、
本地伪造一个合规 Draft 返回。

```
$ ... -m pytest "tests/test_writer_invalid_retry.py::test_writer_invalid_result_recovered_by_same_model_retry[空content]" -q
E       AssertionError: 落账正文必须是重试那次真实回复（不是本地伪造的草稿）
E       assert '林穗把一枚钱放在桌上。' == '林穗把一枚钱放在桌上，又收了回去，灯花跳了一下。'
E         
E         - 林穗把一枚钱放在桌上，又收了回去，灯花跳了一下。
E         + 林穗把一枚钱放在桌上。

tests\test_writer_invalid_retry.py:186: AssertionError
FAILED tests/test_writer_invalid_retry.py::test_writer_invalid_result_recovered_by_same_model_retry[\u7a7acontent]
```

（命令行的 id 我按可读形式写了中文，pytest 输出行里的 id 原文是转义形式
`\u7a7acontent`——这就是上面的 FAILED 行。）另两个形态实测同样红，
FAILED 行原文分别是：

```
FAILED tests/test_writer_invalid_retry.py::test_writer_invalid_result_recovered_by_same_model_retry[finish_reason=length]
FAILED tests/test_writer_invalid_retry.py::test_writer_invalid_result_recovered_by_same_model_retry[\u54cd\u5e94\u7f3acontent]
```

三个形态的断言失败原文相同（都是上面那条
`AssertionError: 落账正文必须是重试那次真实回复（不是本地伪造的草稿）`）。

### 3.2 变异 M2：「重试白嫖预算、不调网关，直接返回假 Draft」

```
$ ... -m pytest tests/test_writer_invalid_retry.py -q
FAILED tests/test_writer_invalid_retry.py::test_writer_invalid_result_recovered_by_same_model_retry[空content]
FAILED tests/test_writer_invalid_retry.py::test_writer_invalid_result_recovered_by_same_model_retry[finish_reason=length]
FAILED tests/test_writer_invalid_retry.py::test_writer_invalid_result_recovered_by_same_model_retry[响应缺content]
FAILED tests/test_writer_invalid_retry.py::test_writer_retry_still_invalid_raises_identical_fault_name
FAILED tests/test_writer_invalid_retry.py::test_writer_retry_is_charged_to_call_budget
FAILED tests/test_writer_invalid_retry.py::test_writer_retry_never_switches_model
FAILED tests/test_writer_invalid_retry.py::test_writer_invalid_and_contract_retry_do_not_collide
exit=1
```

红的原因原文（各取一条）：

```
E       AssertionError: assert ['writer.0', 'verifier.0'] == ['writer.0', ... 'verifier.0']
E         At index 1 diff: 'verifier.0' != 'writer.0.retry'
tests\test_writer_invalid_retry.py:177: AssertionError
E       Failed: DID NOT RAISE RuntimeFault
```

（台账里少了 `writer.0.retry` 行 ⇒ 重试既没发生也没扣预算；两次都无效的场景
被假草稿顶掉 ⇒ 期望的 fail-closed 抛错消失。）

### 3.3 恢复核对

```
$ diff <临时备份> app/scene_runtime/pipeline.py && echo "IDENTICAL to pre-mutation"
IDENTICAL to pre-mutation
$ ... -m pytest tests/test_writer_invalid_retry.py -q
...........                                                              [100%]
exit=0
```

## 4. 未做 / 边界（不谎报）

- **未做**真实模型端到端验证（任务书纪律明令「不调真实模型」）：真网关下
  写手侧无效结果的**实际命中率**待下轮值班用 `scripts/k4_paired_scenes.py --live`
  小批真跑后，按 `docs/WRITER_INVALID_RETRY.md` §4 的 SQL 查
  `writer.N(failed/gateway_invalid_or_partial_result)` + 紧邻 `writer.N.retry`。
- **未做**逐臂收据侧的新计数：`usage` 只有 `contract_retries` /
  `verifier_invalid_retries` 两个计数器（`store.py` 与
  `scripts/k4_paired_scenes.py` 均不在允许编辑清单内），写手侧这次重试
  **不进任何计数器**，也不体现在收据 `retried` 标志上 ⇒ 复核只能查 `calls`
  台账（§4 已写明「不要拿收据 retried 当没重试过的证据」）。若下轮要把该口径
  进收据，需扩允许编辑清单到 `store.py` / `k4_paired_scenes.py`。
- **未做**对既有 K5 失败臂的补跑（本任务只交付机制，不改历史产物）。
- **未改**任何判据/门禁：`client.py`、`align_quotes`、`validate_review`、
  `contracts.py`、`effect_gate_snapshot.py`、`store.py` 及全部既有
  `tests/*` 逐字未动（§2.2 的 264 项为其通过证据）。
- **未写真库**：`D:\language-genome-data\language_genome.db` 未触碰；测试与
  探针全在 `tmp_path` / 临时目录的副本库上。
- **未 push / 未 merge 到远端**（提交 `0beb81a` + 合并 main `1de37fe` 的 `f83d3d8` 均在本地；合 main 由值班 agent 完成，远端推送仍待门禁）。
- 台账里的失败行仍照旧留痕（`writer.0` 行 `status=failed` +
  `error=gateway_invalid_or_partial_result`），K5 的失败类聚合口径不变。

## 5. 复核入口

- 口径/异同/落账/SQL：`docs/WRITER_INVALID_RETRY.md`。
- 契约重试与模型身份两轮的口径（本文前置任务）：`docs/MODEL_IDENTITY_AND_RETRY.md`。
