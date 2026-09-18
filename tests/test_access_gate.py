"""远程访问门回归（2026-09-14）。

集霸要手机/外网远程批改。页面含私密语料 + **有写入口**
（`POST /review/{id}/verdict` 直接写实验数据），暴露前必须加鉴权。

本文件锁死四条：
1. 本机直连**免鉴权**（否则 `present_files` 预览和本机日常使用会坏）；
2. 远程无令牌 → 401；带正确令牌 → 通过；
3. `?t=` 换 cookie 并**跳回干净 URL**（令牌不留在地址栏/历史/分享里）；
4. **❗经反向代理的 127.0.0.1 必须仍要鉴权**。
   这是最隐蔽的洞：cloudflared 把隧道流量转到本机，`request.client.host` 就是 `127.0.0.1`。
   若只判断 host，**整条外网流量会被当本机放行，鉴权形同虚设**。
"""
import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import access as A   # noqa: E402


@pytest.fixture(autouse=True)
def _restore_token():
    """`_build()` 会改模块全局 `_TOKEN`；不还原会污染其他测试模块。

    这类全局状态污染很难查（表现为"别的文件莫名 401"），故用 autouse 强制还原。
    """
    saved = A._TOKEN
    yield
    A._TOKEN = saved


def _build(host: str, token: str | None = "TOK"):
    """构造一个只含访问门的 app，并把 scope 的客户端地址改写成 `host`。

    本环境的 starlette 版本不支持 `TestClient(client=...)`，故用一层薄 ASGI 包装注入
    `scope["client"]` —— 这样仍是**真集成**（走完整中间件链），而不是只测纯函数。
    """
    A._TOKEN = token
    inner = FastAPI()

    @inner.get("/")
    def _root():
        return {"ok": True}

    @inner.get("/secret")
    def _secret():
        return {"secret": 42}

    A.install(inner)

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (host, 12345)
        await inner(scope, receive, send)

    return TestClient(wrapped)


def _client(host: str = "127.0.0.1", token: str | None = "TOK"):
    return _build(host, token)


# ── ① 本机免鉴权 ──────────────────────────────────────────────
def test_localhost_bypasses_auth():
    assert _client("127.0.0.1").get("/").status_code == 200
    assert _client("::1").get("/").status_code == 200


def test_no_token_configured_disables_gate():
    """未配置令牌时完全不介入（本机使用行为不变）。"""
    assert _client("203.0.113.9", token=None).get("/").status_code == 200


# ── ② 远程需令牌 ──────────────────────────────────────────────
def test_remote_without_token_is_401():
    r = _client("203.0.113.9").get("/")
    assert r.status_code == 401
    assert "访问令牌" in r.text


def test_remote_with_wrong_token_is_401():
    assert _client("203.0.113.9").get("/?t=WRONG").status_code == 401


def test_remote_with_correct_token_sets_cookie_and_redirects():
    """?t= 应 302 跳回干净 URL 并种 cookie；之后裸访问放行。"""
    c = _client("203.0.113.9")
    r = c.get("/?t=TOK", follow_redirects=False)
    assert r.status_code == 302
    assert "t=TOK" not in r.headers["location"], "跳转目标必须去掉令牌参数"
    assert A.COOKIE in r.headers.get("set-cookie", "")

    c.cookies.set(A.COOKIE, "TOK")
    assert c.get("/secret").status_code == 200


def test_cookie_is_secure_only_over_https():
    """Secure 只在 HTTPS 下打。

    HTTPS 下不打 Secure = cookie 可能在明文信道被截；
    HTTP（局域网）下打 Secure = cookie 发不出去，直接把局域网访问弄坏。
    故必须按实际协议动态决定——这是个双向陷阱，两侧都要锁。
    """
    # 隧道场景：X-Forwarded-Proto: https → 必须带 Secure
    c = _client("203.0.113.9")
    r = c.get("/?t=TOK", headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)
    assert "secure" in r.headers.get("set-cookie", "").lower(), \
        "HTTPS 下 cookie 必须带 Secure"

    # 局域网场景：明文 HTTP → 不能带 Secure，否则 cookie 发不出去
    c2 = _client("192.168.1.50")
    r2 = c2.get("/?t=TOK", follow_redirects=False)
    assert "secure" not in r2.headers.get("set-cookie", "").lower(), \
        "HTTP 下不得带 Secure（会弄坏局域网访问）"


def test_cookie_always_httponly():
    """HttpOnly 任何情况下都要有——防脚本读取。"""
    c = _client("203.0.113.9")
    r = c.get("/?t=TOK", follow_redirects=False)
    assert "httponly" in r.headers.get("set-cookie", "").lower()
    c2 = _client("192.168.1.50")
    r2 = c2.get("/?t=TOK", follow_redirects=False)
    assert "httponly" in r2.headers.get("set-cookie", "").lower()


def test_cookie_with_wrong_value_is_401():
    c = _client("203.0.113.9")
    c.cookies.set(A.COOKIE, "NOT-TOK")
    assert c.get("/").status_code == 401


# ── ③ 反向代理：最隐蔽的洞 ────────────────────────────────────
@pytest.mark.parametrize("header", ["CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"])
def test_proxied_localhost_still_requires_token(header):
    """❗核心回归：host 是 127.0.0.1 但带代理头 → 必须仍要令牌。

    cloudflared 隧道流量转发到本机时 `request.client.host` == 127.0.0.1。
    若只判 host 就放行，外网等于完全没有鉴权——这是最危险的一类鉴权洞。
    """
    r = _client("127.0.0.1").get("/", headers={header: "203.0.113.9"})
    assert r.status_code == 401, (
        f"带 {header} 的请求不得按本机放行（否则隧道流量绕过鉴权）")


def test_proxied_localhost_with_token_passes():
    """反向代理下带对令牌仍要能进来——别把正常远程访问也挡死。"""
    r = _client("127.0.0.1").get("/?t=TOK", headers={"CF-Connecting-IP": "203.0.113.9"},
                                 follow_redirects=False)
    assert r.status_code == 302


# ── ④ 令牌来源 ────────────────────────────────────────────────
def test_token_persisted_and_stable(tmp_path, monkeypatch):
    """令牌必须持久化：重启后不变，手机上已种的 cookie 才不会失效。"""
    p = tmp_path / "tok.txt"
    monkeypatch.setattr(A, "TOKEN_PATH", p)
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.delenv("REVIEW_NO_AUTH", raising=False)
    t1 = A.load_token()
    t2 = A.load_token()
    assert t1 and t1 == t2, "同文件两次读取必须一致"
    assert p.read_text(encoding="utf-8").strip() == t1


def test_env_token_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "TOKEN_PATH", tmp_path / "tok.txt")
    monkeypatch.delenv("REVIEW_NO_AUTH", raising=False)
    monkeypatch.setenv("REVIEW_TOKEN", "FROM-ENV")
    assert A.load_token() == "FROM-ENV"


def test_off_switch_beats_env_token(tmp_path, monkeypatch):
    """优先级：显式关闸 > 环境令牌 > 文件 > 自动生成。

    关闸是开发/测试用的显式开关，必须能压过一切——否则测试环境会莫名被 401 挡住。
    """
    monkeypatch.setattr(A, "TOKEN_PATH", tmp_path / "tok.txt")
    monkeypatch.setenv("REVIEW_TOKEN", "FROM-ENV")
    monkeypatch.setenv("REVIEW_NO_AUTH", "1")
    assert A.load_token() is None


def test_file_token_used_when_no_env(tmp_path, monkeypatch):
    """环境没给就落到文件；文件也没有才自动生成（并持久化）。"""
    p = tmp_path / "tok.txt"
    monkeypatch.setattr(A, "TOKEN_PATH", p)
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.delenv("REVIEW_NO_AUTH", raising=False)
    p.write_text("FROM-FILE\n", encoding="utf-8")
    assert A.load_token() == "FROM-FILE"


def test_explicit_disable(tmp_path, monkeypatch):
    """显式关闭鉴权的开关（本机开发用）必须能关掉整道门。"""
    monkeypatch.setattr(A, "TOKEN_PATH", tmp_path / "tok.txt")
    monkeypatch.setenv("REVIEW_NO_AUTH", "1")
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    assert A.load_token() is None
