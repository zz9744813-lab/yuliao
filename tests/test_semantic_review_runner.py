"""Synthetic HTTP receipts; these are not production K2 reviewer votes."""
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL

from app import config
from app.models import ExpressionStrategyV2
from app.semantic_receipts import ensure_semantic_schema
from app.semantic_review import SnapshotError
from app.semantic_review_store import freeze_snapshot
import app.semantic_review_runner as runner
from test_semantic_review_snapshot import CLAIM, _seed

ROUTE = runner.ReviewRoute("requested-a", "provider-a", "actual-a", "channel-7")


def _ready(monkeypatch, tmp_path, *, apply_claim_to_card=True):
    engine, session = _seed(db_path=tmp_path / "synthetic.db")
    if apply_claim_to_card:
        card = session.get(ExpressionStrategyV2, "ESV2-S")
        card.scope = CLAIM["scope_to"]
        card.scope_ids = CLAIM["scope_ids"]
        card.scope_basis = CLAIM["scope_basis"]
        session.commit()
    session.close()
    ensure_semantic_schema(engine)
    snapshot = freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
    monkeypatch.setattr(config, "LLM_MODE", "real")
    monkeypatch.setattr(config, "GATEWAY_BASE_URL", "https://synthetic.example/v1")
    monkeypatch.setattr(config, "GATEWAY_API_KEY", "synthetic-key")
    monkeypatch.setattr(runner, "_require_private_storage", lambda database: None)
    return engine, snapshot["snapshot_id"]


def _response(*, verdict="PASS", model="actual-a", headers=True,
              request_id="upstream-1", finish="stop", text_override=None):
    review = {"verdict": verdict, "reason": "逐条核过本卡实例及反例",
              "cited_instance_ids": ["SI-A"],
              "concerns": ["反例不支持该范围"] if verdict == "BLOCK" else []}
    body = {"id": request_id, "model": model,
            "choices": [{"finish_reason": finish,
                         "message": {"role": "assistant", "content":
                                     text_override if text_override is not None
                                     else json.dumps(review, ensure_ascii=False)}}]}
    attestation = ({"X-LG-Upstream-Provider": "provider-a",
                    "X-LG-Upstream-Model": "actual-a",
                    "X-LG-Upstream-Channel-Id": "channel-7",
                    "X-LG-Upstream-Request-Id": request_id} if headers else {})
    return httpx.Response(200, headers=attestation, json=body)


def _run(engine, snapshot_id, tmp_path, **overrides):
    params = {"max_output_tokens": 512, "max_request_bytes": 100_000,
              "timeout_seconds": 15}
    params.update(overrides)
    return runner.review_snapshot(engine, snapshot_id, ROUTE, **params)


def _counts(engine):
    with engine.connect() as con:
        return tuple(con.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()
                     for table in ("semantic_review_calls", "semantic_review_votes",
                                   "semantic_approval_links"))


def test_one_shot_attested_review_writes_matching_call_and_vote(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)
    sent = []
    response = _response()
    monkeypatch.setattr(runner, "_post_once",
                        lambda raw, timeout: sent.append(raw) or response)
    try:
        result = _run(engine, sid, tmp_path)
        assert result["verdict"] == "PASS"
        assert len(sent) == 1
        assert _counts(engine) == (1, 1, 0)
        with engine.connect() as con:
            row = con.exec_driver_sql(
                "SELECT provider,model_id,upstream_request_id,prompt_sha256,"
                "response_sha256,response_text,request_json FROM semantic_review_calls"
            ).one()
            assert row[0:3] == ("provider-a", "actual-a", "upstream-1")
            assert row.prompt_sha256 == hashlib.sha256(
                sent[0].encode("utf-8")).hexdigest()
            assert row.response_sha256 == hashlib.sha256(
                row.response_text.encode("utf-8")).hexdigest()
            assert row.request_json == sent[0]
            envelope = json.loads(con.exec_driver_sql(
                "SELECT response_json FROM semantic_review_calls").scalar())
            assert envelope["prompt_version"] == runner.PROMPT_VERSION
            assert envelope["pinned_route"] == {
                "provider": "provider-a", "model": "actual-a",
                "channel_id": "channel-7", "requested_model": "requested-a"}
        attempts = tmp_path / "semantic_review_attempts"
        assert len(list(attempts.glob("*.reserved.json"))) == 1
        assert len(list(attempts.glob("*.committed.json"))) == 1
        received = json.loads(next(attempts.glob("*.received.json")).read_text(
            encoding="utf-8"))
        assert "response_text" not in received
        assert "request_json" not in received
        with pytest.raises(runner.ReviewPreflightError,
                           match="k2_model_already_voted"):
            _run(engine, sid, tmp_path)
        assert len(sent) == 1
    finally:
        engine.dispose()


def test_proposed_scope_is_reviewed_before_card_scope_changes(monkeypatch,
                                                               tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path, apply_claim_to_card=False)
    monkeypatch.setattr(runner, "_post_once",
                        lambda raw, timeout: _response())
    try:
        assert _run(engine, sid, tmp_path)["verdict"] == "PASS"
        with engine.connect() as con:
            assert con.exec_driver_sql(
                "SELECT scope FROM expression_strategies_v2"
            ).scalar() == "UNCERTAIN"
    finally:
        engine.dispose()


@pytest.mark.parametrize("response", [
    _response(headers=False),
    _response(model="alias-b"),
    _response(finish="length"),
    _response(text_override='{"verdict":"PASS"}'),
])
def test_unverifiable_completion_never_writes_vote(monkeypatch, tmp_path, response):
    engine, sid = _ready(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(runner, "_post_once",
                        lambda raw, timeout: sent.append(raw) or response)
    try:
        with pytest.raises(runner.ReviewResponseError,
                           match="k2_response_unverifiable"):
            _run(engine, sid, tmp_path)
        assert len(sent) == 1
        assert _counts(engine) == (0, 0, 0)
        assert len(list((tmp_path / "semantic_review_attempts").glob(
            "*.uncommitted.json"))) == 1
    finally:
        engine.dispose()


def test_budget_refusal_is_before_dispatch(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_post_once",
                        lambda *args: pytest.fail("must not dispatch"))
    try:
        with pytest.raises(runner.ReviewPreflightError,
                           match="k2_request_exceeds_budget"):
            _run(engine, sid, tmp_path, max_request_bytes=100)
        assert _counts(engine) == (0, 0, 0)
        assert not (tmp_path / "semantic_review_attempts").exists()
    finally:
        engine.dispose()


def test_remote_plain_http_refuses_before_dispatch(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "GATEWAY_BASE_URL", "http://remote.example/v1")
    monkeypatch.setattr(runner, "_post_once",
                        lambda *args: pytest.fail("must not dispatch"))
    try:
        with pytest.raises(runner.ReviewPreflightError,
                           match="k2_gateway_origin_untrusted"):
            _run(engine, sid, tmp_path)
        assert _counts(engine) == (0, 0, 0)
        assert not (tmp_path / "semantic_review_attempts").exists()
    finally:
        engine.dispose()


def test_untrusted_storage_refuses_before_dispatch(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)

    def untrusted(database):
        raise runner.ReviewPreflightError("k2_storage_acl_untrusted")

    monkeypatch.setattr(runner, "_require_private_storage", untrusted)
    monkeypatch.setattr(runner, "_post_once",
                        lambda *args: pytest.fail("must not dispatch"))
    try:
        with pytest.raises(runner.ReviewPreflightError,
                           match="k2_storage_acl_untrusted"):
            _run(engine, sid, tmp_path)
        assert _counts(engine) == (0, 0, 0)
    finally:
        engine.dispose()


def test_k2_database_path_must_be_existing_absolute_canonical(tmp_path):
    database = tmp_path / "private.db"
    database.touch()
    assert runner._canonical_database_path(str(database)) == database
    with pytest.raises(runner.ReviewPreflightError,
                       match="k2_storage_path_untrusted"):
        runner._canonical_database_path("private.db")
    child = tmp_path / "alias"
    child.mkdir()
    with pytest.raises(runner.ReviewPreflightError,
                       match="k2_storage_path_untrusted"):
        runner._canonical_database_path(str(child / ".." / "private.db"))
    with pytest.raises(runner.ReviewPreflightError,
                       match="k2_storage_path_unverifiable"):
        runner._canonical_database_path(str(tmp_path / "missing.db"))
    with pytest.raises(runner.ReviewPreflightError,
                       match="k2_storage_path_unverifiable"):
        runner._canonical_database_path(str(database) + "\x00")
    directory = tmp_path / "directory-instead-of-db"
    directory.mkdir()
    with pytest.raises(runner.ReviewPreflightError,
                       match="k2_storage_path_untrusted"):
        runner._canonical_database_path(str(directory))


def test_k2_database_path_rejects_symlink_alias_when_supported(tmp_path):
    database = tmp_path / "private.db"
    database.touch()
    alias = tmp_path / "alias.db"
    try:
        alias.symlink_to(database)
    except OSError:
        pytest.skip("symlink creation unavailable on this host")
    with pytest.raises(runner.ReviewPreflightError,
                       match="k2_storage_path_untrusted"):
        runner._canonical_database_path(str(alias))


def test_noncanonical_database_refuses_before_storage_probe_or_dispatch(
        monkeypatch, tmp_path):
    database = tmp_path / "private.db"
    database.touch()
    child = tmp_path / "alias"
    child.mkdir()
    alias = str(child / ".." / "private.db")
    engine = SimpleNamespace(url=URL.create("sqlite", database=alias))
    monkeypatch.setattr(config, "LLM_MODE", "real")
    monkeypatch.setattr(config, "GATEWAY_BASE_URL", "https://synthetic.example/v1")
    monkeypatch.setattr(config, "GATEWAY_API_KEY", "synthetic-key")
    monkeypatch.setattr(runner, "_require_private_storage",
                        lambda path: pytest.fail("storage ACL probe must not run"))
    monkeypatch.setattr(runner, "_post_once",
                        lambda *args: pytest.fail("model must not dispatch"))
    with pytest.raises(runner.ReviewPreflightError,
                       match="k2_storage_path_untrusted"):
        _run(engine, "SS-synthetic", tmp_path)
    assert not (tmp_path / runner.ATTEMPT_DIRECTORY_NAME).exists()


def test_canonical_database_is_the_storage_probe_target(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)
    probed = []

    def stop_after_probe(database):
        probed.append(database)
        raise runner.ReviewPreflightError("synthetic_probe_stop")

    monkeypatch.setattr(runner, "_require_private_storage", stop_after_probe)
    monkeypatch.setattr(runner, "_post_once",
                        lambda *args: pytest.fail("model must not dispatch"))
    try:
        with pytest.raises(runner.ReviewPreflightError,
                           match="synthetic_probe_stop"):
            _run(engine, sid, tmp_path)
        assert probed == [(tmp_path / "synthetic.db").resolve(strict=True)]
        assert _counts(engine) == (0, 0, 0)
        assert not (tmp_path / runner.ATTEMPT_DIRECTORY_NAME).exists()
    finally:
        engine.dispose()


@pytest.mark.parametrize("exit_code,stdout,expected", [
    (0, b"", None),
    (3, b"K2_ACL_DENY:ace\r\n", "k2_storage_acl_untrusted"),
    (3, b"K2_ACL_DENY:not_directory\r\n", "k2_storage_acl_untrusted"),
    (3, b"", "k2_storage_acl_unverifiable"),
    (1, b"", "k2_storage_acl_unverifiable"),
])
def test_windows_acl_probe_isolated_system_module_and_exit_codes(
        monkeypatch, tmp_path, exit_code, stdout, expected):
    captured = []
    monkeypatch.setattr(runner, "os", SimpleNamespace(
        name="nt", environ={"SystemRoot": str(tmp_path),
                            "psmodulepath": "contaminated-bundled-modules"}))

    def fake_run(argv, **kwargs):
        captured.append((argv, kwargs))
        return SimpleNamespace(returncode=exit_code, stdout=stdout)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    database = tmp_path / "private.db"
    if expected:
        with pytest.raises(runner.ReviewPreflightError, match=expected):
            runner._require_private_storage(database)
    else:
        runner._require_private_storage(database)
    assert len(captured) == 1
    argv, kwargs = captured[0]
    assert argv[0] == str(tmp_path / "System32" / "WindowsPowerShell" /
                          "v1.0" / "powershell.exe")
    assert "Import-Module Microsoft.PowerShell.Security" in argv[-1]
    assert "$allowed -notcontains $sid" in argv[-1]
    assert "$null -eq $raw.DiscretionaryAcl" in argv[-1]
    assert "$env:K2_CHECK_DB + '-wal'" in argv[-1]
    assert "$env:K2_CHECK_DB + '-shm'" in argv[-1]
    assert "$env:K2_CHECK_ATTEMPT_DIR" in argv[-1]
    assert "$ancestor = $ancestor.Parent" in argv[-1]
    assert "0x40 -bor 0x10000 -bor 0x40000 -bor 0x80000" in argv[-1]
    assert "[int64]4294967295" in argv[-1]
    assert kwargs["env"]["K2_CHECK_ATTEMPT_DIR"] == str(
        database.parent / runner.ATTEMPT_DIRECTORY_NAME)
    assert kwargs["env"]["PSModulePath"] == str(
        tmp_path / "System32" / "WindowsPowerShell" / "v1.0" /
        "Modules")
    assert kwargs["env"]["K2_CHECK_DB"] == str(database)
    assert "psmodulepath" not in kwargs["env"]


def test_windows_acl_probe_fails_closed_on_missing_system_root_or_probe_error(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt", environ={}))
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *args, **kwargs: pytest.fail("no probe without root"))
    with pytest.raises(runner.ReviewPreflightError,
                       match="k2_storage_acl_unverifiable"):
        runner._require_private_storage(tmp_path / "private.db")

    monkeypatch.setattr(runner, "os", SimpleNamespace(
        name="nt", environ={"SystemRoot": str(tmp_path)}))

    def fail_probe(*args, **kwargs):
        raise runner.subprocess.TimeoutExpired("powershell.exe", 15)

    monkeypatch.setattr(runner.subprocess, "run", fail_probe)
    with pytest.raises(runner.ReviewPreflightError,
                       match="k2_storage_acl_unverifiable"):
        runner._require_private_storage(tmp_path / "private.db")


@pytest.mark.parametrize("mode,file_owner,caller,strict,unsafe", [
    (0o700, 1001, 1001, True, False),
    (0o700 | stat.S_ISGID, 1001, 1001, True, False),
    (0o644, 1001, 1001, True, True),
    (0o750, 1001, 1001, True, True),
    (0o770, 1001, 1001, True, True),
    (0o700, 1002, 1001, True, True),
    (0o700, 1002, 0, True, True),
    (0o755, 0, 1001, False, False),
    (0o775, 0, 1001, False, True),
    (0o777 | stat.S_ISVTX, 0, 1001, False, False),
    (0o777 | stat.S_ISVTX, 1002, 1001, False, True),
])
def test_posix_storage_mode_checks_owner_and_ancestor_replace_rights(
        mode, file_owner, caller, strict, unsafe):
    assert runner._unsafe_posix_storage_mode(
        mode, file_owner, caller, strict=strict) is unsafe


@pytest.mark.parametrize("variant,expected", [
    ("private", None),
    ("replaceable_ancestor", "k2_storage_acl_untrusted"),
    ("loose_wal", "k2_storage_acl_untrusted"),
    ("symlink_wal", "k2_storage_acl_untrusted"),
    ("unreadable_ancestor", "k2_storage_acl_unverifiable"),
    ("unreadable_sidecar", "k2_storage_acl_unverifiable"),
])
def test_posix_acl_probe_checks_optional_files_and_entire_path(
        monkeypatch, tmp_path, variant, expected):
    database = tmp_path / "private.db"
    wal = Path(str(database) + "-wal")
    modes = {
        str(database): (stat.S_IFREG | 0o600, 1001),
        str(tmp_path): (stat.S_IFDIR | 0o700, 1001),
    }
    modes.update({str(path): (stat.S_IFDIR | 0o755, 0)
                  for path in tmp_path.parents})
    if variant == "replaceable_ancestor":
        modes[str(tmp_path.parent)] = (stat.S_IFDIR | 0o777, 0)
    elif variant == "loose_wal":
        modes[str(wal)] = (stat.S_IFREG | 0o644, 1001)
    elif variant == "symlink_wal":
        modes[str(wal)] = (stat.S_IFLNK | 0o777, 1001)

    def fake_lstat(path, *args, **kwargs):
        if variant == "unreadable_sidecar" and str(path) == str(wal):
            raise PermissionError(str(path))
        if str(path) not in modes:
            raise FileNotFoundError(str(path))
        mode, owner = modes[str(path)]
        return SimpleNamespace(st_mode=mode, st_uid=owner)

    def fake_stat(path, *args, **kwargs):
        if variant == "unreadable_ancestor" and path == tmp_path.parent:
            raise PermissionError(str(path))
        return fake_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(runner, "os", SimpleNamespace(
            name="posix", geteuid=lambda: 1001))
        patch.setattr(Path, "lstat", fake_lstat)
        patch.setattr(Path, "stat", fake_stat)
        if expected:
            with pytest.raises(runner.ReviewPreflightError, match=expected):
                runner._require_private_storage(database)
        else:
            runner._require_private_storage(database)


def test_case_variant_of_same_model_refuses_before_second_dispatch(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(runner, "_post_once",
                        lambda raw, timeout: sent.append(raw) or _response())
    try:
        _run(engine, sid, tmp_path)
        variant = runner.ReviewRoute("requested-b", "provider-a",
                                     "ACTUAL-A", "channel-7")
        with pytest.raises(runner.ReviewPreflightError,
                           match="k2_model_already_voted"):
            runner.review_snapshot(engine, sid, variant,
                                   max_output_tokens=512,
                                   max_request_bytes=100_000,
                                   timeout_seconds=15)
        assert len(sent) == 1
    finally:
        engine.dispose()


def test_http_failure_records_response_status_without_vote(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_post_once",
                        lambda raw, timeout: httpx.Response(503, text="gateway unavailable"))
    try:
        with pytest.raises(runner.ReviewResponseError,
                           match="k2_gateway_http_503"):
            _run(engine, sid, tmp_path)
        assert _counts(engine) == (0, 0, 0)
        events = list((tmp_path / "semantic_review_attempts").glob(
            "*.received_http_error.json"))
        assert len(events) == 1
        assert json.loads(events[0].read_text(encoding="utf-8"))["status_code"] == 503
    finally:
        engine.dispose()


def test_evidence_change_during_call_keeps_receipts_empty(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)

    def changed_after_dispatch(raw, timeout):
        with engine.begin() as con:
            con.execute(text(
                "UPDATE expression_strategies_v2 SET abstract_operation='已改' "
                "WHERE id='ESV2-S'"))
        return _response()

    monkeypatch.setattr(runner, "_post_once", changed_after_dispatch)
    try:
        with pytest.raises(SnapshotError, match="snapshot_stale"):
            _run(engine, sid, tmp_path)
        assert _counts(engine) == (0, 0, 0)
        assert len(list((tmp_path / "semantic_review_attempts").glob(
            "*.received.json"))) == 1
    finally:
        engine.dispose()


def test_unknown_transport_outcome_is_logged_without_retry(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)
    sent = []

    def outcome_unknown(raw, timeout):
        sent.append(raw)
        raise runner.ReviewOutcomeUnknown("k2_gateway_outcome_unknown")

    monkeypatch.setattr(runner, "_post_once", outcome_unknown)
    try:
        with pytest.raises(runner.ReviewOutcomeUnknown):
            _run(engine, sid, tmp_path)
        assert len(sent) == 1
        assert _counts(engine) == (0, 0, 0)
        attempts = tmp_path / "semantic_review_attempts"
        assert len(list(attempts.glob("*.outcome_unknown.json"))) == 1
        with pytest.raises(runner.ReviewPreflightError,
                           match="k2_seat_already_attempted"):
            _run(engine, sid, tmp_path)
        assert len(sent) == 1
    finally:
        engine.dispose()


def test_unexpected_dispatch_error_is_still_outcome_unknown(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)
    sent = []

    def unexpected(raw, timeout):
        sent.append(raw)
        raise OSError("synthetic transport failure")

    monkeypatch.setattr(runner, "_post_once", unexpected)
    try:
        with pytest.raises(runner.ReviewOutcomeUnknown,
                           match="k2_gateway_outcome_unknown"):
            _run(engine, sid, tmp_path)
        assert len(sent) == 1
        assert _counts(engine) == (0, 0, 0)
        assert len(list((tmp_path / "semantic_review_attempts").glob(
            "*.outcome_unknown.json"))) == 1
    finally:
        engine.dispose()
