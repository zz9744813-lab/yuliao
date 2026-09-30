# K2 语义收据铸造驱动器（2026-09-30 派工 lg-k2-receipt-mint）

## 0. 一句话

`app/semantic_review_runner.review_snapshot()` 是全仓唯一能写 `semantic_review_calls` /
`semantic_review_votes` 的入口，而它**没有 CLI**（源码 docstring 原话："No CLI or
default route silently turns this into a bulk run."）。本工单补的就是这件驱动器：
`scripts/k2_receipt_mint.py`——把已冻结的快照按**两席两条 route**真的投出去，逐条
记账，失败照实记；默认目标是 **fixture 库**，真库要双开关。

派工时现场读数（只读直核 `D:\language-genome-data\language_genome.db`）：
`semantic_review_snapshots` = 16、`semantic_review_calls` = 0、`semantic_review_votes` = 0。
收据链缺的就是这一段。

## 1. 两席怎么配

席位身份**就是** `app.semantic_review_runner.ReviewRoute`，字段名与校验全部用消费侧
现行的 `validate()`（本件不新增、不放宽、不改判据）：

```python
ReviewRoute(requested_model, upstream_provider, upstream_model, upstream_channel_id)
```

| 字段 | 判据（消费侧现行） | 说明 |
|---|---|---|
| `requested_model` | `_MODEL` 正则 + 禁 `agy/` `qoder/` `wb/` `zcode/` 前缀 + 必须等于 `config.canonical_model()` | 投给网关的 `model` |
| `upstream_provider` | `^[a-z][a-z0-9_-]{0,63}$` | 证明头 `x-lg-upstream-provider` |
| `upstream_model` | `_MODEL` 正则 | 证明头 `x-lg-upstream-model`，且**必须**等于上游响应体的 `model` |
| `upstream_channel_id` | 非空、≤128 字符 | 证明头 `x-lg-upstream-channel-id` |

**两席必须是不同 `upstream_model`**（本件在装配时就拒：`k2_seat_upstream_model_not_distinct`）。
理由与判据同源，不必另记：

- 消费侧按 `(snapshot_id, lower(model_id))` 投一票即锁（重复投 → `k2_model_already_voted`，
  `app/semantic_review_runner.py:466`）；
- 准入侧 `app/semantic_approval._current_two_pass_approval` 要求同 snapshot 恰好 2 票、
  `model_id` 互异、每票都挂一条 call 收据、**每模型恰好 1 次 call**，且两票都 PASS
  （`same_upstream_model` / `model_attempt_count_invalid` / `non_pass_vote:*`）。

配置方式（`LG_ATTEST_*`，第 N 席带 `LG_ATTEST_<N>_` 前缀；第 1 席兼容无前缀写法，
与 `tools/attestation_gateway.route_from_env()` 同一组键）：

| 变量 | 必填 | 含义 |
|---|---|---|
| `LG_ATTEST_UPSTREAM_BASE_URL` | ✔ | 上游 OpenAI 兼容根（如 `http://127.0.0.1:4000/v1`） |
| `LG_ATTEST_UPSTREAM_API_KEY` | ✔ | 上游凭据；只进网关，**绝不**落审计、绝不出证明头 |
| `LG_ATTEST_ROUTE_PROVIDER` | ✔ | 核准路由 provider |
| `LG_ATTEST_ROUTE_MODEL` | ✔ | 核准路由 model（证明头 model 恒等上游体 model） |
| `LG_ATTEST_ROUTE_CHANNEL_ID` | ✔ | 核准路由 channel |
| `LG_ATTEST_AUDIT_PATH` | ✔ | 网关 append-only 审计 JSONL 落点 |
| `LG_ATTEST_REQUESTED_MODEL` | — | 缺省 = `LG_ATTEST_ROUTE_MODEL` |
| `LG_K2_FIXTURE_ROOT` | — | fixture 库根目录（缺省 `%LOCALAPPDATA%\lg-k2-receipt-mint`） |

缺任一必填项 ⇒ `k2_seat_env_missing:seatN:<变量名>`，**不猜、不回落任何默认路由**。

## 2. 网关怎么起

证明头**不由本件生成**：全仓唯一生成
`x-lg-upstream-provider / -model / -channel-id / -request-id` 的模块是
`tools/attestation_gateway.py`（提交 b0a394b）。本件**每席各起一个**进程内网关：

- `AG.gateway_from_env(...)` 装配单条核准路由 + `make_httpx_forwarder(route)` 转发器 +
  `AuditLog(path)` 记账；
- `AG.make_server(gateway, "127.0.0.1", 0)` 绑定内核分配的临时端口，daemon 线程
  `serve_forever()`；
- `config.LLM_MODE = "real"`、`config.GATEWAY_BASE_URL = http://127.0.0.1:<port>/v1`、
  `config.GATEWAY_API_KEY = secrets.token_hex(16)`（每次运行随机，只在本网关内有意义）；
  循环结束在 `finally` 里**复原**原值（同进程内不污染其它调用方）。

硬约束：上游 HTTP 非 200 / 体非 JSON 对象 / 缺 `id` 或 `model` / 体 `model` 与核准
路由不符 ⇒ 网关**拒发**（502 + **零证明头** + 审计记 `denied`）⇒ 消费侧
`ReviewResponseError("k2_gateway_http_502")`，**不生成任何收据**。

## 3. 跑法

```powershell
# 默认：fixture 库（现造 2 条快照）× 2 席 × 2 快照 = 4 次派发
$env:LG_ATTEST_UPSTREAM_BASE_URL = "http://127.0.0.1:4000/v1"
$env:LG_ATTEST_UPSTREAM_API_KEY  = "<master key>"
$env:LG_ATTEST_ROUTE_PROVIDER    = "litellm"
$env:LG_ATTEST_ROUTE_MODEL       = "deepseek-v4.1-flash"
$env:LG_ATTEST_ROUTE_CHANNEL_ID  = "lg-k2-seat1"
$env:LG_ATTEST_AUDIT_PATH        = "$env:LOCALAPPDATA\lg-k2-receipt-mint\attest-seat1.jsonl"
$env:LG_ATTEST_2_UPSTREAM_BASE_URL = "http://127.0.0.1:4000/v1"
$env:LG_ATTEST_2_UPSTREAM_API_KEY  = "<master key>"
$env:LG_ATTEST_2_ROUTE_PROVIDER    = "litellm"
$env:LG_ATTEST_2_ROUTE_MODEL       = "kimi-k3"          # 必须与 seat-1 异模型
$env:LG_ATTEST_2_ROUTE_CHANNEL_ID  = "lg-k2-seat2"
$env:LG_ATTEST_2_AUDIT_PATH        = "$env:LOCALAPPDATA\lg-k2-receipt-mint\attest-seat2.jsonl"
python scripts/k2_receipt_mint.py --snapshots 2 --seats 2
```

CLI：`--snapshots N`（fixture 快照条数，默认 2）、`--seats N`（席数，默认 2）、
`--max-output-tokens / --max-request-bytes / --timeout-seconds`（消费侧预算上限）、
`--fixture-dir`、`--database`、`--i-know-this-is-live`。stdout 是机器可读 JSON 摘要，
rc：0 = 全部成功，1 = 有逐条失败记录，2 = 前置拒绝（没派发过一次）。

摘要每行字段：`snapshot_id / seat / model_identity / status / call_id / vote_id /
vote / error_type / error_message`，末尾 `readout` 是**现场读数**（四张表计数 + 逐
(snapshot, 模型) 的票数与 `calls_for_model`）。

**硬边界**：

- 默认目标是 fixture 库（`LG_K2_FIXTURE_ROOT` / `%LOCALAPPDATA%\lg-k2-receipt-mint`）；
- 写真库必须**同时**给 `--database <path>` 与 `--i-know-this-is-live`，且目标必须
  **已存在**（`live_database_missing`）——双开关也不许凭空造库；
- 每席每 snapshot **恰好一次**：失败只记账，不重试（`ReviewOutcomeUnknown` 重投会毁掉
  "每模型恰好一次"的准入判据）；
- fixture 根**不能**用 `%TEMP%`：Windows 上消费侧 `_require_private_storage` 实测拒
  `...\AppData\Local\Temp`（`k2_storage_acl_untrusted`，本工单实测），而 fixture 库
  恰恰要过真闸（测试不 monkeypatch）。

## 4. fixture 库怎么来的（为什么快照必然"当前"）

`new_fixture_database()` 全走仓库既有建表/写路径，不手搓 DDL：

1. `app.db.Base.metadata.create_all(engine)`；
2. `app.promotion_audits.ensure_promotion_audit_schema(engine)`（真 DDL + append-only 触发器）；
3. 塞 1 张 `replicated` 卡 + 1 条 `strategy_conditions` + 2 部登记作品（1 根 + 1 镜像）
   + 2 条 `verified` 实例（`src_ok` 严格 true、`reviewer_version=k2def-v1`、
   `text_version=corpus-v1`、span 与 `evidence_sha256` 自洽）+ 1 条 replicated 锚审计；
4. `app.semantic_receipts.ensure_semantic_schema(engine)`；
5. `app.semantic_review_store.freeze_snapshot(engine, ...)` × N 冻结待审快照。

因为快照是**当场按当前证据算出来的**，`review_snapshot` 里的
`verify_current_snapshot` 重算必然通过（`snapshot_stale` 不可能发生）。

## 5. 本工单实测读数

### 5.1 合成上游（`tests/test_k2_receipt_mint.py`，14 项全过）

链路是真的：私有落盘 sqlite（过真 ACL 闸）+ 仓库建表路径 + 每席一个真进程内网关 +
消费侧 `review_snapshot`；只有"上游模型"是标准库 HTTP 合成服务（`/chat/completions`，
回读冻结输入里的实例 ID 再构造合法四键判定正文）。覆盖：

- 2 快照 × 2 席 ⇒ calls = 4、votes = 4、approval_links = 0；逐 (snapshot, model)
  恰 1 票、每模型 1 次 call；上游侧同时断言打出去的就是**冻结输入**
  （system 提示逐字 = `runner.SYSTEM_PROMPT`、`temperature=0`、`stream=false`）；
- 重复投 ⇒ `ReviewPreflightError: k2_model_already_voted`，**且合成上游一次都没被打**
  （防重发生在派发之前），计数不增；
- 少发任一证明头（4 个头各测一次）⇒ `ReviewResponseError: k2_response_unverifiable`，
  计数不增；网关拒发路径 ⇒ `ReviewResponseError: k2_gateway_http_502`，计数不增；
- 上游不可达 ⇒ `ReviewOutcomeUnknown: k2_gateway_outcome_unknown`，计数不增；
- BLOCK / ABSTAIN 票面**原样**记账（不被改写成 PASS）；
- 真库写闸：`--database` 缺开关 ⇒ rc=2 且不落任何文件；双开关但库不存在 ⇒ rc=2。

#### 5.1.1 本轮为"跑得快且不偶发"改的两处（2026-09-30 复核）

上一轮验收命令在验证门上报红（单条
`test_missing_attestation_header_blocks_receipt[x-lg-upstream-model]` 失败、整轮
`exit_code=124` 超时）。本机现场复核定性为**环境负载打穿时间预算**，不是判据错：

1. 消费侧每**一次派发**起一个 `powershell.exe` 做
   `_require_private_storage`，`subprocess.run(timeout=15)`——超时即
   `ReviewPreflightError: k2_storage_acl_unverifiable`（不是 `ReviewResponseError`），
   于是"少发证明头"那条断言被**环境问题**顶掉；
2. 逐测试现造 fixture 库（`create_all` + 播种 + `freeze_snapshot`）在满载时是
   **1.5s → 25s** 的抖动项（实测某轮 setup 17-25s、call 29-36s）。

本机现场读数：同一时刻机器上跑着 ~40 个 `python.exe`（其它会话/服务），不可控。

两条收口（都只动 `tests/`，不碰 `app/`、不碰判据）：

- **fixture 模板化**：`template`（module 作用域）整轮只建一次库，每个测试
  `shutil.copyfile` 一份独立副本——建库成本 14 次 → 1 次；副本里 calls/votes
  从 0 开始、`semantic_review_attempts` 栅栏目录全新，测试之间不共享任何写状态；
- **派发前预热 ACL 探测**：`fixture_store` 在把库交给被测件之前先原地
  `runner._require_private_storage(Path(database))` 一次，把冷启动的 PowerShell
  开销挪到被测窗口之外，降低 15s 上限被偶发打穿的概率。

净效果：派发次数 45 → 15（反向自检按"1 快照 1 席"就足以坐实"不生成收据"），
整轮墙钟 43s → **23s**（连跑三轮 rc 均为 0）。**没有**因此删掉任何一条断言，
也**没有**加重试：判据读数仍是逐条原文。


### 5.2 真模型两席冒烟（本工单核心风险探针，1 快照 × 2 席，**只碰 fixture 库**）

上游 = 本机免费档 LiteLLM `http://127.0.0.1:4000/v1`（key 读
`F:\Hermes\secrets\litellm_master_key.txt`）；seat-1 = `deepseek-v4.1-flash`、
seat-2 = `kimi-k3`。2026-09-30 07:00Z 复跑原文：

```
$ python scripts/k2_receipt_mint.py --seats 2 --snapshots 1 --timeout-seconds 150
RC=0
results[0] = {"snapshot_id": "SS-0188e11ee46e4a308f33ef3f29d56beb", "seat": "seat-1",
              "model_identity": "litellm/deepseek-v4.1-flash", "status": "ok",
              "call_id": "K2C-29f98e8d43b04318bb0ba3d8490ddec0",
              "vote_id": "K2V-68846fe819804244adee4ae6cad8fb16",
              "vote": "ABSTAIN", "error_type": null, "error_message": null}
results[1] = {"snapshot_id": "SS-0188e11ee46e4a308f33ef3f29d56beb", "seat": "seat-2",
              "model_identity": "litellm/kimi-k3", "status": "ok",
              "call_id": "K2C-2ded1b6fb8a34fef9a36d63d9958aac5",
              "vote_id": "K2V-8cf4f6f70f5c4cdeb925e0403999cd7e",
              "vote": "BLOCK", "error_type": null, "error_message": null}
ok = 2, failed = 0
readout.counts = {"semantic_review_snapshots": 1, "semantic_review_calls": 2,
                  "semantic_review_votes": 2, "semantic_approval_links": 0}
readout.votes  = [{model_id: deepseek-v4.1-flash, verdict: ABSTAIN, calls_for_model: 1},
                  {model_id: kimi-k3,             verdict: BLOCK,   calls_for_model: 1}]
```

对 fixture 库只读直查的 SQL 原文：

```
SQL> SELECT 'semantic_review_snapshots', COUNT(*) FROM semantic_review_snapshots
   -> ('semantic_review_snapshots', 1)
SQL> SELECT 'semantic_review_calls', COUNT(*) FROM semantic_review_calls
   -> ('semantic_review_calls', 2)
SQL> SELECT 'semantic_review_votes', COUNT(*) FROM semantic_review_votes
   -> ('semantic_review_votes', 2)
SQL> SELECT 'semantic_approval_links', COUNT(*) FROM semantic_approval_links
   -> ('semantic_approval_links', 0)
SQL> SELECT model_id, provider, upstream_request_id, completed_at FROM semantic_review_calls
   -> ('deepseek-v4.1-flash', 'litellm', 'chatcmpl-REONeiIOpk79OrX3BO575VN6', '2026-09-30T06:59:57.500226Z')
   -> ('kimi-k3',             'litellm', 'chatcmpl-c3d54d84-dc53-4242-81a0-b90115f8af15', '2026-09-30T07:00:38.826655Z')
```

两席的网关审计各 1 行 `issued`（`request-id` 恒等上游体 `id`）：

```
attest-seat1.jsonl  issued deepseek-v4.1-flash chatcmpl-REONeiIOpk79OrX3BO575VN6  response_sha256=17f31f7cdd582bb5…
attest-seat2.jsonl  issued kimi-k3             chatcmpl-c3d54d84-dc53-4242-81a0-b90115f8af15  response_sha256=1415c2bdbd9277d1…
```

派发事件留痕（`semantic_review_attempts/`，逐次 `reserved → received → committed`）：

```
K2A-c7169844…  reserved@06:59:54 → received@06:59:57 → committed@06:59:57   (3.3s)  seat-1
K2A-fbad8550…  reserved@06:59:58 → received@07:00:38 → committed@07:00:38  (40.7s)  seat-2
两席 reserved 事件的 input_sha256 完全相同 = c0e88aef47ff4927ba267c5cad23f30ad4e8e25e2c26159f544ce815df6f4fe8
```

**结论（本次最有价值的读数）**：免费档模型**可用**——两席各一次真调用走通了
"冻结快照 → 证明网关（发四头）→ 消费侧解析 → call+vote 同事务落库"全链路，
零 `ReviewResponseError`、零 `ReviewOutcomeUnknown`。

同一份冻结输入（`input_sha256` 逐字节相同）两席判出**不同的票面**，且各自的理由
都点到了判据上真实存在的缺口——这就是"两席独立"的实证，不是复读：

- `deepseek-v4.1-flash` ⇒ **ABSTAIN**："`effect_ref` 为 null，且唯一条件
  SC-K2FIXTURE 的 `evidence_refs` 为空、`predicate_state` 为 unknown …
  SI-K2FIXM 虽在 instances 中出现，但被 stripped 标记为 `mirror_dedup`，且其
  `work_id WK-K2FIXM` 不在 `admitted_refs` 与 `scope_ids` 内，不能作为独立证据。
  因此证据不足以作出 PASS 或 BLOCK 判定。"
- `kimi-k3` ⇒ **BLOCK**（`concerns` 5 条）："条件 SC-K2FIXTURE 无 evidence_refs 且
  predicate_state unknown / 全部证据实质只有 SI-K2FIXA 一条 / 片段仅 5 字 /
  来源 `metadata_basis` 自承 fixture / `scope_claim.scope_ids` 只覆盖 WK-K2FIXA"。

`ABSTAIN` 与 `BLOCK` 都在契约内合法（`SYSTEM_PROMPT` 明写"证据不足时 ABSTAIN"），
且**正是判据期望的行为**：本 fixture 只有 1 条可用实例，两个模型都没有硬凑 PASS。

**这条读数对正式真跑的直接含义**：免费档**能出票**，但在真 K2 证据上大概率
**不是两票 PASS**（本 fixture 上两席都拒绝背书）。`status=verified` 需要两票 PASS，
所以"把驱动器跑通"与"K2 能进 verified"是两件事，后者仍未被证明（见第 6 节）。

补充读数（上一轮 06:20Z 单席冒烟，同样成立）：`deepseek-v4.1-flash` 1 快照 1 席
⇒ calls=1 / votes=1 / approval_links=0，票面 ABSTAIN，把该收据喂回准入侧原判据
`_check_vote` 逐条对账通过，`_current_two_pass_approval` 报
`two_pass_votes_missing`（单席的必然读数，不是收据缺陷）。

### 5.3 上游选型探针（同一 fixture、1 快照 1 席，逐个模型单跑）

真跑要 32 次派发（16 快照 × 2 席），所以**先把候选模型逐个单跑一遍**再开窗。实测
（都是真模型、都是 fixture 库）：

| 候选（LiteLLM 免费档） | 派发结果 | 票面 | 耗时（reserved→终态） |
|---|---|---|---|
| `deepseek-v4.1-flash` | `status=ok`，call+vote 落库 | `ABSTAIN` | **3.3s**（另测 4.2s） |
| `kimi-k3` | `status=ok`，call+vote 落库 | `BLOCK` | **40.7s**（另测 17.0s） |
| `glm-5.3` | `ReviewOutcomeUnknown: k2_gateway_outcome_unknown` | **无收据** | **120.2s**（超时） |

`glm-5.3` 那一格的原文（本机 stderr 尾部）与后果：

```
httpx.ReadTimeout: timed out          # tools/attestation_gateway.py:201 make_httpx_forwarder
--- results[1] ---
{"snapshot_id": "SS-e4fe...", "seat": "seat-2", "model_identity": "litellm/glm-5.3",
 "status": "failed", "call_id": null, "vote_id": null, "vote": null,
 "error_type": "ReviewOutcomeUnknown", "error_message": "k2_gateway_outcome_unknown"}
ok = 1, failed = 1
readout.counts = {"semantic_review_snapshots": 1, "semantic_review_calls": 1,
                  "semantic_review_votes": 1, "semantic_approval_links": 0}
--- 派发事件留痕 ---
reserved      2026-09-30T06:26:48.836596Z  {"channel_id": "lg-k2-seat2"}
outcome_unknown 2026-09-30T06:28:49.071327Z {"reason": "ReviewOutcomeUnknown"}
--- seat2 网关审计 ---
exists: False | bytes: 0               # 转发器抛异常 ⇒ 网关来不及记账，也没有证明头
```

**`kimi-k3` 的耗时波动必须写进选型结论**：同一台机器、同一个 fixture，
17.0s → 40.7s 是 **2.4× 抖动**（本机同时跑着几十个 python 进程）。距离网关转发器
120s 硬上限的余量因此只剩 ~3×，而真 K2 快照的输入比本 fixture 大得多、输出更长。
选型排序不变，但**开窗前必须对真快照实测单次耗时**，且要为 `kimi-k3` 这一席
预留最慢的一档预算。

三条必须记住的机制性结论：

1. **网关转发器的 httpx 超时是 120s 硬编码**
   （`tools/attestation_gateway.py:make_httpx_forwarder(route, timeout=120.0)`，
   本工单**不许改**该文件）。所以消费侧 `--timeout-seconds` 调再大也没用：上游慢于
   120s 的模型在这套网关上**不可用**（实测 glm-5.3 就是这样）。
2. **超时按"结果未知"记账，不按失败记账**：`ReviewOutcomeUnknown` ⇒ 零收据、
   零重试、审计无行、只留 `outcome_unknown` 事件。这是对的——请求可能已在上游执行，
   重投会破坏"每模型恰好一次"。但代价是**该席该轮的机会就这一次**。
3. **两席确实独立**：`deepseek-v4.1-flash` 判 `ABSTAIN`、`kimi-k3` 判 `BLOCK`，
   同一份冻结输入、两个不同 `model_id`、两份互相独立的理由
   （kimi-k3 的 `concerns` 逐条点名 `SC-K2FIXTURE.evidence_refs` 为空、
   `metadata_basis='fixture: 合成登记行'`、`SI-K2FIXM` 被 `mirror_dedup` 剥离后只剩
   单条证据）。**判据没有在盖章**，这正是两席机制要的东西。

## 6. 真库真跑前还差什么

按依赖顺序，四件事（**本工单一件都没做，也一件都不许由本工单做**）：

1. **真库 ACL 前置**。`review_snapshot` 先过
   `_require_private_storage(database)`：目标库文件、其所在目录、存在的
   `-wal`/`-shm`/`semantic_review_attempts` 侧件必须只允许
   {当前用户, SYSTEM, Administrators} 写，且卷根以下每一级祖先不得带
   DELETE_CHILD/DELETE/WRITE_DAC/WRITE_OWNER/GENERIC_ALL。实测
   `%LOCALAPPDATA%\lg-k2-receipt-mint` 可过、`...\AppData\Local\Temp` 不可过；
   `D:\language-genome-data` 需另行实测（不在本工单授权范围内）。不过闸 ⇒
   `k2_storage_acl_untrusted`，**一次都不派发**。
2. **停写窗口**。真库 45.5 GB、WAL 模式、14:0x 仍在被写（`-wal` 29MB 在长）。
   派发前要停 uvicorn/导入器等全部写者：`_load_round` 与提交段都在
   `BEGIN IMMEDIATE` 下重读快照，证据一变就是 `k2_round_changed_during_call`（票作废、
   且该席该模型**不可再来一次**）。窗口长度 = 16 快照 × 2 席 = 32 次派发；
   按 5.3 实测的单次耗时（4.2s / 17.0s）粗估是**分钟级**，但真 K2 快照的输入大得多
   （真 `review_input_json` 远大于本 fixture 的 ~5KB），且网关转发器有 120s 硬上限，
   真跑前必须先按真快照实测单次耗时再定窗口长度。
3. **上游选型结论**。已定（5.3 实测）：
   - seat-1 = `deepseek-v4.1-flash`（LiteLLM `127.0.0.1:4000`）**技术可用**，4.2s；
   - seat-2 = `kimi-k3` **技术可用**，17.0s；两者 `model_id` 互异、票面互相独立；
   - **出局**：`glm-5.3`（>120s，网关转发器硬超时 ⇒ 恒 `ReviewOutcomeUnknown`）；
     同理任何推理链 >120s 的模型在这套网关上都要先出局。
   - **仍未定**：真 K2 快照有 16 条实例、`review_input_json` 远大于本 fixture 的 ~5KB，
     上面两个耗时**不能外推**（更大的输入 + 更长的输出 ⇒ 更慢，逼近 120s 就有风险）；
     且真证据上两席都要 **PASS** 才进 `verified`，免费档会不会系统性
     ABSTAIN/BLOCK 只能真跑知道。开窗前先对**真快照**做只读的请求体字节数与
     `max_output_tokens` 预算预检（`review_snapshot` 的
     `k2_request_exceeds_budget` 与 120s 网关上限是两道独立的门）。
4. **两席各一次、不可重来的人工编排**。同一 `(snapshot, model)` 第二次派发被消费侧
   锁死（`k2_model_already_voted`），且派发栅栏 `semantic_review_attempts/seat-<sha>.json`
   跨进程防重；失败只允许**如实记账 + 另起新轮次（freeze 新 snapshot）**，不允许重投同一轮。

5. **负载前提（本轮新加的读数，真跑前必须落实）**。消费侧每派发一次的
   `_require_private_storage` 是一个 `subprocess.run(timeout=15)` 的
   `powershell.exe`——**15s 打不住就是 `k2_storage_acl_unverifiable`，该席该轮直接作废**
   （栅栏已落、不可重投）。本轮实测这台机器同时跑着几十个 python 进程时，同一个
   fixture 建库从 1.5s 涨到 25s、`kimi-k3` 单次派发从 17.0s 涨到 40.7s。
   所以 32 次派发的真跑窗口**必须避开并发负载**（其它会话/服务/索引/杀软全盘扫描），
   否则损失的不是"慢一点"，而是**某一席永久少一票**。

### 附：真跑命令形状（本工单**未执行**）

```powershell
python scripts/k2_receipt_mint.py --database D:\language-genome-data\language_genome.db `
  --i-know-this-is-live --seats 2 --timeout-seconds 300
```

（双开关缺一即 `MINT_REFUSED: live_write_requires_explicit_ack`、rc=2、不落任何文件。）
