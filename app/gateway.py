"""LLM 网关：OpenAI 兼容中转 + mock 模式。

- real 模式：走 config.GATEWAY_BASE_URL，统一记录 token / 延迟 / 状态到 llm_calls
- mock 模式：LG_LLM_MODE=mock 时不发网络，返回确定性伪输出，可跑通全流程做 dry-run
- 不做价格假设：中转站单价未知，cost 一律 None，只记 token
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import config
from .db import session
from .models import LlmCall

# 线程本地实验归属：stage 线程设置后，该线程发出的所有调用都记到该实验名下，
# 报告统计用量时精确切账，不再依赖"实验创建时间起"的时间窗（并行实验会串账）。
_tls = threading.local()


def bind_experiment(experiment_id: str | None) -> None:
    _tls.experiment_id = experiment_id


@dataclass
class ChatResult:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    status: str = "ok"
    error: str | None = None


class LLMError(Exception):
    pass


def _record(purpose: str, model: str, prompt_version: str, r: ChatResult) -> None:
    with session() as s:
        s.add(LlmCall(
            experiment_id=getattr(_tls, "experiment_id", None),
            purpose=purpose, model=model, prompt_version=prompt_version,
            tokens_in=r.tokens_in, tokens_out=r.tokens_out,
            latency_ms=r.latency_ms, status=r.status, error=r.error,
        ))
        s.commit()


def chat(*, model: str, system: str, user: str, purpose: str,
         prompt_version: str = "", temperature: float = 0.7,
         max_tokens: int = 4096, seed: int | None = None) -> ChatResult:
    if config.LLM_MODE == "mock":
        return _mock_chat(model=model, user=user, purpose=purpose,
                          prompt_version=prompt_version, temperature=temperature, seed=seed)
    return _real_chat(model=model, system=system, user=user, purpose=purpose,
                      prompt_version=prompt_version, temperature=temperature,
                      max_tokens=max_tokens, seed=seed)


# ── Antigravity（Google Gemini）桥 ────────────────────────────
# 模型 id 形如 `agy/gemini-3.8-flash-high` → 走本机 agy 桥（F:\Hermes\scripts\agy_cli.py）。
# 为什么走 CLI 而不是 HTTP：本机到 Google 的可用通道就是这个桥（会自行注入 127.0.0.1:2080 代理）。
# ⚠ 纪律（见 docs/wb-agy-bridge-guide.md §8）：
#   · 单账号共享额度 → **顺序调用，不要并发**（跑这类模型时 concurrency 必须 =1）；
#   · 每次调用有 ~13k token 的固定系统开销；
#   · print 单轮上限默认 5 分钟，长任务必须显式调大，否则输出被砍成 partial；
#   · `-p` 会吃掉后一个参数 → 桥接调用一律用位置参数形式的提示词。
_AGY_BRIDGE = Path("F:/Hermes/scripts/agy_cli.py")

# 必须**顺序**调用的模型前缀：本机 CLI 类通道是单账号共享额度，
# 并发会互相挤掉/报额度错（见 docs/wb-agy-bridge-guide.md §8）。
# 所有跑模型的循环都要先问这个函数，别自己判断字符串前缀。
SERIAL_MODEL_PREFIXES = ("agy/", "qoder/", "wb/", "zcode/")


def is_serial_model(model: str) -> bool:
    """该模型是否必须串行调用（单账号共享额度）。"""
    return any((model or "").startswith(x) for x in SERIAL_MODEL_PREFIXES)


def split_models(models) -> tuple[list[str], list[str]]:
    """把模型分成 (可并发, 必须串行) 两组。"""
    serial = [m for m in models if is_serial_model(m)]
    par = [m for m in models if not is_serial_model(m)]
    return par, serial


def _agy_chat(*, model: str, system: str, user: str, purpose: str,
              prompt_version: str) -> ChatResult:
    import subprocess
    real = model.split("/", 1)[1]
    prompt = (system.strip() + "\n\n" + user) if system else user
    t0 = time.time()
    cmd = [sys.executable, str(_AGY_BRIDGE), prompt, "--json", "--model", real,
           "--print-timeout", "10m", "--timeout", "900"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=1000)
    except subprocess.TimeoutExpired:
        r = ChatResult(text="", tokens_in=0, tokens_out=0,
                       latency_ms=int((time.time() - t0) * 1000),
                       status="failed", error="agy bridge timeout")
        _record(purpose, model, prompt_version, r)
        raise LLMError("agy bridge timeout")
    text = (proc.stdout or "").strip()
    # 桥把进度行写到 stderr（`[agy] 9.4s status=SUCCESS ...`），stdout 才是模型回答。
    if proc.returncode != 0 or not text:
        r = ChatResult(text="", tokens_in=0, tokens_out=0,
                       latency_ms=int((time.time() - t0) * 1000), status="failed",
                       error=f"agy rc={proc.returncode}: {(proc.stderr or '')[:200]}")
        _record(purpose, model, prompt_version, r)
        raise LLMError(r.error)
    r = ChatResult(text=text, tokens_in=0, tokens_out=0,
                   latency_ms=int((time.time() - t0) * 1000))
    _record(purpose, model, prompt_version, r)
    return r


# ── 本机 CLI 通道：Qoder（免费档）与 WorkBuddy 国际版 ──────────
# 两个都是**本机 CLI、单账号共享额度 → 必须串行**（见 SERIAL_MODEL_PREFIXES）。
# 模型 id 形如 `qoder/Qwen3.8-Flash`、`wb/hy4-preview-f`。
# ⚠ 凭证纪律（docs/wb-agy-bridge-guide.md §2.3）：桥接自己读令牌，
#   我们**不打印、不写文件、不落库**任何 key/token。
_QODER_BRIDGE = Path("F:/Hermes/scripts/qoder_cli.py")
_WB_BRIDGE = Path("F:/Hermes/scripts/wbai_bridge.py")
# ZCode：**账号自带套餐额度**（Start/Coding Plan，走 zcode.z.ai 的 anthropic 代理）。
# 与 relay 无关 —— 用它跑批量等于白嫖套餐包，是本机最便宜的代码/文本产能。
# 模型 id 形如 `zcode/glm-5.3-flash`；桥接内部 `--provider builtin:zai-start-plan`
# 会把 GUI 里整条 provider 配置搬给 CLI（裸 key 直连该端点会 401，必须让 CLI 自己发）。
_ZCODE_BRIDGE = Path("F:/Hermes/scripts/zcode_cli.py")

# 本机 CLI 输出是"人看的"，需要剥壳：Qoder 会先打一行 `[meta] {...}`，
# WB 把回答塞在 JSON 的 detail 里并带 `result: ` 前缀。
_META_LINE = re.compile(r"^\[meta\].*$", re.M)


def _run_bridge(cmd: list[str], *, env_extra: dict | None = None,
                strip_proxy: bool = False, timeout: int = 900) -> tuple[str, str, int]:
    """跑桥接，返回 (stdout, stderr, rc)。strip_proxy：Qoder 直连更快（挂代理不报错、只是慢）。"""
    import subprocess
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    if strip_proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
            env.pop(k, None)
    # ⚠ `stdin=DEVNULL` 不能省：后台（nohup）跑时子进程会继承调用方的 stdin 管道，
    # 只要那个管道不 EOF，桥接就**一直挂着不发请求**——表现是"进程活着、日志没动静、
    # 一次调用都没发出去"（2026-09-18 实测：WB 通道因此静默卡了 12 分钟）。
    # 这也是 serve_remote.sh 里记过的同一条坑。
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env,
                          stdin=subprocess.DEVNULL)
    return proc.stdout or "", proc.stderr or "", proc.returncode


def _qoder_chat(*, model: str, system: str, user: str, purpose: str,
                prompt_version: str) -> ChatResult:
    real = model.split("/", 1)[1]
    prompt = (system.strip() + chr(10) + chr(10) + user) if system else user
    t0 = time.time()
    try:
        out, err, rc = _run_bridge([sys.executable, str(_QODER_BRIDGE), prompt,
                                    "-m", real, "--json"],
                                   strip_proxy=True, timeout=600)
    except Exception as e:                      # noqa: BLE001
        r = ChatResult(text="", tokens_in=0, tokens_out=0,
                       latency_ms=int((time.time() - t0) * 1000), status="failed",
                       error=f"qoder bridge: {type(e).__name__}")
        _record(purpose, model, prompt_version, r)
        raise LLMError(r.error)
    text = _META_LINE.sub("", out).strip()
    if rc != 0 or not text:
        r = ChatResult(text="", tokens_in=0, tokens_out=0,
                       latency_ms=int((time.time() - t0) * 1000), status="failed",
                       error=f"qoder rc={rc}: {(err or out)[:200]}")
        _record(purpose, model, prompt_version, r)
        raise LLMError(r.error)
    r = ChatResult(text=text, tokens_in=0, tokens_out=0,
                   latency_ms=int((time.time() - t0) * 1000))
    _record(purpose, model, prompt_version, r)
    return r


def _wb_chat(*, model: str, system: str, user: str, purpose: str,
             prompt_version: str) -> ChatResult:
    import json as _json
    real = model.split("/", 1)[1]
    prompt = (system.strip() + chr(10) + chr(10) + user) if system else user
    t0 = time.time()
    try:
        out, err, rc = _run_bridge([sys.executable, str(_WB_BRIDGE), "--model", real,
                                    "--json", "--timeout", "850", prompt], timeout=900)
    except Exception as e:                      # noqa: BLE001
        r = ChatResult(text="", tokens_in=0, tokens_out=0,
                       latency_ms=int((time.time() - t0) * 1000), status="failed",
                       error=f"wb bridge: {type(e).__name__}")
        _record(purpose, model, prompt_version, r)
        raise LLMError(r.error)
    text = ""
    i, j = out.find("{"), out.rfind("}")
    if i >= 0 and j > i:
        try:
            d = _json.loads(out[i:j + 1])
            text = str(d.get("detail") or d.get("result") or "").strip()
            if text.startswith("result:"):
                text = text[len("result:"):].strip()
        except Exception:
            text = ""
    if rc != 0 or not text:
        r = ChatResult(text="", tokens_in=0, tokens_out=0,
                       latency_ms=int((time.time() - t0) * 1000), status="failed",
                       error=f"wb rc={rc}: {(err or out)[:200]}")
        _record(purpose, model, prompt_version, r)
        raise LLMError(r.error)
    r = ChatResult(text=text, tokens_in=0, tokens_out=0,
                   latency_ms=int((time.time() - t0) * 1000))
    _record(purpose, model, prompt_version, r)
    return r


def _zcode_chat(*, model: str, system: str, user: str, purpose: str,
                prompt_version: str) -> ChatResult:
    """ZCode 套餐线（glm-5.3-flash）：走本机 CLI + 账号套餐额度，不经 relay。"""
    real = model.split("/", 1)[1]
    prompt = (system.strip() + chr(10) + chr(10) + user) if system else user
    t0 = time.time()
    try:
        out, err, rc = _run_bridge([sys.executable, str(_ZCODE_BRIDGE), prompt,
                                    "--json", "--model", real,
                                    "--provider", "builtin:zai-start-plan",
                                    "--timeout", "850"], timeout=900)
    except Exception as e:                      # noqa: BLE001
        r = ChatResult(text="", tokens_in=0, tokens_out=0,
                       latency_ms=int((time.time() - t0) * 1000), status="failed",
                       error=f"zcode bridge: {type(e).__name__}")
        _record(purpose, model, prompt_version, r)
        raise LLMError(r.error)
    text, tin, tout = "", 0, 0
    i, j = out.find("{"), out.rfind("}")
    if i >= 0 and j > i:
        try:
            d = json.loads(out[i:j + 1])
            text = str(d.get("response") or "").strip()
            u = d.get("usage") or {}
            tin = int(u.get("inputTokens") or 0)
            tout = int(u.get("outputTokens") or 0)
        except Exception:
            text = ""
    if rc != 0 or not text:
        r = ChatResult(text="", tokens_in=0, tokens_out=0,
                       latency_ms=int((time.time() - t0) * 1000), status="failed",
                       error=f"zcode rc={rc}: {(err or out)[:200]}")
        _record(purpose, model, prompt_version, r)
        raise LLMError(r.error)
    r = ChatResult(text=text, tokens_in=tin, tokens_out=tout,
                   latency_ms=int((time.time() - t0) * 1000))
    _record(purpose, model, prompt_version, r)
    return r


def _real_chat(*, model: str, system: str, user: str, purpose: str,
               prompt_version: str, temperature: float, max_tokens: int,
               seed: int | None) -> ChatResult:
    if model.startswith("agy/"):
        return _agy_chat(model=model, system=system, user=user, purpose=purpose,
                         prompt_version=prompt_version)
    if model.startswith("qoder/"):
        return _qoder_chat(model=model, system=system, user=user, purpose=purpose,
                           prompt_version=prompt_version)
    if model.startswith("wb/"):
        return _wb_chat(model=model, system=system, user=user, purpose=purpose,
                        prompt_version=prompt_version)
    if model.startswith("zcode/"):
        return _zcode_chat(model=model, system=system, user=user, purpose=purpose,
                           prompt_version=prompt_version)
    if not config.GATEWAY_BASE_URL or not config.GATEWAY_API_KEY:
        raise LLMError("LG_GATEWAY_BASE_URL / LG_GATEWAY_API_KEY 未配置")

    url = f"{config.GATEWAY_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {config.GATEWAY_API_KEY}"}
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if seed is not None:
        payload["seed"] = seed

    t0 = time.time()
    last_err: Exception | None = None
    cur_tokens = max_tokens
    for attempt in range(config.MAX_RETRIES):
        try:
            payload_local = dict(payload)
            payload_local["max_tokens"] = cur_tokens
            with httpx.Client(timeout=config.HTTP_TIMEOUT_S) as cli:
                resp = cli.post(url, headers=headers, json=payload_local)
            if resp.status_code == 200:
                data = resp.json()
                choice = data["choices"][0]
                msg = choice.get("message") or {}
                content = msg.get("content") or ""
                finish = choice.get("finish_reason")
                usage = data.get("usage") or {}
                # 空容陷阱（P0）：推理预算烧完 → content 空；或某模型偶发 stop+空。
                # 未用完重试：加倍 max_tokens；最后一次仍空 → 记 failed 并抛错，
                # 绝不让空文本以 ok 身份落库（下游 judge 会拿空串当真样本评）。
                if not content.strip():
                    last_err = LLMError(f"empty content (finish={finish}, max_tokens={cur_tokens})")
                    if attempt < config.MAX_RETRIES - 1:
                        cur_tokens = min(cur_tokens * 2, 8192)
                        continue
                    r = ChatResult(text="", tokens_in=int(usage.get("prompt_tokens") or 0),
                                   tokens_out=int(usage.get("completion_tokens") or 0),
                                   latency_ms=int((time.time() - t0) * 1000),
                                   status="failed", error=str(last_err))
                    _record(purpose, model, prompt_version, r)
                    raise last_err
                r = ChatResult(
                    text=content,
                    tokens_in=int(usage.get("prompt_tokens") or 0),
                    tokens_out=int(usage.get("completion_tokens") or 0),
                    latency_ms=int((time.time() - t0) * 1000),
                )
                _record(purpose, model, prompt_version, r)
                return r
            if resp.status_code in {408, 409, 429, 500, 502, 503, 504} and attempt < config.MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            r = ChatResult(text="", tokens_in=0, tokens_out=0,
                           latency_ms=int((time.time() - t0) * 1000),
                           status="failed", error=f"HTTP {resp.status_code}: {resp.text[:200]}")
            _record(purpose, model, prompt_version, r)
            raise LLMError(r.error)
        except httpx.HTTPError as e:
            last_err = e
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
    r = ChatResult(text="", tokens_in=0, tokens_out=0,
                   latency_ms=int((time.time() - t0) * 1000),
                   status="failed", error=str(last_err))
    _record(purpose, model, prompt_version, r)
    raise LLMError(str(last_err))


# ── mock 模式 ───────────────────────────────────────────────

def _seed_from(*parts: str) -> int:
    h = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16)


_MOCK_RESPONSES = {
    "extract": '{"event":"两人对峙","intention":"试探","reader_effect":"紧张"}',
    "reconstruct": "他站着，没有立刻开口。屋外风扫过檐角，灯花轻轻跳了一下。半晌，他才把茶盏搁回去，缓缓道：\"这件事，先不提。\"",
    "adversarial_recon": "他站了一会，没有说话。\n他半天没吭声。\n他把话咽了回去。\n他迟迟不作声。",
    "judge": '{"verdict":"uncertain","score":0.5,"confidence":0.6,"abstain":false,"issues":[],"reasoning":"mock"}',
    "sem_residual": '{"missing":[],"added":[],"contradicted":[],"certainty_shift":0,"explicitness_delta":0,"subtext_preserved":0.5,"pov_consistent":true,"tags":[],"notes":"mock"}',
    "propositions": '{"propositions":[{"id":"P1","text":"发生了某事"},{"id":"P2","text":"某人有意图"}]}',
    # 实验引擎 mock 端到端要用：源校勘（source_integrity）的确定性伪输出
    "source_integrity": '{"src_ok": true, "defects": [], "severity": "low"}',
}


def _mock_chat(*, model: str, user: str, purpose: str,
               prompt_version: str, temperature: float, seed: int | None) -> ChatResult:
    h = _seed_from(model, purpose, user[:32], str(seed))
    if purpose.startswith("extract"):
        tpl = _MOCK_RESPONSES["extract"]
    elif "adversarial_recon" in purpose:
        tpl = _MOCK_RESPONSES["adversarial_recon"]
    elif purpose.startswith("reconstruct"):
        tpl = _MOCK_RESPONSES["reconstruct"]
    elif "judge" in purpose:
        tpl = _MOCK_RESPONSES["judge"]
    elif "sem_residual" in purpose:
        tpl = _MOCK_RESPONSES["sem_residual"]
    elif "propositions" in purpose:
        tpl = _MOCK_RESPONSES["propositions"]
    elif purpose.startswith("source_integrity"):
        tpl = _MOCK_RESPONSES["source_integrity"]
    else:
        tpl = f"mock:{purpose}"
    r = ChatResult(
        text=tpl,
        tokens_in=(len(user) // 4 + 10) + (h % 7),
        tokens_out=len(tpl) // 4 + (h % 11),
        latency_ms=5 + (h % 30),
    )
    _record(purpose, model, prompt_version, r)
    return r
