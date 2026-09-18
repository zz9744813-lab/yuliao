"""Writer 接口骨架回归：模式校验、prompt 同源、gateway 调用参数。"""
import pytest

from app import writer
from app.writer import WriterRequest, build_prompt, write


def test_frame_only_requires_frame():
    with pytest.raises(ValueError):
        WriterRequest(mode="frame_only").validate()


def test_context_only_requires_prev():
    with pytest.raises(ValueError):
        WriterRequest(mode="context_only").validate()


def test_context_and_frame_requires_both():
    with pytest.raises(ValueError):
        WriterRequest(mode="context_and_frame", prev1="上段", frame=None).validate()
    with pytest.raises(ValueError):
        WriterRequest(mode="context_and_frame", prev1=None, frame={"event": "x"}).validate()


def test_prompt_frame_only_uses_recon_builder():
    sys_p, user = build_prompt(WriterRequest(mode="frame_only", frame={"event": "他离开"}))
    assert "他离开" in user and sys_p == writer.RECON_CTX_SYSTEM


def test_prompt_context_only_matches_experiment_template():
    sys_p, user = build_prompt(WriterRequest(mode="context_only",
                                             prev2="更远的一段。", prev1="紧邻的一段。"))
    assert sys_p == writer.CTXONLY_SYSTEM
    # 与 scripts/factorial_d.py 的 USER 模板同源（正典在 app.prompts_ctx）
    assert "【前文-2】更远的一段。" in user
    assert "【前文-1】紧邻的一段。" in user
    assert user.endswith('写"下一段"：')


def test_prompt_context_and_frame_matches_phase15_template():
    _, user = build_prompt(WriterRequest(mode="context_and_frame", prev1="上段",
                                         frame={"event": "他离开", "must_not_state": ["去向"]}))
    assert "【当前段骨架】" in user and "must_not_state" in user
    assert user.endswith('现在写"当前段"：')


def test_write_passes_mode_version_and_returns_meta(monkeypatch):
    captured = {}

    def fake_chat(**kw):
        captured.update(kw)

        class R:
            text = "  生成的正文。 "
            tokens_in, tokens_out, latency_ms = 10, 5, 123

        return R()

    monkeypatch.setattr(writer, "chat", fake_chat)
    req = WriterRequest(mode="context_and_frame", prev2="a", prev1="b",
                        frame={"event": "x"})
    res = write(req, model="test/model", temperature=0.5)
    assert res.text == "生成的正文。"
    assert res.prompt_version == "recon_ctx_v1"
    assert captured["purpose"] == "writer:context_and_frame"
    assert captured["prompt_version"] == "recon_ctx_v1"
    assert captured["temperature"] == 0.5
    assert res.meta["latency_ms"] == 123


def test_write_context_only_version(monkeypatch):
    monkeypatch.setattr(writer, "chat", lambda **kw: type("R", (), {
        "text": "x", "tokens_in": 0, "tokens_out": 0, "latency_ms": 0})())
    res = write(WriterRequest(mode="context_only", prev1="b"))
    assert res.prompt_version == "recon_ctxonly_v1"
