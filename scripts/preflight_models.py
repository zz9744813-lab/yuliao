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
        return ModelCheck(name, True, reason=f"按去 vendor 前缀命中池内 `{hit}`", pool_size=len(pool))
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
