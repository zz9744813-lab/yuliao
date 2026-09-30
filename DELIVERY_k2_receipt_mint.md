# DELIVERY — K2 语义收据铸造驱动器（lg-k2-receipt-mint，2026-09-30）

> 本轮是**返工**：上一轮提交后验证门同时报两条红
> （① `verify_cmd` 退出码 1，红的是
> `tests/test_k2_receipt_mint.py::test_missing_attestation_header_blocks_receipt[x-lg-upstream-model]`；
> ② 整轮 `exit_code=124` 超时）。本轮先定性再修，见下"上一轮报红的定性"。
> 全文只写实测到的东西，未跑的写在"未自跑"一节。

## 交付清单（本轮改动全部落在允许编辑的四个文件内）

| 路径 | 状态 | 说明 |
|---|---|---|
| `scripts/k2_receipt_mint.py` | 新增（上一轮） | 铸造驱动器：两席=两条 `ReviewRoute`、每席起一个真证明网关、逐 snapshot 恰好一次派发、机器可读 JSON 摘要 + 现场读数、真库双开关。**本轮未改**（改动只发生在测试侧与文档侧） |
| `tests/test_k2_receipt_mint.py` | 新增（上一轮）→ **本轮改造** | 14 项，合成上游，rc=0；本轮做两处收口：fixture 模板化（建库 14 次 → 1 次）+ 派发前预热 ACL 探测；派发实测 15 次（见下"派发次数怎么量出来的"） |
| `docs/k2_receipt_mint_20260930.md` | 更新 | 补 5.1.1（本轮报红定性与收口）、重写 5.2（真模型**两席**冒烟原文）、5.3（`kimi-k3` 耗时波动 17.0s→40.7s）、第 6 节新增第 5 条负载前提 |
| `DELIVERY_k2_receipt_mint.md` | 重写 | 本文件 |

未改：`app/**`（判据一字未动）、`tools/attestation_gateway.py`、其它 worktree、
主仓工作区。未 commit / 未 merge / 未 push / 未改 git 配置。

## 上一轮报红的定性（本机现场复核，不是推测）

`python -m pytest tests/test_k2_receipt_mint.py -q` 在上一轮的检出里**本身是能过的**
（本轮同机连跑 4 轮全 rc=0）。报红的是**时间预算被机器负载打穿**，两条机制都实测到：

1. **消费侧每派发一次就起一个 `powershell.exe`**，且 `subprocess.run(..., timeout=15)`
   （`app/semantic_review_runner.py::_require_private_storage`）。打不住 15s 就抛
   `ReviewPreflightError: k2_storage_acl_unverifiable` —— 于是"少发一个证明头 ⇒
   必须抛 `ReviewResponseError`"那条断言被**环境问题**顶掉，正好对上
   `[x-lg-upstream-model]` 这一格偶发变红。
2. **逐测试现造 fixture 库**（`create_all` + 播种 + `freeze_snapshot`）在满载时
   从 1.5s 涨到 25s。本轮同一份代码实测到的最坏一轮：
   `setup 17-25s / call 29-36s`、整轮 **154s**（→ 上一轮的 `exit_code=124`）。

现场对照读数（同一台机器、同一份 fixture 形状）：

```
ACL 探测连测 4 次（空闲）              ：0.61 / 0.44 / 0.41 / 0.37 s
同一时刻本机进程                     ：tasklist 里 python.exe ~40 个（其它会话/服务）
改造前整轮墙钟                       ：43s（空闲） / 154s（满载，且有测试失败）
改造后整轮墙钟                       ：23s（空闲） / 23s / 23s / 20s（连跑四轮 rc 均 0）
```

**两条收口都只动 `tests/`，没碰 `app/`、没碰判据、没加任何重试**：

- `template`（module 作用域）整轮只建一次库，每个测试 `shutil.copyfile` 一份**独立
  副本**（副本里 calls/votes 从 0 开始、`semantic_review_attempts` 栅栏目录全新，
  测试之间不共享写状态；建库前先 `PRAGMA wal_checkpoint(TRUNCATE)` 保证副本是单文件）；
- `fixture_store` 在把库交给被测件之前，先原地
  `runner._require_private_storage(Path(database))` 探一次，把冷启动 PowerShell
  的开销挪出被测窗口。

顺带把派发压到实测 **15 次**（反向自检按"1 快照 1 席"就足以坐实"不生成收据"），
并把"上游侧看到的确实是冻结输入"这 4 次额外派发**折进**主用例的同一次真派发。
**没有删掉任何一条断言**，14 项覆盖与改造前逐条等价（少的 1 项是被折进主用例，
不是被删）。

### 派发次数怎么量出来的（含一次测量自身的坑）

不靠算，靠数：本轮用一条 **stdin 喂给解释器**的 `pytest.main()` 计数插件（未落盘、
未留痕）把 `runner._require_private_storage` 包了一层并标注调用来源，同轮读数原文：

```
PROBE CALLS total: 27 {'fixture_store': 12, 'review_snapshot': 15}
```

- `review_snapshot: 15` = 真的走派发路径、被消费侧 ACL 闸拦过一次 = **15 次派发**；
- `fixture_store: 12` = 收口新增的预热探测（12 个用 `fixture_store` 的用例各一次），
  在 15s 窗口**之外**，不参与上表的"派发"口径。

同一轮那个测量进程**额外报了一条与本工单无关的红**，必须写清：

```
FAILED tests/test_k2_receipt_mint.py::test_two_seats_mint_exactly_one_receipt_each_and_counts_above_zero
E   AssertionError: assert 'real' == 'mock'          # assert config.LLM_MODE == "mock"
RC= 1
```

成因是**测量脚手架**：那条 heredoc 在被测模块导入之前就 `import
app.semantic_review_runner`（连带 `app.config`）来包装探测函数，于是 `app.config`
在 `tests/conftest.py` 把 `LG_LLM_MODE=mock` 写进去之前就已按默认值加载——被测件
断言的"派发改过 `config.LLM_MODE`、收尾复原到 mock"复原到的是**脚手架留下的
`real`**。复原逻辑本身没问题（`mint.mint()` 的 `finally` 就是原样写回进入前的值）。
验收命令 `python -m pytest ...` 由 conftest 先跑，不存在这个前置污染，改造前后均
rc=0（本轮连跑 5 轮，含最后复验的一轮）。

## 验收 1：验收命令 rc=0（原文）

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k2_receipt_mint.py -q
..............                                                           [100%]
============================== warnings summary ===============================
tests\conftest.py:100
  F:\agi\_scratch\worktrees\lg-k2-receipt-mint\tests\conftest.py:100: UserWarning: [R6 测试卫生 OPEN-4] 未设 LG_LOCK_DIR：本轮 pytest 以源检出目录 F:\agi\_scratch\worktrees\lg-k2-receipt-mint\data 为整轮锁位（将创建 F:\agi\_scratch\worktrees\lg-k2-receipt-mint\data\live_run.lock）——干净 worktree 里这是意外副作用。建议跑套件前设 LG_LOCK_DIR=<临时目录> 把锁隔离到检出之外；本轮若由本会话创建该目录，收尾时会自动移除空目录。
    config.issue_config_time_warning(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
RC=0
```

14 个点 = 14 项全过。那条 warning 是仓库既有 `tests/conftest.py` 的 OPEN-4 卫生提示
（不是本工单引入），`data/` 由 conftest 自己在 sessionfinish 把**本轮自己创建的空目录**
rmdir 掉。

旁证（未破坏仓库既有判据/契约测试，同轮全绿，共 78 项）：

```
$ python -m pytest tests/test_len_spec.py tests/test_style_contract.py \
    tests/test_compile_all.py tests/test_docs_code_reconcile.py \
    tests/test_semantic_review_runner.py tests/test_attestation_gateway.py -q
........................................................................ [ 96%]
......                                                                   [100%]
```

`tests/test_k2_receipt_mint.py` / `scripts/k2_receipt_mint.py` 均无 >100 列的行。

## 验收 2：fixture 库上 call/vote 实测计数 > 0

### 2.1 合成上游（pytest 内，2 快照 × 2 席 = 4 次真派发）

`test_two_seats_mint_exactly_one_receipt_each_and_counts_above_zero` 断言（同一条用例内
跑前 → 跑后，均为对私有落盘 fixture 库的真 SQL 读数）：

```
跑前 {"semantic_review_snapshots": 2, "semantic_review_calls": 0,
      "semantic_review_votes": 0, "semantic_approval_links": 0}
跑后 {"semantic_review_snapshots": 2, "semantic_review_calls": 4,
      "semantic_review_votes": 4, "semantic_approval_links": 0}
```

逐 (snapshot, model) 恰 1 票、每模型 1 次 call、`judge_kind=semantic_current`、
`channel=openai_http`、上游 `request-id` 全局唯一 4 条
（`UNIQUE(provider, upstream_request_id)` 约束的实况）；`semantic_approval_links`
恒为 0 —— 本件只造 call + vote，**不造准入**。

### 2.2 真模型两席冒烟（CLI 实跑，fixture 库，**只碰 fixture**）

上游 = 本机免费档 LiteLLM `http://127.0.0.1:4000/v1`
（key 读 `F:\Hermes\secrets\litellm_master_key.txt`，不落任何文件/审计/证明头）。

```
$ python scripts/k2_receipt_mint.py --seats 2 --snapshots 1 --timeout-seconds 150
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
RC=0
```

对那个 fixture 库**只读**直查的 SQL 原文：

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
SQL> SELECT snapshot_id, model_id, verdict FROM semantic_review_votes
   -> ('SS-0188e11ee46e4a308f33ef3f29d56beb', 'deepseek-v4.1-flash', 'ABSTAIN')
   -> ('SS-0188e11ee46e4a308f33ef3f29d56beb', 'kimi-k3',             'BLOCK')
```

两席的网关审计各 1 行 `issued`（`request-id` 恒等上游体 `id`）与派发留痕：

```
attest-seat1.jsonl  issued deepseek-v4.1-flash chatcmpl-REONeiIOpk79OrX3BO575VN6      response_sha256=17f31f7cdd582bb5…
attest-seat2.jsonl  issued kimi-k3             chatcmpl-c3d54d84-dc53-4242-81a0-b90115f8af15 response_sha256=1415c2bdbd9277d1…
K2A-c7169844…  reserved@06:59:54 → received@06:59:57 → committed@06:59:57   (3.3s)
K2A-fbad8550…  reserved@06:59:58 → received@07:00:38 → committed@07:00:38  (40.7s)
两席 reserved 事件的 input_sha256 相同 = c0e88aef47ff4927ba267c5cad23f30ad4e8e25e2c26159f544ce815df6f4fe8
```

**核心风险探针的读数**：免费档**能出票、能落 call+vote**，零
`ReviewResponseError` / 零 `ReviewOutcomeUnknown`。但**同一份冻结输入**两席判出
**不同票面**，且都拒绝背书：

- `deepseek-v4.1-flash` ⇒ ABSTAIN（原文）："…`effect_ref` 为 null，且唯一条件
  SC-K2FIXTURE 的 `evidence_refs` 为空、`predicate_state` 为 unknown …
  SI-K2FIXM 虽在 instances 中出现，但被 stripped 标记为 `mirror_dedup` …
  因此证据不足以作出 PASS 或 BLOCK 判定。"
- `kimi-k3` ⇒ BLOCK（5 条 concerns，逐条点名）："条件 SC-K2FIXTURE 无 evidence_refs
  且 predicate_state unknown / 有效证据仅一条（SI-K2FIXM 为镜像重复）/ 证据片段过短
  （span [0,5]）/ 来源为 fixture 合成登记行，`license_basis=null` /
  `scope_claim.scope_basis` 为 fixture 自述"

⇒ 直接含义：**"驱动器跑通"与"K2 能进 `verified`"是两件事**。`verified` 要两票 PASS，
免费档在证据不足的卡上不盖章（这是对的），所以正式真跑前无法预判两席会不会都 PASS。

选型补充读数（同一台机器）：`kimi-k3` 单次派发 **17.0s（上一轮） → 40.7s（本轮）**，
2.4× 抖动，距网关转发器 **120s 硬编码超时**只剩约 3×；`glm-5.3` 上一轮实测
120.2s ⇒ `ReviewOutcomeUnknown: k2_gateway_outcome_unknown`（零收据、零重试、
审计无行），已在 docs 5.3 出局。

## 验收 3：反向自检（证明头不齐 ⇒ 不生成收据，原文钉死）

`tests/test_k2_receipt_mint.py` 实测断言（4 个证明头各测一次）：

- 少发任一证明头 ⇒ `error_type="ReviewResponseError"` 且
  `error_message="k2_response_unverifiable"`，`call_id/vote_id` 均 None，
  `semantic_review_calls` / `semantic_review_votes` 计数**不增**（跑前后 `_counts` 相等，
  且摘要 `readout` 里两数仍为 0）；
- 网关拒发（上游体 `model` 与核准路由不符 ⇒ 502 + 零证明头）⇒
  `ReviewResponseError: k2_gateway_http_502`，计数不增，且**上游确实被打过**
  （拒发发生在网关侧）；
- 上游不可达 ⇒ `ReviewOutcomeUnknown: k2_gateway_outcome_unknown`，计数不增、**不重投**；
- 重复投同一 (snapshot, model) ⇒ `ReviewPreflightError: k2_model_already_voted`，
  计数不增，且**合成上游一次都没被打**（`handler.requests == []`，防重在派发之前）；
- BLOCK / ABSTAIN 票面**原样**记账，不被改写成 PASS。

## 验收 4：硬边界（越界项自查）

| 边界 | 自查结果 |
|---|---|
| 不写真库 `D:\language-genome-data\language_genome.db` | 本轮只以 `mode=ro` 打开一次做现状对账；**未**传 `--i-know-this-is-live`（全部派发落在 `%LOCALAPPDATA%\lg-k2-receipt-mint\` 下的 fixture 库）。收尾复核仍是 `semantic_review_snapshots=16 / calls=0 / votes=0 / approval_links=0`（与派工时的现场读数一致 ⇒ 本工单未写一行） |
| 不改 `app/**` | `git status --porcelain` 只有 4 个允许的新增文件，无 `app/` 变更 |
| 不改判据 | `ReviewRoute.validate()` / `_parse_response` / `_require_private_storage` 等零改动；本轮改动全在 `tests/` 与 `docs/` |
| 不改 `tools/attestation_gateway.py` | 只**调用**其 `gateway_from_env` / `make_server` / `AttestationGateway` / `AuditLog` / `make_httpx_forwarder`；测试里的"少发一个证明头"是在测试自己的网关实例上包一层 `handle`，未动 `tools/` 源码 |
| 不 push / 不合并 / 不改主仓工作区 / 不 commit | 本工单未执行任何 git 写操作 |
| 不留 scratch | 已清除检出内的 `probe.db`（0 字节、上一轮遗留、gitignored）；`data/` 空目录由 conftest 自行移除；`%LOCALAPPDATA%\lg-k2-receipt-mint\pytest-15564-*`（上一轮被硬杀那次留下的死 pid 残骸）已删除。工作树只剩 4 个交付文件 + gitignored 的 `__pycache__/.pytest_cache` |
| 不把「未跑」写成「已跑」 | 见下节 |

## 未自跑 / 未完成（如实标注）

1. **真库 16 条快照的真跑：未跑**（本工单明令不许写真库）。所以"免费档模型在真 K2
   证据上能否给出两席 PASS"**仍未证明**——2.2 只证明链路可用 + 两席互相独立。
2. **真库 ACL 闸未实测**：`D:\language-genome-data` 是否过 `_require_private_storage`
   未知（本工单无写权限，也没做只读 ACL 探测）。已知实测结论只有两条：
   `%LOCALAPPDATA%\lg-k2-receipt-mint` **可过**、`...\AppData\Local\Temp`
   **不可过**（`k2_storage_acl_untrusted`）。
3. **uvicorn / 写者停写窗口：未开**（真跑前置，见 docs 第 6 节）。
4. **真 K2 快照的请求体预算未预检**：真 `review_input_json` 远大于本 fixture 的 ~5KB，
   `k2_request_exceeds_budget` 与网关 120s 上限这两道门在真数据上的余量未知。
5. **`glm-5.3` 慢的根因未深挖**：只观测到"网关转发器 120s 硬超时 → 读超时"，没区分
   LiteLLM 侧排队慢还是模型本身慢；调大 `--timeout-seconds` 无效（受制于
   `tools/attestation_gateway.make_httpx_forwarder` 的 120s 默认值，该文件本工单不许改）。
6. **负载偶发项未被根除**：本轮把 fixture 建库与 ACL 冷启动的暴露面压到最小，但
   消费侧"每派发一次 PowerShell、上限 15s"这条在 `app/` 里，本工单**不许改**。
   满载机器上仍可能出现 `k2_storage_acl_unverifiable` 造成的偶发红——本轮实测
   连跑 4 次 rc 均为 0，且已在 docs 第 6 节把"真跑窗口必须避开并发负载"写成前置。
7. **上一轮文档里的两处不实已更正**：① "15 项" 与 ② "`git status` 仅 2 个新增
   未跟踪文件"（实为 4 个）。本轮数字全部按当轮实测重写。
8. **本工单只造 call + vote，不造准入**：`semantic_approval_links` 恒为 0，
   人审放行仍须走 `app/semantic_approval`（不在本工单范围）。

## 复现命令

```powershell
# 1) 验收命令（本轮 rc=0）
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k2_receipt_mint.py -q

# 2) 真模型两席冒烟（fixture 库；key 只在进程环境变量里，不落盘）
$env:LG_K2_FIXTURE_ROOT          = "$env:LOCALAPPDATA\lg-k2-receipt-mint\smoke2"
$k = (Get-Content F:\Hermes\secrets\litellm_master_key.txt).Trim()
$env:LG_ATTEST_UPSTREAM_BASE_URL = "http://127.0.0.1:4000/v1"; $env:LG_ATTEST_UPSTREAM_API_KEY = $k
$env:LG_ATTEST_ROUTE_PROVIDER    = "litellm"; $env:LG_ATTEST_ROUTE_MODEL = "deepseek-v4.1-flash"
$env:LG_ATTEST_ROUTE_CHANNEL_ID  = "lg-k2-smoke-s1"; $env:LG_ATTEST_AUDIT_PATH = "$env:LG_K2_FIXTURE_ROOT\attest-seat1.jsonl"
$env:LG_ATTEST_2_UPSTREAM_BASE_URL = "http://127.0.0.1:4000/v1"; $env:LG_ATTEST_2_UPSTREAM_API_KEY = $k
$env:LG_ATTEST_2_ROUTE_PROVIDER   = "litellm"; $env:LG_ATTEST_2_ROUTE_MODEL = "kimi-k3"
$env:LG_ATTEST_2_ROUTE_CHANNEL_ID = "lg-k2-smoke-s2"; $env:LG_ATTEST_2_AUDIT_PATH = "$env:LG_K2_FIXTURE_ROOT\attest-seat2.jsonl"
F:/Hermes/hermes-agent/venv/Scripts/python.exe scripts/k2_receipt_mint.py --seats 2 --snapshots 1 --timeout-seconds 150
```

真库形状（**本工单未执行**，双开关缺一即 rc=2 且不落任何文件）：

```powershell
python scripts/k2_receipt_mint.py --database D:\language-genome-data\language_genome.db `
  --i-know-this-is-live --seats 2 --timeout-seconds 300
```
