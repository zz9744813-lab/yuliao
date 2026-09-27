"""可信代理白名单 `LG_TRUSTED_PROXIES` 回归（2026-09-27，R2b）。

审计《language-genome-code-audit-20260923》条件性风险：服务以 `--host 0.0.0.0`
暴露，而 loopback 免令牌的判定只认「peer 是 127.0.0.1/::1/localhost 且**无**代理头」。
**若回环反向代理漏传 `cf-connecting-ip` / `x-forwarded-for` / `x-real-ip`**，
整站就免鉴权。改前那句「有代理头就不放行」在行为上最严，却是**隐式**的：
部署方看不出自己正处在哪个风险面上。本次把判定升为显式策略 + `LG_TRUSTED_PROXIES`
白名单 + 启动自检可机械核验输出。

本文件钉死五件事（前四件是判定链，第五件是可核验性）：

1. **无代理头 + 回环 peer ⇒ 免令牌**（既有行为不被本次破坏；注意免令牌本身
   仍需显式 `LG_LOCAL_BYPASS=1`，见 `tests/test_remote_caps_budget.py`）；
2. **有代理头 + 回环 peer + env 未设 ⇒ 要求令牌**（默认最严，绝不静默放宽）；
3. **有代理头 + 回环 peer + peer 在白名单内 ⇒ 仍要求令牌**，且
   `via=trusted_proxy` 可观察——白名单给的是**标注**，不是通行证；
4. **有代理头 + 非回环 peer ⇒ 要求令牌**；
5. **`self_check()` 输出含 `LG_TRUSTED_PROXIES` 字面量**，未设时含 `(未设)`。

另附两条加固回归：非法白名单条目永不匹配（错配不静默）、白名单在任何组合下
都不能把「需令牌」翻成「免令牌」（含 `LG_LOCAL_BYPASS=1` 同开的最坏组合）。

隔离：本文件只构造一个内存 FastAPI + 只含访问门，不 import `app.main`/
`app.db`，DB URL 显式指向 tmp_path（见 `_isolate`），令牌文件路径也被重定向
到 tmp_path——不碰真库、不写真 `data/`。
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import access as A   # noqa: E402

_VIA = "X-Access-Via"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每次用例一个独立临时库 + 独立令牌文件，并把两个开关的默认拉回最严。

    `install()` 不改这两个全局，故不还原；但 `A._TOKEN` 会被 `_build()` 改写，
    必须还原，否则会污染同轮其他测试模块（这类污染表现为「别的文件莫名 401」）。
    """
    monkeypatch.setenv("LG_DATABASE_URL", f"sqlite:///{(tmp_path / 'iso.db').as_posix()}")
    monkeypatch.setattr(A, "TOKEN_PATH", tmp_path / "review_token.txt")
    monkeypatch.setattr(A, "ADMIN_TOKEN_PATH", tmp_path / "admin_token.txt")
    monkeypatch.delenv("LG_TRUSTED_PROXIES", raising=False)   # 默认＝最严
    monkeypatch.delenv("LG_LOCAL_BYPASS", raising=False)      # 默认＝免令牌关
    saved = A._TOKEN
    yield
    A._TOKEN = saved


def _build(host: str, token: str | None = "TOK"):
    """构造只含访问门的 app，并把 ASGI scope 的客户端地址改写成 `host`。

    本环境 starlette 不支持 `TestClient(client=...)`，故用一层薄 ASGI 包装注入
    `scope["client"]` —— 仍是**真集成**（走完整中间件链）。
    """
    A._TOKEN = token
    inner = FastAPI()

    @inner.get("/")
    def _root():
        return {"ok": True}

    @inner.post("/experiments")
    def _mk():
        return {"ok": True}

    A.install(inner)

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (host, 12345)
        await inner(scope, receive, send)

    return TestClient(wrapped)


# ── ① 无代理头 + 回环 peer ⇒ 免令牌（既有行为不被破坏）─────────────
def test_loopback_without_proxy_header_still_bypasses(monkeypatch):
    """免令牌的前提没被本次收口改动：回环 peer + 无代理头 + 显式开 bypass ⇒ 200。

    免令牌本身仍由 `LG_LOCAL_BYPASS` 显式 opt-in（R2 既有口径，本次未动）。
    `::1` / `localhost` 两个等价回环写法一并钉住。
    """
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    for host in ("127.0.0.1", "::1", "localhost"):
        c = _build(host)
        r = c.get("/")
        assert r.status_code == 200, f"{host} 无代理头时应免令牌"
        assert A.classify(host, False) == "loopback_direct"


def test_loopback_without_proxy_header_needs_token_when_bypass_off(monkeypatch):
    """默认（bypass 未开）时，回环 peer 无代理头也需令牌——收口没有顺手放宽。"""
    r = _build("127.0.0.1").get("/")
    assert r.status_code == 401
    assert A.classify("127.0.0.1", False) == "loopback_direct", \
        "分类标签与放行与否解耦：loopback_direct 只说明「无代理头」，不说明已免令牌"


# ── ② 有代理头 + 回环 peer + env 未设 ⇒ 要求令牌（默认最严）─────────
@pytest.mark.parametrize("header", ["CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"])
def test_proxy_header_on_loopback_needs_token_when_env_unset(monkeypatch, header):
    """❗核心：白名单**未设**时，带代理头的回环请求一律要令牌——默认最严。

    这一格就是审计点名的风险面：回环代理若漏传头 ⇒ 落到 `loopback_direct`；
    反过来只要头传到了，就绝不能因为「peer 看着像本机」而放行。
    """
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")     # 最坏组合：连 bypass 都开了
    r = _build("127.0.0.1").get("/", headers={header: "203.0.113.9"})
    assert r.status_code == 401, f"白名单未设 + {header} 必须要求令牌（默认最严）"
    assert r.headers[_VIA] == "untrusted_proxy"


# ── ③ 有代理头 + 回环 peer + 在白名单内 ⇒ 仍要求令牌，via=trusted_proxy ──
def test_whitelisted_proxy_peer_still_needs_token_but_via_is_observable(monkeypatch):
    """白名单只把标签从 untrusted 抬成 trusted，**不改变需令牌这一格**。

    `X-Access-Via: trusted_proxy` 让部署方在运行时就能核对「这条流量是不是
    真的来自我声明的代理」，无需挂调试器。
    """
    monkeypatch.setenv("LG_TRUSTED_PROXIES", "127.0.0.1")
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    c = _build("127.0.0.1")
    r = c.get("/", headers={"CF-Connecting-IP": "203.0.113.9"})
    assert r.status_code == 401, "白名单命中也不免令牌"
    assert r.headers[_VIA] == "trusted_proxy"
    # 纯函数侧同一结论
    assert A.classify("127.0.0.1", True) == "trusted_proxy"
    assert A.peer_is_trusted_proxy("127.0.0.1") is True
    # 带对令牌的代理流量照常可进（别把正常远程访问挡死）
    r2 = c.get("/?t=TOK", headers={"CF-Connecting-IP": "203.0.113.9"},
               follow_redirects=False)
    assert r2.status_code == 302 and r2.headers[_VIA] == "trusted_proxy"


def test_whitelist_cidr_covers_loopback_peer(monkeypatch):
    """CIDR 形态的白名单同样只标注不放宽（覆盖 `127.0.0.0/8` 里任一地址）。"""
    monkeypatch.setenv("LG_TRUSTED_PROXIES", " 10.0.0.0/8 , 127.0.0.0/8 ")
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    r = _build("127.0.0.1").get("/", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401
    assert r.headers[_VIA] == "trusted_proxy"
    assert A.trusted_proxies() == ("10.0.0.0/8", "127.0.0.0/8"), "空项须丢弃、首尾空白须 strip"


def test_whitelisted_proxy_still_blocked_on_admin_endpoint(monkeypatch):
    """白名单标注过的流量，持评审档令牌打管理端点仍 403——档位面不受影响。"""
    monkeypatch.setenv("LG_TRUSTED_PROXIES", "127.0.0.1")
    c = _build("127.0.0.1")
    c.cookies.set(A.COOKIE, "TOK")
    r = c.post("/experiments", headers={"CF-Connecting-IP": "203.0.113.9"})
    assert r.status_code == 403
    assert r.headers[_VIA] == "trusted_proxy"


# ── ④ 有代理头 + 非回环 peer ⇒ 要求令牌 ─────────────────────────
def test_non_loopback_peer_with_proxy_header_needs_token(monkeypatch):
    """非回环 peer 一律需令牌，且标签恒为 remote（白名单改不了它）。"""
    monkeypatch.setenv("LG_TRUSTED_PROXIES", "203.0.113.0/24")
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    r = _build("203.0.113.9").get("/", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401
    assert r.headers[_VIA] == "remote"
    assert A.classify("203.0.113.9", True) == "remote"


# ── ⑤ self_check() 可机械核验 ─────────────────────────────────
def test_self_check_prints_env_name_and_unset_literal(monkeypatch, capsys):
    """未设时必须原样打印 `LG_TRUSTED_PROXIES` 与字面量 `(未设)`。

    不许折叠成「无白名单」之类说法——部署方要能一眼对账「我到底配没配」。
    """
    monkeypatch.delenv("LG_TRUSTED_PROXIES", raising=False)
    A._TOKEN = "TOK"
    A.self_check()
    out = capsys.readouterr().out
    assert "LG_TRUSTED_PROXIES" in out
    assert "(未设)" in out, f"未设时必须出现字面量 (未设)，实际输出：{out}"
    assert "默认最严" in out
    # 「代理头出现时的判定结果」也要落成可核对的文字
    assert "untrusted_proxy" in out and "loopback_direct" in out and "remote" in out


def test_self_check_prints_actual_whitelist_value(monkeypatch, capsys):
    """已设时原样打印实际取值与实算判定结果（不泄露令牌，不折叠）。"""
    monkeypatch.setenv("LG_TRUSTED_PROXIES", "127.0.0.1, 10.0.0.0/8")
    A._TOKEN = "TOK"
    A.self_check()
    out = capsys.readouterr().out
    assert "LG_TRUSTED_PROXIES=127.0.0.1, 10.0.0.0/8" in out
    assert "声明 2 条 / 合法 2 条" in out
    assert "trusted_proxy" in out, "白名单覆盖回环 peer 时判定应为 trusted_proxy"
    assert "(未设)" not in out


def test_self_check_flags_bypass_risk_surface(monkeypatch, capsys):
    """bypass 开着时必须喊出「回环代理漏传头 ⇒ 整站免鉴权」的风险面。"""
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    monkeypatch.delenv("LG_TRUSTED_PROXIES", raising=False)
    A._TOKEN = "TOK"
    A.self_check()
    out = capsys.readouterr().out
    assert "漏传" in out and "0.0.0.0" in out


# ── 加固：错配不静默、白名单永不放宽 ───────────────────────────
@pytest.mark.parametrize("junk", ["bogus-entry", "127.0.0.1/33", "999.1.1.1", "127.0.0.999"])
def test_invalid_whitelist_entries_never_match(monkeypatch, capsys, junk):
    """非法条目永不匹配，且 `self_check` 必须点名——不静默吞掉错配。"""
    monkeypatch.setenv("LG_TRUSTED_PROXIES", f"{junk}, 203.0.113.0/24")
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    r = _build("127.0.0.1").get("/", headers={"X-Real-IP": "203.0.113.9"})
    assert r.status_code == 401
    assert r.headers[_VIA] == "untrusted_proxy", "非法条目不得被当成白名单命中"
    A.self_check()
    assert junk in capsys.readouterr().out, "错配条目必须在自检里被点名"


def test_whitelist_can_never_widen_bypass(monkeypatch):
    """不变量穷举：白名单与 bypass 怎么配，`loopback_direct` 都是免令牌的唯一入口。"""
    monkeypatch.setenv("LG_TRUSTED_PROXIES", "127.0.0.1, ::1, localhost, 0.0.0.0/0")
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    assert A.classify("127.0.0.1", True) in ("trusted_proxy", "untrusted_proxy")
    for host in ("127.0.0.1", "::1", "localhost"):
        for has_hdr in (True, False):
            route = A.classify(host, has_hdr)
            if has_hdr:
                assert route != "loopback_direct", f"{host} 带代理头绝不可免令牌"
            else:
                assert route == "loopback_direct"
    r = _build("127.0.0.1").get("/", headers={"CF-Connecting-IP": "203.0.113.9"})
    assert r.status_code == 401, "白名单写 0.0.0.0/0 也不得放行带代理头的请求"


def test_access_route_reads_real_request(monkeypatch):
    """`access_route` 从真实请求取 peer 与代理头事实（与纯函数同结论）。"""
    monkeypatch.delenv("LG_TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    inner = FastAPI()
    seen: dict[str, str] = {}

    @inner.middleware("http")
    async def _probe(request, call_next):
        seen["route"] = A.access_route(request)
        return await call_next(request)

    @inner.get("/")
    def _root():
        return {"ok": True}

    A.install(inner)

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = ("127.0.0.1", 1234)
        await inner(scope, receive, send)

    c = TestClient(wrapped)
    c.get("/")
    assert seen["route"] == "loopback_direct"
    c.get("/", headers={"CF-Connecting-IP": "203.0.113.9"})
    assert seen["route"] == "untrusted_proxy"


# ── 2026-09-27 会审（qwen 席）补钉：strict 与跨族 ────────────────────────


def test_host_bits_entry_is_rejected_not_widened(monkeypatch, capsys):
    """`127.0.0.1/0` 这类「主机位被置位」的条目**必须被判非法**，绝不归一放大。

    会审实测（qwen 席）：旧写法 `ip_network(entry, strict=False)` 会把
    `127.0.0.1/0` 归一成 `0.0.0.0/0`（匹配**全部 IPv4**），一条手滑的条目就能
    把整个 v4 空间拉进白名单，与「容错方向只能是更严」的注释自相矛盾。
    """
    monkeypatch.setenv(A._TRUSTED_PROXIES_ENV, "127.0.0.1/0")
    good, bad = A._parse_trusted_proxies()
    assert good == (), "主机位被置位的条目不得进入白名单"
    assert len(bad) == 1 and "127.0.0.1/0" in bad[0]
    # 归一放大若发生，任何 v4 地址都会命中——这里逐点钉死不放行
    for host in ("127.0.0.1", "10.1.2.3", "203.0.113.9"):
        assert A.peer_is_trusted_proxy(host) is False
    # 自检必须点名该条目被忽略（不是静默）
    A.self_check()
    out = capsys.readouterr().out
    assert "127.0.0.1/0" in out


@pytest.mark.parametrize("mixed", ["127.0.0.1,::1/128", "::1/128,127.0.0.1"])
def test_cross_family_whitelist_does_not_raise(monkeypatch, mixed):
    """白名单混填 v4+v6 时，跨族包含判定不得抛异常（否则中间件对全站 500）。

    会审点名：`IPv4Address in IPv6Network` 的跨族行为需钉一条回归。
    本 Python 上实测返回 False（不抛），这里把该行为固定下来。
    """
    monkeypatch.setenv(A._TRUSTED_PROXIES_ENV, mixed)
    # 不抛：逐族各查一遍，且只有本族条目命中
    assert A.peer_is_trusted_proxy("127.0.0.1") is True
    assert A.peer_is_trusted_proxy("::1") is True
    # 跨族不得因异常而误判为可信
    monkeypatch.setenv(A._TRUSTED_PROXIES_ENV, "::1/128")
    assert A.peer_is_trusted_proxy("127.0.0.1") is False
    monkeypatch.setenv(A._TRUSTED_PROXIES_ENV, "127.0.0.0/8")
    assert A.peer_is_trusted_proxy("::1") is False
