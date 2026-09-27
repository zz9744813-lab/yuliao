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
- **非空正文不静默覆写**（清洗正文保留门，审计 2026-09-23 非阻断项续作）：
  `--polish` / `--llm` 命中**已有非空** `text_clean` 时默认**拒写**，该段记成
  「待人工裁决」（`integrity` JSON 键 + 审计日志 JSONL）；显式
  `--overwrite-text-clean` 才越门覆写，且**旧值全文**落审计日志留痕。
  `text_clean` 为空的首写不受本门影响，仍是原口径。
- 下游（取题、上下文、生成）一律优先读 `text_clean`。
- 只用规则能修干净的，不进 LLM（省钱）；剩下还有拉丁/带调拼音的才送 LLM。
- 规则清洗后仍带伪影且 LLM 也修不了的 → 标 `integrity.clean_failed=1`，下游照旧排除。

用法：
    python scripts/clean_text.py --scan                  # 只看分布
    python scripts/clean_text.py --rules                 # 只跑规则（秒级）
    python scripts/clean_text.py --llm --conc 8          # 规则修不掉的送 LLM
    python scripts/clean_text.py --llm --overwrite-text-clean   # 越门：允许覆写非空正文（留痕）
    python scripts/clean_text.py --report
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import config, db, limits  # noqa: E402
from app.gateway import chat, is_serial_model  # noqa: E402
from app.models import Segment  # noqa: E402
from _conc_guard import check_conc as _check_conc, pool_workers as _pool_workers  # noqa: E402  # 并发闸唯一实现（2026-09-25 去重）
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

# ── 正文保留门（审计《language-genome-code-audit-20260923》非阻断项）──
# LLM 截断、跑题、只回一句"已修复"时，直接覆写会把好正文换成残句，
# 而 text_clean 是下游取题/上下文/生成优先读的正文——误清洗会静默污染下游。
# 门默认开启；设 CLEAN_TEXT_GUARD=0/off/false/no 可临时关闭（对比用）。
GUARD_ENV = "CLEAN_TEXT_GUARD"
GUARD_MIN_LEN = 20       # 去空白后低于此长度 → 视为垃圾输出（如"已修复"），拒
GUARD_MIN_RATIO = 0.6    # 相对长度下限：out < 0.6×src → 视为截断，拒
GUARD_MAX_RATIO = 1.2    # 相对长度上限：out > 1.2×src → 视为"原文 + 垃圾追加"，拒
                         # （1.2 = 允许 LLM 把拼音写回汉字带来的长度浮动，
                         #  超出即认为多出了原文以外的内容；审计建议同值）
GUARD_MIN_SIMILARITY = 0.6  # 相似卡：SequenceMatcher(None, src, out).ratio() 低于此
                         # → 视为跑题改写（内容换掉但长度没露馅）。取 0.6 是因为合法
                         # 修复只动少量字符（拼音还原、水印剥离），ratio 实测 ≥0.8；
                         # 跑题段落与原文几乎无公共字符，ratio 接近 0，两侧余量都大。
GUARD_MAX_LATIN_RATIO = 0.3 # 拉丁占比卡：out 里 [A-Za-z] 占比 > 此 而 src 不 > 此
                         # → 视为"原文中文、结果被整段拼音化（无声调）"。中文小说正文
                         # 拉丁占比通常在 10% 以下，拼音化会推到 50% 以上，0.3 留一倍余量；
                         # src 自己就超阈值（本来就是大段英文/拉丁）时本卡保持沉默，
                         # 因为那种形态不是"拼音化污染"，误杀代价高于漏判。
GUARD_SHORT_SRC = 40     # 短原文豁免口径：src 去空白后 ≤ 40 字时**不做比例卡**，
                         # 只卡绝对下限——否则 25 字的正常短段会被 0.6 比例误伤全拒。
                         # 上限卡/相似卡/拉丁占比卡同口径豁免：短段里"合理改写"天然会
                         # 动掉大半字符，长度比、相似度、拉丁占比在小分母下都失真，
                         # 误杀是整段拒绝落库（好正文永远修不掉），代价高于漏判。
_LATIN_CHAR = re.compile(r"[A-Za-z]")
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
_stat = {"rule_ok": 0, "llm_ok": 0, "llm_failed": 0, "llm_rejected": 0,
         "llm_identical": 0, "skip": 0, "still_broken": 0,
         "gate_blocked": 0, "gate_overwritten": 0}


def _latin_ratio(text: str) -> float:
    """拉丁字母（A-Za-z）占字符数比例；空串返回 0.0。"""
    return len(_LATIN_CHAR.findall(text)) / len(text) if text else 0.0


def guard_verdict(src: str, out: str) -> str:
    """正文保留门的判定：返回 'accept' / 'identical' / 'too_short' / 'truncated'
    / 'latinized' / 'oversize' / 'divergent'（除 'accept'/'identical' 外一律不落库）。

    口径（见 GUARD_* 常量注释）：
    - out 去空白后与 src 去空白后完全相同 → 'identical'（幂等，免无谓 UPDATE，非拒绝）；
    - out 去空白后 < GUARD_MIN_LEN → 'too_short'（垃圾输出，如"已修复"）；
    - src 去空白后 > GUARD_SHORT_SRC 且 out < GUARD_MIN_RATIO×src → 'truncated'；
      短原文豁免比例卡，避免把正常短段全拒。
    三张补强卡（审计《language-genome-code-audit-20260923》非阻断项 §4 的三种放行
    反例；与 truncated 同享 GUARD_SHORT_SRC 短原文豁免，语义互不覆盖）：
    - 'latinized'：out 拉丁占比 > GUARD_MAX_LATIN_RATIO 而 src 不超 → 整段无声调拼音化
      （反例 c）。放在最前：拼音化必然同时拉长文本、拉低相似度，先判才能给出贴病灶的
      态名，否则同一污染会被笼统记成 oversize/divergent，统计归因失真；
    - 'oversize'：out > GUARD_MAX_RATIO×src → 原文照抄外加垃圾追加（反例 b）；
    - 'divergent'：SequenceMatcher(None, s_src, s_out).ratio() < GUARD_MIN_SIMILARITY
      → 长度相当但内容跑题（反例 a）。
    """
    s_src, s_out = (src or "").strip(), (out or "").strip()
    if s_out == s_src:
        return "identical"
    if len(s_out) < GUARD_MIN_LEN:
        return "too_short"
    if len(s_src) > GUARD_SHORT_SRC:
        n_src, n_out = len(s_src), len(s_out)
        if n_out < GUARD_MIN_RATIO * n_src:
            return "truncated"
        if (_latin_ratio(s_out) > GUARD_MAX_LATIN_RATIO
                and _latin_ratio(s_src) <= GUARD_MAX_LATIN_RATIO):
            return "latinized"
        if n_out > GUARD_MAX_RATIO * n_src:
            return "oversize"
        if SequenceMatcher(None, s_src, s_out).ratio() < GUARD_MIN_SIMILARITY:
            return "divergent"
    return "accept"


def _guard_enabled(guard: bool | None = None) -> bool:
    """门开关：显式参数优先；否则读 GUARD_ENV（默认开启）。"""
    if guard is not None:
        return guard
    return os.environ.get(GUARD_ENV, "").strip().lower() not in {"0", "off", "false", "no"}


# ── 清洗正文保留门（任务 lg-clean-text-preserve-gate；审计 2026-09-23 非阻断项续作）──
# 上面的 GUARD_* 是**内容质量门**：判「LLM 这一次的结果坏不坏」，判据是 src↔out。
# 它不看另一件事：**text_clean 里是否已躺着一份非空正文、马上要被整个丢掉**。
# `--polish` 与 `--llm` 的落笔都发生在已有非空 text_clean 之上（polish 的目标段
# 定义即「text_clean 非 None」；llm 的 src = text_clean or text），重跑即**覆写**，
# 被覆写的旧值原本无留痕——本门补的就是这一条。
# 口径（默认拒写）：
# - 旧值为空（NULL/空白）→ 正常首写，门完全不介入、不留痕（原口径逐字不变）；
# - 旧值非空且新值去空白后与旧值相同 → 幂等跳过（覆写无意义，不算事件）；
# - 旧值非空且新值不同 → **不写**：旧值原地保留，该段记「待人工裁决」
#   （integrity JSON 键 `text_clean_preserve_gate`，风格借 import_corpus_v2 的
#   「JSON 键留痕、不加新列」口径），并落一条 pending_review 审计事件；
# - 显式 `--overwrite-text-clean`（函数参数 overwrite=True）才越门：写入新值，
#   **旧值全文 + sha256** 落审计日志 overwritten 事件，并清除待裁决标记。
# 审计日志 = JSONL，默认 `config.DATA_DIR/clean_text_preserve_gate.jsonl`
# （`CLEAN_TEXT_GATE_LOG` 可显式改路径，测试/隔离用）。
PRESERVE_KEY = "text_clean_preserve_gate"          # segments.integrity 里的留痕键
GATE_LOG_ENV = "CLEAN_TEXT_GATE_LOG"              # 审计 JSONL 路径覆盖（env）
GATE_LOG_NAME = "clean_text_preserve_gate.jsonl"  # 默认落 config.DATA_DIR

_log_lock = threading.Lock()


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gate_log_path() -> Path:
    """覆写审计日志路径：CLEAN_TEXT_GATE_LOG 显式覆盖 > config.DATA_DIR 默认。"""
    ov = (os.environ.get(GATE_LOG_ENV) or "").strip()
    return Path(ov) if ov else config.DATA_DIR / GATE_LOG_NAME


def _append_gate_log(record: dict) -> None:
    """审计事件追加写 JSONL（行式、只增不改；进程内加锁防线程交错）。"""
    p = gate_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _log_lock:
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _integrity_flags(raw: str | None) -> dict | None:
    """integrity JSON → dict（空视为 {}）。解析不出 JSON 对象 → None：**不猜**。

    None 时调用侧不得重写 integrity——那是别的工序的留痕，覆盖它本身就是丢数据；
    此时裁决事件只落审计日志（JSONL 仍是全量的，DB 标记缺失可由日志对账）。"""
    if not (raw or "").strip():
        return {}
    try:
        d = json.loads(raw)
    except Exception:                            # noqa: BLE001
        return None
    return d if isinstance(d, dict) else None


def _mark_pending_review(seg: Segment, old: str, new: str, via: str) -> None:
    """把「该段的非空正文被拒写、待人工裁决」记进 integrity JSON（只增本门键）。"""
    flags = _integrity_flags(seg.integrity)
    if flags is None:
        return
    flags[PRESERVE_KEY] = {
        "status": "pending_review",
        "via": via,
        "old_sha256": _sha256(old),
        "proposed_sha256": _sha256(new),
        "old_len": len(old),
        "proposed_len": len(new),
    }
    seg.integrity = json.dumps(flags, ensure_ascii=False)


def _clear_pending_review(seg: Segment) -> None:
    """越门覆写成功后清除待裁决标记（该段已裁决＝按新值落定）。"""
    flags = _integrity_flags(seg.integrity)
    if not isinstance(flags, dict) or PRESERVE_KEY not in flags:
        return
    flags.pop(PRESERVE_KEY)
    seg.integrity = json.dumps(flags, ensure_ascii=False)


def preserve_gate_write(seg: Segment, new_value: str, *, overwrite: bool, via: str) -> str:
    """清洗正文保留门——`polish` / `run_llm` 两条**可能覆写非空正文**的写路径
    落 `text_clean` 的唯一入口。返回裁决：

    - 'write'       旧值为空 → 正常首写（原口径，不触发门、不留痕）；
    - 'identical'   旧值非空、新值去空白后与旧值相同 → 幂等跳过，不写、不算事件；
    - 'blocked'     旧值非空、未授权越门 → **拒写**，旧值原地保留，
                    integrity 记「待人工裁决」+ 审计日志 pending_review 事件；
    - 'overwritten' 旧值非空、`overwrite=True` 显式越门 → 写入新值，
                    **旧值全文 + sha256** 落审计日志 overwritten 事件。
    """
    old = seg.text_clean or ""
    if not old.strip():
        seg.text_clean = new_value
        return "write"
    if new_value.strip() == old.strip():
        return "identical"
    base = {"via": via, "segment_id": seg.id, "work_id": seg.work_id,
            "old_sha256": _sha256(old), "new_sha256": _sha256(new_value),
            "old_len": len(old), "new_len": len(new_value), "ts": _now_ts()}
    if overwrite:
        _append_gate_log({**base, "action": "overwritten", "old_value": old})
        seg.text_clean = new_value
        _clear_pending_review(seg)
        return "overwritten"
    _mark_pending_review(seg, old, new_value, via)
    _append_gate_log({**base, "action": "pending_review", "proposed_value": new_value})
    return "blocked"


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

def run_rules(limit: int = 0, only_dirty: bool = False, dry_run: bool = False) -> dict:
    """全库过一遍规则清洗，写 text_clean。

    dry_run=True：只统计会改多少段，**不写任何行、不 commit**（2026-09-24 主控
    误伤实录：--rules 分支曾无视 --dry-run 静默覆写生产库 text_clean）。
    """
    with db.session() as s:
        q = s.query(Segment)
        if limit:
            q = q.limit(limit)
        segs = q.all()
        if dry_run:
            # 口径与真跑逐字对齐：真写时每个 text_clean 为空的段都会落一次写，
            # 预报必须数同一批段——不许"估"。
            n = sum(1 for seg in segs if not seg.text_clean)
            return {"dry_run": True, "would_clean": n, "checked": len(segs)}
        n = 0
        for seg in segs:
            if seg.text_clean:
                # 保留门不入本路径：非空 text_clean 在这里**永远不写**（上方 continue），
                # 下面落笔只可能发生在空段上——不构成「覆写非空正文」。
                continue
            c = clean_rules(seg.text)
            seg.text_clean = c
            n += 1
        s.commit()
    _stat["rule_ok"] = n
    return {"rule_cleaned": n}


def polish(limit: int = 0, dry_run: bool = False, overwrite: bool = False) -> dict:
    """对**已清洗文本**再跑一遍规则。

    为什么要单独一步：规则表会随样本增加（本轮就补了 `阅读请锁定{　}` 与空花括号），
    而 LLM 还原的结果**不能**用原文重跑规则覆盖（那会把刚还原好的拼音打回去）。
    顺序必须是：规则 → LLM → 规则（polish）。

    dry_run=True：只统计会改多少段，**不写任何行、不 commit**。dry 的口径维持
    「加门前规则还会改多少段」的预报（残留普查用），不受保留门影响。

    清洗正文保留门（默认拒写）：本函数的目标段定义即 `text_clean` 非 None，
    每一次落笔都是对**非空正文的覆写**——默认只记「待人工裁决」、不写正文；
    显式 `overwrite=True`（CLI `--overwrite-text-clean`）才覆写，旧值全文落审计日志。
    """
    with db.session() as s:
        segs = s.query(Segment).filter(Segment.text_clean.isnot(None)).all()
        if limit:
            segs = segs[:limit]
        if dry_run:
            n = sum(1 for seg in segs
                    if clean_rules(seg.text_clean) != (seg.text_clean or ""))
            return {"dry_run": True, "would_clean": n, "checked": len(segs)}
        n = overwrote = pending = 0
        for seg in segs:
            c = clean_rules(seg.text_clean)
            if c != (seg.text_clean or ""):
                verdict = preserve_gate_write(seg, c, overwrite=overwrite, via="polish")
                if verdict in ("write", "overwritten"):
                    n += 1
                if verdict == "overwritten":
                    overwrote += 1
                elif verdict == "blocked":
                    pending += 1
        s.commit()
    return {"polished": n, "checked": len(segs),
            "gate_overwritten": overwrote, "gate_pending": pending}


def run_llm(conc: int = 8, limit: int = 0, only_batch_segments: bool = False,
            guard: bool | None = None, overwrite: bool = False) -> dict:
    """把规则修不掉的送 LLM 还原拼音。幂等：text_clean 已无拉丁残留的会跳过。

    正文保留门（默认开）：LLM 结果过短/截断时不覆写 text_clean，计 llm_rejected；
    可用 guard=False 或环境变量 CLEAN_TEXT_GUARD=0 关闭（对比用）。
    清洗正文保留门（默认拒写）：内容质量门放行后，若该段 `text_clean` **已非空**，
    这次落笔就是**覆写**——默认不写、计 gate_blocked、该段记「待人工裁决」；
    显式 `overwrite=True`（CLI `--overwrite-text-clean`）才越门，旧值全文落审计日志。
    `text_clean` 为空的首写不受本门影响（计 llm_ok，与加门前逐字一致）。
    conc 是运行时兜底口径：≤ limits.MAX_CONCURRENCY，且 LLM_MODEL 命中串行
    纪律（gateway.is_serial_model）时恒 1——见 _pool_workers。
    """
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
    guard_on = _guard_enabled(guard)

    def one(batch: list[tuple[str, str]]) -> None:
        try:
            outs = llm_repair_batch([t for _, t in batch])
        except Exception:                            # noqa: BLE001
            with _lock:
                _stat["llm_failed"] += len(batch)
            return
        with db.session() as s:
            for (sid, src), out in zip(batch, outs):
                if not out:
                    with _lock:
                        _stat["llm_failed"] += 1
                    continue
                seg = s.get(Segment, sid)
                if seg is None:
                    continue
                if guard_on:
                    verdict = guard_verdict(src, out)
                    if verdict == "identical":       # 幂等：不写，免无谓 UPDATE
                        with _lock:
                            _stat["llm_identical"] += 1
                        continue
                    if verdict != "accept":          # 过短/截断/拼音化/超长/跑题 → 保留原值不写
                        with _lock:
                            _stat["llm_rejected"] += 1
                        continue
                # 内容质量门放行 ≠ 可以落笔：旧 text_clean 非空时这一步是**覆写**，
                # 走清洗正文保留门（默认拒写 + 待人工裁决；--overwrite-text-clean 越门留痕）。
                gate_verdict = preserve_gate_write(seg, out, overwrite=overwrite, via="llm")
                with _lock:
                    if gate_verdict == "blocked":
                        _stat["gate_blocked"] += 1
                    elif gate_verdict in ("write", "overwritten"):
                        _stat["llm_ok"] += 1
                        if gate_verdict == "overwritten":
                            _stat["gate_overwritten"] += 1
                        if looks_broken(out):
                            _stat["still_broken"] += 1
            s.commit()

    t0 = time.time()
    workers = _pool_workers(conc, [LLM_MODEL], serial_check=is_serial_model)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, batches))
    print(f"完成：llm_ok={_stat['llm_ok']} failed={_stat['llm_failed']} "
          f"门拒={_stat['llm_rejected']} 幂等={_stat['llm_identical']} "
          f"覆写拒写={_stat['gate_blocked']} 越门覆写={_stat['gate_overwritten']} "
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--rules", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--polish", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--conc", type=int, default=8,
                    help=f"LLM 线程池并发（上界 app/limits.MAX_CONCURRENCY="
                         f"{limits.MAX_CONCURRENCY}，越界报错退出）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite-text-clean", action="store_true",
                    help="越门开关（默认关）：允许覆写非空 text_clean。清洗正文保留门"
                         "默认对覆写**拒写**并记「待人工裁决」；越门时旧值全文+sha256 "
                         "落审计日志 JSONL（见 clean_text_preserve_gate.jsonl）")
    return ap


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = build_parser()
    args = ap.parse_args(argv)
    _check_conc(ap, args.conc, "--conc")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    db.init_db()
    if args.scan or args.report:
        report()
        return
    if args.rules:
        if args.dry_run:
            print("DRY-RUN：只统计，未写入任何数据、未 commit —— 以下数字是预报，不是真跑结果")
            print(json.dumps(run_rules(limit=args.limit, dry_run=True), ensure_ascii=False))
            return
        print(json.dumps(run_rules(limit=args.limit), ensure_ascii=False))
        report()
        return
    if args.polish:
        if args.dry_run:
            print("DRY-RUN：只统计，未写入任何数据、未 commit —— 以下数字是预报，不是真跑结果")
            print(json.dumps(polish(limit=args.limit, dry_run=True), ensure_ascii=False))
            return
        print(json.dumps(polish(limit=args.limit,
                                overwrite=args.overwrite_text_clean), ensure_ascii=False))
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
        print(json.dumps(run_llm(conc=args.conc, limit=args.limit,
                                 overwrite=args.overwrite_text_clean), ensure_ascii=False))
        report()
        return
    build_parser().print_help()


if __name__ == "__main__":
    main()
