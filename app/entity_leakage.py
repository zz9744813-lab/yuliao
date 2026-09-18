"""Entity-normalized Leakage（Phase 1.5 §六）。

动机：Frame 必须携带人物/地点等身份信息，强模型据此回填专名 → adversarial 分被
"必要语义实体泄漏"抬高，对信息更丰富的 L-Frame 不公平。

方法：工作级实体词表（jieba.posseg 的 nr/ns/nt，≥2 次出现）→ 把 Frame 表示与原文中的
实体替换为 ⟦E1⟧…⟦Ek⟧ → 在归一化文本上重算 char6 / word3 / adversarial。
raw 与 entity-normalized 两口径并列呈现：
  raw 高 + norm 低  → 泄漏主要是必要实体回填（可接受）
  raw 高 + norm 高  → 真正的表达形式泄漏（要修 Frame/prompt）
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from .leakage import adversarial_layer, char_layer, word_layer
from .models import Segment

_PLACEHOLDER = "⟦E{}⟧"


def build_entity_lexicon(s: Session, work_id: str, sample: int = 400,
                         min_count: int = 2) -> list[str]:
    """工作级专名词表：随机抽样段 + jieba 词性标注（nr 人名/ns 地名/nt 机构）。"""
    import random
    segs = s.query(Segment).filter_by(work_id=work_id, seg_version=1).all()
    segs = random.Random(7).sample(segs, min(sample, len(segs)))
    counts: dict[str, int] = {}
    try:
        import jieba.posseg as posseg
    except Exception:
        return []
    for seg in segs:
        for w, flag in posseg.cut(seg.text[:300]):
            if flag in ("nr", "ns", "nt") and 2 <= len(w) <= 5 \
                    and not re.search(r"[，。！？…「」\s]", w):
                counts[w] = counts.get(w, 0) + 1
    return sorted((w for w, c in counts.items() if c >= min_count),
                  key=len, reverse=True)


def normalize(text: str, entities: list[str]) -> str:
    out = text
    for i, name in enumerate(entities):
        out = out.replace(name, _PLACEHOLDER.format(i + 1))
    return out


def entity_normalized_report(frame_repr: str, src: str, entities: list[str],
                             adversarial_k: int = 8) -> dict:
    norm_frame, norm_src = normalize(frame_repr, entities), normalize(src, entities)
    hits = [e for e in entities if e in frame_repr or e in src]
    out = {
        "entities_in_pair": hits[:20],
        "n_entities_normalized": len(hits),
        "char6_norm": char_layer(norm_frame, norm_src)["score"],
        "word3_norm": word_layer(norm_frame, norm_src)["score"],
    }
    adv = adversarial_layer(norm_frame, norm_src, k=adversarial_k)
    out["adversarial_norm"] = {"score": adv.get("score"),
                               "status": adv.get("status"),
                               "detail_head": str(adv.get("top_hits") or adv.get("restorations") or "")[:100]}
    return out
