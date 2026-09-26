"""K2 非基准供给登记器（k2-rereview-nonbench，2026-09-26 派工）。

补上「机制已修、供给为零」之间的缺失一环：真库 `work_sources.source_type`
里**没有任何** `production_nonbenchmark_` 前缀族行（主控只读实测：fixture 2 /
human_fiction 7），segments 的 role 只有 benchmark 833 与 NULL 437,585，
82/83 条 strategy_instances 证据全落在 benchmark 段上 ⇒ K3 的 A 臂（非基准
独立证据）恒空。本件把「登记一批合规非基准来源 + 切段入库」做成可重跑的
确定性管道，并用 `--preflight` 给出**登记前 vs 登记后（合成库模拟）**的
`k3_reachable_segments` / `a_arm_evidence_count` 对照，把「A 臂为什么是 0、
登记后为什么能非 0」变成机器可核的两行数字。

**单源复用纪律（硬约束 C2/C3）**：判据本体一律取自既有实现，本文件不另写
一套——
- 来源类型合规：`k2_extract_backfill.nonbenchmark_compliant_source`
  （前缀常量 `NONBENCHMARK_SOURCE_TYPE_PREFIX` 只 import 引用，不落本地字面量）；
- 段 role 口径：`k2_extract_backfill._role_passes_scope("nonbenchmark", role)`
  与 `BENCHMARK_ROLE`；
- K3 来源闸（可达段指标）：`k2_extract_backfill._k3_source_gate`
  （即 `knowledge_query._evidence_for` 单实例块的同序镜像）；
- A 臂证据数：直接调 `app.knowledge_query._evidence_for` 本尊；
- 列宽硬校验：`register_work_sources._check_source_type_width`；
- 复审标记：`app.knowledge_extract.REVIEW_MARKER_NEW_DEF`（预演探针实例用，
  不新造标记位）。
本件**不改** `app/knowledge_query.py` 的排除集、**不动**既有 benchmark 段的
任何一行（C3：只做增量插行，无 UPDATE/DELETE 路径——源码 grep 由回归钉死）。

硬契约（与任务书逐条对齐，回归测试同名钉死）：
- C1 缺省即 `--dry-run`：零写入（真库 md5+mtime 逐字节不变），判定阶段
  一次都不打开可写连接（回归把 `open_write_connection` 换成「一调用就炸」）；
- C2 只接受 `production_nonbenchmark_` 前缀族的 source_type（由 `--tag` 拼出
  后仍过 `nonbenchmark_compliant_source`——拼不出合规值即拒），段 role 必须
  显式非空且 != benchmark；
- C3 只增不改：写路径只有 INSERT，判定只读（`mode=ro` 连接层）；
- C4 `--preflight`：真库上只读算「登记前」，把库备份进临时合成副本、在副本
  上走**同一套**登记写路径 + 注入预演探针实例，再算「登记后」，两行对照打印
  `k3_reachable_segments` / `a_arm_evidence_count`；登记后两指标任一为 0 ⇒
  本件以非零退出（可证伪方向：机制若回退，preflight 必须炸，不许静默绿灯）；
- C5 `--commit`：单事务（`BEGIN IMMEDIATE`，任一步异常整体回滚）；幂等——
  同一内容重跑 = 全 skip、零写入、不开可写连接；内容漂移（同 work 键不同
  文本哈希）响亮拒绝，不静默重锚；
- C6 零真实模型调用：本件是纯确定性文本切分+插行，唯一名义上的模型入口
  `live_model_client()` 只声明不被任何路径调用；`--live` 直接拒（rc=1）。

诚实边界（写进报告输出，不许被引用成别的口径）：
- 「登记后 A 臂非空」是**合成库模拟**（探针实例是 `extractor_model=
  "preflight-probe"` 的假证据，只证明来源闸放行，不证明任何策略被证据支撑）；
  **不构成「K4 已解冻」**——真库里全部策略卡仍停在 status=hypothesis，
  K3 候选预筛（status/observation 门）通过数照样如实打印
  （`n_k3_status_gate_pass_strategies`），当前真库读数是 0；
- 登记的段 `integrity` 留 NULL：本件不自证 `src_ok`（那是 source_check 的
  活），所以这些段**不会**进 K2 抽取池，只会进 K3 来源闸口径的可达统计——
  残留项，如实交还主控。

用法：
    python scripts/k2_nonbench_register.py --corpus 语料.txt --tag k2nb        # dry-run
    python scripts/k2_nonbench_register.py --corpus 目录/ --tag k2nb --preflight
    python scripts/k2_nonbench_register.py --corpus 目录/ --tag k2nb --commit --reviewer R1
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 工作树根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import k2_extract_backfill as k2b                       # noqa: E402  口径单源
import k5_promotion_gate_explain as GE                  # noqa: E402  ro 连接层
import register_work_sources as rws                     # noqa: E402  列宽校验
from app import knowledge_query as KQ                   # noqa: E402  A 臂本体
from app.models import (ExpressionStrategyV2, Segment,  # noqa: E402
                        Work, WorkSource)

TOOL = "k2_nonbench_register"
EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2
PROMOTE_TAG = "PRECHECK-PASS"
NO_GO_TAG = "NO-GO"
PROBE_EXTRACTOR = "preflight-probe"
DEFAULT_ROLE = "train"
DEFAULT_TEXT_VERSION = "corpus-v1"
DEFAULT_PROBE_MAX = 50
# 段落切分：空行分界（与任务书设计一致；确定性、无模型）。
PARA_SPLIT = re.compile(r"\n\s*\n")
SENT_SPLIT = re.compile(r"[。！？；!?;\n]")


class RegistrarRefused(RuntimeError):
    """前置不齐（口径不合 / 漂移冲突 / 选择集非法）——写之前的响亮拒绝。"""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
        ).isoformat(timespec="seconds")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------- 连接层（C1/C3 的抓手）
def open_write_connection(db_path: Path) -> sqlite3.Connection:
    """**唯一**的可写连接入口，且只允许 `--commit` 的真实写路径调用。
    dry-run / preflight 判定 / 合成副本写都不得经过这里（回归用「一调用
    就炸」的替身钉死这条纪律）。"""
    return sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)


def open_synth_connection(db_path: Path) -> sqlite3.Connection:
    """合成副本专用连接：preflight 只写 tempfile 里的副本，永不指向真库。"""
    return sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)


def open_ro_session(db_path: Path):
    """真库一律 `mode=ro`（连接层成立，不靠自觉）——复用解释器的实现。"""
    return GE.open_ro_session(Path(db_path))


# ------------------------------------------------------------ 输入与计划
def iter_corpus_files(corpus: Path) -> list[Path]:
    """--corpus 接受单文件或目录（目录取排序后的 *.txt）。"""
    p = Path(corpus)
    if p.is_file():
        return [p]
    if p.is_dir():
        files = sorted(x for x in p.glob("*.txt") if x.is_file())
        if not files:
            raise RegistrarRefused(f"目录里没有 .txt 语料：{p}")
        return files
    raise RegistrarRefused(f"--corpus 路径不存在：{p}")


def split_paragraphs(text: str) -> list[str]:
    """确定性切段：空行分界、去空白、丢空段、单段截到列语义内（Text 无上限，
    但 20,000 字以内的正常段落原样保留；超长段落按 2000 字硬切不吞字）。"""
    out: list[str] = []
    for para in PARA_SPLIT.split(text.replace("\r\n", "\n")):
        para = para.strip()
        if not para:
            continue
        if len(para) <= 2000:
            out.append(para)
        else:
            out.extend(para[i:i + 2000] for i in range(0, len(para), 2000))
    return out


def work_id_for(tag: str, filename: str) -> str:
    """work 主键 = (tag, 文件名) 的稳定哈希——**不含内容**：内容漂移必须
    撞出 drift_conflict（响亮），而不是换键静默插第二份。"""
    return "WK-" + _sha(f"nonbench|{tag}|{filename}")[:12]


def segment_id_for(work_id: str, ordinal: int, text: str) -> str:
    return "SEG-" + _sha(f"{work_id}|{ordinal}|{_sha(text)}")[:24]


def build_plan(files: list[Path], *, tag: str, role: str,
               text_version: str, basis: str) -> dict:
    """校验 + 组计划（纯函数，不碰库）。任何不合口径的输入在这一步就炸。"""
    source_type = f"{k2b.NONBENCHMARK_SOURCE_TYPE_PREFIX}{tag}"
    # C2：拼出来的值仍过唯一判定入口——前缀常量被人改掉/拼错即拒，
    # 不接受「本地再写一条 startswith」的第二口径。
    if not k2b.nonbenchmark_compliant_source(source_type):
        raise RegistrarRefused(
            f"source_type={source_type!r} 不过 "
            "k2_extract_backfill.nonbenchmark_compliant_source——拒登记")
    rws._check_source_type_width(source_type)
    # C2：段 role 显式非空、非 benchmark，且过 nonbenchmark 口径同一支笔。
    role_key = (role or "").strip()
    if not role_key or role_key == k2b.BENCHMARK_ROLE:
        raise RegistrarRefused(
            f"段 role 必须显式非空且 != {k2b.BENCHMARK_ROLE!r}，收到 "
            f"{role!r}——拒登记")
    if not k2b._role_passes_scope("nonbenchmark", role_key):
        raise RegistrarRefused(f"role={role_key!r} 不过 nonbenchmark 口径")
    works: list[dict] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        paras = split_paragraphs(text)
        if not paras:
            raise RegistrarRefused(f"语料文件切出 0 段：{f.name}")
        wid = work_id_for(tag, f.name)
        segs = [{"id": segment_id_for(wid, n, t), "work_id": wid,
                 "ordinal": n, "text": t, "n_chars": len(t),
                 "n_sentences": max(1, len([x for x in SENT_SPLIT.split(t)
                                            if x.strip()])),
                 "sha256": _sha(t)} for n, t in enumerate(paras)]
        works.append({
            "work_id": wid, "filename": f.name, "title": f.stem,
            "source_type": source_type, "text_version": text_version,
            "role": role_key, "basis": basis,
            # 内容锚：按 ordinal 序拼接段文本的 sha256（K1-A 同款纪律：
            # 漂移 ⇒ 响亮拒绝，不静默重锚）。
            "anchor": _sha("\n".join(t for t in paras)),
            "segments": segs,
        })
    return {"tool": TOOL, "tag": tag, "source_type": source_type,
            "text_version": text_version, "role": role_key, "works": works}


# ------------------------------------------- 既有状态核对（幂等/漂移判定）
def inspect_state(db_path: Path, plan: dict) -> dict:
    """ro 核对每个 work 键的现状：absent=待插 / identical=跳过 /
    drift_conflict=内容变了（响亮拒绝）。"""
    out = {}
    s = open_ro_session(db_path)
    try:
        for w in plan["works"]:
            row = (s.query(WorkSource.work_id, WorkSource.text_sha256,
                           WorkSource.source_type)
                   .filter(WorkSource.work_id == w["work_id"]).first())
            if row is None:
                out[w["work_id"]] = {"state": "absent", "n_segments": 0}
                continue
            state = ("identical" if row.text_sha256 == w["anchor"]
                     else "drift_conflict")
            n_seg = (s.query(Segment).filter(Segment.work_id ==
                                             w["work_id"]).count())
            out[w["work_id"]] = {"state": state, "n_segments": n_seg,
                                 "registered_source_type": row.source_type}
    finally:
        s.close()
    return out


# ----------------------------------------------------- 写侧（单事务，C5）
def commit_registration(db_path: Path, plan: dict, *, conn_factory=None,
                        reviewer: str | None = None) -> dict:
    """一次事务插入全部缺失的 works/work_sources/segments 行；全是
    identical 时**不开任何连接**（幂等重跑 = 零写入的强形态）。
    任何异常 ⇒ ROLLBACK，两表回到事务前状态。只 INSERT，无 UPDATE/DELETE。
    conn_factory 缺省在**调用时**解析模块名 `open_write_connection`
    （晚绑定：回归把它换成替身即可精确计数真实写路径的打开次数）。"""
    if conn_factory is None:
        conn_factory = open_write_connection
    state = inspect_state(db_path, plan)
    if any(v["state"] == "drift_conflict" for v in state.values()):
        bad = [k for k, v in state.items() if v["state"] == "drift_conflict"]
        raise RegistrarRefused(
            f"内容漂移（同 work 键、锚哈希不符）：{bad}——不静默重锚，拒写")
    todo = [w for w in plan["works"] if state[w["work_id"]]["state"] == "absent"]
    if not todo:
        return {"committed": False, "written_works": 0, "written_segments": 0,
                "skipped_identical": len(plan["works"]), "connections_opened": 0,
                "note": "全部 work 键内容与锚一致——幂等重跑，零写入"}
    ts = _now_iso()
    con = conn_factory(Path(db_path))
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            for w in todo:
                con.execute(
                    "INSERT INTO works (id, title, source, created_at)"
                    " VALUES (?,?,?,?)",
                    (w["work_id"], w["title"],
                     f"nonbench_register:{plan['tag']}:{w['filename']}", ts))
                con.execute(
                    "INSERT INTO work_sources (id, work_id, canonical_work_id,"
                    " author_id, genre_ids, source_type, text_version,"
                    " text_sha256, purpose_basis, identity_purposes,"
                    " license_purposes, license_basis, allowed_purposes,"
                    " metadata_status, metadata_basis, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("WSRC-" + w["work_id"][3:], w["work_id"], w["work_id"],
                     None, json.dumps([]), w["source_type"],
                     w["text_version"], w["anchor"], w["basis"],
                     json.dumps([]), json.dumps([]), None, json.dumps([]),
                     "unverified",
                     "K2 非基准登记器批量登记（签认="
                     + (reviewer or "合成副本预演/无")
                     + "）：作者/题材未逐一考据，如实留空（不猜）；"
                     "src_ok 不由本件自证",
                     ts))
                for seg in w["segments"]:
                    con.execute(
                        "INSERT INTO segments (id, work_id, ordinal, text,"
                        " text_clean, n_sentences, n_chars, integrity, role,"
                        " seg_version, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (seg["id"], w["work_id"], seg["ordinal"], seg["text"],
                         seg["text"], seg["n_sentences"], seg["n_chars"],
                         None, w["role"], 1, ts))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return {"committed": True, "written_works": len(todo),
                "written_segments": sum(len(w["segments"]) for w in todo),
                "skipped_identical": len(plan["works"]) - len(todo),
                "connections_opened": 1, "ts": ts}
    finally:
        con.close()


# ------------------------------------------------- 指标（C4 的两个数字）
def compute_metrics(db_path: Path) -> dict:
    """只读计算两个口径数字（判定连接永远 mode=ro）：

    - `k3_reachable_segments`：全库段中过 `k2b._k3_source_gate`
      （= `_evidence_for` 来源闸同序镜像）的段数；
    - `a_arm_evidence_count`：Σ 每条既有策略的 `KQ._evidence_for` 证据区间数
      （A 臂 = 非基准合格证据，直接调 K3 本尊，不镜像）；
    - `n_k3_status_gate_pass_strategies`：K3 候选预筛（status/observation 门）
      能过的策略卡数——真库现状为 0，登记不改变它（诚实注记，防「A 臂非空」
      被读成「K4 解冻」）。"""
    s = open_ro_session(db_path)
    try:
        reg = {ws.work_id: ws for ws in s.query(WorkSource).all()}
        n_seg_total = n_reach = 0
        reach_by_work: dict[str, int] = {}
        for seg in s.query(Segment).all():
            n_seg_total += 1
            ws = reg.get(seg.work_id)
            tv = ws.text_version if ws is not None else None
            if k2b._k3_source_gate(seg, tv, work_source=ws) is None:
                n_reach += 1
                reach_by_work[seg.work_id] = reach_by_work.get(
                    seg.work_id, 0) + 1
        strategy_ids = [r[0] for r in s.query(
            ExpressionStrategyV2.id).order_by(ExpressionStrategyV2.id).all()]
        a_arm = 0
        for sid in strategy_ids:
            a_arm += KQ._evidence_for(s, sid, {})[1]
        n_status_pass = 0
        for st in s.query(ExpressionStrategyV2).all():
            if (st.status in KQ.eligible_statuses(st.version)
                    and st.observation_status in KQ.ELIGIBLE_OBSERVATION):
                n_status_pass += 1
        return {"k3_reachable_segments": n_reach,
                "segments_total": n_seg_total,
                "a_arm_evidence_count": a_arm,
                "n_strategies": len(strategy_ids),
                "n_k3_status_gate_pass_strategies": n_status_pass,
                "reachable_by_work": reach_by_work}
    finally:
        s.close()


def inject_probe_instances(db_path: Path, plan: dict, *,
                           probe_max: int = DEFAULT_PROBE_MAX) -> int:
    """合成副本专用：给每条既有策略注入至多 1 条**预演探针**实例
    （status=verified + 新口径复审标记），挂在本次登记作品的段上——
    用来证明「来源闸放行后 `_evidence_for` 数得见」，不证明任何语义结论。
    只在 tempfile 副本上调用；真库永不经这里。"""
    s = open_ro_session(db_path)
    try:
        strategy_ids = [r[0] for r in s.query(
            ExpressionStrategyV2.id).order_by(ExpressionStrategyV2.id).all()]
        segs = (s.query(Segment).filter(
            Segment.work_id.in_([w["work_id"] for w in plan["works"]]))
            .order_by(Segment.work_id, Segment.ordinal).all())
        tv = plan["text_version"]
        rows = []
        for i, sid in enumerate(strategy_ids[:probe_max] if segs else []):
            seg = segs[i % len(segs)]
            rows.append((
                "SI-" + _sha(f"probe|{sid}|{seg.id}")[:24], sid, 1,
                seg.work_id, seg.id, None, tv, 0, seg.n_chars, seg.text,
                _sha(seg.text), json.dumps({}), "preflight probe（非语义证据）",
                None, PROBE_EXTRACTOR,
                _marker(), "verified", _now_iso()))
    finally:
        s.close()
    if not rows:
        return 0
    con = open_synth_connection(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            for r in rows:
                con.execute(
                    "INSERT INTO strategy_instances (id, strategy_id,"
                    " strategy_version, work_id, segment_id, frame_id,"
                    " text_version, span_start, span_end, evidence_text,"
                    " evidence_sha256, conditions_observed, observed_content,"
                    " effect_ref, extractor_model, reviewer_version, status,"
                    " created_at) VALUES ("
                    + ",".join("?" * 18) + ")", r)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return len(rows)
    finally:
        con.close()


def _marker() -> str:
    from app import knowledge_extract as KE
    return KE.REVIEW_MARKER_NEW_DEF


# ------------------------------------------------------------- 预演（C4）
def make_synth_copy(db_path: Path, tmp: Path) -> Path:
    """真库 → 临时合成副本：经 mode=ro 连接的 backup API（真库连写意图
    都不存在），副本落 tempfile，调用方负责 rmtree。"""
    dst = tmp / "synth_copy.db"
    src = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro",
                          uri=True)
    try:
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dst


def preflight(db_path: Path, plan: dict, *, probe_max: int) -> dict:
    before = compute_metrics(db_path)
    tmp = Path(tempfile.mkdtemp(prefix="k2nb_preflight_"))
    after = dict(before)
    injected = 0
    wrote: dict = {}
    try:
        copy = make_synth_copy(db_path, tmp)
        wrote = commit_registration(copy, plan,
                                    conn_factory=open_synth_connection)
        injected = inject_probe_instances(copy, plan, probe_max=probe_max)
        after = compute_metrics(copy)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    go = (after["k3_reachable_segments"] > 0
          and after["a_arm_evidence_count"] > 0)
    return {"before": before, "after": after, "commit_on_copy": wrote,
            "probe_instances_injected": injected, "go": go,
            "note": "登记后数字来自合成库模拟（临时副本 + 预演探针实例）；"
                    "不裁定任何 status/scope，不构成「K4 已解冻」——"
                    "真库 status 门通过数见 before/after 的"
                    " n_k3_status_gate_pass_strategies"}


# ------------------------------------------------ 报告渲染（判词单点）
def line_of(kind: str, m: dict, *, suffix: str = "") -> str:
    return (f"{kind} k3_reachable_segments={m['k3_reachable_segments']}"
            f" a_arm_evidence_count={m['a_arm_evidence_count']}"
            f" segments_total={m['segments_total']}"
            f" n_strategies={m['n_strategies']}"
            f" n_k3_status_gate_pass_strategies="
            f"{m['n_k3_status_gate_pass_strategies']}{suffix}")


def build_report(args, db_path: Path, plan: dict) -> tuple[dict, int, list[str]]:
    state = inspect_state(db_path, plan)
    report = {"tool": TOOL, "mode": args.mode, "db": str(db_path),
              "plan_summary": {
                  "tag": plan["tag"], "source_type": plan["source_type"],
                  "role": plan["role"], "text_version": plan["text_version"],
                  "works": [{"work_id": w["work_id"], "filename": w["filename"],
                             "n_segments": len(w["segments"]),
                             "anchor": w["anchor"][:12]} for w in plan["works"]]},
              "existing_state": state,
              "discipline": {"db_writes_during_judgement": 0,
                             "model_calls": 0,
                             "caliber_source":
                                 "k2_extract_backfill + knowledge_query 单源复用",
                             "additive_rows_only": True}}
    lines: list[str] = []
    drift = [k for k, v in state.items() if v["state"] == "drift_conflict"]
    if drift:
        report["refused"] = {"reason": "drift_conflict", "works": drift}
        lines.append(f"{NO_GO_TAG} reason=drift_conflict works={drift} "
                     "——内容漂移，不静默重锚，零写入")
        return report, EXIT_REFUSED, lines
    if args.mode == "preflight":
        pf = preflight(db_path, plan, probe_max=args.probe_max)
        report["preflight"] = pf
        lines.append(line_of("登记前", pf["before"]))
        lines.append(line_of("登记后（合成库模拟）", pf["after"],
                             suffix=f" probe_instances_injected="
                                    f"{pf['probe_instances_injected']}"))
        lines.append(pf["note"])
        if not pf["go"]:
            lines.append(f"{NO_GO_TAG} reason=after_zero ——登记后指标仍为 0，"
                         "预演不通过（本件不放行，非零退出）")
            return report, EXIT_REFUSED, lines
        lines.append(PROMOTE_TAG)
        return report, EXIT_OK, lines
    if args.mode == "commit":
        if not (args.reviewer or "").strip():
            report["refused"] = {"reason": "no_reviewer"}
            lines.append(f"{NO_GO_TAG} reason=no_reviewer ——真写必须带 "
                         "--reviewer 签认，零写入")
            return report, EXIT_ERROR, lines
        res = commit_registration(db_path, plan, reviewer=args.reviewer)
        report["commit"] = res
        for w in plan["works"]:
            st = state[w["work_id"]]
            lines.append(f"work={w['work_id']} state={st['state']} "
                         f"n_segments={len(w['segments'])}")
        lines.append(f"commit: written_works={res['written_works']} "
                     f"written_segments={res['written_segments']} "
                     f"skipped_identical={res['skipped_identical']}")
        lines.append(PROMOTE_TAG if res["committed"] or
                     res["skipped_identical"] else NO_GO_TAG)
        return report, EXIT_OK, lines
    # dry-run（缺省）：只报计划与现状，零写入
    m = compute_metrics(db_path)
    report["metrics_before"] = m
    lines.append(line_of("现状（未登记）", m))
    for w in plan["works"]:
        st = state[w["work_id"]]
        lines.append(f"dry-run work={w['work_id']} file={w['filename']} "
                     f"segments={len(w['segments'])} state={st['state']}")
    lines.append(f"{PROMOTE_TAG} mode=dry-run planned_writes="
                 f"{sum(1 for w in plan['works'] if state[w['work_id']]['state'] == 'absent')} "
                 "（计划，未写）")
    return report, EXIT_OK, lines


def live_model_client(*_a, **_k):
    """名义上唯一的模型入口——本件任何路径都不调用（C6：登记是纯确定性
    切分+插行）。保留符号只为让回归能把它换成「一调用就炸」的替身。"""
    raise RegistrarRefused("本件零模型调用：live_model_client 不允许被走到")


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=TOOL)
    ap.add_argument("--corpus", required=True,
                    help="语料 .txt 文件或含 .txt 的目录")
    ap.add_argument("--tag", required=True,
                    help="来源类型后缀：登记为 production_nonbenchmark_<tag>")
    ap.add_argument("--role", default=DEFAULT_ROLE,
                    help=f"切段的显式 role（非空且 != benchmark），默认 "
                         f"{DEFAULT_ROLE}")
    ap.add_argument("--text-version", dest="text_version",
                    default=DEFAULT_TEXT_VERSION)
    ap.add_argument("--basis", default="K2 非基准登记器批量登记（未逐一考据）",
                    help="purpose_basis 文本")
    ap.add_argument("--db", default="", help="库路径（缺省走候选序）")
    ap.add_argument("--repo-root", dest="repo_root", default=str(ROOT))
    ap.add_argument("--probe-max", dest="probe_max", type=int,
                    default=DEFAULT_PROBE_MAX)
    ap.add_argument("--reviewer", default="", help="--commit 签认人")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="mode", action="store_const",
                      const="dry-run", help="缺省模式：零写入")
    mode.add_argument("--commit", dest="mode", action="store_const",
                      const="commit", help="真实写（单事务、幂等）")
    mode.add_argument("--preflight", dest="mode", action="store_const",
                      const="preflight", help="登记前/后对照（合成库模拟）")
    ap.add_argument("--live", action="store_true",
                    help="未开放（本件零模型调用，直接拒）")
    ap.add_argument("--json-out", dest="json_out", default="")
    ns = ap.parse_args(argv)
    ns.mode = ns.mode or "dry-run"
    return ns


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.live:
        print(f"{NO_GO_TAG} reason=live_refused ——本件零真实模型调用，"
              "--live 未开放，且未发生任何写入")
        return EXIT_ERROR
    try:
        db_path, candidates = GE.resolve_db(args.db, Path(args.repo_root))
        if not db_path.exists():
            print(f"{NO_GO_TAG} reason=db_unreadable ——候选路径全部不存在，"
                  f"不猜任何数字：{candidates}")
            return EXIT_ERROR
        plan = build_plan(iter_corpus_files(Path(args.corpus)), tag=args.tag,
                          role=args.role, text_version=args.text_version,
                          basis=args.basis)
        report, rc, lines = build_report(args, db_path, plan)
    except RegistrarRefused as e:
        print(f"{NO_GO_TAG} reason=plan_refused {e}")
        return EXIT_REFUSED
    except SystemExit:
        raise
    except Exception as e:                                   # noqa: BLE001
        print(f"{NO_GO_TAG} reason=error {type(e).__name__}: {e}")
        return EXIT_ERROR
    report["db_candidates"] = candidates
    for ln in lines:
        print(ln)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1),
            encoding="utf-8")
    print(f"[{TOOL}] mode={args.mode} rc={rc} 判定只读=mode-ro 连接层"
          f" 真库零写入={'是（非 --commit）' if args.mode != 'commit' else '否（--commit）'}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
