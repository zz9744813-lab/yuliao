"""Offline story contracts must be repeatable without opening DB or gateway."""
from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from app.scene_runtime.offline_scene_bundle import (
    SceneBundleError, check_offline_scene_bundle)


def _bundle() -> dict:
    return {
        "status": "offline_candidate_not_canon_approved",
        "source": {
            "title": "原创试点",
            "production_pack_sha256": "a" * 64,
            "chapter_contract": 1,
            "scope": "开篇两场",
        },
        "unresolved_canon": ["签署资格待核定"],
        "world": {
            "book_id": "original-one",
            "branch_id": "main",
            "revision": 0,
            "characters": {"protagonist": "主角"},
            "facts": {
                "rescue_state": {
                    "value": 0, "visible_to": ["protagonist"],
                    "reader_visible": True, "mutable": True,
                },
                "unsigned": {
                    "value": True, "visible_to": ["protagonist"],
                    "reader_visible": True, "mutable": False,
                },
            },
            "rules": ["未签署不得启动救援锚"],
        },
        "plans": [
            {
                "book_id": "original-one", "branch_id": "main",
                "scene_id": "scene-1", "idempotency_key": "scene-1-v1",
                "expected_revision": 0, "pov": "protagonist",
                "goal": "确认范围", "style": "具体行动",
                "events": [{"event_id": "observed", "description": "确认幸存者",
                            "changes": [{"fact": "rescue_state", "before": 0,
                                         "after": 1}]}],
                "min_chars": 100, "max_chars": 500,
            },
            {
                "book_id": "original-one", "branch_id": "main",
                "scene_id": "scene-2", "idempotency_key": "scene-2-v1",
                "expected_revision": 1, "pov": "protagonist",
                "goal": "提出条款", "style": "具体行动",
                "events": [{"event_id": "terms", "description": "救援待签",
                            "changes": [{"fact": "rescue_state", "before": 1,
                                         "after": 2}]}],
                "min_chars": 100, "max_chars": 500,
            },
        ],
        "runtime_budget_draft": {
            "per_scene_per_arm_max_calls": 6,
            "per_scene_per_arm_max_output_tokens_per_call": 3000,
            "per_scene_per_arm_max_elapsed_seconds": 600,
            "route_and_price_verified": False,
            "monetary_cap_approved": False,
        },
    }


def _save(tmp_path, bundle: dict):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return path


def test_two_arm_scene_structure_pass_is_never_live_ready(tmp_path):
    report = check_offline_scene_bundle(
        _save(tmp_path, _bundle()), expected_pack_sha256="a" * 64)
    assert report["structure_pass"] is True
    assert report["live_ready"] is False
    assert report["scene_ids"] == ["scene-1", "scene-2"]
    assert report["declared_pack_digest_matched_expected"] is True
    assert report["source_pack_file_checked"] is False
    assert report["db_registration_checked"] is False
    assert report["model_calls"] == 0
    assert len(report["initial_world_sha256"]) == 64
    assert len(report["final_world_sha256"]) == 64


def test_declared_pack_hash_needs_independent_expected_hash(tmp_path):
    path = _save(tmp_path, _bundle())
    assert (check_offline_scene_bundle(path)
            ["declared_pack_digest_matched_expected"] is False)
    with pytest.raises(SceneBundleError, match="source_pack_digest_mismatch"):
        check_offline_scene_bundle(path, expected_pack_sha256="b" * 64)


@pytest.mark.parametrize("change,reason", [
    (lambda b: b["plans"][1].update(expected_revision=2),
     "bundle_plan_invalid:world_revision_conflict"),
    (lambda b: b["plans"][1]["events"][0]["changes"][0]
     .update(before=0), "bundle_plan_invalid:plan_precondition_conflict"),
    (lambda b: b["plans"][1].update(scene_id="scene-1"),
     "bundle_scene_identity_duplicate"),
    (lambda b: b["plans"][1].update(idempotency_key="scene-1-v1"),
     "bundle_scene_identity_duplicate"),
    (lambda b: b["plans"][0]["events"][0]["changes"][0]
     .update(fact="unsigned", before=True, after=False),
     "bundle_plan_invalid:unknown_or_immutable_fact"),
    (lambda b: b.update(status="canon_approved"), "bundle_schema_invalid"),
    (lambda b: b["world"].update(unexpected="x"), "bundle_schema_invalid"),
])
def test_invalid_bundle_rejected(tmp_path, change, reason):
    bundle = _bundle()
    change(bundle)
    with pytest.raises(SceneBundleError, match=reason):
        check_offline_scene_bundle(_save(tmp_path, bundle))


def test_duplicate_json_keys_and_oversize_rejected(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text('{"status":"first","status":"second"}', encoding="utf-8")
    with pytest.raises(SceneBundleError, match="bundle_json_invalid"):
        check_offline_scene_bundle(path)
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(SceneBundleError, match="bundle_size_invalid"):
        check_offline_scene_bundle(path)


def _driver():
    script = Path(__file__).resolve().parents[1] / "scripts" / "k4_paired_scenes.py"
    spec = spec_from_file_location("k4_bundle_driver_test", script)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_k4_preflight_reports_bundle_but_stays_closed(
        tmp_path, monkeypatch, capsys):
    driver = _driver()
    path = _save(tmp_path, _bundle())
    monkeypatch.setattr(driver.db, "session", lambda: nullcontext(object()))
    monkeypatch.setattr(driver, "preflight_world", lambda book_id, _session: {
        "book_id": book_id, "ready": False, "empty_reason": None,
        "review_reason": None, "world_reason": "real_scene_plan_unverified",
        "k3_status": "empty",
    })
    monkeypatch.setattr(sys, "argv", [
        "k4", "--preflight", "--book-id", "original-one", "--scenes", "2",
        "--scene-bundle", str(path), "--expected-pack-sha256", "a" * 64,
    ])
    with pytest.raises(SystemExit, match="real_scene_plan_unverified"):
        driver.main()
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is False
    assert report["scene_bundle"]["structure_pass"] is True
    assert report["scene_bundle"]["live_ready"] is False
    assert report["scene_bundle"]["model_calls"] == 0


def test_k4_scene_bundle_cannot_enter_live_runner(tmp_path, monkeypatch):
    driver = _driver()
    path = _save(tmp_path, _bundle())
    monkeypatch.setattr(sys, "argv", [
        "k4", "--live", "--book-id", "original-one", "--scenes", "2",
        "--scene-bundle", str(path), "--expected-pack-sha256", "a" * 64,
    ])
    monkeypatch.setattr(driver, "preflight_world", lambda *_args: pytest.fail(
        "live bundle must fail before DB preflight"))
    with pytest.raises(SystemExit, match="仅供 --preflight"):
        driver.main()
