"""原始文本清洗 —— 拼音 / 站点水印 / 空括号残留（集霸 2026-09-18 要求）。

## 为什么必须清，而不是"过滤掉就行"

集霸原话：「先把原始文本的那些拼音广告啥的搞一下不然评个屁啊。」
实测比例（v2 切分的全部 29.6 万段）：

| 作品 | 带伪影 |
|---|---|
| 将夜 | 7.0% |
| 凡人修仙传 | 5.4% |
| 斗罗大陆 | 1.4% |
| 琼明神女录（精校） | 0.26% |

伪影有三类，**只有第一类能靠删**：

1. **站点水印整段插入**：`(手打中文网7*24小时不间断更新纯txt手打小说m)`、`小.说。t/x/t天.堂`、
   `co` 独占一行。→ 删掉即可，信息无损。
2. **拼音替换**：`白sè雾气`、`lù出`、`jīng神状态`、`神sè各异`。盗版工具把**生僻字**换成拼音。
   → 删了会丢字（"白雾气"），必须**还原成汉字**。这类要 LLM 按上下文补。
3. **空括号残留**：`()()`（水印被上一道工序抠掉后剩下的壳）。→ 删。

为什么不能只在取样时排除：伪影也出现在**上文**里（评审台显示的那一段），
集霸看到的就是这个；而且被排除的 5~7% 是整块语料，训练数据白扔。

## 口径

- **原文不动**：清洗结果写 `segments.text_clean`，`text` 保持原样（可审计、可重跑）。
- 下游（取题、上下文、生成）一律优先读 `text_clean`。
- 只用规则能修干净的，不进 LLM（省钱）；剩下还有拉丁/带调拼音的才送 LLM。
- 规则清洗后仍带伪影且 LLM 也修不了的 → 标 `integrity.clean_failed=1`，下游照旧排除。

用法：
    python scripts/clean_text.py --scan                  # 只看分布
    python scripts/clean_text.py --rules                 # 只跑规则（秒级）
    python scripts/clean_text.py --llm --conc 8          # 规则修不掉的送 LLM
    python scripts/clean_text.py --report
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
from app.gateway import chat  # noqa: E402
from app.models import Segment  # noqa: E402
import preflight_models as pf  # noqa: E402  # 批量防呆①：开跑前校验模型名在网关池内

RULES_PV = "clean_rules_v1"
LLM_PV = "clean_llm_v1"
LLM_MODEL = "z-ai/glm-5.3"

# ── 规则 ───────────────────────────────────────────────────────
# 每条都来自实测样本，不做没见过的推测式清洗
_WM_PATTERNS = [
    # 站点水印整段插入（括号包裹的推广语）
    re.compile(r"[（(]\s*手打[^）)]*[）)]"),
    re.compile(r"[（(]\s*(?:未完待续|本章完|手机用户请[^）)]*)[）)]"),
    re.compile(r"(?:最新章节|全文阅读|请记住本站|记住本站|免费阅读|首发|更新最快|"
               r"最快更新|无弹窗|手机阅读|顶点小说|笔趣|笔趣窝)[^\n，。！？；]*"),
    re.compile(r"[a-zA-Z0-9]*\.?(?:com|net|cn|org)\b[^\n]*"),          # 域名尾巴
    re.compile(r"小\s*[.、。]\s*说.*?天\s*[.、。]\s*堂"),
    re.compile(r"t\s*/\s*x\s*/\s*t", re.I),                            # 站点缩写
    # 实测第二类：`阅读请锁定{　}` 这类"引导语 + 空花括号"，以及光秃秃的 `[]`／`{}`
    re.compile(r"(?:阅读请锁定|请锁定|锁定)[^\n]{0,12}?[｛{][^｝}]*[｝}]"),
    re.compile(r"[｛{][\s　]*[｝}]|\[\s*\]"),
    # 三、空括号残留（水印被抠掉后的壳）
    re.compile(r"[（(]\s*[）)]"),
]
# 行首行尾的残留分隔符（水印被删掉后剩下的 ` / ` 之类；不动引号括号等成对符号）
_EDGE_JUNK = re.compile(r"^[\s/\\|·•_~、]+|[\s/\\|·•_~]+$")
# 独占一行的碎片（`co` / 单字母 / 纯符号行）
_JUNK_LINE = re.compile(r"^[\s\W]*[a-zA-Z]{0,3}[\s\W]*$")
_ACCENT = re.compile(r"[āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]")
_LATIN_RUN = re.compile(r"[A-Za-z]{1,8}")
_EMPTY_BRACKET = re.compile(r"[（(]\s*[）)]|\[\s*\]|【\s*】")


def clean_rules(text: str) -> str:
    """规则清洗：删站点水印与空括号、处理 `mｍ` 之类的半角残留。**不动拼音**。"""
    t = text or ""
    for pat in _WM_PATTERNS:
        t = pat.sub("", t)
    lines = [ln for ln in t.split("\n") if not _JUNK_LINE.match(ln)]
    lines = [_EDGE_JUNK.sub("", ln) for ln in lines]
    t = "\n".join(ln for ln in lines if ln.strip())
    t = t.replace("mｍ", "").replace("（）", "").replace("()", "")
    t = _EMPTY_BRACKET.sub("", t)
    return t.strip()


def needs_llm(text: str) -> bool:
    """规则洗完后是否还需 LLM：还剩带调拼音，或"汉字+拉丁"粘连（拼音替换的形态）。"""
    if not text:
        return False
    if _ACCENT.search(text):
        return True
    # `白sè` 形态：汉字紧邻拉丁串（合法的英文专名在中文小说里几乎不这样粘）
    return bool(re.search(r"[\u4e00-\u9fff][A-Za-z]{1,8}|[A-Za-z]{1,8}[\u4e00-\u9fff]", text))


def looks_broken(text: str) -> bool:
    """清洗后是否仍明显是坏文本（下游据此排除）。"""
    if not text or len(text.strip()) < 10:
        return True
    if _ACCENT.search(text):
        return True
    return len(re.findall(r"[\u4e00-\u9fff][A-Za-z]{1,8}", text)) >= 2


LLM_SYSTEM = ("你是中文文本修复器。只把被盗版工具换成拼音的字**还原成汉字**，"
              "并删掉残留的站点水印碎片。不改写文风、不增删内容、不调整句读。")
# 分批：一段一次调用要跑 1 万多段（≈11 小时）。10 段一包，调用数掉到约 1/10，
# 且同包内互相不干扰（编号返回，顺序可校验）。
LLM_BATCH = 10
LLM_PROMPT = """下面有 {n} 段中文小说（编号 1..{n}），其中有些字被盗版工具替换成了拼音
（例：`白sè`=白色、`lù出`=露出、`jīng神`=精神），也可能残留站点水印碎片。

{blocks}

对**每一段**只做两件事：
1. 把拼音**按上下文还原**成最恰当的那个汉字；
2. 删掉残留的站点水印碎片（如 `(手打中文网…)`、`co`、孤立字母）。

其余每一个字都保持原样：不要润色、不要改标点、不要增删句子、不要合并或拆分段落。
若某段本来就干净，原样返回。

只输出一行 JSON：
{{"items": [{{"i": 1, "text": "第1段修复后的文本"}}, {{"i": 2, "text": "第2段修复后的文本"}}]}}"""

_lock = threading.Lock()
_stat = {"rule_ok": 0, "llm_ok": 0, "llm_failed": 0, "skip": 0, "still_broken": 0}


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


def llm_repair_batch(texts: list[str]) -> list[str | None]:
    """一批多段一起修；返回与输入等长的列表（失败的为 None）。

    返回顺序靠 `i` 编号对齐，**不靠模型输出顺序**——模型偶尔会合并或跳号，
    按顺序 zip 会把 A 的正文写到 B 头上（这类串行污染静默且致命）。
    """
    from app.prompt_render import render
    blocks = "\n\n".join(f"【{i + 1}】{t}" for i, t in enumerate(texts))
    r = chat(model=LLM_MODEL, system=LLM_SYSTEM,
             user=render(LLM_PROMPT, n=len(texts), blocks=blocks),
             purpose="clean_llm", prompt_version=LLM_PV,
             temperature=0.0, max_tokens=6000)
    d = parse_json(r.text)
    out: list[str | None] = [None] * len(texts)
    for it in ((d or {}).get("items") or []):
        try:
            k = int(it.get("i")) - 1
        except Exception:                            # noqa: BLE001
            continue
        if 0 <= k < len(out):
            out[k] = (it.get("text") or "").strip() or None
    return out


# ── 主流程 ─────────────────────────────────────────────────────

def run_rules(limit: int = 0, only_dirty: bool = False) -> dict:
    """全库过一遍规则清洗，写 text_clean。"""
    with db.session() as s:
        q = s.query(Segment)
        if limit:
            q = q.limit(limit)
        segs = q.all()
        n = 0
        for seg in segs:
            if seg.text_clean:
                continue
            c = clean_rules(seg.text)
            seg.text_clean = c
            n += 1
        s.commit()
    _stat["rule_ok"] = n
    return {"rule_cleaned": n}


def polish(limit: int = 0) -> dict:
    """对**已清洗文本**再跑一遍规则。

    为什么要单独一步：规则表会随样本增加（本轮就补了 `阅读请锁定{　}` 与空花括号），
    而 LLM 还原的结果**不能**用原文重跑规则覆盖（那会把刚还原好的拼音打回去）。
    顺序必须是：规则 → LLM → 规则（polish）。
    """
    with db.session() as s:
        segs = s.query(Segment).filter(Segment.text_clean.isnot(None)).all()
        if limit:
            segs = segs[:limit]
        n = 0
        for seg in segs:
            c = clean_rules(seg.text_clean)
            if c != (seg.text_clean or ""):
                seg.text_clean = c
                n += 1
        s.commit()
    return {"polished": n, "checked": len(segs)}


def run_llm(conc: int = 8, limit: int = 0, only_batch_segments: bool = False) -> dict:
    """把规则修不掉的送 LLM 还原拼音。幂等：text_clean 已无拉丁残留的会跳过。"""
    with db.session() as s:
        rows = [(x.id, x.text_clean or x.text) for x in s.query(Segment).all()]
    todo = [(i, t) for i, t in rows if needs_llm(t)]
    if limit:
        todo = todo[:limit]
    batches = [todo[i:i + LLM_BATCH] for i in range(0, len(todo), LLM_BATCH)]
    print(f"需要 LLM 还原的段：{len(todo)}（全库 {len(rows)}）→ {len(batches)} 批 "
          f"× {LLM_BATCH} 段")
    if not todo:
        return {"llm": 0}

    def one(batch: list[tuple[str, str]]) -> None:
        try:
            outs = llm_repair_batch([t for _, t in batch])
        except Exception:                            # noqa: BLE001
            with _lock:
                _stat["llm_failed"] += len(batch)
            return
        with db.session() as s:
            for (sid, _), out in zip(batch, outs):
                if not out:
                    with _lock:
                        _stat["llm_failed"] += 1
                    continue
                seg = s.get(Segment, sid)
                if seg is None:
                    continue
                seg.text_clean = out
                with _lock:
                    _stat["llm_ok"] += 1
                    if looks_broken(out):
                        _stat["still_broken"] += 1
            s.commit()

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, conc)) as ex:
        list(ex.map(one, batches))
    print(f"完成：llm_ok={_stat['llm_ok']} failed={_stat['llm_failed']} "
          f"仍坏={_stat['still_broken']}（{(time.time() - t0) / 60:.1f} 分钟）")
    return dict(_stat)


def report() -> None:
    with db.session() as s:
        total = s.query(Segment).count()
        cleaned = s.query(Segment).filter(Segment.text_clean.isnot(None)).count()
        dirty_raw = 0
        still = 0
        for x in s.query(Segment).all():
            raw_dirty = needs_llm(x.text) or bool(clean_rules(x.text) != (x.text or "").strip())
            if raw_dirty:
                dirty_raw += 1
            if x.text_clean and looks_broken(x.text_clean):
                still += 1
    print(f"段总数 {total}；text_clean 已写 {cleaned}")
    print(f"原始带伪影 {dirty_raw}（{dirty_raw / total:.2%}）；清洗后仍坏 {still}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--rules", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--polish", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--conc", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db.init_db()
    if args.scan or args.report:
        report()
        return
    if args.rules:
        print(json.dumps(run_rules(limit=args.limit), ensure_ascii=False))
        report()
        return
    if args.polish:
        print(json.dumps(polish(limit=args.limit), ensure_ascii=False))
        report()
        return
    if args.llm:
        if args.dry_run:
            with db.session() as s:
                n = sum(1 for x in s.query(Segment).all()
                        if needs_llm(x.text_clean or x.text))
            print(f"dry-run：需 LLM 的段 {n}")
            return
        # 批量防呆①（P0 死 id 事故）：拼音还原整批走 LLM_MODEL，池外=白跑一轮
        pf.require_models([LLM_MODEL], source="clean_text")
        print(json.dumps(run_llm(conc=args.conc, limit=args.limit), ensure_ascii=False))
        report()
        return
    ap.print_help()


if __name__ == "__main__":
    main()
