"""K2 可信证明网关（生成侧）—— lg-k2-attestation-gateway.

背景：`app/semantic_review_runner.py` 是消费侧，只*读取*并校验
`x-lg-upstream-provider / -model / -channel-id / -request-id` 四个证明头
（见 `semantic_review_runner.py:176-208`）。缺任一头，即使正文写 PASS 也不生成
语义票。本模块补齐**生成侧**：把 chat/completions 原样转发到**单条预先核准**的
OpenAI 兼容上游路由，并按上游响应体的 `id`/`model` 追加可审计的证明头，同时落一条
append-only 审计记录。

信任模型（详见 docs/attestation_gateway_20260930.md）：证明头不是上游给的，
而是本网关在捕获上游响应体 `id`/`model` 后**自己生成并逐条记账**——头里的
`request-id` 恒等于上游体 `id`、`model` 恒等于上游体 `model`，且与审计记录中同一
次调用的 SHA256 绑定；消费侧再拿这四个头与"预先核准路由 + 响应体"三方对账。

硬约束（任一不满足 ⇒ 不回任何证明头、返回 502、审计记「拒发」）：
  - 上游 HTTP 状态非 200；
  - 上游响应体不是 JSON 对象，或缺 `id` / 缺 `model`（或 `id` 非非空字符串）；
  - 上游响应体 `model` 与核准路由不一致。

边界：本模块**不写真实库**、**不调用真实模型**（测试全用合成上游）、**不改判据**
（不改 `app/semantic_review_runner.py`）。依赖仅标准库 + 仓库既有 httpx。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import httpx

# 消费侧读取的四个证明头，小写规范形式；与 _ATTESTATION_FIELDS 一一对应。
ATTESTATION_HEADER_PREFIX = "x-lg-upstream-"
ATTESTATION_FIELDS = ("provider", "model", "channel-id", "request-id")

# 拒发时统一对外返回的状态码（不回任何证明头）。
DENY_STATUS = 502

ISSUED = "issued"    # 发放
DENIED = "denied"    # 拒发

# 转发缓冲上限：防御性护栏，非本任务判据；消费侧另有 512KiB 上限。
MAX_FORWARD_BYTES = 8 * 1024 * 1024


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Route:
    """单条预先核准的上游路由身份 + 访问凭据（api_key 绝不落审计/绝不出头）。"""

    base_url: str
    api_key: str
    provider: str
    model: str          # 核准的上游 model：证明头 model 与体 model 都必须等于它
    channel_id: str

    def identity(self) -> dict:
        """可用于审计的"路由身份"，刻意不含 api_key。"""
        return {"provider": self.provider, "model": self.model,
                "channel_id": self.channel_id}


@dataclass
class UpstreamReply:
    """一次上游往返的原始结果（体按原样字节回传，绝不重编码）。"""

    status: int
    body: bytes


@dataclass
class GatewayResponse:
    status: int
    headers: dict
    body: bytes


# 上游转发器契约：把请求体字节转发到核准路由，返回原始 status + 体字节。
Forwarder = Callable[[bytes], UpstreamReply]


class AuditLog:
    """append-only JSONL：只追加、不覆盖、不重写历史行；进程内写锁防并发交错。"""

    def __init__(self, path: str):
        self.path = os.fspath(path)
        self._lock = threading.Lock()

    def record(self, decision: str, route: Route, request_sha256: str,
               response_sha256: str, *, upstream_id: str | None,
               upstream_model: str | None, deny_reason: str | None = None) -> None:
        entry = {
            "at": _now_utc(),
            "decision": decision,                       # issued | denied
            "route": route.identity(),                  # provider/model/channel_id
            "upstream_id": upstream_id,
            "upstream_model": upstream_model,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
        }
        if deny_reason is not None:
            entry["deny_reason"] = deny_reason
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        raw = line.encode("utf-8")
        parent = os.path.dirname(os.path.abspath(self.path))
        with self._lock:
            os.makedirs(parent, exist_ok=True)
            # "ab" = 二进制只追加；绝不 truncate、绝不 seek 回写历史行。
            with open(self.path, "ab") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())


def _decide(reply: UpstreamReply, route: Route):
    """返回 (issue: bool, upstream_id, upstream_model, deny_reason)。

    只有当上游 200、体是可解析 JSON 对象、且 `id`/`model` 齐备且 model 与
    核准路由一致时，才允许发放证明头。否则一律拒发。
    """
    if reply.status != 200:
        return (False, None, None, f"upstream_status_{reply.status}")
    try:
        parsed = json.loads(reply.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return (False, None, None, "upstream_body_not_json")
    if not isinstance(parsed, dict):
        return (False, None, None, "upstream_body_not_object")
    upstream_id = parsed.get("id")
    upstream_model = parsed.get("model")
    if not isinstance(upstream_id, str) or not upstream_id.strip():
        return (False, upstream_id, upstream_model, "upstream_missing_id")
    if not isinstance(upstream_model, str) or not upstream_model:
        return (False, upstream_id, upstream_model, "upstream_missing_model")
    if upstream_model != route.model:
        return (False, upstream_id, upstream_model, "upstream_model_route_mismatch")
    return (True, upstream_id, upstream_model, None)


def attest(request_body: bytes, route: Route, forward: Forwarder,
           audit: AuditLog) -> GatewayResponse:
    """核心逻辑：转发 → 捕获上游 id/model → 发放/拒发 + 记账。纯函数，便于单测。"""
    reply = forward(request_body)
    request_sha = _sha256(request_body)
    response_sha = _sha256(reply.body)
    issue, upstream_id, upstream_model, deny_reason = _decide(reply, route)

    if not issue:
        # 拒发：不回任何证明头，统一 502；审计记「拒发」。
        audit.record(DENIED, route, request_sha, response_sha,
                     upstream_id=upstream_id if isinstance(upstream_id, str) else None,
                     upstream_model=upstream_model if isinstance(upstream_model, str) else None,
                     deny_reason=deny_reason)
        error_body = json.dumps(
            {"error": {"type": "k2_attestation_denied", "reason": deny_reason}},
            ensure_ascii=False, sort_keys=True).encode("utf-8")
        return GatewayResponse(DENY_STATUS, {}, error_body)

    # 发放：证明头由本网关生成，request-id 恒等体 id、model 恒等体 model。
    headers = {
        ATTESTATION_HEADER_PREFIX + "provider": route.provider,
        ATTESTATION_HEADER_PREFIX + "model": upstream_model,
        ATTESTATION_HEADER_PREFIX + "channel-id": route.channel_id,
        ATTESTATION_HEADER_PREFIX + "request-id": upstream_id,
    }
    audit.record(ISSUED, route, request_sha, response_sha,
                 upstream_id=upstream_id, upstream_model=upstream_model)
    return GatewayResponse(200, headers, reply.body)


class AttestationGateway:
    """把 route + 转发器 + 审计日志粘在一起的可调用网关对象。"""

    def __init__(self, route: Route, forward: Forwarder, audit: AuditLog):
        self.route = route
        self.forward = forward
        self.audit = audit

    def handle(self, request_body: bytes) -> GatewayResponse:
        return attest(request_body, self.route, self.forward, self.audit)


def make_httpx_forwarder(route: Route, timeout: float = 120.0) -> Forwarder:
    """生产用转发器：把请求体原样 POST 到核准路由（OpenAI 兼容）。测试不用它。"""
    url = route.base_url.rstrip("/") + "/chat/completions"

    def forward(request_body: bytes) -> UpstreamReply:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, content=request_body, headers={
                "Authorization": f"Bearer {route.api_key}",
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            })
            return UpstreamReply(resp.status_code, resp.content)

    return forward


def _make_handler(gateway: AttestationGateway) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LG-Attestation-Gateway/1.0"

        def _reply(self, status: int, headers: dict, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # 明确关连接：每次请求后即断开，避免 keep-alive 线程在 shutdown 时滞留。
            self.send_header("Connection", "close")
            # 刻意**不发** Content-Encoding（消费侧要求 identity），也**不发**证明头
            # 除非 headers 里确实有（拒发时 headers 为空 ⇒ 零证明头）。
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib 约定名
            if not self.path.endswith("/chat/completions"):
                self._reply(404, {}, b'{"error":{"type":"not_found"}}')
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._reply(400, {}, b'{"error":{"type":"bad_content_length"}}')
                return
            if length < 0 or length > MAX_FORWARD_BYTES:
                self._reply(413, {}, b'{"error":{"type":"request_too_large"}}')
                return
            request_body = self.rfile.read(length) if length else b""
            result = gateway.handle(request_body)
            self._reply(result.status, result.headers, result.body)
            self.close_connection = True

        def log_message(self, *args) -> None:  # 静音默认 stderr 访问日志
            return

    return Handler


def make_server(gateway: AttestationGateway, host: str = "127.0.0.1",
                port: int = 0) -> ThreadingHTTPServer:
    """构造（已绑定端口的）HTTP 服务；调用方负责 serve_forever/shutdown。

    port=0 时由内核分配临时端口，实际端口在 `server.server_address[1]`。
    """
    return ThreadingHTTPServer((host, port), _make_handler(gateway))


_ENV_KEYS = {
    "base_url": "LG_ATTEST_UPSTREAM_BASE_URL",
    "api_key": "LG_ATTEST_UPSTREAM_API_KEY",
    "provider": "LG_ATTEST_ROUTE_PROVIDER",
    "model": "LG_ATTEST_ROUTE_MODEL",
    "channel_id": "LG_ATTEST_ROUTE_CHANNEL_ID",
    "audit": "LG_ATTEST_AUDIT_PATH",
}


def route_from_env(environ: dict | None = None) -> Route:
    """由环境变量给出核准路由（base_url/api_key/路由身份）。缺任一项即拒绝启动。"""
    env = os.environ if environ is None else environ
    missing = sorted(name for key, name in _ENV_KEYS.items()
                     if key != "audit" and not (env.get(name) or "").strip())
    if missing:
        raise RuntimeError("attestation_gateway_env_missing:" + ",".join(missing))
    return Route(
        base_url=env[_ENV_KEYS["base_url"]].strip(),
        api_key=env[_ENV_KEYS["api_key"]].strip(),
        provider=env[_ENV_KEYS["provider"]].strip(),
        model=env[_ENV_KEYS["model"]].strip(),
        channel_id=env[_ENV_KEYS["channel_id"]].strip(),
    )


def gateway_from_env(environ: dict | None = None) -> AttestationGateway:
    env = os.environ if environ is None else environ
    route = route_from_env(env)
    audit_path = (env.get(_ENV_KEYS["audit"]) or "").strip()
    if not audit_path:
        raise RuntimeError("attestation_gateway_env_missing:" + _ENV_KEYS["audit"])
    return AttestationGateway(route, make_httpx_forwarder(route), AuditLog(audit_path))


def main(argv=None) -> int:
    """生产入口：读环境变量，起服务并常驻。本任务不运行它（测试用合成上游）。"""
    gateway = gateway_from_env()
    env = os.environ
    host = (env.get("LG_ATTEST_LISTEN_HOST") or "127.0.0.1").strip()
    port = int((env.get("LG_ATTEST_LISTEN_PORT") or "8080").strip())
    server = make_server(gateway, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
