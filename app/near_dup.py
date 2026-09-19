"""Train/Benchmark 近重复检测器（任务九）。

目的：防止"训练用的 Segment 与基准评测的 Segment 是近重复"——否则标定结论是
背书不是泛化（工程方案 §near-dup 的要求）。

四层 + 可选第五层（与泄漏检测同一套 embedding 后端）：
  exact      归一化后 sha256 全等
  ngram      char 6-gram Jaccard
  minhash    128 维 MinHash 估 Jaccard（纯 Python，无三方依赖）
  simhash    64-bit SimHam（char 3-gram 特征），汉明距离
  embedding  见 app/leakage.embed_similarity（网关可用则真向量，否则 hashing 代理）

is_near_dup() 任何一层过阈值即 True（保守：宁可误杀）。
基准隔离：Segment.role = "benchmark" 的段禁止进入任何 train 采样（create_experiment 过滤）。
corpus v2 隔离：v1 的 TYPO_MAP 修复镜像段（Work.v2_of）同文双份，禁止进入采样域与基准切分。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from . import config, db
from .models import Segment, exclude_corpus_v2_segments

_NGRAM = 6
_MH_PERMS = 128
_SIMHASH_BITS = 64

_THRESHOLDS = {"exact": 1.0, "ngram": 0.15, "minhash": 0.15, "simhash": 12}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _ngrams(text: str, n: int) -> set[str]:
    t = _norm(text)
    return {t[i:i + n] for i in range(0, max(0, len(t) - n + 1))}


def exact_layer(a: str, b: str) -> float:
    ha = hashlib.sha256(_norm(a).encode()).hexdigest()
    hb = hashlib.sha256(_norm(b).encode()).hexdigest()
    return 1.0 if ha == hb else 0.0


def ngram_layer(a: str, b: str, n: int = _NGRAM) -> float:
    ga, gb = _ngrams(a, n), _ngrams(b, n)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _mh_signature(text: str) -> list[int]:
    grams = _ngrams(text, 3)
    if not grams:
        return [1 << 30] * _MH_PERMS
    sig = []
    for i in range(_MH_PERMS):
        m = min(int(hashlib.sha1(f"{i}:{g}".encode()).hexdigest()[:8], 16) for g in grams)
        sig.append(m)
    return sig


def minhash_layer(a: str, b: str) -> float:
    sa, sb = _mh_signature(a), _mh_signature(b)
    eq = sum(1 for x, y in zip(sa, sb) if x == y)
    return eq / _MH_PERMS


def _simhash(text: str) -> int:
    feats = Counter(_norm(text)[i:i + 3] for i in range(max(0, len(_norm(text)) - 2)))
    v = [0] * _SIMHASH_BITS
    for f, w in feats.items():
        h = int(hashlib.md5(f.encode()).hexdigest(), 16)
        for i in range(_SIMHASH_BITS):
            v[i] += w if (h >> i) & 1 else -w
    out = 0
    for i, x in enumerate(v):
        if x > 0:
            out |= 1 << i
    return out


def simhash_layer(a: str, b: str) -> float:
    """返回汉明距离（0=全同；<=12 视为近重复）。"""
    ha, hb = _simhash(a), _simhash(b)
    return float(bin(ha ^ hb).count("1"))


def embedding_layer(a: str, b: str) -> dict:
    from .leakage import embed_similarity
    return embed_similarity(a, b)


def near_dup_report(a: str, b: str, use_embedding: bool = True) -> dict:
    out = {
        "exact": exact_layer(a, b),
        "ngram": round(ngram_layer(a, b), 4),
        "minhash": round(minhash_layer(a, b), 4),
        "simhash_hamming": simhash_layer(a, b),
    }
    emb_hit = False
    if use_embedding:
        e = embedding_layer(a, b)
        out["embedding"] = {"backend": e.get("backend"), "similarity": e.get("similarity")}
        # hashing 代理只是词汇分布相似度，不参与判定；真向量才允许一票命中
        emb_hit = (str(e.get("backend", "")).startswith("gateway")
                   and (e.get("similarity") or 0) >= 0.85)
    out["is_near_dup"] = bool(
        out["exact"] >= _THRESHOLDS["exact"]
        or out["ngram"] >= _THRESHOLDS["ngram"]
        or out["minhash"] >= _THRESHOLDS["minhash"]
        or out["simhash_hamming"] <= _THRESHOLDS["simhash"]
        or emb_hit)
    return out


# ── 基准隔离 ────────────────────────────────────────────────

def split_benchmark(work_id: str | None = None, n: int = 200, seed: int = 99118,
                    exclude_ids: list[str] | None = None) -> dict:
    """从已回填 integrity 的段里抽 n 段标为 benchmark（role='benchmark'）。

    只在尚未有 benchmark 时执行一次；实验已用过的段（exclude_ids）不能进基准。
    """
    import random
    with db.session() as s:
        # corpus v2 镜像段永不入基准：v1/v2 同文，双份入基准=基准被污染
        q = s.query(Segment).filter(Segment.role.is_(None),
                                    exclude_corpus_v2_segments())
        if work_id:
            q = q.filter(Segment.work_id == work_id)
        pool = q.all()
        if exclude_ids:
            pool = [x for x in pool if x.id not in set(exclude_ids)]
        picked = random.Random(seed).sample(pool, min(n, len(pool)))
        for x in picked:
            x.role = "benchmark"
        s.commit()
        return {"marked": len(picked), "pool": len(pool)}


def train_sampling_pool(s, work_ids: list[str] | None = None,
                        seg_version: int | None = None,
                        eligible_only: bool = False) -> list[Segment]:
    """create_experiment 的合法采样域：role 不得是 benchmark；可按切分版本/合格率过滤。

    corpus v2 镜像段一律排除（v1/v2 同文，双份入池会重复计数）。
    """
    q = s.query(Segment).filter(Segment.role.is_(None) | (Segment.role == "train"),
                                exclude_corpus_v2_segments())
    if work_ids:
        q = q.filter(Segment.work_id.in_(work_ids))
    if seg_version is not None:
        q = q.filter(Segment.seg_version == seg_version)
    if eligible_only:
        q = q.filter(Segment.integrity.like('%"eligible": true%'))
    return q.all()
