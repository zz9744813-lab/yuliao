"""A06 回归：非空截断输出不得当成功（审查 20260920-1810）。

事故：网关读了 finish_reason 却只拒绝空正文——finish_reason=length 且
正文非空时仍返回 status=ok，复现返回「尚未写完的半句」而调用状态是
成功；自由文本候选可能以完整样本身份进重建与后续评审。

修复契约（监督 2026-09-21 08:25 口径）：
1. 明确的完成原因白名单 OK_FINISH_REASONS（stop/end_turn/stop_sequence/
   None——部分中转成功时不回 finish_reason）；
2. length / content_filter / tool_calls 等非白名单完成方式，正文非空
   也一律记**非完整产物**（本趟 usage 已由 A05 结算入账）；
3. 有上限的恢复流程：length 走既有预算加倍重试（≤MAX_RETRIES、≤8192）；
   不可恢复的完成方式立即失败，不空转烧钱；
4. 测试不只覆盖「截断且空文本」——半句+length 是主案。
"""
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.gateway as gw                    # noqa: E402
from app import config, db                    # noqa: E402
from app.models import LlmCall                # noqa: E402

MODEL = "testmodel-a06"
_REAL_CLIENT = httpx.Client   # 补丁前固定真类：同测试内二次 install 时
                              # httpx.Client 已是 lambda，再取就套娃了


def _body(content="他站在门口，还没说完", finish="length", pin=10, pout=8):
    return {"choices": [{"message": {"content": content},
                          "finish_reason": finish}],
            "usage": {"prompt_tokens": pin, "completion_tokens": pout}}


@pytest.fixture()
def scripted(monkeypatch):
    db.init_db()

    class _S:
        def __init__(self, script):
            self.script = list(script)

        def __call__(self, request):
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    def _install(script):
        s = _S(script)
        transport = httpx.MockTransport(s)
        monkeypatch.setattr(gw.httpx, "Client",
                            lambda timeout=None: _REAL_CLIENT(transport=transport,
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
    return gw._real_chat(model=MODEL, system="s", user="u", purpose="a06-test",
                         prompt_version="pv", temperature=0.1, max_tokens=512,
                         seed=1)


def _rows():
    with db.session() as s:
        rs = (s.query(LlmCall).filter(LlmCall.model == MODEL)
              .order_by(LlmCall.attempt_no).all())
        return [dict(att=r.attempt_no, status=r.status, error=(r.error or "")[:60],
                     tin=r.tokens_in, tout=r.tokens_out) for r in rs]


def test_half_sentence_length_rejected_then_recovers(scripted):
    """主案：length + 非空半句 → 拒收留痕（usage 入账）→ 加预算重试成功。

    旧实现这里返回 status=ok——半句以完整样本身份进下游。"""
    scripted([httpx.Response(200, json=_body(content="他站在门口，还没说完",
                                             finish="length", pin=12, pout=8)),
             httpx.Response(200, json=_body(content="完整的一句话说完了。",
                                             finish="stop", pin=20, pout=6))])
    r = _call()
    assert r.status == "ok" and r.text.startswith("完整")
    rows = _rows()
    assert len(rows) == 2
    assert rows[0]["status"] == "failed" and "finish_reason=length" in rows[0]["error"], \
        "半句截断必须记非完整产物"
    assert (rows[0]["tin"], rows[0]["tout"]) == (12, 8), "截断趟已烧 usage 必须保存"
    assert rows[1]["status"] == "ok"


def test_length_exhausts_capped_recovery_fails(scripted):
    """加预算重试有上限：MAX_RETRIES 趟全截断 → 抛错，每趟各自留痕+usage。"""
    scripted([httpx.Response(200, json=_body())] * config.MAX_RETRIES)
    with pytest.raises(gw.LLMError, match="incomplete.*length"):
        _call()
    rows = _rows()
    assert len(rows) == config.MAX_RETRIES, \
        f"有上限恢复：最多 {config.MAX_RETRIES} 趟，不许无限重试"
    assert all(x["status"] == "failed" for x in rows)
    assert all("finish_reason=length" in x["error"] for x in rows)
    assert all(x["tin"] == 10 and x["tout"] == 8 for x in rows), "每趟 usage 都在账"


def test_content_filter_fails_immediately_no_retry(scripted):
    """不可恢复的完成方式立即失败：重发同 payload 救不回 content_filter，
    不空转烧钱（有上限恢复 = length 加预算；其余一次即止）。"""
    scripted([httpx.Response(200, json=_body(content="敏感内容", finish="content_filter")),
             httpx.Response(200, json=_body(content="正常", finish="stop"))])
    with pytest.raises(gw.LLMError, match="content_filter"):
        _call()
    rows = _rows()
    assert len(rows) == 1, "content_filter 不许重试——第二发是白烧钱"


def test_whitelist_accepts_end_turn_and_missing_finish(scripted):
    """白名单：end_turn / None（部分中转成功时不回 finish_reason）→ ok。"""
    scripted([httpx.Response(200, json=_body(content="正常结束A", finish="end_turn"))])
    assert _call().text == "正常结束A"
    scripted([httpx.Response(200, json=_body(content="正常结束B", finish=None))])
    assert _call().text == "正常结束B"
    rows = _rows()
    assert len(rows) == 2 and all(x["status"] == "ok" for x in rows)


def test_truncated_and_empty_still_caught_by_empty_gate(scripted):
    """旧口径不回归：截断**且**空文本走 P0 空容闸（有独立报错与恢复）。"""
    scripted([httpx.Response(200, json=_body(content="", finish="length", pin=5)),
             httpx.Response(200, json=_body(content="回来了。", finish="stop", pin=6))])
    r = _call()
    assert r.status == "ok" and r.text == "回来了。"
    rows = _rows()
    assert rows[0]["status"] == "failed" and "empty content" in rows[0]["error"], \
        "空正文走 P0 闸（与 A06 非空截断是两条路径，都要拦）"
    assert rows[0]["tin"] == 5, "空正文趟的 usage 照样在账（A05）"
