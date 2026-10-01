"""写手侧「网关结果无效」同角色可恢复重试（任务书 2026-10-01 C）——零真实调用。

实测证据 C：`gateway_invalid_or_partial_result` 占 K5 真实臂失败 4/46
（近轮 r32/r35/r38 各 1 条）。该类在**校验席**早有同口径重试
（`_call_verified` ⇒ stage+'.retry'），在**写手**席此前当场炸臂
（`run()` 直调 `_call`，零重试）⇒ 一条早场空响应吃掉该臂本场景剩余场。
口径：
· `client.py` 的无效判据（空 content / finish_reason != 'stop' / 响应解析
  异常）一个字未改，本文件用的是**真** `GatewayClient.invoke`；
· 写手上收到该原因 ⇒ 同 role（同模型，**结构性禁止换模型**）在
  stage+'.retry' 再调 1 次，输入逐字不变；
· 重试走真 `_call`/真 `reserve_call` ⇒ 计入 calls 表与 usage.calls
  （不白嫖预算）、不进改写轮；
· 只重试这一类：model_identity_mismatch / invalid_model_contract /
  其它 RuntimeFault 一律原样抛；重试仍无效 ⇒ 照旧抛同一失败原因名。
全部用注入式 httpx MockTransport 假网关，解析与校验走真码。
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
from app.scene_runtime.pipeline import (INVALID_RESULT_FAULT,  # noqa: E402
                                        SceneRunner)
from app.scene_runtime.store import Store                       # noqa: E402

W = "kimi-k3"
V = "mc22-flash"
PROSE = "林穗把一枚钱放在桌上，又收了回去，灯花跳了一下。"


def good_draft():
    return json.dumps({"text": PROSE}, ensure_ascii=False)


def _review_body(payload, *, hard=False):
    """按计划事件逐字生成合规评审（hard=True 时只多加一条正文内证据的 hard
    意见 ⇒ validate_review 只剩 unresolved_hard_issue ⇒ 必然进改写轮）。"""
    plan, text = payload["plan"], payload["text"]
    issues = [{"kind": "hard", "description": "动机交代不足", "quote": text}] if hard else []
    return json.dumps({"issues": issues,
                       "changes": [{"fact": c["fact"], "after": c["after"],
                                    "quote": text}
                                   for ev in plan["events"] for c in ev["changes"]],
                       "events": [{"event_id": ev["event_id"], "quote": text}
                                  for ev in plan["events"]]},
                      ensure_ascii=False)


# 假网关的三种「无效」形态（对齐 client.py 判据，本文件不改判据）：
INVALID_EMPTY = {"content": ""}                       # 空 content
INVALID_TRUNCATED = {"content": good_draft(), "finish": "length"}   # finish_reason != stop
INVALID_NO_CONTENT = {"omit_content": True}            # 响应缺 content ⇒ 解析异常


@pytest.fixture
def live_cfg(monkeypatch):
    monkeypatch.setattr(_cfg, "LLM_MODE", "real")
    monkeypatch.setattr(_cfg, "GATEWAY_BASE_URL", "http://gateway.invalid/v1")
    monkeypatch.setattr(_cfg, "GATEWAY_API_KEY", "test-key-only")
    monkeypatch.delenv("LG_ALLOW_MODEL_SUBSTITUTION", raising=False)


class Gateway:
    """脚本化假网关：队列项=str 即回复原文；dict 见上方三种无效形态，另支持
    served_model（模拟换皮）/ status（模拟非 200）。队列打空后 writer 恒回
    合规草稿、verifier 恒按负载回合规评审。"""

    def __init__(self, writer_replies=(), verifier_replies=(), hard_first=False):
        self.wq, self.vq = list(writer_replies), list(verifier_replies)
        self.hard_first = hard_first
        self.verifier_seen = 0
        self.requests = []
        self.transport = httpx.MockTransport(self._handler)

    def _handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        role = "writer" if body["model"] == W else "verifier"
        item = (self.wq if role == "writer" else self.vq)
        item = item.pop(0) if item else None
        if isinstance(item, dict) and item.get("status", 200) != 200:
            return httpx.Response(item["status"], json={"error": "nope"})
        if isinstance(item, dict):
            content = item.get("content", "")
            finish = item.get("finish", "stop")
        elif isinstance(item, str):
            content, finish = item, "stop"
        else:
            if role == "writer":
                content, finish = good_draft(), "stop"
            else:
                self.verifier_seen += 1
                content = _review_body(json.loads(body["messages"][1]["content"]),
                                       hard=self.hard_first and self.verifier_seen == 1)
                finish = "stop"
        message = {"role": "assistant"}
        if not (isinstance(item, dict) and item.get("omit_content")):
            message["content"] = content
        data = {"id": "chatcmpl-fake",
                "model": (item.get("served_model") if isinstance(item, dict) and item.get("served_model")
                          else (W if role == "writer" else V)),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
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


def _job_id(store):
    with store.connection() as db:
        return db.execute("SELECT id FROM jobs").fetchone()[0]


def _rows(store):
    with store.connection() as db:
        return {r[0]: {"status": r[1], "error": r[2], "requested_model": r[3]}
                for r in db.execute("SELECT stage,status,error,requested_model FROM calls")}


INVALID_FORMS = [
    pytest.param(INVALID_EMPTY, id="空content"),
    pytest.param(INVALID_TRUNCATED, id="finish_reason=length"),
    pytest.param(INVALID_NO_CONTENT, id="响应缺content"),
]


@pytest.mark.parametrize("bad", INVALID_FORMS)
def test_writer_invalid_result_recovered_by_same_model_retry(scene, live_cfg, bad):
    """正向（真 client 判据 + 真 `_call` + 真 `_parse_or_retry`）：写手首次
    拿到无效结果 ⇒ 同模型重试给合规 Draft ⇒ 整链成功。失败的那次如实落
    failed 行（留痕），重试以 writer.0.retry 落账、计入 usage.calls。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[bad])
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    usage = store.usage(receipt["job_id"])
    assert [a["stage"] for a in usage["attempts"]] == [
        "writer.0", "writer.0.retry", "verifier.0"]
    assert usage["calls"] == 3, "重试那次必须出现在 usage.calls（不白嫖预算）"
    rows = _rows(store)
    assert rows["writer.0"]["status"] == "failed"
    assert rows["writer.0"]["error"] == INVALID_RESULT_FAULT
    assert rows["writer.0.retry"]["status"] == "succeeded"
    with store.connection() as db:
        committed = db.execute("SELECT text FROM commits").fetchone()[0]
    assert committed == PROSE, "落账正文必须是重试那次真实回复（不是本地伪造的草稿）"
    # 两口径计数互不冒充：写手侧「网关无效重试」不进契约/校验席两个计数器
    # （store.py 不在允许编辑清单内，详见 docs/WRITER_INVALID_RETRY.md §4）。
    assert usage["contract_retries"] == 0
    assert usage["verifier_invalid_retries"] == 0


def test_writer_retry_still_invalid_raises_identical_fault_name(scene, live_cfg):
    """反向①：两次都无效 ⇒ 仍抛 gateway_invalid_or_partial_result（失败原因
    名逐字不变）、两次尝试都留痕、只重试 1 次（无 .retry.retry）。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[INVALID_EMPTY, INVALID_TRUNCATED])
    with pytest.raises(RuntimeFault) as excinfo:
        _run(store, plan, knowledge, gateway)
    assert str(excinfo.value) == "gateway_invalid_or_partial_result"
    assert _stages(store) == ["writer.0", "writer.0.retry"]
    assert len(gateway.requests) == 2, "只许重试 1 次"
    rows = _rows(store)
    assert rows["writer.0"]["status"] == "failed"
    assert rows["writer.0.retry"]["status"] == "failed"
    assert store.usage(_job_id(store))["calls"] == 2


def test_writer_retry_is_charged_to_call_budget(scene, live_cfg):
    """反向②：重试**计入** usage.calls，不白嫖——max_calls=2 时 writer 主调 +
    重试正好占满两个位，verifier 主调被 call_budget_exhausted 拦住
    （若重试白嫖预算，此处会放行到 3 次调用甚至提交成功）。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[INVALID_EMPTY])
    with pytest.raises(RuntimeFault, match="call_budget_exhausted"):
        _run(store, plan, knowledge, gateway, budget=Budget(max_calls=2))
    assert _stages(store) == ["writer.0", "writer.0.retry"]
    usage = store.usage(_job_id(store))
    assert usage["calls"] == 2, "重试占了一个真实预算位"
    assert len(gateway.requests) == 2


def test_model_identity_mismatch_is_not_retried(scene, live_cfg):
    """反向③a：model_identity_mismatch 不得触发本重试（仍当场抛，且失败行
    照旧落 requested/actual 真相）：只调了 1 次，没有 writer.0.retry 行。
    形态：回复**内容有效**但网关换皮（实际模型≠请求模型）——它与「结果无效」
    是两类不同故障，本重试只认后者。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[{"content": good_draft(),
                                       "served_model": "deepseek-v4.1-flash"}])
    with pytest.raises(RuntimeFault) as excinfo:
        _run(store, plan, knowledge, gateway)
    assert str(excinfo.value) == "model_identity_mismatch:%s->deepseek-v4.1-flash" % W
    assert _stages(store) == ["writer.0"]
    assert len(gateway.requests) == 1
    row = _rows(store)["writer.0"]
    assert row["status"] == "failed" and row["error"].startswith("model_identity_mismatch:")


def test_invalid_model_contract_uses_its_own_retry_only(scene, live_cfg):
    """反向③b：invalid_model_contract 不得触发本重试——回复**内容**不合契约
    时走的是契约重试口径（writer.0.retry + contract_retries=1），与网关无效
    重试互不冒充；两次都不合 ⇒ 照旧抛 invalid_model_contract:Draft。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=["{}", "{}"])
    with pytest.raises(RuntimeFault, match="invalid_model_contract:Draft"):
        _run(store, plan, knowledge, gateway)
    assert _stages(store) == ["writer.0", "writer.0.retry"]
    assert _rows(store)["writer.0.retry"]["status"] == "succeeded"
    usage = store.usage(_job_id(store))
    assert usage["contract_retries"] == 1
    assert usage["verifier_invalid_retries"] == 0
    with store.connection() as db:
        frozen = json.loads(db.execute(
            "SELECT request FROM calls WHERE stage='writer.0.retry'").fetchone()[0])
    assert frozen["input"]["contract_error"].startswith("invalid_model_contract:Draft")


def test_other_gateway_faults_are_not_retried(scene, live_cfg):
    """反向③c：非 200（gateway_http_500）等其它 RuntimeFault 一律原样抛，
    本重试只认「结果无效」这一类。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[{"status": 500}])
    with pytest.raises(RuntimeFault, match="gateway_http_500"):
        _run(store, plan, knowledge, gateway)
    assert _stages(store) == ["writer.0"]
    assert len(gateway.requests) == 1


def test_writer_retry_never_switches_model(scene, live_cfg):
    """反向④：重试用的是**同一模型**——两次请求 model 逐字相同；台账 frozen
    请求的 model 仍是 writer 那一个；整臂跑完后请求过的模型集合恰 = {writer,
    verifier} 两个（保住「一臂一模型对唯一」门禁前提）。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[INVALID_EMPTY])
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    assert [r["model"] for r in gateway.requests[:2]] == [W, W], "重试换了模型"
    assert set(r["model"] for r in gateway.requests) == {W, V}
    with store.connection() as db:
        models = [json.loads(r[0])["model"] for r in db.execute(
            "SELECT request FROM calls ORDER BY rowid")]
    assert models == [W, W, V], "台账里两次写手落账的模型必须都等于请求模型"
    rows = _rows(store)
    assert rows["writer.0"]["requested_model"] == W
    assert rows["writer.0.retry"]["requested_model"] == W


def test_writer_retry_blocked_when_last_call_slot_is_left(scene, live_cfg):
    """反向⑤：预算只剩 1 次调用时，重试必须被 call_budget_exhausted 拦住。
    构造：round 0（writer.0 + verifier.0 出 hard issue）已花掉 2 次，
    max_calls=3 ⇒ writer.1 用掉最后 1 次后它拿到无效结果 ⇒ 重试无处可去，
    如实 call_budget_exhausted（不静默放宽、不白嫖）。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[good_draft(), INVALID_EMPTY], hard_first=True)
    with pytest.raises(RuntimeFault, match="call_budget_exhausted"):
        _run(store, plan, knowledge, gateway, budget=Budget(max_calls=3))
    assert _stages(store) == ["writer.0", "verifier.0", "writer.1"]
    assert len(gateway.requests) == 3, "重试请求根本不该发出（预算先拦）"
    assert _rows(store)["writer.1"]["error"] == INVALID_RESULT_FAULT


def test_writer_invalid_and_contract_retry_do_not_collide(scene, live_cfg):
    """两口径叠加时 stage 基于**最终落账 stage** 续名（与校验席同一约定）：
    writer.0 无效 ⇒ writer.0.retry（网关无效口径），其回复又不合契约 ⇒
    writer.0.retry.retry（契约口径）⇒ 提交成功。不续名就会在 reserve_call
    撞 call_input_conflict。"""
    store, plan, knowledge = scene
    gateway = Gateway(writer_replies=[INVALID_EMPTY, "{}"])
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    assert _stages(store) == ["writer.0", "writer.0.retry", "writer.0.retry.retry", "verifier.0"]
    usage = store.usage(receipt["job_id"])
    assert usage["calls"] == 4
    assert usage["contract_retries"] == 1
    assert usage["verifier_invalid_retries"] == 0
    assert len(gateway.requests) == 4


def test_invalid_result_fault_literal_reconciles_with_client():
    """`pipeline.INVALID_RESULT_FAULT` 必须与 `client.py` 抛出的那一份逐字一致。

    判据在 `client.py`、触发码写在 `pipeline.py`（两文件各一份字面量）。
    `client.py` 改名而 pipeline 没跟 ⇒ 写手/校验席重试**静默失效**
    （fail-closed 变成「永不触发」）。这条对账断言把该耦合钉死
    （与仓库既有 `test_docs_code_reconcile.py` 同一风格）。"""
    import inspect

    from app.scene_runtime import client as _client

    assert INVALID_RESULT_FAULT in inspect.getsource(_client)
    assert f'RuntimeFault("{INVALID_RESULT_FAULT}")' in inspect.getsource(_client)
