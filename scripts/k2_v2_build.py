"""K2 v2 合并卡（S1/S2）端到端构建器（2026-09-30 派工 lg-k2-v2-cards）。

## 为什么是这条路（现场只读直核结论，本件不重新论证）

8 张 v1 legacy 卡的语义审查至今 0 条两席全 PASS（真库 `semantic_review_votes`
28/22/22，`semantic_approval_links` 只写进 2 条）⇒ K4 `preflight_world()` 的
`review_status=verified` 达不到，三场 live 起不来。项目自己的定稿
（`docs/策略卡合并方案_20260923.md`）给的唯一正当出路是**合并成 2 张 v2 卡**：
S1「克制留白 vs 潜台词摊开」（`v2:留白摊开`）、S2「直给节拍 vs 注水拆拍」
（`v2:节拍注水`）。本件把这张路线**端到端跑通**：落卡 → 成对对照证据 → 晋升链。

## 判据单源（本件不另写一套）

- **成对证据**：`scripts/k2_contrast_extract.py` 的门0–门3（+ 门4/门5）按构造
  标注（`op` 必填、混合 op 拒收）——本件只调它的 `build_pairs` / `gate_pair` /
  `run_contrast`，一条判据都不复制、不放宽。
- **晋升链**：`scripts/k5_promotion_write.py` 的 `evaluate()`（阶梯 + 各级证据
  前置 + scope 逐级推导）与 `commit_promotion()`（单事务 CAS + 不可变审计行 +
  `semantic_approval_links`）——本件只调它，不重写判词、不自填 verdict。
- **两席收据**：`scripts/k2_receipt_mint.py` 的 `seat_routes_from_env` /
  `start_gateways` / `mint` + `app.semantic_review_runner.review_snapshot`
  （全仓唯一能写 `semantic_review_calls`/`semantic_review_votes` 的入口）。
- **准读数**：`app.semantic_admission.approved_selected`（K4 消费的是它）。

## 硬边界

- **真库只读**：目标路径命中真库候选 ⇒ `RealDatabaseRefused` 当场拒（见
  `assert_copy_target`）。一切写操作只发生在调用方给的**副本库**上。
- **不碰 8 张 v1 卡**：本件对 `expression_strategies_v2` 里 `version=1` 且
  `strategy_key LIKE 'legacy:%'` 的行**只读**——不改 status / scope /
  observation_status 一个字节（`approved_selected` 要求晋升审计与卡片逐字一致）。
- **`hypothesis` 不许跳级直写 `verified`**：本件只经 `evaluate()` 走
  `hypothesis → observed → replicated → verified` 阶梯，每次只走一级
  （`evaluate` 对跨级输入一律 `skip_ladder` 拒）。
- **不许自填判词**：语义审查判词（`semantic_review_votes.verdict`）只能由网关
  真实调用产出（`review_snapshot`）；缺两席收据则 `verified` 写不进去，
  `semantic_approval_links` 也不会有行。
- **纯离线**：本件**不联网生成 AI 侧**。AI 侧片段由调用方注入
  （`--ai-side pairs.json`），只有 `--stage receipts` 会按席位环境变量把**已冻结
  的快照**真投给上游网关（`LG_ATTEST_*`，见 `k2_receipt_mint`）。
- **对照两极都进冻结证据**：`k2_contrast_extract._persist_one` 只把 AI 侧记成
  `ai_side_sha256`（**原文不入库**），而两席看的 `review_input` 逐字取
  `conditions_observed` ⇒ 冻结证据里**只有人类侧**，对照的另一极缺席，两席
  无从核验本协议的核心主张（现场实测：seat1 如实 ABSTAIN，原文见
  `attach_ai_side` docstring）。`--stage attach` 把**同一批注入对**的 AI 侧
  原文按 `human_sha256` 对齐并入 `conditions_observed`（对不上/冲突即拒），
  判据一字不改、只补**已判过门**的对照另一极，不放宽任何门。
- **不编造 `effect_hypothesis`**：合并方案 §1 只给「语义意图」（定义内容），
  没有效果假设 ⇒ 如实写 `EFFECT_UNDECIDED`，`effect_status='untested'`。
- **幂等**：卡 id 由 `strategy_key + version` 派生；同输入重跑不重复落卡、
  不重复落实例（`k2_contrast_extract` 的 `evidence_sha256` 去重照用、
  `attach` 同值 no-op）、不重复投席（`review_snapshot` 的
  `(snapshot_id, model_id)` 投一票即锁）。

用法（阶段可单跑，`all` = 全链）：

    python scripts/k2_v2_build.py --db <副本.db> --stage cards
    python scripts/k2_v2_build.py --db <副本.db> --stage evidence \\
        --ai-side pairs.json --pairs-ledger k2_pairs.jsonl --live
    python scripts/k2_v2_build.py --db <副本.db> --stage attach --ai-side pairs.json
    python scripts/k2_v2_build.py --db <副本.db> --stage attest
    python scripts/k2_v2_build.py --db <副本.db> --stage ladder --reviewer R
    LG_ATTEST_*=... python scripts/k2_v2_build.py --db <副本.db> --stage receipts
    python scripts/k2_v2_build.py --db <副本.db> --stage verified --reviewer R
    python scripts/k2_v2_build.py --db <副本.db> --stage readout --book-id WK-...

退出码：0=该阶段全过；2=拒绝语义（门拒/无收据/指向真库）；1=用法或运行错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import k2_contrast_extract as CX            # noqa: E402  门0–门3 单源
import k5_promotion_write as KP             # noqa: E402  晋升判词单源
import k2_receipt_mint as MINT              # noqa: E402  两席收据驱动器
from app.models import ExpressionStrategyV2, StrategyInstance  # noqa: E402
from app import knowledge_extract as KE     # noqa: E402  复审标记
from app.semantic_review import SnapshotError  # noqa: E402  快照拒绝（fail-closed）

TOOL = "k2_v2_build"
SCHEMA = "k2_v2_build/v1"
V2_VERSION = 2
S1_KEY, S2_KEY = CX.S1_KEY, CX.S2_KEY
CARD_VERSION_KEYS = (S1_KEY, S2_KEY)
EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2

# 真库只读目标（派工给定）。命中即拒：副本库必须与真库不同文件。
REAL_DB_CANDIDATES = (Path("D:/language-genome-data/language_genome.db"),)

# effect_hypothesis 纪律：合并方案 §1 给的是「语义意图」（定义内容，方案原文
# 标明「不作分类器」），**不是**效果假设；本轮没有效果实验 ⇒ 如实写未定。
EFFECT_UNDECIDED = ("未定——v2 合并卡未做效果实验；合并方案 §1 只给「语义意图」"
                   "（定义内容），不构成效果假设")

# 合并方案 §1 两条新卡（抽象操作/不变项/失败模式逐字取自方案 §1；§2 给出 8→2+1
# 的逐卡对应关系，`legacy_primary` 取该卡归并依据里的首个 legacy 血缘 id，
# 全部归并集合记在 `source` 里，不因单值列而丢血缘）。
CARD_SPECS: tuple[dict, ...] = (
    {
        "label": "S1", "strategy_key": S1_KEY,
        "title": "克制留白 vs 潜台词摊开",
        "legacy_primary": "ES-252069e72b43",
        "legacy_all": ("ES-252069e72b43", "ES-d2d3e56bc0c3"),
        "ops": (CX.OP_ADD_INTERPRETATION, CX.OP_ADD_PSYCH_NARRATION),
        "abstract_operation": (
            "克制留白 vs 潜台词摊开——人类把言外之意留在字面之下：含蓄表态、"
            "点到即止、沉默后直接给结论；AI 则把潜台词摊开成明文：新增解释句、"
            "心理旁白、条件补充、重复申明。落点：对话留白、悬念、疑问。"
            "语义意图=语义增量（AI 侧存在人类侧无对应来源的陈述）；运行时归属"
            "由构造操作 op 声明、门0 验证，不作分类器。"),
        "invariants": [
            "人类侧无解释段（对动机/含义不作说明）",
            "收束干脆",
        ],
        "failure_modes": [
            "把一句潜台词拆成多句申明",
            "补「他其实/是因为/心里明白」类解释",
            "添加原文没有的条件、让步、总结",
        ],
    },
    {
        "label": "S2", "strategy_key": S2_KEY,
        "title": "直给节拍 vs 注水拆拍",
        "legacy_primary": "ES-7e3d5b2add03",
        "legacy_all": ("ES-7e3d5b2add03", "ES-ec5ad8233e4d", "ES-67f8a86edf86",
                       "ES-17f795e4341b", "ES-785358a815e5"),
        "ops": (CX.OP_SPLIT_BEATS, CX.OP_DILUTE_MODIFIERS),
        "abstract_operation": (
            "直给节拍 vs 注水拆拍——人类把已明说的动作/情绪/台词一步到位："
            "短句直给、单一动作、必要直白词；AI 则把同一内容拉长稀释：拆成"
            "多节拍（微动作链/慢镜头/渐进台词）、堆叠比喻/感官/氛围修饰、"
            "环境铺垫、套路节拍词。落点：对话动作、概述、本体推进、情绪爆发。"
            "语义意图=语义不变、密度下降（长度↑、修饰↑、无新增陈述）；运行时"
            "归属由构造操作 op 声明、门0 验证，不作分类器。"),
        "invariants": [
            "事件与指称不变（无新增陈述）",
            "节拍落在动作/情绪本身",
        ],
        "failure_modes": [
            "拆拍（动作短句计数显著多于人类侧）",
            "修饰密度上升（比喻/感官词/氛围词）",
            "「忽然/就在/最后」类套路收束",
        ],
    },
)
CARDS_BY_KEY = {c["strategy_key"]: c for c in CARD_SPECS}


class RealDatabaseRefused(RuntimeError):
    """目标路径是真库（或真库的另一个名字）——本件只许写副本库。"""


class EvidenceAttestError(RuntimeError):
    """实例不具备升格资格（门记录不全 / 非本协议产出 / 已升格过）。"""


# --------------------------------------------------------------- 副本库闸
def _resolve(path) -> Path:
    return Path(path).expanduser().resolve()


def real_db_paths(repo_root=None) -> set[Path]:
    """真库候选集合（派工给定的绝对路径 + 本仓 `data/` 默认位）。
    比较一律在 `resolve()` 之后的**路径字面量**上做（Windows 上大小写不敏感）。"""
    banned = {p.resolve() for p in REAL_DB_CANDIDATES}
    if repo_root is not None:
        banned.add((Path(repo_root) / "data" / "language_genome.db").resolve())
    return banned


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def assert_copy_target(db_path, repo_root=None) -> Path:
    """目标必须是**副本库**：解析后路径命中真库候选 ⇒ `RealDatabaseRefused`。

    真库在本轮全程只读（现场只读直核用的就是它），绝不允许被当写入目标。
    副本另需**确实存在于磁盘**且与真库不同文件——不猜、不回落默认值。
    """
    target = _resolve(db_path)
    for banned in sorted(real_db_paths(repo_root)):
        if _same_file(target, banned):
            raise RealDatabaseRefused(
                f"real_database_refused:{target}（真库只读；本件只写副本库）")
    return target


def open_engine(database: Path) -> Engine:
    """进程内 sqlite 引擎（WAL + foreign_keys），路径由调用方先过副本库闸。"""
    engine = create_engine(f"sqlite:///{Path(database).as_posix()}",
                           future=True,
                           connect_args={"check_same_thread": False,
                                         "timeout": 30.0})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


# ------------------------------------------------------------------ 卡片
def card_id(strategy_key: str, version: int = V2_VERSION) -> str:
    """确定性卡 id（形如 `ESV2-<12hex>`，与既有 `app.ids.new_id("ESV2")` 同形）：
    由 `strategy_key + version` 派生 ⇒ 同输入重跑必得同 id（幂等的基础）。"""
    digest = hashlib.sha256(f"{strategy_key}|v{version}".encode(
        "utf-8")).hexdigest()[:12]
    return f"ESV2-{digest}"


def card_payload(spec: dict, version: int = V2_VERSION) -> dict:
    """一张 v2 卡的落库字段（status/observation_status 固定在阶梯底
    `hypothesis`——判定≠升格，落卡即 hypothesis；scope 固定 UNCERTAIN，
    由 `k5_promotion_write.scope_candidates` 在同一次晋升事务里逐级推导）。"""
    return {
        "id": card_id(spec["strategy_key"], version),
        "strategy_key": spec["strategy_key"],
        "version": version,
        "abstract_operation": spec["abstract_operation"],
        "invariants": list(spec["invariants"]),
        "effect_hypothesis": EFFECT_UNDECIDED,
        "failure_modes": list(spec["failure_modes"]),
        "status": "hypothesis",
        "source": (f"k2_v2_build mergeplan: {spec['label']} "
                   f"← legacy {','.join(spec['legacy_all'])}"),
        "legacy_strategy_id": spec["legacy_primary"],
        "scope": "UNCERTAIN",
        "scope_ids": [],
        "scope_basis": "",
        "observation_status": "hypothesis",
        "effect_status": "untested",
    }


def build_cards(session: Session, version: int = V2_VERSION) -> dict:
    """两张 v2 卡落库（幂等）：已存在则**核字段一致**后跳过（不一致即报错，
    不覆盖——已被后续晋升改写过的卡重跑也不该被拽回 hypothesis）。"""
    created, reused, checked = [], [], []
    for spec in CARD_SPECS:
        payload = card_payload(spec, version)
        row = session.get(ExpressionStrategyV2, payload["id"])
        if row is None:
            session.add(ExpressionStrategyV2(**payload))
            created.append(payload["id"])
            continue
        drift = [key for key in ("strategy_key", "version", "abstract_operation",
                                 "invariants", "effect_hypothesis",
                                 "failure_modes", "legacy_strategy_id")
                 if getattr(row, key) != payload[key]]
        if drift:
            raise EvidenceAttestError(
                f"card_field_drift:{payload['id']}:{','.join(sorted(drift))}"
                "（同名卡已存在但定义与合并方案 §1 不符——不覆盖，fail-visible）")
        reused.append(payload["id"])
        checked.append({"strategy_id": row.id, "status": row.status,
                        "observation_status": row.observation_status,
                        "scope": row.scope, "scope_ids": list(row.scope_ids or []),
                        "effect_status": row.effect_status})
    session.commit()
    return {"stage": "cards", "version": version, "created": created,
            "reused": reused, "current": checked}


def read_cards(session: Session, version: int = V2_VERSION) -> list[dict]:
    return [{"strategy_id": card_id(spec["strategy_key"], version),
             "strategy_key": spec["strategy_key"], "label": spec["label"]}
            for spec in CARD_SPECS]


# ------------------------------------------------------- 成对证据（门0–门3）
def extract_evidence(session: Session, pairs: list[CX.ContrastPair], *,
                     live: bool, ledger_path: str,
                     extractor_model: str = CX.PROTOCOL) -> dict:
    """成对对照证据落库：**门判据一字不改**地转调 `k2_contrast_extract`。

    `live=False` ⇒ 零库写、零文件写（只出统计与拒绝样本）；`live=True` ⇒ 过门对
    逐条落 `strategy_instances`（status=proposed，判定≠升格）+ 写 JSONL 旁路账本
    （含被门拒掉的完整配对与拒绝理由原文）。"""
    rep = CX.run_contrast(session, pairs, live=live,
                          extractor_model=extractor_model,
                          ledger_path=ledger_path)
    return {"stage": "evidence", **rep}


# --------------------------------- 证据完整性：把 AI 侧并入冻结证据（可复算）
def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def attach_ai_side(session: Session, pairs: list[CX.ContrastPair]) -> dict:
    """把调用方注入的 **AI 侧原文**并进已落实例的 `conditions_observed`。

    ## 为什么必须补这一步（2026-09-30 现场实测，非推断）

    `k2_contrast_extract._persist_one` 只把 AI 侧记成 `ai_side_sha256` +
    `ai_side_chars`，**AI 侧原文不入库**。而 `app.semantic_review.build_snapshot`
    冻结给两席的 `review_input` 逐字取 `conditions_observed`——于是两席看到
    的每条实例只有**人类侧**一句原文（`evidence_text` == `segment_text`）加一个
    sha256，**对照的另一极从未出现在证据里**。

    实测后果（副本库，G:/lg_tmp/k2v2 两张 v2 卡各一轮真收据）：
    `kimi-k3` 判 PASS、`deepseek-v4.1-flash` 判 **ABSTAIN**，理由逐字为
    「冻结证据只给出各实例的 evidence_text 与 observed_content 标签，未提供
    人类侧对照文本或 AI 侧新增句与人类侧原文的差异比对，无法独立核实……」。

    这**不是**判定席过严或证据不足，而是**证据本身缺了对照的一极**：本协议
    的立论就是「同一场景人类留白 vs AI 摊开」，只给人类侧时该主张在结构上
    不可核验，ABSTAIN 是如实反应。补上 AI 侧后**同一个模型、同一份冻结输入**
    （只多一个已声明字段）改判 PASS（现场复核逐字见 DELIVERY 证据二·0）。

    ## 纪律（不许靠这个放宽任何东西）

    - AI 侧**只**来自 `--ai-side` 注入的同一批 `ContrastPair`（本件不联网、
      不生成、不从账本回读）；落进去的每一段都用**门已判过的**那份原文。
    - 逐对按 `human_sha256` 对齐到已落实例；**对不上即拒**（不猜、不按序配）。
    - 幂等：同输入重跑只写一次（值相同则 no-op），不新增实例行。
    - 判据一字不改：六道门（`CX.GATES`）在 `attach` 之前已判完且**全 pass**
      才可能落行；本函数**不重判、不放宽、不碰** `status`/`reviewer_version`。
    - 冲突即拒：若实例上已有 `ai_side_text` 且与注入值逐字不同 ⇒ 拒
      （同一 human 侧配两个不同 AI 侧 = 证据歧义，fail-visible）。
    """
    attached, unchanged, refused = [], [], []
    for pair in pairs:
        from app.models import StrategyInstance
        st = CX.resolve_strategy(session, pair.strategy_key)
        if st is None:
            refused.append({"pair": CX.pair_id(pair),
                            "why": f"库内无可用策略卡 {pair.strategy_key}"})
            continue
        row = (session.query(StrategyInstance)
               .filter(StrategyInstance.strategy_id == st.id,
                       StrategyInstance.strategy_version == st.version,
                       StrategyInstance.evidence_sha256 == pair.human_sha256)
               .first())
        if row is None:
            refused.append({"pair": CX.pair_id(pair),
                            "why": "无对应已落实例（该对未落库或已不在本卡上）"})
            continue
        observed = dict(row.conditions_observed or {})
        if observed.get("protocol") != CX.PROTOCOL:
            refused.append({"instance_id": row.id,
                            "why": "非本协议产出的实例，不碰"})
            continue
        digest = _sha256(pair.ai_text)
        if observed.get("ai_side_sha256") and \
                observed["ai_side_sha256"] != digest:
            refused.append({"instance_id": row.id,
                            "why": f"ai_side_sha256 与注入 AI 侧不符"
                                   f"（库={observed['ai_side_sha256'][:12]}… "
                                   f"注入={digest[:12]}…）——证据歧义，拒绝"})
            continue
        prior = observed.get("ai_side_text")
        if prior is not None:
            if prior == pair.ai_text:
                unchanged.append(row.id)
            else:
                refused.append({"instance_id": row.id,
                                "why": "已有 ai_side_text 且与注入值逐字不同"
                                       "（同一 human 侧配两个 AI 侧）——拒绝"})
            continue
        observed["ai_side_text"] = pair.ai_text
        observed["ai_side_sha256"] = digest
        observed["ai_side_chars"] = len(pair.ai_text)
        observed["human_side_chars"] = len(pair.human_text or "")
        row.conditions_observed = observed
        attached.append({"instance_id": row.id, "strategy_id": st.id,
                         "op": pair.op, "op_label": observed.get("op_label"),
                         "ai_side_chars": len(pair.ai_text)})
    session.commit()
    return {"stage": "attach", "protocol": CX.PROTOCOL,
            "n_pairs": len(pairs), "n_attached": len(attached),
            "attached": attached, "unchanged": unchanged, "refused": refused}


# ---------------------------------------------- 证据实例升格（机械、可复算）
def attest_instances(session: Session) -> dict:
    """`proposed → verified` + 打新口径复审标记 `k2def-v1`。

    升格资格是**可复算的机械事实**，不是判断：本协议落库的实例带
    `conditions_observed.gates` 六门逐门结果，且落库那一刻已经过
    `app.knowledge.verify_instance_span` 对登记段逐字复核。资格三件套缺一即拒：
      ① `extractor_model == paired_contrast_v2`（非本协议产出不碰）；
      ② `conditions_observed.gates` 六门**全为 pass**（不认口头声称）；
      ③ 现 `status == 'proposed'`（已升格过不重复计数）。
    落库时另按 `evidence_sha256` 幂等；`reviewer_version` 只写标记位一个列，
    不改 span / work_id / text_version / status 语义以外的任何东西。
    """
    gates_all_pass = set(dict.fromkeys(name for name, _f in CX.GATES))
    rows = session.query(StrategyInstance).all()
    attested, refused = [], []
    for ins in rows:
        if ins.extractor_model != CX.PROTOCOL:
            continue
        observed = (ins.conditions_observed or {})
        if observed.get("protocol") != CX.PROTOCOL:
            refused.append({"instance_id": ins.id,
                            "why": "conditions_observed.protocol 不符"})
            continue
        gates = observed.get("gates") or {}
        if set(gates) != gates_all_pass or any(
                v != "pass" for v in gates.values()):
            refused.append({"instance_id": ins.id,
                            "why": f"门记录不全/有非 pass: {gates}"})
            continue
        if ins.status != "proposed":
            refused.append({"instance_id": ins.id,
                            "why": f"status={ins.status}（非 proposed，不重复升格）"})
            continue
        if (ins.reviewer_version or "") == KE.REVIEW_MARKER_NEW_DEF:
            refused.append({"instance_id": ins.id, "why": "已带复审标记"})
            continue
        ins.status = "verified"
        ins.reviewer_version = KE.REVIEW_MARKER_NEW_DEF
        attested.append({"instance_id": ins.id, "strategy_id": ins.strategy_id,
                         "work_id": ins.work_id, "segment_id": ins.segment_id,
                         "op": observed.get("op"),
                         "op_label": observed.get("op_label")})
    session.commit()
    return {"stage": "attest", "protocol": CX.PROTOCOL,
            "review_marker": KE.REVIEW_MARKER_NEW_DEF,
            "attested": attested, "n_attested": len(attested),
            "refused": refused}


# ------------------------------------------------------------ 晋升阶梯
def ladder(db_path: Path, strategy_id: str, *, to: str, reviewer: str,
           version: int = V2_VERSION) -> dict:
    """走一级阶梯：`evaluate()`（判词单源，只读）→ 通过则 `commit_promotion()`。

    批量升格红线照用既有执行器：一次只处理**一条**策略（`evaluate` 逐条产出，
    逐条提交），不引入第二条写路径。`--to verified` 缺两席收据时 `evaluate`
    自身就返回 `semantic_review_unverifiable:*` 且 `plan=None` ⇒ 库里什么都不写。
    """
    s = KP.open_ro_session(db_path)
    try:
        st = s.get(ExpressionStrategyV2, strategy_id)
        if st is None:
            raise EvidenceAttestError(f"strategy_missing:{strategy_id}")
        verdict = KP.evaluate(s, st, {}, to, reviewer)
    finally:
        s.close()
    if verdict["decision"] != KP.PROMOTE:
        return {"stage": "ladder", "strategy_id": strategy_id,
                "requested_to": to, "decision": verdict["decision"],
                "reason": verdict["reason"], "line": verdict["line"],
                "committed": False}
    result = KP.commit_promotion(Path(db_path), verdict)
    return {"stage": "ladder", "strategy_id": strategy_id,
            "requested_to": to, "decision": verdict["decision"],
            "reason": verdict["reason"], "line": verdict["line"],
            "committed": True, "write": result}


def scope_claim(db_path: Path, strategy_id: str, version: int = V2_VERSION
                ) -> dict:
    """当前卡行上的 scope 声明（K5 侧「拟准入范围」）——快照与 `verified` 写侧
    都以它为 claim；`approved_selected` 之后也按同一组值复核。"""
    # 用本进程引擎而非 `k5_promotion_gate_explain` 的 `mode=ro` 连接：副本库处于
    # WAL 模式时只读连接无法恢复 `-shm`（sqlite3 CANTOPEN）。此处只读不写。
    engine = open_engine(db_path)
    try:
        with Session(engine) as s:
            st = s.get(ExpressionStrategyV2, strategy_id)
            if st is None:
                raise EvidenceAttestError(f"strategy_missing:{strategy_id}")
            return {"scope_to": st.scope, "scope_ids": list(st.scope_ids or []),
                    "scope_basis": st.scope_basis}
    finally:
        engine.dispose()


# ------------------------------------------------------ 两席收据（真调用）
def mint_receipts(db_path: Path, strategy_id: str, *,
                  version: int = V2_VERSION, seat_count: int = 2) -> dict:
    """冻结当前证据的一轮审查快照，并把**两席**真投给上游网关。

    席位身份/上游路由全部来自 `LG_ATTEST_*`（`k2_receipt_mint` 读法：第 N 席
    `LG_ATTEST_<N>_*`，第 1 席兼容无前缀）；两席 `upstream_model` 必须互异
    （消费侧按 `(snapshot_id, model_id)` 投一票即锁，准入侧也要求同 snapshot
    恰好 2 票、模型互异、每模型恰好 1 次 call、两票都 PASS）。本函数**不生成
    判词**：`review_snapshot` 走真实上游调用，失败逐条记 `error_type` +
    `error_message` 原文。
    """
    engine = open_engine(db_path)
    attached: list[MINT.AttachedSeat] = []
    try:
        from app.semantic_receipts import ensure_semantic_schema
        from app.semantic_review_store import freeze_snapshot
        claim = scope_claim(db_path, strategy_id, version)
        ensure_semantic_schema(engine)
        snapshot = freeze_snapshot(engine, strategy_id, version, claim)
        routes = MINT.seat_routes_from_env(seat_count=seat_count)
        attached = MINT.start_gateways(routes)
        summary = MINT.mint(engine, [snapshot["snapshot_id"]], attached)
        return {"stage": "receipts", "strategy_id": strategy_id,
                "claim": claim, "snapshot": snapshot,
                "seats": summary["seats"], "results": summary["results"],
                "ok": summary["ok"], "failed": summary["failed"],
                "readout": summary["readout"]}
    finally:
        MINT.stop_gateways(attached)
        engine.dispose()


# ------------------------------------------------------------------ 读数
def approval_readout(db_path: Path, strategy_id: str, *,
                     version: int = V2_VERSION) -> dict:
    """对单张 v2 卡跑 K4 消费的那个函数（`approved_selected`）：返回准入清单
    或 `ApprovalError` 原文。`selected` 条目逐字取自 `query_knowledge` 的产出，
    本件不自己拼。"""
    from app.semantic_admission import approved_selected
    from app.semantic_approval import ApprovalError
    from app.promotion_audits import PromotionAuditSchemaError
    from app.semantic_receipts import ReceiptSchemaError
    engine = open_engine(db_path)
    try:
        with Session(bind=engine) as s:
            entry = _selected_entry(s, strategy_id, version)
            try:
                manifest = approved_selected(s, [entry])
            except (ApprovalError, PromotionAuditSchemaError,
                    ReceiptSchemaError) as exc:
                return {"stage": "readout", "strategy_id": strategy_id,
                        "admitted": False,
                        "error": f"{type(exc).__name__}:{exc}"}
            return {"stage": "readout", "strategy_id": strategy_id,
                    "admitted": True, "manifest": manifest}
    finally:
        engine.dispose()


def _selected_entry(session: Session, strategy_id: str, version: int) -> dict:
    """`approved_selected` 要的 `selected` 条目：从**当前卡行**逐字读出来
    （与 `query_knowledge` 的 entry 同字段同来源，不另拼一份）。"""
    st = session.get(ExpressionStrategyV2, strategy_id)
    if st is None:
        raise EvidenceAttestError(f"strategy_missing:{strategy_id}")
    if st.version != version:
        raise EvidenceAttestError(
            f"strategy_version_mismatch:{strategy_id}:{st.version}!={version}")
    return {"strategy_id": st.id, "strategy_key": st.strategy_key,
            "version": st.version, "status": st.status,
            "scope": st.scope, "observation_status": st.observation_status,
            "effect_status": st.effect_status,
            "abstract_operation": st.abstract_operation,
            "invariants": st.invariants, "failure_modes": st.failure_modes,
            "effect_hypothesis": st.effect_hypothesis}


def k3_readout(db_path: Path, book_id: str, *,
               versions: set[str] | None = None) -> dict:
    """K3-A 收窄读数：按 `versions`（如 `{"2"}`）查该已登记世界的 A 臂候选，
    报 `k3_status` / `selected_ids` / `scope_ids` 原文 + 逐卡拒绝理由。

    A 臂 policy 构造照抄 `k4_paired_scenes.build_a_arm_policy`（K4 唯一的
    消费口径；`app/scene_runtime/knowledge_v2.py` 不可改，故在此复刻其字段）。
    """
    from app import knowledge_query as KQ
    from app.knowledge import PACKAGE_CONTRACT_VERSION
    engine = open_engine(db_path)
    try:
        with Session(bind=engine) as s:
            policy = _a_arm_policy(book_id)
            resp = KQ.query_knowledge(policy, s, versions=versions)
            selected = resp.get("selected") or []
            scope_ids = {}
            for e in selected:
                st = s.get(ExpressionStrategyV2, e.get("strategy_id"))
                scope_ids[e.get("strategy_id")] = list(st.scope_ids or []) \
                    if st is not None else None
            return {"stage": "readout", "book_id": book_id,
                    "versions": sorted(versions) if versions else None,
                    "k3_status": resp.get("status"),
                    "selected_ids": [e.get("strategy_id") for e in selected],
                    "selected": [{"strategy_id": e.get("strategy_id"),
                                  "strategy_key": e.get("strategy_key"),
                                  "version": e.get("version"),
                                  "scope": e.get("scope"),
                                  "observation_status": e.get("observation_status"),
                                  "evidence_count": e.get("score_components", {}).get(
                                      "evidence_count"),
                                  "evidence_root_works": e.get(
                                      "evidence_root_works")} for e in selected],
                    "scope_ids": scope_ids,
                    "rejected": resp.get("rejected"),
                    "budget": resp.get("budget"),
                    "package_sha256": resp.get("package_sha256")}
    finally:
        engine.dispose()


def _a_arm_policy(book_id: str) -> dict:
    """A 臂知识包 policy（与 `k4_paired_scenes.build_a_arm_policy` 同字段；
    book_id 只入参 echo，查询侧不按 book_id 收窄候选——收窄靠 `scope` 匹配）。"""
    from app.knowledge import PACKAGE_CONTRACT_VERSION
    return {"contract_version": PACKAGE_CONTRACT_VERSION, "book_id": book_id,
            "branch_id": None, "scene_id": "k4-s0", "semantic_requirements": {
                "goal": "支付一枚钱（s0）", "pov": "lin", "style": "简洁"},
            "limits": {"context_items": 3}}


def card_status_readout(db_path: Path) -> list[dict]:
    """两张 v2 卡的当前列取值（`scope_ids` 原文逐字，不截断）。"""
    s = KP.open_ro_session(db_path)
    try:
        rows = []
        for spec in CARD_SPECS:
            st = s.get(ExpressionStrategyV2, card_id(spec["strategy_key"]))
            rows.append({
                "strategy_id": st.id if st else card_id(spec["strategy_key"]),
                "strategy_key": spec["strategy_key"], "label": spec["label"],
                "version": st.version if st else V2_VERSION,
                "status": st.status if st else None,
                "observation_status": st.observation_status if st else None,
                "effect_status": st.effect_status if st else None,
                "scope": st.scope if st else None,
                "scope_ids": list(st.scope_ids or []) if st else [],
                "scope_basis": st.scope_basis if st else "",
                "legacy_strategy_id": st.legacy_strategy_id if st else None})
        return rows
    finally:
        s.close()


def instance_readout(db_path: Path) -> list[dict]:
    """本协议落下的证据实例逐条读数（op / op_label / 门结果 / 复审标记）。"""
    engine = open_engine(db_path)
    try:
        with Session(bind=engine) as s:
            out = []
            for ins in s.query(StrategyInstance).order_by(StrategyInstance.id):
                if ins.extractor_model != CX.PROTOCOL:
                    continue
                obs = ins.conditions_observed or {}
                out.append({"instance_id": ins.id, "strategy_id": ins.strategy_id,
                            "work_id": ins.work_id, "segment_id": ins.segment_id,
                            "status": ins.status,
                            "reviewer_version": ins.reviewer_version,
                            "op": obs.get("op"), "op_label": obs.get("op_label"),
                            "scene_keys": obs.get("scene_keys"),
                            "gates": obs.get("gates"),
                            "evidence_sha256": ins.evidence_sha256,
                            "span": [ins.span_start, ins.span_end]})
            return out
    finally:
        engine.dispose()


# ------------------------------------------------------------------- CLI
STAGES = ("cards", "evidence", "attach", "attest", "ladder", "receipts",
          "verified", "readout", "all")


def _reviewer(a) -> str:
    reviewer = (a.reviewer or "").strip()
    if not reviewer:
        raise SystemExit("--reviewer 必填（无签认不写；判红即缺签认身份）")
    return reviewer


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, dest="db",
                    help="目标库路径（必须是**副本库**；指向真库即拒）")
    ap.add_argument("--repo-root", default=str(ROOT), dest="repo_root")
    ap.add_argument("--stage", default="readout", choices=(*STAGES, "all"))
    ap.add_argument("--ai-side", default="", dest="ai_side",
                    help="调用方注入的成对 JSON（--stage evidence/attach 必填；"
                         "AI 侧只从这里来，本件不联网生成）")
    ap.add_argument("--pairs-ledger", default="k2_pairs.jsonl",
                    dest="pairs_ledger", help="JSONL 旁路账本路径")
    ap.add_argument("--live", action="store_true", dest="live",
                    help="证据阶段真落库（缺省只出统计、零库写）")
    ap.add_argument("--reviewer", default="", help="签认身份（ladder/verified 必填）")
    ap.add_argument("--seats", type=int, default=2, dest="seats",
                    help="receipts 阶段的席位数（真跑缺任一席即投不满两票）")
    ap.add_argument("--book-id", default="", dest="book_id",
                    help="readout 阶段的已登记世界")
    ap.add_argument("--versions", default="", dest="versions",
                    help="readout 阶段的版本收窄，逗号分隔（如 2 ⇒ {\"2\"}）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args(argv)

    try:
        db_path = assert_copy_target(a.db, a.repo_root)
    except RealDatabaseRefused as exc:
        print(f"[{TOOL}] REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if not Path(db_path).is_file():
        print(f"[{TOOL}] 库不存在：{db_path}", file=sys.stderr)
        return EXIT_ERROR

    stages = list(STAGES[:-1]) if a.stage == "all" else [a.stage]
    out: list[dict] = []
    try:
        engine = open_engine(db_path)
        try:
            for stage in stages:
                out.append(_run_stage(stage, a, db_path, engine))
        finally:
            engine.dispose()
    except (SnapshotError, KP.PromotionGuardError, EvidenceAttestError,
            RealDatabaseRefused, ValueError) as exc:
        print(f"[{TOOL}] 运行错误 {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:                       # noqa: BLE001  逐字记账
        print(f"[{TOOL}] 运行错误 {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps({"tool": TOOL, "schema": SCHEMA, "db": str(db_path),
                      "stage": a.stage, "results": out},
                     ensure_ascii=False, indent=1))
    if a.as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return EXIT_REFUSED if any(r.get("failed") or r.get("admitted") is False
                               or r.get("decision") == KP.NO_PROMOTE
                               for r in out) else EXIT_OK


def _run_stage(stage: str, a, db_path: Path, engine: Engine) -> dict:
    """一个阶段 = 一个独立读数。`all` 顺序即依赖顺序（卡 → 证据 → 并 AI 侧 →
    升格 → 阶梯 → 收据 → verified → 读数）。"""
    if stage == "cards":
        with Session(bind=engine) as s:
            return build_cards(s)
    if stage == "evidence":
        if not a.ai_side:
            raise ValueError("--stage evidence 需要 --ai-side（AI 侧只由调用方"
                             "注入；本件不联网生成）")
        pairs = CX.load_pairs_file(a.ai_side)
        with Session(bind=engine) as s:
            return extract_evidence(s, pairs, live=a.live,
                                    ledger_path=a.pairs_ledger)
    if stage == "attest":
        with Session(bind=engine) as s:
            return attest_instances(s)
    if stage == "attach":
        if not a.ai_side:
            raise ValueError("--stage attach 需要 --ai-side（AI 侧只由调用方"
                             "注入；本件不联网生成、不从账本回读）")
        pairs = CX.load_pairs_file(a.ai_side)
        with Session(bind=engine) as s:
            return attach_ai_side(s, pairs)
    if stage in ("ladder", "verified"):
        if not a.reviewer.strip():
            raise ValueError(f"--stage {stage} 需要非空 --reviewer（无签认不写）")
        # hypothesis -> observed -> replicated：每次一事务、只走一级（禁跳级由
        # evaluate 自己拒，本件不绕）。verified 是单级 replicated -> verified。
        targets = (("observed", "replicated") if stage == "ladder"
                   else ("verified",))
        rows = []
        for spec in CARD_SPECS:
            sid = card_id(spec["strategy_key"])
            for to in targets:
                rows.append(ladder(db_path, sid, to=to,
                                   reviewer=a.reviewer.strip()))
        return {"stage": stage, "verdicts": rows,
                "failed": sum(1 for r in rows if not r["committed"])}
    if stage == "receipts":
        rows = []
        for spec in CARD_SPECS:
            rows.append(mint_receipts(db_path, card_id(spec["strategy_key"]),
                                      seat_count=a.seats))
        return {"stage": stage, "rounds": rows,
                "failed": sum(r["failed"] for r in rows)}
    if stage == "readout":
        out = {"stage": "readout", "cards": card_status_readout(db_path),
               "instances": instance_readout(db_path), "admission": []}
        for spec in CARD_SPECS:
            out["admission"].append(
                approval_readout(db_path, card_id(spec["strategy_key"])))
        if a.book_id:
            versions = ({v.strip() for v in a.versions.split(",") if v.strip()}
                        or None) if a.versions else None
            out["k3"] = k3_readout(db_path, a.book_id, versions=versions)
        out["failed"] = sum(1 for r in out["admission"] if not r["admitted"])
        return out
    raise ValueError(f"unknown_stage:{stage}")


if __name__ == "__main__":
    raise SystemExit(main())
