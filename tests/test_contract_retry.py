"""Draft/Review 契约失败的同模型可恢复重试（任务书 2026-10-01 B）——零真实调用。

实测证据 B：invalid_model_contract:Draft 占真实臂失败最大一块（13/46）——
模型偶发「多包一层/少字段/截断」不按契约输出，而不是能力不足。口径：
· _parse_attempt 失败 ⇒ 同 role（同模型，**结构性禁止换模型重试**）在
  stage+'.retry' 再调 1 次，输入附上一次原文与校验错误原文；
· 重试走真 _call/真 parse/真 model_validate，计入 calls 表与 usage.calls
  （不白嫖预算）、不进改写轮；
· 重试仍不合契约 ⇒ 照旧 raise invalid_model_contract（契约一字不放宽）。
全部用注入式 httpx MockTransport 假网关（两次不同回复），解析与校验走真码。
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as _cfg                                  # noqa: E402
from app.scene_runtime.client import GatewayClient              # noqa: E402
from app.scene_runtime.contracts import (Budget, Change, Fact,  # noqa: E402
                                         KnowledgePackage,
                                         PlannedEvent, RuntimeFault,
                                         ScenePlan, World)
from app.scene_runtime.pipeline import SceneRunner              # noqa: E402
from app.scene_runtime.store import Store                       # noqa: E402

W = "kimi-k3"
V = "mc22-flash"
PROSE = "林穗把一枚钱放在桌上，又收了回去，灯花跳了一下。"


def good_draft():
    return json.dumps({"text": PROSE}, ensure_ascii=False)


def fenced(text):
    return "```json\n" + text + "\n```"


def good_review(payload):
    plan, text = payload["plan"], payload["text"]
    return json.dumps({"issues": [],
                       "changes": [{"fact": c["fact"], "after": c["after"],
                                    "quote": text}
                                   for ev in plan["events"] for c in ev["changes"]],
                       "events": [{"event_id": ev["event_id"], "quote": text}
                                  for ev in plan["events"]]},
                      ensure_ascii=False)


@pytest.fixture
def live_cfg(monkeypatch):
    monkeypatch.setattr(_cfg, "LLM_MODE", "real")
    monkeypatch.setattr(_cfg, "GATEWAY_BASE_URL", "http://gateway.invalid/v1")
    monkeypatch.setattr(_cfg, "GATEWAY_API_KEY", "test-key-only")
    monkeypatch.delenv("LG_ALLOW_MODEL_SUBSTITUTION", raising=False)


class Gateway:
    """脚本化假网关：队列项=str 即回复原文；dict 可带 content/finish。
    队列打空后 writer 恒回合规草稿、verifier 恒按负载回合规评审。"""

    def __init__(self, writer_replies=(), verifier_replies=()):
        self.wq, self.vq = list(writer_replies), list(verifier_replies)
        self.requests = []
        self.transport = httpx.MockTransport(self._handler)

    def _handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        role = "writer" if body["model"] == W else "verifier"
        q = self.wq if role == "writer" else self.vq
        if q:
            item = q.pop(0)
            if isinstance(item, dict):
                content, finish = item.get("content", ""), item.get("finish", "stop")
            else:
                content, finish = item, "stop"
        elif role == "writer":
            content, finish = good_draft(), "stop"
        else:
            content = good_review(json.loads(body["messages"][1]["content"]))
            finish = "stop"
        data = {"id": "chatcmpl-fake", "model": W if role == "writer" else V,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": content},
                             "finish_reason": finish}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
        return httpx.Response(200, json=data)


@pytest.fixture
def scene(tmp_path):
    store = Store(tmp_path / "runtime.sqlite")
    store.create_world(World(book_id="book-a", revision=0,
                             characters={"lin": "林穗", "shen": "沈砚"},
                             facts={"coins": Fact(value=3, visible_to=["lin"]),
                                    "received": Fact(value=0,
                                                     visible_to=["lin", "shen"])},
                             rules=["No magic"]))
    plan = ScenePlan(book_id="book-a", scene_id="s1", idempotency_key="s1-v1",
                     expected_revision=0, pov="lin", goal="支付一枚钱",
                     style="简洁", min_chars=1, max_chars=500,
                     events=[PlannedEvent(event_id="pay", description="支付一枚钱",
                                          changes=[Change(fact="coins", before=3, after=2),
                                                   Change(fact="received", before=0, after=1)])])
    knowledge = KnowledgePackage(package_id="empty-1", book_id="book-a",
                                 source_kind="empty", techniques=[])
    return store, plan, knowledge


def _run(store, plan, knowledge, gateway, budget=None):
    client = GatewayClient(W, V, transport=gateway.transport)
    return SceneRunner(store, client).run(plan, knowledge, budget or Budget())


def _stages(store):
    with store.connection() as db:
        return [r[0] for r in db.execute("SELECT stage FROM calls ORDER BY rowid")]


BAD_NESTED = json.dumps({"draft": {"text": PROSE}}, ensure_ascii=False)
BAD_MISSING = "{}"
BAD_TRUNCATED = '{"text": "林穗把一枚'
BAD_DOUBLE_FENCE = "```json\n" + fenced(good_draft()) + "\n```"
BAD_NO_FENCE_PROSE = "这是正文：" + good_draft()


@pytest.mark.parametrize("bad", [
    pytest.param(BAD_NESTED, id="多包一层"),
    pytest.param(BAD_MISSING, id="缺字段"),
    pytest.param(BAD_TRUNCATED, id="截断"),
    pytest.param(BAD_DOUBLE_FENCE, id="双层围栏"),
    pytest.param(BAD_NO_FENCE_PROSE, id="围栏外套话"),
])
def test_contract_broken_reply_repaired_by_same_model_retry(scene, live_cfg, bad):
    """正向（真 _parse_contract + 真 model_validate）：多包一层/缺字段/
    截断/双围栏/带解说的回复第一次烧掉、同模型重试给对 ⇒ 整链成功。
    重试以 writer.0.retry 落账、计入 usage.calls 与 contract_retries。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[bad])
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    usage = store.usage(receipt["job_id"])
    assert [a["stage"] for a in usage["attempts"]] == [
        "writer.0", "writer.0.retry", "verifier.0"]
    assert usage["calls"] == 3, "重试那次必须出现在 usage.calls（不白嫖预算）"
    assert usage["contract_retries"] == 1
    assert usage["verifier_invalid_retries"] == 0


def test_plain_fenced_json_passes_without_retry(scene, live_cfg):
    """既有「剥一层围栏」语义不变：合规围栏 JSON 一次过，不触发重试。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[fenced(good_draft())])
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    usage = store.usage(receipt["job_id"])
    assert usage["calls"] == 2 and usage["contract_retries"] == 0
    assert _stages(store) == ["writer.0", "verifier.0"]


def test_retry_still_invalid_still_raises_contract(scene, live_cfg):
    """反向①：重试后仍不合契约 ⇒ 照旧 raise invalid_model_contract——
    契约不放宽；且只重试 1 次（无 .retry.retry、无第二轮）。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[BAD_MISSING, BAD_TRUNCATED])
    with pytest.raises(RuntimeFault, match="invalid_model_contract:Draft"):
        _run(store, plan, knowledge, gateway)
    assert _stages(store) == ["writer.0", "writer.0.retry"]
    with store.connection() as db:
        job = db.execute("SELECT id FROM jobs").fetchone()[0]
    usage = store.usage(job)
    assert usage["calls"] == 2 and usage["contract_retries"] == 1


def test_retry_request_carries_previous_error_verbatim(scene, live_cfg):
    """重试请求必须附上一次回复原文与解析/校验错误原文（模型才有机会
    改对），错误码前缀与落账一致。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[BAD_MISSING])
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    retry_body = gateway.requests[1]
    retry_input = json.loads(retry_body["messages"][1]["content"])
    assert retry_input["previous_reply"] == BAD_MISSING
    assert retry_input["contract_error"].startswith("invalid_model_contract:Draft")
    assert retry_input["repair_instruction"]
    with store.connection() as db:
        frozen = json.loads(db.execute(
            "SELECT request FROM calls WHERE stage='writer.0.retry'").fetchone()[0])
    assert "contract_error" in frozen["input"]
    assert frozen["model"] == W, "重试的落账模型必须仍是请求模型"


def test_retry_consumes_call_budget(scene, live_cfg):
    """反向③：重试计入调用预算——max_calls=2 时 writer 主调+重试已占满，
    verifier 主调直接 call_budget_exhausted（旧世界这会放行：重试不存在
    或白嫖）。预算闸不因重试机制松一寸。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[BAD_MISSING])
    with pytest.raises(RuntimeFault, match="call_budget_exhausted"):
        _run(store, plan, knowledge, gateway, budget=Budget(max_calls=2))
    assert _stages(store) == ["writer.0", "writer.0.retry"]


def test_retry_never_switches_model(scene, live_cfg):
    """反向④：换模型重试被结构性禁止——role 不变 ⇒ 模型名不变；
    全场请求过的模型集合恰好 = {writer, verifier} 两个。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[BAD_TRUNCATED],
                      verifier_replies=[BAD_MISSING])
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    assert set(g["model"] for g in gateway.requests) == {W, V}
    for g, stage in zip(gateway.requests, _stages(store)):
        want = W if stage.startswith("writer") else V
        assert g["model"] == want, f"{stage} 换了模型：{g['model']} != {want}"


def test_verifier_contract_retry_distinct_from_invalid_retry(scene, live_cfg):
    """verifier 判定 JSON 不合契约 ⇒ verifier.0.retry 同模型重试；与
    gateway 无效重试口径分开记（contract_retries vs verifier_invalid_retries）。"""
    store, plan, knowledge = scene
    gateway = Gateway(verifier_replies=['{"issues": []}'])
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    assert _stages(store) == ["writer.0", "verifier.0", "verifier.0.retry"]
    usage = store.usage(receipt["job_id"])
    assert usage["contract_retries"] == 1
    assert usage["verifier_invalid_retries"] == 0, "契约重试不得冒充无效重试口径"


def test_gateway_invalid_retry_keeps_old_ledger(scene, live_cfg):
    """既有 verifier 无效重试链路回归：空回复 ⇒ gateway_invalid 落 failed，
    stage+'.retry' 成功；该次不算契约重试（两口径互不污染）。"""
    store, plan, knowledge = scene
    gateway = Gateway(verifier_replies=[{"content": ""}])
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    assert _stages(store) == ["writer.0", "verifier.0", "verifier.0.retry"]
    usage = store.usage(receipt["job_id"])
    assert usage["calls"] == 3
    assert usage["verifier_invalid_retries"] == 1
    assert usage["contract_retries"] == 0
