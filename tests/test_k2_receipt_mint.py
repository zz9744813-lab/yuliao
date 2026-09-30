"""K2 收据铸造驱动器测试 —— 全合成上游，**零真实模型调用、零真库写入**。

覆盖任务书的四条硬要求：

1. 两席（两条 route）× N 个 fixture 快照各投**恰好一次** ⇒
   `semantic_review_calls` / `semantic_review_votes` 实测计数 > 0，且逐
   (snapshot, model) 恰一票、每模型恰好一次 call（准入侧
   `app/semantic_approval._current_two_pass_approval` 的原判据）；
2. 重复投 ⇒ `k2_model_already_voted`（幂等/防重），计数不增；
3. 反向自检：网关少发一个证明头 ⇒ `review_snapshot` 抛 `ReviewResponseError`
   （原文 message 钉死），且 call/vote 计数**不增**；网关拒发（上游 model 与核准
   路由不符 ⇒ 502 + 零证明头）同样不增；
4. 真库写闸：`--database` 缺 `--i-know-this-is-live` 即拒（rc=2），且拒时**不碰**
   目标文件。

链路是真的：私有落盘 sqlite（真过 `_require_private_storage` 真闸，不
monkeypatch）+ 仓库既有建表路径（`Base.metadata.create_all` /
`ensure_promotion_audit_schema` / `ensure_semantic_schema` /
`freeze_snapshot`）+ 每席一个真进程内 `tools/attestation_gateway` + 消费侧
`app.semantic_review_runner.review_snapshot` 一次。合成上游用标准库 HTTP 服务，
其判定正文**从冻结输入里回读实例 ID** 再构造合法四键 JSON（不是糊一个 `{}`）。
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app import config
import app.semantic_review_runner as runner
from scripts import k2_receipt_mint as mint
import tools.attestation_gateway as gw

SEAT_MODELS = ("synthetic-seat-a", "synthetic-seat-b")
PROVIDER = "synthetic"
CHANNEL_IDS = ("chan-synthetic-a", "chan-synthetic-b")


# ── 合成上游：标准库 OpenAI 兼容 /chat/completions，零真实模型 ────────────────
def _review_body(request: dict, verdict: str, cited: list[str]) -> dict:
    """按 `app.semantic_review_runner` 的判据构造**合法**判定正文。

    键必须且只能是 verdict / reason / cited_instance_ids / concerns
    （`_parse_response` 逐条校验），cited 必须落在冻结输入的实例 ID 集合内，
    BLOCK 必须带 concerns。
    """
    review = {
        "verdict": verdict,
        "reason": f"合成审查员：逐条核过 {len(cited)} 条冻结实例与来源登记",
        "cited_instance_ids": cited,
        "concerns": ["合成票：反例不足以支撑该范围"] if verdict == "BLOCK" else [],
    }
    return {"id": f"chatcmpl-synth-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "model": request["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": json.dumps(review,
                                                            ensure_ascii=False)}}]}


class _SyntheticUpstreamHandler(http.server.BaseHTTPRequestHandler):
    """回读请求里的冻结 `review_input`，把实例 ID 抄进 cited（真审查员的样子）。"""

    protocol_version = "HTTP/1.1"
    verdict = "PASS"
    echo_model = True  # False ⇒ 故意让体 model 与核准路由不符（触发网关拒发）
    requests: list = []

    def do_POST(self):  # noqa: N802 - stdlib 约定名
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw.decode("utf-8"))
        except ValueError:
            request = {}
        cls = type(self)
        cls.requests.append(request)
        cited = []
        for message in request.get("messages", []):
            try:
                frozen = json.loads(message["content"])
            except (KeyError, TypeError, ValueError):
                continue
            cited = [i["instance_id"] for i in frozen.get("instances", [])
                     if isinstance(i, dict)]
        body = _review_body(request, cls.verdict, cited or ["SI-K2FIXA"])
        if not cls.echo_model:
            body["model"] = body["model"] + "-unregistered"
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw_body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw_body)

    def log_message(self, *args):  # 静音访问日志
        return


def _start(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def _stop(server):
    server.shutdown()
    server.server_close()


# ── fixture 库（私有落盘 sqlite，走真 ACL 闸） ────────────────────────────────
@pytest.fixture(scope="module")
def template():
    """**整轮只建一次**模板库（create_all + 播种 + freeze），后续每测试拷一份。

    本机实测：`new_fixture_database` 的建表+冻结在满载机器上是 1.5s → 25s 的
    抖动项，而逐测试重建要 14 次；改成「建一次 + 拷 N 份（每份一次文件拷贝）」
    把建库成本从 14 次压到 1 次，只留消费侧每派发一次的 ACL 探测作为真实成本。
    拷贝出来的是**独立库**：votes/calls 从 0 开始、派发栅栏目录全新，测试之间
    不共享任何写状态。
    """
    root = mint.fixture_root().resolve()
    directory = root / f"template-{os.getpid()}"
    database, snapshot_ids = mint.new_fixture_database(directory, snapshot_count=2)
    engine = mint.open_engine(database)
    try:
        with engine.connect() as conn:  # 落盘并清空 WAL sidecar，副本才是单文件
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        engine.dispose()
    yield {"directory": directory, "database": database,
           "snapshot_ids": list(snapshot_ids)}
    if directory.resolve().parent == root and directory.is_dir():
        shutil.rmtree(directory)


@pytest.fixture()
def fixture_store(template):
    """每个测试一份独立拷贝（私有目录、落盘 sqlite、快照 id 与模板一致）。"""
    root = mint.fixture_root().resolve()
    directory = root / f"pytest-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "fixture.db"
    shutil.copyfile(template["database"], database)
    # 预热消费侧的 ACL 探测：`_require_private_storage` 每次派发起一个
    # `powershell.exe` 且**上限 15s**（app/semantic_review_runner.py）。本机
    # 同时跑着几十个 python 进程，冷启动可能撞到上限，把本该是
    # `ReviewResponseError`（证明头不齐）的读数控成 `k2_storage_acl_unverifiable`
    # ——那是环境问题，不是被测件的判据。先原地探一次把这条路径探热。
    runner._require_private_storage(Path(database))
    engine = mint.open_engine(database)
    try:
        yield {"database": database, "engine": engine, "directory": directory,
               "snapshot_ids": list(template["snapshot_ids"])}
    finally:
        engine.dispose()
        resolved = directory.resolve()
        if resolved.parent == root and resolved.is_dir():
            shutil.rmtree(resolved)


def _seat_env(upstream_url: str, directory: Path, **overrides) -> dict:
    env = {}
    for index, (model, channel) in enumerate(zip(SEAT_MODELS, CHANNEL_IDS), start=1):
        prefix = "LG_ATTEST_" if index == 1 else f"LG_ATTEST_{index}_"
        env.update({
            prefix + "UPSTREAM_BASE_URL": upstream_url,
            prefix + "UPSTREAM_API_KEY": "sk-synthetic-not-a-real-key",
            prefix + "ROUTE_PROVIDER": PROVIDER,
            prefix + "ROUTE_MODEL": model,
            prefix + "ROUTE_CHANNEL_ID": channel,
            prefix + "AUDIT_PATH": str(directory / f"attest-seat{index}.jsonl"),
        })
    env.update(overrides)
    return env


def _mint_with_synthetic_upstream(store, handler_cls, *, drop_headers=(),
                                 seat_count=2, snapshot_ids=None, **budget):
    """起「合成上游 + 每席一个真网关」，跑一次真 `mint`。返回 summary。

    `snapshot_ids=None` ⇒ 用 fixture 全部快照；反向自检只投 1 条即可坐实"不生成
    收据"，少投一条就少一次派发前的 PowerShell ACL 探测（消费侧
    `_require_private_storage` 每派发一次、上限 15s），显著压低整轮墙钟与偶发面。
    """
    upstream, upstream_url = _start(handler_cls)
    env = _seat_env(upstream_url, store["directory"])
    routes = mint.seat_routes_from_env(env, seat_count=seat_count)
    attached = []
    try:
        for seat in routes:
            gateway = gw.AttestationGateway(seat.gateway_route(),
                                            gw.make_httpx_forwarder(
                                                seat.gateway_route()),
                                            gw.AuditLog(seat.audit_path))
            if drop_headers:  # 反向自检：故意让网关少发证明头（不改 tools/ 源码）
                inner = gateway.handle

                def handle(request_body, _inner=inner, _drop=drop_headers):
                    result = _inner(request_body)
                    for name in _drop:
                        result.headers.pop(name, None)
                    return result

                gateway.handle = handle
            server = gw.make_server(gateway, "127.0.0.1", 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            attached.append(mint.AttachedSeat(
                name=seat.name, route=seat.route,
                base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                api_key="synthetic-gateway-key", audit_path=seat.audit_path,
                server=server))
        params = {"max_output_tokens": 1024, "max_request_bytes": 400_000,
                  "timeout_seconds": 30}
        params.update(budget)
        summary = mint.mint(store["engine"],
                            store["snapshot_ids"] if snapshot_ids is None
                            else list(snapshot_ids),
                            attached, **params)
        return summary
    finally:
        mint.stop_gateways(attached)
        _stop(upstream)


def _counts(store) -> dict:
    with store["engine"].connect() as conn:
        return {table: conn.exec_driver_sql(
            f"SELECT COUNT(*) FROM {table}").scalar_one()
            for table in ("semantic_review_snapshots", "semantic_review_calls",
                          "semantic_review_votes", "semantic_approval_links")}


def _receipt_rows(store) -> list:
    with store["engine"].connect() as conn:
        return [dict(row) for row in conn.execute(text(
            "SELECT v.snapshot_id AS snapshot_id, v.model_id AS model_id, "
            "v.provider AS provider, v.verdict AS verdict, v.vote_id AS vote_id, "
            "v.judge_kind AS judge_kind, v.call_receipt_id AS call_id, "
            "c.upstream_request_id AS upstream_request_id, c.channel AS channel "
            "FROM semantic_review_votes v JOIN semantic_review_calls c "
            "ON c.call_id = v.call_receipt_id ORDER BY v.vote_id")
        ).mappings()]


# ── 1. 两席 × N 快照：每席每快照恰好一票，计数 > 0 ──────────────────────────
def test_two_seats_mint_exactly_one_receipt_each_and_counts_above_zero(fixture_store):
    assert _counts(fixture_store) == {"semantic_review_snapshots": 2,
                                      "semantic_review_calls": 0,
                                      "semantic_review_votes": 0,
                                      "semantic_approval_links": 0}
    handler = type("Up", (_SyntheticUpstreamHandler,), {"requests": []})
    summary = _mint_with_synthetic_upstream(fixture_store, handler)

    assert summary["failed"] == 0, summary["results"]
    assert summary["ok"] == 4  # 2 快照 × 2 席
    assert [row["vote"] for row in summary["results"]] == ["PASS"] * 4
    assert all(row["error_type"] is None and row["error_message"] is None
               for row in summary["results"])
    assert {row["seat"] for row in summary["results"]} == {"seat-1", "seat-2"}

    counts = _counts(fixture_store)
    assert counts["semantic_review_calls"] == 4 > 0
    assert counts["semantic_review_votes"] == 4 > 0
    # 本件只造 call + vote，绝不造准入放行。
    assert counts["semantic_approval_links"] == 0

    rows = _receipt_rows(fixture_store)
    assert len(rows) == 4
    for snapshot_id in fixture_store["snapshot_ids"]:
        per_snapshot = [r for r in rows if r["snapshot_id"] == snapshot_id]
        assert len(per_snapshot) == 2, per_snapshot
        # 准入侧原判据：同 snapshot 两票 model_id 互异、每票都挂一条 call 收据。
        assert len({r["model_id"] for r in per_snapshot}) == 2
        for row in per_snapshot:
            assert row["model_id"] in SEAT_MODELS
            assert row["judge_kind"] == "semantic_current"
            assert row["channel"] == "openai_http"
            assert row["upstream_request_id"].startswith("chatcmpl-synth-")
    # 上游 request-id 全局唯一（UNIQUE(provider, upstream_request_id) 约束的实况）。
    assert len({r["upstream_request_id"] for r in rows}) == 4
    assert summary["readout"]["counts"]["semantic_review_calls"] == 4
    assert summary["readout"]["counts"]["semantic_review_votes"] == 4
    # 消费侧配置只在 mint 期间被改，事后复原（不污染同进程其它调用方）。
    assert config.LLM_MODE == "mock"

    # 上游侧读数（复用同一次真派发：ACL 探测每派发一次，不另起一轮）——
    # 打出去的确实是**冻结输入**，且请求形状符合消费侧契约。
    assert len(handler.requests) == 4
    for request in handler.requests:
        assert request["model"] in SEAT_MODELS
        assert request["temperature"] == 0 and request["stream"] is False
        assert [m["role"] for m in request["messages"]] == ["system", "user"]
        assert request["messages"][0]["content"] == runner.SYSTEM_PROMPT
        frozen = json.loads(request["messages"][1]["content"])
        assert {i["instance_id"] for i in frozen["instances"]} == {
            "SI-K2FIXA", "SI-K2FIXM"}
        assert frozen["scope_claim"]["scope_to"] == "WORK"
    assert runner.PROMPT_VERSION in runner.PROMPT_REGISTRY


@pytest.mark.parametrize("verdict", ["BLOCK", "ABSTAIN"])
def test_non_pass_verdicts_are_recorded_verbatim_not_as_success(fixture_store,
                                                                 verdict):
    """判据原样记账：BLOCK/ABSTAIN 也是**成功**的派发，但票面不得被改写成 PASS。"""
    handler = type("Up", (_SyntheticUpstreamHandler,),
                   {"verdict": verdict, "requests": []})
    summary = _mint_with_synthetic_upstream(
        fixture_store, handler, seat_count=1,
        snapshot_ids=fixture_store["snapshot_ids"][:1])
    assert summary["failed"] == 0
    assert len(summary["results"]) == 1  # 1 快照 × 1 席
    assert {row["vote"] for row in summary["results"]} == {verdict}
    assert _counts(fixture_store)["semantic_review_votes"] == 1


# ── 2. 重复投 ⇒ k2_model_already_voted（幂等/防重） ──────────────────────────
def test_repeat_mint_is_refused_and_counts_do_not_grow(fixture_store):
    one = fixture_store["snapshot_ids"][:1]
    first = _mint_with_synthetic_upstream(
        fixture_store, type("Up", (_SyntheticUpstreamHandler,), {"requests": []}),
        snapshot_ids=one)
    assert first["ok"] == 2  # 1 快照 × 2 席
    before = _counts(fixture_store)

    handler = type("Up", (_SyntheticUpstreamHandler,), {"requests": []})
    again = _mint_with_synthetic_upstream(fixture_store, handler, seat_count=1,
                                         snapshot_ids=one)

    assert again["ok"] == 0 and again["failed"] == 1
    for row in again["results"]:
        assert row["status"] == "failed"
        assert row["error_type"] == "ReviewPreflightError"
        assert row["error_message"] == "k2_model_already_voted"
        assert row["call_id"] is None and row["vote_id"] is None
    assert _counts(fixture_store) == before
    assert handler.requests == []  # 防重发生在派发**之前**：一次都没打上游
    assert again["readout"]["counts"]["semantic_review_votes"] == 2


# ── 3. 反向自检：证明头不齐 ⇒ ReviewResponseError，计数不增 ──────────────────
@pytest.mark.parametrize("dropped", [
    "x-lg-upstream-channel-id", "x-lg-upstream-request-id",
    "x-lg-upstream-provider", "x-lg-upstream-model",
])
def test_missing_attestation_header_blocks_receipt(fixture_store, dropped):
    before = _counts(fixture_store)
    summary = _mint_with_synthetic_upstream(
        fixture_store, type("Up", (_SyntheticUpstreamHandler,), {"requests": []}),
        drop_headers=(dropped,),
        seat_count=1, snapshot_ids=fixture_store["snapshot_ids"][:1])

    assert summary["ok"] == 0 and summary["failed"] == 1
    for row in summary["results"]:
        assert row["error_type"] == "ReviewResponseError"
        assert row["error_message"] == "k2_response_unverifiable"
        assert row["call_id"] is None and row["vote_id"] is None
    assert _counts(fixture_store) == before
    assert summary["readout"]["counts"]["semantic_review_calls"] == 0
    assert summary["readout"]["counts"]["semantic_review_votes"] == 0


def test_gateway_denial_without_headers_blocks_receipt(fixture_store):
    """网关拒发路径（上游 model 与核准路由不符 ⇒ 502 + 零证明头）。"""
    handler = type("Up", (_SyntheticUpstreamHandler,),
                   {"echo_model": False, "requests": []})
    before = _counts(fixture_store)
    summary = _mint_with_synthetic_upstream(
        fixture_store, handler, seat_count=1,
        snapshot_ids=fixture_store["snapshot_ids"][:1])

    assert summary["ok"] == 0 and summary["failed"] == 1
    assert {row["error_type"] for row in summary["results"]} == {
        "ReviewResponseError"}
    assert {row["error_message"] for row in summary["results"]} == {
        "k2_gateway_http_502"}
    assert _counts(fixture_store) == before
    assert handler.requests != []  # 上游确实被打过（拒发发生在网关侧）


def test_unreachable_gateway_is_recorded_as_outcome_unknown(fixture_store):
    """派发不出结果 ⇒ ReviewOutcomeUnknown 原文记账，计数不增、绝不重投。"""
    route = runner.ReviewRoute("synthetic-seat-a", PROVIDER, "synthetic-seat-a",
                               CHANNEL_IDS[0])
    dead = mint.AttachedSeat(name="seat-dead", route=route,
                             base_url="http://127.0.0.1:9/v1",
                             api_key="synthetic-gateway-key",
                             audit_path=str(fixture_store["directory"] / "dead.jsonl"))
    before = _counts(fixture_store)
    summary = mint.mint(fixture_store["engine"], fixture_store["snapshot_ids"][:1],
                        [dead], max_output_tokens=1024, max_request_bytes=400_000,
                        timeout_seconds=5)
    assert summary["ok"] == 0 and summary["failed"] == 1
    row = summary["results"][0]
    assert row["error_type"] == "ReviewOutcomeUnknown"
    assert row["error_message"] == "k2_gateway_outcome_unknown"
    assert _counts(fixture_store) == before


# ── 4. 硬边界：真库写闸 / 两席必须异模型 / 缺环境变量即拒 ────────────────────
def test_live_database_write_requires_explicit_ack(tmp_path):
    live = tmp_path / "language_genome.db"
    with pytest.raises(mint.MintPreflightError,
                       match="live_write_requires_explicit_ack"):
        mint.resolve_target(str(live), i_know_this_is_live=False)
    # CLI 层同样拒，且拒时**不建库、不建表、不落任何文件**。
    assert mint.main(["--database", str(live)]) == 2
    assert list(tmp_path.iterdir()) == []
    # 双开关也不许凭空造库：目标必须已存在。
    with pytest.raises(mint.MintPreflightError, match="live_database_missing"):
        mint.resolve_target(str(live), i_know_this_is_live=True)
    assert mint.main(["--database", str(live), mint.LIVE_ACK]) == 2
    assert list(tmp_path.iterdir()) == []
    # 席位数为 0 也拒（不投零席冒充"跑过了"）。
    assert mint.main(["--snapshots", "0"]) == 2


def test_seat_routes_require_distinct_upstream_models_and_full_env(fixture_store):
    directory = fixture_store["directory"]
    with pytest.raises(mint.MintPreflightError, match="k2_seat_env_missing"):
        mint.seat_routes_from_env({})
    same = _seat_env("http://127.0.0.1:1/v1", directory,
                     LG_ATTEST_2_ROUTE_MODEL=SEAT_MODELS[0])
    with pytest.raises(mint.MintPreflightError,
                       match="k2_seat_upstream_model_not_distinct"):
        mint.seat_routes_from_env(same)
    # 缺上游键 ⇒ 拒（不回落到任何默认路由）。
    short = _seat_env("http://127.0.0.1:1/v1", directory)
    del short["LG_ATTEST_2_UPSTREAM_API_KEY"]
    with pytest.raises(mint.MintPreflightError,
                       match="LG_ATTEST_2_UPSTREAM_API_KEY"):
        mint.seat_routes_from_env(short)
    # 判据校验也归 ReviewRoute：requested_model 不合法即拒。
    bad = _seat_env("http://127.0.0.1:1/v1", directory,
                    LG_ATTEST_2_REQUESTED_MODEL="agy/deepseek-v4.1-flash")
    with pytest.raises(mint.MintPreflightError,
                       match="k2_seat_route_invalid:seat2"):
        mint.seat_routes_from_env(bad)


def test_fixture_root_avoids_world_writable_temp():
    """fixture 根不能落在 Temp（Windows 上真闸实测拒 k2_storage_acl_untrusted）。"""
    root = mint.fixture_root()
    assert "Temp" not in root.parts
    assert root.name == mint.FIXTURE_DIR_NAME
    assert mint.fixture_root({"LG_K2_FIXTURE_ROOT": str(Path.cwd())}) == \
        Path.cwd().resolve()


def test_mint_requires_snapshots_and_seats(fixture_store):
    seats = [mint.AttachedSeat(name="seat-1",
                               route=runner.ReviewRoute("m", "p", "m", "c"),
                               base_url="http://127.0.0.1:9/v1", api_key="k",
                               audit_path="x")]
    with pytest.raises(mint.MintPreflightError,
                       match="k2_mint_requires_snapshots_and_seats"):
        mint.mint(fixture_store["engine"], [], seats)
    with pytest.raises(mint.MintPreflightError,
                       match="k2_mint_requires_snapshots_and_seats"):
        mint.mint(fixture_store["engine"], fixture_store["snapshot_ids"], [])
