"""A05 回归：网关重试不再吞账（审查 20260920-1810）。

事故（审查 MockTransport 复现）：两次上游请求合计 45 token，账本只留
一条成功记录 15 token——第一次失败的 usage 凭空消失。根因：可重试
HTTP / 空正文续试 / 传输异常都在循环内静默 continue，只有最终结果落
一行；实验引擎又把 llm_calls 当唯一费用/失败账本 → 报表反映不出真实
请求尝试，失败率被低估、成本被少记。

修复契约（监督 2026-09-21 08:25 口径）：
1. 每次派发**先预留**（dispatched 行）——进程中途崩掉这次尝试也留痕；
   每次返回**单独结算**（本尝试的 usage/延迟/状态/错误）；
2. 账本分列两个口径：n（HTTP 尝试）与 n_logical（逻辑调用，按
   logical_call_id 分组；历史行 NULL → 每行自成一组）；
3. 截断/错误请求已产生的 usage 必须保存（空正文那趟烧的 token 不许消失）；
4. 未知费用保持未知（cost=None，不冒充已知 0）；
5. **N 次 HTTP 尝试 = N 条尝试记录；失败那次同样留痕。**
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.gateway as gw                    # noqa: E402
from app import config, db, observability    # noqa: E402
from app.models import LlmCall               # noqa: E402

MODEL = "testmodel-v1"


def _ok_body(content="正常输出", pin=10, pout=5, finish="stop"):
    return {"choices": [{"message": {"content": content},
                          "finish_reason": finish}],
            "usage": {"prompt_tokens": pin, "completion_tokens": pout}}


class _Scripted:
    """按序回放脚本响应；可投递 httpx 响应或异常类（传输失败）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def scripted(monkeypatch):
    """把网关的 httpx.Client 换成注入 MockTransport 的工厂（测试环境专用）。

    本文件可能在全库任何表都没建的裸库上单跑——fixture 先 init_db。"""
    db.init_db()

    def _install(script):
        s = _Scripted(script)
        transport = httpx.MockTransport(s)
        real_client = httpx.Client
        monkeypatch.setattr(gw.httpx, "Client",
                            lambda timeout=None: real_client(transport=transport,
                                                            timeout=timeout))
        monkeypatch.setattr(config, "GATEWAY_BASE_URL", "http://gateway.test")
        monkeypatch.setattr(config, "GATEWAY_API_KEY", "k-test")
        monkeypatch.setattr(gw.time, "sleep", lambda _s: None)
        return s

    yield _install
    try:
        with db.session() as s2:
            s2.query(LlmCall).filter(LlmCall.model == MODEL).delete()
            s2.commit()
    except Exception:
        pass


def _call():
    return gw._real_chat(model=MODEL, system="s", user="u", purpose="a05-test",
                         prompt_version="pv", temperature=0.1, max_tokens=512,
                         seed=1)


def _rows():
    with db.session() as s:
        rs = (s.query(LlmCall).filter(LlmCall.model == MODEL)
              .order_by(LlmCall.attempt_no).all())
        return [dict(id=r.id, lcid=r.logical_call_id, att=r.attempt_no,
                     status=r.status, error=(r.error or "")[:60],
                     tin=r.tokens_in, tout=r.tokens_out, cost=r.cost) for r in rs]


def test_attempt_rows_match_http_attempts_503_then_ok(scripted):
    """N 次 HTTP 尝试 = N 条尝试记录；503 那次同样留痕。"""
    scripted([httpx.Response(503, text="upstream boom"),
             httpx.Response(200, json=_ok_body())])
    r = _call()
    assert r.status == "ok"
    rows = _rows()
    assert len(rows) == 2, "两次 HTTP 尝试必须两行（旧实现只留最后一行）"
    assert all(x["lcid"] == rows[0]["lcid"] for x in rows), "同一逻辑调用共用 lcid"
    assert [x["att"] for x in rows] == [0, 1]
    assert rows[0]["status"] == "failed" and "503" in rows[0]["error"], \
        "失败尝试的痕迹不许消失"
    assert rows[1]["status"] == "ok" and rows[1]["tin"] == 10


def test_empty_content_retry_keeps_burned_usage(scripted):
    """审查复现口径：两次请求合计 45 token 必须全额入账。

    第一次 200 但空正文（usage 已烧 30）→ 重试；第二次成功 15。
    旧账只留成功行 15，30 凭空消失。"""
    scripted([httpx.Response(200, json=_ok_body(content="  ", pin=30, pout=0)),
             httpx.Response(200, json=_ok_body(pin=15, pout=15))])
    r = _call()
    assert r.status == "ok"
    rows = _rows()
    assert len(rows) == 2
    assert rows[0]["status"] == "failed" and "empty content" in rows[0]["error"]
    assert rows[0]["tin"] == 30, "空正文那趟已烧的 usage 必须保存"
    assert rows[1]["tin"] == 15 and rows[1]["tout"] == 15
    assert sum(x["tin"] for x in rows) == 45, "45 token 全额在账（事故的复现数）"


def test_transport_error_attempt_recorded(scripted):
    """传输异常的尝试也留痕（旧实现 except 后静默 continue）。"""
    scripted([httpx.ConnectError("connection reset by peer"),
             httpx.Response(200, json=_ok_body())])
    r = _call()
    assert r.status == "ok"
    rows = _rows()
    assert len(rows) == 2
    assert rows[0]["status"] == "failed" and "transport" in rows[0]["error"]


def test_all_attempts_fail_leaves_n_failed_rows(scripted):
    """全军覆没：MAX_RETRIES 趟 = MAX_RETRIES 行全 failed（每趟各自留痕）。"""
    scripted([httpx.Response(503, text="down")] * config.MAX_RETRIES)
    with pytest.raises(gw.LLMError):
        _call()
    rows = _rows()
    assert len(rows) == config.MAX_RETRIES, \
        f"{config.MAX_RETRIES} 趟尝试必须 {config.MAX_RETRIES} 行"
    assert all(x["status"] == "failed" for x in rows)
    assert all(x["lcid"] == rows[0]["lcid"] for x in rows)


def test_reserve_then_settle_crash_trace(scripted):
    """先预留：派发前就有 dispatched 行——崩在请求中途也留痕；结算改写它。"""
    db.init_db()
    rid = gw._reserve("a05-test", MODEL, "pv", "LC-test-lcid", 7)
    with db.session() as s:
        row = s.get(LlmCall, rid)
        assert row.status == "dispatched" and row.attempt_no == 7 \
            and row.logical_call_id == "LC-test-lcid"
    gw._settle(rid, gw.ChatResult(text="", tokens_in=3, tokens_out=4,
                                  latency_ms=99, status="failed", error="boom"))
    with db.session() as s:
        row = s.get(LlmCall, rid)
        assert (row.status, row.error) == ("failed", "boom")
        assert (row.tokens_in, row.tokens_out, row.latency_ms) == (3, 4, 99)
    with db.session() as s:
        s.query(LlmCall).filter_by(id=rid).delete()
        s.commit()


def test_unknown_cost_stays_none(scripted):
    """未知费用保持未知：所有行 cost=None，不冒充已知 0。"""
    scripted([httpx.Response(200, json=_ok_body())])
    _call()
    assert all(x["cost"] is None for x in _rows()), \
        "中转单价未知 → cost=None 是契约（写 0 就是冒充已知）"


def test_single_shot_rows_get_logical_id(scripted):
    """单发路径（mock / 桥接式 _record）：每行自成逻辑调用，lcid 非空 attempt=0。"""
    db.init_db()
    gw.bind_experiment("EXP-A05TEST")
    try:
        gw._record("a05-test", MODEL, "pv",
                   gw.ChatResult(text="x", tokens_in=1, tokens_out=2,
                                 latency_ms=5, status="ok"))
    finally:
        gw.bind_experiment(None)
    rows = _rows()
    assert len(rows) == 1 and rows[0]["lcid"] and rows[0]["att"] == 0


def test_observability_block_reports_both_counts(scripted):
    """报表双口径分列：n=尝试数、n_logical=逻辑调用数。"""
    scripted([httpx.Response(503, text="x"),
             httpx.Response(200, json=_ok_body())])
    _call()
    with db.session() as s:
        rows = (s.query(LlmCall).filter(LlmCall.model == MODEL)
                .order_by(LlmCall.attempt_no).all())
    b = observability._block(rows)
    assert b["n"] == 2 and b["n_logical"] == 1, \
        "一次逻辑调用两次尝试：n=2 / n_logical=1（旧报表只有 n，且少记失败）"
    assert b["failed"] == 1, "失败率从此如实"
