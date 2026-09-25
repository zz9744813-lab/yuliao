"""K4 live 收据补记 worlds_dir（审计非阻断项收口，2026-09-25）回归钉。

验收点（任务 docs/K4_世界目录收据_20260924.md）：
① live 收据（four["receipts"] 逐条）含 worlds_dir / worlds_dir_exists /
   created_at 三字段，worlds_dir 为绝对路径、created_at 为 ISO8601 UTC；
② 默认（非 live）路径不产生新字段副作用：离线收据键集与产物顶层键集与
   改动前逐字一致（离线清理行为也不变——用后即删）；
③ 临时目录清理不越界：live 正常收口留库（worlds_dir_cleaned=False，
   tokens 对账要读）；live 异常/中断且收据未落盘 → 只清理本次 mkdtemp
   自建目录，同父目录下的他人目录分毫不动。

纪律：live 路径用例全程把 mkdtemp 换绑到 tmp_path + 假 client（FxClient
顶替 GatewayClient）+ freeze=False 强绑——零真实调用、零 LG 库冻结写、
不碰真库 `data/language_genome.db`（conftest 本已将测试库指到临时 SQLite）。
"""
import importlib.util as _u
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location("k4", ROOT / "scripts" / "k4_paired_scenes.py")
k4 = _u.module_from_spec(_spec)
_spec.loader.exec_module(k4)

from knowledge_seed import seed_knowledge          # noqa: E402
from app import db, config as _cfg                 # noqa: E402
from app.scene_runtime.store import Store          # noqa: E402

# 改动前（db67d88）离线收据与产物顶层的既有键集——逐字钉死，多一键即红
BASE_RECEIPT_KEYS = {"scene", "arm", "job_id", "usage", "live",
                     "channel_changed", "gateway_host", "models",
                     "retried", "verifier_attempts"}
BASE_OUT_KEYS = {"artifacts", "analysis", "live", "channel_changed",
                 "worlds_dir"}
ISO8601_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _factory(tmp_path, tag="wd"):
    n = {"i": 0}

    def make():
        n["i"] += 1
        store = Store(tmp_path / f"{tag}{n['i']}" / "k4.sqlite")
        store.create_world(k4.build_world())
        return store
    return make


def _fake_mkdtemp_under(tmp_path, created):
    """把 k4 里的 tempfile.mkdtemp 换绑到 tmp_path 下并登记产物目录——
    测试进程绝不往系统临时目录丢东西，清理断言全部可核。"""
    real = tempfile.mkdtemp

    def fake(*args, **kw):
        kw["dir"] = str(tmp_path)
        d = real(*args, **kw)
        created.append(d)
        return d
    return fake


# ── ① live 收据三字段 ──────────────────────────────────────────────────────

def test_live_receipts_carry_worlds_dir_fields(tmp_path):
    """live 收据逐条含三新字段：worlds_dir 绝对路径且指向世界目录、
    worlds_dir_exists 为 bool、created_at 为 ISO8601 UTC（Z 后缀）。"""
    seed_knowledge()
    wd = tmp_path / "worlds"
    wd.mkdir()
    ts = "2026-09-25T00:00:00Z"
    with db.session() as s:
        four = k4.run_paired(_factory(tmp_path), k4.FxClient(), s,
                             live=True, freeze=False,
                             worlds_dir=str(wd), worlds_created_at=ts)
    assert not four["failures"], four["failures"]
    assert len(four["receipts"]) == 6
    for r in four["receipts"]:
        assert r["live"] is True
        assert r["worlds_dir"] == str(wd.resolve())
        assert Path(r["worlds_dir"]).is_absolute(), "必须记绝对路径"
        assert r["worlds_dir_exists"] is True and \
            isinstance(r["worlds_dir_exists"], bool)
        assert r["created_at"] == ts
        assert ISO8601_UTC.match(r["created_at"]), r["created_at"]


def test_live_without_worlds_dir_refuses_to_run(tmp_path):
    """纪律钉：live 缺 worlds_dir/worlds_created_at 即拒跑（ValueError）——
    不存在「真跑了但收据没地址」的第三条路。"""
    seed_knowledge()
    with db.session() as s:
        with pytest.raises(ValueError, match="worlds_dir"):
            k4.run_paired(_factory(tmp_path), k4.FxClient(), s,
                          live=True, freeze=False)
        with pytest.raises(ValueError, match="worlds_dir"):
            k4.run_paired(_factory(tmp_path), k4.FxClient(), s,
                          live=True, freeze=False, worlds_dir=str(tmp_path))


def test_deleted_worlds_dir_reflected_in_receipt(tmp_path):
    """worlds_dir_exists 是写收据时的实况：目录已不在即 False（收据不撒谎）。"""
    seed_knowledge()
    wd = tmp_path / "gone"
    wd.mkdir()
    with db.session() as s:
        four = k4.run_paired(_factory(tmp_path, "g"), k4.FxClient(), s,
                             live=True, freeze=False,
                             worlds_dir=str(wd / "sub"),   # 从未创建
                             worlds_created_at="2026-09-25T00:00:00Z")
    assert not four["failures"], four["failures"]
    assert len(four["receipts"]) == 6      # 非空前提（防空断言 vacuous）
    assert all(r["worlds_dir_exists"] is False
               for r in four["receipts"]), four["receipts"]


# ── ② 默认（非 live）路径逐字不变 ──────────────────────────────────────────

def test_offline_receipts_and_output_keys_unchanged(tmp_path):
    """离线收据键集=改动前逐字（无 worlds_dir/worlds_dir_exists/
    created_at/worlds_dir_cleaned）；即使调用方多传 worlds_dir 参数也不
    消费（行为不变）。顶层产物键集同样不变。"""
    seed_knowledge()
    with db.session() as s:
        four = k4.run_paired(_factory(tmp_path, "off"), k4.FxClient(), s,
                             live=False)
        four2 = k4.run_paired(_factory(tmp_path, "off2"), k4.FxClient(), s,
                              live=False,                 # 多传也不消费
                              worlds_dir=str(tmp_path),
                              worlds_created_at="2026-09-25T00:00:00Z")
    assert len(four["receipts"]) == 6
    for r in four["receipts"] + four2["receipts"]:
        assert set(r) == BASE_RECEIPT_KEYS, \
            f"离线收据出现新字段：{set(r) - BASE_RECEIPT_KEYS}"


def test_offline_main_output_unchanged_and_dir_cleaned(tmp_path, monkeypatch):
    """main 离线跑：顶层键集不变、收据无新字段；worlds_dir 记入产物且
    用后即清（既有清理行为保持——目录已不存在）。"""
    seed_knowledge()
    out_dir = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["k4", "--out", str(out_dir)])
    k4.main()
    art = json.loads((out_dir / "k4_paired.json").read_text(encoding="utf-8"))
    assert set(art) == BASE_OUT_KEYS, set(art) - BASE_OUT_KEYS
    assert art["live"] is False
    for r in art["artifacts"]["receipts"]:
        assert set(r) == BASE_RECEIPT_KEYS
    assert Path(art["worlds_dir"]).is_absolute()
    assert not Path(art["worlds_dir"]).exists(), "离线临时目录必须已清理"


# ── ③ live 路径：正常收口留库、异常中断保守清理 ────────────────────────────

def _wire_fake_live(monkeypatch, tmp_path, created):
    """live 测试公共接线：双闸环境 + LLM_MODE=mock（预检闸不适用，与
    test_live_double_gate 同口径）+ mkdtemp 换绑 tmp_path +
    GatewayClient 换 FxClient + freeze 强绑 False（零 LG 库写）。"""
    monkeypatch.setenv("K4_ALLOW_LIVE", "1")
    assert _cfg.LLM_MODE == "mock"      # 前提钉死，不赌环境默认值
    monkeypatch.setattr(k4.tempfile, "mkdtemp",
                        _fake_mkdtemp_under(tmp_path, created))
    import app.scene_runtime.client as client_mod
    monkeypatch.setattr(client_mod, "GatewayClient",
                        lambda w, v: k4.FxClient())
    orig = k4.run_paired
    monkeypatch.setattr(k4, "run_paired",
                        lambda *A, **K: orig(*A, **dict(K, freeze=False)))


def test_live_main_keeps_worlds_dir_marks_not_cleaned(tmp_path, monkeypatch):
    """live 正常收口：收据逐条带四字段，worlds_dir_cleaned=False（留库
    供 tokens 对账），目录确实还在且就是 mkdtemp 自建的那个。"""
    created = []
    _wire_fake_live(monkeypatch, tmp_path, created)
    out_dir = tmp_path / "out_live"
    monkeypatch.setattr(sys, "argv", ["k4", "--live",
                                      "--writer-model", "a",
                                      "--verifier-model", "b",
                                      "--out", str(out_dir)])
    k4.main()
    art = json.loads((out_dir / "k4_paired.json").read_text(encoding="utf-8"))
    assert art["live"] is True
    assert created, "前提：mkdtemp 走的是本测试换绑的假实现"
    for r in art["artifacts"]["receipts"]:
        assert r["worlds_dir"] == str(Path(created[0]).resolve())
        assert r["worlds_dir_exists"] is True
        assert ISO8601_UTC.match(r["created_at"]), r["created_at"]
        assert r["worlds_dir_cleaned"] is False, \
            "正常收口 live 留库作收据——不许报已清理"
    assert Path(created[0]).is_dir(), "正常收口后世界目录必须仍在（对账）"


def test_aborted_live_cleans_only_own_dir(tmp_path, monkeypatch, capsys):
    """泄漏路径收口：live 中途异常、收据从未落盘 → 只删本次 mkdtemp 自建
    目录；同父目录下他人目录（含同名结构）分毫不动；清理事实记入 stderr。"""
    created = []
    _wire_fake_live(monkeypatch, tmp_path, created)
    # 他人目录：与自建目录同父、内含文件——保守清理绝不允许碰它
    other = tmp_path / "someone_elses_data"
    (other / "arm1").mkdir(parents=True)
    marker = other / "arm1" / "k4.sqlite"
    marker.write_text("do not touch", encoding="utf-8")
    sibling_store = tmp_path / "wd1"      # 上一次跑的残留，也不许扫到
    sibling_store.mkdir()

    def _boom(*A, **K):
        raise RuntimeError("boom-abort-midway")
    monkeypatch.setattr(k4, "run_paired", _boom)
    monkeypatch.setattr(sys, "argv", ["k4", "--live",
                                      "--writer-model", "a",
                                      "--verifier-model", "b"])
    with pytest.raises(RuntimeError, match="boom-abort-midway"):
        k4.main()
    assert created, "前提：自建目录确实被创建过（泄漏源存在）"
    for d in created:
        assert not Path(d).exists(), f"泄漏：中断后自建临时目录仍在 {d}"
    assert marker.exists() and \
        marker.read_text(encoding="utf-8") == "do not touch", \
        "清理越界：动了他人目录"
    assert sibling_store.is_dir(), "清理越界：动了同父下非本次自建的目录"
    err = capsys.readouterr().err
    assert "worlds_dir_cleaned=true" in err, err
