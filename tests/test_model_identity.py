"""live 通道「模型身份诚实化」（任务书 2026-10-01 A）——零真实调用。

背景（实测证据 A）：litellm 网关会静默替换上游模型（世界库扫描 491 次调用
里 42 次请求≠实际）。诚实化口径：
· calls 表新增 requested_model / actual_model / model_substituted 持久列；
· 默认 fail-closed：actual != requested ⇒ RuntimeFault
  ("model_identity_mismatch:req->actual")；
· LG_ALLOW_MODEL_SUBSTITUTION=1 显式降级为「如实记录 + substituted 标记」；
  **两种模式都必须落账真相，不许静默**；
· k4 逐臂收据：models=请求（语义不变），models_actual=实际服务模型，
  model_identity_ok=布尔（无请求侧记录 ⇒ False，不为凑绿放松）。
全部用注入式 httpx MockTransport（假网关），不触任何真实模型额度。
"""
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util as _u
_spec = _u.spec_from_file_location("k4", ROOT / "scripts" / "k4_paired_scenes.py")
k4 = _u.module_from_spec(_spec)
_spec.loader.exec_module(k4)

from knowledge_seed import seed_knowledge                       # noqa: E402
from app import config as _cfg                                  # noqa: E402
from app import db                                              # noqa: E402
from app.scene_runtime import RUNTIME_VERSION                   # noqa: E402
from app.scene_runtime.client import GatewayClient              # noqa: E402
from app.scene_runtime.contracts import (Budget, Change,        # noqa: E402
                                         Fact, KnowledgePackage,
                                         PlannedEvent, RuntimeFault,
                                         ScenePlan, World)
from app.scene_runtime.pipeline import SceneRunner              # noqa: E402
from app.scene_runtime.store import Store                       # noqa: E402

W = "mc22-flash"
V = "deepseek-v4.1-flash"
DRAFT_TEXT = "林穗把一枚钱放在桌上，又收了回去，灯花跳了一下。"


@pytest.fixture
def live_cfg(monkeypatch):
    """假网关环境：real 模式 + 占位 endpoint（MockTransport 不触网）+
    默认不开降级开关（逐测试再按需 setenv）。**非 autouse**——k4 收据
    集成测试走离线 pack 路径，必须保持 LLM_MODE=mock（freeze=False 在
    real 模式下会被语义批准闸拒，属既有纪律，不是本测试要动的判据）。"""
    monkeypatch.setattr(_cfg, "LLM_MODE", "real")
    monkeypatch.setattr(_cfg, "GATEWAY_BASE_URL", "http://gateway.invalid/v1")
    monkeypatch.setattr(_cfg, "GATEWAY_API_KEY", "test-key-only")
    monkeypatch.delenv("LG_ALLOW_MODEL_SUBSTITUTION", raising=False)


class Gateway:
    """脚本化假网关：writer 回复按队列出牌（空则给合规草稿），
    verifier 恒按请求负载回合规评审 JSON。served_* = 实际服务模型
    （模拟 litellm 换皮）；model=None 模拟网关不报实际模型。"""

    def __init__(self, served_writer=W, served_verifier=V, omit_model=False):
        self.served = {"writer": served_writer, "verifier": served_verifier}
        self.omit_model = omit_model
        self.requests = []
        self.transport = httpx.MockTransport(self._handler)

    @staticmethod
    def _review(payload):
        plan, text = payload["plan"], payload["text"]
        return json.dumps({"issues": [],
                           "changes": [{"fact": c["fact"], "after": c["after"],
                                        "quote": text}
                                       for ev in plan["events"] for c in ev["changes"]],
                           "events": [{"event_id": ev["event_id"], "quote": text}
                                      for ev in plan["events"]]},
                          ensure_ascii=False)

    def _handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        role = "writer" if body["model"] == W else "verifier"
        if role == "writer":
            content = json.dumps({"text": DRAFT_TEXT}, ensure_ascii=False)
        else:
            content = self._review(json.loads(body["messages"][1]["content"]))
        data = {"id": "chatcmpl-fake",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22}}
        if not self.omit_model:
            data["model"] = self.served[role]
        return httpx.Response(200, json=data)

    def models_requested(self):
        return [b["model"] for b in self.requests]


@pytest.fixture
def scene(tmp_path):
    store = Store(tmp_path / "runtime.sqlite")
    store.create_world(World(book_id="book-a", revision=0,
                             characters={"lin": "林穗", "shen": "沈砚"},
                             facts={"coins": Fact(value=3, visible_to=["lin"]),
                                    "received": Fact(value=0,
                                                     visible_to=["lin", "shen"])},
                             rules=["No magic"]))
    plan = ScenePlan(book_id="book-a", scene_id="s1", idempotency_key="s1-v1",
                     expected_revision=0, pov="lin", goal="支付一枚钱",
                     style="简洁", min_chars=1, max_chars=500,
                     events=[PlannedEvent(event_id="pay", description="支付一枚钱",
                                          changes=[Change(fact="coins", before=3, after=2),
                                                   Change(fact="received", before=0, after=1)])])
    knowledge = KnowledgePackage(package_id="empty-1", book_id="book-a",
                                 source_kind="empty", techniques=[])
    return store, plan, knowledge


def _run(store, plan, knowledge, gateway):
    client = GatewayClient(W, V, transport=gateway.transport)
    receipt = SceneRunner(store, client).run(plan, knowledge, Budget())
    return receipt


def _call_row(store, stage):
    with store.connection() as db:
        row = db.execute("SELECT status,error,requested_model,actual_model,"
                         "model_substituted FROM calls WHERE stage=?",
                         (stage,)).fetchone()
    return dict(row) if row else None


def test_success_persists_identity_columns(scene, live_cfg):
    """正向：一致通道把请求/实际模型逐次落进 calls 表新列（A1 落账）。"""
    store, plan, knowledge = scene
    gateway = Gateway()
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    for stage in ("writer.0", "verifier.0"):
        row = _call_row(store, stage)
        assert row["status"] == "succeeded"
        expected = W if stage.startswith("writer") else V
        assert row["requested_model"] == expected and row["actual_model"] == expected
        assert row["model_substituted"] == 0


def test_mismatch_fail_closed_by_default(scene, live_cfg):
    """反向②：actual != requested 且未开降级开关 ⇒ 必须 raise；且失败行
    同样把真相落账（error 码 + actual_model 列），审计侧看得到被拒的皮。"""
    store, plan, knowledge = scene
    gateway = Gateway(served_writer="opencode/nemotron-3-ultra-free")
    with pytest.raises(RuntimeFault,
                       match=r"model_identity_mismatch:mc22-flash->"
                             r"opencode/nemotron-3-ultra-free"):
        _run(store, plan, knowledge, gateway)
    row = _call_row(store, "writer.0")
    assert row["status"] == "failed"
    assert row["error"].startswith("model_identity_mismatch:")
    assert row["requested_model"] == W
    assert row["actual_model"] == "opencode/nemotron-3-ultra-free"
    assert row["model_substituted"] == 1, "被拒的替换也必须如实进台账"


def test_substitution_env_downgrade_records_truth(scene, live_cfg, monkeypatch):
    """LG_ALLOW_MODEL_SUBSTITUTION=1：不抛错，但 substituted 标记与
    actual_model 如实落账（收据可核），绝不静默。"""
    store, plan, knowledge = scene
    monkeypatch.setenv("LG_ALLOW_MODEL_SUBSTITUTION", "1")
    gateway = Gateway(served_writer="deepseek-v4.1-flash")
    receipt = _run(store, plan, knowledge, gateway)
    assert receipt["status"] == "committed"
    row = _call_row(store, "writer.0")
    assert row["status"] == "succeeded"
    assert row["requested_model"] == W and row["actual_model"] == "deepseek-v4.1-flash"
    assert row["model_substituted"] == 1
    attempts = {a["stage"]: a for a in store.usage(receipt["job_id"])["attempts"]}
    assert attempts["writer.0"]["substituted"] is True
    assert attempts["verifier.0"]["substituted"] is False


def test_unreported_actual_is_mismatch(scene, live_cfg):
    """网关连实际模型都不报 = 身份不可证 ⇒ 同属不一致，默认 fail-closed。"""
    store, plan, knowledge = scene
    gateway = Gateway(omit_model=True)
    with pytest.raises(RuntimeFault,
                       match=r"model_identity_mismatch:mc22-flash->unreported"):
        _run(store, plan, knowledge, gateway)
    assert _call_row(store, "writer.0")["actual_model"] is None


def test_same_upstream_under_two_names_is_not_ok(scene, live_cfg, monkeypatch):
    """「异模型」前提的可证伪性：两个请求名被同一上游服务 ⇒
    model_identity_ok 必须 False、models_actual 两个值互现（不藏）。"""
    store, plan, knowledge = scene
    monkeypatch.setenv("LG_ALLOW_MODEL_SUBSTITUTION", "1")
    gateway = Gateway(served_writer="one-real-upstream",
                      served_verifier="one-real-upstream")
    receipt = _run(store, plan, knowledge, gateway)
    fields = k4.model_identity_fields(store.usage(receipt["job_id"]))
    assert fields["model_identity_ok"] is False
    assert fields["models_actual"] == {"writer": "one-real-upstream",
                                       "verifier": "one-real-upstream"}


def test_identity_fields_helper_verdicts():
    """model_identity_fields 纯函数口径（A3）：一致=True；换皮=False；
    夹具无请求侧记录=无法证明=False；同角色逐次换皮=清单如实全列。"""
    ok = k4.model_identity_fields({"attempts": [
        {"stage": "writer.0", "requested_model": W, "actual_model": W},
        {"stage": "verifier.0", "requested_model": V, "actual_model": V}]})
    assert ok == {"models_actual": {"writer": W, "verifier": V},
                  "model_identity_ok": True}
    subst = k4.model_identity_fields({"attempts": [
        {"stage": "writer.0", "requested_model": W, "actual_model": "x"}]})
    assert subst["models_actual"]["writer"] == "x"
    assert subst["model_identity_ok"] is False
    fixture = k4.model_identity_fields({"attempts": [
        {"stage": "writer.0", "requested_model": None, "actual_model": "fx"}]})
    assert fixture["model_identity_ok"] is False, "无请求侧记录不得冒充可证"
    multi = k4.model_identity_fields({"attempts": [
        {"stage": "writer.0", "requested_model": W, "actual_model": "b"},
        {"stage": "writer.0.retry", "requested_model": W, "actual_model": "a"}]})
    assert multi["models_actual"]["writer"] == ["a", "b"]
    assert multi["model_identity_ok"] is False
    assert k4.model_identity_fields({})["model_identity_ok"] is False


def test_legacy_calls_table_migrates_additively(tmp_path):
    """旧世界库（改前 9 列 calls 表）逐字可开：加性 ALTER 补列、既有行
    原样保留、RUNTIME_VERSION 不抬（历史 k4_worlds 产物仍可审计重放）。"""
    legacy = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy) as db:
        db.executescript(f"""
            CREATE TABLE runtime_meta(version TEXT NOT NULL);
            CREATE TABLE jobs(
              id TEXT PRIMARY KEY, book TEXT, branch TEXT, scene TEXT,
              idem TEXT, request_hash TEXT NOT NULL, request TEXT NOT NULL,
              context TEXT NOT NULL, status TEXT NOT NULL, verified TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(book,branch,idem));
            CREATE TABLE calls(
              job TEXT, stage TEXT, request_hash TEXT NOT NULL, status TEXT NOT NULL,
              request TEXT NOT NULL, response TEXT, error TEXT,
              started_at TEXT NOT NULL, duration_ms INTEGER,
              PRIMARY KEY(job,stage), FOREIGN KEY(job) REFERENCES jobs(id));
            INSERT INTO runtime_meta VALUES('{RUNTIME_VERSION}');
            INSERT INTO jobs VALUES('scene-old','b','main','s','k','h','{{}}','{{}}',
                                    'committed',NULL,'2026-01-01T00:00:00+00:00',
                                    '2026-01-01T00:00:00+00:00');
            INSERT INTO calls VALUES('scene-old','writer.0','h','succeeded','{{}}',
                                     NULL,NULL,'2026-01-01T00:00:00+00:00',5);
        """)
    store = Store(legacy)                    # 打开即迁移，不抛 unsupported_runtime_schema
    with store.connection() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(calls)")}
        kept = db.execute("SELECT job,stage,status,duration_ms FROM calls").fetchone()
    assert {"requested_model", "actual_model", "model_substituted"} <= cols
    assert tuple(kept) == ("scene-old", "writer.0", "succeeded", 5)
    Store(legacy)                            # 再开一次：迁移幂等不报错
    with store.connection() as db:
        assert db.execute("SELECT version FROM runtime_meta").fetchone()[0] == RUNTIME_VERSION


def test_k4_live_receipts_carry_identity_fields(tmp_path):
    """live 逐臂收据：新增 models_actual/model_identity_ok + 每次 verifier
    尝试的 model_actual；models 语义不变（=请求）；usage 带 contract_retries。
    离线收据键集既有契约（test_k4_worlds_dir_receipt 钉死）逐字不变。"""
    seed_knowledge()
    n = {"i": 0}

    def factory():
        n["i"] += 1
        store = Store(tmp_path / f"wd{n['i']}" / "k4.sqlite")
        store.create_world(k4.build_world())
        return store

    wd = tmp_path / "worlds"
    wd.mkdir()
    with db.session() as s:
        live = k4.run_paired(factory, k4.FxClient(), s, live=True, freeze=False,
                             worlds_dir=str(wd), worlds_created_at="2026-10-01T00:00:00Z")
    assert not live["failures"], live["failures"]
    for r in live["receipts"]:
        assert set(r["models"]) == {"writer", "verifier"}, "models 语义不变"
        assert r["models"] == {"writer": "fx-w", "verifier": "fx-v"}
        assert r["models_actual"] == {"writer": "fx", "verifier": "fx"}
        # 夹具通道≠真实身份证明：fail-closed 记 False，绝不为凑绿放宽。
        assert r["model_identity_ok"] is False
        assert r["usage"]["contract_retries"] == 0
        assert all("model_actual" in a for a in r["verifier_attempts"])
    dirs2 = {"i": 0}

    def factory2():
        dirs2["i"] += 1
        store = Store(tmp_path / f"off{dirs2['i']}" / "k4.sqlite")
        store.create_world(k4.build_world())
        return store
    with db.session() as s:
        off = k4.run_paired(factory2, k4.FxClient(), s, live=False, freeze=False)
    assert not off["failures"], off["failures"]
    for r in off["receipts"]:
        assert "models_actual" not in r and "model_identity_ok" not in r, \
            "离线收据形状是既有契约（键集钉死于 test_k4_worlds_dir_receipt），不得漂移"
