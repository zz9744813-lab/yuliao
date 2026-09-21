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
from types import SimpleNamespace

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


# ── P0 死模型 id（2026-09-20）：残留必须是 0，且出口要能自纠 ──────

def test_egress_canonicalizes_the_dead_alias(monkeypatch):
    """调用方递来已下线的旧 id 时，出口必须换成在册名再发。

    为什么放在出口而不是"改完字符串就完事"：旧名还活在**历史实验配置**与
    控制台默认值里，改不完；而它的表现不是报错，是整批 503 + 调用方只见计数
    （第五批扩产就被这么误判成"池子耗尽"）。
    """
    sent: dict = {}

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "好"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            sent["model"], sent["auth"] = json["model"], dict(headers or {})
            return Resp()

    import httpx
    monkeypatch.setattr(gateway, "httpx", SimpleNamespace(Client=Client,
                                                          HTTPError=httpx.HTTPError))
    # A05 后重试环的记账点从 _record 移到 _reserve（先预留）/_settle
    # （单独结算）——「账本记真正发出去的名字」契约钉在 _reserve 上。
    monkeypatch.setattr(gateway, "_reserve",
                        lambda purpose, model, pv, lcid, att:
                        (sent.setdefault("logged", model), "LC-fake")[1])
    monkeypatch.setattr(gateway, "_settle", lambda rid, r: None)
    monkeypatch.setattr(config, "GATEWAY_BASE_URL", "http://gw.test:3000/v1")
    monkeypatch.setattr(config, "GATEWAY_API_KEY", "sk-test")
    dead, live = next(iter(config.DEAD_MODEL_ALIASES.items()))
    assert gateway._real_chat(model=dead, system="s", user="u", purpose="t",
                              prompt_version="pv", temperature=0.0,
                              max_tokens=16, seed=None).text == "好"
    assert sent["model"] == live, "发出去的仍是死 id → 整批 503"
    assert sent["logged"] == live, "llm_calls 要记真正发出去的名字，否则用量表在骗人"


def test_live_model_defaults_carry_no_dead_id():
    """脚本级"默认模型常量"一律指向在册名（默认值会被整批复用，错一个=白跑一轮）。

    只收**导入不碰库**的脚本：v2_rebuild / gen_cand_one 这类在模块级查库，
    放进单测里会让这一条变成分钟级慢测。
    """
    import ai_ranking_build as AR
    import bench_recon_setup as BR
    import controlled_corruption as CC
    import heldout_eval as HE
    import pref_judge as PJ
    import source_check as SRC
    import xcorpus_bias as XC
    dead = set(config.DEAD_MODEL_ALIASES)
    defaults = [BR.MODEL, SRC.MODEL, CC.DEFAULT_GEN, CC.DEFAULT_VERIFY, *CC.DEFAULT_JUDGES,
                *HE.JUDGES, *AR.JUDGES, *PJ.DEFAULT_JUDGES.split(","),
                *XC.DEFAULT_JUDGES.split(","), config.DEFAULT_LLM_MODEL, config.STRONG_MODEL,
                *config.DEFAULT_RECON_MODELS, *config.EXTRACTOR_MODELS]
    bad = sorted({m.strip() for m in defaults if m.strip() in dead})
    assert not bad, f"默认值里仍有已下线 id {bad}"
    # 单一来源：默认名必须就是常量本身，而不是又一份同字面量（改常量要能全线跟着变）
    assert CC.DEFAULT_GEN == config.DEFAULT_LLM_MODEL
    assert config.DEFAULT_RECON_MODELS[0] == config.DEFAULT_LLM_MODEL


def test_live_surfaces_do_not_advertise_the_dead_id():
    """README / .env 模板 / 控制台默认值是"人会抄的地方"，残留数必须为 0。

    docs/*.md 与 data/ 是历史证据，不在射程内（事故记录里就是要留旧名）。
    """
    dead = set(config.DEAD_MODEL_ALIASES)
    root = Path(__file__).resolve().parent.parent
    hits = []
    for rel in ("README.md", ".env.example", "app/static/index.html"):
        text = (root / rel).read_text(encoding="utf-8")
        hits += [(rel, d) for d in dead if d in text]
    assert not hits, f"活文件里还写着已下线 id：{hits}"


def test_alias_table_is_two_way_consistent():
    """死 id 别名表是**唯一口径**：写入侧归一、查询侧新旧都认，两边吃同一张表。

    表被 7 处调用（gateway 出口、pref_judge/heldout_eval/xcorpus_bias 的幂等键、
    controlled_corruption 的去重、judge_matrix 的读数）。写新查旧或写旧查新都
    不报错，只是静默漏数据——所以口径一致性只能靠测试钉住。
    """
    for dead, live in config.DEAD_MODEL_ALIASES.items():
        assert config.canonical_model(dead) == live
        assert config.canonical_model(live) == live, "在册名再被改写就是二次事故"
        assert set(config.model_any(dead)) == set(config.model_any(live)) == {dead, live}
    assert config.canonical_model(f"  {config.DEFAULT_LLM_MODEL}  ") == config.DEFAULT_LLM_MODEL
    assert set(config.model_any("moonshotai/kimi-k3")) == {"moonshotai/kimi-k3"}
