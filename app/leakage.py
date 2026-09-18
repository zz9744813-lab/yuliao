"""泄漏检测四层半：

1. char6_containment   连续 6 字以上重合的覆盖率（兜底、确定性）
2. word3_containment   词级 3-gram 覆盖率（jieba 可选，无则退化为字级 4-gram）
3. rare_phrase         按实验内 IDF 加权的重合度，专杀“罕见短语被偷用”
4. adversarial         盲眼强模型仅凭 Frame 尝试还原原句（最终裁决）
5. embedding           预留：配置 LG_EMBED_* 后启用，未配置时跳过

分数统一 0~1，越高越像偷了原文。
"""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from typing import Iterable

from . import config
from .gateway import LLMError, chat
from .prompt_render import render

CHAR_N = 6
WORD_N = 3
RARE_CHAR_N = 5

_re_non_cjk = re.compile(r"[^一-鿿0-9A-Za-z]+")


def _norm(text: str) -> str:
    return _re_non_cjk.sub("", text)


def char_ngrams(text: str, n: int = CHAR_N) -> set[str]:
    t = _norm(text)
    if len(t) < n:
        return {t} if t else set()
    return {t[i : i + n] for i in range(len(t) - n + 1)}


def _word_tokens(text: str) -> list[str]:
    try:
        import jieba  # type: ignore
        return [w for w in jieba.cut(text) if w.strip()]
    except Exception:
        t = _norm(text)
        return [t[i : i + 4] for i in range(max(len(t) - 3, 1))]  # 退化字级 4-gram


def _word_ngrams(text: str, n: int = WORD_N) -> set[tuple[str, ...]]:
    toks = _word_tokens(text)
    if len(toks) < n:
        return {tuple(toks)} if toks else set()
    return {tuple(toks[i : i + n]) for i in range(len(toks) - n + 1)}


def containment(a: Iterable, b: Iterable) -> float:
    sa, sb = set(a), set(b)
    if not sa:
        return 0.0
    return round(len(sa & sb) / len(sa), 4)


# ── layer 1/2：确定性 ────────────────────────────────────────

def char_layer(frame_repr: str, source_text: str) -> dict:
    return {
        "score": containment(char_ngrams(frame_repr), char_ngrams(source_text)),
        "n_grams_frame": len(char_ngrams(frame_repr)),
    }


def word_layer(frame_repr: str, source_text: str) -> dict:
    return {"score": containment(_word_ngrams(frame_repr), _word_ngrams(source_text))}


# ── layer 3：IDF 加权的罕见短语 ──────────────────────────────

class RarePhraseIndex:
    """以整个实验段集合为分词背景，计算每个 char-RARE_CHAR_N-gram 的 IDF。"""

    def __init__(self, corpus_texts: list[str]):
        df: Counter[str] = Counter()
        for t in corpus_texts:
            for g in char_ngrams(t, RARE_CHAR_N):
                df[g] += 1
        self.N = max(len(corpus_texts), 1)
        self.df = df

    def idf(self, gram: str) -> float:
        return math.log((self.N + 1) / (1 + self.df.get(gram, 0)))

    def weighted_containment(self, frame_repr: str, source_text: str) -> dict:
        fg = char_ngrams(frame_repr, RARE_CHAR_N)
        sg = char_ngrams(source_text, RARE_CHAR_N)
        if not fg:
            return {"score": 0.0, "top_hits": []}
        common = fg & sg
        numerator = sum(self.idf(g) for g in common)
        denominator = sum(self.idf(g) for g in fg)
        top = sorted(common, key=lambda g: -self.idf(g))[:10]
        return {
            "score": round(numerator / max(denominator, 1e-9), 4),
            "top_hits": top,
        }


# ── layer 4：对抗还原（LLM）─────────────────────────────────

ADVERSARIAL_PROMPT_VERSION = "adversarial_recon_v1"

ADVERSARIAL_PROMPT = """你是一名熟悉中文小说的编辑。下面给你一个从某段已出版中文小说里抽取的"语义骨架"（SemanticFrame）。

请你仅凭这个骨架，写出你心目中原作者最可能写出的原句。不要解释，直接输出 K 个不同还原版本，每行一个。

骨架：
«{frame_json}»

输出格式：
V1: ...
V2: ...
...
V{K}: ..."""


def adversarial_layer(frame_repr: str, source_text: str, k: int = 8) -> dict:
    if config.LLM_MODE == "mock":
        return {
            "score": 0.0, "max_sim": 0.0, "mean_sim": 0.0,
            "restorations": ["(mock)"], "attempts": 0, "status": "skipped_mock",
        }
    prompt = render(ADVERSARIAL_PROMPT, frame_json=frame_repr, K=k)
    try:
        r = chat(
            model=config.STRONG_MODEL,
            system="你只做一道题：读骨架猜原文。",
            user=prompt,
            purpose="adversarial_recon",
            prompt_version=ADVERSARIAL_PROMPT_VERSION,
            temperature=0.7,
            max_tokens=2500,
        )
    except LLMError as e:
        return {"score": 0.0, "status": "failed", "error": str(e), "attempts": 0}

    lines = [ln for ln in (r.text or "").splitlines() if ln.strip()]
    restorations = []
    for ln in lines:
        ln = re.sub(r"^V\d+[:：]\s*", "", ln.strip())
        if ln:
            restorations.append(ln)
    if not restorations:
        return {"score": 0.0, "status": "empty", "attempts": 0, "raw": r.text[:400]}

    sims = []
    for rest in restorations:
        # 双向指标：字级 n-gram 覆盖 + difflib 长度归一
        a = containment(char_ngrams(rest, CHAR_N), char_ngrams(source_text, CHAR_N))
        b = difflib.SequenceMatcher(None, _norm(rest), _norm(source_text)).ratio()
        sims.append(max(a, b))
    top = sorted(zip(sims, restorations), key=lambda x: -x[0])
    return {
        "score": round(max(sims), 4),
        "max_sim": round(max(sims), 4),
        "mean_sim": round(sum(sims) / len(sims), 4),
        "attempts": len(restorations),
        "restorations": [t[1][:60] for t in top[:3]],
        "status": "ok",
    }


# ── embedding（任务四落地）──────────────────────────────────
# 两个后端：
#   1) 网关 /embeddings（配置了 LG_EMBEDDING_MODEL 才启用）——真语义向量
#   2) hashing 代理：char-bigram 特征哈希 256 维余弦——纯确定性，本质是
#      "词汇分布相似度"，near_dup 判定只信 gateway 后端
import hashlib as _hl
import math as _math

_EMB_CACHE: dict[str, list[float]] = {}
_EMB_DIM = 256
_LOCAL_MODEL = None


def _local_embed(texts: list[str]) -> list[list[float]] | None:
    """本地 bge-small-zh（fastembed/ONNX，离线）。HF 被墙时用 HF_ENDPOINT=hf-mirror 预下载。"""
    global _LOCAL_MODEL
    try:
        from fastembed import TextEmbedding
    except Exception:
        return None
    missing = [t for t in texts if t not in _EMB_CACHE]
    if missing:
        try:
            if _LOCAL_MODEL is None:
                _LOCAL_MODEL = TextEmbedding("BAAI/bge-small-zh-v1.5")
            for t, v in zip(missing, _LOCAL_MODEL.embed(missing)):
                _EMB_CACHE[t] = [float(x) for x in v]
        except Exception:
            return None
    if any(t not in _EMB_CACHE for t in texts):
        return None
    return [_EMB_CACHE[t] for t in texts]


def _hash_vec(text: str) -> list[float]:
    t = re.sub(r"\s+", "", text or "")
    v = [0.0] * _EMB_DIM
    grams = [t[i:i + 2] for i in range(max(0, len(t) - 1))]
    if not grams:
        return v
    for g in grams:
        h = int(_hl.md5(g.encode()).hexdigest()[:8], 16)
        v[h % _EMB_DIM] += 1.0
    n = _math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _gateway_embed(texts: list[str]) -> list[list[float]] | None:
    if not getattr(config, "EMBEDDING_MODEL", None) or not config.GATEWAY_BASE_URL \
            or not config.GATEWAY_API_KEY:
        return None
    import httpx
    missing = [t for t in texts if t not in _EMB_CACHE]
    if missing:
        try:
            with httpx.Client(timeout=60) as cli:
                r = cli.post(f"{config.GATEWAY_BASE_URL}/embeddings",
                             headers={"Authorization": f"Bearer {config.GATEWAY_API_KEY}"},
                             json={"model": config.EMBEDDING_MODEL, "input": missing})
            if r.status_code != 200:
                return None
            data = r.json()["data"]
            for t, d in zip(missing, data):
                _EMB_CACHE[t] = d["embedding"]
        except Exception:
            return None
    if any(t not in _EMB_CACHE for t in texts):
        return None
    return [_EMB_CACHE[t] for t in texts]


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = _math.sqrt(sum(x * x for x in a)) or 1.0
    nb = _math.sqrt(sum(x * x for x in b)) or 1.0
    return num / (na * nb)


def embed_similarity(a: str, b: str) -> dict:
    # 后端优先级：本地真向量 > 网关 > hashing 代理（仅 diagnostic，不参与判定）
    vecs = _local_embed([a, b])
    if vecs is not None:
        return {"backend": "local:bge-small-zh-v1.5",
                "similarity": round(_cos(vecs[0], vecs[1]), 4)}
    vecs = _gateway_embed([a, b])
    if vecs is not None:
        return {"backend": f"gateway:{config.EMBEDDING_MODEL}",
                "similarity": round(_cos(vecs[0], vecs[1]), 4)}
    return {"backend": "hash-surrogate",
            "similarity": round(_cos(_hash_vec(a), _hash_vec(b)), 4)}


def embedding_layer(frame_repr: str, source_text: str) -> dict:
    out = embed_similarity(frame_repr, source_text)
    out["score"] = out["similarity"]
    return out


def composite_score(layers: dict[str, dict]) -> float:
    """把确定性层的分数合成一个保守下界（对抗还原单独解读，不进这个分数）。"""
    scores = [layers[k]["score"] for k in ("char6", "word3", "rare") if k in layers]
    return round(max(scores), 4) if scores else 0.0
