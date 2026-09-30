"""K2 可信证明网关测试 —— 全合成上游，**零真实模型调用、不落真实库**。

覆盖任务书的五条：正常发放、三类拒发（缺 id / model 不符 / 上游 5xx 等）、
append-only 审计、端到端（起本网关后用 `semantic_review_runner._post_once` +
`_parse_response` 产出合格 review）、以及核心反向自检（网关少发一个头 ⇒
`_parse_response` 必须抛 `ReviewResponseError`）。
"""
from __future__ import annotations

import json
import threading

import http.server
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

_REVIEW = {"verdict": "PASS", "reason": "逐条核过本卡实例及反例",
           "cited_instance_ids": ["SI-A"], "concerns": []}
_COMPLETION_TEXT = json.dumps(_REVIEW, ensure_ascii=False)


def _completion_body(*, upstream_id=UPSTREAM_ID, model=UPSTREAM_MODEL,
                     text=_COMPLETION_TEXT, finish="stop") -> bytes:
    """OpenAI 风格完成体：runner 的 _parse_response 只认这一形状。"""
    return json.dumps({
        "id": upstream_id, "model": model,
        "choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": text}}],
    }, ensure_ascii=False).encode("utf-8")


def _route(base_url="http://upstream.invalid/v1") -> gw.Route:
    return gw.Route(base_url, "sk-synthetic", PROVIDER, UPSTREAM_MODEL, CHANNEL_ID)


def _review_route() -> runner.ReviewRoute:
    return runner.ReviewRoute(REQUESTED_MODEL, PROVIDER, UPSTREAM_MODEL, CHANNEL_ID)


def _stub_forwarder(reply: gw.UpstreamReply):
    return lambda request_body: reply


def _read_audit_lines(path):
    with open(path, "rb") as handle:
        return [ln for ln in handle.read().split(b"\n") if ln]


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
                    drop_headers=()):
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

    gw_server = gw.make_server(gateway, "127.0.0.1", 0)
    threading.Thread(target=gw_server.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "GATEWAY_BASE_URL",
                        f"http://127.0.0.1:{gw_server.server_address[1]}/v1")
    monkeypatch.setattr(config, "GATEWAY_API_KEY", "synthetic-key")

    def teardown():
        _stop(gw_server)
        _stop(up_server)

    return {"up_server": up_server, "up_handler": up_handler, "gw_server": gw_server,
            "route": route, "audit": audit, "gateway": gateway, "teardown": teardown}


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
        records = [json.loads(ln) for ln in _read_audit_lines(stack["audit"].path)]
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
        # 贴原文（真跑可见）：绕过 pytest 捕获，直写真实 stdout，配合 -s 呈现。
        import sys
        sys.__stdout__.write(
            f"[REVERSE-CHECK] {type(excinfo.value).__module__}."
            f"{type(excinfo.value).__name__}: {excinfo.value}"
            f" | __cause__: {type(excinfo.value.__cause__).__name__}: "
            f"{excinfo.value.__cause__}\n")
        sys.__stdout__.flush()
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
        records = [json.loads(ln) for ln in _read_audit_lines(stack["audit"].path)]
        assert len(records) == 1 and records[0]["decision"] == gw.DENIED
    finally:
        stack["teardown"]()
