# 交付报告 — K2 可信证明网关（lg-k2-attestation-gateway）

日期：2026-09-30　工作目录：`F:\agi\_scratch\worktrees\k2-attestation-gateway`（worktree，分支 `task/k2-attestation-gateway`）

## 1. 交付物（仅限授权清单内的文件）

| 路径 | 动作 | 说明 |
|------|------|------|
| `tools/attestation_gateway.py` | 新增 | 生成侧网关：`POST /chat/completions` → 原样转发到单条核准上游 → 按上游体 `id`/`model` 生成四证明头 → append-only JSONL 审计。仅 stdlib + 仓库既有 `httpx`。 |
| `tests/test_attestation_gateway.py` | 新增 | 合成上游、零真实模型/零真库；覆盖任务书 5 项 + 拒发透传。 |
| `docs/attestation_gateway_20260930.md` | 新增 | 信任模型 / TLS 落地与信任锚 / 边界。 |
| `DELIVERY_k2_attestation_gateway.md` | 新增 | 本文件。 |

未改 `app/semantic_review_runner.py`（判据一行未动）；未碰
`data/language_genome.db`；未 push / 合并 / 改主仓工作区。`tools/` 无
`__init__.py`——经核 `python -m pytest`（仓库根在 `sys.path`）以**命名空间包**导入
`tools.attestation_gateway`，未新增清单外文件。

## 2. 验收命令与真实输出（原文，未加工）

验收命令（任务书给定）：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_attestation_gateway.py -q
```

实际输出：

```
..........                                                               [100%]
============================== warnings summary ===============================
tests\conftest.py:100
  F:\agi\_scratch\worktrees\k2-attestation-gateway\tests\conftest.py:100: UserWarning: [R6 测试卫生 OPEN-4] 未设 LG_LOCK_DIR：本轮 pytest 以源检出目录 F:\agi\_scratch\worktrees\k2-attestation-gateway\data 为整轮锁位（将创建 ...\data\live_run.lock）——干净 worktree 里这是意外副作用。建议跑套件前设 LG_LOCK_DIR=<临时目录> 把锁隔离到检出之外；本轮若由本会话创建该目录，收尾时会自动移除空目录。
    config.issue_config_time_warning(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
RC=0
```

**rc = 0，10 条全过。** 逐条（`-v -o addopts=""`，同 venv）：

```
tests/test_attestation_gateway.py::test_normal_attestation_headers_match_upstream_body PASSED [ 10%]
tests/test_attestation_gateway.py::test_denial_paths_return_502_without_headers[missing_id-...-upstream_missing_id] PASSED [ 20%]
tests/test_attestation_gateway.py::test_denial_paths_return_502_without_headers[missing_model-...-upstream_missing_model] PASSED [ 30%]
tests/test_attestation_gateway.py::test_denial_paths_return_502_without_headers[model_mismatch-...-upstream_model_route_mismatch] PASSED [ 40%]
tests/test_attestation_gateway.py::test_denial_paths_return_502_without_headers[upstream_5xx-503-upstream unavailable-upstream_status_503] PASSED [ 50%]
tests/test_attestation_gateway.py::test_denial_paths_return_502_without_headers[not_json-...-upstream_body_not_json] PASSED [ 60%]
tests/test_attestation_gateway.py::test_audit_is_append_only PASSED      [ 70%]
tests/test_attestation_gateway.py::test_end_to_end_runner_produces_qualifying_review PASSED [ 80%]
tests/test_attestation_gateway.py::test_reverse_selfcheck_missing_header_rejected_by_runner PASSED [ 90%]
tests/test_attestation_gateway.py::test_denial_upstream_end_to_end_runner_sees_502_no_headers PASSED [100%]
======================== 10 passed, 1 warning in 3.01s ========================
```

（唯一 warning 来自仓库既有 `conftest.py` 的 R6/OPEN-4 测试卫生提示：本轮锁位临时建于
`<worktree>/data` 并在 `sessionfinish` 自动移除，收尾后已确认 `data/` 不存在，非本任务
产物。）

## 3. 任务书「必做」逐条对照

| # | 必做 | 实现 | 验证 |
|---|------|------|------|
| 1a | `POST /chat/completions` 原样转发到单条核准上游，体原样回传 | `attest()` + `make_httpx_forwarder()`；发放时 `result.body == reply.body` | `test_normal_...`（断言体逐字节）、`test_end_to_end_...`（断言合成上游收到的转发体 == 原请求体） |
| 1b | 捕获体 `id`/`model` 并追加四头 | `attest()` 发放分支生成 `x-lg-upstream-*` | `test_normal_...` |
| 1c | 硬约束：request-id==体 id、model==体 model；缺 id/model/非200/model 与路由不符 ⇒ 无头 + 502 + 记拒发 | `_decide()` + 拒发分支 | `test_denial_paths_return_502_without_headers`（5 例）、`test_denial_upstream_end_to_end_runner_sees_502_no_headers` |
| 1d | append-only 审计（路由身份/上游 id·model/请求 SHA256/响应 SHA256/UTC/发放·拒发）；只追加不重写 | `AuditLog`（`open(..., "ab")` + `fsync`） | `test_normal_...`（字段）、`test_audit_is_append_only`（2 行 + 首行逐字节未变） |
| 2 | 测试：正常/三类拒发/append-only/端到端/反向自检 | `tests/test_attestation_gateway.py` | 全 rc=0（见 §2） |
| 3 | 文档：信任模型 / TLS 与信任锚 / 边界 | `docs/attestation_gateway_20260930.md` | — |

## 4. 反向自检（核心）——真实输出原文

### 4.1 `pytest -s -k reverse`（真实 stdout 行）

```
tests\test_attestation_gateway.py [REVERSE-CHECK] app.semantic_review_runner.ReviewResponseError: k2_response_unverifiable | __cause__: ValueError: upstream identity mismatch
.
================= 1 passed, 9 deselected, 1 warning in 1.52s ==================
```

（网关故意少发 `x-lg-upstream-channel-id`：响应仍 200，但
`"x-lg-upstream-channel-id" not in response.headers` 已在测试内断言。）

### 4.2 让 `_parse_response` 抛出未捕获异常的完整 traceback（同链路、同一入参形状，
经 stdin 一次性运行，未落任何脚本文件）

```
status 200 has-channel-id-header? False
Traceback (most recent call last):
  File "F:\agi\_scratch\worktrees\k2-attestation-gateway\app\semantic_review_runner.py", line 208, in _parse_response
    raise ValueError("upstream identity mismatch")
ValueError: upstream identity mismatch

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "<stdin>", line 27, in <module>
  File "F:\agi\_scratch\worktrees\k2-attestation-gateway\app\semantic_review_runner.py", line 236, in _parse_response
    raise ReviewResponseError("k2_response_unverifiable") from exc
app.semantic_review_runner.ReviewResponseError: k2_response_unverifiable
```

结论（有据）：**少发任一证明头 ⇒ 消费侧 `semantic_review_runner._parse_response`
必抛 `ReviewResponseError("k2_response_unverifiable")`**，其底层原因是
`ValueError("upstream identity mismatch")`（`semantic_review_runner.py:208`），抛出点
在 `semantic_review_runner.py:236`。即使正文写 PASS 也不生成语义票——即本任务补齐的
断点确被真正闭合。

## 5. 端到端（真实链路）

`test_end_to_end_runner_produces_qualifying_review`：起**合成上游 stdlib HTTP 服务** +
**本网关 stdlib HTTP 服务**（网关用生产同款 `make_httpx_forwarder` 经 httpx 转发到合成
上游），把 `config.GATEWAY_BASE_URL` 指向网关，直接调 `semantic_review_runner._post_once`
+ `_parse_response`：产出 `verdict="PASS"`、`headers["request-id"]=="up-1"`、
`headers["model"]=="actual-a"`，合成上游收到的转发体与请求体逐字节相等，审计恰 1 条
`issued`。全链无真实模型、无真库（不落 `semantic_review_calls/votes`）。

## 6. 未自跑 / 边界声明（如实标注，未跑过的一律不写成跑过）

- ✅ **已真跑**：`attest()`/`AuditLog`/`make_httpx_forwarder`/`make_server` 的发放、
  三类拒发、append-only、端到端、反向自检——见 §2/§3/§4/§5，均在指定 venv 实跑 rc=0。
- ✅ **已真跑**：`route_from_env` 的**缺环境变量 fail-closed** 分支（抛
  `RuntimeError: attestation_gateway_env_missing:...`）。
- ❌ **未自跑**：`main()` 常驻服务与 `route_from_env` 的**全环境变量成功路径**——启动
  常驻服务属"起 ongoing service"，越界；且需真实监听端口与真实上游。代码路径经
  `make_server`+`serve_forever` 在测试内以临时端口起停验证，但 `main()` 入口本身未执行。
- ❌ **未自跑**：**真实 TLS/https + 自签证书 + 自定义信任锚**。测试全用回环 http +
  直接调 `_post_once`（合成测试，非生产放宽）。生产 https 与「把内网 CA 装入 httpx 所用
  certifi 束、绝不 `verify=False`」的落地**仅在文档 §4 记述方法，未做端到端 TLS 实测**。
- ❌ **未自跑**：`semantic_review_runner.review_snapshot` 的**完整 DB 事务 / ACL 探针 /
  seat 预留**——任务书限定「合成正文、不落真库」，故只用 `_post_once` + `_parse_response`
  验证"头能被消费侧核验"，未接通写库。收据链真正落库与 `GATE_K2_evidence` 转绿**不在
  本次交付范围**。
- ❌ **不涉及**：真实模型、真实网关、真实库读写；`api_key` 从不进证明头/审计/回包。

## 7. 工作树清洁

收尾 `git status` 仅新增清单内文件（`tools/`、`tests/test_attestation_gateway.py`、
两份 `*.md`）；无 scratch/smoke/temp 脚本残留（一次性烟测均经 stdin 运行，未落盘；
`tee` 临时输出已删除；`data/` 由 conftest 收尾自动移除并已确认不存在）。
