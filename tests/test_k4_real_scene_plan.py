"""K4 真实场景包闸回归（2026-09-30，docs/K4_REAL_SCENE_PLAN.md）。

被钉的判据（`preflight_world` 单一来源，`--preflight` 与 `--live` 共用）：
包过校验 ∧ 世界已登记 ∧ A 臂包非空 ∧（既有）语义审查收据可核验 ∧ 预算合同齐
⇒ `ready=True`；缺任一项仍拒并给具体原因码。**不传 `--scene-bundle` 仍恒拒**
（合成夹具路径的拒绝行为逐字不变）。

① 正例：真实三场包 + 已登记 + 非空 A 臂 + 可核验审查链 ⇒ ready=True，
   world_reason 给逐项读数；CLI `--preflight` 零退出。
② 负例（≥6，逐条断言仍拒 + 原因码可读）：包缺一场 / 预算合同缺一臂 /
   包 SHA 不匹配 / 自摘要被改 / 未登记 book_id / 场景未过 validate_plan /
   角色可见性冲突 / 预算越契约域 / A 臂策略不匹配 / schema 越界 /
   期望哈希格式非法 / legacy 候选包不受新通路抬举。
③ live 消费**同一个** ready：无包仍拒（零 GatewayClient 构造）；有包且过闸
   才构造客户端并按包内世界/场景卡/逐臂预算合同执行。
④ 零真实模型调用（全部用夹具库 + tmp_path + FxClient 顶替 GatewayClient），
   不碰真库 data/language_genome.db（conftest 已把测试库指到临时 SQLite）。

真实包内容转录自仓库内既有素材
F:/Hermes/team/K4_ZHUTIAN_OFFLINE_SCENES_20260928.json（sha256 8263a6fb…9ec85e），
三场草案结构与 `docs/K4_世界目录收据_20260924.md` 的契约口径一致；逐臂预算合同
按 contracts.Budget 的域冻结。
"""
from __future__ import annotations

import copy
import importlib.util as _u
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

_spec = _u.spec_from_file_location("k4", ROOT / "scripts" / "k4_paired_scenes.py")
k4 = _u.module_from_spec(_spec)
_spec.loader.exec_module(k4)

from app.scene_runtime.contracts import Budget, KnowledgePackage  # noqa: E402
from app.scene_runtime.store import Store                      # noqa: E402
from test_k5_promotion_write import (_reviewable_round, _synthetic_vote,  # noqa: E402
                                     _verdict, k5w)

BOOK = "WK-A"
SCENES = ["zhl-c1-s1", "zhl-c1-s2", "zhl-c1-s3"]


# ── 夹具：一个「已登记 + A 臂非空 + 两席审查链可核验」的临时库 ────────────────
@pytest.fixture()
def ready_world(tmp_path, monkeypatch):
    """K2 晋升链完整、A 臂 matched、WK-A 已登记的**临时合成库**（tmp_path）。

    复用的是 K5 晋升写侧的同一套取证（_reviewable_round → 双席票 → commit），
    不是本件自造状态：因此「正例」证明的是闸真会翻，而不是本件放松了什么。
    """
    db, engine, sid = _reviewable_round(tmp_path, monkeypatch)
    try:
        _synthetic_vote(monkeypatch, engine, sid, "a")
        _synthetic_vote(monkeypatch, engine, sid, "b")
        k5w.commit_promotion(db, _verdict(db, to="verified"))
        yield engine
    finally:
        engine.dispose()


def _pack(book_id: str = BOOK) -> dict:
    return k4.build_real_scene_pack(book_id)


def _write(tmp_path, payload: dict, name: str = "pack.json") -> tuple:
    """写包并回填自摘要——诚实出包流程（改内容必得重算 self_sha256）。"""
    payload = copy.deepcopy(payload)
    payload.pop("self_sha256", None)
    payload["self_sha256"] = k4.pack_self_sha256(payload)
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path, payload["self_sha256"]


def _check(tmp_path, payload: dict, *, book_id: str = BOOK, scenes: int = 3,
           expected: str | None = None, name: str = "pack.json") -> dict:
    path, sha = _write(tmp_path, payload, name)
    return k4.check_real_scene_pack(path, expected_pack_sha256=expected or sha,
                                    book_id=book_id, scene_count=scenes)


def _boom_gateway(monkeypatch) -> list:
    """把 GatewayClient 换成「一构造就记账并炸」——过闸前零真实调用可证。"""
    import app.scene_runtime.client as _cm

    built = []

    class _Boom:
        def __init__(self, *a, **k):
            built.append(True)
            raise AssertionError("闸没过（或未带真实场景包）时不得构造客户端")

    monkeypatch.setattr(_cm, "GatewayClient", _Boom)
    return built


def _cli_session(monkeypatch, engine):
    monkeypatch.setattr(k4.db, "session", lambda: Session(engine))


# ── ① 正例：闸真的翻 ─────────────────────────────────────────────────────────

def test_real_scene_pack_flips_the_gate_with_readable_reason(ready_world,
                                                             tmp_path):
    report = _check(tmp_path, _pack())
    assert report["pack_valid"] is True
    assert report["live_ready"] is False, "包本身从不授权真跑（闸才放行）"
    assert report["scene_ids"] == SCENES
    assert report["book_id_remapped"] is True and \
        report["origin_book_id"] == "zhutian-hongyanlu", "改写 book_id 必须显式"
    assert report["a_arm_policy"]["bound"] is True
    assert report["budget_contract"]["arms"] == ["A", "B"]
    assert report["budget_contract"]["entries"] == 6
    with Session(ready_world) as session:
        pre = k4.preflight_world(BOOK, session, scene_pack=report)
    assert pre["registered"] is True
    assert pre["knowledge_ready"] is True, pre          # 既有 K2/K3 闸未被放宽
    assert pre["pack_ok"] is True
    assert pre["ready"] is True, pre["world_reason"]
    assert pre["world_reason"].startswith("real_scene_plan_pack_verified:")
    for reading in ("pack_sha256=", "scenes=3", "budget_contract=arms[A,B]",
                    "max_calls=6", "max_rewrites=2", "max_elapsed_seconds=600",
                    "registered=True", "k3_status=matched",
                    "a_arm_techniques=", "review_status=verified"):
        assert reading in pre["world_reason"], pre["world_reason"]


def test_cli_preflight_real_pack_exits_zero(ready_world, tmp_path, monkeypatch,
                                            capsys):
    path, sha = _write(tmp_path, _pack())
    built = _boom_gateway(monkeypatch)
    _cli_session(monkeypatch, ready_world)
    monkeypatch.setattr(sys, "argv", [
        "k4", "--preflight", "--book-id", BOOK, "--scene-bundle", str(path),
        "--expected-pack-sha256", sha])
    k4.main()                       # ready ⇒ 不抛 SystemExit（exit 0）
    out = capsys.readouterr().out
    pre = json.loads(out[:out.index("\n[preflight] 通过")])
    assert pre["ready"] is True
    assert pre["scene_pack"]["self_sha256"] == sha
    assert pre["scene_pack"]["pov_by_scene"]["zhl-c1-s2"] == "CHAR-NING"
    assert built == [], "预检零客户端构造"


def test_emit_scene_pack_round_trips_through_the_gate(ready_world, tmp_path,
                                                      monkeypatch, capsys):
    """--emit-scene-pack 生成 → 直接喂闸 ⇒ ready=True（出包/用包同源）。"""
    target = tmp_path / "generated.json"
    monkeypatch.setattr(sys, "argv", ["k4", "--emit-scene-pack", str(target),
                                      "--book-id", BOOK])
    k4.main()
    printed = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert printed["self_sha256"] == json.loads(
        target.read_text(encoding="utf-8"))["self_sha256"]
    assert printed["scene_ids"] == SCENES
    # 不覆盖既有包（防拿旧包冒充新包）
    monkeypatch.setattr(sys, "argv", ["k4", "--emit-scene-pack", str(target),
                                      "--book-id", BOOK])
    with pytest.raises(SystemExit, match="目标已存在"):
        k4.main()
    # 生成物进闸
    _cli_session(monkeypatch, ready_world)
    monkeypatch.setattr(sys, "argv", [
        "k4", "--preflight", "--book-id", BOOK, "--scene-bundle", str(target),
        "--expected-pack-sha256", printed["self_sha256"]])
    k4.main()


# ── ② 负例：逐条仍拒 + 原因码可读 ────────────────────────────────────────────

def test_negative_pack_missing_a_scene(tmp_path):
    payload = _pack()
    payload["plans"] = payload["plans"][:2]
    with pytest.raises(k4.ScenePackError,
                       match=r"pack_scene_count_mismatch:2!=3"):
        _check(tmp_path, payload)


def test_negative_budget_contract_missing_one_arm(tmp_path):
    payload = _pack()
    entries = payload["budget_contract"]["entries"]
    payload["budget_contract"]["entries"] = [
        e for e in entries if not (e["arm"] == "B" and
                                   e["scene_id"] == "zhl-c1-s3")]
    with pytest.raises(k4.ScenePackError,
                       match=r"pack_budget_contract_incomplete:missing="
                             r"B:zhl-c1-s3"):
        _check(tmp_path, payload)


def test_negative_expected_sha256_mismatch(tmp_path):
    """换包冒充：包里 self_sha256 与 CLI 钉的值不符即拒。"""
    path, _sha = _write(tmp_path, _pack())
    with pytest.raises(k4.ScenePackError, match="pack_expected_sha256_mismatch"):
        k4.check_real_scene_pack(path, expected_pack_sha256="b" * 64,
                                 book_id=BOOK, scene_count=3)


def test_negative_tampered_pack_self_hash(tmp_path):
    payload = _pack()
    payload["world"]["facts"]["temporary_terms"]["value"] = "inactive"  # 偷改
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(k4.ScenePackError, match="pack_self_sha256_mismatch"):
        k4.check_real_scene_pack(path, expected_pack_sha256=payload[
            "self_sha256"], book_id=BOOK, scene_count=3)


def test_negative_expected_sha256_format(tmp_path):
    path, sha = _write(tmp_path, _pack())
    with pytest.raises(k4.ScenePackError,
                       match="pack_expected_sha256_invalid"):
        k4.check_real_scene_pack(path, expected_pack_sha256="NOTAHASH",
                                 book_id=BOOK, scene_count=3)


def test_negative_pack_bound_to_another_book(tmp_path):
    with pytest.raises(k4.ScenePackError, match="pack_book_id_mismatch"):
        _check(tmp_path, _pack("WK-OTHER"), book_id=BOOK)


def test_negative_scene_fails_validate_plan(tmp_path):
    """第 2 场的 before 与世界实际状态不符 ⇒ plan_precondition_conflict。"""
    payload = _pack()
    payload["plans"][1]["events"][0]["changes"][0]["before"] = "already_refused"
    payload["plans"][1]["events"][0]["changes"][0]["after"] = "refused_later"
    with pytest.raises(k4.ScenePackError,
                       match="pack_plan_invalid:zhl-c1-s2:"
                             "plan_precondition_conflict"):
        _check(tmp_path, payload)


def test_negative_revision_order_broken(tmp_path):
    """修订顺序错位（第 3 场按 rev1 声明）⇒ world_revision_conflict。"""
    payload = _pack()
    payload["plans"][2]["expected_revision"] = 1
    with pytest.raises(k4.ScenePackError,
                       match="pack_plan_invalid:zhl-c1-s3:"
                             "world_revision_conflict"):
        _check(tmp_path, payload)


def test_negative_character_visibility_conflict(tmp_path):
    """角色可见性冲突：CHAR-NING 改一个只对 CHAR-GU 可见的事实。"""
    payload = _pack()
    payload["world"]["facts"]["rescue_scope"]["visible_to"] = ["CHAR-GU"]
    payload["plans"][1]["events"][0]["changes"].append(
        {"fact": "rescue_scope", "before": "confirmed_zone_only",
         "after": "city_wide"})
    with pytest.raises(k4.ScenePackError,
                       match="pack_plan_invalid:zhl-c1-s2:"
                             "planned_fact_outside_pov"):
        _check(tmp_path, payload)


def test_negative_immutable_fact_touched(tmp_path):
    payload = _pack()
    payload["plans"][0]["events"][0]["changes"].append(
        {"fact": "anchor_link", "before": "inactive", "after": "active"})
    with pytest.raises(k4.ScenePackError,
                       match="pack_plan_invalid:zhl-c1-s1:"
                             "unknown_or_immutable_fact"):
        _check(tmp_path, payload)


def test_negative_budget_out_of_contract_domain(tmp_path):
    payload = _pack()
    payload["budget_contract"]["entries"][0]["limits"]["max_calls"] = 99
    with pytest.raises(k4.ScenePackError,
                       match="pack_budget_out_of_contract:A:zhl-c1-s1"):
        _check(tmp_path, payload)


def test_negative_a_arm_policy_not_bound(tmp_path):
    payload = _pack()
    payload["a_arm_policy"]["semantic_requirements"]["pov"] = "CHAR-HAN"
    with pytest.raises(k4.ScenePackError, match="pack_a_arm_policy_mismatch"):
        _check(tmp_path, payload)


def test_negative_schema_and_canon_status(tmp_path):
    payload = _pack()
    payload["surprise_key"] = 1
    with pytest.raises(k4.ScenePackError, match="pack_schema_invalid"):
        _check(tmp_path, payload, name="a.json")
    payload = _pack()
    payload["canon_status"] = "canon_approved_by_me"
    with pytest.raises(k4.ScenePackError, match="pack_schema_invalid"):
        _check(tmp_path, payload, name="b.json")


def test_negative_duplicate_scene_identity(tmp_path):
    payload = _pack()
    payload["plans"][1]["scene_id"] = "zhl-c1-s1"
    with pytest.raises(k4.ScenePackError,
                       match="pack_scene_identity_duplicate"):
        _check(tmp_path, payload)


def test_negative_scene_count_from_cli_must_match(tmp_path):
    """--scenes 2 但包内三场 ⇒ 拒（不静默跑前两场）。"""
    with pytest.raises(k4.ScenePackError,
                       match="pack_scene_count_mismatch:3!=2"):
        _check(tmp_path, _pack(), scenes=2)


def test_negative_scene_scope_mismatch(tmp_path):
    """场景卡 book_id/branch_id 与包不符 ⇒ 拒（不认「同一本书的不同分支」）。"""
    payload = _pack()
    payload["plans"][0]["branch_id"] = "alt"
    with pytest.raises(k4.ScenePackError, match="pack_scene_scope_mismatch"):
        _check(tmp_path, payload)


def test_negative_budget_contract_arms_incomplete(tmp_path):
    """arms 声明只剩一臂（哪怕 entries 六条齐全）⇒ 拒：合同不许自称单臂。"""
    payload = _pack()
    payload["budget_contract"]["arms"] = ["A"]
    with pytest.raises(k4.ScenePackError,
                       match="pack_budget_contract_incomplete:arms=A"):
        _check(tmp_path, payload)


def test_negative_budget_contract_duplicate_entry(tmp_path):
    """同一 (arm, scene) 两条上限（可互相矛盾）⇒ 拒：逐场预算不许有歧义。"""
    payload = _pack()
    entry = payload["budget_contract"]["entries"][0]
    payload["budget_contract"]["entries"].append(copy.deepcopy(entry))
    with pytest.raises(k4.ScenePackError,
                       match="pack_budget_contract_duplicate:A:zhl-c1-s1"):
        _check(tmp_path, payload)


def test_negative_budget_contract_extra_scene(tmp_path):
    """合同里多出一条包外场次 ⇒ 拒（多出的上限不会被执行，却会进读数）。"""
    payload = _pack()
    payload["budget_contract"]["entries"].append(
        {"arm": "A", "scene_id": "zhl-c1-s9",
         "limits": dict(payload["budget_contract"]["entries"][0]["limits"])})
    with pytest.raises(k4.ScenePackError,
                       match="pack_budget_contract_extra:A:zhl-c1-s9"):
        _check(tmp_path, payload)


def test_negative_pack_file_layer(tmp_path):
    """文件层三拒：读不到 / 不是合法 JSON（重复键）/ 超 1 MiB 上限。"""
    with pytest.raises(k4.ScenePackError, match="pack_unreadable"):
        k4.check_real_scene_pack(tmp_path / "nope.json",
                                 expected_pack_sha256="a" * 64,
                                 book_id=BOOK, scene_count=3)
    dup = tmp_path / "dup.json"
    dup.write_text('{"schema_version":"k4-real-scene-pack/1","a":1,"a":2}',
                   encoding="utf-8")
    with pytest.raises(k4.ScenePackError, match="pack_json_invalid"):
        k4.check_real_scene_pack(dup, expected_pack_sha256="a" * 64,
                                 book_id=BOOK, scene_count=3)
    huge = tmp_path / "huge.json"
    huge.write_text("{" + '"x":"' + "y" * (k4.MAX_SCENE_PACK_BYTES + 8) + '"}',
                    encoding="utf-8")
    with pytest.raises(k4.ScenePackError, match="pack_size_invalid"):
        k4.check_real_scene_pack(huge, expected_pack_sha256="a" * 64,
                                 book_id=BOOK, scene_count=3)


# ── ②b 闸侧负例：世界未登记 / A 臂空 / 审查不可核验（包再好也拒）──────────────

def test_negative_unregistered_book_id_still_refused(ready_world, tmp_path,
                                                     monkeypatch, capsys):
    """包完全合格（绑定到这个 id），但 work_sources 无该行 ⇒ 仍拒 world_not_registered。"""
    orphan = "WK-NOT-REGISTERED"
    report = _check(tmp_path, _pack(orphan), book_id=orphan)
    with Session(ready_world) as session:
        pre = k4.preflight_world(orphan, session, scene_pack=report)
    assert pre["registered"] is False
    assert pre["pack_ok"] is True
    assert pre["ready"] is False
    assert "world_not_registered" in pre["world_reason"]
    assert pre["world_reason"].startswith("real_scene_plan_unverified:")
    # CLI：未登记 + 合格包 ⇒ 非零退出，零客户端构造
    built = _boom_gateway(monkeypatch)
    _cli_session(monkeypatch, ready_world)
    path, sha = _write(tmp_path, _pack(orphan), name="orphan.json")
    monkeypatch.setattr(sys, "argv", [
        "k4", "--preflight", "--book-id", orphan,
        "--scene-bundle", str(path), "--expected-pack-sha256", sha])
    with pytest.raises(SystemExit, match="world_not_registered"):
        k4.main()
    assert built == []


def test_negative_a_arm_empty_still_refused(ready_world, tmp_path, monkeypatch,
                                            capsys):
    """A 臂空（k3 非 matched）⇒ 仍拒，原因码是 empty_package 而非包的问题。

    制造方式＝把策略行降级出合格 status 集合（eligible_statuses 只认
    verified）——不删行：策略被 K2 收据/实例/晋升审计外键引用，删行会撞 FK，
    那是把「造出空 A 臂」偷换成「拆库」。
    """
    from app.models import ExpressionStrategyV2
    with Session(ready_world) as session:
        for row in session.query(ExpressionStrategyV2).all():
            row.status = "hypothesis"
        session.commit()
    report = _check(tmp_path, _pack())
    with Session(ready_world) as session:
        pre = k4.preflight_world(BOOK, session, scene_pack=report)
    assert pre["pack_ok"] is True
    assert pre["n_techniques"] == 0
    assert pre["ready"] is False
    assert "empty_package" in pre["world_reason"]
    built = _boom_gateway(monkeypatch)
    _cli_session(monkeypatch, ready_world)
    path, sha = _write(tmp_path, _pack())
    monkeypatch.setattr(sys, "argv", [
        "k4", "--live", "--book-id", BOOK, "--scene-bundle", str(path),
        "--expected-pack-sha256", sha, "--writer-model", "w",
        "--verifier-model", "v"])
    monkeypatch.setenv("K4_ALLOW_LIVE", "1")
    monkeypatch.setenv("LG_LOCK_DIR", str(tmp_path / "lock"))
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "LLM_MODE", "real")
    with pytest.raises(SystemExit, match="empty_package"):
        k4.main()
    assert built == [], "未过闸：零客户端构造"


def test_negative_review_unverifiable_still_refused(ready_world, tmp_path):
    """A 臂非空但审查收据不可核验 ⇒ 仍拒（本件不放宽既有 K2 闸）。"""
    from app.semantic_review_store import freeze_snapshot
    from app.models import ExpressionStrategyV2
    with Session(ready_world) as session:
        card = session.get(ExpressionStrategyV2, "ESV2-T")
        claim = {"scope_to": card.scope, "scope_ids": card.scope_ids,
                 "scope_basis": card.scope_basis}
    freeze_snapshot(ready_world, "ESV2-T", 1, claim)   # 新一轮 ⇒ 旧签认作废
    report = _check(tmp_path, _pack())
    with Session(ready_world) as session:
        pre = k4.preflight_world(BOOK, session, scene_pack=report)
    assert pre["n_techniques"] > 0
    assert pre["review_status"] == "semantic_review_unverifiable"
    assert pre["ready"] is False
    assert "semantic_review_unverifiable" in pre["world_reason"]


def test_negative_no_bundle_keeps_synthetic_refusal(ready_world):
    """不传 --scene-bundle ⇒ 行为与今天一致（仍拒，原因码不变前缀）。"""
    with Session(ready_world) as session:
        pre = k4.preflight_world(BOOK, session)
    assert pre["ready"] is False
    assert pre["pack_ok"] is False
    assert pre["world_reason"].startswith("real_scene_plan_unverified")
    assert "no_scene_pack" in pre["world_reason"]


def test_negative_legacy_candidate_bundle_is_not_promoted(ready_world,
                                                          tmp_path, monkeypatch,
                                                          capsys):
    """legacy 离线候选包（无 schema_version）：仍只结构检查、不抬举闸。"""
    legacy = {
        "status": "offline_candidate_not_canon_approved",
        "source": {"title": "候选", "production_pack_sha256": "a" * 64,
                   "chapter_contract": 1, "scope": "开篇两场"},
        "unresolved_canon": ["签署资格待核定"],
        "world": {"book_id": BOOK, "branch_id": "main", "revision": 0,
                  "characters": {"gu": "顾"},
                  "facts": {"s": {"value": 0, "visible_to": ["gu"],
                                  "reader_visible": True, "mutable": True}},
                  "rules": []},
        "plans": [
            {"book_id": BOOK, "scene_id": "a", "idempotency_key": "a1",
             "expected_revision": 0, "pov": "gu", "goal": "确认",
             "style": "行动", "events": [
                 {"event_id": "e", "description": "确认幸存者",
                  "changes": [{"fact": "s", "before": 0, "after": 1}]}],
             "min_chars": 100, "max_chars": 500},
            {"book_id": BOOK, "scene_id": "b", "idempotency_key": "b1",
             "expected_revision": 1, "pov": "gu", "goal": "提出条款",
             "style": "行动", "events": [
                 {"event_id": "f", "description": "救援待签",
                  "changes": [{"fact": "s", "before": 1, "after": 2}]}],
             "min_chars": 100, "max_chars": 500}],
        "runtime_budget_draft": {
            "per_scene_per_arm_max_calls": 6,
            "per_scene_per_arm_max_output_tokens_per_call": 3000,
            "per_scene_per_arm_max_elapsed_seconds": 600,
            "route_and_price_verified": False,
            "monetary_cap_approved": False},
    }
    assert k4.scene_bundle_kind(_write_legacy(tmp_path, legacy)) == "legacy"
    path = _write_legacy(tmp_path, legacy)
    built = _boom_gateway(monkeypatch)
    _cli_session(monkeypatch, ready_world)
    monkeypatch.setattr(sys, "argv", [
        "k4", "--preflight", "--book-id", BOOK, "--scenes", "2",
        "--scene-bundle", str(path),
        "--expected-pack-sha256", "a" * 64])
    with pytest.raises(SystemExit, match="real_scene_plan_unverified"):
        k4.main()
    pre = json.loads(capsys.readouterr().out)
    assert pre["scene_bundle"]["structure_pass"] is True
    assert pre["scene_bundle"]["live_ready"] is False
    assert pre["pack_ok"] is False and pre["ready"] is False
    assert built == []


def _write_legacy(tmp_path, payload: dict) -> Path:
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_negative_real_pack_refused_for_offline_run(tmp_path, monkeypatch):
    """真实场景包不接离线跑（离线跑仍是合成夹具产物面）。"""
    path, sha = _write(tmp_path, _pack())
    monkeypatch.setattr(sys, "argv", ["k4", "--scene-bundle", str(path),
                                      "--expected-pack-sha256", sha])
    with pytest.raises(SystemExit, match="只接 --preflight/--live"):
        k4.main()


def test_negative_budget_calls_conflicting_with_contract(tmp_path):
    payload = _pack()
    payload["budget_contract"]["entries"][0]["limits"]["max_calls"] = 4
    path, sha = _write(tmp_path, payload)
    pack = k4.load_real_scene_pack(path)
    budgets = k4._pack_budget_contract(pack, [p.scene_id for p in pack.plans])

    class _Store:
        def __init__(self, root):
            self.root = Path(root)

        def create_world(self, world, **kw):
            return None

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        factory = _Store(tmp)
        with pytest.raises(ValueError, match="与场景包内 A:zhl-c1-s1"):
            k4.run_paired(lambda: factory, k4.FxClient(), object(),
                          scene_plans=list(pack.plans), budget_contract=budgets,
                          n_scenes=3, budget_calls=7)


# ── ③ live 消费同一个 ready；过闸后按包执行 ───────────────────────────────────

def test_live_without_pack_still_refused_after_ready_world(ready_world,
                                                           tmp_path,
                                                           monkeypatch):
    """K2/K3 全绿但**没带场景包** ⇒ 仍拒 real_scene_plan_unverified、零构造。"""
    monkeypatch.setenv("K4_ALLOW_LIVE", "1")
    monkeypatch.setenv("LG_LOCK_DIR", str(tmp_path / "lock"))
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "LLM_MODE", "real")
    built = _boom_gateway(monkeypatch)
    _cli_session(monkeypatch, ready_world)
    monkeypatch.setattr(sys, "argv", ["k4", "--live", "--book-id", BOOK,
                                      "--writer-model", "w",
                                      "--verifier-model", "v"])
    with pytest.raises(SystemExit, match="real_scene_plan_unverified"):
        k4.main()
    assert built == []


def test_live_passes_same_gate_then_runs_pack_scenes(ready_world, tmp_path,
                                                     monkeypatch, capsys):
    """过闸才构造客户端；跑的是包内世界/场景卡/逐臂预算合同，收据落包哈希。"""
    monkeypatch.setenv("K4_ALLOW_LIVE", "1")
    monkeypatch.setenv("LG_LOCK_DIR", str(tmp_path / "lock"))
    from app import config as _cfg
    import app.scene_runtime.client as _cm
    monkeypatch.setattr(_cfg, "LLM_MODE", "real")
    built = []
    fixture = k4.FxClient()

    class _FixtureGateway:
        models = {"writer": "fx-w", "verifier": "fx-v", "transport": "fixture"}

        def __init__(self, writer, verifier):
            built.append((writer, verifier))

        invoke = fixture.invoke

    monkeypatch.setattr(_cm, "GatewayClient", _FixtureGateway)
    _cli_session(monkeypatch, ready_world)
    path, sha = _write(tmp_path, _pack())
    out_dir = tmp_path / "live_out"
    monkeypatch.setattr(sys, "argv", [
        "k4", "--live", "--book-id", BOOK, "--scene-bundle", str(path),
        "--expected-pack-sha256", sha, "--writer-model", "w",
        "--verifier-model", "v", "--out", str(out_dir)])
    k4.main()
    assert built == [("w", "v")], "过闸后才构造真实客户端"
    art = json.loads((out_dir / "k4_paired.json").read_text(encoding="utf-8"))
    assert art["live"] is True and art["book_id"] == BOOK
    assert art["scene_pack"]["sha256"] == sha
    assert art["scene_pack"]["scene_ids"] == SCENES
    receipts = art["artifacts"]["receipts"]
    assert [r["scene"] for r in receipts] == [s for s in SCENES
                                              for _ in ("A", "B")]
    for r in receipts:
        assert r["scene_pack_sha256"] == sha
        assert r["scene_pack_arms"] == ["A", "B"]
        assert r["budget_calls"] == 6
        assert r["usage"]["calls"] > 0
    assert not art["artifacts"]["failures"], art["artifacts"]["failures"]
    # 跑的是包内世界：产物世界库里是真实角色名，不是「林穗」夹具
    world_dir = Path(art["worlds_dir"]) / "arm1" / "k4.sqlite"
    export = Store(world_dir).export(BOOK)
    assert export and export[-1]["text"], "包内场景必须真写出正文"


def test_pack_driven_execution_uses_pack_budget_contract(tmp_path):
    """库层：run_paired 吃 scene_plans+budget_contract ⇒ 逐场预算取自合同。

    lg_session 给真 Session（conftest 已把库指到临时 SQLite，且此处
    seed_knowledge() 建表）：A 臂的 frozen_package_for_scene 会走 K3 只读
    查询，替身对象会在 s.query(...) 上炸（无表则报
    knowledge_query_unavailable）——那是替身/建表缺陷，不是预算合同的读数。
    """
    import knowledge_seed as KS
    KS.seed_knowledge()
    payload = _pack("WK-LIB")
    payload["budget_contract"]["entries"][0]["limits"]["max_calls"] = 3
    path, _sha = _write(tmp_path, payload)
    pack = k4.load_real_scene_pack(path)
    budgets = k4._pack_budget_contract(pack, [p.scene_id for p in pack.plans])
    n = {"i": 0}
    roots = []

    def factory():
        n["i"] += 1
        root = tmp_path / f"arm{n['i']}"
        roots.append(root)
        store = Store(root / "k4.sqlite")
        store.create_world(pack.world)
        return store

    with k4.db.session() as lg_session:
        four = k4.run_paired(factory, k4.FxClient(), lg_session,
                             scene_plans=list(pack.plans),
                             budget_contract=budgets,
                             n_scenes=3, book_id="WK-LIB")
    assert not four["failures"], four["failures"]
    assert [r["scene"] for r in four["receipts"]] == [
        "zhl-c1-s1", "zhl-c1-s1", "zhl-c1-s2", "zhl-c1-s2",
        "zhl-c1-s3", "zhl-c1-s3"]
    # 只有包内被改成 3 的那一条（A:zhl-c1-s1）取 3；其余六条仍取合同默认 6
    # ——若实现退回 Budget() 默认值，这条会一起变成 6（读不出逐场合同）。
    for r in four["receipts"]:
        key = (r["arm"], r["scene"])
        assert r["budget_calls"] == (3 if key == ("A", "zhl-c1-s1") else 6), \
            f"逐场预算必须取包内合同：{key} 读到 {r['budget_calls']}"
    # 两臂各自的 rev3 世界都推进了三场（包内事件链可离线推进）
    for root in roots:
        assert Store(root / "k4.sqlite").snapshot("WK-LIB").revision == 3


def test_default_run_untouched_without_pack(tmp_path, monkeypatch, capsys):
    """无场景包 ⇒ 合成夹具路径逐字不变（三场 s1..s3、林穗世界、无新收据键）。"""
    import knowledge_seed as KS
    from app import db
    KS.seed_knowledge()
    monkeypatch.setattr(sys, "argv", ["k4", "--out", str(tmp_path / "out")])
    k4.main()
    art = json.loads((tmp_path / "out" / "k4_paired.json").read_text(
        encoding="utf-8"))
    assert set(art) == {"artifacts", "analysis", "live", "channel_changed",
                        "worlds_dir"}
    assert [r["scene"] for r in art["artifacts"]["receipts"]] == [
        "s1", "s1", "s2", "s2", "s3", "s3"]
    assert set(art["artifacts"]["receipts"][0]) == {
        "scene", "arm", "job_id", "usage", "budget_calls", "live",
        "channel_changed", "gateway_host", "models", "retried",
        "verifier_attempts"}
    with db.session() as session:
        pre = k4.preflight_world("WK-K4", session)
    assert pre["ready"] is False
    assert pre["world_reason"].startswith("real_scene_plan_unverified")


def test_pack_budget_domain_matches_contract_budget():
    """逐臂上限的域与 contracts.Budget 同源（不另立一套更宽的域）。

    2026-10-01：上/下限不再写死数字，改为**读 contracts.Budget 自己的 Field 元数据**
    （`ge`/`le`）——本次把 `le` 从 20/2 提到 60/4 后，旧写死数字会假红；读元数据既
    不会再假红，也仍然能抓到「包声明了比契约更宽的域」这种真问题。
    """
    limits = k4.REAL_SCENE_CARD_SOURCE["budget_contract_limits"]
    assert Budget(**limits) == Budget(max_calls=6, max_rewrites=2)
    checked = 0
    for name, field in Budget.model_fields.items():
        for meta in field.metadata:
            le = getattr(meta, "le", None)
            ge = getattr(meta, "ge", None)
            if le is not None:
                with pytest.raises(Exception):
                    Budget(**{**limits, name: le + 1})
                checked += 1
            if ge is not None:
                with pytest.raises(Exception):
                    Budget(**{**limits, name: ge - 1})
                checked += 1
    assert checked >= 10, "至少核到 10 个域边界（防元数据读取退化成空跑）"
    knowledge = KnowledgePackage(package_id="scope-only", book_id="WK-A",
                                 source_kind="knowledge_query_v2",
                                 techniques=[])
    assert knowledge.techniques == [], "离线不得凭空造 A 臂技巧"
