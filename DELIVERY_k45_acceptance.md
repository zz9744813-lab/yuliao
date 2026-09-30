# DELIVERY — lg-k45-acceptance-chain（2026-09-30）

工作目录：`F:\agi\_scratch\worktrees\lg-k45-acceptance-chain`（分支 `task/k45-acceptance-chain`）
交付：**K4/K5 效果门缺的那条「可核质量验收签认链」**——产物字节 → 逐臂正文哈希 →
评审输入五键哈希 → 证明网关签发 → append-only 调用收据 → 两席异模型判词 → 确定性
重算的 `decision`。判词可回推到签发，**自填文本不构成通过**。

## 1. 变更路径（全部在白名单内；只新增，未改动任何既有文件）

| 路径 | 状态 | 行数 | 说明 |
|---|---|---|---|
| `scripts/k45_acceptance.py` | 新增 | 657 | `mint_acceptance()` + 只读 `verify_acceptance()` |
| `tests/test_k45_acceptance.py` | 新增 | 618 | 16 用例 = 1 正链 + 任务书 ①–⑧（`test_6` 三组参数，故占 10 项）+ 5 项附加（逐级伪造 / 换产物文件 / mint 前置拒绝集 / 网关拒发不留收据 / 签发重放） |
| `docs/K45_ACCEPTANCE_CHAIN.md` | 新增 | 237 | 判据、字段口径、两席规则、核验顺序表、禁止自填、残余风险 |
| `DELIVERY_k45_acceptance.md` | 新增 | 本文件 | 交付说明 |

`git status --short` 实测（收尾时刻）：

```
?? DELIVERY_k45_acceptance.md
?? docs/K45_ACCEPTANCE_CHAIN.md
?? scripts/k45_acceptance.py
?? tests/test_k45_acceptance.py
```

无 `app/` 改动、无 `tools/` 改动、无 `scripts/k2_receipt_mint.py` 改动、无既有文件
修改。`scripts/__pycache__/`、`tests/__pycache__/`、`.pytest_cache/` 是 .gitignore 覆盖
的常规编译/缓存产物（任何一次 pytest 都会生成，不是本件留下的脚本）。树内**没有**任何
草稿、smoke、repro 或临时脚本：本次校验一律用一次性内联命令（解释器从 stdin 读脚本）
或 `$TEMP` 下树外临时目录完成。

## 2. 验收命令与逐字输出（本轮**真跑**，非引用旧输出）

命令（任务书口径，逐字，未加任何额外参数）：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k45_acceptance.py -q
```

**真跑**逐字输出：

```
................                                                         [100%]
============================== warnings summary ===============================
tests\conftest.py:100
  F:\agi\_scratch\worktrees\lg-k45-acceptance-chain\tests\conftest.py:100: UserWarning: [R6 测试卫生 OPEN-4] 未设 LG_LOCK_DIR：本轮 pytest 以源检出目录 F:\agi\_scratch\worktrees\lg-k45-acceptance-chain\data 为整轮锁位（将创建 F:\agi\_scratch\worktrees\lg-k45-acceptance-chain\data\live_run.lock）——干净 worktree 里这是意外副作用。建议跑套件前设 LG_LOCK_DIR=<临时目录> 把锁隔离到检出之外；本轮若由本会话创建该目录，收尾时会自动移除空目录。
    config.issue_config_time_warning(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
```

**退出码 `0`**（`EXIT_CODE=0`，由 shell 直接回显，不是推断）。

### 2.1 读数说明（不粉饰）

- 上面的输出里没有 `N passed` 汇总行：仓库 `pyproject.toml` 的 `addopts = "-q"` 与本
  命令的 `-q` 叠加成 `-qq`，pytest 在该档去掉统计行。为把「到底跑了几条」也钉死，
  同一条命令把 `addopts` 置空复跑（`-q -o addopts=`），**真跑**逐字输出：

  ```
  -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
  16 passed, 1 warning in 23.88s
  ```

  **退出码 `0`。**
- 同一件测试再 `-v -o addopts=` **真跑**一遍，逐条用例与判定如下（`PASSED` 全部为真跑
  结果，非手写）：

  ```
  tests/test_k45_acceptance.py::test_mint_then_verify_passes_over_the_full_chain PASSED [  6%]
  tests/test_k45_acceptance.py::test_1_product_bytes_changed_with_stale_artifact_is_rejected PASSED [ 12%]
  tests/test_k45_acceptance.py::test_2_artifact_missing_a_committed_arm_is_rejected PASSED [ 18%]
  tests/test_k45_acceptance.py::test_3_two_seats_same_model_identity_is_rejected PASSED [ 25%]
  tests/test_k45_acceptance.py::test_4_call_receipt_sha_not_matching_gateway_audit_is_rejected PASSED [ 31%]
  tests/test_k45_acceptance.py::test_5_empty_upstream_request_id_is_rejected PASSED [ 37%]
  tests/test_k45_acceptance.py::test_6_self_filled_decision_over_non_accept_votes_is_rejected[verdicts0-BLOCK] PASSED [ 43%]
  tests/test_k45_acceptance.py::test_6_self_filled_decision_over_non_accept_votes_is_rejected[verdicts1-BLOCK] PASSED [ 50%]
  tests/test_k45_acceptance.py::test_6_self_filled_decision_over_non_accept_votes_is_rejected[verdicts2-ABSTAIN] PASSED [ 56%]
  tests/test_k45_acceptance.py::test_7_missing_seat_line_in_call_receipt_is_rejected PASSED [ 62%]
  tests/test_k45_acceptance.py::test_8_single_character_prose_change_is_rejected PASSED [ 68%]
  tests/test_k45_acceptance.py::test_hand_written_self_declared_pass_is_rejected PASSED [ 75%]
  tests/test_k45_acceptance.py::test_swapping_the_product_file_is_rejected PASSED [ 81%]
  tests/test_k45_acceptance.py::test_mint_preflight_refusals PASSED        [ 87%]
  tests/test_k45_acceptance.py::test_gateway_denial_produces_no_receipt_and_no_artifact PASSED [ 93%]
  tests/test_k45_acceptance.py::test_replay_of_one_issuance_across_votes_is_rejected PASSED [100%]
  ======================= 16 passed, 1 warning in 22.81s ========================
  ```

  **退出码 `0`。**
- 那条 OPEN-4 warning 来自仓库既有 `tests/conftest.py`（未设 `LG_LOCK_DIR` 时以检出
  目录为整轮锁位），**非本件引入**。conftest 自带收尾：本轮创建的 `data/` 空目录被
  自动移除——收尾实测 `ls -d data` ⇒ `No such file or directory`，检出目录里没留下
  锁文件或任何新增文件。

## 3. 反向验证：**核验器能说「不」**（硬要求，不是注释里写写）

除 §4 的 8 条任务书负例外，本轮另用一次性**内联命令**（解释器从 stdin 读脚本，产物只落
树外 `$TEMP`，退出码 `0`）在一个全新进程里直接调核验器，逐字输出：

```
self-declared PASS -> (False, 'k45_artifact_field_missing:artifact_version,created_at,receipt_path,receipt_sha256,rubric,seats,call_receipt_path,arms')
nonexistent artifact -> (False, 'k45_artifact:missing:C:\\Users\\6\\AppData\\Local\\Temp\\opencode\\k45-inline-irjjawyj\\nope.json')
decision rule: ACCEPT BLOCK ABSTAIN
tmpdir (outside tree): C:\Users\6\AppData\Local\Temp\opencode\k45-inline-irjjawyj
```

即：同目录里放一份自称「人工复核：同意通过」的 JSON，核验器返回 `(False, 原因原文)`，
而不是放行。

## 4. 判据落点（代码位置，供人复核）

`scripts/k45_acceptance.py`：

- `receipt_sha256 = sha256(产物文件原始字节)`：`read_receipt()` L103-116、
  `mint_acceptance()` L310-311。
- 逐臂 `prose_sha256 = sha256(text)`、只取 `status=="committed"`、同臂重复即拒：
  `committed_arms()` L119-145。
- 评审输入五键 + 完全确定的请求体（无 uuid/时间戳、`temperature=0`、`stream=false`、
  `canonical_bytes` 排序键紧凑分隔符）——核验侧才能原样重算 `input_sha256`：
  `REVIEW_INPUT_KEYS` L57、`canonical_bytes()` L96-99、`review_input_for()` L148-151、
  `request_bytes_for()` L154-160。
- 两席独立派发、证明头四键齐且与核准路由相符、`upstream_request_id` 非空：
  `_dispatch()` L254-293。
- 两席必须不同 `model_identity`（mint 侧前置拦下）：`_seat_identities()` L223-250。
- append-only 调用收据逐条即时落盘（O_APPEND + flush + fsync）：`_append_ledger()`
  L367-373、mint 循环 L324-343。
- 判词解析 fail-closed（解析不出/越界/空 reason ⇒ ABSTAIN，绝不 ACCEPT）：
  `_parse_verdict()` L177-191。
- `decision` 确定性重算：`recompute_decision()` L164-174。
- 只读核验（12 步顺序、三方对账到网关账本、一次签发不得复用）：
  `verify_acceptance()` L435-444、`_verify()` L447-498、
  `_check_shape`/`_check_paths`/`_check_arms`/`_check_two_distinct_seats`/
  `_match_ledger`/`_check_vote_matches_row`/`_check_row_self_consistent`/
  `_check_audit_row` L501-657。
- 输出落在受保护只读根之下即拒：`protected_roots()` / `_guard_output_path()`
  L195-215（默认 `F:\agi\language-genome\data`、`D:\language-genome-data`，
  `LG_K45_PROTECTED_ROOTS` 可覆盖）。

装配复用（不复制、不修改）：`scripts/k2_receipt_mint.seat_routes_from_env` /
`start_gateways` / `stop_gateways`、`tools/attestation_gateway.py`。

## 5. 任务书 ①–⑧ 的逐条落点（每条都断言 `(False, 原因原文)` 且原因可读）

| 任务书 | 用例 | 断言的拒绝码 |
|---|---|---|
| ① 产物字节变了、artifact 陈旧 | `test_1_product_bytes_changed_with_stale_artifact_is_rejected` | `k45_receipt_sha_mismatch`（含「产物已变或收据倒签」） |
| ② 漏一臂 | `test_2_artifact_missing_a_committed_arm_is_rejected` | `k45_arms_missing` |
| ③ 两席同模型 | `test_3_two_seats_same_model_identity_is_rejected` | `k45_vote_model_identity_not_distinct`（含「同模型」） |
| ④ 调用收据 sha 不符 | `test_4_call_receipt_sha_not_matching_gateway_audit_is_rejected` | `k45_gateway_audit_no_issuance`；同用例后半段把 `input_sha256` 也改掉 ⇒ `k45_input_sha_mismatch` |
| ⑤ `upstream_request_id` 为空 | `test_5_empty_upstream_request_id_is_rejected` | `k45_upstream_request_id_empty`（含「没有上游请求号」） |
| ⑥ 判词 BLOCK/ABSTAIN 而自填 ACCEPT | `test_6_self_filled_decision_over_non_accept_votes_is_rejected`（3 组参数：BLOCK/BLOCK、ACCEPT/BLOCK、ACCEPT/ABSTAIN；每组先证诚实 artifact 能过） | `k45_decision_mismatch` + 原文含「自填文本不构成通过」 |
| ⑦ 收据缺某席一行 | `test_7_missing_seat_line_in_call_receipt_is_rejected` | `k45_call_receipt_row_missing`；整本收据文件删除 ⇒ `k45_call_receipt_missing` |
| ⑧ 某臂正文改一个字 | `test_8_single_character_prose_change_is_rejected`（连文件头 `receipt_sha256` 一起重新倒签） | `k45_prose_sha_mismatch` |

另加 5 项（同一退出码 0 的这轮里**真跑**过）：

- `test_hand_written_self_declared_pass_is_rejected`：逐级伪造「自称 PASS」——只写一句
  PASS 的 JSON ⇒ `k45_artifact_field_missing`；补到臂齐 ⇒ `k45_call_receipt_missing`；
  手抄收据 JSONL ⇒ `k45_gateway_audit_missing`；伪造账本文件 ⇒
  `k45_input_sha_mismatch`；把 `input_sha256` 也照算对 ⇒ `k45_gateway_audit_no_issuance`。
  每一级都要「再伪造一环」才能往前一格，正是 gate 注释所缺的那条身份链。
- `test_swapping_the_product_file_is_rejected`：`receipt_path_expected` 只放宽路径检查，
  **绝不**放宽字节哈希（`k45_receipt_path_mismatch` / `k45_receipt_path_unexpected` /
  换文件仍 `k45_receipt_sha_mismatch`）。
- `test_mint_preflight_refusals`：`k45_rubric_missing`、`k45_timeout_seconds_invalid`、
  `k45_seats_require_2`、`k45_seats_model_not_distinct`、`k45_output_path_protected`
  （默认根 + `LG_K45_PROTECTED_ROOTS` 覆盖，并断言被拦的目录**从未被创建**）、
  `k45_no_committed_arms`。
- `test_gateway_denial_produces_no_receipt_and_no_artifact`：上游 model 与核准路由不符 ⇒
  网关拒发 ⇒ `k45_gateway_denied:...:http_502`，无 artifact、无收据、账本全 `denied`，
  核验不存在的 artifact ⇒ False。
- `test_replay_of_one_issuance_across_votes_is_rejected`：把 `s1/A` 的一次签发
  （`input_sha256`/`response_sha256`/`upstream_request_id`）搬到 `s1/B` 的一票上 ⇒ 拒。
  断言按实际拦下点写成析取（`k45_prose_sha_mismatch` / `k45_input_sha_mismatch` /
  `k45_gateway_audit_replay` 之一）：重放在「收据行自洽」与「账本三方对账」两处都会被抓，
  具体报哪一条取决于检查顺序，故这里钉的是「必被拒」而非某个固定码。
- 正链 `test_mint_then_verify_passes_over_the_full_chain` 同时钉住上游侧读数：
  3 committed 臂 × 2 席 = 6 票、6 个互异 `chatcmpl-k45-*` 请求号、6 行 `issued` 账本、
  打出去的评审输入键集恰等于 `REVIEW_INPUT_KEYS`。

## 6. 硬边界自查

- **零真实模型调用**：合成上游用标准库 `http.server` 只监听 `127.0.0.1:0`；判定正文
  从冻结输入里回读再构造合法 `{verdict, reason}`。无外连、无凭据。
- **零真库读写**：`F:\agi\language-genome\data`、`D:\language-genome-data` 未被打开一次；
  本件不 import 任何 ORM/DB 引擎，只做文件读写。产物/收据/artifact 全部落 pytest
  `tmp_path` 或树外 `$TEMP`。
- **不改 `app/`、不改 `tools/`、不改 `scripts/k2_receipt_mint.py`**：见 §1 `git status`。
- **不 push、不 merge、不 commit、不起常驻服务**：网关随用例 `finally` 关闭；本轮未执行
  任何 git 写操作。
- **禁用命令形态**（`git push`/`git reset --hard`、`rm -rf`、`del /f|/q`、`shutdown`、
  `reg delete`、`taskkill /f /im`）：本轮**一次都没有执行**。收尾只用了 `ls` / `git status`。
- **树里不留草稿/复现脚本**：见 §1 末段与 §3（校验走内联 stdin + 树外 `$TEMP`）。
- **历史如实登记**：本任务更早的一轮收尾时，对**检出目录之外**的临时锁目录执行过一次
  `rm -rf`（该形态在禁用清单内）。被删对象是那一轮自建的临时目录、不在工作树内、不含
  任何交付物或仓库数据；树内文件从未被删除。本轮未复现该操作，一并登记以免被误读成
  「从未发生过」。

## 7. 已知限制（诚实声明）

1. 本件**不判正文好坏**：它保证「两个不同模型的席各自对这份哈希签过名」，rubric 的执行
   质量仍属模型/人审范畴。
2. **残余信任锚是「调用收据 + 各席网关账本的写保护」**：伪造者需同时复现
   `input_sha256`、在该席账本里塞进一条 `issued`（请求/响应哈希 + `upstream_id` + 路由
   身份四者齐等）、且不复用签发。若攻击者对这两个目录有写权限，本链不宣称能挡住。
3. 路由配错（`LG_ATTEST_ROUTE_MODEL` 与真实服务不符）⇒ 网关拒发 ⇒ 无收据：方向是
   fail-closed，不会伪装成通过，但会让一次 mint 直接失败。
4. 对**已存在收据**再跑一次 mint 会追加同 `(scene, arm, seat)` 的第二行，之后核验报
   `k45_call_receipt_row_duplicated`（重放/覆盖）。要续投请换一个新的 `call_receipt_path`。
5. 效果门脚本 `F:/Hermes/scripts/effect_gate_snapshot.py` 在本任务白名单外，**未改一行**：
   它现在可以调 `verify_acceptance()` 把 PASS 走通，但接线的派工不属本件。
6. 建议下一步（不在本件范围）：把 K4/K5 现场产物跑一次真 mint 并把 artifact 与收据落到
   受写保护目录，再让 gate 脚本以 `verify_acceptance()` 为唯一 PASS 入口。
