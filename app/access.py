"""远程访问门（2026-09-14 加）。

背景：盲评页面原本只绑 `127.0.0.1`、且**完全无鉴权**。集霸要远程批改（手机 / 外网），
一旦暴露就必须先解决两件事：

1. **内容是私密的**（语料为成人向小说），局域网里被人翻到不合适；
2. **页面有写入口**——`POST /review/{id}/verdict` 会直接写进实验数据。
   谁能访问谁就能往判定表里灌数据，污染掉整条实验链。

设计（按第一性原理取舍）：
- **令牌从文件读，自动生成并持久化** → 重启后令牌不变，手机上的 cookie 不会失效。
- **本机免鉴权** → 不破坏 `present_files` 的本机预览与本机日常使用。
- **但要识别反向代理**：cloudflared 把隧道流量转到本机，`request.client.host` 会是 `127.0.0.1`。
  若只看 host 就会**把整条外网流量当本机放行**，鉴权形同虚设。
  故：只有在 host 是本机 **且没有** `CF-Connecting-IP` / `X-Forwarded-For` 时才免鉴权。
- **`?t=<token>` 一次性换 cookie 并跳回干净 URL** → 手机上点一下链接就长期免输；
  也避免令牌留在地址栏/历史/分享里。
- 比较用 `hmac.compare_digest`，避免时序侧信道。
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

_ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = _ROOT / "data" / "review_token.txt"
COOKIE = "rv_token"
COOKIE_MAX_AGE = 90 * 24 * 3600          # 90 天
_LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")
# 反向代理注入的请求头：出现任一即说明请求来自隧道/代理，不得按本机放行
_PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip")


def load_token() -> str | None:
    """优先环境变量；否则读文件；都没有则生成并持久化。返回 None 表示显式关闭鉴权。"""
    if os.environ.get("REVIEW_NO_AUTH") == "1":
        return None
    env = (os.environ.get("REVIEW_TOKEN") or "").strip()
    if env:
        return env
    if TOKEN_PATH.exists():
        t = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if t:
            return t
    t = secrets.token_urlsafe(24)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(t + "\n", encoding="utf-8")
    try:
        TOKEN_PATH.chmod(0o600)
    except OSError:
        pass
    return t


_TOKEN = load_token()
_PROXY_HEADERS_ACTIVE = False


def _is_local(request: Request) -> bool:
    """本机直连（非经代理）。经代理的请求即便 host 是 127.0.0.1 也不算本机。"""
    host = request.client.host if request.client else ""
    if host not in _LOCAL_HOSTS:
        return False
    return not any(h in request.headers for h in _PROXY_HEADERS)


def _is_https(request: Request) -> bool:
    """请求是否经 HTTPS（隧道场景）。

    只看 `url.scheme` 不够——应用在 cloudflared 后面收到的是明文 HTTP，
    真正协议在 `X-Forwarded-Proto` 里。
    """
    if request.url.scheme == "https":
        return True
    return (request.headers.get("x-forwarded-proto") or "").lower() == "https"


def _ok(supplied: str | None) -> bool:
    return bool(supplied) and _TOKEN is not None and hmac.compare_digest(supplied, _TOKEN)


_GATE_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>需要访问令牌</title></head>
<body style="font-family:-apple-system,'Segoe UI',sans-serif;background:#faf9f6;color:#2c2a26;
             display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0">
<form style="background:#fff;border:1px solid #e6e3dd;border-radius:12px;padding:26px 28px;
             box-shadow:0 2px 12px rgba(0,0,0,.05);width:300px" method="get"
      onsubmit="location.replace('/?t='+encodeURIComponent(document.getElementById('t').value));return false">
  <div style="font-size:15px;font-weight:600;margin-bottom:4px">需要访问令牌</div>
  <div style="font-size:12.5px;color:#8d887d;margin-bottom:14px">本页面含私密内容，粘贴令牌后进入</div>
  <input id="t" autocomplete="off" autofocus  style="width:100%;box-sizing:border-box;padding:9px 11px;
         border:1px solid #dcd9d2;border-radius:8px;font-size:14px;font-family:inherit"
         placeholder="粘贴令牌">
  <button type="submit" style="width:100%;margin-top:12px;padding:10px;border:0;border-radius:8px;
          background:#2c2a26;color:#fff;font-size:14px;font-family:inherit;cursor:pointer">进入</button>
</form></body></html>"""


def install(app) -> None:
    """挂上访问门。未配置令牌时完全不介入（本机使用行为不变）。"""
    if _TOKEN is None:
        return

    @app.middleware("http")
    async def _access_gate(request: Request, call_next):
        if _is_local(request):
            return await call_next(request)

        q_token = request.query_params.get("t")
        if _ok(q_token):
            # 用 ?t= 换 cookie，并跳回去掉参数的 URL——令牌不再留在地址栏/历史里。
            # Secure 只在 HTTPS 下打：局域网是明文 HTTP，打了 Secure 就发不出去，
            # 会把"局域网也能用"这条路弄坏。按请求实际协议动态决定。
            clean = request.url.remove_query_params("t")
            resp = RedirectResponse(url=str(clean), status_code=302)
            resp.set_cookie(COOKIE, q_token, max_age=COOKIE_MAX_AGE,
                            httponly=True, samesite="lax", secure=_is_https(request))
            return resp
        if _ok(request.cookies.get(COOKIE)):
            return await call_next(request)
        return HTMLResponse(_GATE_HTML, status_code=401)
