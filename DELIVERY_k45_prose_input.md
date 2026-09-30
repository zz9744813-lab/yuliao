# DELIVERY — K45 签认链：评审输入补正文全文（2026-09-30）

工作目录：`F:\agi\_scratch\worktrees\lg-k45-prose-input`（分支 `task/k45-prose-input`）
基线：`2686076`（main HEAD）；本件只动 `scripts/k45_acceptance.py` 与
`tests/test_k45_acceptance.py`，并同步两份文档的口径描述。

## 1. 这件补的是哪一格（结构性缺口，不是风格问题）

`effect_gate_snapshot._k4_gate/_k5_gate` 的**唯一** PASS 入口是
`verify_acceptance()` 返回 `(True, ...)` 且 `decision == ACCEPT`；
`decision` 由两席判词确定性重算（`recompute_decision`：两票全 ACCEPT 才 ACCEPT）。

原评审输入是**五键**：`{scene, arm, prose_sha256, rubric, receipt_sha256}`——
**没有正文**。席被明确告知「正文哈希与 receipt_sha256 由外部链上核验，你无法也不得
据自填内容签发」。于是：**任何诚实的席都只能判 ABSTAIN**（输入里没有任何可据以判断
的证据）。2026-09-30 真产物 + 真两席模型实跑复现：`decision=ABSTAIN`。

⇒ 只要输入不含正文，K4/K5 两门**结构性不可翻**——与 K2/K3 那两处「写死 FAIL」
（注释写「同目录任意 JSON 自称人工 PASS 没有身份/签认链」）属同一类问题：
门在，但没有任何合法路径能让它 PASS。

## 2. 改法（补证据，不放宽判据）

| 处 | 改动 |
|---|---|
| `REVIEW_INPUT_KEYS` | 五键 → 六键：加 `prose`（该臂正文全文） |
| `committed_arms()` | 返回项加 `text`（正文）——**只在本模块内传递**，artifact 里始终只有哈希 |
| `review_input_for()` | 输入含 `prose = arm["text"]` |
| `mint_acceptance()` | 不变（本就用 `committed_arms()` 的臂对象构造输入） |
| `verify_acceptance()` | 用**产物**里的正文重算 `input_sha256`：`receipt_arm[(scene,arm)]` 传给 `_check_row_self_consistent` |
| `SYSTEM_PROMPT` | 说明 `prose` 为该臂正文全文，reason 须引用正文中的具体证据 |

**绑定强度一字不减**（三条同时成立才可能 PASS）：

1. `prose_sha256` 仍由正文逐字算出，`_check_arms()` 逐臂核它与**当前产物**相符
   （`k45_prose_sha_mismatch`）；
2. verify 侧用产物正文**原样重算整个 `input_sha256`**，改一个字即
   `k45_input_sha_mismatch`；
3. artifact 里**没有正文**——正文的唯一出处是产物，收据/artifact 不能自供正文。

## 3. 判据表（改动前后）

| # | 判据 | 改动前 | 改动后 |
|---|---|---|---|
| 1 | 输入键集 | 5 键（`REVIEW_INPUT_KEYS`） | 6 键（+`prose`） |
| 2 | 席可见证据 | 只有哈希 ⇒ 只能 ABSTAIN | 正文全文 + rubric ⇒ 可判 ACCEPT/BLOCK |
| 3 | `input_sha256` 重算源 | 输入五键 | 输入六键，`prose` 取自产物 |
| 4 | 篡改正文一字 | 拒（`k45_prose_sha_mismatch`） | 拒（`k45_prose_sha_mismatch` 或 `k45_input_sha_mismatch`） |
| 5 | artifact 需带正文 | 否 | **否**（仍只哈希） |
| 6 | 门可翻路径 | **不存在** | 存在（两席真读正文后全 ACCEPT） |

## 4. 复现命令

```bash
cd F:/agi/_scratch/worktrees/lg-k45-prose-input
TMPDIR=G:/tmp TEMP=G:/tmp TMP=G:/tmp LG_LOCK_DIR=G:/tmp/lg_lock \
  F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest \
  tests/test_k45_acceptance.py -q -p no:cacheprovider
# => 16 passed
```

正例 `test_mint_then_verify_passes_over_the_full_chain` 里新增断言：三臂打出去的
输入中 `prose` **逐字**等于产物正文（`TEXT_A/B/C`），且 `prose_sha256` 由正文算出。

## 5. 反例清单（本改动不引入任何放行口）

- 换正文一字（连 `receipt_sha256` 一起重倒签）：`k45_prose_sha_mismatch` / `k45_input_sha_mismatch`。
- 手写 artifact 自称 PASS：`k45_artifact_field_missing` / `k45_arms_missing` / `k45_gateway_audit_missing`。
- 自造「看起来签发过」的账本但 `input_sha256` 抄错：`k45_input_sha_mismatch`（六键重算）。
- 同一签发复用到多票：`k45_gateway_audit_replay`。
- `decision` 自填与两席判词不符：`k45_decision_mismatch`。

## 6. 为什么不改 `app/`、不碰网关

网关（`tools/attestation_gateway.py`）仍是唯一签发证明头并逐条记账之处；本件只改
「读产物 → 构造评审输入 → 派发 → 记收据 → 重算」里的输入构造与重算两处，
不新增判据、不删判据、不写库。
