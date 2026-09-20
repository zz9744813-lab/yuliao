"""One dispatch per durable reservation, using the existing gateway configuration.

The calibration gateway retries internally and hides actual model / finish reason.
This pilot shares its endpoint and credentials but deliberately uses a one-shot
transport so budget and unknown-result semantics stay observable. No raw error log.
"""
from __future__ import annotations

import httpx

from .contracts import RuntimeFault, canonical


class OutcomeUnknown(RuntimeFault):
    pass


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
            return {"text": text, "requested_model": model, "actual_model": data.get("model"),
                    "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
                    "finish_reason": choice.get("finish_reason"), "provider_request_id": data.get("id")}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeFault("gateway_invalid_or_partial_result") from exc
