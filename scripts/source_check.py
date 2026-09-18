"""原始文本完整性检查 —— 掉字/多字/人名不一致/句子未完（集霸 2026-09-18）。

## 为什么要有这一步

集霸看到的那一屏里其实有**两个**毛病：

1. 我生成的劣化版是病句（`不应该地扫描不到`）→ 已在 `controlled_corruption.py`
   里升级成硬拒 + 重写 ADVERB_INFLATION 的类型定义。
2. **原始 txt 本身就缺字**：
   - `还有和千仞雪一起逃走的五名强。` ← 掉了「者」
   - `千雪的神念突然…` ← 掉了「仞」（同一段里另一处又写成「千仞雪」，前后不一致）
   - `有吴天斗罗护法` ← 「昊天」写成「吴天」
   （已核对：`text` 与 `text_clean` 完全一致 → **不是清洗弄丢的**，是语料源头就坏。）

   他原话「字都不舍得搞完了？」骂的就是这个。第 2 类我的清洗器抓不到：它只查
   拉丁/带调拼音/水印，而"掉一个汉字"在字面上完全合法。

## 做法

按段调用 LLM，只判**文本完整性**（不评文风），结果写 `segments.integrity`：

```json
{"src_ok": false, "defects": ["第3句'五名强'疑缺'者'", "人名前后不一致：千雪/千仞雪"],
 "severity": "high|mid|low"}
```

下游（挑段、建批、生成劣化）**只取 src_ok=true**。

## 口径

- 只判"这文本是不是完好的"，**不判写得好不好**（文风是另一件事，别混）。
- 作者本人的风格问题（如唐家三少爱用"地"当"的"）**算缺陷但不拦**——
  它是真实语料的一部分；只有**信息被破坏**（缺字、缺句、名字指代混乱）才拦。
- 幂等：已有 `integrity.src_ok` 的段默认跳过。

用法：
    python scripts/source_check.py --scan
    python scripts/source_check.py --run --scope used --conc 8
    python scripts/source_check.py --run --scope all-frames --conc 8
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import db  # noqa: E402
from app.gateway import bind_experiment, chat  # noqa: E402
from app.models import Candidate, ControlledCorruption, Frame, Segment  # noqa: E402

PV = "source_integrity_v1"
MODEL = "moonshotai/kimi-k3"     # 判完整性要细读，用稳的模型
SYSTEM = ("你是中文文本校勘员。只判断这段文本**是否完好**（有没有缺字/多字/句子被截断/"
          "人名前后不一致/水印残留），**不评价文笔好坏**，也不要改写它。")
PROMPT = """下面是一段从网上下载的中文小说（盗版 txt 常有掉字、多字、水印），请只做校勘。

【文本】
{text}

逐条检查：
1. **缺字/多字**：有没有明显读不通、像是掉了一个字或多了一个字的地方？
   （例：`逃走的五名强。` 疑缺「者」；`偏偏却穿着` 疑多「却」）
2. **截断**：句子有没有没写完就断掉的？
3. **人名/称谓前后不一致**：同一个人在同一段里有没有两种写法？
   （例：`千仞雪` 与 `千雪`）
4. **水印残留**：还有没有站点广告、乱码、孤立的字母或符号？

注意：作者本人的用词习惯（例如把「的」写成「地」）**不算**缺陷，不要报。

只输出一行 JSON：
{{"src_ok": true|false,
  "defects": ["逐条写出具体位置与问题（没把握就别写）"],
  "severity": "high|mid|low"}}

判据：**只要信息可能被破坏（缺字/截断/人名混乱）就 src_ok=false**；
纯粹是作者文风、但文本完整的，src_ok=true。"""

_lock = threading.Lock()
_stat = {"ok": 0, "failed": 0, "skip": 0, "bad": 0}


def parse_json(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(t[i:j + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


# ── 确定性错字表（频次自洽确认后写死，可扩展）──────────────────
# 判据不是"我觉得应该这么写"，而是**语料自身的频次**：同一个作品里
# `昊天斗罗` ×48 / `吴天斗罗` ×1 / `了天斗罗` ×1 → 少数派就是抄写错误。
# 复现命令见 HANDOVER §7.7。凡命中即判 src_ok=false（缺字/错字属信息受损）。
KNOWN_TYPOS = {
    "吴天": "昊天",      # 斗罗大陆：昊天斗罗
    "了天斗罗": "昊天斗罗",
    "千雪": "千仞雪",     # 斗罗大陆：千仞雪（同段另一处写作"千仞雪"，前后不一致）
}


def rule_defects(text: str) -> list[str]:
    """确定性规则：命中已知错字/掉字。比 LLM 可靠，且零成本。"""
    out = []
    for bad, good in KNOWN_TYPOS.items():
        if bad == "千雪":
            # `千雪` 前面若是 `仞` 就说明是 `千仞雪` 的一部分，不算错
            import re as _re
            if _re.search(r"(?<!仞)千雪", text or ""):
                out.append(f"疑似掉字：`{bad}`（应为 `{good}`）")
            continue
        if bad in (text or ""):
            out.append(f"疑似错字：`{bad}`（应为 `{good}`）")
    return out


def check_one(text: str, exp_id: str | None = None) -> dict | None:
    from app.prompt_render import render
    # exp_id：实验引擎跑本阶段时把 llm_calls 归账到该实验（gateway 线程本地归属）；
    # CLI 直跑仍是 None，行为与以前完全一致。
    bind_experiment(exp_id)
    r = chat(model=MODEL, system=SYSTEM, user=render(PROMPT, text=text),
             purpose="source_integrity", prompt_version=PV,
             temperature=0.0, max_tokens=2500)
    return parse_json(r.text)


def targets(scope: str) -> list[str]:
    """scope：used=审查/劣化用到的段；all-frames=所有抽过 L 帧的段；bench=基准段。"""
    with db.session() as s:
        if scope == "used":
            ids = {r[0] for r in s.query(Candidate.segment_id).distinct()}
            ids |= {r[0] for r in s.query(ControlledCorruption.segment_id).distinct()}
        elif scope == "all-frames":
            ids = {r[0] for r in s.query(Frame.segment_id).filter(
                Frame.granularity == "L").distinct()}
        else:
            from app.models import Segment as S
            ids = {r[0] for r in s.query(S.id).filter(S.role == "benchmark")}
        rows = [(x.id, x.text_clean or x.text, x.integrity) for x in
                s.query(Segment).filter(Segment.id.in_(ids)).all()]
    out = []
    for sid, text, integ in rows:
        try:
            have = json.loads(integ or "{}")
        except Exception:
            have = {}
        if "src_ok" in have:
            _stat["skip"] += 1
            continue
        out.append((sid, text))
    return out


def run(scope: str = "used", conc: int = 8, limit: int = 0,
        ids: list[str] | None = None, exp_id: str | None = None) -> dict:
    if ids is not None:
        with db.session() as s:
            rows = [(x.id, x.text_clean or x.text, x.integrity) for x in
                    s.query(Segment).filter(Segment.id.in_(ids)).all()]
        todo = []
        for sid, text, integ in rows:
            try:
                have = json.loads(integ or "{}")
            except Exception:
                have = {}
            if "src_ok" in have:
                _stat["skip"] += 1
                continue
            todo.append((sid, text))
    else:
        todo = targets(scope)
    if limit:
        todo = todo[:limit]
    print(f"待检查 {len(todo)} 段（scope={scope}，已检查跳过 {_stat['skip']}）")
    if not todo:
        return dict(_stat)

    def one(item):
        sid, text = item
        hits = rule_defects(text)
        if hits:                                  # 规则命中 → 直接判坏，不花 LLM 的钱
            with db.session() as s:
                seg = s.get(Segment, sid)
                if seg:
                    prev = {}
                    try:
                        prev = json.loads(seg.integrity or "{}")
                    except Exception:
                        prev = {}
                    prev.update({"src_ok": False, "severity": "high",
                                 "defects": hits, "checked_pv": PV + "+rules"})
                    seg.integrity = json.dumps(prev, ensure_ascii=False)
                    s.commit()
            with _lock:
                _stat["bad"] += 1
            return
        if not text or len(text.strip()) < 10:
            with db.session() as s:
                seg = s.get(Segment, sid)
                if seg:
                    seg.integrity = json.dumps({"src_ok": False, "severity": "high",
                                                "defects": ["段为空或过短"]},
                                               ensure_ascii=False)
                    s.commit()
            with _lock:
                _stat["bad"] += 1
            return
        try:
            d = check_one(text, exp_id)
        except Exception:                            # noqa: BLE001
            with _lock:
                _stat["failed"] += 1
            return
        if not d:
            with _lock:
                _stat["failed"] += 1
            return
        with db.session() as s:
            seg = s.get(Segment, sid)
            if seg is None:
                return
            prev = {}
            try:
                prev = json.loads(seg.integrity or "{}")
            except Exception:
                prev = {}
            prev.update({"src_ok": bool(d.get("src_ok")), "severity": d.get("severity"),
                         "defects": d.get("defects") or [], "checked_pv": PV})
            seg.integrity = json.dumps(prev, ensure_ascii=False)
            s.commit()
        with _lock:
            _stat["ok"] += 1
            if not d.get("src_ok"):
                _stat["bad"] += 1

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, conc)) as ex:
        list(ex.map(one, todo))
    print(f"完成：ok={_stat['ok']} 判坏={_stat['bad']} failed={_stat['failed']} "
          f"（{(time.time() - t0) / 60:.1f} 分钟）")
    return dict(_stat)


def scan() -> None:
    with db.session() as s:
        rows = [(x.id, x.integrity) for x in s.query(Segment).all()]
    tot = len(rows)
    checked = ok = bad = 0
    for _sid, integ in rows:
        try:
            d = json.loads(integ or "{}")
        except Exception:
            continue
        if "src_ok" in d:
            checked += 1
            ok += bool(d["src_ok"])
            bad += not d["src_ok"]
    print(f"全库 {tot} 段；已检查 {checked}（完好 {ok}，判坏 {bad}），未检查 {tot - checked}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--scope", default="used",
                    choices=("used", "all-frames", "bench"))
    ap.add_argument("--conc", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    db.init_db()
    if args.scan:
        scan()
        return
    if args.run:
        print(json.dumps(run(scope=args.scope, conc=args.conc, limit=args.limit),
                         ensure_ascii=False))
        scan()
        return
    ap.print_help()


if __name__ == "__main__":
    main()
