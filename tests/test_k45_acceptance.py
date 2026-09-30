"""K4/K5 验收签认链测试 —— **零真实模型调用、零真库读写**。

链路是真的：每席一个真 `tools/attestation_gateway`（由
`scripts.k2_receipt_mint.seat_routes_from_env` / `start_gateways` 装配，本测试不改
它们），上游是标准库合成的 OpenAI 兼容 `/chat/completions`。产物 JSON 也是合成的
（`k4_paired.json` 结构：`artifacts.prose[]` 含 scene/arm/text/status），全部落在
pytest `tmp_path` 之下，`F:\\agi\\language-genome\\data` 与
`D:\\language-genome-data` 一个字节都不碰。

正例 1 条（跑通全链并核回）+ 负例 8 条（任务书 ①–⑧ 逐条，另加「自填 PASS」与
派发前置拒），每条都断言**被拒且原因可读**。反向验证是硬要求：核验器必须能说
「不」，而不是只在注释里写。
"""
from __future__ import annotations

import hashlib
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
TEXT_C = "雨停在凌晨，瓦上还有水声。他推开半扇门，看见台阶上昨夜没看完的那封信。"


def receipt_document() -> dict:
    """合成 K4 产物：三臂 committed + 一臂 failed + 一臂 skipped（后者不进签认面）。"""
    return {
        "artifacts": {
            "prose": [
                {"scene": "s1", "arm": "A", "text": TEXT_A, "status": "committed"},
                {"scene": "s1", "arm": "B", "text": TEXT_B, "status": "committed"},
                {"scene": "s2", "arm": "A", "text": TEXT_C, "status": "committed"},
                {"scene": "s2", "arm": "B", "status": "failed"},
            ],
            "receipts": [{"scene": "s1", "arm": "A", "job_id": "scene-synthetic",
                          "usage": {"calls": 2, "tokens": 1063}}],
            "failures": [{"scene": "s2", "arm": "B", "error": "call_budget_exhausted"}],
            "skipped": [{"scene": "s3", "arm": "A", "reason": "前场失败"}],
        },
        "meta": {"live": True, "gate": "k4_paired_synthetic"},
    }


# ── 合成上游 ─────────────────────────────────────────────────────────────────
class _SyntheticUpstream(http.server.BaseHTTPRequestHandler):
    """按核准路由回显 model（网关才签发）；`echo_model=False` ⇒ 网关拒发。"""

    protocol_version = "HTTP/1.1"
    verdict = "ACCEPT"
    verdicts: dict | None = None      # 按 model 分派，用于两席异判词
    echo_model = True
    requests: list = []

    def do_POST(self):  # noqa: N802 - stdlib 约定名
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        request = json.loads(raw.decode("utf-8"))
        type(self).requests.append(request)
        verdict = (self.verdicts or {}).get(request.get("model"), self.verdict)
        review = {"verdict": verdict,
                  "reason": f"合成验收席按 rubric 逐句核过：{verdict}"}
        body = {"id": f"chatcmpl-k45-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "model": request["model"] if self.echo_model
                else request["model"] + "-unregistered",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": json.dumps(review,
                                                               ensure_ascii=False)}}]}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return


def _start_upstream(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


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


# ── 一次完整签认链（每测试独立目录，互不共享写状态） ──────────────────────────
def build_chain(tmp_path: Path, *, verdict: str = "ACCEPT", verdicts: dict | None = None,
                echo_model: bool = True, document: dict | None = None,
                name: str = "chain") -> dict:
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    receipt_path = directory / "k4_paired.json"
    document = receipt_document() if document is None else document
    receipt_path.write_text(json.dumps(document, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    handler = type("Up", (_SyntheticUpstream,),
                   {"verdict": verdict, "verdicts": verdicts,
                    "echo_model": echo_model, "requests": []})
    upstream, upstream_url = _start_upstream(handler)
    routes = k2mint.seat_routes_from_env(_seat_env(upstream_url, directory),
                                         seat_count=2)
    attached = k2mint.start_gateways(routes)
    error = None
    artifact = None
    try:
        artifact = k45.mint_acceptance(
            receipt_path, RUBRIC, attached, timeout_seconds=30,
            artifact_path=directory / "acceptance.json",
            call_receipt_path=directory / "k45_calls.jsonl")
    except Exception as exc:  # 派发即拒：交给用例断言异常原文
        error = exc
    finally:
        k2mint.stop_gateways(attached)
        upstream.shutdown()
        upstream.server_close()
    return {"directory": directory, "receipt_path": receipt_path,
            "artifact_path": directory / "acceptance.json",
            "ledger_path": directory / "k45_calls.jsonl",
            "audit_paths": [Path(seat.audit_path) for seat in attached],
            "artifact": artifact, "error": error, "requests": handler.requests}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def ledger_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                            for r in rows), encoding="utf-8")


def verify(chain: dict) -> tuple[bool, str]:
    return k45.verify_acceptance(chain["receipt_path"], chain["artifact_path"])


# ── 正例：真跑一遍链，核回 (True, ...)，并逐字段坐实签认面 ────────────────────
def test_mint_then_verify_passes_over_the_full_chain(tmp_path):
    chain = build_chain(tmp_path)
    assert chain["error"] is None, chain["error"]
    artifact = chain["artifact"]

    ok, why = verify(chain)
    assert ok, why
    assert artifact["decision"] == "ACCEPT"
    assert artifact["artifact_version"] == k45.ARTIFACT_VERSION
    assert artifact["rubric"] == RUBRIC
    assert artifact["receipt_sha256"] == hashlib.sha256(
        chain["receipt_path"].read_bytes()).hexdigest()
    # 三臂 committed 全覆盖；failed/skipped 臂不进签认面。
    assert {(a["scene"], a["arm"]) for a in artifact["arms"]} == {
        ("s1", "A"), ("s1", "B"), ("s2", "A")}
    assert all(len(a["votes"]) == 2 for a in artifact["arms"])
    for arm in artifact["arms"]:
        identities = {v["model_identity"] for v in arm["votes"]}
        assert len(identities) == 2, arm
        assert {v["verdict"] for v in arm["votes"]} == {"ACCEPT"}
        assert arm["prose_sha256"] == hashlib.sha256(
            {("s1", "A"): TEXT_A, ("s1", "B"): TEXT_B,
             ("s2", "A"): TEXT_C}[
                (arm["scene"], arm["arm"])].encode("utf-8")).hexdigest()

    rows = ledger_rows(chain["ledger_path"])
    assert len(rows) == 6  # 3 臂 × 2 席，逐条即时落盘
    assert len({r["upstream_request_id"] for r in rows}) == 6
    assert all(r["upstream_request_id"].startswith("chatcmpl-k45-") for r in rows)
    assert all(set(k45.LEDGER_FIELDS) <= set(r) for r in rows)
    # 网关侧签发数与收据行数一致：每票都有签发，没有"自称审过"的空洞。
    issued = sum(1 for path in chain["audit_paths"]
                 for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip() and json.loads(line)["decision"] == "issued")
    assert issued == 6
    # 打出去的评审输入形状 = `REVIEW_INPUT_KEYS` 契约，**且含正文全文**。
    assert len(chain["requests"]) == 6
    sent = json.loads(chain["requests"][0]["messages"][1]["content"])
    assert set(sent) == set(k45.REVIEW_INPUT_KEYS)
    assert sent["rubric"] == RUBRIC
    assert sent["receipt_sha256"] == artifact["receipt_sha256"]
    # 2026-09-30 修：输入里必须有正文全文，否则任何诚实的席都只能 ABSTAIN
    # （席看不到正文、也不许据自填内容签发）⇒ decision 永远到不了 ACCEPT ⇒ 门结构性不可翻。
    sent_by_key = {}
    for request in chain["requests"]:
        payload = json.loads(request["messages"][1]["content"])
        sent_by_key[(payload["scene"], payload["arm"])] = payload
    assert len(sent_by_key) == 3
    assert sent_by_key[("s1", "A")]["prose"] == TEXT_A
    assert sent_by_key[("s1", "B")]["prose"] == TEXT_B
    assert sent_by_key[("s2", "A")]["prose"] == TEXT_C
    # 正文进输入不等于信自填：哈希仍是逐字算出来的。
    assert sent_by_key[("s1", "A")]["prose_sha256"] == hashlib.sha256(
        TEXT_A.encode("utf-8")).hexdigest()


# ── 负例 ①：产物字节变了但 artifact 未更新（倒签） ────────────────────────────
def test_1_product_bytes_changed_with_stale_artifact_is_rejected(tmp_path):
    chain = build_chain(tmp_path)
    assert verify(chain)[0] is True
    document = read_json(chain["receipt_path"])
    document["meta"]["live"] = False          # 与正文无关的字节变更，同样算换产物
    write_json(chain["receipt_path"], document)

    ok, why = verify(chain)
    assert ok is False
    assert "k45_receipt_sha_mismatch" in why and "倒签" in why, why


# ── 负例 ②：少一臂 ───────────────────────────────────────────────────────────
def test_2_artifact_missing_a_committed_arm_is_rejected(tmp_path):
    chain = build_chain(tmp_path)
    artifact = read_json(chain["artifact_path"])
    artifact["arms"] = artifact["arms"][:2]   # 丢掉 (s2, A)
    write_json(chain["artifact_path"], artifact)

    ok, why = verify(chain)
    assert ok is False
    assert "k45_arms_missing" in why and "s2/A" in why, why


# ── 负例 ③：同模型两席 ───────────────────────────────────────────────────────
def test_3_two_seats_same_model_identity_is_rejected(tmp_path):
    chain = build_chain(tmp_path)
    artifact = read_json(chain["artifact_path"])
    ledger = ledger_rows(chain["ledger_path"])
    first = artifact["seats"][0]
    for arm in artifact["arms"]:
        for vote in arm["votes"]:
            if vote["seat"] == artifact["seats"][1]["seat"]:
                vote.update(provider=first["provider"], model_id=first["model_id"],
                            model_identity=first["model_identity"])
    artifact["seats"][1].update(provider=first["provider"],
                                model_id=first["model_id"],
                                model_identity=first["model_identity"])
    for row in ledger:
        if row["seat"] == artifact["seats"][1]["seat"]:
            row.update(provider=first["provider"], model_id=first["model_id"],
                       model_identity=first["model_identity"])
    write_json(chain["artifact_path"], artifact)
    write_ledger(chain["ledger_path"], ledger)

    ok, why = verify(chain)
    assert ok is False
    assert "k45_vote_model_identity_not_distinct" in why and "同模型" in why, why


# ── 负例 ④：调用收据 sha 与网关账本不符 ──────────────────────────────────────
def test_4_call_receipt_sha_not_matching_gateway_audit_is_rejected(tmp_path):
    chain = build_chain(tmp_path)
    artifact = read_json(chain["artifact_path"])
    ledger = ledger_rows(chain["ledger_path"])
    forged = hashlib.sha256(b"someone-elses-response").hexdigest()
    for row in ledger:
        if (row["scene"], row["arm"]) == ("s1", "A"):
            row["response_sha256"] = forged
    for arm in artifact["arms"]:
        if (arm["scene"], arm["arm"]) == ("s1", "A"):
            for vote in arm["votes"]:
                vote["response_sha256"] = forged
    write_json(chain["artifact_path"], artifact)
    write_ledger(chain["ledger_path"], ledger)

    ok, why = verify(chain)
    assert ok is False
    assert "k45_gateway_audit_no_issuance" in why, why
    assert "s1/A" in why and "response_sha256" in why, why

    # input_sha256 被改（评审输入五键重算不符）也照样拦下。
    chain2 = build_chain(tmp_path, name="chain2")
    artifact2 = read_json(chain2["artifact_path"])
    ledger2 = ledger_rows(chain2["ledger_path"])
    seat1 = artifact2["seats"][0]["seat"]
    forged_inputs = {}
    for row in ledger2:
        if row["seat"] == seat1:
            row["input_sha256"] = hashlib.sha256(
                row["input_sha256"].encode()).hexdigest()
            forged_inputs[(row["scene"], row["arm"])] = row["input_sha256"]
    for arm in artifact2["arms"]:
        for vote in arm["votes"]:
            if vote["seat"] == seat1:
                vote["input_sha256"] = forged_inputs[(arm["scene"], arm["arm"])]
    write_json(chain2["artifact_path"], artifact2)
    write_ledger(chain2["ledger_path"], ledger2)
    ok2, why2 = verify(chain2)
    assert ok2 is False
    assert "k45_input_sha_mismatch" in why2, why2


# ── 负例 ⑤：upstream_request_id 为空 ─────────────────────────────────────────
def test_5_empty_upstream_request_id_is_rejected(tmp_path):
    chain = build_chain(tmp_path)
    artifact = read_json(chain["artifact_path"])
    ledger = ledger_rows(chain["ledger_path"])
    for row in ledger:
        row["upstream_request_id"] = "  "
    for arm in artifact["arms"]:
        for vote in arm["votes"]:
            vote["upstream_request_id"] = "  "
    write_json(chain["artifact_path"], artifact)
    write_ledger(chain["ledger_path"], ledger)

    ok, why = verify(chain)
    assert ok is False
    assert "k45_upstream_request_id_empty" in why, why
    assert "没有上游请求号" in why, why


# ── 负例 ⑥：decision 自填 ACCEPT，但两席判词是 BLOCK / ABSTAIN ───────────────
@pytest.mark.parametrize("verdicts,expected", [
    ({"synthetic-seat-a": "BLOCK", "synthetic-seat-b": "BLOCK"}, "BLOCK"),
    ({"synthetic-seat-a": "ACCEPT", "synthetic-seat-b": "BLOCK"}, "BLOCK"),
    ({"synthetic-seat-a": "ACCEPT", "synthetic-seat-b": "ABSTAIN"}, "ABSTAIN"),
])
def test_6_self_filled_decision_over_non_accept_votes_is_rejected(tmp_path,
                                                                  verdicts,
                                                                  expected):
    chain = build_chain(tmp_path, verdicts=verdicts, name="votes")
    artifact = read_json(chain["artifact_path"])
    assert artifact["decision"] == expected          # 重算口径本身正确
    assert verify(chain)[0] is True                  # 诚实的 BLOCK/ABSTAIN 也过核验

    artifact["decision"] = "ACCEPT"                  # 自填翻绿
    write_json(chain["artifact_path"], artifact)
    ok, why = verify(chain)
    assert ok is False
    assert "k45_decision_mismatch" in why and "自填文本不构成通过" in why, why
    assert expected in why, why


# ── 负例 ⑦：append-only 收据文件里某席缺行 ───────────────────────────────────
def test_7_missing_seat_line_in_call_receipt_is_rejected(tmp_path):
    chain = build_chain(tmp_path)
    rows = ledger_rows(chain["ledger_path"])
    seat2 = chain["artifact"]["seats"][1]["seat"]
    write_ledger(chain["ledger_path"], [r for r in rows if r["seat"] != seat2])

    ok, why = verify(chain)
    assert ok is False
    assert "k45_call_receipt_row_missing" in why and seat2 in why, why

    # 整本收据文件缺失同样拒（不许"只有 artifact 自称通过"）。
    chain["ledger_path"].unlink()
    ok2, why2 = verify(chain)
    assert ok2 is False and "k45_call_receipt_missing" in why2, why2


# ── 负例 ⑧：某臂 prose 文本被改一个字符（连 receipt_sha256 一起重新倒签） ────
def test_8_single_character_prose_change_is_rejected(tmp_path):
    document = receipt_document()
    chain = build_chain(tmp_path, document=document)
    assert verify(chain)[0] is True

    document["artifacts"]["prose"][1]["text"] = TEXT_B[:-1] + "！"  # 改末一个字
    write_json(chain["receipt_path"], document)
    artifact = read_json(chain["artifact_path"])
    # 只把文件头哈希换掉（模拟"重新盖章但判词仍绑旧正文"）：正文哈希那一关必须拦住。
    artifact["receipt_sha256"] = hashlib.sha256(
        chain["receipt_path"].read_bytes()).hexdigest()
    write_json(chain["artifact_path"], artifact)

    ok, why = verify(chain)
    assert ok is False
    assert "k45_prose_sha_mismatch" in why and "s1/B" in why, why


# ── 额外反向验证：同目录任意 JSON 自称 PASS ⇒ 一律拒（本任务要补的那格） ──────
def test_hand_written_self_declared_pass_is_rejected(tmp_path):
    directory = tmp_path / "fake"
    directory.mkdir()
    receipt_path = directory / "k4_paired.json"
    write_json(receipt_path, receipt_document())
    receipt_sha = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    artifact_file = directory / "acceptance.json"
    TEXT_BY_KEY = {(p["scene"], p["arm"]): p.get("text", "")
                   for p in receipt_document()["artifacts"]["prose"]}

    # ① 一份自称「人工复核：同意通过」的 JSON：字段都齐不了。
    write_json(artifact_file, {"artifact_version": k45.ARTIFACT_VERSION,
                               "created_at": "2026-09-30T00:00:00Z",
                               "receipt_path": str(receipt_path.resolve()),
                               "receipt_sha256": receipt_sha,
                               "decision": "ACCEPT", "rubric": RUBRIC,
                               "note": "人工复核：同意通过"})
    ok, why = k45.verify_acceptance(receipt_path, artifact_file)
    assert ok is False and "k45_artifact_field_missing" in why, why

    # ② 补齐席位/臂字段，但臂面不全（当前产物有 3 条 committed 臂）。
    seats = [{"seat": f"seat-{i}", "provider": PROVIDER, "model_id": model,
              "model_identity": f"{PROVIDER}/{model}", "requested_model": model,
              "gateway_base_url": "http://127.0.0.1:9/v1",
              "audit_path": str(directory / "nope.jsonl")}
             for i, model in enumerate(SEAT_MODELS, 1)]
    partial = {"artifact_version": k45.ARTIFACT_VERSION,
               "created_at": "2026-09-30T00:00:00Z",
               "receipt_path": str(receipt_path.resolve()),
               "receipt_sha256": receipt_sha, "rubric": RUBRIC, "seats": seats,
               "call_receipt_path": str(directory / "nope.jsonl"),
               "decision": "ACCEPT",
               "arms": [{"scene": "s1", "arm": "A",
                         "prose_sha256": hashlib.sha256(
                             TEXT_A.encode("utf-8")).hexdigest(),
                         "votes": [{"seat": s["seat"],
                                    "model_identity": s["model_identity"],
                                    "verdict": "ACCEPT"} for s in seats]}]}
    write_json(artifact_file, partial)
    ok2, why2 = k45.verify_acceptance(receipt_path, artifact_file)
    assert ok2 is False and "k45_arms_missing" in why2 and "s1/B" in why2, why2

    # ③ 臂面抄全（正文哈希都照抄产物），但没有 append-only 调用收据：仍拒。
    partial["arms"] = [
        {"scene": scene, "arm": arm,
         "prose_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
         "votes": [{"seat": s["seat"], "model_identity": s["model_identity"],
                    "verdict": "ACCEPT"} for s in seats]}
        for (scene, arm, text) in (("s1", "A", TEXT_A), ("s1", "B", TEXT_B),
                                   ("s2", "A", TEXT_C))]
    write_json(artifact_file, partial)
    ok3, why3 = k45.verify_acceptance(receipt_path, artifact_file)
    assert ok3 is False and "k45_call_receipt_missing" in why3, why3

    # ④ 连调用收据都现抄一份（自造 JSONL）：该席根本没有网关账本文件 ⇒ 拒。
    ledger_file = directory / "k45_calls.jsonl"
    rows = []
    for arm in partial["arms"]:
        for seat in seats:
            rows.append({key: "" for key in k45.LEDGER_FIELDS} | {
                "scene": arm["scene"], "arm": arm["arm"],
                "prose_sha256": arm["prose_sha256"],
                "receipt_sha256": receipt_sha, "seat": seat["seat"],
                "provider": seat["provider"], "model_id": seat["model_id"],
                "model_identity": seat["model_identity"],
                "upstream_request_id": f"chatcmpl-self-claimed-{arm['scene']}{arm['arm']}",
                "verdict": "ACCEPT", "reason": "自填：我看过了"})
    write_ledger(ledger_file, rows)
    partial["call_receipt_path"] = str(ledger_file)
    write_json(artifact_file, partial)
    ok4, why4 = k45.verify_acceptance(receipt_path, artifact_file)
    assert ok4 is False and "k45_gateway_audit_missing" in why4, why4

    # ⑤ 现造一份「看起来签发过」的网关账本：input_sha256 抄错即被重算拦住。
    for seat in seats:
        (directory / f"audit-{seat['seat']}.jsonl").write_text(
            json.dumps({"seq": 1, "decision": "issued",
                        "route": {"provider": seat["provider"],
                                  "model": seat["model_id"], "channel_id": "x"},
                        "upstream_id": "chatcmpl-self-claimed",
                        "request_sha256": "0" * 64,
                        "response_sha256": "0" * 64}, ensure_ascii=False) + "\n",
            encoding="utf-8")
    for seat in partial["seats"]:
        seat["audit_path"] = str(directory / f"audit-{seat['seat']}.jsonl")
    write_json(artifact_file, partial)
    ok5, why5 = k45.verify_acceptance(receipt_path, artifact_file)
    assert ok5 is False and "k45_input_sha_mismatch" in why5, why5

    # ⑥ 连 input_sha256 都算对：只要该次调用没有真的网关签发行，仍然拒。
    for arm in partial["arms"]:
        for seat in seats:
            row = next(r for r in rows if (r["scene"], r["arm"], r["seat"]) ==
                       (arm["scene"], arm["arm"], seat["seat"]))
            row["input_sha256"] = hashlib.sha256(k45.request_bytes_for(
                k45.review_input_for({"scene": arm["scene"], "arm": arm["arm"],
                                      "prose_sha256": arm["prose_sha256"],
                                      "text": TEXT_BY_KEY[(arm["scene"],
                                                           arm["arm"])]},
                                     RUBRIC, receipt_sha),
                seat["requested_model"])).hexdigest()
            row["response_sha256"] = hashlib.sha256(b"fabricated").hexdigest()
    write_ledger(ledger_file, rows)
    ok6, why6 = k45.verify_acceptance(receipt_path, artifact_file)
    assert ok6 is False and "k45_gateway_audit_no_issuance" in why6, why6
    assert "网关账本里没有" in why6, why6


def test_swapping_the_product_file_is_rejected(tmp_path):
    """一份真链 artifact 拿去核**另一份**产物文件：显式 override 也翻不过字节哈希。"""
    chain = build_chain(tmp_path, name="real")
    other = tmp_path / "other"
    other.mkdir()
    swapped = other / "k4_paired.json"
    document = receipt_document()
    document["artifacts"]["prose"][0]["text"] = TEXT_A + "（换了产物）"
    write_json(swapped, document)

    ok, why = k45.verify_acceptance(swapped, chain["artifact_path"])
    assert ok is False and "k45_receipt_path_mismatch" in why, why

    ok2, why2 = k45.verify_acceptance(swapped, chain["artifact_path"],
                                      receipt_path_expected=swapped)
    assert ok2 is False and "k45_receipt_sha_mismatch" in why2, why2

    # override 只解"路径不同"这一关，绝不解字节哈希那一关。
    ok3, why3 = k45.verify_acceptance(chain["receipt_path"], chain["artifact_path"],
                                      receipt_path_expected=chain["receipt_path"])
    assert ok3 is True, why3
    ok4, why4 = k45.verify_acceptance(chain["receipt_path"], chain["artifact_path"],
                                      receipt_path_expected=swapped)
    assert ok4 is False and "k45_receipt_path_unexpected" in why4, why4


# ── 派发前置拒 + 网关拒发不留收据 ────────────────────────────────────────────
class _StubRoute:
    def __init__(self, entry):
        self.upstream_provider = entry["provider"]
        self.upstream_model = entry["model_id"]
        self.requested_model = entry["requested_model"]


class _StubSeat:
    """只用于触发 mint 的前置拒：这些调用一次上游都不该打。"""

    def __init__(self, entry):
        self.name = entry["seat"]
        self.route = _StubRoute(entry)
        self.base_url = entry["gateway_base_url"]
        self.api_key = "stub-key"
        self.audit_path = entry["audit_path"]


def test_mint_preflight_refusals(tmp_path):
    chain = build_chain(tmp_path, name="base")
    receipt_path = chain["receipt_path"]
    seats = chain["artifact"]["seats"]
    fake = [_StubSeat(s) for s in seats]

    with pytest.raises(k45.AcceptanceMintError, match="k45_rubric_missing"):
        k45.mint_acceptance(receipt_path, "  ", fake, timeout_seconds=5)
    with pytest.raises(k45.AcceptanceMintError, match="k45_timeout_seconds_invalid"):
        k45.mint_acceptance(receipt_path, RUBRIC, fake, timeout_seconds=0)
    with pytest.raises(k45.AcceptanceMintError, match="k45_seats_require_2"):
        k45.mint_acceptance(receipt_path, RUBRIC, fake[:1], timeout_seconds=5)
    same = [_StubSeat(s) for s in seats]
    same[1].route.upstream_model = same[0].route.upstream_model
    with pytest.raises(k45.AcceptanceMintError, match="k45_seats_model_not_distinct"):
        k45.mint_acceptance(receipt_path, RUBRIC, same, timeout_seconds=5)

    # 真库根一律不许写（默认只读面 + 环境变量覆盖）。
    with pytest.raises(k45.AcceptanceMintError, match="k45_output_path_protected"):
        k45.mint_acceptance(receipt_path, RUBRIC, fake, timeout_seconds=5,
                            artifact_path=Path("D:/language-genome-data/x.json"),
                            environ={})
    guarded = tmp_path / "guarded"
    with pytest.raises(k45.AcceptanceMintError, match="k45_output_path_protected"):
        k45.mint_acceptance(receipt_path, RUBRIC, fake, timeout_seconds=5,
                            artifact_path=guarded / "out.json",
                            environ={"LG_K45_PROTECTED_ROOTS": str(guarded)})
    assert not guarded.exists()

    # 产物里没有 committed 臂 ⇒ 没有可签认面，直接拒（不产空 artifact 冒充跑过）。
    empty = tmp_path / "empty.json"
    write_json(empty, {"artifacts": {"prose": [
        {"scene": "s1", "arm": "A", "status": "failed"}]}})
    with pytest.raises(k45.AcceptanceMintError, match="k45_no_committed_arms"):
        k45.mint_acceptance(empty, RUBRIC, fake, timeout_seconds=5)


def test_gateway_denial_produces_no_receipt_and_no_artifact(tmp_path):
    """上游 model 与核准路由不符 ⇒ 网关 502 + 零证明头 ⇒ 没有收据、没有 artifact。"""
    chain = build_chain(tmp_path, echo_model=False, name="denied")
    assert isinstance(chain["error"], k45.AcceptanceMintError)
    assert "k45_gateway_denied" in str(chain["error"])
    assert not chain["artifact_path"].exists()
    assert not chain["ledger_path"].exists()
    assert chain["requests"], "上游确实被打过（拒发发生在网关侧）"
    denied = [json.loads(line) for line in
              chain["audit_paths"][0].read_text(encoding="utf-8").splitlines()
              if line.strip()]
    assert denied and all(row["decision"] == "denied" for row in denied)
    ok, why = k45.verify_acceptance(chain["receipt_path"], chain["artifact_path"])
    assert ok is False and "k45_artifact" in why, why


def test_replay_of_one_issuance_across_votes_is_rejected(tmp_path):
    """两票复用同一次网关签发（重放）⇒ 拒。"""
    chain = build_chain(tmp_path, name="replay")
    artifact = read_json(chain["artifact_path"])
    ledger = ledger_rows(chain["ledger_path"])
    donor = next(r for r in ledger if (r["scene"], r["arm"]) == ("s1", "A")
                 and r["seat"] == artifact["seats"][0]["seat"])
    target_arm = next(a for a in artifact["arms"]
                      if (a["scene"], a["arm"]) == ("s1", "B"))
    for row in ledger:
        if (row["scene"], row["arm"], row["seat"]) == (
                "s1", "B", artifact["seats"][0]["seat"]):
            row.update({k: donor[k] for k in ("input_sha256", "response_sha256",
                                              "upstream_request_id")})
    for vote in target_arm["votes"]:
        if vote["seat"] == artifact["seats"][0]["seat"]:
            vote.update({k: donor[k] for k in ("input_sha256", "response_sha256",
                                               "upstream_request_id")})
    write_json(chain["artifact_path"], artifact)
    write_ledger(chain["ledger_path"], ledger)

    ok, why = verify(chain)
    assert ok is False
    assert ("k45_prose_sha_mismatch" in why or "k45_input_sha_mismatch" in why
            or "k45_gateway_audit_replay" in why), why
