"""模型池与串行纪律回归（2026-09-18 集霸指令：停用 muse，换 glm-5.3 + 本机 agy Gemini）。

锁定的不变量：

1. **agy 类通道必须串行**：它是本机 CLI、**单账号共享额度**，并发会互相挤掉
   （docs/wb-agy-bridge-guide.md §8）。所有跑模型的循环都得先问
   `gateway.is_serial_model`，不许自己判断前缀。
2. **默认池里不许再出现 `meta/muse-spark-1.3`**（集霸明确要停用）。
3. `agy/xxx` 走桥、其它走网关：路由错了会静默打到错的通道。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import config, gateway  # noqa: E402


def test_agy_is_serial():
    assert gateway.is_serial_model("agy/gemini-3.8-flash-high")
    assert not gateway.is_serial_model("moonshotai/kimi-k3")
    assert not gateway.is_serial_model("z-ai/glm-5.3")
    assert not gateway.is_serial_model(config.DEFAULT_LLM_MODEL)


def test_split_models_groups_serial_apart():
    par, ser = gateway.split_models(["kimi", "agy/gemini-3.8-flash-high", "z-ai/glm-5.3"])
    assert par == ["kimi", "z-ai/glm-5.3"]
    assert ser == ["agy/gemini-3.8-flash-high"]


def test_agy_route_goes_to_bridge(monkeypatch):
    """`agy/` 前缀必须走本机桥，不能落到 OpenAI 兼容网关（会 404/静默失败）。"""
    called = {}

    def fake_agy(**kw):
        called.update(kw)
        return gateway.ChatResult(text="ok", tokens_in=0, tokens_out=0, latency_ms=1)

    monkeypatch.setattr(gateway, "_agy_chat", fake_agy)
    gateway._real_chat(model="agy/gemini-3.8-flash-high", system="s", user="u",
                       purpose="t", prompt_version="pv", temperature=0.0,
                       max_tokens=16, seed=None)
    assert called.get("model") == "agy/gemini-3.8-flash-high"


def test_default_pools_dropped_muse():
    """集霸 2026-09-18 指令：停用 meta/muse-spark-1.3。"""
    import controlled_corruption as CC
    joined = ",".join(CC.DEFAULT_JUDGES) + "," + CC.DEFAULT_VERIFY
    assert "muse-spark" not in joined, "默认池里还有 muse"
    assert any("glm-5.3" in m for m in CC.DEFAULT_JUDGES), "glm-5.3 应在评委池里"
    assert any(m.startswith("agy/") for m in CC.DEFAULT_JUDGES), "本机 Gemini 应在评委池里"


def test_clean_text_model_dropped_muse():
    import clean_text as CT
    assert "muse-spark" not in CT.LLM_MODEL


def test_local_cli_channels_are_serial():
    """本机 CLI 通道（agy / Qoder / WB 国际版）全是单账号共享额度 → 必须串行。"""
    for m in ("agy/gemini-3.8-flash-high", "qoder/Qwen3.8-Flash", "wb/hy4-preview-f"):
        assert gateway.is_serial_model(m), f"{m} 必须串行"
    par, ser = gateway.split_models(["qoder/Qwen3.8-Flash", "wb/hy4-preview-f",
                                     "agy/gemini-3.8-flash-high", "moonshotai/kimi-k3"])
    assert par == ["moonshotai/kimi-k3"]
    assert len(ser) == 3


def test_qoder_wb_routes(monkeypatch):
    """`qoder/` 与 `wb/` 必须走各自的本机桥，不能落到中转网关。"""
    seen = []

    def fake_qoder(**kw):
        seen.append(("qoder", kw["model"]))
        return gateway.ChatResult(text="ok", tokens_in=0, tokens_out=0, latency_ms=1)

    def fake_wb(**kw):
        seen.append(("wb", kw["model"]))
        return gateway.ChatResult(text="ok", tokens_in=0, tokens_out=0, latency_ms=1)

    monkeypatch.setattr(gateway, "_qoder_chat", fake_qoder)
    monkeypatch.setattr(gateway, "_wb_chat", fake_wb)
    gateway._real_chat(model="qoder/Qwen3.8-Flash", system="s", user="u", purpose="t",
                       prompt_version="pv", temperature=0.0, max_tokens=16, seed=None)
    gateway._real_chat(model="wb/hy4-preview-f", system="s", user="u", purpose="t",
                       prompt_version="pv", temperature=0.0, max_tokens=16, seed=None)
    assert ("qoder", "qoder/Qwen3.8-Flash") in seen
    assert ("wb", "wb/hy4-preview-f") in seen


def test_cli_output_unwrapping():
    """本机 CLI 的输出要剥壳：Qoder 会先打一行 `[meta] {...}`，WB 把回答塞在 detail 里。"""
    assert gateway._META_LINE.sub("", '[meta] {"a":1}\n{"ok": true}').strip() == '{"ok": true}'
