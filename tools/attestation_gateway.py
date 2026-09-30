"""K2 可信证明网关（生成侧）—— lg-k2-attestation-gateway（2026-09-30 加固版）。

背景：`app/semantic_review_runner.py` 是消费侧，只*读取*并校验
`x-lg-upstream-provider / -model / -channel-id / -request-id` 四个证明头
（见 `semantic_review_runner.py:176-181`）。缺任一头，即使正文写 PASS 也不生成
语义票。本模块补齐**生成侧**：把 chat/completions 原样转发到**单条预先核准**的
OpenAI 兼容上游路由，并按上游响应体的 `id`/`model` 追加可审计的证明头，同时落一条
append-only 审计记录。

信任模型（详见 docs/attestation_gateway_20260930.md）：证明头不是上游给的，
而是本网关在捕获上游响应体 `id`/`model` 后**自己生成并逐条记账**——头里的
`request-id` 恒等于上游体 `id`、`model` 恒等于上游体 `model`，且与审计记录中同一
次调用的 SHA256 + 单调序号 `seq` 绑定；消费侧再拿这四个头与"预先核准路由 + 响应体"
三方对账。

硬约束（任一不满足 ⇒ 不回任何证明头、返回 502、审计记「拒发」）：
  - 上游 HTTP 状态非 200；
  - 上游响应体超上限、被读截断（未完整读到）；
  - 上游响应体不是 JSON 对象，或缺 `id` / 缺 `model`（或 `id` 非非空字符串）；
  - `id` / `model` / 路由身份过不了证明头**字符白名单**（挡 CR/LF 头注入）；
  - 上游响应体 `model` 与核准路由不一致。

入站侧（会审 BLOCK 项已收口）：
  - **鉴权 fail-closed**：非回环监听且未配 `LG_ATTEST_INBOUND_TOKEN` ⇒ 拒绝启动；
    配了令牌则校验 `Authorization: Bearer <token>`，不匹配回 401 且不发任何证明头；
  - 协议层硬门：`Transfer-Encoding` / 缺失或非法 `Content-Length` ⇒ 400；超限 413；
    读超时 408；声明长度与实读不符 400——**一律不转发**；
  - `do_POST` 全路径兜异常：转发器异常 ⇒ 502 + 审计拒发行（绝无零记录）。

边界：本模块**不写真实库**、**不调用真实模型**（测试全用合成上游）、**不改判据**
（不改 `app/semantic_review_runner.py`）。依赖仅标准库 + 仓库既有 httpx。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit

import httpx

# 消费侧读取的四个证明头，小写规范形式；与 _ATTESTATION_FIELDS 一一对应。
ATTESTATION_HEADER_PREFIX = "x-lg-upstream-"
ATTESTATION_FIELDS = ("provider", "model", "channel-id", "request-id")

# 拒发时统一对外返回的状态码（不回任何证明头）。
DENY_STATUS = 502

ISSUED = "issued"    # 发放
DENIED = "denied"    # 拒发

# 证明头值白名单（回环 1 段 + 收尾判据在 fullmatch 上）：
# 只放行 [A-Za-z0-9._:-] 且长度 1..200 ⇒ CR/LF/空格/控制字符/超长值一律走拒发。
# 注意**不用 `$`**：它会放过结尾的 \n；fullmatch 不会。
HEADER_VALUE_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")

# 入站请求体上限：网关自带的防御性护栏（消费侧不限制请求体大小，故口径不同，
# 见文档 §9：只有**响应侧**上限必须与消费侧 MAX_RESPONSE_BYTES 对齐）。
MAX_FORWARD_BYTES = 8 * 1024 * 1024
# 上游响应体上限：与消费侧 app/semantic_review_runner.py:45 的
# MAX_RESPONSE_BYTES = 512KiB **同口径**——否则会出现「账本记 issued、收据根本
# 不会被消费侧接受」的口径错位。超限即拒发，且不缓冲超限部分（内存有界）。
MAX_UPSTREAM_RESPONSE_BYTES = 512 * 1024

# 仅当监听主机就是这些名字时才算回环；其余主机名（不解析 DNS，无法证明是回环）
# 一律按非回环处理 ⇒ 要求入站令牌。fail-closed。
LOOPBACK_HOSTNAMES = frozenset({"localhost"})

# handler socket 单次操作读超时（秒）：慢客户端不能无限占住工作线程。
HANDLER_SOCKET_TIMEOUT = 30.0

# Content-Length 严格形态：只收十进制数字（int() 会接受 "5_0"/"+5" 这类怪值）。
_DECIMAL_RE = re.compile(r"[0-9]{1,10}")


class GatewayStartupError(RuntimeError):
    """启动/配置期 fail-closed 错误。

    `main()` 把它转成「单行原因 + 非零退出码」，绝不让裸 traceback 泄到部署面。
    继承 `RuntimeError` 是为了与既有 `route_from_env` 的失败风格一致。
    """


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _log_stderr(message: str) -> None:
    """stderr 单行（不落审计、不含任何密钥）。"""
    print(message, file=sys.stderr, flush=True)


def header_value_ok(value) -> bool:
    """证明头值白名单判定：类型必须是 str 且完全匹配 `HEADER_VALUE_RE`。"""
    return isinstance(value, str) and HEADER_VALUE_RE.fullmatch(value) is not None


def is_loopback_host(host) -> bool:
    """字面量回环判定（不解析 DNS ⇒ 无法证明的一律 False ⇒ fail-closed）。"""
    candidate = (host or "").strip().strip("[]").lower()
    if not candidate:
        return False
    if candidate in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Route:
    """单条预先核准的上游路由身份 + 访问凭据（api_key 绝不落审计/绝不出头）。"""

    base_url: str
    # repr=False：密钥绝不出现在 __repr__ —— repr 会被异常/日志/traceback 局部变量
    # 打印与 pytest 断言展开带出去，identity() 的刻意脱敏会被 repr 绕过。
    api_key: str = field(repr=False)
    provider: str
    model: str          # 核准的上游 model：证明头 model 与体 model 都必须等于它
    channel_id: str

    def identity(self) -> dict:
        """可用于审计的"路由身份"，刻意不含 api_key。"""
        return {"provider": self.provider, "model": self.model,
                "channel_id": self.channel_id}

    def unsafe_header_fields(self) -> list[str]:
        """本路由里会落进证明头、但过不了白名单的字段名（空列表 ⇒ 可发放）。"""
        return sorted(name for name, value in
                      (("provider", self.provider), ("model", self.model),
                       ("channel_id", self.channel_id))
                      if not header_value_ok(value))


@dataclass
class UpstreamReply:
    """一次上游往返的原始结果（体按原样字节回传，绝不重编码）。

    `body_complete=False` 表示体超 MAX_UPSTREAM_RESPONSE_BYTES 被主动截断：
    此时 `body` 只是已读前缀，**不可**当上游原体（不许据此签发/算 SHA）。
    """

    status: int
    body: bytes
    body_complete: bool = True


@dataclass
class GatewayResponse:
    status: int
    headers: dict
    body: bytes


# 上游转发器契约：把请求体字节转发到核准路由，返回原始 status + 体字节。
Forwarder = Callable[[bytes], UpstreamReply]


# ── 跨进程互斥：审计序号不许重号 ────────────────────────────────────────────
# 序号必须跨进程唯一：进程内 threading.Lock 挡不住第二个进程写同一个账本，故再加
# 一层操作系统文件锁（Windows msvcrt.locking / POSIX fcntl.flock，advisory 锁，
# 覆盖「扫账本定号 + 追加落盘」整个临界区）。
if os.name == "nt":  # pragma: no cover - 平台分支
    import msvcrt

    def _lock_exclusive(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:  # pragma: no cover - 平台分支
    import fcntl

    def _lock_exclusive(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: str) -> threading.Lock:
    """同路径共用一把进程内锁：多实例写同一账本时不靠运气。"""
    key = os.path.abspath(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = _PATH_LOCKS[key] = threading.Lock()
        return lock


class AuditLog:
    """append-only JSONL：只追加、不覆盖、不重写历史行。

    每行带**单调序号 `seq`**：进程内绝不重号、跨进程（文件锁）绝不重号、重启后
    从账本尾部接号不回退。`seq` 与 `request_sha256` 一起把「这一次入站请求 ↔
    这一组证明头 ↔ 这一条账本行」绑成可寻址的一对一关系（判定方法见文档 §3）。
    """

    def __init__(self, path: str):
        self.path = os.fspath(path)
        self._lock = _path_lock(self.path)
        self._next_seq = 1
        self._known_size = 0    # 已把 [0, _known_size) 内的所有 seq 计入 _next_seq

    def record(self, decision: str, route: Route, request_sha256: str,
               response_sha256: str | None, *, upstream_id: str | None,
               upstream_model: str | None, deny_reason: str | None = None,
               detail: str | None = None) -> None:
        """追加一行。`response_sha256=None` 表示「压根没拿到完整上游体」。"""
        with self._lock:
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
            # "a+b" = 只追加（O_APPEND：并发写的原子追加）+ 可读（定号要扫账本尾）。
            with open(self.path, "a+b") as handle:
                _lock_exclusive(handle)
                try:
                    seq = self._reserve_seq(handle)
                    entry = {
                        "seq": seq,
                        "at": _now_utc(),
                        "decision": decision,           # issued | denied
                        "route": route.identity(),      # provider/model/channel_id
                        "upstream_id": upstream_id,
                        "upstream_model": upstream_model,
                        "request_sha256": request_sha256,
                        "response_sha256": response_sha256,
                    }
                    if deny_reason is not None:
                        entry["deny_reason"] = deny_reason
                    if detail is not None:
                        entry["detail"] = detail
                    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
                    # 绝不 truncate、绝不 seek 回写历史行（seek 只用于读账本尾部）。
                    handle.write(line.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                    # 追加后当前位置 = 本行末尾；此前的字节都已计入 _next_seq。
                    self._known_size = handle.tell()
                finally:
                    _unlock(handle)

    def _reserve_seq(self, handle) -> int:
        """在文件锁内定下一个不复用的序号。"""
        handle.seek(0, os.SEEK_END)
        if handle.tell() > self._known_size:
            # append-only ⇒ 历史行永不变 ⇒ 只需扫「上次记账之后新追加的字节」。
            handle.seek(self._known_size)
            tail = handle.read()
            highest = 0
            for raw in tail.split(b"\n"):
                if not raw.strip():
                    continue
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    # 硬 kill 残留的半行 / 外来写入：跳过定号，绝不改写它。
                    continue
                value = parsed.get("seq") if isinstance(parsed, dict) else None
                if isinstance(value, int) and not isinstance(value, bool):
                    highest = max(highest, value)
            if highest >= self._next_seq:
                self._next_seq = highest + 1
            if tail and not tail.endswith(b"\n"):
                # 硬 kill 会在 write 与 flush 之间留下无换行的半行 JSONL；不给它
                # 封口的话，后面每一行都会被粘到它后面、整段不可解析。这里**只追加**
                # 一个换行（不改写任何既有字节），让账本从下一行起恢复逐行可解析。
                handle.write(b"\n")
        seq = self._next_seq
        self._next_seq = seq + 1
        return seq


def _decide(reply: UpstreamReply, route: Route):
    """返回 (issue: bool, upstream_id, upstream_model, deny_reason)。

    判据优先级（deny_reason 取第一个命中的）：状态 → 体完整性 → JSON → JSON 对象 →
    id 存在 → id 字符白名单 → model 存在 → model 字符白名单 → model 与路由一致。
    字符白名单必须在"回写响应头"之前过，且排在 model 相等之前：脏 id 即便 model
    正确也绝不出话。
    """
    if reply.status != 200:
        return (False, None, None, f"upstream_status_{reply.status}")
    if not reply.body_complete:
        return (False, None, None, "upstream_response_too_large")
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
    if not header_value_ok(upstream_id):
        return (False, upstream_id, upstream_model, "upstream_id_invalid_chars")
    if not isinstance(upstream_model, str) or not upstream_model.strip():
        return (False, upstream_id, upstream_model, "upstream_missing_model")
    if not header_value_ok(upstream_model):
        return (False, upstream_id, upstream_model, "upstream_model_invalid_chars")
    if upstream_model != route.model:
        return (False, upstream_id, upstream_model, "upstream_model_route_mismatch")
    return (True, upstream_id, upstream_model, None)


def _record_denial(audit: AuditLog, route: Route, request_sha: str,
                   response_sha: str | None, upstream_id, upstream_model,
                   deny_reason: str, detail: str | None = None) -> None:
    """记一条拒发：上游字段只收 str（不是 str 就当"没拿到身份"）。"""
    audit.record(DENIED, route, request_sha, response_sha,
                 upstream_id=upstream_id if isinstance(upstream_id, str) else None,
                 upstream_model=(upstream_model if isinstance(upstream_model, str)
                                 else None),
                 deny_reason=deny_reason, detail=detail)


def _denied_response(deny_reason: str) -> GatewayResponse:
    """拒发回包：零证明头 + 统一 502。

    注：消费侧对非 200 **从不读 body**（`semantic_review_runner.py:190-191` 直接抛
    `k2_gateway_http_<status>`），所以 deny_reason 只是给运维/人看的，判定一律回账本。
    """
    error_body = json.dumps(
        {"error": {"type": "k2_attestation_denied", "reason": deny_reason}},
        ensure_ascii=False, sort_keys=True).encode("utf-8")
    return GatewayResponse(DENY_STATUS, {}, error_body)


def attest(request_body: bytes, route: Route, forward: Forwarder,
           audit: AuditLog) -> GatewayResponse:
    """核心逻辑：转发 → 捕获上游 id/model → 发放/拒发 + 记账。纯函数，便于单测。

    任何路径都必留**恰好一条**审计行：路由身份脏、转发器抛异常、上游不合规，
    都不会出现"炸出去/零记录"。
    """
    request_sha = _sha256(request_body)

    # 路由身份自己脏 ⇒ 证明头必脏 ⇒ 转发前就停（省一次上游调用，且不出话）。
    unsafe = route.unsafe_header_fields()
    if unsafe:
        _record_denial(audit, route, request_sha, None, None, None,
                       "route_identity_invalid_chars", detail=",".join(unsafe))
        return _denied_response("route_identity_invalid_chars")

    try:
        reply = forward(request_body)
    except Exception as exc:  # noqa: BLE001 - 故意兜住一切上游/网络故障
        # 详情只记异常**类名**：消息里可能带 URL/凭据片段，不落账本。
        _record_denial(audit, route, request_sha, None, None, None,
                       "forward_exception", detail=type(exc).__name__)
        return _denied_response("forward_exception")

    response_sha = _sha256(reply.body) if reply.body_complete else None
    issue, upstream_id, upstream_model, deny_reason = _decide(reply, route)

    if not issue:
        # 拒发：不回任何证明头，统一 502；审计记「拒发」。
        _record_denial(audit, route, request_sha, response_sha,
                       upstream_id, upstream_model, deny_reason)
        return _denied_response(deny_reason)

    # 发放：证明头由本网关生成，request-id 恒等体 id、model 恒等体 model；
    # 四个值全部已过 HEADER_VALUE_RE（_decide + 上面的路由身份检查）。
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

    def close(self) -> None:
        """释放转发器自持的连接池（转发器契约的**可选**扩展，不影响测试桩）。"""
        closer = getattr(self.forward, "close", None)
        if callable(closer):
            closer()


def make_httpx_forwarder(route: Route, timeout: float = 120.0,
                         client: "httpx.Client | None" = None) -> Forwarder:
    """生产用转发器：把请求体原样 POST 到核准路由（OpenAI 兼容）。测试不用它。

    连接池复用：Client 建一次、跨请求复用（httpx.Client 官方声明线程安全，
    ThreadingHTTPServer 的多线程 handler 共用同一个 client）；`follow_redirects=False`
    是 fail-closed——绝不带着 Authorization 头跟着 302 跳去别处。响应按
    MAX_UPSTREAM_RESPONSE_BYTES 流式截断，超限立刻停读（内存有界）。
    """
    url = route.base_url.rstrip("/") + "/chat/completions"
    session = client if client is not None else httpx.Client(
        timeout=timeout, follow_redirects=False)

    def forward(request_body: bytes) -> UpstreamReply:
        with session.stream("POST", url, content=request_body, headers={
            "Authorization": f"Bearer {route.api_key}",
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }) as response:
            buffer = bytearray()
            for chunk in response.iter_bytes():
                if len(buffer) + len(chunk) > MAX_UPSTREAM_RESPONSE_BYTES:
                    return UpstreamReply(response.status_code, bytes(buffer),
                                         body_complete=False)
                buffer.extend(chunk)
            return UpstreamReply(response.status_code, bytes(buffer))

    forward.close = session.close
    return forward


def _protocol_error_body(error_type: str, reason: str | None = None) -> bytes:
    payload: dict = {"error": {"type": error_type}}
    if reason is not None:
        payload["error"]["reason"] = reason
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _make_handler(gateway: AttestationGateway, inbound_token: str = "") -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LG-Attestation-Gateway/1.1"
        # socket 单次操作超时：慢客户端不能无限占住线程。socketserver 的
        # StreamRequestHandler.setup() 会 connection.settimeout(self.timeout)。
        timeout = HANDLER_SOCKET_TIMEOUT

        def _reply(self, status: int, headers: dict, body: bytes) -> None:
            # 明确关连接：每次请求后即断开，避免 keep-alive 线程在 shutdown 时滞留；
            # 所有分支（成功/401/404/400/408/413/500）都走这里，行为一致。
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            # 刻意**不发** Content-Encoding（消费侧要求 identity），也**不发**证明头
            # 除非 headers 里确实有（拒发时 headers 为空 ⇒ 零证明头）。
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _protocol_denial(self, status: int, error_type: str, reason: str,
                             body: bytes = b"", headers: dict | None = None) -> None:
            """协议层/鉴权层拒发：回状态码 + 零证明头，**并留一行拒发审计**。

            不变量：每一次入站 POST 恰好一行审计（`decision=denied`）——账本里
            不会出现"调过网关但查无此行"的空洞。`request_sha256` 记的是实际读到的
            字节（协议层拒发常为 0 字节，即 SHA256("")），`response_sha256=null`
            表示压根没转发、没上游体。
            """
            self._audit_protocol_denial(reason, body)
            self._reply(status, headers or {}, _protocol_error_body(error_type, reason))

        def _audit_protocol_denial(self, reason: str, body: bytes = b"") -> None:
            gateway.audit.record(DENIED, gateway.route, _sha256(body), None,
                                 upstream_id=None, upstream_model=None,
                                 deny_reason=reason)

        def _read_exact(self, length: int) -> bytes:
            """读满 length 字节；客户端提前断 ⇒ 返回短读（由调用方判 400）。"""
            buffer = bytearray()
            while len(buffer) < length:
                chunk = self.rfile.read(length - len(buffer))
                if not chunk:
                    break
                buffer.extend(chunk)
            return bytes(buffer)

        def _read_request_body(self):
            """返回 (body, (状态码, deny_reason) | None)：协议层硬门，先于任何转发。"""
            if (self.headers.get("Transfer-Encoding") or "").strip():
                # 显式拒 chunked：不能把未知长度的流当空体转发（也不按读至 EOF
                # 处理——那等于让上游替我们猜长度）。
                return b"", (400, "transfer_encoding_unsupported")
            lengths = self.headers.get_all("Content-Length") or []
            if not lengths:
                # 缺 Content-Length：HTTP/1.1 下这是无长度请求，拒（不猜）。
                return b"", (400, "content_length_missing")
            if len(lengths) > 1:
                # 多个 Content-Length = 请求走私经典入口，直接拒。
                return b"", (400, "content_length_invalid")
            raw = (lengths[0] or "").strip()
            if _DECIMAL_RE.fullmatch(raw) is None:
                return b"", (400, "content_length_invalid")
            length = int(raw)
            if length > MAX_FORWARD_BYTES:
                return b"", (413, "request_too_large")
            try:
                body = self._read_exact(length)
            except TimeoutError:
                # 读超时：客户端"声明了长度却不发"。前缀字节可能已进 rfile 内部缓冲
                # 而取不回来，故这里按"已读 0 字节"记账（宁可少记也不谎报）。
                return b"", (408, "request_body_read_timeout")
            except OSError:
                return b"", (400, "request_body_read_error")
            if len(body) != length:
                # 声明长度与实读不符：绝不静默截断后照常转发记账。
                return body, (400, "request_body_truncated")
            return body, None

        def _post(self) -> None:
            if not check_inbound_authorization(self.headers.get("Authorization"),
                                               inbound_token):
                # 401 只给 WWW-Authenticate，**零证明头**（凭据不符的调用方拿不到
                # 任何"本网关确实调过核准上游"的凭据）。
                self._protocol_denial(
                    401, "unauthorized", "inbound_unauthorized", b"",
                    headers={"WWW-Authenticate":
                             'Bearer realm="lg-attestation-gateway"'})
                return
            if not self.path.endswith("/chat/completions"):
                self._protocol_denial(404, "not_found", "path_not_found")
                return
            body, failure = self._read_request_body()
            if failure is not None:
                self._protocol_denial(failure[0], "bad_request", failure[1], body)
                return
            result = gateway.handle(body)   # attest 内部已兜转发异常 ⇒ 必有审计行
            self._reply(result.status, result.headers, result.body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib 约定名
            # 全路径兜底：handler 绝不允许裸炸线程（客户端只该看到状态码，不是
            # 连接中断）。审计写失败之类硬故障走 500 + stderr 单行。
            try:
                self._post()
            except Exception as exc:  # noqa: BLE001
                _log_stderr("attestation_gateway_handler_error:"
                            + type(exc).__name__)
                try:
                    self._reply(500, {},
                                _protocol_error_body("gateway_internal_error"))
                except Exception:  # noqa: BLE001 - 回包都失败就没什么可做的了
                    pass

        def log_message(self, *args) -> None:  # 静音默认 stderr 访问日志
            return

    return Handler


def check_inbound_authorization(header_value, inbound_token: str) -> bool:
    """入站鉴权：未配令牌 ⇒ 不拦（此时只允许回环监听，见 check_inbound_binding）；
    配了令牌 ⇒ 恒定时间比对 `Authorization: Bearer <token>`。"""
    if not inbound_token:
        return True
    if not isinstance(header_value, str):
        return False
    scheme, _, presented = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return secrets.compare_digest(presented.strip().encode("utf-8"),
                                 inbound_token.encode("utf-8"))


def check_inbound_binding(host: str, inbound_token: str) -> None:
    """监听地址 × 入站令牌 的 fail-closed 约束：非回环必须配令牌，否则拒绝启动。

    默认 127.0.0.1 只是缓解——`LG_ATTEST_LISTEN_HOST` 改成非回环就等于把上游
    api_key 借给所有能触达该端口的进程（开放代理/confused deputy）。故：非回环 +
    无令牌 ⇒ 抛 GatewayStartupError，`main()` 非零退出。
    """
    if is_loopback_host(host):
        return
    if not (inbound_token or "").strip():
        raise GatewayStartupError(
            "inbound_token_required_for_non_loopback_listen:"
            f"host={host},set={INBOUND_TOKEN_ENV}")


def make_server(gateway: AttestationGateway, host: str = "127.0.0.1",
                port: int = 0, *, inbound_token: str = "") -> ThreadingHTTPServer:
    """构造（已绑定端口的）HTTP 服务；调用方负责 serve_forever/shutdown。

    port=0 时由内核分配临时端口，实际端口在 `server.server_address[1]`。
    绑定前先过 `check_inbound_binding`（非回环无令牌 ⇒ 拒，不开监听口）。
    """
    check_inbound_binding(host, inbound_token)
    return ThreadingHTTPServer((host, port), _make_handler(gateway, inbound_token))


_ENV_KEYS = {
    "base_url": "LG_ATTEST_UPSTREAM_BASE_URL",
    "api_key": "LG_ATTEST_UPSTREAM_API_KEY",
    "provider": "LG_ATTEST_ROUTE_PROVIDER",
    "model": "LG_ATTEST_ROUTE_MODEL",
    "channel_id": "LG_ATTEST_ROUTE_CHANNEL_ID",
    "audit": "LG_ATTEST_AUDIT_PATH",
}

INBOUND_TOKEN_ENV = "LG_ATTEST_INBOUND_TOKEN"
LISTEN_HOST_ENV = "LG_ATTEST_LISTEN_HOST"
LISTEN_PORT_ENV = "LG_ATTEST_LISTEN_PORT"


def _check_base_url(raw: str) -> None:
    """镜像消费侧的来源硬门（`k2_gateway_origin_untrusted`）：生成侧不能更松。

    非 https 仅放行回环；禁 userinfo（否则 api_key 会随 URL 出现在日志/异常里）；
    禁 query/fragment（拼出来的 /chat/completions 会带歧义）。
    """
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"}:
        raise GatewayStartupError("attestation_gateway_base_url_scheme_untrusted:"
                                  + (parts.scheme or "<none>"))
    if parts.username or parts.password:
        raise GatewayStartupError(
            "attestation_gateway_base_url_userinfo_forbidden")
    if not parts.hostname:
        raise GatewayStartupError("attestation_gateway_base_url_host_missing")
    if parts.query or parts.fragment:
        raise GatewayStartupError(
            "attestation_gateway_base_url_query_fragment_forbidden")
    if parts.scheme == "http" and not is_loopback_host(parts.hostname):
        raise GatewayStartupError(
            "attestation_gateway_base_url_http_non_loopback:" + parts.hostname)


def route_from_env(environ: dict | None = None) -> Route:
    """由环境变量给出核准路由（base_url/api_key/路由身份）。缺任一项即拒绝启动。"""
    env = os.environ if environ is None else environ
    missing = sorted(name for key, name in _ENV_KEYS.items()
                     if key != "audit" and not (env.get(name) or "").strip())
    if missing:
        raise GatewayStartupError("attestation_gateway_env_missing:"
                                  + ",".join(missing))
    base_url = (env[_ENV_KEYS["base_url"]] or "").strip()
    _check_base_url(base_url)
    route = Route(
        base_url=base_url,
        # 刻意**不 strip**：密钥含合法首尾空白时静默改写会导致上游 401 且难定位。
        # 空值（含全空白）已在上面的 missing 判据里 fail-closed。
        api_key=env[_ENV_KEYS["api_key"]],
        provider=(env[_ENV_KEYS["provider"]] or "").strip(),
        model=(env[_ENV_KEYS["model"]] or "").strip(),
        channel_id=(env[_ENV_KEYS["channel_id"]] or "").strip(),
    )
    unsafe = route.unsafe_header_fields()
    if unsafe:
        raise GatewayStartupError("attestation_gateway_route_header_unsafe:"
                                  + ",".join(unsafe))
    return route


def gateway_from_env(environ: dict | None = None) -> AttestationGateway:
    env = os.environ if environ is None else environ
    route = route_from_env(env)
    audit_path = (env.get(_ENV_KEYS["audit"]) or "").strip()
    if not audit_path:
        raise GatewayStartupError("attestation_gateway_env_missing:"
                                  + _ENV_KEYS["audit"])
    return AttestationGateway(route, make_httpx_forwarder(route), AuditLog(audit_path))


def listen_from_env(environ: dict | None = None) -> tuple[str, int]:
    """监听地址/端口：端口解析失败 ⇒ 明确报错（非裸 traceback），非 0..65535 亦拒。"""
    env = os.environ if environ is None else environ
    host = (env.get(LISTEN_HOST_ENV) or "127.0.0.1").strip() or "127.0.0.1"
    raw_port = (env.get(LISTEN_PORT_ENV) or "8080").strip() or "8080"
    try:
        port = int(raw_port)
    except ValueError:
        raise GatewayStartupError("attestation_gateway_listen_port_invalid:"
                                  + raw_port) from None
    if not 0 <= port <= 65535:
        raise GatewayStartupError("attestation_gateway_listen_port_out_of_range:"
                                  + raw_port)
    return host, port


def inbound_token_from_env(environ: dict | None = None) -> str:
    """入站令牌：刻意不 strip（与 api_key 同口径，密钥原样使用）。"""
    env = os.environ if environ is None else environ
    return env.get(INBOUND_TOKEN_ENV) or ""


def _startup_failed(reason: str) -> int:
    _log_stderr("attestation_gateway_startup_failed:" + reason)
    return 2


def main(argv=None) -> int:
    """生产入口：读环境变量，起服务并常驻。

    任何启动期失败（缺环境变量/上游 base_url 不可信/路由身份含非法字符/端口解析
    失败/非回环监听无入站令牌）都只回「单行原因 + 退出码 2」，绝不裸 traceback。
    """
    try:
        gateway = gateway_from_env()
        host, port = listen_from_env()
        server = make_server(gateway, host, port,
                             inbound_token=inbound_token_from_env())
    except GatewayStartupError as exc:
        return _startup_failed(str(exc))
    except Exception as exc:  # noqa: BLE001 - 启动面一律不打 traceback
        return _startup_failed(type(exc).__name__)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        gateway.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
