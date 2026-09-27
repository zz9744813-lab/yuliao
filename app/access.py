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
- **2026-09-27（R2b）代理来源判定升为显式策略**：判定链抽成具名路由标签
  （`classify` / `access_route`）——`loopback_direct`（回环 + 无代理头，唯一可
  免令牌的前提）/ `trusted_proxy` / `untrusted_proxy` / `remote`（后三者一律需令牌）。
  新增 `LG_TRUSTED_PROXIES`（逗号分隔 IP/CIDR，**默认空=最严**）：回环 peer 带着
  代理头时，只有 peer 命中白名单才标 `trusted_proxy`，否则 `untrusted_proxy`——
  **两者同样要求令牌**，白名单只提供可观测性与审计线索，绝不放宽。
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
import ipaddress
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
# 启动自检里做实算演示用的回环 peer 样例（`_LOCAL_HOSTS` 的首项，纯展示用常量）
LOCAL_SAMPLE = "127.0.0.1"
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


# ── R2b（审计残留 2026-09-27）：可信代理白名单，从「隐式否决」升为「显式策略」 ──
#
# 改前的判定链只有一句「有代理头 ⇒ 不按本机放行」。它**行为上最严**，但部署方
# 看不出「我是不是正处在回环代理漏传头的风险面上」——白/灰/黑三态在日志里长得
# 一模一样。本次把判定抽成具名路由标签并新增 `LG_TRUSTED_PROXIES`：
#
#   peer 回环 + 无代理头           ⇒ via=loopback_direct  （唯一可免令牌的前提）
#   peer 回环 + 有代理头 + 在白名单 ⇒ via=trusted_proxy    （仍需令牌）
#   peer 回环 + 有代理头 + 不在     ⇒ via=untrusted_proxy  （仍需令牌）
#   peer 非回环                    ⇒ via=remote           （仍需令牌）
#
# **白名单不放宽任何一格**：trusted_proxy 与 untrusted_proxy 同样要求令牌，
# 区别只在标签可观测 + 启动自检能原样喊出「这条流量来自你声明的代理」。
# 环境变量未设（默认）⇒ 白名单为空 ⇒ 任何带代理头的请求都不免令牌。
# 绝不引入静默放宽，也没有任何 env 能把「需令牌」翻成「免令牌」
# （`LG_LOCAL_BYPASS` 的既有语义一字未改）。
_TRUSTED_PROXIES_ENV = "LG_TRUSTED_PROXIES"


def trusted_proxies() -> tuple[str, ...]:
    """`LG_TRUSTED_PROXIES` 的实际生效条目：逗号分隔的 IP / CIDR，逐项 strip。

    默认空 = 最严。空项（`,,` / 尾逗号 / 纯空白）直接丢弃，不产生「空串匹配一切」
    的口子。每次调用现读环境（测试可 monkeypatch），与 `local_bypass_enabled` 同协议。
    """
    raw = os.environ.get(_TRUSTED_PROXIES_ENV) or ""
    out = [p.strip() for p in raw.split(",")]
    return tuple(p for p in out if p)


def _parse_trusted_proxies() -> tuple[tuple[tuple[str, object], ...], tuple[str, ...]]:
    """把声明条目解析成 `(合法条目, 被忽略的非法条目原文)`。

    合法条目形如 `(原文, ip_network)`（单 IP 归一为 /32；**`strict=True`**，
    主机位被置位的写法如 `127.0.0.1/0` 视为非法——见下）。
    非法条目**剔除**而非放行；单独把它们的**原文 + 原因**带回，是为了让
    `self_check` 能如实喊出「声明 N 条 / 合法 M 条 / 忽略 K 条 + 具体哪几条及
    原因」——拼错或被归一放大的 IP 被静默忽略，正是要消灭的那类「看起来配了
    其实没生效」。
    """
    good: list[tuple[str, object]] = []
    bad: list[str] = []
    for entry in trusted_proxies():
        try:
            net = ipaddress.ip_network(entry, strict=True)
        except ValueError as e:
            # strict=True 拒绝「主机位被置位」的写法（如 `127.0.0.1/0`）。
            # 2026-09-27 会审（qwen 席）实测：旧写法 strict=False 会把
            # `127.0.0.1/0` **归一成 `0.0.0.0/0`**（匹配全部 IPv4），与本函数
            # 「容错方向只能是更严」的注释自相矛盾——一条手滑的条目就能把整个
            # v4 空间拉进白名单。改 strict 后这类条目落 bad，只被剔除、不放行。
            bad.append(f"{entry}（{e}）")
        else:
            good.append((entry, net))
    return tuple(good), tuple(bad)


def peer_is_trusted_proxy(host: str) -> bool:
    """`host` 是否命中 `LG_TRUSTED_PROXIES`（单 IP 精确匹配 / CIDR 包含）。

    非法条目（拼错的 IP、非 CIDR 文本）**一律不匹配**，绝不「解析失败就当全信」——
    白名单是收紧工具，容错方向只能是更严。一个坏条目不让其余合法条目失效。
    """
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False                      # peer 主机名/怪值：不在白名单内
    good, _bad = _parse_trusted_proxies()
    return any(addr in net for _raw, net in good)



def classify(host: str, has_proxy_headers: bool) -> str:
    """判定该请求的鉴权路由标签（与请求对象解耦的纯函数，便于自检直接调用）。

    标签即 `access_route` 的返回值域：
    `loopback_direct` / `trusted_proxy` / `untrusted_proxy` / `remote`。
    注意**只有** `loopback_direct` 是免令牌的前提，另三格一律需令牌——
    这条不变量由 `_is_local` 的等值比较机械保证。
    """
    if host not in _LOCAL_HOSTS:
        return "remote"
    if not has_proxy_headers:
        return "loopback_direct"
    if peer_is_trusted_proxy(host):
        return "trusted_proxy"
    return "untrusted_proxy"


def access_route(request: Request) -> str:
    """从真实请求取 peer + 代理头事实，交给 `classify` 出标签。"""
    host = request.client.host if request.client else ""
    return classify(host, any(h in request.headers for h in _PROXY_HEADERS))


def _is_local(request: Request) -> bool:
    """本机直连（非经代理）**且** loopback 免令牌已显式开启。

    经代理的请求即便 host 是 127.0.0.1 也不算本机（代理头一票否决，
    即使 LG_LOCAL_BYPASS=1 也不豁免——回环代理漏传头不能变成全站免鉴权）。
    """
    return access_route(request) == "loopback_direct" and local_bypass_enabled()


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
    """启动自检（R2 + R2b + 两档令牌）：如实打印鉴权面、loopback 免令牌开关状态、
    两档令牌状态、兼容期开关（若开）、`LG_TRUSTED_PROXIES` 实际取值、代理头出现时
    的判定结果与各端点档位。

    绑定面（0.0.0.0 还是 loopback）归 uvicorn `--host` 管，应用进程内拿
    不到真实值，这里指明去哪看；serve_remote.sh 侧会另行打印它控制的
    `--host`。免令牌状态是应用侧能确知的事实，必须原样亮出来——审计
    残留 R2 的诉求就是「免令牌不许再是隐式默认」。

    R2b 追加的诉求：白名单**原样**打印（未设即 `(未设)`，不许折叠成「无」之类
    的说法），并把「代理头出现时会怎么判」落成一行可机械核对的文字，
    使部署方一眼看出自己是否处在「回环代理漏传头 ⇒ 全站免鉴权」的风险面上。
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
    # ── R2b：可信代理白名单 + 代理头判定，原样可核对 ──────────────
    entries = trusted_proxies()
    good, bad = _parse_trusted_proxies()
    raw = (os.environ.get(_TRUSTED_PROXIES_ENV) or "").strip()
    print(f"[access] 可信代理白名单：LG_TRUSTED_PROXIES="
          f"{raw if raw else '(未设)'}（声明 {len(entries)} 条 / 合法 "
          f"{len(good)} 条 / 非法忽略 {len(bad)} 条）"
          + ("——默认最严：带代理头的请求一律需令牌"
             if not entries else
             "——仅用于标注代理来源，**不放宽**鉴权（仍需令牌）"),
          flush=True)
    if bad:
        print("[access] ⚠ 白名单有无法解析的条目（已按「永不匹配」处理，"
              f"不静默放过）：{'、'.join(bad)}", flush=True)

    # 用真实回环 peer 实算「有代理头 / 无代理头」两格，落成可机械核对的文字
    print(f"[access] 代理头判定（回环 peer 样例 {LOCAL_SAMPLE}）："
          f"带代理头 ⇒ via={classify(LOCAL_SAMPLE, True)}（需令牌）；"
          f"无代理头 ⇒ via={classify(LOCAL_SAMPLE, False)}"
          f"（免令牌的前提，但还需 LG_LOCAL_BYPASS=1 才真免）；"
          f"非回环 peer 203.0.113.9 ⇒ via={classify('203.0.113.9', True)}"
          f"（需令牌）；白名单是否覆盖该 peer："
          f"{'是' if peer_is_trusted_proxy(LOCAL_SAMPLE) else '否'}", flush=True)
    if local_bypass_enabled():
        print("[access] ⚠ loopback 免令牌=开启：正处于「回环反向代理漏传代理头 ⇒ "
              "整站免鉴权」的风险面。请确认绑定面不是 0.0.0.0，"
              "或去掉 LG_LOCAL_BYPASS=1。", flush=True)
    if admin_legacy_shared():
        print("[access] ⚠⚠ 兼容期显式开关：LG_ADMIN_LEGACY_SHARED=1 ——"
              " 未显式配置 ADMIN_TOKEN 时管理档**共用评审档令牌**。"
              " 迁移完成后请去掉该开关恢复两档分离。", flush=True)


def _tagged(resp, route: str):
    """给门自己产出的响应打上路由标签（`X-Access-Via`），使 `via=trusted_proxy`
    一类判定在**运行时可观测**，不必挂调试器。

    只回显分类结论、不回显 peer/令牌值；标签本身不参与鉴权判定（判定已在
    `classify` 里完成），故加不加它不改变任何一格放行结果。
    """
    resp.headers["X-Access-Via"] = route
    return resp


def install(app) -> None:
    """挂上访问门。未配置令牌时完全不介入（本机使用行为不变）。"""
    self_check()
    if _TOKEN is None:
        return

    @app.middleware("http")
    async def _access_gate(request: Request, call_next):
        if _is_local(request):
            return await call_next(request)
        route = access_route(request)          # 仅用于响应头打标签，不参与判定

        q_token = request.query_params.get("t")
        if q_token and _grade(q_token) is not None:
            # 用 ?t= 换 cookie，并跳回去掉参数的 URL——令牌不再留在地址栏/历史里。
            # Secure 只在 HTTPS 下打：局域网是明文 HTTP，打了 Secure 就发不出去，
            # 会把"局域网也能用"这条路弄坏。按请求实际协议动态决定。
            clean = request.url.remove_query_params("t")
            resp = RedirectResponse(url=str(clean), status_code=302)
            resp.set_cookie(COOKIE, q_token, max_age=COOKIE_MAX_AGE,
                            httponly=True, samesite="lax", secure=_is_https(request))
            return _tagged(resp, route)
        grade = _grade(request.cookies.get(COOKIE))
        if grade is None:
            return _tagged(HTMLResponse(_GATE_HTML, status_code=401), route)
        if requires_admin(request.method, request.url.path) and grade != "admin":
            return _tagged(PlainTextResponse(
                "该端点需要管理档令牌（ADMIN_TOKEN）。持有评审档令牌"
                "（REVIEW_TOKEN）不足以完成此操作。", status_code=403), route)
        return await call_next(request)
