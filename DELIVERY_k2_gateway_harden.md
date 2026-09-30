# 交付报告 — K2 证明网关加固（lg-k2-gateway-harden）

日期：2026-09-30　工作目录：`F:\agi\_scratch\worktrees\lg-k2-gateway-harden`（worktree，未 commit / 未 push）
基线：会审双席对 `b0a394b` 的判定（glm-5.3 PASS / qwen3.8-flash BLOCK），原文
`F:\Hermes\team\reviews\language-genome-b0a394b686.md`。

## 1. 交付物（仅限授权清单内的文件）

| 路径 | 动作 | 说明 |
|------|------|------|
| `tools/attestation_gateway.py` | 改（加固） | 证明头字符白名单、入站鉴权 fail-closed、审计单调 `seq`、全路径 try/except、`Content-Length` 硬门、`api_key` 不入 repr、`main()` 端口解析、client 复用、上限口径对齐、跨进程文件锁 |
| `tests/test_attestation_gateway.py` | 改（扩充） | 10 条既有全保留 + 97 条新增（49 个测试函数，参数化后共 **107** 条），合成上游、零真实模型、零真库 |
| `docs/attestation_gateway_20260930.md` | 改（更新） | 新增 §2 deny_reason 全表、§3.1 审计唯一键与判定方法、§4 入站鉴权、§9 两侧口径差异、**§10 会审逐条对账（14 组）**、§11 测试映射表 |
| `DELIVERY_k2_gateway_harden.md` | 新增 | 本文件 |

**未改**：`app/semantic_review_runner.py`（判据一行未动）、`app/` 下任何文件、
`DELIVERY_k2_attestation_gateway.md`（上一轮交付报告，不在本次授权清单内）、
`data/language_genome.db`（从未触碰）。未 push / 合并 / 改主仓工作区。
`tools/` 仍无 `__init__.py`——本仓 `pyproject.toml` 只有 `[project]` +
`[tool.pytest.ini_options]`，**无 `[build-system]`、无显式 packages 列表**，即"仓库根
在 `sys.path` + 命名空间包导入 + `python -m tools.attestation_gateway` 直跑"的用法，
不存在被打包排除的问题（会审 qwen-10 的补输入项已核实，见 §5）。

## 2. 验收命令与真实输出（原文，未加工）

验收命令（任务书给定）：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_attestation_gateway.py -q
```

实际输出（工作目录 `F:\agi\_scratch\worktrees\lg-k2-gateway-harden`）：

```
........................................................................ [ 67%]
...................................                                      [100%]
============================== warnings summary ===============================
tests\conftest.py:100
  F:\agi\_scratch\worktrees\lg-k2-gateway-harden\tests\conftest.py:100: UserWarning: [R6 测试卫生 OPEN-4] 未设 LG_LOCK_DIR：本轮 pytest 以源检出目录 F:\agi\_scratch\worktrees\lg-k2-gateway-harden\data 为整轮锁位（将创建 F:\agi\_scratch\worktrees\lg-k2-gateway-harden\data\live_run.lock）——干净 worktree 里这是意外副作用。建议跑套件前设 LG_LOCK_DIR=<临时目录> 把锁隔离到检出之外；本轮若由本会话创建该目录，收尾时会自动移除空目录。
    config.issue_config_time_warning(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=== RC=0 ===
```

**RC = 0，107 条全过。**（唯一 warning 来自仓库既有 `conftest.py` 的 R6/OPEN-4 测试
卫生提示；本轮按验收命令原样跑（未设 `LG_LOCK_DIR`），conftest 在 sessionfinish 自动
rmdir 它自建的空锁目录，跑完 `ls data` 确认目录不存在——见 §7。）

逐条 `-v` 清单（同一 venv，`-v -o addopts=""`；超长参数化 ID 以 `…` 缩写，函数名与
结论未缩写）：

```
============================= test session starts =============================
platform win32 -- Python 3.11.16, pytest-9.1.1, pluggy-1.6.0 -- F:\Hermes\hermes-agent\venv\Scripts\python.exe
rootdir: F:\agi\_scratch\worktrees\lg-k2-gateway-harden
configfile: pyproject.toml
plugins: anyio-4.12.1
collecting ... collected 107 items

tests/test_attestation_gateway.py::test_normal_attestation_headers_match_upstream_body PASSED [  0%]
tests/test_attestation_gateway.py::test_denial_paths_return_502_without_headers[missing_id-200-{"model": "actual-a", "choices": []}-upstream_missing_id] PASSED [  1%]
tests/test_attestation_gateway.py::test_denial_paths_return_502_without_headers[missing_model-200-{"id": "up-1", "choices": []}-upstream_missing_model] PASSED [  2%]
tests/test_attestation_gateway.py::test_denial_paths_return_502_without_headers[model_mismatch-200-{"id": "up-1", "model": "alias-b", "choices": [{…转义后的 review 正文…}]}-upstream_model_route_mismatch] PASSED [  3%]
tests/test_attestation_gateway.py::test_denial_paths_return_502_without_headers[upstream_5xx-503-upstream unavailable-upstream_status_503] PASSED [  4%]
tests/test_attestation_gateway.py::test_denial_paths_return_502_without_headers[not_json-200-plain text not json-upstream_body_not_json] PASSED [  5%]
tests/test_attestation_gateway.py::test_audit_is_append_only PASSED      [  6%]
tests/test_attestation_gateway.py::test_end_to_end_runner_produces_qualifying_review PASSED [  7%]
tests/test_attestation_gateway.py::test_reverse_selfcheck_missing_header_rejected_by_runner PASSED [  8%]
tests/test_attestation_gateway.py::test_denial_upstream_end_to_end_runner_sees_502_no_headers PASSED [  9%]
tests/test_attestation_gateway.py::test_proof_header_value_charset_whitelist_denies[id_crlf_injection-body_kwargs0-upstream_id_invalid_chars] PASSED [ 10%]
tests/test_attestation_gateway.py::test_proof_header_value_charset_whitelist_denies[id_trailing_newline-body_kwargs1-upstream_id_invalid_chars] PASSED [ 11%]
tests/test_attestation_gateway.py::test_proof_header_value_charset_whitelist_denies[id_with_space-body_kwargs2-upstream_id_invalid_chars] PASSED [ 12%]
tests/test_attestation_gateway.py::test_proof_header_value_charset_whitelist_denies[id_too_long-body_kwargs3-upstream_id_invalid_chars] PASSED [ 13%]
tests/test_attestation_gateway.py::test_proof_header_value_charset_whitelist_denies[id_tab-body_kwargs4-upstream_id_invalid_chars] PASSED [ 14%]
tests/test_attestation_gateway.py::test_proof_header_value_charset_whitelist_denies[id_non_ascii-body_kwargs5-upstream_id_invalid_chars] PASSED [ 14%]
tests/test_attestation_gateway.py::test_proof_header_value_charset_whitelist_denies[model_crlf_injection-body_kwargs6-upstream_model_invalid_chars] PASSED [ 15%]
tests/test_attestation_gateway.py::test_proof_header_value_charset_whitelist_denies[model_with_space-body_kwargs7-upstream_model_invalid_chars] PASSED [ 16%]
tests/test_attestation_gateway.py::test_proof_header_value_charset_whitelist_denies[model_too_long-body_kwargs8-upstream_model_invalid_chars] PASSED [ 17%]
tests/test_attestation_gateway.py::test_crlf_in_upstream_id_cannot_inject_response_headers PASSED [ 18%]
tests/test_attestation_gateway.py::test_unsafe_route_identity_denies_before_forwarding PASSED [ 19%]
tests/test_attestation_gateway.py::test_non_loopback_listen_without_token_refuses_to_start PASSED [ 20%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[None--True] PASSED [ 21%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[Bearer tok--True] PASSED [ 22%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[Bearer tok-tok-True] PASSED [ 23%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[bearer tok-tok-True] PASSED [ 24%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[BEARER tok-tok-True] PASSED [ 25%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[Bearer   tok  -tok-True] PASSED [ 26%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[Bearer tok2-tok-False] PASSED [ 27%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[Bearer  tok2-tok-False] PASSED [ 28%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[Basic tok-tok-False] PASSED [ 28%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[tok-tok-False] PASSED [ 29%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[-tok-False] PASSED [ 30%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[None-tok-False] PASSED [ 31%]
tests/test_attestation_gateway.py::test_inbound_authorization_matrix[Bearer -tok-False] PASSED [ 32%]
tests/test_attestation_gateway.py::test_inbound_token_rejects_missing_and_wrong_bearer PASSED [ 33%]
tests/test_attestation_gateway.py::test_forward_exception_returns_502_and_audits_denial[connect_timeout-exc0-ConnectTimeout] PASSED [ 34%]
tests/test_attestation_gateway.py::test_forward_exception_returns_502_and_audits_denial[connect_error-exc1-ConnectError] PASSED [ 35%]
tests/test_attestation_gateway.py::test_forward_exception_returns_502_and_audits_denial[read_timeout-exc2-ReadTimeout] PASSED [ 36%]
tests/test_attestation_gateway.py::test_forward_exception_returns_502_and_audits_denial[unexpected-exc3-RuntimeError] PASSED [ 37%]
tests/test_attestation_gateway.py::test_forward_exception_over_http_returns_502_not_dropped_connection PASSED [ 38%]
tests/test_attestation_gateway.py::test_chunked_transfer_encoding_returns_400_without_forwarding PASSED [ 39%]
tests/test_attestation_gateway.py::test_missing_content_length_returns_400_without_forwarding PASSED [ 40%]
tests/test_attestation_gateway.py::test_invalid_content_length_returns_400_without_forwarding[Content-Length: abc\r\n-content_length_invalid] PASSED [ 41%]
tests/test_attestation_gateway.py::test_invalid_content_length_returns_400_without_forwarding[Content-Length: -1\r\n-content_length_invalid] PASSED [ 42%]
tests/test_attestation_gateway.py::test_invalid_content_length_returns_400_without_forwarding[Content-Length: 5_0\r\n-content_length_invalid] PASSED [ 42%]
tests/test_attestation_gateway.py::test_invalid_content_length_returns_400_without_forwarding[Content-Length: 2, 2\r\n-content_length_invalid] PASSED [ 43%]
tests/test_attestation_gateway.py::test_invalid_content_length_returns_400_without_forwarding[Content-Length: 2\r\nContent-Length: 2\r\n-content_length_invalid] PASSED [ 44%]
tests/test_attestation_gateway.py::test_request_too_large_returns_413_without_forwarding PASSED [ 45%]
tests/test_attestation_gateway.py::test_truncated_request_body_returns_400_without_forwarding PASSED [ 46%]
tests/test_attestation_gateway.py::test_slow_client_read_timeout_returns_408_without_forwarding PASSED [ 47%]
tests/test_attestation_gateway.py::test_path_not_found_returns_404_and_audits PASSED [ 48%]
tests/test_attestation_gateway.py::test_issued_row_binds_seq_request_and_proof_headers PASSED [ 49%]
tests/test_attestation_gateway.py::test_audit_seq_is_monotonic_across_calls PASSED [ 50%]
tests/test_attestation_gateway.py::test_audit_seq_continues_after_restart_without_rollback PASSED [ 51%]
tests/test_attestation_gateway.py::test_audit_seq_never_reused_across_processes PASSED [ 52%]
tests/test_attestation_gateway.py::test_audit_seq_is_monotonic_under_concurrency PASSED [ 53%]
tests/test_attestation_gateway.py::test_audit_heals_torn_last_line_without_rewriting_history PASSED [ 54%]
tests/test_attestation_gateway.py::test_response_cap_matches_consumer_cap PASSED [ 55%]
tests/test_attestation_gateway.py::test_upstream_response_cap_bounds_buffer_and_marks_incomplete PASSED [ 56%]
tests/test_attestation_gateway.py::test_oversize_upstream_response_denied_end_to_end PASSED [ 57%]
tests/test_attestation_gateway.py::test_httpx_forwarder_reuses_one_client PASSED [ 57%]
tests/test_attestation_gateway.py::test_httpx_forwarder_sends_bearer_and_identity PASSED [ 58%]
tests/test_attestation_gateway.py::test_denial_upstream_body_not_object[[]] PASSED [ 59%]
tests/test_attestation_gateway.py::test_denial_upstream_body_not_object["s"] PASSED [ 60%]
tests/test_attestation_gateway.py::test_denial_upstream_body_not_object[null] PASSED [ 61%]
tests/test_attestation_gateway.py::test_denial_upstream_body_not_object[123] PASSED [ 62%]
tests/test_attestation_gateway.py::test_denial_upstream_body_not_object[true] PASSED [ 63%]
tests/test_attestation_gateway.py::test_denial_upstream_body_not_object[["a"]] PASSED [ 64%]
tests/test_attestation_gateway.py::test_denial_upstream_id_empty_or_non_string[-] PASSED [ 65%]
tests/test_attestation_gateway.py::test_denial_upstream_id_empty_or_non_string[   -   ] PASSED [ 66%]
tests/test_attestation_gateway.py::test_denial_upstream_id_empty_or_non_string[None-None] PASSED [ 67%]
tests/test_attestation_gateway.py::test_denial_upstream_id_empty_or_non_string[123-None] PASSED [ 68%]
tests/test_attestation_gateway.py::test_denial_upstream_id_empty_or_non_string[1.5-None] PASSED [ 69%]
tests/test_attestation_gateway.py::test_denial_upstream_id_empty_or_non_string[bad_id5-None] PASSED [ 70%]
tests/test_attestation_gateway.py::test_denial_upstream_id_empty_or_non_string[bad_id6-None] PASSED [ 71%]
tests/test_attestation_gateway.py::test_route_repr_hides_api_key PASSED  [ 71%]
tests/test_attestation_gateway.py::test_api_key_never_leaves_gateway PASSED [ 72%]
tests/test_attestation_gateway.py::test_route_from_env_success_path PASSED [ 73%]
tests/test_attestation_gateway.py::test_route_from_env_missing_key_is_fail_closed[LG_ATTEST_ROUTE_CHANNEL_ID] PASSED [ 74%]
tests/test_attestation_gateway.py::test_route_from_env_missing_key_is_fail_closed[LG_ATTEST_ROUTE_MODEL] PASSED [ 75%]
tests/test_attestation_gateway.py::test_route_from_env_missing_key_is_fail_closed[LG_ATTEST_ROUTE_PROVIDER] PASSED [ 76%]
tests/test_attestation_gateway.py::test_route_from_env_missing_key_is_fail_closed[LG_ATTEST_UPSTREAM_API_KEY] PASSED [ 77%]
tests/test_attestation_gateway.py::test_route_from_env_missing_key_is_fail_closed[LG_ATTEST_UPSTREAM_BASE_URL] PASSED [ 78%]
tests/test_attestation_gateway.py::test_route_from_env_blank_value_is_fail_closed PASSED [ 79%]
tests/test_attestation_gateway.py::test_route_from_env_keeps_api_key_verbatim PASSED [ 80%]
tests/test_attestation_gateway.py::test_route_from_env_rejects_untrusted_base_url[http://upstream.invalid/v1-base_url_http_non_loopback] PASSED [ 81%]
tests/test_attestation_gateway.py::test_route_from_env_rejects_untrusted_base_url[ftp://upstream.invalid/v1-base_url_scheme_untrusted] PASSED [ 82%]
tests/test_attestation_gateway.py::test_route_from_env_rejects_untrusted_base_url[https://user:pass@upstream.invalid/v1-base_url_userinfo_forbidden] PASSED [ 83%]
tests/test_attestation_gateway.py::test_route_from_env_rejects_untrusted_base_url[https://upstream.invalid/v1?a=1-base_url_query_fragment_forbidden] PASSED [ 84%]
tests/test_attestation_gateway.py::test_route_from_env_rejects_untrusted_base_url[https:///v1-base_url_host_missing] PASSED [ 85%]
tests/test_attestation_gateway.py::test_route_from_env_accepts_https_and_loopback_http PASSED [ 85%]
tests/test_attestation_gateway.py::test_route_from_env_rejects_unsafe_route_identity PASSED [ 86%]
tests/test_attestation_gateway.py::test_gateway_from_env_success_and_missing_audit PASSED [ 87%]
tests/test_attestation_gateway.py::test_listen_from_env_success_path[env0-expected0] PASSED [ 88%]
tests/test_attestation_gateway.py::test_listen_from_env_success_path[env1-expected1] PASSED [ 89%]
tests/test_attestation_gateway.py::test_listen_from_env_success_path[env2-expected2] PASSED [ 90%]
tests/test_attestation_gateway.py::test_listen_from_env_rejects_bad_port[env0-listen_port_invalid] PASSED [ 91%]
tests/test_attestation_gateway.py::test_listen_from_env_rejects_bad_port[env1-listen_port_invalid] PASSED [ 92%]
tests/test_attestation_gateway.py::test_listen_from_env_rejects_bad_port[env2-listen_port_out_of_range] PASSED [ 93%]
tests/test_attestation_gateway.py::test_listen_from_env_rejects_bad_port[env3-listen_port_out_of_range] PASSED [ 94%]
tests/test_attestation_gateway.py::test_main_startup_failures_return_nonzero_without_traceback[overrides0-attestation_gateway_env_missing] PASSED [ 95%]
tests/test_attestation_gateway.py::test_main_startup_failures_return_nonzero_without_traceback[overrides1-attestation_gateway_listen_port_invalid] PASSED [ 96%]
tests/test_attestation_gateway.py::test_main_startup_failures_return_nonzero_without_traceback[overrides2-attestation_gateway_listen_port_out_of_range] PASSED [ 97%]
tests/test_attestation_gateway.py::test_main_startup_failures_return_nonzero_without_traceback[overrides3-attestation_gateway_base_url_http_non_loopback] PASSED [ 98%]
tests/test_attestation_gateway.py::test_main_refuses_non_loopback_listen_without_token PASSED [ 99%]
tests/test_attestation_gateway.py::test_main_success_path_serves_and_closes PASSED [100%]

============================ 107 passed in 24.62s ============================
```

## 3. 关键证据（一次性 stdin 脚本，真跑原文）

### 3.1 入站鉴权 + 响应头注入防线 + 审计账本（真实 HTTP 往返，非单测桩）

合成上游故意把 `id` 写成 `"up-1\r\nSet-Cookie: pwn=1\r\nX-Injected: yes"`，网关配了
入站令牌 `tok-3`，三次真实 POST 的**响应原文**（`F:/Hermes/hermes-agent/venv/Scripts/
python.exe - <<'PY' … PY`，脚本经 stdin 运行，未落任何文件）：

```
=== [1] 无 Authorization ===
HTTP/1.1 401 Unauthorized
Server: LG-Attestation-Gateway/1.1 Python/3.11.16
Date: Wed, 30 Sep 2026 06:59:57 GMT
Content-Type: application/json
Content-Length: 69
Connection: close
WWW-Authenticate: Bearer realm="lg-attestation-gateway"

{"error": {"reason": "inbound_unauthorized", "type": "unauthorized"}}
=== [2] 错令牌 ===
HTTP/1.1 401 Unauthorized
Server: LG-Attestation-Gateway/1.1 Python/3.11.16
Date: Wed, 30 Sep 2026 06:59:57 GMT
Content-Type: application/json
Content-Length: 69
Connection: close
WWW-Authenticate: Bearer realm="lg-attestation-gateway"

{"error": {"reason": "inbound_unauthorized", "type": "unauthorized"}}
=== [3] 正确令牌（上游 id 含 CRLF）===
HTTP/1.1 502 Bad Gateway
Server: LG-Attestation-Gateway/1.1 Python/3.11.16
Date: Wed, 30 Sep 2026 06:59:57 GMT
Content-Type: application/json
Content-Length: 83
Connection: close

{"error": {"reason": "upstream_id_invalid_chars", "type": "k2_attestation_denied"}}
=== [4] 审计账本 ===
{"at": "2026-09-30T06:59:57.744171Z", "decision": "denied", "deny_reason": "inbound_unauthorized", "request_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "response_sha256": null, "route": {"channel_id": "channel-7", "model": "actual-a", "provider": "provider-a"}, "seq": 1, "upstream_id": null, "upstream_model": null}
{"at": "2026-09-30T06:59:57.779176Z", "decision": "denied", "deny_reason": "inbound_unauthorized", "request_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "response_sha256": null, "route": {"channel_id": "channel-7", "model": "actual-a", "provider": "provider-a"}, "seq": 2, "upstream_id": null, "upstream_model": null}
{"at": "2026-09-30T06:59:57.821266Z", "decision": "denied", "deny_reason": "upstream_id_invalid_chars", "request_sha256": "237c6f03b16e97f1180946c0bfe0e2e30c5d7240e20e51320ad779a137faae9e", "response_sha256": "5a7b0d8d6dd294715fdec6ac1cbcbe67176ec4314e1e3cf02a13d61c0a359769", "route": {"channel_id": "channel-7", "model": "actual-a", "provider": "provider-a"}, "seq": 3, "upstream_id": "up-1\r\nSet-Cookie: pwn=1\r\nX-Injected: yes", "upstream_model": "actual-a"}

=== [5] 账本原始字节里有裸 CR/LF 吗 === False
=== [6] 账本/回包里有 sk-REAL-KEY 吗 === False
```

可读结论：三次入站 POST ⇒ **恰好三行审计、`seq` 1/2/3 不重号**；无凭据/错凭据一律 401
且零证明头；脏 `id` 的真实往返被 502 + 零证明头挡下，**响应头里没有 `Set-Cookie`、
没有 `X-Injected`**；脏值以 JSON 转义形式留证（账本字节里无裸 CR/LF，行结构未被污染）；
上游密钥既不在账本也不在回包。

### 3.2 启动面 fail-closed（同一 stdin 方式，只跑失败路径，不起服务）

```
attestation_gateway_startup_failed:inbound_token_required_for_non_loopback_listen:host=0.0.0.0,set=LG_ATTEST_INBOUND_TOKEN
attestation_gateway_startup_failed:attestation_gateway_listen_port_invalid:eighty
attestation_gateway_startup_failed:attestation_gateway_listen_port_out_of_range:70000
attestation_gateway_startup_failed:attestation_gateway_env_missing:LG_ATTEST_ROUTE_MODEL
attestation_gateway_startup_failed:attestation_gateway_base_url_http_non_loopback:upstream.invalid
attestation_gateway_startup_failed:attestation_gateway_base_url_userinfo_forbidden
attestation_gateway_startup_failed:attestation_gateway_route_header_unsafe:provider
非回环监听且未配入站令牌                       -> rc=2
端口不是整数                                 -> rc=2
端口越界                                 -> rc=2
缺路由身份环境变量                                 -> rc=2
上游 base_url 非回环 http                       -> rc=2
上游 base_url 带 userinfo                       -> rc=2
路由身份含 CR/LF                       -> rc=2
api_key 是否被静默 strip: '  sk-padded  '
回环判定 0.0.0.0/10.1.2.3/localhost/::1/::/example.invalid: [False, False, True, True, False, False]
```

（stderr 先于 stdout 打印是因为两条流分别缓冲；`rc=2` 全部非零且**无 traceback**。）

### 3.3 反向自检（核心）——真实 stdout 原文

`pytest -s -k reverse -o addopts=""`：

```
collected 106 items / 105 deselected / 1 selected

tests\test_attestation_gateway.py [REVERSE-CHECK] app.semantic_review_runner.ReviewResponseError: k2_response_unverifiable | __cause__: ValueError: upstream identity mismatch
.

====================== 1 passed, 105 deselected in 2.04s =======================
```

（该次运行在新增 408 用例之前，故此处是 `106 items / 105 deselected`；当前套件为
107 条，见 §2 的最终一轮。）结论不变：**网关少发任一证明头 ⇒ 消费侧
`_parse_response` 必抛 `ReviewResponseError("k2_response_unverifiable")`**，底层
`ValueError("upstream identity mismatch")`。本版已按双席建议把直写 `sys.__stdout__`
改成 `print()`，输出走 pytest 捕获机制。

### 3.4 跨进程审计序号（真实子进程，测试内即含）

`test_audit_seq_never_reused_across_processes` 用 `sys.executable -c …` 起**两个独立
解释器**各写 3 行，父进程再写 1 行，断言 `seq` 严格为 `[1,2,3,4,5,6,7]`——这既是
"跨进程不重号"也是"重启后不回退"的实测（每轮 -v 清单里该条 PASSED）。

## 4. 任务书「必做」逐条对照

| # | 任务书必做 | 实现 | 验证（用例名） |
|---|-----------|------|--------------|
| 1a | 证明头值白名单校验，脏值 ⇒ 拒发（无头/502/审计新 deny_reason） | `HEADER_VALUE_RE` + `_decide` 新增 3 个 charset 分支 + `Route.unsafe_header_fields()` | `test_proof_header_value_charset_whitelist_denies`(8)、`test_crlf_in_upstream_id_cannot_inject_response_headers`、`test_unsafe_route_identity_denies_before_forwarding` |
| 1b | 入站鉴权 fail-closed：非回环无令牌拒启动；令牌不符 401 且不发头 | `check_inbound_binding`（`make_server` 绑定前调用）、`check_inbound_authorization` | `test_non_loopback_listen_without_token_refuses_to_start`、`test_inbound_authorization_matrix`(13)、`test_inbound_token_rejects_missing_and_wrong_bearer`、`test_main_refuses_non_loopback_listen_without_token` |
| 1c | 审计单调序号（进程内+跨进程+重启不回退）+ 请求侧标识与证明绑定 + 文档写判定方法 | `AuditLog._reserve_seq`（`threading.Lock` + 路径共享锁 + OS 文件锁 + 账本尾接号）、`seq`/`request_sha256` 入行、docs §3.1 | `test_issued_row_binds_seq_request_and_proof_headers`、`test_audit_seq_is_monotonic_across_calls`、`test_audit_seq_continues_after_restart_without_rollback`、`test_audit_seq_never_reused_across_processes`、`test_audit_seq_is_monotonic_under_concurrency` |
| 1d | `do_POST` 全路径 try/except：转发器异常 ⇒ 502 + 审计拒发行 | `attest()` 兜 `except Exception`；`do_POST` 外层再兜（异常 ⇒ 500 + stderr 单行） | `test_forward_exception_returns_502_and_audits_denial`(4)、`test_forward_exception_over_http_returns_502_not_dropped_connection` |
| 1e | 缺/非法 `Content-Length` ⇒ 400，不转发 | `_read_request_body`：TE 优先拒、缺 CL、非法/多个 CL、读超时 408、实读短于声明 400 | `test_chunked_...`、`test_missing_content_length_...`、`test_invalid_content_length_...`(5)、`test_truncated_request_body_...`、`test_slow_client_read_timeout_returns_408_without_forwarding` |
| 1f | `Route.api_key` 用 `field(repr=False)` + 断言 `repr(route)` 不含密钥 | `field(repr=False)` | `test_route_repr_hides_api_key`、`test_api_key_never_leaves_gateway` |
| 1g | `main()` 端口解析错误 ⇒ 明确报错退出（不裸 traceback） | `listen_from_env` 捕获 `ValueError`/越界；`main()` 统一 `_startup_failed` ⇒ rc 2 | `test_listen_from_env_rejects_bad_port`(4)、`test_main_startup_failures_return_nonzero_without_traceback`(4) |
| 1h | 建议项：client 复用 / 上限口径 —— 改或如实说明 | **都改**：client 单例复用（+`follow_redirects=False`）；响应侧上限对齐 512KiB 并流式截断；请求侧 8MiB **有意保留**并写明理由 | `test_httpx_forwarder_reuses_one_client`、`test_response_cap_matches_consumer_cap`、`test_upstream_response_cap_bounds_buffer_and_marks_incomplete`、`test_oversize_upstream_response_denied_end_to_end`；理由见 docs §9.1 |
| 2 | 扩充测试：8 里每条补真用例 + 保留既有 10 条 + rc=0 | 49 个测试函数 / 107 条；既有 10 条**函数名与断言全保留**（仅反向自检的打印方式按 glm-7 改为 `print()`） | §2 原文 rc=0 |
| 3 | 更新文档：入站鉴权 / 唯一键与判定 / 新 deny_reason / 非回环约束 + 逐条对账 | docs §2、§3.1、§4、§9、§10（14 组对账）、§11 | — |
| 4 | 交付文档：逐条对账表 + 真跑原文 + 未自跑标注 | 本文件 §2/§3/§5/§6 | — |

## 5. 会审 9 组意见逐条对账（详细版见 docs §10）

| 会审组 | 意见要点 | 处理 | 用例名 / 证据 |
|-------|---------|------|--------------|
| **1 严重·响应头注入** | 头值取自上游体、无字符校验 ⇒ CRLF 响应拆分 | **已改** | `test_proof_header_value_charset_whitelist_denies`、`test_crlf_in_upstream_id_cannot_inject_response_headers`（§3.1 原文）、`test_unsafe_route_identity_denies_before_forwarding` |
| **2 严重·开放代理/confused deputy** | 入站无鉴权，非回环 ⇒ 密钥代理裸奔 | **已改** | `test_non_loopback_listen_without_token_refuses_to_start`、`test_inbound_token_rejects_missing_and_wrong_bearer`、`test_main_refuses_non_loopback_listen_without_token`、§3.1/§3.2 原文 |
| **3 严重·审计唯一性** | 无请求侧 nonce/序号，`(response_sha256, upstream_request_id)` 非唯一键 | **已改** | `test_issued_row_binds_seq_request_and_proof_headers`、`test_audit_seq_is_monotonic_across_calls`、`test_audit_seq_continues_after_restart_without_rollback`、`test_audit_seq_never_reused_across_processes`、`test_audit_seq_is_monotonic_under_concurrency`；判定方法 docs §3.1 |
| **4 一般·转发异常零记录** | `handle()` 未捕获 ⇒ 线程炸、连接断、账本零记录 | **已改** | `test_forward_exception_returns_502_and_audits_denial`(4 类异常)、`test_forward_exception_over_http_returns_502_not_dropped_connection` |
| **5 一般·缺 CL 静默转发** | 无 CL（chunked）⇒ `length=0` 把空体转发 | **已改** | `test_chunked_transfer_encoding_returns_400_without_forwarding`、`test_missing_content_length_returns_400_without_forwarding`、`test_invalid_content_length_returns_400_without_forwarding`(5)、`test_truncated_request_body_returns_400_without_forwarding` |
| **6 一般·repr 泄密钥** | `Route.api_key` 未 `field(repr=False)` | **已改** | `test_route_repr_hides_api_key`、`test_api_key_never_leaves_gateway` |
| **7 一般·端口解析** | `int(env[...PORT])` 未捕获 `ValueError` | **已改** | `test_listen_from_env_rejects_bad_port`(4)、`test_main_startup_failures_return_nonzero_without_traceback` |
| **8 一般·测试缺口** | body_not_object / id 异常 / 413 / 404 / 转发异常 / route_from_env / main() / 并发完整性 / 回包无 api_key | **已改（全部补真用例）** | `test_denial_upstream_body_not_object`(6)、`test_denial_upstream_id_empty_or_non_string`(7)、`test_request_too_large_returns_413_without_forwarding`、`test_path_not_found_returns_404_and_audits`、`test_route_from_env_success_path`、`test_gateway_from_env_success_and_missing_audit`、`test_main_success_path_serves_and_closes`、`test_audit_seq_is_monotonic_under_concurrency`、`test_api_key_never_leaves_gateway` |
| **9 建议·client 复用 + 上限口径** | 每请求新建 client；8MiB vs 512KiB 口径不一致 | **已改（两条都改）**；请求侧 8MiB 有意保留，理由与响应侧对齐一并写进 docs §9.1/§9.2 | `test_httpx_forwarder_reuses_one_client`、`test_httpx_forwarder_sends_bearer_and_identity`、`test_response_cap_matches_consumer_cap`、`test_upstream_response_cap_bounds_buffer_and_marks_incomplete`、`test_oversize_upstream_response_denied_end_to_end` |

### 5.1 会审原文里**未改**的项与实测理由

| 会审意见 | 为什么不改 | 依据 |
|---------|-----------|------|
| qwen（截断段）：转发异常应让消费侧走 `k2_gateway_outcome_unknown`（可重试）而非 `k2_gateway_http_502`（已定论） | **任务书第 4 条明确要求"转发器异常 ⇒ 502 + 审计拒发行"**，改分类会违反任务书；且分类口径属消费侧/上游预算（仓库有 `fix/infra-fault-classify` 前例），不在本模块边界 | docs §9.3 写明区分办法（回查 `deny_reason=forward_exception` + `detail`） |
| qwen：`deny_reason` 只进回包 body，而消费侧对非 200 从不读 body | 消费侧不改（本任务不改判据），故只能文档化；账本仍是唯一判定依据 | docs §9.4 |
| qwen-7 后半：每条审计持全局锁 `fsync`，热路径串行化 | **不改**：去掉锁会换来序号重号/两行交错，代价远大于吞吐。审计本身是低频路径（每次 K2 审查一行） | `test_audit_seq_never_reused_across_processes`（锁的必要性） |
| qwen-10：需补输入确认 `tools/` 无 `__init__.py` 的 packaging 约定 | **不改（已核实无风险）**：`pyproject.toml` 无 `[build-system]`、无显式 packages 列表；`find_spec("tools")` ⇒ `origin=None`、search locations = `tools/` ⇒ 命名空间包正常解析 | §1 末段 |
| qwen-11：`§0` 行号锚点（会审称 `_parse_response` 在 190） | **不改（会审这一条不成立）**：`rg -n "^def _parse_response" app/semantic_review_runner.py` = **189**；其余锚点已逐一重核 | docs §0 末注 |
| glm-3 一半：`MAX_FORWARD_BYTES` 与消费侧 512KiB 口径不一致 | 请求侧**有意保留 8MiB**：消费侧根本不限制请求体，收紧会凭空拒掉合法的大 dossier；只有响应侧口径错位会污染账本/收据配对，那一侧已对齐 | docs §9.1 |

## 6. 未自跑 / 边界声明（如实标注，未跑过的一律不写成跑过）

- ✅ **已真跑**：`attest()`/`AuditLog`/`make_httpx_forwarder`/`make_server` 的发放、全部
  拒发分支（上游状态 / 非 JSON / 非对象 / 缺 id / 缺 model / model 不符 / 三类字符
  白名单 / 响应超限 / 路由身份脏 / 转发异常）、append-only、跨进程序号、并发序号、
  半行封口、端到端、反向自检——§2 全部 rc=0。
- ✅ **已真跑**：`route_from_env` / `gateway_from_env` / `listen_from_env` 的**成功与
  全部失败路径**；`main()` 的**全部失败路径**（rc=2 + 单行原因 + 无 traceback）与**成功
  路径的完整生命周期**（用假 server 替掉 `serve_forever`，断言 host/port/token 传参、
  `serve_forever → shutdown → server_close → gateway.close` 都被调用，见
  `test_main_success_path_serves_and_closes`）。真实 HTTP 往返的 401/404/400/408/413/
  502/200 均经真实 socket 跑过。
- ✅ **已真跑**：跨进程审计序号（两个独立解释器 + 父进程，§3.4）。
- ❌ **未自跑**：`main()` 里**真实** `serve_forever()` 常驻（起 ongoing service 越界）。
  代码路径本身在测试内以临时端口起停验证过（`make_server` + 真实 handler 线程），
  但 `main()` 的常驻循环、信号处理（`KeyboardInterrupt` 分支）、`gateway.close()`
  里的真实连接池释放未经真实 `main()` 进程验证。
- ❌ **未自跑**：**非回环监听 + 已配令牌**的**实际绑定**（`make_server(gateway,
  "0.0.0.0", 0, inbound_token=…)`）。理由：测试里对外开监听口没有必要且不合规；
  该分支的判定逻辑由纯函数 `check_inbound_binding`/`is_loopback_host` 覆盖
  （`test_non_loopback_listen_without_token_refuses_to_start` 内含 6 个主机名断言），
  且 `main()` 的**拒绝**分支（补配令牌前的必然状态）已实测 rc=2。
- ❌ **未自跑**：**真实 https 上游 + 内网 CA + 自定义信任锚**。测试全用回环 http +
  合成上游。生产 TLS 落地方法见 docs §5.2，**未做端到端 TLS 实测**。
- ❌ **未自跑**：`make_httpx_forwarder` 对**真实远端上游**的调用（只对合成上游跑过；
  §3.1 的转发也是打到合成上游）。**未使用任何真实模型、真实网关、真实 api_key**。
- ❌ **未自跑**：`semantic_review_runner.review_snapshot` 的完整 DB 事务 / ACL 探针 /
  seat 预留 / **真实收据落库**（`semantic_review_calls`）。任务书限定"合成正文、不落
  真库"，故只用 `_post_once` + `_parse_response` 验证"头能被消费侧核验"。收据链真正
  落库与 `GATE_K2_evidence` 转绿**不在本次交付范围**。
- ❌ **未自跑**：POSIX 分支的 `fcntl.flock` 路径（当前环境 win32，只实测了
  `msvcrt.locking` 分支）。两分支代码对称，POSIX 侧需 CI 复核。
- ❌ **未跑全量套件**：只跑了 `tests/test_attestation_gateway.py`（验收命令）与
  `tests/test_compile_all.py`（1 passed，确认全库仍可编译）。**没有**跑
  `python -m pytest` 全量；本模块不改动其它测试依赖的判据文件，但全量绿与否本轮未验证。

## 7. 工作树清洁

- `git status --short` 只有：`M tests/test_attestation_gateway.py`、
  `M tools/attestation_gateway.py`（本轮提交前状态），加授权清单内的
  `docs/attestation_gateway_20260930.md`、`DELIVERY_k2_gateway_harden.md`。
- **无任何清单外新文件**：所有验证脚本均经 `python - <<'PY'`（stdin）或 `-c` 一次性
  运行，脚本本体未落盘；临时审计文件写在 `tempfile.mkdtemp()` 下（检出目录之外）。
- 未创建 `data/`（`ls data` ⇒ 不存在）；`__pycache__` 为 pytest 运行产物（已被
  `.gitignore` 覆盖，`git status` 不显示）。
- 未 commit / 未 merge / 未 push / 未改主仓工作区 / 未改 `app/`。
