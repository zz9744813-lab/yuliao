"""K4/K5 效果验收的**可核签认链**（2026-09-30 派工 lg-k45-acceptance-chain）。

## 这件工具补的是哪一格

`F:/Hermes/scripts/effect_gate_snapshot.py` 的 `_k4_gate` / `_k5_gate` 尾部写死
FAIL，注释给的理由是「同目录任意 JSON 自称人工 PASS **没有身份/签认链**」。本件就
是把那条链补上：验收判词必须能一路回推到

    产物字节 → 逐臂正文哈希 → 评审输入哈希 → 证明网关签发（upstream_request_id +
    网关账本行） → append-only 调用收据 → 两席异模型判词 → 确定性重算的 decision

**自填文本不构成通过**：`verify_acceptance` 只读、逐条重算，任一环缺失或不符即
`(False, 原因原文)`，绝不静默放行。判据口径见 docs/K45_ACCEPTANCE_CHAIN.md。

## 与 K2 线的关系

派发装配（席位路由 + 每席一个进程内证明网关）**复用**
`scripts/k2_receipt_mint.seat_routes_from_env` / `start_gateways`，本件不复制也不
修改它们，更不修改 `tools/attestation_gateway.py`：网关是唯一签发
`x-lg-upstream-provider/-model/-channel-id/-request-id` 四个证明头并逐条记账之处，
本件只做「读产物 → 构造评审输入 → 派发 → 记收据 → 重算」。

## 硬边界

- 本件**不写任何库**（既不碰 `F:\\agi\\language-genome\\data`，也不碰
  `D:\\language-genome-data`），只写调用方显式给定的收据/artifact 路径；落在受保护
  只读根之下即拒（`k45_output_path_protected`）。
- 零真实模型调用是**测试侧**约束（合成上游 + 回环网关）；本件自身只按环境变量给出
  的核准路由发 HTTP 请求，不内置任何真上游默认值。
- 两席必须**不同 `model_identity`**：同模型两票在 `mint` 阶段就被拒
  （`k45_seats_model_not_distinct`），`verify` 再核一遍。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ARTIFACT_VERSION = "k45-acceptance/v1"
VERDICTS = ("ACCEPT", "BLOCK", "ABSTAIN")
REQUIRED_SEATS = 2
HEADER_PREFIX = "x-lg-upstream-"
ATTESTATION_HEADERS = (HEADER_PREFIX + "provider", HEADER_PREFIX + "model",
                       HEADER_PREFIX + "channel-id", HEADER_PREFIX + "request-id")

# 评审输入的五键契约（任务书口径）：改任何一键都会改 input_sha256。
REVIEW_INPUT_KEYS = ("scene", "arm", "prose_sha256", "rubric", "receipt_sha256")
# 调用收据行的必备字段（JSONL 每行）。
LEDGER_FIELDS = ("seat", "provider", "model_id", "model_identity",
                 "upstream_request_id", "input_sha256", "response_sha256",
                 "receipt_sha256", "scene", "arm", "prose_sha256",
                 "verdict", "reason")

SYSTEM_PROMPT = (
    "你是 K4/K5 正文质量验收席，只依据给定 rubric 评审单臂正文。"
    "只输出一个 JSON 对象，键必须是 verdict 与 reason："
    "verdict 只能取 ACCEPT / BLOCK / ABSTAIN 之一；reason 为简短中文依据。"
    "正文哈希与 receipt_sha256 由外部链上核验，你无法也不得据自填内容签发。")

PROTECTED_ROOTS_ENV = "LG_K45_PROTECTED_ROOTS"
DEFAULT_PROTECTED_ROOTS = (r"F:\agi\language-genome\data",
                           r"D:\language-genome-data")


class AcceptanceMintError(RuntimeError):
    """派发前/派发中即拒：不产出 artifact，绝不把失败写成通过。"""


class _Rejected(Exception):
    """verify 内部的拒绝信号，统一被 `verify_acceptance` 转成 (False, 原文)。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    """确定性序列化：排序键 + 紧凑分隔符 + UTF-8，重算哈希的唯一口径。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


# ── 产物（K4/K5 收据 JSON）读取 ──────────────────────────────────────────────
def read_receipt(receipt_path: str | Path) -> tuple[bytes, dict]:
    """返回 (原始字节, 解析后的 JSON)。字节原样保留：`receipt_sha256` 就是它的哈希。"""
    path = Path(receipt_path)
    if not path.is_file():
        raise AcceptanceMintError(f"k45_receipt_missing:{path}")
    raw = path.read_bytes()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AcceptanceMintError(
            f"k45_receipt_not_json:{path}:{type(exc).__name__}") from exc
    if not isinstance(parsed, dict):
        raise AcceptanceMintError(f"k45_receipt_not_object:{path}")
    return raw, parsed


def committed_arms(receipt: dict) -> list[dict]:
    """产物里全部 `status=="committed"` 的臂：{scene, arm, prose_sha256}。

    同 (scene, arm) 出现两行即拒——验收链不接受"同一臂两份正文，任选一份签"。
    """
    prose = ((receipt.get("artifacts") or {}).get("prose")) or []
    if not isinstance(prose, list):
        raise AcceptanceMintError("k45_prose_not_list")
    arms: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in prose:
        if not isinstance(item, dict) or item.get("status") != "committed":
            continue
        scene, arm, text = item.get("scene"), item.get("arm"), item.get("text")
        if not isinstance(scene, str) or not isinstance(arm, str):
            raise AcceptanceMintError("k45_arm_identity_missing")
        if not isinstance(text, str) or not text.strip():
            raise AcceptanceMintError(
                f"k45_arm_text_missing:{scene}/{arm}")
        key = (scene, arm)
        if key in seen:
            raise AcceptanceMintError(f"k45_arm_duplicated:{scene}/{arm}")
        seen.add(key)
        arms.append({"scene": scene, "arm": arm, "prose_sha256": _sha_text(text)})
    if not arms:
        raise AcceptanceMintError("k45_no_committed_arms")
    return arms


def review_input_for(arm: dict, rubric: str, receipt_sha256: str) -> dict:
    return {"scene": arm["scene"], "arm": arm["arm"],
            "prose_sha256": arm["prose_sha256"], "rubric": rubric,
            "receipt_sha256": receipt_sha256}


def request_bytes_for(review_input: dict, requested_model: str) -> bytes:
    """完全确定的请求体（无 uuid/时间戳）：verify 侧才能原样重算 input_sha256。"""
    return canonical_bytes({
        "messages": [{"content": SYSTEM_PROMPT, "role": "system"},
                     {"content": canonical_bytes(review_input).decode("utf-8"),
                      "role": "user"}],
        "model": requested_model, "stream": False, "temperature": 0})


# ── 判词/decision ────────────────────────────────────────────────────────────
def recompute_decision(verdicts: list[str]) -> str:
    """两席判词 → decision 的确定性规则（任务书口径）。

    任一 BLOCK ⇒ BLOCK；两席均 ACCEPT ⇒ ACCEPT；含 ABSTAIN 且无 BLOCK ⇒ ABSTAIN。
    空判词集不是 ACCEPT，是 ABSTAIN（没证据就没结论）。
    """
    if any(v == "BLOCK" for v in verdicts):
        return "BLOCK"
    if verdicts and all(v == "ACCEPT" for v in verdicts):
        return "ACCEPT"
    return "ABSTAIN"


def _parse_verdict(body: bytes) -> tuple[str, str]:
    """上游正文 → (verdict, reason)。解析不出来 ⇒ **ABSTAIN**（fail-closed，绝不判 ACCEPT）。"""
    try:
        parsed = json.loads(body.decode("utf-8"))
        content = parsed["choices"][0]["message"]["content"]
        review = json.loads(content) if isinstance(content, str) else content
        verdict, reason = review["verdict"], review["reason"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return ("ABSTAIN",
                f"malformed_review_response:{type(exc).__name__}")
    if verdict not in VERDICTS:
        return ("ABSTAIN", f"verdict_not_allowed:{verdict!r}")
    if not isinstance(reason, str) or not reason.strip():
        return ("ABSTAIN", "reason_missing")
    return (verdict, reason)


# ── 输出路径守卫 ─────────────────────────────────────────────────────────────
def protected_roots(environ: dict | None = None) -> list[Path]:
    env = os.environ if environ is None else environ
    raw = (env.get(PROTECTED_ROOTS_ENV) or "").strip()
    items = [p for p in raw.replace("\\", "/").split(";") if p.strip()] if raw else [
        p.replace("\\", "/") for p in DEFAULT_PROTECTED_ROOTS]
    out = []
    for item in items:
        try:
            out.append(Path(item).resolve())
        except OSError:
            continue
    return out


def _guard_output_path(path: Path, environ: dict | None = None) -> Path:
    resolved = Path(path).expanduser().resolve()
    for root in protected_roots(environ):
        if resolved == root or root in resolved.parents:
            raise AcceptanceMintError(
                f"k45_output_path_protected:{resolved}<{root}")
    return resolved


def _default_output_paths(receipt_path: Path, suffix: str) -> Path:
    return receipt_path.resolve().parent / (receipt_path.resolve().stem + suffix)


# ── 席位校验 ─────────────────────────────────────────────────────────────────
def _seat_identities(seats: list) -> list[dict]:
    if not isinstance(seats, (list, tuple)) or len(seats) != REQUIRED_SEATS:
        raise AcceptanceMintError(
            f"k45_seats_require_{REQUIRED_SEATS}:got={len(seats) if isinstance(seats, (list, tuple)) else 'non-sequence'}")
    entries = []
    for seat in seats:
        route = getattr(seat, "route", None)
        for attr in ("name", "base_url", "api_key", "audit_path"):
            if not str(getattr(seat, attr, "") or "").strip():
                raise AcceptanceMintError(f"k45_seat_field_missing:{attr}")
        if route is None:
            raise AcceptanceMintError("k45_seat_route_missing")
        provider = str(getattr(route, "upstream_provider", "") or "").strip()
        model_id = str(getattr(route, "upstream_model", "") or "").strip()
        requested = str(getattr(route, "requested_model", "") or "").strip()
        if not provider or not model_id or not requested:
            raise AcceptanceMintError(
                f"k45_seat_identity_incomplete:{getattr(seat, 'name', '?')}")
        entries.append({"seat": seat.name, "provider": provider,
                        "model_id": model_id,
                        "model_identity": f"{provider}/{model_id}",
                        "requested_model": requested,
                        "gateway_base_url": seat.base_url,
                        "audit_path": str(Path(seat.audit_path).resolve())})
    identities = [e["model_identity"].casefold() for e in entries]
    if len(set(identities)) != len(identities):
        raise AcceptanceMintError("k45_seats_model_not_distinct:" + ",".join(identities))
    return entries


# ── 派发 ─────────────────────────────────────────────────────────────────────
def _dispatch(seat: dict, api_key: str, payload: bytes,
              timeout_seconds: float) -> dict:
    """向该席网关发一次评审请求，校验四个证明头齐且与核准路由一致。

    网关拒发（502/零证明头）或证明头与核准路由不符 ⇒ 抛错：没有签发就没有收据行。
    `api_key` 只进请求头，绝不落收据、绝不进 artifact。
    """
    url = seat["gateway_base_url"].rstrip("/") + "/chat/completions"
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
            response = client.post(url, content=payload, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept-Encoding": "identity"})
    except Exception as exc:  # 只记异常类名：消息里可能带 URL/凭据片段
        raise AcceptanceMintError(
            f"k45_dispatch_failed:{seat['seat']}:{type(exc).__name__}") from exc
    headers = {k.lower(): v for k, v in response.headers.items()}
    missing = [name for name in ATTESTATION_HEADERS
               if not (headers.get(name) or "").strip()]
    if response.status_code != 200:
        raise AcceptanceMintError(
            f"k45_gateway_denied:{seat['seat']}:http_{response.status_code}")
    if missing:
        raise AcceptanceMintError(
            f"k45_attestation_headers_missing:{seat['seat']}:" + ",".join(missing))
    if headers[HEADER_PREFIX + "provider"] != seat["provider"]:
        raise AcceptanceMintError(
            f"k45_attestation_provider_mismatch:{seat['seat']}")
    if headers[HEADER_PREFIX + "model"] != seat["model_id"]:
        raise AcceptanceMintError(
            f"k45_attestation_model_mismatch:{seat['seat']}")
    request_id = headers[HEADER_PREFIX + "request-id"].strip()
    if not request_id:
        raise AcceptanceMintError(f"k45_upstream_request_id_empty:{seat['seat']}")
    return {"upstream_request_id": request_id,
            "provider": headers[HEADER_PREFIX + "provider"].strip(),
            "model_id": headers[HEADER_PREFIX + "model"].strip(),
            "channel_id": headers[HEADER_PREFIX + "channel-id"].strip(),
            "body": response.content}


def mint_acceptance(receipt_path: str | Path, rubric: str, seats: list, *,
                    timeout_seconds: float, artifact_path: str | Path | None = None,
                    call_receipt_path: str | Path | None = None,
                    environ: dict | None = None) -> dict:
    """逐臂 × 两席派发独立评审，追加调用收据，产出 acceptance artifact（并返回它）。

    任一 (臂, 席) 没拿到「网关签发 + 证明头齐」的响应 ⇒ 整体拒绝，不落 artifact：
    半截收据不许冒充一次验收。收据是 append-only，逐条即时落盘，失败也留痕。
    """
    if not isinstance(rubric, str) or not rubric.strip():
        raise AcceptanceMintError("k45_rubric_missing")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise AcceptanceMintError("k45_timeout_seconds_invalid")
    receipt = Path(receipt_path)
    raw, parsed = read_receipt(receipt)
    receipt_sha256 = _sha_bytes(raw)
    arms = committed_arms(parsed)
    entries = _seat_identities(list(seats))

    artifact_target = _guard_output_path(
        Path(artifact_path) if artifact_path
        else _default_output_paths(receipt, ".k45_acceptance.json"), environ)
    ledger_target = _guard_output_path(
        Path(call_receipt_path) if call_receipt_path
        else _default_output_paths(receipt, ".k45_calls.jsonl"), environ)
    for target in (artifact_target, ledger_target):
        target.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for arm in arms:
        review_input = review_input_for(arm, rubric, receipt_sha256)
        for seat, source in zip(entries, seats):
            payload = request_bytes_for(review_input, seat["requested_model"])
            outcome = _dispatch(seat, str(source.api_key), payload,
                                float(timeout_seconds))
            verdict, reason = _parse_verdict(outcome["body"])
            rows.append({"at": _now(), "seat": seat["seat"],
                         "provider": outcome["provider"],
                         "model_id": outcome["model_id"],
                         "model_identity": f"{outcome['provider']}/{outcome['model_id']}",
                         "upstream_request_id": outcome["upstream_request_id"],
                         "input_sha256": _sha_bytes(payload),
                         "response_sha256": _sha_bytes(outcome["body"]),
                         "receipt_sha256": receipt_sha256,
                         "scene": arm["scene"], "arm": arm["arm"],
                         "prose_sha256": arm["prose_sha256"],
                         "verdict": verdict, "reason": reason})
            _append_ledger(ledger_target, rows[-1])

    artifact = {
        "artifact_version": ARTIFACT_VERSION,
        "created_at": _now(),
        "receipt_path": str(receipt.resolve()),
        "receipt_sha256": receipt_sha256,
        "rubric": rubric,
        "seats": entries,
        "call_receipt_path": str(ledger_target),
        "n_arms": len(arms),
        "arms": [{"scene": arm["scene"], "arm": arm["arm"],
                  "prose_sha256": arm["prose_sha256"],
                  "votes": [dict(r) for r in rows
                            if (r["scene"], r["arm"]) == (arm["scene"], arm["arm"])]}
                 for arm in arms],
        "decision": recompute_decision([r["verdict"] for r in rows]),
    }
    artifact_target.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    artifact["artifact_path"] = str(artifact_target)
    return artifact


def _append_ledger(path: Path, row: dict) -> None:
    """append-only 追加一行（O_APPEND + flush），不读不重写历史行。"""
    line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


# ── 只读核验 ─────────────────────────────────────────────────────────────────
def _read_json_object(path: Path, code: str) -> dict:
    try:
        parsed = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError as exc:
        raise _Rejected(f"{code}:missing:{path}") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise _Rejected(f"{code}:not_json:{path}:{type(exc).__name__}") from exc
    if not isinstance(parsed, dict):
        raise _Rejected(f"{code}:not_object:{path}")
    return parsed


def _read_ledger(path: Path) -> list[dict]:
    if not path.is_file():
        raise _Rejected(f"k45_call_receipt_missing:{path}")
    rows = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError as exc:
            raise _Rejected(
                f"k45_call_receipt_unparseable:{path}#L{number}:{type(exc).__name__}") from exc
        if not isinstance(row, dict):
            raise _Rejected(f"k45_call_receipt_not_object:{path}#L{number}")
        missing = [key for key in LEDGER_FIELDS if key not in row]
        if missing:
            raise _Rejected(
                f"k45_call_receipt_field_missing:{path}#L{number}:" + ",".join(missing))
        rows.append(row)
    return rows


def _read_audit(path: Path) -> list[dict]:
    """证明网关账本（签发侧唯一权威记录）。缺文件即拒——没有签发就没有签认。"""
    if not path.is_file():
        raise _Rejected(f"k45_gateway_audit_missing:{path}")
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue  # 硬 kill 残留半行：跳过，不参与匹配
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _same_path(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False


def verify_acceptance(receipt_path: str | Path, artifact_path: str | Path, *,
                      receipt_path_expected: str | Path | None = None
                      ) -> tuple[bool, str]:
    """只读核验：全部重算，任一不符即 `(False, 原因原文)`。检查顺序见文档 §4。"""
    try:
        raw = Path(receipt_path).read_bytes()
    except OSError as exc:
        return (False, f"k45_receipt_unreadable:{type(exc).__name__}")
    return _verify(receipt_path, raw, artifact_path,
                   receipt_path_expected=receipt_path_expected)


def _verify(receipt_path: str | Path, raw: bytes, artifact_path: str | Path, *,
            receipt_path_expected: str | Path | None) -> tuple[bool, str]:
    try:
        artifact = _read_json_object(Path(artifact_path), "k45_artifact")
        _check_shape(artifact)
        current_receipt = _sha_bytes(raw)
        _check_paths(artifact, receipt_path, receipt_path_expected)
        if artifact["receipt_sha256"] != current_receipt:
            raise _Rejected(
                "k45_receipt_sha_mismatch:artifact 记 "
                f"{artifact['receipt_sha256'][:16]}…，当前产物字节哈希 "
                f"{current_receipt[:16]}…（产物已变或收据倒签）")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _Rejected(f"k45_receipt_not_json:{type(exc).__name__}") from exc
        if not isinstance(parsed, dict):
            raise _Rejected("k45_receipt_not_object")
        arms = committed_arms(parsed)
        _check_arms(artifact, arms)

        ledger = _read_ledger(Path(artifact["call_receipt_path"]))
        seats = {s["seat"]: s for s in artifact["seats"]}
        audits = {name: _read_audit(Path(seat["audit_path"]))
                  for name, seat in seats.items()}
        used_seqs: set[Any] = set()
        verdicts: list[str] = []
        for arm in artifact["arms"]:
            votes = arm["votes"]
            _check_two_distinct_seats(arm, votes, seats)
            for vote in votes:
                row = _match_ledger(ledger, arm, vote)
                _check_vote_matches_row(arm, vote, row)
                _check_row_self_consistent(row, seats[vote["seat"]],
                                           artifact["rubric"],
                                           artifact["receipt_sha256"], arm)
                _check_audit_row(audits[vote["seat"]], row, vote["seat"], arm,
                                 used_seqs)
                verdicts.append(row["verdict"])

        recomputed = recompute_decision(verdicts)
        if artifact["decision"] != recomputed:
            raise _Rejected(
                f"k45_decision_mismatch:artifact 自填 {artifact['decision']}，"
                f"两席判词重算为 {recomputed}（判词 {verdicts}）——自填文本不构成通过")
        return (True, f"OK({ARTIFACT_VERSION}) {len(arms)} 臂 × {len(seats)} 席 = "
                      f"{len(verdicts)} 票，decision={recomputed}，"
                      "全部哈希/证明头/网关账本/两席异模型逐条重算相符")
    except _Rejected as exc:
        return (False, str(exc))
    except Exception as exc:  # 核验器自身出错也必须 fail-closed，绝不返回 True
        return (False, f"k45_verifier_error:{type(exc).__name__}:{exc}")


def _check_shape(artifact: dict) -> None:
    required = ("artifact_version", "created_at", "receipt_path", "receipt_sha256",
                "rubric", "seats", "call_receipt_path", "arms", "decision")
    missing = [key for key in required if key not in artifact]
    if missing:
        raise _Rejected("k45_artifact_field_missing:" + ",".join(missing))
    if artifact["artifact_version"] != ARTIFACT_VERSION:
        raise _Rejected(
            f"k45_artifact_version_unsupported:{artifact['artifact_version']}")
    if not isinstance(artifact["arms"], list) or not artifact["arms"]:
        raise _Rejected("k45_artifact_arms_empty")
    if not isinstance(artifact["seats"], list) or len(artifact["seats"]) != REQUIRED_SEATS:
        raise _Rejected(f"k45_artifact_seats_require_{REQUIRED_SEATS}")
    for seat in artifact["seats"]:
        for key in ("seat", "provider", "model_id", "model_identity",
                    "requested_model", "gateway_base_url", "audit_path"):
            if not str(seat.get(key) or "").strip():
                raise _Rejected(f"k45_artifact_seat_field_missing:{key}")
    if not str(artifact["rubric"] or "").strip():
        raise _Rejected("k45_artifact_rubric_empty")


def _check_paths(artifact: dict, receipt_path: str | Path,
                 receipt_path_expected: str | Path | None) -> None:
    """当前核的文件必须是调用方指定的那一个；未指定时必须是 artifact 记录的那个。"""
    if receipt_path_expected is not None:
        if not _same_path(receipt_path, receipt_path_expected):
            raise _Rejected(
                f"k45_receipt_path_unexpected:核的是 {Path(receipt_path).resolve()}，"
                f"要求的是 {Path(receipt_path_expected).resolve()}")
        return
    if not _same_path(receipt_path, artifact["receipt_path"]):
        raise _Rejected(
            f"k45_receipt_path_mismatch:artifact 记录 {artifact['receipt_path']}，"
            f"当前核 {Path(receipt_path).resolve()}"
            "（换文件需显式传 receipt_path_expected）")


def _check_arms(artifact: dict, arms: list[dict]) -> None:
    current = {(a["scene"], a["arm"]): a["prose_sha256"] for a in arms}
    signed = {(a["scene"], a["arm"]): a for a in artifact["arms"]}
    absent = sorted(f"{s}/{a}" for s, a in current if (s, a) not in signed)
    if absent:
        raise _Rejected("k45_arms_missing:产物里 committed 的臂未全部签认 -> "
                        + ",".join(absent))
    extra = sorted(f"{s}/{a}" for s, a in signed if (s, a) not in current)
    if extra:
        raise _Rejected("k45_arms_extra:签认链里有当前产物不存在的臂 -> "
                        + ",".join(extra))
    for key, arm in signed.items():
        if arm["prose_sha256"] != current[key]:
            raise _Rejected(
                f"k45_prose_sha_mismatch:{key[0]}/{key[1]} 正文哈希与当前产物不符"
                f"（artifact {arm['prose_sha256'][:16]}… vs 产物 {current[key][:16]}…）")


def _check_two_distinct_seats(arm: dict, votes: list, seats: dict) -> None:
    if not isinstance(votes, list) or len(votes) != REQUIRED_SEATS:
        raise _Rejected(
            f"k45_arm_votes_require_{REQUIRED_SEATS}:{arm['scene']}/{arm['arm']}"
            f":got={len(votes) if isinstance(votes, list) else 'non-list'}")
    identities = [str(v.get("model_identity") or "").casefold() for v in votes]
    if any(not i for i in identities):
        raise _Rejected(f"k45_vote_model_identity_empty:{arm['scene']}/{arm['arm']}")
    if len(set(identities)) != len(identities):
        raise _Rejected(
            f"k45_vote_model_identity_not_distinct:{arm['scene']}/{arm['arm']}"
            f" 两席同模型 {identities[0]} 不构成独立签认")
    names = [v.get("seat") for v in votes]
    if len(set(names)) != len(names) or set(names) != set(seats):
        raise _Rejected(
            f"k45_vote_seat_set_mismatch:{arm['scene']}/{arm['arm']}:{names}")


def _match_ledger(ledger: list[dict], arm: dict, vote: dict) -> dict:
    matches = [row for row in ledger
               if (row.get("scene"), row.get("arm"), row.get("seat"))
               == (arm["scene"], arm["arm"], vote.get("seat"))]
    if not matches:
        raise _Rejected(
            f"k45_call_receipt_row_missing:{arm['scene']}/{arm['arm']}"
            f"/{vote.get('seat')} 在 append-only 调用收据里没有对应行")
    if len(matches) > 1:
        raise _Rejected(
            f"k45_call_receipt_row_duplicated:{arm['scene']}/{arm['arm']}"
            f"/{vote.get('seat')} 共 {len(matches)} 行（重放/覆盖）")
    return matches[0]


def _check_vote_matches_row(arm: dict, vote: dict, row: dict) -> None:
    differ = [key for key in LEDGER_FIELDS
              if key in vote and vote.get(key) != row.get(key)]
    if differ:
        raise _Rejected(
            f"k45_vote_ledger_mismatch:{arm['scene']}/{arm['arm']}/{row['seat']}"
            " artifact 判词与调用收据不符 -> " + ",".join(differ))


def _check_row_self_consistent(row: dict, seat: dict, rubric: str,
                               receipt_sha256: str, arm: dict) -> None:
    if row["provider"] != seat["provider"] or row["model_id"] != seat["model_id"]:
        raise _Rejected(
            f"k45_seat_identity_mismatch:{row['seat']}:收据 {row['provider']}/"
            f"{row['model_id']} vs artifact {seat['provider']}/{seat['model_id']}")
    if row["model_identity"] != f"{row['provider']}/{row['model_id']}":
        raise _Rejected(f"k45_model_identity_mismatch:{row['seat']}")
    if not str(row["upstream_request_id"] or "").strip():
        raise _Rejected(
            f"k45_upstream_request_id_empty:{row['scene']}/{row['arm']}/{row['seat']}"
            " —— 没有上游请求号就没有可核的签认")
    if row["verdict"] not in VERDICTS:
        raise _Rejected(f"k45_verdict_not_allowed:{row['seat']}:{row['verdict']!r}")
    if not str(row["reason"] or "").strip():
        raise _Rejected(f"k45_reason_empty:{row['seat']}")
    if row["prose_sha256"] != arm["prose_sha256"]:
        raise _Rejected(
            f"k45_prose_sha_mismatch:{row['scene']}/{row['arm']}:调用收据绑的正文哈希 "
            f"{row['prose_sha256'][:16]}… 与产物 {arm['prose_sha256'][:16]}… 不符")
    if row["receipt_sha256"] != receipt_sha256:
        raise _Rejected(
            f"k45_call_receipt_sha_mismatch:{row['seat']}:调用收据绑的产物哈希与 "
            "artifact/当前产物不一致")
    expected_input = _sha_bytes(request_bytes_for(
        review_input_for({"scene": row["scene"], "arm": row["arm"],
                          "prose_sha256": row["prose_sha256"]}, rubric,
                         receipt_sha256), seat["requested_model"]))
    if row["input_sha256"] != expected_input:
        raise _Rejected(
            f"k45_input_sha_mismatch:{row['scene']}/{row['arm']}/{row['seat']}"
            f":收据记 {str(row['input_sha256'])[:16]}…，按评审输入五键重算为 "
            f"{expected_input[:16]}…")


def _check_audit_row(audit_rows: list[dict], row: dict, name: str,
                     arm: dict, used_seqs: set) -> None:
    """调用收据行必须能在**该席证明网关账本**里找到同一次签发（三方对账）。"""
    hits = [ar for ar in audit_rows
            if ar.get("decision") == "issued"
            and ar.get("request_sha256") == row["input_sha256"]
            and ar.get("response_sha256") == row["response_sha256"]
            and ar.get("upstream_id") == row["upstream_request_id"]
            and isinstance(ar.get("route"), dict)
            and ar["route"].get("provider") == row["provider"]
            and ar["route"].get("model") == row["model_id"]]
    if not hits:
        raise _Rejected(
            f"k45_gateway_audit_no_issuance:{name}:{arm['scene']}/{arm['arm']} "
            f"网关账本里没有 (input_sha256={str(row['input_sha256'])[:16]}…, "
            f"response_sha256={str(row['response_sha256'])[:16]}…, "
            f"upstream_request_id={row['upstream_request_id']}) 的签发记录"
            " —— 调用收据与网关不可对账")
    seqs = [h.get("seq") for h in hits]
    if row["input_sha256"] in used_seqs:
        raise _Rejected(f"k45_gateway_audit_replay:{name} 同一签发被复用到多票")
    if any(s is None for s in seqs):
        raise _Rejected(f"k45_gateway_audit_seq_missing:{name}")
    used_seqs.add(row["input_sha256"])
