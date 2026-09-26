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
- 幂等：已有**严格布尔** `integrity.src_ok` 的段默认跳过；存量值类型不严
  （字符串/数字/null）按未校验处理，强制重查。
- 防呆（T-GUARD）：开跑前先校验 `LG_SOURCE_MODEL` 在网关模型池内（池外 fail-fast 并
  打印最接近的名字，见 `scripts/preflight_models.py`）；累计 20 次调用后失败率 >30%
  当场熔断中止。起因：`data/_dbg/DIAG_rescore_387.md`（名字写错 → 387 条 503 全灭）。

`--scope` 现共四档（default=used，其余三档选取语义与历史逐字一致）：

- `used`：审查/劣化用到的段（Candidate ∪ ControlledCorruption 引用的段）；
- `all-frames`：所有抽过 L 帧的段；
- `bench`：role == 'benchmark' 的基准段；
- `nonbench`（审计 P0 主线 1 的检查面）：**K2 非基准试点供给池**——候选 =
  `role != 'benchmark'`（判据是「不等于」，NULL/train 都算非基准）**且**所属
  作品在 `work_sources` 登记为合规人类语料（`human_fiction` / 前缀
  `production_nonbenchmark_*`；判定单源复用 `scripts/k2_extract_backfill.py` 的
  `nonbenchmark_compliant_source`，不许另写一套，import 不到即 fail-closed
  报错退出）**且**来源类型不命中 K2 侧同源排除集
  `app.knowledge_query.DEFAULT_EXCLUDED_SOURCE_TYPES`（值 = fixture / synthetic
  / commentary；与 K2 侧同源，单源复用同一常量，import 不到即 fail-closed）**且**
  `text_clean` 非空。与 `k2_extract_backfill --source-scope nonbenchmark` 的段宇宙
  **逐字一致**（白名单 + 排除集双闸同判据）——把这条供给面纳入 source_check 的补查
  通道，不放松任何既有门（src_ok 幂等/严格布尔口径照旧）。

`--work-id <WK-...>`：把范围**收窄**到指定作品（可重复参数，或逗号分隔）。
收窄是唯一允许的方向：结果恒 ⊆ 该 scope 自己的选取集，绝不用它扩宽到
不合规来源（给不合规/无关 work_id = 空集，不报错也不放行）。

## 大库与事务纪律（运行手册）

真实库 10,395,708 段 ⇒ 段选取一律走 `iter_targets()` 的 keyset 分页
（内存 O(一批)，不整批入内存；`targets()` 只是兼容壳，真跑请用前者）。
`--limit N` 取满 N 段后**显式 close() 生成器**，`iter_targets` 内部的
`db.session()` 立即归还，**不把读事务挂到 GC**。
`nonbench` 分页与 `scan()` 全库扫描都是**每批自开自闭 session**，
批与批之间不持有读事务（长读事务残余处置，2026-09-26；细节见
`iter_targets` docstring 的「长事务纪律」）。

用法：
    python scripts/source_check.py --scan
    python scripts/source_check.py --run --scope used --conc 8
    python scripts/source_check.py --run --scope all-frames --conc 8
    python scripts/source_check.py --run --scope nonbench --conc 8
    python scripts/source_check.py --run --scope nonbench \
        --work-id WK-dc90993434e9 --conc 8
"""
from __future__ import annotations

import os

import argparse
import contextlib
import itertools
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import or_

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import db  # noqa: E402
from app.gateway import bind_experiment, chat, is_serial_model  # noqa: E402
from app.models import Candidate, ControlledCorruption, Frame, Segment, WorkSource  # noqa: E402
from app import config  # noqa: E402
import preflight_models as pf  # noqa: E402  # 批量防呆①：模型名预检
import k2_extract_backfill as k2b  # noqa: E402  # K2 试点来源口径唯一入口（单源复用，import 不到即 fail-closed）
from _conc_guard import check_conc, pool_workers  # noqa: E402  # 并发闸共用入口（上界 app/limits.MAX_CONCURRENCY，越界报错退出；串行纪律复用 gateway.is_serial_model）

# nonbench 排除集与 K2 侧同源（单源复用）：k2_extract_backfill 经
# `from app import knowledge_query as KQ` 消费 `DEFAULT_EXCLUDED_SOURCE_TYPES`
# （见其 k3_evidence_preview 的来源闸），source_check 复用同一对象，绝不另写一套；
# import 不到（或 KQ 缺该常量）即 fail-closed 报错退出。
from app import knowledge_query as _KQ  # noqa: E402  # K2 侧同源入口（与 k2_extract_backfill 同一引用）
try:
    NONBENCH_EXCLUDED_SOURCE_TYPES = _KQ.DEFAULT_EXCLUDED_SOURCE_TYPES
except AttributeError as _e:
    raise RuntimeError(
        "无法单源复用 K2 侧排除集常量 "
        "(app.knowledge_query.DEFAULT_EXCLUDED_SOURCE_TYPES)："
        f"{_e}。nonbench 口径必须与 K2 同源，已 fail-closed 退出，禁止另写一套。"
    ) from _e

PV = "source_integrity_v1"
# 判完整性要细读，用稳的模型；但**允许被调度覆盖**：夜间要把第一阶段也分派到
# 免费/套餐通道时，设 LG_SOURCE_MODEL=zcode/glm-5.3-flash 即可（默认值不变）。
MODEL = os.environ.get("LG_SOURCE_MODEL", "moonshotai/kimi-k3")
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
# first_error：本轮**首条失败原文**。纪律：失败率再高，只报数字不报原因就等于没报
# （2026-09-20 第五批扩产 100% failed 被误归因成"池子耗尽"，就是因为它）。
_stat = {"ok": 0, "failed": 0, "skip": 0, "bad": 0, "unverified": 0, "first_error": ""}


# ── 严格布尔口径（纯函数，无 DB / 无网络，便于回归）──────────────────
# 缺陷实证（审计 P1）：旧写入用 bool(d.get("src_ok"))，模型返回 `{"src_ok": "false"}`
# 这类合法 JSON 但类型不严的值时，bool("false") == True ⇒ 落库成 src_ok=true，
# 被下游（benchmark_build / goldpick_build / k2_extract_backfill 均用 `is True`）
# 当成"源文本完好"的合格证据。全仓只有写入方松 ⇒ 真缺陷。

def parse_src_ok(value) -> bool | None:
    """三态：JSON true → True（完好），JSON false → False（判坏），
    其余（"true"/"false"/1/0/None/缺字段/其它类型）→ None = 未校验。"""
    if value is True:
        return True
    if value is False:
        return False
    return None


def merge_integrity(prev: dict | None, verdict: bool | None, severity=None,
                    defects=None, checked_pv: str = "", raw: str = "") -> dict:
    """合并出新的 integrity dict（纯函数）。

    verdict 为 True/False：写**严格布尔** src_ok。
    verdict 为 None（模型返回类型不严）：**绝不写布尔 src_ok**，改成显式未校验态
    `{"src_ok_unverified": true, "src_ok_raw": <截断 200 字的模型原文片段>}`，
    并把历史残留的 src_ok（可能是落过库的字符串/数字）一并清掉——
    免得 `"src_ok" in d` 这类旧判断把它当成"已查过"。"""
    out = dict(prev or {})
    if verdict is True or verdict is False:
        out.pop("src_ok_unverified", None)
        out.pop("src_ok_raw", None)
        out["src_ok"] = verdict
        if severity is not None:
            out["severity"] = severity
        out["defects"] = list(defects or [])
        if checked_pv:
            out["checked_pv"] = checked_pv
    else:
        out.pop("src_ok", None)
        out["src_ok_unverified"] = True
        out["src_ok_raw"] = (raw or "")[:200]
        if checked_pv:
            out["checked_pv"] = checked_pv
        if severity is not None:
            out["severity"] = severity
        if defects:
            out["defects"] = list(defects)
    return out


def needs_check(have: dict | None) -> bool:
    """幂等判定（纯函数）：仅当 src_ok 是严格布尔（True/False）才跳过；
    字符串/数字/null/缺字段一律要重查——存量脏值不允许被幂等永久留存。"""
    v = (have or {}).get("src_ok")
    return not (v is True or v is False)

# 批量防呆②：失败率熔断。累计 LLM 调用满 BREAKER_MIN_CALLS 次后，failed/total 超线
# 就当场中止（返回 dict 里 aborted=true），不等跑完 387 条再报 ok=0。
# 起因见 data/_dbg/DIAG_rescore_387.md（模型名写错 → 503 全灭 × 7.4 分钟）。
BREAKER_MIN_CALLS = 20
BREAKER_FAIL_RATE = 0.3


def _preflight() -> str | None:
    """开跑前校验模型名在网关池内；None=可跑，否则返回不可跑的原因（含候选名）。

    mock 模式根本不发网络调用（gateway 回确定性伪输出），无从校验也无需校验。
    """
    return pf.preflight_block([MODEL], source="source_check")


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


def parse_work_ids(raw) -> list[str]:
    """`--work-id` 展开：可重复参数、可逗号分隔，去首尾空白、去重，保序返回。

    空输入 → []（= 不收窄）。不合规/无关 work_id 交给 targets() 做纯交集，
    只收窄不放宽——绝不用它扩宽到不合规来源。
    """
    out: list[str] = []
    for chunk in raw or []:
        for tok in str(chunk).split(","):
            tok = tok.strip()
            if tok and tok not in out:
                out.append(tok)
    return out


def _needs_check_row(sid: str, text: str, integ: str | None):
    """（内部）按 integrity 判定该段是否还要送检；要则产出 (sid, text)。

    与改造前 targets() 尾部逐字同口径：needs_check 为假计 skip；
    已带 src_ok 的存量值计 unverified（类型不严 → 按未校验处理，必须重查）。
    """
    try:
        have = json.loads(integ or "{}")
    except Exception:
        have = {}
    if not isinstance(have, dict):
        have = {}
    if not needs_check(have):
        _stat["skip"] += 1
        return
    if "src_ok" in have:
        _stat["unverified"] += 1   # 存量值类型不严：按未校验计，且必须重查
    yield sid, text


def iter_targets(scope: str, *, work_ids: list[str] | None = None,
                 batch: int = 20000):
    """**流式**产出 (seg_id, text)，内存 O(一批)；选取语义与 targets() 逐字一致。

    为什么必须流式：K3 批量入册后真实库已到 **10,395,708 段**，其中
    role!=benchmark 且 text_clean 非空的有 **10,323,132 段**。旧写法把这
    10,323,132 段一次性 `.all()` 进 `ids` 集合、再灌进 `rows` 列表，
    粗估 ≈3.6GB ⇒ 实测直接 `MemoryError`（scan() 同病）。
    这里改为按 `Segment.id` 升序 **keyset 分页**（每批 `batch` 段），
    只做集合选取，批序不影响结果集；`--limit` 也因此第一次真正做到
    「不把全库读进内存」。

    scope：used=审查/劣化用到的段；all-frames=所有抽过 L 帧的段；
    bench=基准段；nonbench=K2 非基准试点供给池。

    work_ids：把范围**收窄**到指定作品——与 scope 选取集做纯交集（⊆），
    收窄是唯一允许的方向。None/[] = 不收窄。

    batch：keyset 分页页大小（默认 20000 段）。只影响开几批 SQL，
    **不影响选取结果**（id 严格升序推进 ⇒ 不漏不重）——测试正是靠把它
    钉成 3 来证明这一点。

    ## 长事务纪律（运行手册，2026-09-26 残余处置）
    分页在 SQLite 上是**快照读**。两个 scope 的 session 生命周期不同，
    跑长窗口前按此判断：
    - `nonbench`：**每批自开自闭 session**（`with db.session()` 在循环体内），
      批与批之间不持有读事务 ⇒ 千万级段池整轮检查期间**不跨批持有**长读事务，
      夜间批处理不会被写侧（import/标注入库）长时间挡在 WAL 之外。
      代价：每批重新下发合规来源集合（`work_sources` 全表投影，随 work 数
      线性，实测 40 部作品 = 40 行，可忽略）。
    - `used` / `all-frames` / `bench`：这三档的**选取集本身有界**
      （候选段 / L 帧 / 基准段，实测 benchmark 仅 833 段），为保住
      「先取 id 集、再按 500 分块拉整行」的旧口径（`id IN (...)` 一次绑定会撞
      SQLite 变量数上限，实测 394k 变量 OperationalError: too many SQL
      variables），这些段的 session 仍**跨越整个惰性产出期**。
      有界 ⇒ 单事务读的行数有限；**若日后把这两档扩到千万级，必须先改成
      每批自开自闭 session**，否则流式检查期间会一直挂着长读事务。
    """
    if scope == "nonbench":
        # 与 k2_extract_backfill.segment_universe(source_scope='nonbenchmark')
        # 逐字一致（双闸同判据，绝不另写一套）：
        # ① 段 role 显式「不等于 benchmark」（NULL 亦算非基准——SQL 明写
        #    or_(IS NULL, !=)，避免 `!=` 在 SQL 里把 NULL 吞掉的口径漂移）；
        # ② 所属作品在 work_sources 登记为合规人类语料（白名单
        #    nonbenchmark_compliant_source，单源复用 k2b，不另写）；
        # ③ 排除集（fixture/synthetic/commentary）——单源复用 K2 侧同源常量
        #    app.knowledge_query.DEFAULT_EXCLUDED_SOURCE_TYPES（k2b 经 KQ 同源
        #    消费），与 K2 侧逐字一致；
        # ④ text_clean 非空（与抽取侧 `(text_clean or '').strip()` 同闸）。
        #
        # 合规来源集合要先物化成 Python 集合才能拼 `IN (...)`，所以第一段
        # 用一个短 session 读它，读完即随 `with` 关闭；分页循环另起短 session，
        # 每批自开自闭（见上面「长事务纪律」）。
        with db.session() as s:
            reg = s.query(WorkSource.work_id, WorkSource.source_type).all()
            compliant = {wid for wid, st in reg
                         if k2b.nonbenchmark_compliant_source(st)
                         and (st or "") not in NONBENCH_EXCLUDED_SOURCE_TYPES}
        narrow = set(work_ids) if work_ids else None
        if not compliant or (narrow is not None and not narrow):
            return
        last = None
        while True:
            # 每批一个短事务：批与批之间不持有读事务（长读事务残余处置）
            with db.session() as s:
                q = s.query(Segment.id, Segment.text_clean, Segment.text,
                            Segment.integrity).filter(
                    Segment.work_id.in_(compliant),
                    or_(Segment.role.is_(None), Segment.role != "benchmark"))
                if narrow is not None:
                    # 只收窄：与 scope 选取集做交集（子集），扩宽路径在此不存在
                    q = q.filter(Segment.work_id.in_(narrow))
                if last is not None:
                    # keyset 推进：严格大于上批末行 id ⇒ 不漏（跳过的只在
                    # `text_clean` 空与 needs_check 两道闸里，id 不会被重发）
                    # 不重（`>` 而非 `>=`）。改成 `>=` 会把批末行重发一遍，
                    # tests/test_source_check_streaming.py 的「不漏不重」用例会红。
                    q = q.filter(Segment.id > last)
                rows = q.order_by(Segment.id).limit(batch).all()
            if not rows:
                return
            for sid, text_clean, text, integ in rows:
                last = sid
                if not (text_clean or "").strip():
                    continue
                yield from _needs_check_row(sid, text_clean or text, integ)
        return                      # nonbench 走 keyset 分页，不落到下面三档
    # used / all-frames / bench：选取集本身有界（候选段 / L 帧 / 基准段，
    # 实测 benchmark 仅 833 段），保持原口径——先取 id 集，再按 500 分块
    # 拉整行（`id IN (...)` 一次绑定会撞 SQLite 变量数上限，实测 394k 变量
    # OperationalError: too many SQL variables），只是改成惰性产出。
    with db.session() as s:

        if scope == "used":
            ids = {r[0] for r in s.query(Candidate.segment_id).distinct()}
            ids |= {r[0] for r in s.query(ControlledCorruption.segment_id).distinct()}
        elif scope == "all-frames":
            ids = {r[0] for r in s.query(Frame.segment_id).filter(
                Frame.granularity == "L").distinct()}
        else:
            ids = {r[0] for r in s.query(Segment.id).filter(
                Segment.role == "benchmark")}
        for i in range(0, len(ids), 500):
            part = sorted(ids)[i:i + 500]
            q = s.query(Segment).filter(Segment.id.in_(part))
            if work_ids:
                q = q.filter(Segment.work_id.in_(set(work_ids)))
            for x in q.all():
                yield from _needs_check_row(x.id, x.text_clean or x.text,
                                            x.integrity)


def targets(scope: str, *, work_ids: list[str] | None = None) -> list[str]:
    """scope：used=审查/劣化用到的段；all-frames=所有抽过 L 帧的段；
    bench=基准段；nonbench=K2 非基准试点供给池（合规人类语料 + role!=benchmark
    + 来源不命中 K2 侧同源排除集 fixture/synthetic/commentary + text_clean 非空，
    来源判据与白名单/排除集单源复用 k2_extract_backfill → app.knowledge_query）。

    work_ids：把范围**收窄**到指定作品——与 scope 选取集做纯交集（⊆），
    收窄是唯一允许的方向。None/[] = 不收窄，结果与改动前逐字一致。

    **兼容壳**：既有调用方与 tests/test_source_check_nonbench_scope.py 依赖
    列表语义（`== []`、`{x[0] for x in ...}`），故这里仍返回 list。
    真内核是 iter_targets()——千万级段池必须走它，在真实库上 list() 会 OOM。
    """
    return list(iter_targets(scope, work_ids=work_ids))


def run(scope: str = "used", conc: int = 8, limit: int = 0,
        ids: list[str] | None = None, exp_id: str | None = None,
        work_ids: list[str] | None = None) -> dict:
    blocked = _preflight()
    if blocked:
        print(f"[预检失败] 模型名 `{MODEL}` 用不了，未开跑：{blocked}")
        return {**dict(_stat), "aborted": True}
    _stat["first_error"] = ""          # 本轮失败原因只属于本轮
    abort = threading.Event()

    def _count(*keys: str, first_error: str = "") -> None:
        """累计计数；满 BREAKER_MIN_CALLS 次后失败率超线就置熔断（只报一次）。

        first_error 只记**本轮第一条**原文（后来的不覆盖）：熔断/日报要能自己说原因。
        """
        msg = None
        with _lock:
            for k in keys:
                _stat[k] += 1
            if first_error and not _stat["first_error"]:
                _stat["first_error"] = pf.redact(first_error)
            calls = _stat["ok"] + _stat["failed"]
            rate = _stat["failed"] / calls if calls else 0.0
            if (calls >= BREAKER_MIN_CALLS and rate > BREAKER_FAIL_RATE
                    and not abort.is_set()):
                abort.set()
                msg = (f"熔断：失败率 {rate:.1%}，已中止；"
                       f"首条错误原文：{_stat['first_error'] or '（一条异常文本都没捕获到）'}")
        if msg:
            print(msg)

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
            if not isinstance(have, dict):
                have = {}
            if not needs_check(have):
                _stat["skip"] += 1
                continue
            if "src_ok" in have:
                _stat["unverified"] += 1   # 存量值类型不严：按未校验计，且必须重查
            todo.append((sid, text))
    else:
        # 流式内核：千万级段池绝不整批进内存（旧写法在真实库上 MemoryError）。
        gen = iter_targets(scope, work_ids=work_ids)
        if limit:
            # 取满 limit 就**显式 close()**：islice 只做了「停止取数」，生成器
            # 仍是活的，它内部 `with db.session()` 的读事务要挂到 GC 才释放
            # （CPython 里 next() 不再引用后虽可回收，但那是不可预期的时机，
            #  期间长读事务一直占着）。close() 抛 GeneratorExit 进到 with 栈里
            # ⇒ session 立即归还。
            with contextlib.closing(gen) as g:
                todo = list(itertools.islice(g, limit))
        else:
            first = next(gen, None)
            todo = [] if first is None else itertools.chain((first,), gen)
    label = f"scope={scope}"
    if work_ids:
        label += f"，work_id 收窄 {len(set(work_ids))} 本"
    if isinstance(todo, list):
        print(f"待检查 {len(todo)} 段（{label}，已检查跳过 {_stat['skip']}）")
        if not todo:
            return {**dict(_stat), "aborted": False}
    else:
        # 流式时不给假数字：总数只在完成行按实际取了几段报出
        print(f"待检查：流式扫描（{label}；总数在完成行给出）")

    def one(item):
        if abort.is_set():
            return
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
                    prev = merge_integrity(prev, False, "high", hits, PV + "+rules")
                    seg.integrity = json.dumps(prev, ensure_ascii=False)
                    s.commit()
            _count("bad")
            return
        if not text or len(text.strip()) < 10:
            with db.session() as s:
                seg = s.get(Segment, sid)
                if seg:
                    seg.integrity = json.dumps(
                        merge_integrity({}, False, "high", ["段为空或过短"]),
                        ensure_ascii=False)
                    s.commit()
            _count("bad")
            return
        try:
            d = check_one(text, exp_id)
        except Exception as e:                             # noqa: BLE001
            _count("failed", first_error=f"{type(e).__name__}: {e}")
            return
        if not d:
            _count("failed",
                   first_error="LLM 返回空/JSON 解析不出（原文见 llm_calls 该行 error 字段）")
            return
        # 严格口径：只有 JSON true/false 才算已校验；类型不严（"false"/1/null 等）
        # 一律 unverified，绝不落库成布尔，更不计入 ok。
        verdict = parse_src_ok(d.get("src_ok"))
        raw = json.dumps(d, ensure_ascii=False)
        with db.session() as s:
            seg = s.get(Segment, sid)
            if seg is None:
                return
            prev = {}
            try:
                prev = json.loads(seg.integrity or "{}")
            except Exception:
                prev = {}
            new_integ = merge_integrity(prev, verdict, d.get("severity"),
                                        d.get("defects") or [], PV, raw=raw)
            seg.integrity = json.dumps(new_integ, ensure_ascii=False)
            s.commit()
        if verdict is True:
            _count("ok")
        elif verdict is False:
            _count("ok", "bad")
        else:
            _count("unverified")

    t0 = time.time()
    workers = pool_workers(conc, [MODEL], serial_check=is_serial_model)   # 运行时兜底：绕过 CLI 直调 run() 也开不出越界池
    fed = len(todo) if isinstance(todo, list) else 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        # 分批喂：`ex.map` 会先把整批建成 futures 列表，全量提交等于把整池驻留内存。
        it = iter(todo)
        while True:
            chunk = list(itertools.islice(it, 2000))
            if not chunk:
                break
            if not isinstance(todo, list):
                fed += len(chunk)
            list(ex.map(one, chunk))
    done = (f"完成：进货 {fed} 段；ok={_stat['ok']} 判坏={_stat['bad']} failed={_stat['failed']} "
            f"unverified={_stat['unverified']}（{(time.time() - t0) / 60:.1f} 分钟）")
    if _stat["unverified"]:
        done += f"；{_stat['unverified']} 段模型返回类型不严/存量脏值，按未校验处理，需重跑"
    if _stat["failed"]:
        # 有失败就必须当场说原因——不许把"去找网关"留给人工翻 DB
        done += f"；首条错误原文：{_stat['first_error'] or '（未捕获到异常文本）'}"
    print(done)
    return {**dict(_stat), "aborted": abort.is_set()}


def scan() -> None:
    # 真实库 10,395,708 段：旧写法 `s.query(Segment).all()` 一次性物化
    # （实测 MemoryError）。改为按 Segment.id 升序 keyset 分页，内存 O(一批)。
    # 计数口径**一行未动**（tot/checked/ok/bad/unverified 与分页前逐值一致，
    # tests/test_source_check_streaming.py 拿改造前的一次性扫描当尺子对拍）。
    # 长事务残余处置：每批自开自闭 session ⇒ 全库扫描期间不跨批持有读事务
    # （与 iter_targets 的 nonbench 分支同一纪律，见其 docstring「长事务纪律」）。
    tot = checked = ok = bad = unverified = 0
    batch = 20000
    last = None
    while True:
        with db.session() as s:
            q = s.query(Segment.id, Segment.integrity)
            if last is not None:
                q = q.filter(Segment.id > last)
            rows = q.order_by(Segment.id).limit(batch).all()
        if not rows:
            break
        for _sid, integ in rows:
            last = _sid
            tot += 1
            try:
                d = json.loads(integ or "{}")
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            v = parse_src_ok(d.get("src_ok"))
            if v is True:
                checked += 1
                ok += 1
            elif v is False:
                checked += 1
                bad += 1
            elif "src_ok" in d or d.get("src_ok_unverified"):
                unverified += 1    # 类型不严的存量值/显式未校验态：不算完好也不算判坏
    msg = (f"全库 {tot} 段；已检查 {checked}（完好 {ok}，判坏 {bad}），"
           f"未校验 {unverified}，未检查 {tot - checked - unverified}")
    if unverified:
        msg += "；未校验段按未校验处理，需重跑"
    print(msg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--scope", default="used",
                    choices=("used", "all-frames", "bench", "nonbench"),
                    help=("段选取口径（默认 %(default)s=现状不变）："
                          "used=审查/劣化用到的段；all-frames=所有抽过 L 帧的段；"
                          "bench=role=='benchmark' 的基准段；"
                          "nonbench=K2 非基准试点供给池——段 role != 'benchmark'"
                          "（NULL 也算非基准，判据是「不等于」）且所属作品在 "
                          "work_sources 登记为合规人类语料（human_fiction / "
                          "production_nonbenchmark_*，判定单源复用 "
                          "scripts/k2_extract_backfill.py 的 "
                          "nonbenchmark_compliant_source，import 不到即 "
                          "fail-closed 报错退出）且来源类型不命中 K2 侧同源排除集 "
                          "DEFAULT_EXCLUDED_SOURCE_TYPES（fixture / synthetic / "
                          "commentary，与 K2 侧同源、单源复用 app.knowledge_query，"
                          "import 不到即 fail-closed）且 text_clean 非空。"))
    ap.add_argument("--work-id", action="append", default=[],
                    help=("把范围收窄到指定作品（WK-…，可重复参数或逗号分隔）。"
                          "收窄是唯一允许的方向：结果恒 ⊆ 该 scope 自己的选取集，"
                          "绝不用它扩宽到不合规来源（给不合规/无关 work_id = "
                          "空集，不报错也不放行）。"))
    ap.add_argument("--conc", type=int, default=8,
                    help=f"线程池并发（上界 app/limits.MAX_CONCURRENCY，越界报错退出；"
                         f"{MODEL} 等单账号 CLI 通道命中时强制串行 workers=1）")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    check_conc(ap, args.conc, "--conc")   # 闸在任何库/网络副作用之前：越界响亮报错退出
    db.init_db()
    if args.scan:
        scan()
        return
    if args.run:
        res = run(scope=args.scope, conc=args.conc, limit=args.limit,
                  work_ids=parse_work_ids(args.work_id))
        print(json.dumps(res, ensure_ascii=False))
        if res.get("aborted"):
            sys.exit(2)     # 预检没过 / 失败率熔断：非零退出，别让夜间窗口静默空转
        scan()
        return
    ap.print_help()


if __name__ == "__main__":
    main()
