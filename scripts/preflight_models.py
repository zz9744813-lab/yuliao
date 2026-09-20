"""模型名预检 —— 批量脚本开跑前先问一句「这名字网关认不认」（T-GUARD 2026-09-19）。

起因（`data/_dbg/DIAG_rescore_387.md`）：一轮 387 段的源校勘把 `LG_SOURCE_MODEL`
写成 `deepseek/deepseek-v4.1`，而池里只有 `deepseek-v4.1-flash`。网关对每个请求
回 503 `model_not_found`，387 条无一例外，白跑 7.4 分钟且 ok=0。开跑前问一次
`GET /models` 就能把 7.4 分钟变成 1 秒的报错，并把最接近的名字告诉你。

口径：
- 只校验**名字在不在池内**，不保证有额度/没下线（网关列表会骗人：minimax 已 410、
  agnes-pro 报 403，见 docs/HANDOVER.md 网关一节）。
- 本机桥接通道（`agy/` `qoder/` `wb/` `zcode/`）不经中转网关，池里没有属正常 → 放行。
- 池里 `vendor/model` 与 `model` 两种写法混用（`z-ai/glm-5.3` 与 `deepseek-v4.1-flash`
  并存），故比对时对 vendor 前缀做归一，避免误杀配置里在用的默认名。
- **绝不打印 API key**：key 只进请求头。

用法：
    python scripts/preflight_models.py moonshotai/kimi-k3       # 池内 → exit 0
    python scripts/preflight_models.py deepseek/deepseek-v4.1   # 池外 → exit 2 + 候选
    # 网关不可达 / 未配置 → exit 3
"""
from __future__ import annotations

import difflib
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import config  # noqa: E402  # 网关地址/密钥的读取口径与全项目一致（.env 走 config）

# /models 只是列名字，秒级就该回；卡住等于不可达（批量脚本不该为预检等 5 分钟，
# 所以不复用 LG_HTTP_TIMEOUT_S=300 那档）
TIMEOUT_S = 30.0
N_CANDIDATES = 3

EXIT_OK = 0
EXIT_NOT_IN_POOL = 2
EXIT_UNREACHABLE = 3

# 本机 CLI 桥接通道：不走中转网关，所以不在 /models 池内（前缀表由测试锁定同源）
LOCAL_BRIDGE_PREFIXES = ("agy/", "qoder/", "wb/", "zcode/")


class GatewayUnreachable(RuntimeError):
    """拿不到模型池（网络/网关报错/配置缺失）——预检做不成，不等于模型名合法。"""


@dataclass
class ModelCheck:
    name: str
    ok: bool
    reason: str = ""
    candidates: tuple[str, ...] = ()
    pool_size: int = 0
    # 只有"去掉 vendor 前缀才命中"时为真：网关要的是**池内原样**的 id，
    # 这种写法照样可能 503（第五批扩产就是这一型），所以单独标出来给调用方显形。
    bare_hit: bool = False
    pool_hint: str = ""


def _bare(name: str) -> str:
    """去掉 vendor 前缀：`deepseek/deepseek-v4.1-flash` → `deepseek-v4.1-flash`。"""
    return (name or "").rsplit("/", 1)[-1]


def is_local_bridge(name: str) -> bool:
    return any((name or "").startswith(x) for x in LOCAL_BRIDGE_PREFIXES)


def _gateway() -> tuple[str, str]:
    base = (config.GATEWAY_BASE_URL or "").rstrip("/")
    key = config.GATEWAY_API_KEY or ""
    if not base or not key:
        raise GatewayUnreachable("LG_GATEWAY_BASE_URL / LG_GATEWAY_API_KEY 未配置（项目 .env）")
    return base, key


def fetch_models() -> list[str]:
    """网关模型池名单。任何拿不到池的情况一律抛 GatewayUnreachable（不猜、不放行）。"""
    base, key = _gateway()
    try:
        with httpx.Client(timeout=TIMEOUT_S) as cli:
            resp = cli.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        raise GatewayUnreachable(f"GET {base}/models 失败：{type(e).__name__}") from e
    if resp.status_code != 200:
        raise GatewayUnreachable(f"GET {base}/models → HTTP {resp.status_code}")
    try:
        items = (resp.json() or {}).get("data") or []
    except ValueError as e:
        raise GatewayUnreachable("网关 /models 返回的不是 JSON") from e
    pool = [str(x.get("id")) for x in items if isinstance(x, dict) and x.get("id")]
    if not pool:
        raise GatewayUnreachable("网关 /models 返回空池（列表异常，无法据此判断）")
    return pool


def nearest(name: str, pool: list[str], k: int = N_CANDIDATES) -> list[str]:
    """池内与 `name` 最接近的 k 个（原名与去 vendor 前缀名都比一遍，取更高相似度）。

    排序对 (相似度, 名字) 做确定性 tie-break：同分时结果不随池顺序/字典实现漂移。
    """
    scored = []
    target, tbare = (name or "").lower(), _bare(name).lower()
    for m in pool:
        ratio = max(difflib.SequenceMatcher(None, target, m.lower()).ratio(),
                    difflib.SequenceMatcher(None, tbare, _bare(m).lower()).ratio())
        scored.append((round(ratio, 4), m))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [m for _r, m in scored[:k]]


def check_model(name: str) -> ModelCheck:
    """问一句：`name` 能不能拿去跑批量。池外时附最接近的 3 个候选。

    拿不到池 → 抛 GatewayUnreachable（CLI 据此 exit 3），绝不静默放行。
    """
    name = (name or "").strip()
    if not name:
        return ModelCheck(name, False, "模型名为空（LG_SOURCE_MODEL 覆盖成了空串？）")
    if is_local_bridge(name):
        return ModelCheck(name, True, reason="本机桥接通道，不经中转网关池 → 预检跳过")
    pool = fetch_models()
    if name in pool:
        return ModelCheck(name, True, pool_size=len(pool))
    hit = next((m for m in pool if _bare(m) == _bare(name)), None)
    if hit:
        return ModelCheck(name, True, reason=f"按去 vendor 前缀命中池内 `{hit}`",
                          pool_size=len(pool), bare_hit=True, pool_hint=hit)
    return ModelCheck(name, False,
                      reason=f"网关模型池里没有 `{name}`（池内 {len(pool)} 个）",
                      candidates=tuple(nearest(name, pool)), pool_size=len(pool))


def describe(c: ModelCheck) -> str:
    """人看的单行结论（含候选名）。"""
    if c.ok:
        pool = f"（池内 {c.pool_size} 个）" if c.pool_size else ""
        return f"OK：`{c.name}` 可用{pool}" + (f" — {c.reason}" if c.reason else "")
    line = f"不可用：{c.reason}"
    if c.candidates:
        line += "；最接近的候选：" + ", ".join(c.candidates)
    return line


def redact(text, limit: int = 300) -> str:
    """把网关错误原文压成"可直接贴进汇报"的一行：去 key、压平换行、截断。"""
    t = " ".join(str(text or "").split())
    key = (config.GATEWAY_API_KEY or "").strip()
    if key and key in t:
        t = t.replace(key, "***")
    return t[:limit] + ("…" if len(t) > limit else "")


def classify_llm_failure(error: str | None) -> str:
    """非 ok 调用的错误分类（bal-v2 批次验收的判据来源，规格见
    docs/proposal-length-balanced-regen-20260920.md §3 前置探针行）。

    返回 "503" / "failed_parse" / "other"：
    · 503 族——错误含 503 / model_not_found / no available channel /
      无可用渠道（09-19 死 id 事故的原文形状）；
    · failed_parse 族——错误为空（无从诊断按解析失败计）或含 parse；
    · 其余 other。
    批次验收先决判据：**503 计数=0，非 ok 只许 failed_parse**——
    任何 503/other 都意味着批次口径被污染（见 batch_llm_health）。
    """
    t = str(error or "").strip()
    low = t.lower()
    if ("503" in t or "model_not_found" in low
            or "no available channel" in low or "无可用渠道" in t):
        return "503"
    if not t or "parse" in low:
        return "failed_parse"
    return "other"


def batch_llm_health(rows) -> tuple[bool, str]:
    """批次验收判据：全 ok 或非 ok 只 failed_parse → (True, 汇总)；
    出现任何 503 / other → (False, 首条污染原文[已过 redact]）。

    rows：带 .status 与 .error 的对象序列（LlmCall 同形）。
    failed_parse 的认定走双通道：error 文本含 parse 族，或 status 本身
    记为 failed_parse（有的管线把家族写进 status——两种都算解析失败）。
    """
    n_failed_parse = 0
    for r in rows:
        if r.status == "ok":
            continue
        if (classify_llm_failure(r.error) == "failed_parse"
                or "parse" in str(r.status or "").lower()):
            n_failed_parse += 1
            continue
        return False, f"非 ok 非 failed_parse：{classify_llm_failure(r.error)}: {redact(r.error, 160)}"
    return True, f"ok（failed_parse {n_failed_parse} 条）"


def preflight_block(models, *, source: str = "") -> str | None:
    """批量脚本开跑前的统一闸门：None=可跑，否则返回拒绝理由（含最接近候选）。

    调用方自己决定怎么收场（CLI 用 require_models；返回 dict 的 run() 用它置 aborted）。
    mock 模式根本不发网络调用（gateway 回确定性伪输出），无从校验也无需校验 → 放行。
    拿不到池按**不放行**处理，与 check_model 同口径（不猜）。

    "去 vendor 前缀才命中"这一类**不改判定**（放宽是既定口径，见模块 docstring：
    池里两种写法混用，收紧会误杀在用的配置），但会把原名打给用户——第五批扩产的
    503 就是这个形状：`deepseek/deepseek-v4.1-flash` 去前缀能对上池里的名字，
    预检当时会说"OK"，网关却照收 503。判定不变、原因显形，才既不误杀也看得见。
    """
    if config.LLM_MODE == "mock":
        return None
    msgs, soft, seen = [], [], set()
    for name in models:
        name = (name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            c = check_model(name)
        except GatewayUnreachable as e:
            msgs.append(f"拿不到网关模型池，无法确认 `{name}`：{e}")
            continue
        if not c.ok:
            msgs.append(describe(c))
        elif c.bare_hit and config.canonical_model(name) == name:
            # 别名表覆盖到的名字走的是出口归一（发出去的就是池内名），不必再提醒
            soft.append(f"`{name}` 不在池内原样名单里，靠去 vendor 前缀命中 `{c.pool_hint}`"
                        f"——若这轮报 model_not_found，改成 `{c.pool_hint}`")
    for line in soft:
        print(f"[预检提示] {source or '?'}：{line}", file=sys.stderr)
    if not msgs:
        return None
    return (f"[{source}] " if source else "") + "；".join(msgs)


def require_models(models, *, source: str = "") -> None:
    """CLI 入口用：池外/拿不到池 → 一秒退出（exit 2），绝不让整轮批量白跑。

    2026-09-19 的 387 段源校勘与 09-20 的第五批扩产都是同一形状：模型名不在池内
    → 100% 503 → 跑完才看得见"failed"，原因要人工翻 DB 才找到。
    """
    blocked = preflight_block(models, source=source)
    if blocked:
        print(f"[预检失败] 未开跑：{blocked}", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    names = list(sys.argv[1:] if argv is None else argv)
    if not names:
        print("用法：python scripts/preflight_models.py <model> [<model>...]")
        return 1
    code = EXIT_OK
    for name in names:
        try:
            c = check_model(name)
        except GatewayUnreachable as e:
            print(f"[预检] {name}: 网关不可达 —— {e}")
            return EXIT_UNREACHABLE
        print(f"[预检] {describe(c)}")
        if not c.ok:
            code = EXIT_NOT_IN_POOL
    return code


if __name__ == "__main__":
    sys.exit(main())
