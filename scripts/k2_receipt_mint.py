"""K2 语义收据铸造驱动器（2026-09-30 派工 lg-k2-receipt-mint）。

## 为什么需要这件工具

`app/semantic_review_runner.review_snapshot()` 是全仓**唯一**能写
`semantic_review_calls` / `semantic_review_votes` 的入口，而它**没有 CLI 驱动器**
（docstring 原话："No CLI or default route silently turns this into a bulk
run."）。于是 K2 线 8 条策略的现场读数停在：

- `semantic_review_snapshots` = 16 行（= 8 策略 × 2 席的待审快照，输入已冻结）；
- `semantic_review_calls` = 0、`semantic_review_votes` = 0 ⇒ 一次真实审查都没跑过。

`scripts/k5_promotion_write.py` 与 `evaluate` 要求"两席独立语义审查收据"，所以缺的
不是判据、不是快照，而是**把已冻结的快照真的投出去、逐条记账**的驱动器。本件就是
那件驱动器，且只做这一件事：调用 `review_snapshot` 一次、记一次账、失败照实记。

## 两席 = 两条 route（判据一字不改）

席位身份**就是** `app.semantic_review_runner.ReviewRoute`（字段名/校验全用消费侧
现行的 `validate()`，本件不新增、不放宽任何一条判据）。两席必须是**不同
`upstream_model`**：消费侧按 `(snapshot_id, lower(model_id))` 投一票即锁
（重复投 → `k2_model_already_voted`），且准入侧
`app/semantic_approval._current_two_pass_approval` 要求同一 snapshot 恰好 2 票、
`model_id` 互异、每模型恰好 1 次 call、且两票都 PASS。

路由身份与上游一律由 `LG_ATTEST_*` 环境变量给出（与
`tools/attestation_gateway.route_from_env()` 同一组键，第 N 席带 `LG_ATTEST_<N>_`
前缀，第 1 席兼容无前缀写法）：

| 变量 | 含义 |
|---|---|
| `LG_ATTEST_UPSTREAM_BASE_URL` | 上游 OpenAI 兼容根（如 `http://127.0.0.1:4000/v1`） |
| `LG_ATTEST_UPSTREAM_API_KEY` | 上游凭据（只进网关，**绝不**落审计、绝不出头） |
| `LG_ATTEST_ROUTE_PROVIDER` | 核准路由的 provider（证明头 `x-lg-upstream-provider`） |
| `LG_ATTEST_ROUTE_MODEL` | 核准路由的 model（证明头 `x-lg-upstream-model`） |
| `LG_ATTEST_ROUTE_CHANNEL_ID` | 核准路由的 channel（证明头 `x-lg-upstream-channel-id`） |
| `LG_ATTEST_REQUESTED_MODEL` | 投给网关的 model（默认同上；须过 `canonical_model`） |
| `LG_ATTEST_AUDIT_PATH` | 网关 append-only 审计 JSONL 落点（必填） |
| `LG_K2_GATEWAY_API_KEY` | 消费侧 `config.GATEWAY_API_KEY`（缺省每次运行随机生成） |

## 证明头来自网关，不来自本件

`tools/attestation_gateway.py`（提交 b0a394b）是全仓唯一生成
`x-lg-upstream-provider/-model/-channel-id/-request-id` 四个证明头的模块。本件为
**每席各起一个**进程内网关（`make_server(port=0)` + daemon 线程），把
`config.GATEWAY_BASE_URL` 指到它、`config.GATEWAY_API_KEY` 指到随运行生成的令牌。
网关的上游由上表环境变量给出；上游 `model` 与核准路由不符 ⇒ 网关**拒发**（502、
零证明头、审计记 denied）⇒ 消费侧抛 `ReviewResponseError`，**不生成收据**。

## 硬边界（越界即判红）

- **默认目标是 fixture 库**（本件在私有目录里现造，`--snapshots` 条），不是真库；
- 写真库必须**同时**显式传 `--database <path>` 与 `--i-know-this-is-live`；缺一即
  `MintPreflightError("live_write_requires_explicit_ack")`。**本任务不使用该开关**；
- 每席每 snapshot **恰好一次**派发，不重试、不吞异常：失败逐条记
  `error_type` + `error_message` 原文（`ReviewOutcomeUnknown` 一律不重投）；
- 不改 `app/`、不改判据、不改 `tools/attestation_gateway.py`、不 push / 不合并。

## 边界外的诚实声明

本件**不**创造准入：`semantic_approval_links`（人审放行）仍须另外走
`app/semantic_approval`；本件只造 call + vote 两张收据。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app import config  # noqa: E402
from app import knowledge as K  # noqa: E402
from app import knowledge_extract as KE  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import (ExpressionStrategyV2, Segment, StrategyCondition,  # noqa: E402
                        StrategyInstance, Work, WorkSource)
from app.promotion_audits import ensure_promotion_audit_schema  # noqa: E402
from app.semantic_receipts import ensure_semantic_schema  # noqa: E402
from app.semantic_review_runner import ReviewRoute, review_snapshot  # noqa: E402
from app.semantic_review_store import freeze_snapshot  # noqa: E402
import tools.attestation_gateway as AG  # noqa: E402

LIVE_ACK = "--i-know-this-is-live"
FIXTURE_ROOT_ENV = "LG_K2_FIXTURE_ROOT"
FIXTURE_DIR_NAME = "lg-k2-receipt-mint"
FIXTURE_TEXT = "夜里起了风，他坐在桌前，又把信纸折了两折。"
FIXTURE_STRATEGY_ID = "ESV2-K2FIXTURE"
FIXTURE_CLAIM = {"scope_to": "WORK", "scope_ids": ["WK-K2FIXA"],
                 "scope_basis": "fixture: 两条已登记证据"}
DEFAULT_MAX_OUTPUT_TOKENS = 2048
DEFAULT_MAX_REQUEST_BYTES = 512 * 1024
DEFAULT_TIMEOUT_SECONDS = 180.0


class MintPreflightError(RuntimeError):
    """No request was dispatched."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


# ── 席位（判据字段全用消费侧现行定义） ───────────────────────────────────────
@dataclass(frozen=True)
class SeatRoute:
    """一席的完整身份：上游路由 + 核准证明身份。"""

    name: str
    route: ReviewRoute
    upstream_base_url: str
    upstream_api_key: str
    audit_path: str

    def gateway_route(self) -> AG.Route:
        return AG.Route(base_url=self.upstream_base_url,
                        api_key=self.upstream_api_key,
                        provider=self.route.upstream_provider,
                        model=self.route.upstream_model,
                        channel_id=self.route.upstream_channel_id)


@dataclass
class AttachedSeat:
    """已起网关的一席：消费侧只需知道网关地址与令牌。"""

    name: str
    route: ReviewRoute
    base_url: str
    api_key: str
    audit_path: str
    server: object = None  # AG.make_server 的句柄，仅供同进程收尾关闭


_SEAT_KEYS = ("UPSTREAM_BASE_URL", "UPSTREAM_API_KEY", "ROUTE_PROVIDER",
              "ROUTE_MODEL", "ROUTE_CHANNEL_ID", "AUDIT_PATH")


def _seat_var(index: int, key: str) -> str:
    """第 index 席（1 起）优先变量名：`LG_ATTEST_<index>_<key>`。"""
    return f"LG_ATTEST_{index}_{key}"


def _seat_value(env: dict, index: int, key: str) -> str:
    """第 index 席（1 起）读 `LG_ATTEST_<index>_<key>`；第 1 席兼容无前缀。"""
    names = [_seat_var(index, key)]
    if index == 1:
        names.append(f"LG_ATTEST_{key}")
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def seat_routes_from_env(environ: dict | None = None, *,
                         seat_count: int = 2) -> list[SeatRoute]:
    """由 `LG_ATTEST_*` 装配 N 席路由；缺项即拒（不猜、不回落默认值）。

    两席的 `upstream_model` 必须互异：消费侧按模型投一票即锁，准入侧也要求
    同 snapshot 两票不同 `model_id`——这里先按判据的同一理由提前拦下。
    """
    env = dict(os.environ if environ is None else environ)
    if type(seat_count) is not int or seat_count < 1:
        raise MintPreflightError("k2_seat_count_invalid")
    seats: list[SeatRoute] = []
    for index in range(1, seat_count + 1):
        missing = [_seat_var(index, suffix) for suffix in _SEAT_KEYS
                   if not _seat_value(env, index, suffix)]
        if missing:
            raise MintPreflightError(
                f"k2_seat_env_missing:seat{index}:" + ",".join(missing))
        provider = _seat_value(env, index, "ROUTE_PROVIDER")
        model = _seat_value(env, index, "ROUTE_MODEL")
        channel_id = _seat_value(env, index, "ROUTE_CHANNEL_ID")
        requested = _seat_value(env, index, "REQUESTED_MODEL") or model
        route = ReviewRoute(requested_model=requested,
                            upstream_provider=provider,
                            upstream_model=model,
                            upstream_channel_id=channel_id)
        try:
            route.validate()
        except Exception as exc:  # 判据原文照传，不改写
            raise MintPreflightError(
                f"k2_seat_route_invalid:seat{index}:{exc}") from exc
        seats.append(SeatRoute(
            name=f"seat-{index}", route=route,
            upstream_base_url=_seat_value(env, index, "UPSTREAM_BASE_URL"),
            upstream_api_key=_seat_value(env, index, "UPSTREAM_API_KEY"),
            audit_path=_seat_value(env, index, "AUDIT_PATH")))
    models = [seat.route.upstream_model.casefold() for seat in seats]
    if len(set(models)) != len(models):
        raise MintPreflightError("k2_seat_upstream_model_not_distinct")
    return seats


def start_gateways(seats: list[SeatRoute], *,
                   host: str = "127.0.0.1") -> list[AttachedSeat]:
    """每席起一个进程内证明网关（port=0 由内核分配），返回消费侧地址。"""
    attached: list[AttachedSeat] = []
    try:
        for seat in seats:
            gateway = AG.gateway_from_env(_seat_env_for(seat))
            server = AG.make_server(gateway, host, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            attached.append(AttachedSeat(
                name=seat.name, route=seat.route,
                base_url=f"http://{host}:{server.server_address[1]}/v1",
                api_key=secrets.token_hex(16),
                audit_path=seat.audit_path, server=server))
    except Exception:
        stop_gateways(attached)
        raise
    return attached


def _seat_env_for(seat: SeatRoute) -> dict:
    return {
        "LG_ATTEST_UPSTREAM_BASE_URL": seat.upstream_base_url,
        "LG_ATTEST_UPSTREAM_API_KEY": seat.upstream_api_key,
        "LG_ATTEST_ROUTE_PROVIDER": seat.route.upstream_provider,
        "LG_ATTEST_ROUTE_MODEL": seat.route.upstream_model,
        "LG_ATTEST_ROUTE_CHANNEL_ID": seat.route.upstream_channel_id,
        "LG_ATTEST_AUDIT_PATH": seat.audit_path,
    }


def stop_gateways(seats: list[AttachedSeat]) -> None:
    """关掉本进程起的网关（server 为 None 表示由调用方自管）。"""
    for seat in seats:
        if seat.server is None:
            continue
        seat.server.shutdown()
        seat.server.server_close()
        seat.server = None


# ── fixture 库（默认目标；真库要显式开关） ────────────────────────────────────
def fixture_root(environ: dict | None = None) -> Path:
    r"""私有目录根：`LG_K2_FIXTURE_ROOT` → `%LOCALAPPDATA%/lg-k2-receipt-mint`。

    **不能**用 `tempfile.gettempdir()`：消费侧 `_require_private_storage` 在 Windows
    上实测拒 `...\AppData\Local\Temp`（`k2_storage_acl_untrusted`），而 fixture 库
    恰恰要走真闸（不 monkeypatch），所以根目录必须落在实测可过的私有目录上。
    """
    env = os.environ if environ is None else environ
    override = (env.get(FIXTURE_ROOT_ENV) or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local = (env.get("LOCALAPPDATA") or "").strip()
    if local:
        return Path(local).expanduser() / FIXTURE_DIR_NAME
    return Path.home() / f".{FIXTURE_DIR_NAME}"


def new_fixture_database(directory: Path, *, snapshot_count: int = 2,
                         name: str = "fixture.db") -> tuple[Path, list[str]]:
    """在私有目录里现造一个**落盘** sqlite 并冻结 N 个待审快照。

    建表全走仓库既有路径（`app.db.Base.metadata.create_all` +
    `app.promotion_audits.ensure_promotion_audit_schema` +
    `app.semantic_receipts.ensure_semantic_schema`），快照由
    `app.semantic_review_store.freeze_snapshot` 冻结——与生产同一条写路径，
    所以 `review_snapshot` 的 `verify_current_snapshot` 重算必然通过。
    """
    if type(snapshot_count) is not int or snapshot_count < 1:
        raise MintPreflightError("k2_fixture_snapshot_count_invalid")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    database = (directory / name).resolve()
    _reset_fixture_artifacts(directory, database)
    engine = open_engine(database)
    try:
        Base.metadata.create_all(engine)
        ensure_promotion_audit_schema(engine)
        _seed_fixture_evidence(engine)
        ensure_semantic_schema(engine)
        snapshot_ids = [freeze_snapshot(engine, FIXTURE_STRATEGY_ID, 1,
                                        dict(FIXTURE_CLAIM))["snapshot_id"]
                        for _ in range(snapshot_count)]
    finally:
        engine.dispose()
    return database, snapshot_ids


def _reset_fixture_artifacts(directory: Path, database: Path) -> None:
    """清掉同名旧 fixture 的库文件、sidecar 与派发栅栏。

    投票与栅栏都是幂等锁（重复投 → `k2_model_already_voted` /
    `k2_seat_already_attempted`），留着旧库只会让重跑变成"全失败"。只按**确切
    文件名**删，不扫目录、不删目录本身。
    """
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(database) + suffix)
        if stale.exists():
            stale.unlink()
    attempts = directory / "semantic_review_attempts"
    if attempts.is_dir() and attempts.parent.resolve() == directory.resolve():
        shutil.rmtree(attempts)


def open_engine(database: Path) -> Engine:
    engine = create_engine(f"sqlite:///{Path(database).as_posix()}", future=True,
                           connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _seed_fixture_evidence(engine: Engine) -> None:
    """两条已登记人类源证据（1 根作品 + 1 镜像）+ 1 张 replicated 卡 + 1 条锚审计。

    全部满足 `build_snapshot` 的硬前置：`status=verified`、`src_ok` 严格 true、
    复审标记 `k2def-v1`、`text_version` 在允许集、span 与 `evidence_sha256` 自洽。
    """
    session: Session = sessionmaker(bind=engine, autoflush=False,
                                    expire_on_commit=False)()
    try:
        session.add(ExpressionStrategyV2(
            id=FIXTURE_STRATEGY_ID, strategy_key="K2 收据铸造 fixture 卡", version=1,
            abstract_operation="动作后留一拍", invariants=["不改变已发生的事实"],
            effect_hypothesis="在动作后延迟一拍，读者被迫重估前一句",
            failure_modes=["延迟变成拖沓", "读者误读为时间线跳跃"],
            source="k2_receipt_mint fixture", status="hypothesis",
            observation_status="replicated", effect_status="untested",
            scope="UNCERTAIN", scope_ids=[], scope_basis=""))
        session.add(StrategyCondition(
            id="SC-K2FIXTURE", strategy_id=FIXTURE_STRATEGY_ID, strategy_version=1,
            kind="good_when", dimension="节奏", operator="eq",
            value={"v": "慢"}, required=False, predicate_state="unknown",
            evidence_refs=[]))
        evidence = FIXTURE_TEXT[:5]
        for work_id, seg_id in (("WK-K2FIXA", "SEG-K2FIXA"),
                                ("WK-K2FIXM", "SEG-K2FIXM")):
            session.add(Work(id=work_id, title=work_id, source="file:k2-fixture"))
            session.flush()
            session.add(Segment(
                id=seg_id, work_id=work_id, ordinal=0, text=FIXTURE_TEXT,
                role="train", n_sentences=1, n_chars=len(FIXTURE_TEXT),
                integrity=json.dumps({"src_ok": True}, ensure_ascii=False)))
            session.add(WorkSource(
                id="WSRC-" + work_id, work_id=work_id,
                canonical_work_id="WK-K2FIXA", author_id="AUTH-K2FIX",
                genre_ids=["GEN-K2FIX"], source_type="human_fiction",
                text_version="corpus-v1",
                text_sha256=hashlib.sha256(
                    FIXTURE_TEXT.encode("utf-8")).hexdigest(),
                purpose_basis="fixture: 署名可核对", identity_purposes=["research"],
                license_purposes=[], license_basis=None,
                metadata_status="verified", metadata_basis="fixture: 合成登记行"))
        for ins_id, work_id, seg_id in (("SI-K2FIXA", "WK-K2FIXA", "SEG-K2FIXA"),
                                        ("SI-K2FIXM", "WK-K2FIXM", "SEG-K2FIXM")):
            session.add(StrategyInstance(
                id=ins_id, strategy_id=FIXTURE_STRATEGY_ID, strategy_version=1,
                work_id=work_id, segment_id=seg_id, frame_id=None,
                text_version="corpus-v1", span_start=0, span_end=5,
                evidence_text=evidence, evidence_sha256=K.evidence_sha256(evidence),
                conditions_observed={"节奏": "慢"}, observed_content="留白一拍",
                effect_ref=None, extractor_model="k2_receipt_mint fixture",
                reviewer_version=KE.REVIEW_MARKER_NEW_DEF, status="verified"))
        session.commit()
    finally:
        session.close()
    with engine.begin() as conn:
        # promotion_audits 全列 NOT NULL（app/promotion_audits.py::AUDIT_DDL），
        # 这里补齐一整条 replicated 锚：K2 快照的 replicated_audit 就取它。
        conn.execute(text(
            "INSERT INTO promotion_audits (audit_id, tool, gate_version, "
            "strategy_id, strategy_key, strategy_version, from_status, "
            "to_status, status_column_from, status_column_to, observation_from, "
            "observation_to, scope_from, scope_to, scope_ids, scope_basis, "
            "scope_rule_version, evidence_ref, evidence_count, reviewer, ts, "
            "policy_sha256, columns_written) VALUES "
            "('PAUD-K2FIXTURE', 'k2_receipt_mint_fixture', "
            "'k2_receipt_mint_fixture/v1', :sid, :key, 1, 'observed', "
            "'replicated', 'observed', 'replicated', 'observed', 'replicated', "
            "'UNCERTAIN', 'UNCERTAIN', '[]', 'fixture: scope 未定', "
            "'scope-derive-1', :refs, 2, 'k2_receipt_mint', :ts, "
            "'fixture-policy-sha256', '[]')"),
            {"sid": FIXTURE_STRATEGY_ID, "key": "K2 收据铸造 fixture 卡",
             "refs": json.dumps(["SI-K2FIXA", "SI-K2FIXM"]),
             "ts": "2026-09-30T00:00:00Z"})


# ── 铸造循环：每席每 snapshot 恰好一次 ───────────────────────────────────────
def mint(engine: Engine, snapshot_ids: list[str], seats: list[AttachedSeat], *,
          max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
          max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
          timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """逐 snapshot × 逐席调用 `review_snapshot`，逐条记账，失败照实记原文。

    不重试：`ReviewOutcomeUnknown`（可能已执行）重投会毁掉"每模型恰好一次"的
    准入判据，所以失败只记账、交给人判断。
    """
    if not snapshot_ids or not seats:
        raise MintPreflightError("k2_mint_requires_snapshots_and_seats")
    saved = (config.LLM_MODE, config.GATEWAY_BASE_URL, config.GATEWAY_API_KEY)
    results: list[dict] = []
    try:
        # 消费侧硬前置（app/semantic_review_runner.py:432-434）：LLM_MODE=real +
        # 非空 base_url/api_key。本件只在自己的进程内改，不写环境变量。
        config.LLM_MODE = "real"
        for snapshot_id in snapshot_ids:
            for seat in seats:
                config.GATEWAY_BASE_URL = seat.base_url
                config.GATEWAY_API_KEY = seat.api_key
                results.append(_one(engine, snapshot_id, seat,
                                    max_output_tokens, max_request_bytes,
                                    timeout_seconds))
    finally:
        (config.LLM_MODE, config.GATEWAY_BASE_URL,
         config.GATEWAY_API_KEY) = saved
    return {
        "database": str(Path(engine.url.database).as_posix()),
        "seats": [{"seat": seat.name, "requested_model": seat.route.requested_model,
                   "provider": seat.route.upstream_provider,
                   "upstream_model": seat.route.upstream_model,
                   "channel_id": seat.route.upstream_channel_id,
                   "gateway_base_url": seat.base_url} for seat in seats],
        "snapshots": list(snapshot_ids),
        "budget": {"max_output_tokens": max_output_tokens,
                   "max_request_bytes": max_request_bytes,
                   "timeout_seconds": timeout_seconds},
        "results": results,
        "ok": sum(1 for item in results if item["status"] == "ok"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "readout": live_readout(engine),
    }


def _one(engine: Engine, snapshot_id: str, seat: AttachedSeat,
         max_output_tokens: int, max_request_bytes: int,
         timeout_seconds: float) -> dict:
    row = {"snapshot_id": snapshot_id, "seat": seat.name,
           "model_identity": (seat.route.upstream_provider + "/" +
                              seat.route.upstream_model),
           "status": "failed", "call_id": None, "vote_id": None, "vote": None,
           "error_type": None, "error_message": None}
    try:
        result = review_snapshot(
            engine, snapshot_id, seat.route, max_output_tokens=max_output_tokens,
            max_request_bytes=max_request_bytes, timeout_seconds=timeout_seconds)
    except Exception as exc:  # 逐条记异常类型 + 原文 message，绝不吞、绝不写成成功
        row["error_type"] = type(exc).__name__
        row["error_message"] = str(exc)
        return row
    row.update(status="ok", call_id=result["call_id"],
               vote_id=result["vote_id"], vote=result["verdict"],
               attempt_id=result["attempt_id"])
    return row


def live_readout(engine: Engine) -> dict:
    """现场读数：收据计数 + 逐 (snapshot, 模型) 票数，供人眼对账。"""
    with engine.connect() as conn:
        counts = {table: conn.exec_driver_sql(
            f"SELECT COUNT(*) FROM {table}").scalar_one()
            for table in ("semantic_review_snapshots", "semantic_review_calls",
                          "semantic_review_votes", "semantic_approval_links")}
        per_snapshot = [dict(row) for row in conn.execute(text(
            "SELECT v.snapshot_id AS snapshot_id, v.model_id AS model_id, "
            "v.provider AS provider, v.verdict AS verdict, v.vote_id AS vote_id, "
            "v.call_receipt_id AS call_id, "
            "(SELECT COUNT(*) FROM semantic_review_calls c "
            " WHERE c.snapshot_id=v.snapshot_id"
            " AND lower(c.model_id)=lower(v.model_id)) "
            "AS calls_for_model "
            "FROM semantic_review_votes v ORDER BY v.snapshot_id, v.vote_id")
        ).mappings()]
    return {"at": _now(), "counts": counts, "votes": per_snapshot}


# ── CLI ──────────────────────────────────────────────────────────────────────
def resolve_target(database: str, *, i_know_this_is_live: bool,
                   fixture_dir: str = "") -> tuple[Path, bool]:
    """返回 (目标路径, 是否现造 fixture)。真库必须**双开关**，否则当场拒。"""
    if database:
        if not i_know_this_is_live:
            raise MintPreflightError("live_write_requires_explicit_ack:" + LIVE_ACK)
        live = Path(database).expanduser().resolve()
        # 双开关也不许凭空造库：真跑只投已存在的库（45GB 的真库不可能是新建的）。
        if not live.is_file():
            raise MintPreflightError("live_database_missing:" + str(live))
        return live, False
    if fixture_dir:
        return Path(fixture_dir).expanduser(), True
    return fixture_root(), True


def _report(summary: dict, *, stream=None) -> None:
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=False),
          file=stream or sys.stdout, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="K2 语义收据铸造驱动器（默认 fixture 库；真库需双开关）")
    parser.add_argument("--database", default="",
                        help="目标库路径；缺省=现造 fixture 库。给了就必须同时给 "
                             + LIVE_ACK + "（真库写闸）")
    parser.add_argument(LIVE_ACK, action="store_true", dest="i_know_this_is_live",
                        help="确认目标库是**真库**，允许写入（默认 False）")
    parser.add_argument("--fixture-dir", default="", dest="fixture_dir",
                        help="fixture 库目录（缺省 " + FIXTURE_ROOT_ENV +
                             " / %LOCALAPPDATA%/" + FIXTURE_DIR_NAME + "）")
    parser.add_argument("--snapshots", type=int, default=2, dest="snapshots",
                        help="fixture 库冻结几条待审快照（默认 2）")
    parser.add_argument("--seats", type=int, default=2, dest="seats",
                        help="几席 = 几条 route（默认 2；真跑缺任一席即投不满两票）")
    parser.add_argument("--max-output-tokens", type=int,
                        default=DEFAULT_MAX_OUTPUT_TOKENS, dest="max_output_tokens")
    parser.add_argument("--max-request-bytes", type=int,
                        default=DEFAULT_MAX_REQUEST_BYTES, dest="max_request_bytes")
    parser.add_argument("--timeout-seconds", type=float,
                        default=DEFAULT_TIMEOUT_SECONDS, dest="timeout_seconds")
    args = parser.parse_args(argv)

    try:
        target, is_fixture = resolve_target(
            args.database, i_know_this_is_live=args.i_know_this_is_live,
            fixture_dir=args.fixture_dir)
    except MintPreflightError as exc:
        print(f"MINT_REFUSED: {exc}", file=sys.stderr, flush=True)
        return 2

    snapshot_ids: list[str] = []
    try:
        if is_fixture:
            # 默认路径：现造 fixture 库（**不**碰真库一行）。
            database, snapshot_ids = new_fixture_database(
                target, snapshot_count=args.snapshots)
        else:
            # 真库路径：不建表、不冻结、不写任何行，只读快照列表后逐席投一次。
            database = target
        engine = open_engine(database)
    except Exception as exc:
        _report({"status": "refused", "error_type": type(exc).__name__,
                 "error_message": str(exc), "target": str(target)})
        print(f"MINT_REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr,
              flush=True)
        return 2
    attached: list[AttachedSeat] = []
    try:
        if not snapshot_ids:
            with engine.connect() as conn:
                snapshot_ids = [row[0] for row in conn.execute(text(
                    "SELECT snapshot_id FROM semantic_review_snapshots "
                    "ORDER BY created_at, snapshot_id"))]
        routes = seat_routes_from_env(seat_count=args.seats)
        attached = start_gateways(routes)
        summary = mint(engine, snapshot_ids, attached,
                       max_output_tokens=args.max_output_tokens,
                       max_request_bytes=args.max_request_bytes,
                       timeout_seconds=args.timeout_seconds)
    except Exception as exc:
        _report({"status": "refused", "error_type": type(exc).__name__,
                 "error_message": str(exc), "database": str(database),
                 "snapshots": snapshot_ids})
        print(f"MINT_REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr,
              flush=True)
        return 2
    finally:
        stop_gateways(attached)
        engine.dispose()
    _report(summary)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
