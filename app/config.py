"""全局配置：环境变量 + 项目根目录 .env。不发依赖 pydantic-settings 的复杂结构。

优先级：真实环境变量 > 项目根 .env > 本文件默认值。

- LG_DATABASE_URL    默认 SQLite 文件；切 Postgres 时给 postgresql+psycopg://...
- LG_LLM_MODE        real | mock（mock 不调用网络，供 dry-run / 测试）
- LG_GATEWAY_BASE_URL / LG_GATEWAY_API_KEY   OpenAI 兼容网关中转
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LG_DATA_DIR", str(ROOT / "data")))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(ROOT / ".env")

LLM_MODE = os.environ.get("LG_LLM_MODE", "real")  # real | mock
DATABASE_URL = os.environ.get(
    "LG_DATABASE_URL", f"sqlite:///{(DATA_DIR / 'language_genome.db').as_posix()}"
)

GATEWAY_BASE_URL = os.environ.get("LG_GATEWAY_BASE_URL", "").rstrip("/")
GATEWAY_API_KEY = os.environ.get("LG_GATEWAY_API_KEY", "")

# 重建候选默认模型池（网关实测可用）
# 2026-09-14：移除 glm-5.3（该模型已不可用，留在池里会产出 failed 调用）
# 2026-09-14：加入 agnes-3.0-flash（用户指定；网关 /models 确认存在，是 agnes 系最新 flash）
# 2026-09-20（P0）：中转池内在册的是**无前缀** `deepseek-v4.1-flash`；带 `deepseek/`
# 前缀的旧 id 已下线（503 model_not_found，09-19 20:00 起全线死）。全仓默认模型
# 收敛到本常量（环境变量 LG_LLM_MODEL 可覆盖），不许再散落字面量。
DEFAULT_LLM_MODEL = os.environ.get("LG_LLM_MODEL", "deepseek-v4.1-flash")
# 已下线的历史 id → 在册 id。历史 JudgeRun/Candidate 行里存的是旧名：
# ①查历史数据必须 model_any()（新旧都查）；②写新数据一律 canonical_model()；
# ③按 model 去重/配对的池一律先 canonical 再比，防同一模型新旧名出现两份。
DEAD_MODEL_ALIASES = {"deepseek/deepseek-v4.1-flash": DEFAULT_LLM_MODEL}


def canonical_model(mid: str) -> str:
    """写入侧统一 id：死别名归一到在册名，其余原样。"""
    return DEAD_MODEL_ALIASES.get((mid or "").strip(), (mid or "").strip())


def model_any(mid: str) -> tuple[str, ...]:
    """查询侧兼容元组：给 `.in_()` 用——canonical 名 + 它的所有历史别名。

    契约（调用方据此写 SQL/ORM）：恒返回**非空** tuple[str]，元素均为 str。
    空入参返回 `("",)` 而非 ()——`model in ()` 在 SQL 里是语法错误，
    `model in ("")` 是安全的不命中；None/空 model 不抛异常。
    """
    mid = (mid or "").strip()
    if not mid:
        return ("",)
    ids = {mid, canonical_model(mid)}
    ids |= {k for k, v in DEAD_MODEL_ALIASES.items() if v == canonical_model(mid)}
    out = tuple(sorted(str(x) for x in ids if x))
    return out or ("",)


DEFAULT_RECON_MODELS = [
    m.strip()
    for m in os.environ.get(
        "LG_RECON_MODELS",
        f"{DEFAULT_LLM_MODEL},z-ai/glm-5.3,"
        "moonshotai/kimi-k3,agnes-3.0-flash",
    ).split(",")
    if m.strip()
]
# 抽取 / 对抗还原 / 命题分解 用的强模型
STRONG_MODEL = os.environ.get("LG_STRONG_MODEL", "moonshotai/kimi-k3")
# Frame 抽取器（双抽对照时取前两个）
EXTRACTOR_MODELS = [
    m.strip()
    for m in os.environ.get(
        "LG_EXTRACTOR_MODELS", f"{STRONG_MODEL},{DEFAULT_LLM_MODEL}"
    ).split(",")
    if m.strip()
]

# LLM 调用默认值
DEFAULT_TEMPERATURES = [0.3, 0.6, 0.9, 1.1]
# 推理系模型单次调用可能到 3 分钟以上；90s 会把长思考误杀成 timeout
HTTP_TIMEOUT_S = float(os.environ.get("LG_HTTP_TIMEOUT_S", "300"))
MAX_RETRIES = int(os.environ.get("LG_HTTP_MAX_RETRIES", "3"))
# 网关 /embeddings 的模型名；留空 = 用 hashing 代理（见 app/leakage.embed_similarity）
EMBEDDING_MODEL = os.environ.get("LG_EMBEDDING_MODEL", "").strip() or None

# 判价：中转站单价未知 → cost 记录为 None（沿用 distiller 约定），token 如实记录

# ── 盲评候选白名单 ────────────────────────────────────────────
# 盲评的 A/B 必须是**同一段语义的两种写法**，否则"哪边更好"这个问题本身不成立。
# 实测事故（2026-09-14，集霸发现"牛头不对马嘴"）：`recon_ctxonly_v1` 是"只给上下文、
# 不给 SemanticFrame"的消融条件，模型会**自由续写下一个场景**，与人类段根本不是同一内容；
# 混进盲评后 A 在讲"献出妹妹换命"、B 在讲"钟华弹了俞小塘额头"。
#
# 用**白名单**而非黑名单：新增 prompt 版本若忘记登记，后果是"该版本进不了盲评"（少题，安全），
# 而不是"非平行文本静默混入盲评"（污染全部 agreement/κ 数字，危险）。
BLIND_REVIEW_PROMPT_VERSIONS = ("reconstruct_v1", "recon_ctx_v1")

# 可端上评审台的平行文本口径 = 主池 + 受控劣化（总方案 §7 工作流 B）。
# 为什么拆两个元组而不是直接往上面加：上面那个同时是**抽样池**过滤器
# （make_random_batch / make_stratified_batch / select_harvest_batch 用它圈候选池）。
# 劣化候选是"人类原文的劣化版"，胜率天然接近 0；混进随机池会把候选胜率/κ 整体拽偏。
#   BLIND_REVIEW_PROMPT_VERSIONS → 抽样池（永不含 corrupt_v1）
#   SERVABLE_PROMPT_VERSIONS     → 按批次显式取题时可端出
SERVABLE_PROMPT_VERSIONS = BLIND_REVIEW_PROMPT_VERSIONS + ("corrupt_v1", "corrupt_v2")


def is_servable_pv(pv: str) -> bool:
    """该候选口径能不能端上评审台。

    ⚠ 用**前缀**兜住 `corrupt_*` 整族：生成口径会随类型定义修正而升版
    （corrupt_v1→v2 就是实例），每升一次都要回来改白名单的话，迟一次就是
    "批次里明明有题、取题接口说已清空"的静默事故——2026-09-18 实际发生过：
    集霸判到 12/24 时，剩下 12 题的候选是 corrupt_v2，对他"不存在"。
    抽样池（BLIND_REVIEW_PROMPT_VERSIONS）仍然**不含** corrupt_*，隔离不受影响。
    """
    return pv in SERVABLE_PROMPT_VERSIONS or (pv or "").startswith("corrupt_")
