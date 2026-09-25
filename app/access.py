"""远程访问门（2026-09-14 加）。

背景：盲评页面原本只绑 `127.0.0.1`、且**完全无鉴权**。集霸要远程批改（手机 / 外网），
一旦暴露就必须先解决两件事：

1. **内容是私密的**（语料为成人向小说），局域网里被人翻到不合适；
2. **页面有写入口**——`POST /review/{id}/verdict` 会直接写进实验数据。
   谁能访问谁就能往判定表里灌数据，污染掉整条实验链。

设计（按第一性原理取舍）：
- **令牌从文件读，自动生成并持久化** → 重启后令牌不变，手机上的 cookie 不会失效。
- **本机免鉴权 → 2026-09-23（审计残留 R2）改为显式 opt-in**：旧实现「peer 是
  loopback 且无代理头就免令牌」是**隐式默认**——任何本机回环代理只要漏传
  指定头，整站就免鉴权。现在 loopback 免令牌只在显式设置 `LG_LOCAL_BYPASS=1`
  时开启（默认关闭=本机访问也需令牌；本机开发带令牌的既有用法不受影响，
  serve_remote.sh 不设该开关）。启动自检（`self_check`）如实打印当前状态。
- **但要识别反向代理**：cloudflared 把隧道流量转到本机，`request.client.host` 会是 `127.0.0.1`。
  若只看 host 就会**把整条外网流量当本机放行**，鉴权形同虚设。
  故：只有在 host 是本机 **且没有** `CF-Connecting-IP` / `X-Forwarded-For` 时才免鉴权。
- **`?t=<token>` 一次性换 cookie 并跳回干净 URL** → 手机上点一下链接就长期免输；
  也避免令牌留在地址栏/历史/分享里。
  ⚠ 2026-09-19 修正：此前表单硬编码跳回 `/?t=`，从研究台 `/lab/*` 被拦时会跳到盲评台首页。
  现改为**换完跳回原路径**，并保留原查询参数（如 `?batch=` 这类批改队列参数）。
- 比较用 `hmac.compare_digest`，避免时序侧信道。

两档令牌（2026-09-25，审计 P1「评审令牌可调用任意文件导入」的权限面收口）：

审计 P1 第 5 条引用原话：单令牌下，`POST /corpus/import-file` 与
`POST /review/{id}/verdict` **共用同一令牌**——「评审者能导入任意（根内、白名单、
合规大小的）文件」在**权限分离意义上**仍成立。路径面（`_guard_import_path`）已封死，
但「评审者只该改判定、借令牌不该倍增权限」必须靠档位收口。本次实现两档：

- **评审档（review，既有）**：`REVIEW_TOKEN` env > `data/review_token.txt` >
  自动生成并持久化 + chmod 600。满足：全部只读页面 + 评审端点
  （`POST /review/{id}/verdict`、批改队列、取题 `?t=` 换 cookie 等）。
- **管理档（admin，新增）**：`ADMIN_TOKEN` env > `data/admin_token.txt` >
  自动生成并持久化 + chmod 600（与评审档同款读取协议，**绝不**回退复用评审令牌）。
  满足：评审档的全部权限 **＋** 建段/扩产入口（`POST /corpus/import-inbox`、
  `POST /corpus/import-file`、`POST /corpus/import-distiller`、
  `POST /experiments`、`POST /experiments/{id}/run`）。
- **包含关系**：管理档 ⊇ 评审档，反之不成立。即管理员不必拿两个令牌；
  评审令牌**不能**通过任何管理端点（含 `?t=` / cookie 等价路径）。
- **档位跟着 cookie 值走**：`?t=` 换 cookie 依旧一次换长期有效，cookie 里装的是
  令牌原文，每次请求按值与两档比较现算档位 ⇒ 评审 cookie 永远不会被自动升级成
  管理档。比较全部走 `hmac.compare_digest`。
- **`REVIEW_NO_AUTH=1` = 两档同时失效**（显式关闭鉴权，语义保持，见 `self_check`）。
- **兼容期**（无默认、不静默）：`LG_ADMIN_LEGACY_SHARED=1` 且未显式配置
  `ADMIN_TOKEN` 时，管理档共用评审档令牌——只在既有单令牌部署迁移期间用，
  打开时启动自检会响亮打印（见 `load_admin_token` / `self_check`）。
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

_ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = _ROOT / "data" / "review_token.txt"
ADMIN_TOKEN_PATH = _ROOT / "data" / "admin_token.txt"
COOKIE = "rv_token"
COOKIE_MAX_AGE = 90 * 24 * 3600          # 90 天
_LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")
# 反向代理注入的请求头：出现任一即说明请求来自隧道/代理，不得按本机放行
_PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip")


def load_token() -> str | None:
    """评审档令牌：优先环境变量；否则读文件；都没有则生成并持久化。
    返回 None 表示显式关闭鉴权（REVIEW_NO_AUTH=1）。"""
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


_ADMIN_LEGACY_ENV = "LG_ADMIN_LEGACY_SHARED"


def admin_legacy_shared() -> bool:
    """兼容期显式开关（默认关）：`LG_ADMIN_LEGACY_SHARED=1` 字面等值才算开。

    只在既有单令牌部署的迁移期开：此时未显式配置 `ADMIN_TOKEN`（env/文件都没有）
    则管理档**共用评审档令牌**。这不是静默回退——启动自检会响亮打印该开关。
    取值口径与 `LG_LOCAL_BYPASS` 一致：只有字面 "1" 算开。
    """
    return os.environ.get(_ADMIN_LEGACY_ENV) == "1"


def load_admin_token() -> str | None:
    """管理档令牌：与评审档同款读取协议，但**独立令牌**。

    优先级：`REVIEW_NO_AUTH=1` → None（两档同时失效）；
    `ADMIN_TOKEN` env > `data/admin_token.txt` > 兼容期开关（`LG_ADMIN_LEGACY_SHARED=1`
    时共用评审档令牌，仅那一种情况允许非独立令牌）> 自动生成并持久化。
    """
    if os.environ.get("REVIEW_NO_AUTH") == "1":
        return None
    env = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if env:
        return env
    if ADMIN_TOKEN_PATH.exists():
        t = ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if t:
            return t
    if admin_legacy_shared():
        return _TOKEN
    t = secrets.token_urlsafe(24)
    ADMIN_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_TOKEN_PATH.write_text(t + "\n", encoding="utf-8")
    try:
        ADMIN_TOKEN_PATH.chmod(0o600)
    except OSError:
        pass
    return t


_TOKEN = load_token()
_ADMIN_TOKEN = load_admin_token()
_PROXY_HEADERS_ACTIVE = False

# R2（审计残留 2026-09-23）：loopback 免令牌的**显式开关**。
# 默认关闭 = 本机回环请求也需令牌；只有显式 `LG_LOCAL_BYPASS=1` 才免。
# 取值口径从严：只有字面 "1" 算开（"0"/"yes"/"true" 一律不算——开关语义
# 必须无歧义，避免 "看起来像开了其实没开" 的对账事故）。
_LOCAL_BYPASS_ENV = "LG_LOCAL_BYPASS"


def local_bypass_enabled() -> bool:
    """loopback 免令牌是否显式开启（每次调用现读环境，测试可 monkeypatch）。

    严格等值比较，不 strip、不宽容大小写——" 1"/"01"/"yes" 都不算开。"""
    return os.environ.get(_LOCAL_BYPASS_ENV) == "1"


def _is_local(request: Request) -> bool:
    """本机直连（非经代理）**且** loopback 免令牌已显式开启。

    经代理的请求即便 host 是 127.0.0.1 也不算本机（代理头一票否决，
    即使 LG_LOCAL_BYPASS=1 也不豁免——回环代理漏传头不能变成全站免鉴权）。
    """
    host = request.client.host if request.client else ""
    if host not in _LOCAL_HOSTS:
        return False
    if any(h in request.headers for h in _PROXY_HEADERS):
        return False
    return local_bypass_enabled()


def _is_https(request: Request) -> bool:
    """请求是否经 HTTPS（隧道场景）。

    只看 `url.scheme` 不够——应用在 cloudflared 后面收到的是明文 HTTP，
    真正协议在 `X-Forwarded-Proto` 里。
    """
    if request.url.scheme == "https":
        return True
    return (request.headers.get("x-forwarded-proto") or "").lower() == "https"


def _grade(supplied: str | None) -> str | None:
    """档位判定：值匹配管理档令牌 → "admin"；匹配评审档令牌 → "review"；
    其余（含空/错误）→ None。比较恒用 `hmac.compare_digest`（防时序侧信道）。

    每次请求现读模块全局 `_ADMIN_TOKEN` / `_TOKEN`（测试可 monkeypatch）。
    管理档同时满足评审档：admin 命中优先返回，天然是 `admin ⊇ review`。
    """
    if not supplied:
        return None
    if _ADMIN_TOKEN is not None and hmac.compare_digest(supplied, _ADMIN_TOKEN):
        return "admin"
    if _TOKEN is not None and hmac.compare_digest(supplied, _TOKEN):
        return "review"
    return None


# 管理档专属端点（建段/扩产入口，POST 才认）：评审令牌一律 403。
# 评审端点 POST /review/{id}/verdict、/knowledge/query（只读）与全部 GET 只到评审档。
_ADMIN_ONLY_POST = ("/corpus/import-inbox", "/corpus/import-file",
                    "/corpus/import-distiller", "/experiments")


def requires_admin(method: str, path: str) -> bool:
    """该请求是否要求管理档令牌。

    - 只认 POST：文件导入 / distiller 导入 / 建实验 / run 实验 = 建段或扩产入口；
    - `/experiments/{id}/run` 以「前缀 + 以 /run 结尾」匹配，不漏子路径；
    - GET 一律返回 False（全部只读页面评审档即可）。
    """
    if method != "POST":
        return False
    if path in _ADMIN_ONLY_POST:
        return True
    return path.startswith("/experiments/") and path.endswith("/run")


_GATE_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>需要访问令牌</title></head>
<body style="font-family:-apple-system,'Segoe UI',sans-serif;background:#faf9f6;color:#2c2a26;
             display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0">
<form style="background:#fff;border:1px solid #e6e3dd;border-radius:12px;padding:26px 28px;
             box-shadow:0 2px 12px rgba(0,0,0,.05);width:300px" method="get"
      onsubmit="var u=new URL(location.href);u.searchParams.set('t',document.getElementById('t').value);
                location.replace(u.pathname+u.search+u.hash);return false">
  <div style="font-size:15px;font-weight:600;margin-bottom:4px">需要访问令牌</div>
  <div style="font-size:12.5px;color:#8d887d;margin-bottom:14px">本页面含私密内容，粘贴令牌后进入</div>
  <input id="t" autocomplete="off" autofocus  style="width:100%;box-sizing:border-box;padding:9px 11px;
         border:1px solid #dcd9d2;border-radius:8px;font-size:14px;font-family:inherit"
         placeholder="粘贴令牌">
  <button type="submit" style="width:100%;margin-top:12px;padding:10px;border:0;border-radius:8px;
          background:#2c2a26;color:#fff;font-size:14px;font-family:inherit;cursor:pointer">进入</button>
</form></body></html>"""


def self_check() -> None:
    """启动自检（R2 + 两档令牌）：如实打印鉴权面、loopback 免令牌开关状态、
    两档令牌状态、兼容期开关（若开）与各端点档位。

    绑定面（0.0.0.0 还是 loopback）归 uvicorn `--host` 管，应用进程内拿
    不到真实值，这里指明去哪看；serve_remote.sh 侧会另行打印它控制的
    `--host`。免令牌状态是应用侧能确知的事实，必须原样亮出来——审计
    残留 R2 的诉求就是「免令牌不许再是隐式默认」。
    """
    if _TOKEN is None:
        mode = ("鉴权=关闭（REVIEW_NO_AUTH=1，评审档与管理档**同时失效**"
                "——仅限本机开发/测试）")
    elif local_bypass_enabled():
        mode = ("鉴权=开启；loopback 免令牌=开启（LG_LOCAL_BYPASS=1）"
                "——本机直连免令牌，带代理头的请求仍需令牌")
    else:
        mode = ("鉴权=开启；loopback 免令牌=关闭（LG_LOCAL_BYPASS 未设）"
                "——本机访问也需令牌（?t=<token> 或 cookie）")
    print(f"[access] 启动自检：{mode}；绑定面看 uvicorn --host"
          f"（0.0.0.0=对外暴露，127.0.0.1=仅本机）", flush=True)
    print("[access] 档位：评审档 → 只读页面 + 评审端点"
          "（POST /review/*/verdict、批改队列 /review/batch/*、取题 "
          "/review/next、/experiments/*/review*）；"
          "管理档 → 同时满足评审档 + 管理端点（POST /corpus/import-inbox | "
          "import-file | import-distiller、POST /experiments、"
          "POST /experiments/*/run）", flush=True)
    print(f"[access] 令牌状态：REVIEW_TOKEN={'已配置' if _TOKEN else '已关闭'}；"
          f"ADMIN_TOKEN={'已配置' if _ADMIN_TOKEN else '已关闭'}；"
          f"LG_ADMIN_LEGACY_SHARED={'开' if admin_legacy_shared() else '关'}",
          flush=True)
    if admin_legacy_shared():
        print("[access] ⚠⚠ 兼容期显式开关：LG_ADMIN_LEGACY_SHARED=1 ——"
              " 未显式配置 ADMIN_TOKEN 时管理档**共用评审档令牌**。"
              " 迁移完成后请去掉该开关恢复两档分离。", flush=True)


def install(app) -> None:
    """挂上访问门。未配置令牌时完全不介入（本机使用行为不变）。"""
    self_check()
    if _TOKEN is None:
        return

    @app.middleware("http")
    async def _access_gate(request: Request, call_next):
        if _is_local(request):
            return await call_next(request)

        q_token = request.query_params.get("t")
        if q_token and _grade(q_token) is not None:
            # 用 ?t= 换 cookie，并跳回去掉参数的 URL——令牌不再留在地址栏/历史里。
            # Secure 只在 HTTPS 下打：局域网是明文 HTTP，打了 Secure 就发不出去，
            # 会把"局域网也能用"这条路弄坏。按请求实际协议动态决定。
            clean = request.url.remove_query_params("t")
            resp = RedirectResponse(url=str(clean), status_code=302)
            resp.set_cookie(COOKIE, q_token, max_age=COOKIE_MAX_AGE,
                            httponly=True, samesite="lax", secure=_is_https(request))
            return resp
        grade = _grade(request.cookies.get(COOKIE))
        if grade is None:
            return HTMLResponse(_GATE_HTML, status_code=401)
        if requires_admin(request.method, request.url.path) and grade != "admin":
            return PlainTextResponse(
                "该端点需要管理档令牌（ADMIN_TOKEN）。持有评审档令牌"
                "（REVIEW_TOKEN）不足以完成此操作。", status_code=403)
        return await call_next(request)
