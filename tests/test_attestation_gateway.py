"""K2 可信证明网关测试 —— 全合成上游，**零真实模型调用、不落真实库**。

覆盖三层：
  1. 原任务书五条（正常发放 / 拒发 / append-only / 端到端 / 反向自检）；
  2. 会审 BLOCK 后的加固项（证明头字符白名单、入站鉴权 fail-closed、审计单调序号、
     转发异常零记录、Content-Length 硬门、api_key 不入 repr、main() 端口解析、
     client 复用、上限口径对齐）；
  3. 会审点名的测试缺口（body_not_object / id 非字符串 / 401/404/400/408/413 /
     转发异常 / route_from_env 成功路径 / gateway_from_env / main() 成功与失败路径 /
     并发下审计行完整性 / 回包不含 api_key / 跨进程序号不重号）。
"""
from __future__ import annotations

import http.client
import http.server
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

import app.config as config
import app.semantic_review_runner as runner
import tools.attestation_gateway as gw

# 与消费侧判据对齐的固定身份：网关核准路由与 runner 审查路由必须一致。
PROVIDER = "provider-a"
UPSTREAM_MODEL = "actual-a"
CHANNEL_ID = "channel-7"
REQUESTED_MODEL = "requested-a"
UPSTREAM_ID = "up-1"
INSTANCE_IDS = {"SI-A"}
_API_KEY = "sk-synthetic"
INBOUND_TOKEN = "synthetic-inbound-token"

_REVIEW = {"verdict": "PASS", "reason": "逐条核过本卡实例及反例",
           "cited_instance_ids": ["SI-A"], "concerns": []}
_COMPLETION_TEXT = json.dumps(_REVIEW, ensure_ascii=False)

_ALL_ENV_KEYS = ("LG_ATTEST_UPSTREAM_BASE_URL", "LG_ATTEST_UPSTREAM_API_KEY",
                 "LG_ATTEST_ROUTE_PROVIDER", "LG_ATTEST_ROUTE_MODEL",
                 "LG_ATTEST_ROUTE_CHANNEL_ID", "LG_ATTEST_AUDIT_PATH",
                 gw.LISTEN_HOST_ENV, gw.LISTEN_PORT_ENV, gw.INBOUND_TOKEN_ENV)


def _completion_body(*, upstream_id=UPSTREAM_ID, model=UPSTREAM_MODEL,
                     text=_COMPLETION_TEXT, finish="stop") -> bytes:
    """OpenAI 风格完成体：runner 的 _parse_response 只认这一形状。"""
    return json.dumps({
        "id": upstream_id, "model": model,
        "choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": text}}],
    }, ensure_ascii=False).encode("utf-8")


def _route(base_url="http://upstream.invalid/v1", api_key=_API_KEY) -> gw.Route:
    return gw.Route(base_url, api_key, PROVIDER, UPSTREAM_MODEL, CHANNEL_ID)


def _review_route() -> runner.ReviewRoute:
    return runner.ReviewRoute(REQUESTED_MODEL, PROVIDER, UPSTREAM_MODEL, CHANNEL_ID)


def _stub_forwarder(reply: gw.UpstreamReply):
    return lambda request_body: reply


def _read_audit_lines(path):
    with open(path, "rb") as handle:
        return [ln for ln in handle.read().split(b"\n") if ln]


def _records(path):
    return [json.loads(line) for line in _read_audit_lines(path)]


def _parseable_records(path):
    """读取侧口径：跳过不可解析行（硬 kill 残留的半行），其余逐行解析。"""
    out = []
    for line in _read_audit_lines(path):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _offline_gateway(tmp_path, **route_kwargs):
    """一个不监听端口、转发器为桩的网关对象（供纯函数/启动校验类用例）。"""
    route = _route(**route_kwargs)
    forward = _stub_forwarder(gw.UpstreamReply(200, _completion_body()))
    return gw.AttestationGateway(route, forward,
                                 gw.AuditLog(str(tmp_path / "audit.jsonl")))


def _request_body() -> bytes:
    return json.dumps({"model": REQUESTED_MODEL,
                       "messages": [{"role": "user", "content": "冻结证据"}]}
                      ).encode("utf-8")


def _apply_env(monkeypatch, env: dict) -> None:
    """把整组 LG_ATTEST_* 环境变量对齐到给定字典（先清干净，不受外部环境影响）。"""
    for key in _ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _gateway_env(tmp_path, **overrides) -> dict:
    env = {
        "LG_ATTEST_UPSTREAM_BASE_URL": "https://upstream.invalid/v1",
        "LG_ATTEST_UPSTREAM_API_KEY": _API_KEY,
        "LG_ATTEST_ROUTE_PROVIDER": PROVIDER,
        "LG_ATTEST_ROUTE_MODEL": UPSTREAM_MODEL,
        "LG_ATTEST_ROUTE_CHANNEL_ID": CHANNEL_ID,
        "LG_ATTEST_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
    }
    env.update(overrides)
    return env


# ── HTTP 小工具：一个正常的 POST（走 http.client）、一个裸 socket POST ──────────
def _post(port: int, body: bytes, headers: dict | None = None, *, path="/v1/chat/completions",
          timeout: float = 20.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, {name.lower(): value
                                 for name, value in response.getheaders()}, response.read()
    finally:
        conn.close()


def _raw_post(port: int, request: bytes, *, half_close: bool = False,
              timeout: float = 20.0):
    """裸 socket 发请求（构造畸形 HTTP 头用），返回响应原始字节。"""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        buffer = bytearray()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer.extend(chunk)
    return bytes(buffer)


def _split_response(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        name, _, value = line.decode("latin-1").partition(":")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    return status, headers, body


# ── 合成上游：一个可控的 stdlib HTTP 服务，绝不打真实模型 ────────────────────
class _SyntheticUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    status = 200
    body = b"{}"
    forwarded = []  # 类级：记录收到的请求体，验证"原样转发"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.__class__.forwarded.append(self.rfile.read(length))
        body = self.__class__.body
        self.send_response(self.__class__.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音访问日志
        return


def _start(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop(server):
    server.shutdown()
    server.server_close()


def _make_upstream(status=200, body=None):
    handler = type("SyntheticUp", (_SyntheticUpstreamHandler,),
                   {"status": status, "body": body if body is not None
                    else _completion_body(), "forwarded": []})
    server = _start(handler)
    return server, handler, f"http://127.0.0.1:{server.server_address[1]}/v1"


# 便捷：起一个"网关 + 合成上游"的完整链路，并把 runner 指向该网关。
def _bring_up_stack(monkeypatch, tmp_path, *, status=200, body=None,
                    drop_headers=(), inbound_token="", forward_error=None):
    up_server, up_handler, up_url = _make_upstream(status, body)
    route = _route(up_url)
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))

    gateway = gw.AttestationGateway(route, gw.make_httpx_forwarder(route), audit)
    if drop_headers:
        inner_handle = gateway.handle

        def handle(request_body):
            result = inner_handle(request_body)
            for name in drop_headers:
                result.headers.pop(name, None)
            return result

        gateway.handle = handle  # 反向自检：故意让网关少发一个证明头
    if forward_error is not None:
        def forward_error_body(request_body, _exc=forward_error):
            raise _exc

        gateway.forward = forward_error_body

    gw_server = gw.make_server(gateway, "127.0.0.1", 0, inbound_token=inbound_token)
    threading.Thread(target=gw_server.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "GATEWAY_BASE_URL",
                        f"http://127.0.0.1:{gw_server.server_address[1]}/v1")
    monkeypatch.setattr(config, "GATEWAY_API_KEY", "synthetic-key")

    def teardown():
        _stop(gw_server)
        gateway.close()
        _stop(up_server)

    return {"up_server": up_server, "up_handler": up_handler, "gw_server": gw_server,
            "route": route, "audit": audit, "gateway": gateway,
            "port": gw_server.server_address[1], "teardown": teardown}


# ── 1. 正常路径：四个证明头与响应体 id/model 一致 ────────────────────────────
def test_normal_attestation_headers_match_upstream_body(tmp_path):
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    reply = gw.UpstreamReply(200, _completion_body())
    result = gw.attest(b'{"model":"requested-a"}', _route(), _stub_forwarder(reply), audit)

    assert result.status == 200
    assert result.headers == {
        "x-lg-upstream-provider": PROVIDER,
        "x-lg-upstream-model": UPSTREAM_MODEL,
        "x-lg-upstream-channel-id": CHANNEL_ID,
        "x-lg-upstream-request-id": UPSTREAM_ID,
    }
    # 硬约束：request-id 恒等体 id、model 恒等体 model。
    parsed_body = json.loads(result.body)
    assert result.headers["x-lg-upstream-request-id"] == parsed_body["id"]
    assert result.headers["x-lg-upstream-model"] == parsed_body["model"]
    # 上游响应体原样回传。
    assert result.body == reply.body

    lines = _read_audit_lines(audit.path)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["decision"] == gw.ISSUED
    assert record["upstream_id"] == UPSTREAM_ID and record["upstream_model"] == UPSTREAM_MODEL
    assert record["request_sha256"] == gw._sha256(b'{"model":"requested-a"}')
    assert record["response_sha256"] == gw._sha256(reply.body)
    assert record["route"] == {"provider": PROVIDER, "model": UPSTREAM_MODEL,
                               "channel_id": CHANNEL_ID}
    assert "api_key" not in json.dumps(record)  # 绝不落密钥


# ── 2. 拒发：缺 id / 缺 model / model 不符 / 上游 5xx / 非 JSON ────────────────
@pytest.mark.parametrize("case,status,body,expected_reason", [
    ("missing_id", 200, json.dumps({"model": UPSTREAM_MODEL, "choices": []}).encode(),
     "upstream_missing_id"),
    ("missing_model", 200, json.dumps({"id": UPSTREAM_ID, "choices": []}).encode(),
     "upstream_missing_model"),
    ("model_mismatch", 200, _completion_body(model="alias-b"),
     "upstream_model_route_mismatch"),
    ("upstream_5xx", 503, b"upstream unavailable", "upstream_status_503"),
    ("not_json", 200, b"plain text not json", "upstream_body_not_json"),
])
def test_denial_paths_return_502_without_headers(tmp_path, case, status, body,
                                                  expected_reason):
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    result = gw.attest(b'{"x":1}', _route(), _stub_forwarder(gw.UpstreamReply(status, body)), audit)

    assert result.status == gw.DENY_STATUS == 502
    # 拒发 ⇒ 不回任何证明头。
    assert result.headers == {}
    assert not any(name.startswith(gw.ATTESTATION_HEADER_PREFIX) for name in result.headers)

    lines = _read_audit_lines(audit.path)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["decision"] == gw.DENIED  # 拒发
    assert record["deny_reason"] == expected_reason
    assert record["response_sha256"] == gw._sha256(body)


# ── 3. append-only：连发两次 ⇒ 2 行，且首行逐字节未变 ─────────────────────────
def test_audit_is_append_only(tmp_path):
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    forward = _stub_forwarder(gw.UpstreamReply(200, _completion_body()))
    route = _route()

    gw.attest(b"first-request", route, forward, audit)
    after_first = _read_audit_lines(audit.path)
    assert len(after_first) == 1

    gw.attest(b"second-request", route, forward, audit)
    after_second = _read_audit_lines(audit.path)
    assert len(after_second) == 2
    # 首行内容逐字节未变（不覆盖、不重写历史行）。
    assert after_second[0] == after_first[0]
    # 第二行确为新记录，而非首行副本。
    assert after_second[1] != after_first[0]
    second = json.loads(after_second[1])
    assert second["request_sha256"] == gw._sha256(b"second-request")


# ── 4. 端到端：起本网关（合成上游）后 runner 产出合格 review ──────────────────
def test_end_to_end_runner_produces_qualifying_review(monkeypatch, tmp_path):
    stack = _bring_up_stack(monkeypatch, tmp_path)
    try:
        request_json = json.dumps({"model": REQUESTED_MODEL,
                                   "messages": [{"role": "user", "content": "冻结证据"}]})
        response = runner._post_once(request_json, 15)
        assert response.status_code == 200

        parsed = runner._parse_response(response, _review_route(), INSTANCE_IDS)
        assert parsed["review"] == _REVIEW
        assert parsed["review"]["verdict"] == "PASS"
        assert parsed["headers"]["request-id"] == UPSTREAM_ID
        assert parsed["headers"]["model"] == UPSTREAM_MODEL
        # 网关确把请求体原样转发到了核准上游。
        assert stack["up_handler"].forwarded == [request_json.encode("utf-8")]
        # 审计记一条发放。
        records = _records(stack["audit"].path)
        assert len(records) == 1 and records[0]["decision"] == gw.ISSUED
    finally:
        stack["teardown"]()


# ── 5. 反向自检（核心）：网关少发一个头 ⇒ _parse_response 必抛 ReviewResponseError ─
def test_reverse_selfcheck_missing_header_rejected_by_runner(monkeypatch, tmp_path):
    stack = _bring_up_stack(monkeypatch, tmp_path,
                            drop_headers=("x-lg-upstream-channel-id",))
    try:
        request_json = json.dumps({"model": REQUESTED_MODEL,
                                   "messages": [{"role": "user", "content": "冻结证据"}]})
        response = runner._post_once(request_json, 15)
        # 网关仍返回 200，但少发了 channel-id 证明头。
        assert response.status_code == 200
        assert "x-lg-upstream-channel-id" not in response.headers

        with pytest.raises(runner.ReviewResponseError) as excinfo:
            runner._parse_response(response, _review_route(), INSTANCE_IDS)
        assert str(excinfo.value) == "k2_response_unverifiable"
        # 贴原文（真跑可见）：走 pytest 捕获机制，-s 时进真实 stdout。
        print(f"[REVERSE-CHECK] {type(excinfo.value).__module__}."
              f"{type(excinfo.value).__name__}: {excinfo.value}"
              f" | __cause__: {type(excinfo.value.__cause__).__name__}: "
              f"{excinfo.value.__cause__}")
    finally:
        stack["teardown"]()


# ── 边界自检：上游拒发时，runner 端也拿不到证明头（走 502 分支）───────────────
def test_denial_upstream_end_to_end_runner_sees_502_no_headers(monkeypatch, tmp_path):
    stack = _bring_up_stack(monkeypatch, tmp_path, status=503,
                            body=b"upstream unavailable")
    try:
        response = runner._post_once('{"model":"requested-a"}', 15)
        # 网关把非 200 上游统一映射为 502，且零证明头。
        assert response.status_code == 502
        assert not [name for name in response.headers
                    if name.startswith(gw.ATTESTATION_HEADER_PREFIX)]
        with pytest.raises(runner.ReviewResponseError, match="k2_gateway_http_502"):
            runner._parse_response(response, _review_route(), INSTANCE_IDS)
        records = _records(stack["audit"].path)
        assert len(records) == 1 and records[0]["decision"] == gw.DENIED
    finally:
        stack["teardown"]()


# ══════════════════════════════════════════════════════════════════════════════
# 会审 BLOCK 后的加固用例
# ══════════════════════════════════════════════════════════════════════════════

# ── A. 证明头字符白名单（响应头注入）：含 CR/LF/空格/超长 ⇒ 拒发 ──────────────
@pytest.mark.parametrize("case,body_kwargs,expected_reason", [
    ("id_crlf_injection", {"upstream_id": "up-1\r\nSet-Cookie: pwn=1\r\n"},
     "upstream_id_invalid_chars"),
    ("id_trailing_newline", {"upstream_id": "up-1\n"}, "upstream_id_invalid_chars"),
    ("id_with_space", {"upstream_id": "up 1"}, "upstream_id_invalid_chars"),
    ("id_too_long", {"upstream_id": "a" * 201}, "upstream_id_invalid_chars"),
    ("id_tab", {"upstream_id": "up\t1"}, "upstream_id_invalid_chars"),
    ("id_non_ascii", {"upstream_id": "编号-1"}, "upstream_id_invalid_chars"),
    ("model_crlf_injection", {"model": "actual-a\r\nX-Injected: 1"},
     "upstream_model_invalid_chars"),
    ("model_with_space", {"model": "actual a"}, "upstream_model_invalid_chars"),
    ("model_too_long", {"model": "m" * 201}, "upstream_model_invalid_chars"),
])
def test_proof_header_value_charset_whitelist_denies(tmp_path, case, body_kwargs,
                                                    expected_reason):
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    body = _completion_body(**body_kwargs)
    result = gw.attest(b'{"x":1}', _route(), _stub_forwarder(gw.UpstreamReply(200, body)),
                       audit)

    assert result.status == gw.DENY_STATUS == 502
    assert result.headers == {}          # 一个证明头都不发
    assert result.body.find(b"\r") < 0 and result.body.find(b"\n") < 0

    record = _records(audit.path)[0]
    assert record["decision"] == gw.DENIED
    assert record["deny_reason"] == expected_reason
    # 脏值留证（JSON 转义，账本行里没有裸 CR/LF ⇒ 行结构不被污染）。
    assert record["response_sha256"] == gw._sha256(body)
    assert b"\r" not in open(audit.path, "rb").read()


def test_crlf_in_upstream_id_cannot_inject_response_headers(monkeypatch, tmp_path):
    """端到端反证：脏 id 到达真实 handler 也不能注进任何响应头。"""
    evil_id = "up-1\r\nSet-Cookie: pwn=1\r\nX-Injected: yes"
    stack = _bring_up_stack(monkeypatch, tmp_path, body=_completion_body(upstream_id=evil_id))
    try:
        status, headers, body = _post(stack["port"], _request_body())

        assert status == 502
        assert "set-cookie" not in headers and "x-injected" not in headers
        assert not [name for name in headers
                    if name.startswith(gw.ATTESTATION_HEADER_PREFIX)]
        assert json.loads(body)["error"]["reason"] == "upstream_id_invalid_chars"
        # 上游确实被真实往返过一次（不是"没调用所以干净"）。
        assert stack["up_handler"].forwarded == [_request_body()]

        records = _records(stack["audit"].path)
        assert len(records) == 1
        assert records[0]["decision"] == gw.DENIED
        assert records[0]["deny_reason"] == "upstream_id_invalid_chars"
        assert records[0]["upstream_id"] == evil_id   # 原样留证（JSON 已转义）
    finally:
        stack["teardown"]()
    assert b"\r" not in (tmp_path / "audit.jsonl").read_bytes()


def test_unsafe_route_identity_denies_before_forwarding(tmp_path):
    """路由身份自己脏 ⇒ 转发前就停，一个证明头都不出。"""
    route = gw.Route("http://127.0.0.1:1/v1", _API_KEY,
                     "provider\r\nX-Evil: 1", UPSTREAM_MODEL, CHANNEL_ID)
    assert route.unsafe_header_fields() == ["provider"]

    calls = []

    def forward(request_body):
        calls.append(request_body)
        return gw.UpstreamReply(200, _completion_body())

    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    result = gw.attest(b'{"x":1}', route, forward, audit)

    assert result.status == 502 and result.headers == {}
    assert calls == []                       # 压根没出话
    record = _records(audit.path)[0]
    assert record["deny_reason"] == "route_identity_invalid_chars"
    assert record["detail"] == "provider"
    assert record["response_sha256"] is None


# ── B. 入站鉴权 fail-closed：非回环无令牌拒启动；令牌不符回 401 ────────────────
def test_non_loopback_listen_without_token_refuses_to_start(tmp_path):
    gateway = _offline_gateway(tmp_path)

    with pytest.raises(gw.GatewayStartupError) as excinfo:
        gw.make_server(gateway, "0.0.0.0", 0)
    assert "inbound_token_required_for_non_loopback_listen" in str(excinfo.value)
    assert gw.INBOUND_TOKEN_ENV in str(excinfo.value)      # 明确告诉运维配哪个变量
    assert isinstance(excinfo.value, RuntimeError)          # 与 route_from_env 同风格

    # 回环不拦；配了令牌的非回环放行（纯函数判定，不开监听口）。
    gw.check_inbound_binding("127.0.0.1", "")
    gw.check_inbound_binding("::1", "")
    gw.check_inbound_binding("localhost", "")
    gw.check_inbound_binding("0.0.0.0", INBOUND_TOKEN)
    for host in ("0.0.0.0", "10.1.2.3", "example.invalid", "::", ""):
        with pytest.raises(gw.GatewayStartupError):
            gw.check_inbound_binding(host, "")
    # "::" 不是回环（它是全零地址），必须有令牌。
    with pytest.raises(gw.GatewayStartupError):
        gw.check_inbound_binding("::", "")


@pytest.mark.parametrize("header,token,expected", [
    (None, "", True),          # 未配令牌 ⇒ 不拦（此时只允许回环）
    ("Bearer tok", "", True),
    ("Bearer tok", "tok", True),
    ("bearer tok", "tok", True),          # scheme 大小写不敏感（RFC 7235）
    ("BEARER tok", "tok", True),
    ("Bearer   tok  ", "tok", True),
    ("Bearer tok2", "tok", False),
    ("Bearer  tok2", "tok", False),
    ("Basic tok", "tok", False),
    ("tok", "tok", False),                # 缺 scheme
    ("", "tok", False),
    (None, "tok", False),                 # 缺头
    ("Bearer ", "tok", False),
])
def test_inbound_authorization_matrix(header, token, expected):
    assert gw.check_inbound_authorization(header, token) is expected


def test_inbound_token_rejects_missing_and_wrong_bearer(monkeypatch, tmp_path):
    stack = _bring_up_stack(monkeypatch, tmp_path, inbound_token=INBOUND_TOKEN)
    try:
        # 无 Authorization ⇒ 401、零证明头、上游一次都没被触达。
        status, headers, _ = _post(stack["port"], _request_body())
        assert status == 401
        assert headers["www-authenticate"].startswith("Bearer")
        assert not [name for name in headers
                    if name.startswith(gw.ATTESTATION_HEADER_PREFIX)]
        assert stack["up_handler"].forwarded == []

        # 令牌不符 ⇒ 同上（且每次入站 POST 都留一行审计）。
        status, headers, _ = _post(stack["port"], _request_body(),
                                   {"Authorization": "Bearer wrong-token"})
        assert status == 401
        assert stack["up_handler"].forwarded == []

        denied = _records(stack["audit"].path)
        assert [row["deny_reason"] for row in denied] == ["inbound_unauthorized"] * 2
        assert all(row["response_sha256"] is None for row in denied)
        # 未读入请求体（鉴权前就拒）⇒ request_sha256 = SHA256("")。
        assert all(row["request_sha256"] == gw._sha256(b"") for row in denied)

        # 令牌正确 ⇒ 正常发放。
        status, headers, _ = _post(stack["port"], _request_body(),
                                   {"Authorization": f"Bearer {INBOUND_TOKEN}"})
        assert status == 200
        assert headers["x-lg-upstream-request-id"] == UPSTREAM_ID
        assert len(stack["up_handler"].forwarded) == 1
        assert _records(stack["audit"].path)[-1]["decision"] == gw.ISSUED
    finally:
        stack["teardown"]()


# ── C. 转发器异常：502 + 审计拒发行（绝不零记录、绝不连接中断）─────────────────
@pytest.mark.parametrize("error_name,exc,expected_detail", [
    ("connect_timeout", httpx.ConnectTimeout("synthetic"), "ConnectTimeout"),
    ("connect_error", httpx.ConnectError("synthetic"), "ConnectError"),
    ("read_timeout", httpx.ReadTimeout("synthetic"), "ReadTimeout"),
    ("unexpected", RuntimeError("synthetic boom"), "RuntimeError"),
])
def test_forward_exception_returns_502_and_audits_denial(tmp_path, error_name, exc,
                                                         expected_detail):
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))

    def forward(request_body):
        raise exc

    result = gw.attest(b'{"x":1}', _route(), forward, audit)

    assert result.status == gw.DENY_STATUS == 502
    assert result.headers == {}
    records = _records(audit.path)
    assert len(records) == 1
    assert records[0]["decision"] == gw.DENIED
    assert records[0]["deny_reason"] == "forward_exception"
    assert records[0]["detail"] == expected_detail
    assert records[0]["request_sha256"] == gw._sha256(b'{"x":1}')
    assert records[0]["response_sha256"] is None    # 压根没有上游体
    assert _API_KEY not in json.dumps(records[0])


def test_forward_exception_over_http_returns_502_not_dropped_connection(monkeypatch, tmp_path):
    stack = _bring_up_stack(monkeypatch, tmp_path,
                            forward_error=httpx.ConnectTimeout("synthetic"))
    try:
        status, headers, body = _post(stack["port"], _request_body())

        # 客户端拿到的是 502，而不是连接中断。
        assert status == 502
        assert not [name for name in headers
                    if name.startswith(gw.ATTESTATION_HEADER_PREFIX)]
        assert json.loads(body)["error"]["reason"] == "forward_exception"
        assert stack["up_handler"].forwarded == []     # 一次真实往返都没发生

        records = _records(stack["audit"].path)
        assert len(records) == 1
        assert records[0]["deny_reason"] == "forward_exception"
        assert records[0]["detail"] == "ConnectTimeout"
        # 消费侧对非 200 从不读 body ⇒ 只看得见状态码分类（口径见文档 §9）。
        response = httpx.Response(status, headers={}, content=body,
                                  request=httpx.Request("POST", "http://127.0.0.1/v1"))
        with pytest.raises(runner.ReviewResponseError, match="k2_gateway_http_502"):
            runner._parse_response(response, _review_route(), INSTANCE_IDS)
    finally:
        stack["teardown"]()


# ── D. 协议层硬门：缺/非法 Content-Length、chunked、超限、截断、404 一律不转发 ──
def test_chunked_transfer_encoding_returns_400_without_forwarding(monkeypatch, tmp_path):
    stack = _bring_up_stack(monkeypatch, tmp_path)
    try:
        raw = _raw_post(stack["port"],
                        b"POST /v1/chat/completions HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n"
                        b"Content-Type: application/json\r\n\r\n"
                        b"4\r\n{\"a\":1}\r\n0\r\n\r\n")
        status, headers, body = _split_response(raw)

        assert status == 400
        assert not [name for name in headers if name.startswith(gw.ATTESTATION_HEADER_PREFIX)]
        assert json.loads(body)["error"]["reason"] == "transfer_encoding_unsupported"
        assert stack["up_handler"].forwarded == []
        assert _records(stack["audit"].path)[0]["deny_reason"] == "transfer_encoding_unsupported"
    finally:
        stack["teardown"]()


def test_missing_content_length_returns_400_without_forwarding(monkeypatch, tmp_path):
    stack = _bring_up_stack(monkeypatch, tmp_path)
    try:
        raw = _raw_post(stack["port"],
                        b"POST /v1/chat/completions HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n\r\n"
                        b"{\"a\":1}")
        status, _headers, body = _split_response(raw)

        assert status == 400
        assert json.loads(body)["error"]["reason"] == "content_length_missing"
        assert stack["up_handler"].forwarded == []
        assert _records(stack["audit"].path)[0]["deny_reason"] == "content_length_missing"
    finally:
        stack["teardown"]()


@pytest.mark.parametrize("cl_line,expected_reason", [
    (b"Content-Length: abc\r\n", "content_length_invalid"),
    (b"Content-Length: -1\r\n", "content_length_invalid"),
    (b"Content-Length: 5_0\r\n", "content_length_invalid"),
    (b"Content-Length: 2, 2\r\n", "content_length_invalid"),
    (b"Content-Length: 2\r\nContent-Length: 2\r\n", "content_length_invalid"),
])
def test_invalid_content_length_returns_400_without_forwarding(monkeypatch, tmp_path,
                                                               cl_line, expected_reason):
    stack = _bring_up_stack(monkeypatch, tmp_path)
    try:
        raw = _raw_post(stack["port"],
                        b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        + cl_line + b"Content-Type: application/json\r\n\r\n{}")
        status, _headers, body = _split_response(raw)

        assert status == 400
        assert json.loads(body)["error"]["reason"] == expected_reason
        assert stack["up_handler"].forwarded == []
        assert _records(stack["audit"].path)[0]["deny_reason"] == expected_reason
    finally:
        stack["teardown"]()


def test_request_too_large_returns_413_without_forwarding(monkeypatch, tmp_path):
    stack = _bring_up_stack(monkeypatch, tmp_path)
    try:
        raw = _raw_post(stack["port"],
                        b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        b"Content-Length: "
                        + str(gw.MAX_FORWARD_BYTES + 1).encode() + b"\r\n\r\n")
        status, _headers, body = _split_response(raw)

        assert status == 413
        assert json.loads(body)["error"]["reason"] == "request_too_large"
        assert stack["up_handler"].forwarded == []
        assert _records(stack["audit"].path)[0]["deny_reason"] == "request_too_large"
    finally:
        stack["teardown"]()


def test_truncated_request_body_returns_400_without_forwarding(monkeypatch, tmp_path):
    """声明 100 字节、实发 10 字节就断：绝不静默截断后照常转发记账。"""
    stack = _bring_up_stack(monkeypatch, tmp_path)
    try:
        raw = _raw_post(stack["port"],
                        b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        b"Content-Length: 100\r\n\r\n" + b"x" * 10,
                        half_close=True)
        status, _headers, body = _split_response(raw)

        assert status == 400
        assert json.loads(body)["error"]["reason"] == "request_body_truncated"
        assert stack["up_handler"].forwarded == []
        record = _records(stack["audit"].path)[0]
        assert record["deny_reason"] == "request_body_truncated"
        assert record["request_sha256"] == gw._sha256(b"x" * 10)   # 实读前缀留证
    finally:
        stack["teardown"]()


def test_slow_client_read_timeout_returns_408_without_forwarding(monkeypatch, tmp_path):
    """慢客户端不能无限占住线程：读超时 408 + 审计行，且绝不转发。"""
    monkeypatch.setattr(gw, "HANDLER_SOCKET_TIMEOUT", 0.75)
    stack = _bring_up_stack(monkeypatch, tmp_path)
    try:
        with socket.create_connection(("127.0.0.1", stack["port"]), timeout=25) as sock:
            sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Content-Length: 4000\r\n\r\n" + b"x" * 5)
            buffer = bytearray()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
        status, _headers, body = _split_response(bytes(buffer))

        assert status == 408
        assert json.loads(body)["error"]["reason"] == "request_body_read_timeout"
        assert stack["up_handler"].forwarded == []
        assert _records(stack["audit"].path)[0]["deny_reason"] == "request_body_read_timeout"
    finally:
        stack["teardown"]()


def test_path_not_found_returns_404_and_audits(monkeypatch, tmp_path):
    stack = _bring_up_stack(monkeypatch, tmp_path)
    try:
        status, headers, _ = _post(stack["port"], _request_body(), path="/v1/models")
        assert status == 404
        assert not [name for name in headers
                    if name.startswith(gw.ATTESTATION_HEADER_PREFIX)]
        assert stack["up_handler"].forwarded == []
        assert _records(stack["audit"].path)[0]["deny_reason"] == "path_not_found"
    finally:
        stack["teardown"]()


# ── E. 审计唯一键：单调序号、并发不重号、重启不回退、跨进程不重号 ──────────────
def test_issued_row_binds_seq_request_and_proof_headers(tmp_path):
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    body = _request_body()
    result = gw.attest(body, _route(), _stub_forwarder(gw.UpstreamReply(200, _completion_body())),
                       audit)

    record = _records(audit.path)[0]
    assert record["seq"] == 1
    assert record["request_sha256"] == gw._sha256(body)
    # 证明头 ↔ 账本行一一对应（谁给哪个字段供值，可机械复核）。
    assert result.headers["x-lg-upstream-provider"] == record["route"]["provider"]
    assert result.headers["x-lg-upstream-model"] == record["upstream_model"]
    assert result.headers["x-lg-upstream-channel-id"] == record["route"]["channel_id"]
    assert result.headers["x-lg-upstream-request-id"] == record["upstream_id"]
    assert record["response_sha256"] == gw._sha256(result.body)


def test_audit_seq_is_monotonic_across_calls(tmp_path):
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    route = _route()
    forward = _stub_forwarder(gw.UpstreamReply(200, _completion_body()))

    for index in range(6):
        gw.attest(f"req-{index}".encode(), route, forward, audit)

    records = _records(audit.path)
    assert [row["seq"] for row in records] == [1, 2, 3, 4, 5, 6]
    # 同一上游体重复出现时，(response_sha256, upstream_id) 不再是唯一键，
    # seq 才是 —— 6 行 6 个不重号的序号，恰好 6 次真实往返。
    assert len({row["response_sha256"] for row in records}) == 1
    assert len({row["seq"] for row in records}) == 6


def test_audit_seq_continues_after_restart_without_rollback(tmp_path):
    route = _route()
    forward = _stub_forwarder(gw.UpstreamReply(200, _completion_body()))
    first = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    gw.attest(b"a", route, forward, first)
    gw.attest(b"b", route, forward, first)
    before = open(first.path, "rb").read()

    # 新进程/新实例：从账本接号，不回退、不重号，且不改写历史行。
    second = gw.AuditLog(first.path)
    gw.attest(b"c", route, forward, second)

    records = _records(first.path)
    assert [row["seq"] for row in records] == [1, 2, 3]
    assert open(first.path, "rb").read().startswith(before)


_CROSS_PROCESS_CHILD = """
import sys
sys.path.insert(0, sys.argv[2])
from tools.attestation_gateway import AuditLog, Route
log = AuditLog(sys.argv[1])
route = Route("http://127.0.0.1:1/v1", "sk-child", "p", "m", "c")
for _ in range(3):
    log.record("issued", route, "r" * 64, "s" * 64,
               upstream_id="u", upstream_model="m")
"""


def test_audit_seq_never_reused_across_processes(tmp_path):
    """跨进程：两个独立解释器各写 3 行，父进程再写 1 行 ⇒ 序号严格 1..7。"""
    path = str(tmp_path / "audit.jsonl")
    root = str(Path(__file__).resolve().parent.parent)
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-c", _CROSS_PROCESS_CHILD, path, root],
            capture_output=True, text=True, timeout=180)
        assert completed.returncode == 0, completed.stderr

    gw.attest(b"parent-request", _route(),
              _stub_forwarder(gw.UpstreamReply(200, _completion_body())),
              gw.AuditLog(path))

    assert [row["seq"] for row in _records(path)] == [1, 2, 3, 4, 5, 6, 7]


def test_audit_seq_is_monotonic_under_concurrency(tmp_path):
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    route = _route()
    forward = _stub_forwarder(gw.UpstreamReply(200, _completion_body()))
    failures: list = []

    def worker(worker_id: int) -> None:
        try:
            for index in range(4):
                gw.attest(f"req-{worker_id}-{index}".encode(), route, forward, audit)
        except Exception as exc:  # pragma: no cover - 失败要显式冒出来
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not failures
    lines = _read_audit_lines(audit.path)
    assert len(lines) == 32                       # 一次调用一条，零丢失
    seqs = [json.loads(line)["seq"] for line in lines]
    assert sorted(seqs) == list(range(1, 33))     # 无重号、无缺口
    assert seqs == sorted(seqs)                   # 文件顺序即定号顺序（不乱序）
    assert len({json.loads(line)["request_sha256"] for line in lines}) == 32


def test_audit_heals_torn_last_line_without_rewriting_history(tmp_path):
    """硬 kill 留下的半行只被"补一个换行"封口，历史字节一个都不改。"""
    path = tmp_path / "audit.jsonl"
    torn = b'{"at": "2026-09-30T00:00:00Z", "decision": "iss'
    path.write_bytes(torn)
    audit = gw.AuditLog(str(path))
    audit.record(gw.DENIED, _route(), "a" * 64, None, upstream_id=None,
                 upstream_model=None, deny_reason="upstream_status_500")

    data = path.read_bytes()
    assert data.startswith(torn)                  # 历史字节未动（半行原样留着）
    lines = _read_audit_lines(path)
    assert len(lines) == 2
    assert lines[0] == torn
    # 读取侧口径：跳过不可解析行（硬 kill 残留），其后每行都逐行可解析。
    assert _parseable_records(path)[0]["seq"] == 1   # 半行里没有可继承的 seq ⇒ 从 1 起

    gw.attest(b"{}", _route(), _stub_forwarder(gw.UpstreamReply(200, _completion_body())),
              audit)
    assert [row["seq"] for row in _parseable_records(path)] == [1, 2]


# ── F. 上限口径：与消费侧 MAX_RESPONSE_BYTES 对齐，超限即拒发 ──────────────────
def test_response_cap_matches_consumer_cap():
    assert gw.MAX_UPSTREAM_RESPONSE_BYTES == runner.MAX_RESPONSE_BYTES == 512 * 1024


def test_upstream_response_cap_bounds_buffer_and_marks_incomplete(tmp_path):
    oversize = _completion_body(text="x" * (gw.MAX_UPSTREAM_RESPONSE_BYTES + 4096))
    up_server, _handler, url = _make_upstream(200, oversize)
    forward = gw.make_httpx_forwarder(_route(url))
    try:
        reply = forward(b'{"a":1}')
        assert reply.body_complete is False                  # 未完整读到 ⇒ 不可签发
        assert len(reply.body) <= gw.MAX_UPSTREAM_RESPONSE_BYTES
    finally:
        forward.close()
        _stop(up_server)

    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    result = gw.attest(b'{"a":1}', _route(), _stub_forwarder(reply), audit)
    assert result.status == 502 and result.headers == {}
    record = _records(audit.path)[0]
    assert record["deny_reason"] == "upstream_response_too_large"
    assert record["response_sha256"] is None                 # 不拿半个体冒充上游体


def test_oversize_upstream_response_denied_end_to_end(monkeypatch, tmp_path):
    oversize = _completion_body(text="x" * (gw.MAX_UPSTREAM_RESPONSE_BYTES + 4096))
    stack = _bring_up_stack(monkeypatch, tmp_path, body=oversize)
    try:
        status, headers, body = _post(stack["port"], _request_body())
        assert status == 502
        assert not [name for name in headers
                    if name.startswith(gw.ATTESTATION_HEADER_PREFIX)]
        assert json.loads(body)["error"]["reason"] == "upstream_response_too_large"
        records = _records(stack["audit"].path)
        assert len(records) == 1
        assert records[0]["decision"] == gw.DENIED      # 绝不"记 issued 却无收据"
    finally:
        stack["teardown"]()


# ── G. 转发器：连接池复用 + Bearer/identity 头 + close ────────────────────────
def test_httpx_forwarder_reuses_one_client(monkeypatch, tmp_path):
    up_server, up_handler, url = _make_upstream(200, _completion_body())
    created: list = []

    class CountingClient(httpx.Client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            created.append(self)

    monkeypatch.setattr(gw.httpx, "Client", CountingClient)
    forward = gw.make_httpx_forwarder(_route(url))
    try:
        assert forward(b'{"a":1}').status == 200
        assert forward(b'{"a":2}').status == 200
        assert len(created) == 1                  # 两次调用共用一个 client（连接池复用）
        assert up_handler.forwarded == [b'{"a":1}', b'{"a":2}']
    finally:
        forward.close()
        _stop(up_server)
    assert created[0].is_closed


def test_httpx_forwarder_sends_bearer_and_identity(monkeypatch, tmp_path):
    up_server, _handler, url = _make_upstream(200, _completion_body())
    route = _route(url)
    captured: dict = {}
    real_send = httpx.Client.send

    def spy(self, request, **kwargs):
        captured["headers"] = dict(request.headers)
        return real_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", spy, raising=False)
    forward = gw.make_httpx_forwarder(route)
    try:
        forward(b'{"a":1}')
    finally:
        forward.close()
        _stop(up_server)
    assert captured["headers"]["authorization"] == f"Bearer {_API_KEY}"
    assert captured["headers"]["accept-encoding"] == "identity"


# ── H. 上游 body 不是 JSON 对象 / id 缺失或非字符串（会审点名的缺口）────────────
@pytest.mark.parametrize("body", [b"[]", b'"s"', b"null", b"123", b"true",
                                  json.dumps(["a"]).encode()])
def test_denial_upstream_body_not_object(tmp_path, body):
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    result = gw.attest(b'{"x":1}', _route(), _stub_forwarder(gw.UpstreamReply(200, body)),
                       audit)

    assert result.status == 502 and result.headers == {}
    record = _records(audit.path)[0]
    assert record["deny_reason"] == "upstream_body_not_object"
    assert record["response_sha256"] == gw._sha256(body)
    assert record["upstream_id"] is None and record["upstream_model"] is None


@pytest.mark.parametrize("bad_id,expected_id", [
    ("", ""), ("   ", "   "),                        # 空白串：原样留证但拒发
    (None, None), (123, None), (1.5, None),          # 非字符串：一律不落账本
    (["up-1"], None), ({"a": 1}, None),
])
def test_denial_upstream_id_empty_or_non_string(tmp_path, bad_id, expected_id):
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    body = json.dumps({"id": bad_id, "model": UPSTREAM_MODEL,
                       "choices": []}, ensure_ascii=False).encode("utf-8")
    result = gw.attest(b'{"x":1}', _route(), _stub_forwarder(gw.UpstreamReply(200, body)),
                       audit)

    assert result.status == 502 and result.headers == {}
    record = _records(audit.path)[0]
    assert record["deny_reason"] == "upstream_missing_id"
    # 只留可核验的字符串身份；非字符串 id 一律记 null。
    assert record["upstream_id"] == expected_id
    assert record["response_sha256"] == gw._sha256(body)


# ── I. Route 脱敏：repr/str/异常展开都不含密钥 ────────────────────────────────
def test_route_repr_hides_api_key():
    route = _route(api_key="sk-DO-NOT-LEAK-0123456789")
    text = repr(route)

    assert "sk-DO-NOT-LEAK-0123456789" not in text
    assert "api_key" not in text
    assert str(route) == text                    # dataclass 不定义 __str__ ⇒ 走 repr
    assert f"{route}" == text
    assert PROVIDER in text                      # 非密字段照常可见（脱敏不等于隐身）
    assert "sk-DO-NOT-LEAK-0123456789" not in json.dumps(route.identity())
    with pytest.raises(RuntimeError) as excinfo:
        raise RuntimeError(f"route={route!r}")   # 异常/日志展开的典型形态
    assert "sk-DO-NOT-LEAK-0123456789" not in str(excinfo.value)


def test_api_key_never_leaves_gateway(tmp_path):
    """回包/审计全链路不得出现密钥（含拒发与转发异常路径）。"""
    key = "sk-DO-NOT-LEAK-0123456789"
    route = _route(api_key=key)
    audit = gw.AuditLog(str(tmp_path / "audit.jsonl"))
    cases = [
        gw.UpstreamReply(200, _completion_body()),
        gw.UpstreamReply(500, b"boom"),
        gw.UpstreamReply(200, b"[]"),
        gw.UpstreamReply(200, b"not json"),
        gw.UpstreamReply(200, _completion_body(upstream_id="a\r\nB: c")),
    ]
    for reply in cases:
        result = gw.attest(b'{"k":1}', route, _stub_forwarder(reply), audit)
        assert key not in result.body.decode("utf-8")
        assert key not in json.dumps(result.headers)

    def forward(request_body):
        raise httpx.ConnectError("synthetic")

    result = gw.attest(b'{"k":2}', route, forward, audit)
    assert result.status == 502 and key not in result.body.decode("utf-8")

    text = (tmp_path / "audit.jsonl").read_text("utf-8")
    assert key not in text
    assert len(_records(audit.path)) == len(cases) + 1


# ── J. route_from_env / gateway_from_env / listen_from_env 成功与失败路径 ──────
def test_route_from_env_success_path(tmp_path):
    route = gw.route_from_env(_gateway_env(tmp_path))
    assert route.base_url == "https://upstream.invalid/v1"
    assert route.api_key == _API_KEY
    assert route.identity() == {"provider": PROVIDER, "model": UPSTREAM_MODEL,
                                "channel_id": CHANNEL_ID}
    assert route.unsafe_header_fields() == []


@pytest.mark.parametrize("missing_key", sorted(_ALL_ENV_KEYS[:5]))
def test_route_from_env_missing_key_is_fail_closed(tmp_path, missing_key):
    env = _gateway_env(tmp_path)
    env.pop(missing_key)
    with pytest.raises(gw.GatewayStartupError) as excinfo:
        gw.route_from_env(env)
    assert missing_key in str(excinfo.value)
    assert "attestation_gateway_env_missing" in str(excinfo.value)


def test_route_from_env_blank_value_is_fail_closed(tmp_path):
    env = _gateway_env(tmp_path, LG_ATTEST_ROUTE_MODEL="   ")
    with pytest.raises(gw.GatewayStartupError, match="LG_ATTEST_ROUTE_MODEL"):
        gw.route_from_env(env)
    env = _gateway_env(tmp_path, LG_ATTEST_UPSTREAM_API_KEY="   ")
    with pytest.raises(gw.GatewayStartupError, match="LG_ATTEST_UPSTREAM_API_KEY"):
        gw.route_from_env(env)


def test_route_from_env_keeps_api_key_verbatim(tmp_path):
    """密钥含合法首尾空白时不得被静默改写（否则上游 401 且难定位）。"""
    env = _gateway_env(tmp_path, LG_ATTEST_UPSTREAM_API_KEY="  sk-padded  ")
    assert gw.route_from_env(env).api_key == "  sk-padded  "


@pytest.mark.parametrize("base_url,expected_reason", [
    ("http://upstream.invalid/v1", "base_url_http_non_loopback"),
    ("ftp://upstream.invalid/v1", "base_url_scheme_untrusted"),
    ("https://user:pass@upstream.invalid/v1", "base_url_userinfo_forbidden"),
    ("https://upstream.invalid/v1?a=1", "base_url_query_fragment_forbidden"),
    ("https:///v1", "base_url_host_missing"),
])
def test_route_from_env_rejects_untrusted_base_url(tmp_path, base_url, expected_reason):
    env = _gateway_env(tmp_path, LG_ATTEST_UPSTREAM_BASE_URL=base_url)
    with pytest.raises(gw.GatewayStartupError) as excinfo:
        gw.route_from_env(env)
    assert expected_reason in str(excinfo.value)


def test_route_from_env_accepts_https_and_loopback_http(tmp_path):
    assert gw.route_from_env(_gateway_env(tmp_path)).base_url.startswith("https://")
    env = _gateway_env(tmp_path, LG_ATTEST_UPSTREAM_BASE_URL="http://127.0.0.1:9/v1")
    assert gw.route_from_env(env).base_url == "http://127.0.0.1:9/v1"


def test_route_from_env_rejects_unsafe_route_identity(tmp_path):
    env = _gateway_env(tmp_path, LG_ATTEST_ROUTE_PROVIDER="provider-a\r\nX: 1")
    with pytest.raises(gw.GatewayStartupError) as excinfo:
        gw.route_from_env(env)
    assert "attestation_gateway_route_header_unsafe" in str(excinfo.value)
    assert "provider" in str(excinfo.value)


def test_gateway_from_env_success_and_missing_audit(tmp_path):
    gateway = gw.gateway_from_env(_gateway_env(tmp_path))
    try:
        assert isinstance(gateway, gw.AttestationGateway)
        assert isinstance(gateway.audit, gw.AuditLog)
        assert gateway.audit.path == str(tmp_path / "audit.jsonl")
        assert gateway.route.base_url == "https://upstream.invalid/v1"
    finally:
        gateway.close()

    env = _gateway_env(tmp_path, LG_ATTEST_AUDIT_PATH="")
    with pytest.raises(gw.GatewayStartupError, match="LG_ATTEST_AUDIT_PATH"):
        gw.gateway_from_env(env)


@pytest.mark.parametrize("env,expected", [
    ({}, ("127.0.0.1", 8080)),
    ({"LG_ATTEST_LISTEN_HOST": " 127.0.0.5 ", "LG_ATTEST_LISTEN_PORT": " 9001 "},
     ("127.0.0.5", 9001)),
    ({"LG_ATTEST_LISTEN_PORT": "0"}, ("127.0.0.1", 0)),
])
def test_listen_from_env_success_path(env, expected):
    assert gw.listen_from_env(env) == expected


@pytest.mark.parametrize("env,expected_reason", [
    ({"LG_ATTEST_LISTEN_PORT": "eighty"}, "listen_port_invalid"),
    ({"LG_ATTEST_LISTEN_PORT": "80.5"}, "listen_port_invalid"),
    ({"LG_ATTEST_LISTEN_PORT": "70000"}, "listen_port_out_of_range"),
    ({"LG_ATTEST_LISTEN_PORT": "-1"}, "listen_port_out_of_range"),
])
def test_listen_from_env_rejects_bad_port(env, expected_reason):
    with pytest.raises(gw.GatewayStartupError) as excinfo:
        gw.listen_from_env(env)
    assert expected_reason in str(excinfo.value)


# ── K. main()：失败给单行原因 + 非零退出（不裸 traceback），成功走完生命周期 ────
@pytest.mark.parametrize("overrides,expected_reason", [
    ({"LG_ATTEST_ROUTE_MODEL": ""}, "attestation_gateway_env_missing"),
    ({"LG_ATTEST_LISTEN_PORT": "eighty"}, "attestation_gateway_listen_port_invalid"),
    ({"LG_ATTEST_LISTEN_PORT": "70000"}, "attestation_gateway_listen_port_out_of_range"),
    ({"LG_ATTEST_UPSTREAM_BASE_URL": "http://upstream.invalid/v1"},
     "attestation_gateway_base_url_http_non_loopback"),
])
def test_main_startup_failures_return_nonzero_without_traceback(monkeypatch, tmp_path,
                                                               capsys, overrides,
                                                               expected_reason):
    _apply_env(monkeypatch, _gateway_env(tmp_path, **overrides))

    def forbidden(*args, **kwargs):  # 失败路径绝不建服务（更不绑监听口）
        raise AssertionError("main() 失败路径不应构造服务")

    monkeypatch.setattr(gw, "make_server", forbidden)

    assert gw.main([]) == 2
    captured = capsys.readouterr()
    assert "attestation_gateway_startup_failed:" + expected_reason in captured.err
    assert "Traceback" not in captured.err and "Traceback" not in captured.out


def test_main_refuses_non_loopback_listen_without_token(monkeypatch, tmp_path, capsys):
    _apply_env(monkeypatch, _gateway_env(tmp_path, **{gw.LISTEN_HOST_ENV: "0.0.0.0",
                                                      gw.LISTEN_PORT_ENV: "8080"}))

    def forbidden(*args, **kwargs):
        raise AssertionError("鉴权不满足时绝不允许开监听口")

    monkeypatch.setattr(gw, "ThreadingHTTPServer", forbidden)

    assert gw.main([]) == 2
    captured = capsys.readouterr()
    assert "inbound_token_required_for_non_loopback_listen" in captured.err
    assert gw.INBOUND_TOKEN_ENV in captured.err
    assert "Traceback" not in captured.err


def test_main_success_path_serves_and_closes(monkeypatch, tmp_path, capsys):
    _apply_env(monkeypatch, _gateway_env(
        tmp_path, **{gw.LISTEN_HOST_ENV: "127.0.0.1", gw.LISTEN_PORT_ENV: "9123",
                     gw.INBOUND_TOKEN_ENV: INBOUND_TOKEN}))
    calls: dict = {}

    class FakeServer:
        def serve_forever(self):
            calls["serve_forever"] = True

        def shutdown(self):
            calls["shutdown"] = True

        def server_close(self):
            calls["server_close"] = True

    def fake_make_server(gateway, host, port, *, inbound_token=""):
        calls["host"], calls["port"] = host, port
        calls["inbound_token"] = inbound_token
        calls["route"] = gateway.route
        return FakeServer()

    monkeypatch.setattr(gw, "make_server", fake_make_server)

    assert gw.main([]) == 0
    assert calls["host"] == "127.0.0.1" and calls["port"] == 9123
    assert calls["inbound_token"] == INBOUND_TOKEN
    assert calls["route"].base_url == "https://upstream.invalid/v1"
    assert calls["serve_forever"] and calls["shutdown"] and calls["server_close"]
    assert "Traceback" not in capsys.readouterr().err
