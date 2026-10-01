"""One dispatch per durable reservation, using the existing gateway configuration.

The calibration gateway (litellm) may silently serve a different upstream model
than requested; a read-only scan of live-world ledgers measured 42/491 calls
(8.6%) served under a different model. This client therefore records BOTH
requested_model and actual_model per call and flags substitution truthfully
on every reply (never silent). Enforcement of the fail-closed default lives
in the live channel (SceneRunner._call), not in invoke: direct callers keep
the record-only semantics pinned by tests/test_scene_runtime.py. Downgrade to
record-only on the live channel requires LG_ALLOW_MODEL_SUBSTITUTION=1.
No raw error log.
"""
from __future__ import annotations

import os

import httpx

from .contracts import RuntimeFault, canonical


class OutcomeUnknown(RuntimeFault):
    pass


class ModelIdentityMismatch(RuntimeFault):
    """Requested model was not the model actually served. Carries the parsed
    reply so the durable call ledger records the truth even on failure."""

    def __init__(self, message: str, reply: dict):
        super().__init__(message)
        self.reply = reply


def model_substitution_allowed() -> bool:
    """Call-time env read so operators/tests can toggle without re-import."""
    return os.environ.get("LG_ALLOW_MODEL_SUBSTITUTION", "").strip().lower() \
        in {"1", "true", "yes", "on"}


class GatewayClient:
    def __init__(self, writer_model: str, verifier_model: str, *, transport=None):
        from app import config
        if config.LLM_MODE != "real":
            raise RuntimeFault("live_client_requires_real_mode")
        if not config.GATEWAY_BASE_URL or not config.GATEWAY_API_KEY:
            raise RuntimeFault("gateway_not_configured")
        if any(m.startswith(("agy/", "qoder/", "wb/", "zcode/")) for m in (writer_model, verifier_model)):
            raise RuntimeFault("pilot_requires_http_route_with_output_limit")
        self.models = {"writer": writer_model, "verifier": verifier_model, "transport": "gateway-once/1"}
        self.base = config.GATEWAY_BASE_URL
        self.key = config.GATEWAY_API_KEY
        self.transport = transport

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        model = self.models[role]
        request = {"model": model, "messages": [{"role": "system", "content": system},
                   {"role": "user", "content": canonical(payload)}],
                   "max_tokens": max_tokens, "temperature": 0.65 if role == "writer" else 0.0}
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(20.0, timeout)), transport=self.transport) as client:
                response = client.post(self.base.rstrip("/") + "/chat/completions",
                                       headers={"Authorization": f"Bearer {self.key}"}, json=request)
        except httpx.HTTPError as exc:
            # The server may already have executed/billed. No automatic fallback.
            raise OutcomeUnknown("gateway_transport_outcome_unknown") from exc
        if response.status_code != 200:
            raise RuntimeFault(f"gateway_http_{response.status_code}")
        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"]["content"]
            usage = data.get("usage") or {}
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty content")
            if choice.get("finish_reason") != "stop":
                raise ValueError("incomplete response")
            served = data.get("model")
            actual = served if isinstance(served, str) and served.strip() else None
            substituted = actual != model
            reply = {"text": text, "requested_model": model, "actual_model": actual,
                     "model_identity_ok": not substituted,
                     "substituted": substituted,
                     "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
                     "finish_reason": choice.get("finish_reason"), "provider_request_id": data.get("id")}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeFault("gateway_invalid_or_partial_result") from exc
        return reply
