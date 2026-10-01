"""同席同输入可恢复重试（lg-k45-seat-retry）—— **零真实模型调用、零真库读写**。

链路仍是真的：每席一个真 `tools/attestation_gateway`（由
`scripts.k2_receipt_mint.seat_routes_from_env` / `start_gateways` 装配，本测试不改它们），
上游是标准库合成的**可编排脚本** OpenAI 兼容服务；另有一个「假网关」用来复现
`http_4xx` 与 `k45_attestation_*_mismatch` 这两类**绝不重试**的拒。产物 JSON 与全部
落盘都在 pytest `tmp_path` 下，`F:\\agi\\language-genome\\data` 与
`D:\\language-genome-data` 一个字节都不碰。

重试只救「一次判词都没产生」的调用：HTTP 5xx / 超时 / 响应体解析不出判词对象。
反向自检是这里的主角：可读判词（含 BLOCK/ABSTAIN）一律不重试、4xx 与身份不符一律不
重试、重试用尽仍是 ABSTAIN（绝不变成 ACCEPT）、重试前后 payload 字节与 model 一字不变、
账本该 (臂, 席) 仍**只有一行**、`LG_K45_SEAT_RETRY_EXTRA=0` 时行为与今天逐字一致。
"""
from __future__ import annotations

import http.server
import json
import threading
import uuid
from pathlib import Path

import pytest

from scripts import k2_receipt_mint as k2mint
from scripts import k45_acceptance as k45

PROVIDER = "synthetic"
SEAT_MODELS = ("synthetic-seat-a", "synthetic-seat-b")
CHANNEL_IDS = ("chan-synthetic-a", "chan-synthetic-b")
RUBRIC = "双臂是否达成场景意图：叙事连贯、视角不越权、无模板腔；不达标即 BLOCK，证据不足即 ABSTAIN。"

TEXT_A = "林穗数了数手里的铜钱，三枚，够了。她把一枚搁在老人掌心，老人点头收钱，转身进了巷子。"
TEXT_B = "林穗把那枚铜钱递出去时，指尖感到铜面被磨得温滑。摊主接过，丢进木匣，匣里一声轻响。"

ARM_KEYS = [("s1", "A"), ("s1", "B")]


def receipt_document() -> dict:
    """两臂 committed 的合成 K4 产物 ⇒ 每轮 2 臂 × 2 席 = 4 次派发。"""
    return {"artifacts": {"prose": [
        {"scene": "s1", "arm": "A", "text": TEXT_A, "status": "committed"},
        {"scene": "s1", "arm": "B", "text": TEXT_B, "status": "committed"},
    ], "receipts": [], "failures": [], "skipped": []}, "meta": {"live": True}}


# ── 可编排合成上游 ───────────────────────────────────────────────────────────
class _ScriptedUpstream(http.server.BaseHTTPRequestHandler):
    """按 `model` 取脚本：该席**同一臂**第 n 次调用吃 `script[n-1]`（超出后重复最后一项）。

    action 形态：
      `{"status": 502}`                     ⇒ 上游回 5xx ⇒ 网关拒发（零证明头）⇒ http_502
      `{"verdict": "BLOCK", "reason": "…"}` ⇒ 正常可读判词
      `{"content": <任意>}`                 ⇒ 200 但 content 解析不出判词对象（今天那两类）
    """

    protocol_version = "HTTP/1.1"
    scripts: dict = {}
    requests: list = []
    counts: dict = {}
    per_pair: dict = {}

    def do_POST(self):  # noqa: N802 - stdlib 约定名
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        type(self).requests.append(raw)
        payload = json.loads(raw.decode("utf-8"))
        model = payload.get("model")
        review_input = json.loads(payload["messages"][1]["content"])
        pair = (model, review_input["scene"], review_input["arm"])
        type(self).counts[model] = type(self).counts.get(model, 0) + 1
        type(self).per_pair[pair] = type(self).per_pair.get(pair, 0) + 1
        script = self.scripts.get(model) or [{"verdict": "ACCEPT"}]
        action = script[min(type(self).per_pair[pair] - 1, len(script) - 1)]

        if "status" in action:
            self._reply(action["status"], json.dumps(
                {"error": {"type": "synthetic_upstream_down"}}).encode("utf-8"))
            return
        if "verdict" in action:
            content = json.dumps(
                {"verdict": action["verdict"],
                 "reason": action.get("reason",
                                      f"合成席按 rubric 逐句核过：{action['verdict']}")},
                ensure_ascii=False)
        else:
            content = action.get("content")
        body = {"id": f"chatcmpl-k45sr-{uuid.uuid4().hex}",
                "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}]}
        self._reply(200, json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def _reply(self, status: int, payload: bytes) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return


class _FakeGateway(http.server.BaseHTTPRequestHandler):
    """复现「网关自己回 4xx」「证明头缺失」「连上就断」「证明头 model 与核准路由不符」。

    前两类与 mismatch 一样是**该重试的传输故障**，4xx 与身份不符**不许**重试。
    """

    protocol_version = "HTTP/1.1"
    behavior = "http_404"
    requests: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        type(self).requests.append(raw)
        model = json.loads(raw.decode("utf-8"))["model"]
        if self.behavior == "drop":
            self.close_connection = True
            return                      # 一句响应都不发 ⇒ 客户端 RemoteProtocolError
        if self.behavior == "http_404":
            status, headers = 404, {}
            body = json.dumps({"error": {"type": "not_found"}}).encode("utf-8")
        elif self.behavior == "no_headers":  # 200 但零证明头 ⇒ 没有签发就没有收据
            status, headers = 200, {}
            body = json.dumps({"id": "chatcmpl-x", "model": model, "choices": []},
                              ensure_ascii=False).encode("utf-8")
        else:  # 200 + 四个证明头齐，但 model 不是核准路由那一档 ⇒ 身份不符
            status = 200
            headers = {k45.HEADER_PREFIX + "provider": PROVIDER,
                       k45.HEADER_PREFIX + "model": model + "-impersonated",
                       k45.HEADER_PREFIX + "channel-id": "chan-x",
                       k45.HEADER_PREFIX + "request-id": "chatcmpl-mismatch"}
            body = json.dumps({
                "id": "chatcmpl-mismatch", "model": model,
                "choices": [{"index": 0, "message": {
                    "role": "assistant",
                    "content": json.dumps({"verdict": "ACCEPT", "reason": "冒充"})}}]},
                ensure_ascii=False).encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def _start(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def _stop(server) -> None:
    server.shutdown()
    server.server_close()


def _seat_env(upstream_url: str, directory: Path) -> dict:
    env = {}
    for index, (model, channel) in enumerate(zip(SEAT_MODELS, CHANNEL_IDS), 1):
        prefix = "LG_ATTEST_" if index == 1 else f"LG_ATTEST_{index}_"
        env.update({
            prefix + "UPSTREAM_BASE_URL": upstream_url,
            prefix + "UPSTREAM_API_KEY": "sk-synthetic-not-a-real-key",
            prefix + "ROUTE_PROVIDER": PROVIDER,
            prefix + "ROUTE_MODEL": model,
            prefix + "ROUTE_CHANNEL_ID": channel,
            prefix + "AUDIT_PATH": str(directory / f"attest-seat{index}.jsonl"),
        })
    return env


def mint(tmp_path: Path, scripts: dict, *, name: str = "chain",
         environ: dict | None = None, fake_gateway: str | None = None,
         fake_seat: int = 0) -> dict:
    """跑一遍真链：返回 artifact/error、上游收到的原始字节与逐席调用计数、各路径。"""
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    receipt_path = directory / "k4_paired.json"
    receipt_path.write_text(json.dumps(receipt_document(), ensure_ascii=False, indent=2),
                            encoding="utf-8")

    handler = type("Up", (_ScriptedUpstream,),
                   {"scripts": scripts, "requests": [], "counts": {}, "per_pair": {}})
    upstream, upstream_url = _start(handler)
    routes = k2mint.seat_routes_from_env(_seat_env(upstream_url, directory), seat_count=2)
    attached = k2mint.start_gateways(routes)
    if fake_gateway is not None:
        attached[fake_seat].base_url = fake_gateway
    artifact, error = None, None
    try:
        artifact = k45.mint_acceptance(
            receipt_path, RUBRIC, attached, timeout_seconds=30,
            artifact_path=directory / "acceptance.json",
            call_receipt_path=directory / "k45_calls.jsonl",
            environ={} if environ is None else environ)
    except Exception as exc:  # 派发即拒：交给用例断言异常原文
        error = exc
    finally:
        k2mint.stop_gateways(attached)
        _stop(upstream)
    return {"directory": directory, "receipt_path": receipt_path,
            "artifact_path": directory / "acceptance.json",
            "ledger_path": directory / "k45_calls.jsonl",
            "audit_paths": [Path(seat.audit_path) for seat in attached],
            "seats": attached, "artifact": artifact, "error": error,
            "requests": handler.requests, "counts": handler.counts,
            "per_pair": handler.per_pair}


def ledger_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def row_for(chain: dict, scene: str, arm: str, seat: str) -> dict:
    """该 (臂, 席) 在 append-only 账本里**必须恰好一行**——多一行即核验器永久拒。"""
    rows = [r for r in ledger_rows(chain["ledger_path"])
            if (r["scene"], r["arm"], r["seat"]) == (scene, arm, seat)]
    assert len(rows) == 1, (scene, arm, seat, rows)
    return rows[0]


def seat_name(chain: dict, index: int) -> str:
    return chain["seats"][index].name


def verify(chain: dict):
    return k45.verify_acceptance(chain["receipt_path"], chain["artifact_path"])


# ── ①（反向）恒不可解析体：重试用尽后**仍**是 ABSTAIN，绝不翻成 ACCEPT ─────────
def test_1_unchangeably_unparsable_seat_stays_abstain_never_accept(tmp_path):
    fenced = "```json\n{\"verdict\": \"ACCEPT\"}\n```"  # 今天真机的 JSONDecodeError 形状
    chain = mint(tmp_path, {"synthetic-seat-a": [{"content": fenced}],
                            "synthetic-seat-b": [{"verdict": "ACCEPT"}]},
                 name="junk")
    assert chain["error"] is None, chain["error"]
    assert chain["counts"]["synthetic-seat-a"] == 4, "2 臂 × (1 + 默认额外 1 次) = 4"

    seat_a = seat_name(chain, 0)
    rows = ledger_rows(chain["ledger_path"])
    for scene, arm in ARM_KEYS:
        row = row_for(chain, scene, arm, seat_a)
        assert row["verdict"] == "ABSTAIN", row
        assert row["reason"].startswith("malformed_review_response:JSONDecodeError"), row
        assert row["attempts"] == 2, row
    # 判据没被放宽：没有任何一行是 ACCEPT 冒充；decision 由判词重算，仍是 ABSTAIN。
    assert all(r["verdict"] != "ACCEPT" for r in rows if r["seat"] == seat_a)
    assert chain["artifact"]["decision"] == k45.recompute_decision(
        [r["verdict"] for r in rows]) == "ABSTAIN"
    ok, why = verify(chain)
    assert ok is True, why


# ── ②（反向）BLOCK：只派发一次，判词逐字保留 ─────────────────────────────────
def test_2_readable_block_verdict_is_dispatched_exactly_once(tmp_path):
    reason = "视角越权：B 臂第二段直接写进老人内心，rubric 明令禁止。"
    chain = mint(tmp_path, {"synthetic-seat-a": [{"verdict": "BLOCK", "reason": reason}],
                            "synthetic-seat-b": [{"verdict": "ACCEPT"}]},
                 name="block")
    assert chain["error"] is None, chain["error"]
    assert chain["counts"]["synthetic-seat-a"] == 2, "可读判词（含 BLOCK）绝不重试"
    assert chain["counts"]["synthetic-seat-b"] == 2

    seat_a = seat_name(chain, 0)
    for scene, arm in ARM_KEYS:
        row = row_for(chain, scene, arm, seat_a)
        assert row["verdict"] == "BLOCK"
        assert row["reason"] == reason, "判词逐字保留，不改写、不摘要"
        assert "attempts" not in row, "没重试就不该多出这个键"
    assert chain["artifact"]["decision"] == "BLOCK"
    ok, why = verify(chain)
    assert ok is True, why


# ── ③（反向）重试前后 input_sha256 / payload 字节 / model 一字不变 ────────────
def test_3_retry_reuses_identical_payload_bytes_and_model(tmp_path):
    chain = mint(tmp_path, {"synthetic-seat-a": [{"status": 502}, {"verdict": "ACCEPT"}]},
                 name="same-input")
    assert chain["error"] is None, chain["error"]
    assert chain["counts"]["synthetic-seat-a"] == 4

    # 上游收到的**原始请求字节**：同一臂的两次尝试必须逐字节相同（同一 payload 对象重发）。
    by_input: dict = {}
    for raw in chain["requests"]:
        payload = json.loads(raw.decode("utf-8"))
        if payload["model"] != "synthetic-seat-a":
            continue
        by_input.setdefault(payload["messages"][1]["content"], set()).add(raw)
    assert len(by_input) == 2, f"两臂 ⇒ 两份不同输入：{list(by_input)}"
    for review_input, variants in by_input.items():
        assert len(variants) == 1, "重试不得改动 payload 一个字节"
        assert json.loads(review_input)["rubric"] == RUBRIC
    assert {json.loads(raw.decode("utf-8"))["model"] for raw in chain["requests"]} == \
        set(SEAT_MODELS), "重试不许换模型"

    # 网关侧同一个 request_sha256 既有 denied 又有 issued ⇒ 输入字节确实原样重发。
    audit = [json.loads(line) for line in
             chain["audit_paths"][0].read_text(encoding="utf-8").splitlines() if line.strip()]
    denied = {r["request_sha256"] for r in audit if r["decision"] == "denied"}
    issued = {r["request_sha256"] for r in audit if r["decision"] == "issued"}
    assert len(denied) == 2 and denied == issued
    seat_a = seat_name(chain, 0)
    for scene, arm in ARM_KEYS:
        row = row_for(chain, scene, arm, seat_a)
        assert row["input_sha256"] in denied, row
        assert row["model_id"] == "synthetic-seat-a", row
        assert row["attempts"] == 2


# ── ④（反向）发生重试时账本该 (臂, 席) 仍只有 1 行，且 verify 仍 True ──────────
def test_4_retry_still_writes_exactly_one_ledger_row_per_arm_seat(tmp_path):
    # 连续两次解析不出判词（TypeError / KeyError），第三次才给出判词 ⇒ 额外 2 次。
    chain = mint(tmp_path, {"synthetic-seat-a": [{"content": None}, {"content": {"x": 1}},
                                                 {"verdict": "ACCEPT"}]},
                 name="one-row",
                 environ={"LG_K45_SEAT_RETRY_EXTRA": "2"})
    assert chain["error"] is None, chain["error"]
    assert chain["counts"]["synthetic-seat-a"] == 6, "2 臂 × 3 次尝试"

    rows = ledger_rows(chain["ledger_path"])
    assert len(rows) == 4, "2 臂 × 2 席 ⇒ 恰好 4 行（重试绝不多写行）"
    keys = [(r["scene"], r["arm"], r["seat"]) for r in rows]
    assert len(set(keys)) == len(keys) == 4
    seat_a = seat_name(chain, 0)
    for scene, arm in ARM_KEYS:
        row = row_for(chain, scene, arm, seat_a)
        assert row["verdict"] == "ACCEPT" and row["attempts"] == 3, row
    # 前两次失败尝试的响应体一次都没进账本（只有最后一次落行）⇒ 核验器不判重放/多行。
    ok, why = verify(chain)
    assert ok is True, why
    assert "k45_call_receipt_row_duplicated" not in why

    artifact = json.loads(chain["artifact_path"].read_text(encoding="utf-8"))
    for arm in artifact["arms"]:
        assert len(arm["votes"]) == 2
        assert len({v["model_identity"] for v in arm["votes"]}) == 2, "两席仍异模型"
        for vote in arm["votes"]:
            if vote["seat"] == seat_a:
                assert vote["attempts"] == 3


# ── ⑤（反向）LG_K45_SEAT_RETRY_EXTRA=0 ⇒ 与今天逐字一致（同结果、同异常类型） ──
def test_5_retry_disabled_reproduces_todays_behaviour_verbatim(tmp_path):
    off = {"LG_K45_SEAT_RETRY_EXTRA": "0"}

    # 今天：502 直接抛错、整批中止、不留 artifact。
    down = mint(tmp_path, {"synthetic-seat-a": [{"status": 502}, {"verdict": "ACCEPT"}]},
                name="off-502", environ=off)
    assert isinstance(down["error"], k45.AcceptanceMintError), type(down["error"])
    assert str(down["error"]) == "k45_gateway_denied:seat-1:http_502", str(down["error"])
    assert down["artifact"] is None and not down["artifact_path"].exists()
    assert not down["ledger_path"].exists(), "没拿到签发就不许落收据"
    assert down["counts"]["synthetic-seat-a"] == 1, "关闭重试后一次都不许多打"

    # 今天：不可解析体写成 ABSTAIN 行、每席只调一次、没有 attempts 键，decision=ABSTAIN。
    junk = mint(tmp_path, {"synthetic-seat-a": [{"content": "非 JSON 的胡话"}]},
                name="off-junk", environ=off)
    assert junk["error"] is None, junk["error"]
    assert junk["counts"]["synthetic-seat-a"] == 2
    rows = ledger_rows(junk["ledger_path"])
    assert len(rows) == 4
    assert all("attempts" not in r for r in rows), "关闭重试 ⇒ 行形状与今天一致"
    seat_a = seat_name(junk, 0)
    for row in rows:
        if row["seat"] == seat_a:
            assert row["verdict"] == "ABSTAIN"
            assert row["reason"].startswith("malformed_review_response:"), row["reason"]
    assert junk["artifact"]["decision"] == "ABSTAIN"
    ok, why = verify(junk)
    assert ok is True, why

    # 默认值确实写死为 1：不给任何 env（environ={}）也会额外重试一次。
    assert k45.DEFAULT_SEAT_RETRY_EXTRA == 1
    default = mint(tmp_path, {"synthetic-seat-a": [{"status": 502}, {"verdict": "ACCEPT"}]},
                   name="default-on", environ={})
    assert default["error"] is None, default["error"]
    assert default["counts"]["synthetic-seat-a"] == 4


# ── ⑥（正向）第一次 502、第二次正常判词 ⇒ 整批成功 + 1 行 + decision 重算 ─────
def test_6_first_502_second_verdict_completes_the_whole_batch(tmp_path):
    chain = mint(tmp_path, {"synthetic-seat-a": [{"status": 502}, {"verdict": "ACCEPT"}],
                            "synthetic-seat-b": [{"verdict": "ACCEPT"}]},
                 name="recovered")
    assert chain["error"] is None, chain["error"]
    rows = ledger_rows(chain["ledger_path"])
    assert len(rows) == 4
    seat_a = seat_name(chain, 0)
    for scene, arm in ARM_KEYS:
        row = row_for(chain, scene, arm, seat_a)
        assert row["verdict"] == "ACCEPT" and row["attempts"] == 2
        assert row["upstream_request_id"].startswith("chatcmpl-k45sr-")
    assert chain["artifact"]["decision"] == k45.recompute_decision(
        [r["verdict"] for r in rows]) == "ACCEPT"
    assert chain["artifact"]["n_arms"] == 2
    ok, why = verify(chain)
    assert ok is True, why


# ── ⑦（反向补充）4xx 与身份不符：真问题 ⇒ 一次都不许多打 ─────────────────────
def test_7_http_4xx_and_identity_mismatch_are_never_retried(tmp_path):
    four = type("G4", (_FakeGateway,), {"behavior": "http_404", "requests": []})
    four_server, four_url = _start(four)
    try:
        chain = mint(tmp_path, {"synthetic-seat-a": [{"verdict": "ACCEPT"}]},
                     name="http404", fake_gateway=four_url)
        assert isinstance(chain["error"], k45.AcceptanceMintError), chain["error"]
        assert str(chain["error"]) == "k45_gateway_denied:seat-1:http_404", str(chain["error"])
        assert len(four.requests) == 1, "配置/鉴权类 4xx 重试无意义 ⇒ 恰好一次"
        assert not chain["artifact_path"].exists() and not chain["ledger_path"].exists()
    finally:
        _stop(four_server)

    mism = type("GM", (_FakeGateway,), {"behavior": "model_mismatch", "requests": []})
    mism_server, mism_url = _start(mism)
    try:
        chain2 = mint(tmp_path, {"synthetic-seat-a": [{"verdict": "ACCEPT"}]},
                      name="mismatch", fake_gateway=mism_url)
        assert isinstance(chain2["error"], k45.AcceptanceMintError), chain2["error"]
        assert str(chain2["error"]) == "k45_attestation_model_mismatch:seat-1", \
            str(chain2["error"])
        assert len(mism.requests) == 1, "身份不符是真问题 ⇒ 绝不重试"
    finally:
        _stop(mism_server)


# ── ⑧（反向补充）env 值不合法：前置拒，一次上游都不打 ────────────────────────
@pytest.mark.parametrize("raw,code", [("abc", "k45_seat_retry_extra_invalid"),
                                      ("-1", "k45_seat_retry_extra_negative")])
def test_8_invalid_retry_env_is_a_preflight_refusal(tmp_path, raw, code):
    chain = mint(tmp_path, {"synthetic-seat-a": [{"status": 502}]},
                 name=f"bad-env-{raw}", environ={"LG_K45_SEAT_RETRY_EXTRA": raw})
    assert isinstance(chain["error"], k45.AcceptanceMintError), chain["error"]
    assert code in str(chain["error"]), str(chain["error"])
    assert chain["counts"] == {}, "前置拒不许打任何一次上游"
    assert not chain["artifact_path"].exists() and not chain["ledger_path"].exists()


# ── ⑨（回归）已铸现网产物：新增字段是可选的，旧 artifact 不得因本改动变红 ─────
LIVE_MINTS = [
    (r"F:\agi\language-genome\out_k4_3_live_20261001\k4_paired.json",
     r"F:\agi\language-genome\out_k4_3_live_20261001\k4_paired.k45_acceptance.json"),
    (r"F:\Hermes\team\k4_out\retry5_deepseek-v4.1-flash\k4_paired.json",
     r"F:\Hermes\team\k4_out\retry5_deepseek-v4.1-flash\k4_paired.k45_acceptance.json"),
]


@pytest.mark.parametrize("receipt,artifact", LIVE_MINTS,
                         ids=["out_k4_3_live_20261001", "team_k4_out_retry5"])
def test_9_existing_live_artifacts_still_verify_true(receipt, artifact):
    receipt_path, artifact_path = Path(receipt), Path(artifact)
    if not (receipt_path.is_file() and artifact_path.is_file()):
        pytest.skip(f"现网产物不在此机：{artifact_path}")
    ledger = artifact_path.with_name(artifact_path.stem.replace(
        ".k45_acceptance", "") + ".k45_calls.jsonl")
    rows = ledger_rows(ledger)
    assert all("attempts" not in r for r in rows), "旧行本就没有该键"
    ok, why = k45.verify_acceptance(receipt_path, artifact_path)
    assert ok is True, why


def test_10_previously_red_live_product_fails_for_the_same_reason():
    """`F:\\Hermes\\team\\k4_out\\k4_v2_WK-6e5d2623` 那件在**本改动之前**就是红的：
    整批重跑把行追加进同一本 append-only 收据 ⇒ `k45_call_receipt_row_duplicated`
    （2026-10-01 基线实测）。本件只要求：它红的原因一个字没变、且与本改动无关。"""
    directory = Path(r"F:\Hermes\team\k4_out\k4_v2_WK-6e5d2623")
    receipt_path = directory / "k4_paired.json"
    artifact_path = directory / "k4_paired.k45_acceptance.json"
    if not (receipt_path.is_file() and artifact_path.is_file()):
        pytest.skip("现网产物不在此机")
    rows = ledger_rows(directory / "k4_paired.k45_calls.jsonl")
    assert all("attempts" not in r for r in rows)
    ok, why = k45.verify_acceptance(receipt_path, artifact_path)
    if ok:
        return                                    # 将来重铸干净了也算通过
    assert "k45_call_receipt_row_duplicated" in why, why
    assert "attempts" not in why, why


# ── ⑪（正向补充）连接断/零证明头这两类传输故障同样重试；用尽后照旧 fail-closed ─
@pytest.mark.parametrize("behavior,expect", [
    ("drop", "k45_dispatch_failed:seat-1:"),
    ("no_headers", "k45_attestation_headers_missing:seat-1:"),
])
def test_11_transport_layer_failures_are_retried_then_still_fail_closed(tmp_path,
                                                                        behavior,
                                                                        expect):
    for extra, expected_calls in (("1", 2), ("0", 1)):
        stub = type("G", (_FakeGateway,), {"behavior": behavior, "requests": []})
        server, url = _start(stub)
        try:
            chain = mint(tmp_path, {"synthetic-seat-a": [{"verdict": "ACCEPT"}]},
                         name=f"transport-{behavior}-{extra}",
                         environ={"LG_K45_SEAT_RETRY_EXTRA": extra}, fake_gateway=url)
            assert isinstance(chain["error"], k45.AcceptanceMintError), chain["error"]
            assert str(chain["error"]).startswith(expect), str(chain["error"])
            assert len(stub.requests) == expected_calls, (behavior, extra)
            assert not chain["artifact_path"].exists(), "救不回来照样不产 artifact"
            assert not chain["ledger_path"].exists()
        finally:
            _stop(server)
