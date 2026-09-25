"""两档令牌（评审档/管理档）权限分离回归（审计 P1「评审令牌可调用任意文件导入」
权限面收口，2026-09-25）。

背景单令牌口径：`POST /corpus/import-file`、`POST /corpus/import-distiller` 与
`POST /review/{id}/verdict` **共用同一令牌** ⇒ 评审者能导入任意（根内、白名单、
合规大小的）文件，在权限分离意义上越权。本次收口为两档（见 app/access.py）：

- 评审档（`REVIEW_TOKEN` / `data/review_token.txt`）：只读页面 + 评审端点；
- 管理档（`ADMIN_TOKEN` / `data/admin_token.txt`）：评审档的全部权限 **＋**
  建段/扩产入口（corpus 三个导入 + `POST /experiments` + `POST /experiments/*/run`）。
- 管理档 ⊇ 评审档；评审令牌**不能**通过任何管理端点（含 `?t=` / cookie 等价路径）；
  `REVIEW_NO_AUTH=1` = 两档同时失效；比较恒走 `hmac.compare_digest`。

本文件锁死：
1. 只带评审档令牌 → 评审端点 200，导入/建段/扩产端点 403（`?t=` 阶段 302 不执行）；
2. 只带管理档令牌 → 评审端点与导入端点都成功；
3. 两档不相等时，评审令牌过不了管理端点的任何等价路径（`?t=` 换 cookie + cookie 直带）；
4. 无令牌 → 一律 401（`tests/test_access_gate.py` 行为不回归）；
5. `REVIEW_NO_AUTH=1` → 两档同时失效（显式关闭语义保持）；
6. 令牌文件读取/生成/持久化协议不回归（env > 文件 > 自动生成并持久化 + chmod 600），
   管理档与评审档互相独立且默认文件路径不同；
7. 兼容期开关 `LG_ADMIN_LEGACY_SHARED=1` 是唯一允许「共用」的通道，且启动自检响亮打印。

隔离纪律：令牌文件路径一律 monkeypatch 到 `tmp_path`，**绝不读写真实
`data/review_token.txt` / `data/admin_token.txt`**（conftest 的 `REVIEW_NO_AUTH=1`
已保证 import 阶段不落任何真实文件）。全程离线，不发模型请求。
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import access as A   # noqa: E402


# ── 公共件 ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _restore_tokens():
    """`_build()` 会改模块全局 `_TOKEN` / `_ADMIN_TOKEN`；不还原会污染其他
    测试模块（表现为"别的文件莫名 401/403"），故 autouse 强制还原。"""
    saved_r, saved_a = A._TOKEN, A._ADMIN_TOKEN
    yield
    A._TOKEN, A._ADMIN_TOKEN = saved_r, saved_a


def _build(host: str = "203.0.113.9", review: str | None = "RV",
           admin: str | None = "AD"):
    """构造一个含访问门的 app，带一个评审端点与一个管理端点（模拟真实路由），
    并把 scope 的客户端地址改写成 `host`。与 test_access_gate 同款薄 ASGI
    包装：真走完整中间件链。"""
    A._TOKEN = review
    A._ADMIN_TOKEN = admin
    inner = FastAPI()

    @inner.post("/corpus/import-file")
    def _import():
        # 模拟真实导入端点：真能到这里才返回 imported=1
        return {"imported": 1}

    @inner.post("/review/{rid}/verdict")
    def _verdict(rid: str):
        return {"ok": True, "resolved": "tie", "review_id": rid}

    A.install(inner)

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (host, 12345)
        await inner(scope, receive, send)

    return TestClient(wrapped)


# ── ① 档位判定（_grade）──────────────────────────────────────

def test_grade_review_and_admin_distinct(monkeypatch):
    monkeypatch.setattr(A, "_TOKEN", "RV")
    monkeypatch.setattr(A, "_ADMIN_TOKEN", "AD")
    assert A._grade("RV") == "review"
    assert A._grade("AD") == "admin"
    assert A._grade("X") is None
    assert A._grade("") is None
    assert A._grade(None) is None


def test_grade_review_never_masquerades_as_admin(monkeypatch):
    """评审令牌判档必须精准落在 review——若被加成 admin 即升级漏洞。"""
    monkeypatch.setattr(A, "_TOKEN", "RV")
    monkeypatch.setattr(A, "_ADMIN_TOKEN", "AD")
    assert A._grade("RV") == "review"
    assert A._grade("RV") != "admin"


def test_grade_admin_satisfies_review(monkeypatch):
    """管理档令牌是评审档的超集：判档为 admin，且经 admin 命中先返回。"""
    monkeypatch.setattr(A, "_TOKEN", "RV")
    monkeypatch.setattr(A, "_ADMIN_TOKEN", "AD")
    assert A._grade("AD") == "admin"


def test_grade_no_auth_returns_none_for_all(monkeypatch):
    monkeypatch.setattr(A, "_TOKEN", None)
    monkeypatch.setattr(A, "_ADMIN_TOKEN", None)
    assert A._grade("ANYTHING") is None


# ── ② 管理端点档位映射（requires_admin）─────────────────────

def test_requires_admin_import_and_expansion_endpoints():
    for p in ("/corpus/import-inbox", "/corpus/import-file",
              "/corpus/import-distiller", "/experiments",
              "/experiments/EXP-abc123/run"):
        assert A.requires_admin("POST", p) is True, f"{p} 应要求管理档"


def test_requires_admin_review_and_read_endpoints_are_not_admin():
    for p in ("/review/ri1/verdict", "/knowledge/query", "/works",
              "/segments", "/corpus/stats", "/review/batch/b1",
              "/review/next", "/experiments/EXP-1/review",
              "/experiments/EXP-1/report"):
        assert A.requires_admin("POST", p) is False, f"{p} 应只到评审档"
    # 同路径 GET/PUT 一律不算管理档
    for p in ("/experiments", "/corpus/import-file"):
        assert A.requires_admin("GET", p) is False, p
    assert A.requires_admin("PUT", "/corpus/import-file") is False


def test_real_api_post_routes_fully_covered_by_tiers():
    """真实 `app/main:app` 的全部 POST 路由必须被档位清单完全覆盖、无漂移。

    管理档 = corpus 三导入 + 建实验 + run 实验；评审档写入口 = verdict +
    /knowledge/query（只读查询）。清单外出现新 POST = 本测试直接红，
    防"忘了给新高风险端点定档"的静默事故。"""
    from app.main import app as real_app

    post_paths = set()
    for r in real_app.routes:
        methods = getattr(r, "methods", None) or set()
        if "POST" in methods:
            post_paths.add(r.path)
    assert post_paths, "应发现至少一个 POST 路由（introspection 失败）"

    expected_admin = set(A._ADMIN_ONLY_POST) | {"/experiments/{exp_id}/run"}
    expected_review = {"/review/{review_id}/verdict", "/knowledge/query"}
    assert post_paths == (expected_admin | expected_review), (
        f"真实路由与两档清单漂移：{post_paths}")
    for p in expected_admin:
        assert A.requires_admin("POST", p) is True
    for p in expected_review:
        assert A.requires_admin("POST", p) is False


# ── ③ 中间件行为（真走完整中间件链）─────────────────────────

def test_review_token_review_ok_import_403():
    """只带评审档令牌：评审端点 200，导入端点 403。"""
    c = _build()
    c.cookies.set(A.COOKIE, "RV")
    assert c.post("/review/r1/verdict", json={}).status_code == 200
    assert c.post("/corpus/import-file", json={}).status_code == 403


def test_admin_token_both_ok():
    """只带管理档令牌：评审端点与导入端点都成功（管理员不必拿两个令牌）。"""
    c = _build()
    c.cookies.set(A.COOKIE, "AD")
    assert c.post("/corpus/import-file", json={}).status_code == 200
    assert c.post("/review/r1/verdict", json={}).status_code == 200


def test_review_cookie_never_upgraded_to_admin():
    """评审 cookie 携带的档位 = 评审档：导入端点不许因 cookie 被自动升级。"""
    c = _build()
    r = c.get("/?t=RV", follow_redirects=False)
    assert r.status_code == 302 and A.COOKIE in r.headers.get("set-cookie", "")
    c.cookies.set(A.COOKIE, "RV")
    assert c.post("/review/r1/verdict", json={}).status_code == 200
    assert c.post("/corpus/import-file", json={}).status_code == 403


def test_review_query_param_on_import_redirects_then_cookie_403():
    """`?t=<评审令牌>` 打到导入端点：先在换 cookie 阶段被 302 截住（端点不执行），
    换到的 cookie 仍是评审档 → 后续请求 403。即评审令牌没有任何等价路径能
    完成导入。"""
    c = _build()
    r = c.post("/corpus/import-file?t=RV", json={}, follow_redirects=False)
    assert r.status_code == 302, "?t= 阶段端点不得被执行（应 302 换 cookie）"
    assert "imported" not in r.text
    c.cookies.set(A.COOKIE, "RV")
    assert c.post("/corpus/import-file", json={}, follow_redirects=False).status_code == 403


def test_admin_query_param_cookie_path_unlocks_import():
    """管理令牌走 ?t= 换 cookie：cookie 阶段导入端点放行。"""
    c = _build()
    r = c.get("/?t=AD", follow_redirects=False)
    assert r.status_code == 302
    c.cookies.set(A.COOKIE, "AD")
    assert c.post("/corpus/import-file", json={}).status_code == 200


def test_no_token_all_tier_endpoints_401():
    """无令牌 → 一律 401（既有 test_access_gate 行为不回归）。"""
    c = _build()
    assert c.get("/").status_code == 401
    assert c.post("/corpus/import-file", json={}).status_code == 401
    assert c.post("/review/r1/verdict", json={}).status_code == 401


def test_wrong_token_and_cookie_401():
    c = _build()
    assert c.get("/?t=WRONG").status_code == 401
    c.cookies.set(A.COOKIE, "WRONG")
    assert c.post("/corpus/import-file", json={}).status_code == 401
    assert c.post("/review/r1/verdict", json={}).status_code == 401


def test_loopback_bypass_skips_both_tiers(monkeypatch):
    """显式 LG_LOCAL_BYPASS=1 的本机直连依旧免令牌（两档都不过问）——
    R2 既有语义，不代表鉴权降级，只是开关本身的定义域。"""
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    c = _build(host="127.0.0.1")
    assert c.post("/corpus/import-file", json={}).status_code == 200
    assert c.post("/review/r1/verdict", json={}).status_code == 200
    # 带代理头的回环仍要令牌（一票否决）
    c2 = _build(host="127.0.0.1")
    assert c2.post("/corpus/import-file", json={},
                   headers={"CF-Connecting-IP": "203.0.113.9"}).status_code == 401


# ── ④ 令牌读取/生成/持久化协议（管理档与评审档独立）─────────

def test_admin_env_token_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "ADMIN_TOKEN_PATH", tmp_path / "admin.txt")
    monkeypatch.delenv("REVIEW_NO_AUTH", raising=False)
    monkeypatch.delenv("LG_ADMIN_LEGACY_SHARED", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "FROM-ENV-ADMIN")
    assert A.load_admin_token() == "FROM-ENV-ADMIN"


def test_admin_file_token_used_when_no_env(tmp_path, monkeypatch):
    p = tmp_path / "admin.txt"
    monkeypatch.setattr(A, "ADMIN_TOKEN_PATH", p)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("REVIEW_NO_AUTH", raising=False)
    monkeypatch.delenv("LG_ADMIN_LEGACY_SHARED", raising=False)
    p.write_text("FROM-FILE-ADMIN\n", encoding="utf-8")
    assert A.load_admin_token() == "FROM-FILE-ADMIN"


def test_admin_token_generated_persisted_and_stable(tmp_path, monkeypatch):
    """管理令牌与评审令牌同款协议：自动生成并持久化，重启不变。"""
    p = tmp_path / "admin.txt"
    monkeypatch.setattr(A, "ADMIN_TOKEN_PATH", p)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("REVIEW_NO_AUTH", raising=False)
    monkeypatch.delenv("LG_ADMIN_LEGACY_SHARED", raising=False)
    t1 = A.load_admin_token()
    t2 = A.load_admin_token()
    assert t1 and t1 == t2, "同文件两次读取必须一致"
    assert p.read_text(encoding="utf-8").strip() == t1
    assert p.read_text(encoding="utf-8").endswith("\n")


def test_admin_and_review_tokens_generated_independently(tmp_path, monkeypatch):
    """两档各自落自己的文件、值互不相同——绝不共用同一份令牌。"""
    rp = tmp_path / "review.txt"
    ap = tmp_path / "admin.txt"
    monkeypatch.setattr(A, "TOKEN_PATH", rp)
    monkeypatch.setattr(A, "ADMIN_TOKEN_PATH", ap)
    monkeypatch.delenv("REVIEW_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("REVIEW_NO_AUTH", raising=False)
    monkeypatch.delenv("LG_ADMIN_LEGACY_SHARED", raising=False)
    rv = A.load_token()
    ad = A.load_admin_token()
    assert rv and ad
    assert rv != ad, "两档令牌不得自动相等（评审令牌不能当管理令牌用）"
    assert rp.read_text(encoding="utf-8").strip() == rv
    assert ap.read_text(encoding="utf-8").strip() == ad
    assert rp.read_text(encoding="utf-8") != ap.read_text(encoding="utf-8")


def test_default_token_paths_are_siblings_with_distinct_names():
    """默认路径：同一 data/ 目录下 review_token.txt 与 admin_token.txt 两个文件。"""
    assert A.TOKEN_PATH.parent == A.ADMIN_TOKEN_PATH.parent
    assert A.TOKEN_PATH.name == "review_token.txt"
    assert A.ADMIN_TOKEN_PATH.name == "admin_token.txt"
    assert A.TOKEN_PATH != A.ADMIN_TOKEN_PATH


# ── ⑤ REVIEW_NO_AUTH=1：两档同时失效 ─────────────────────────

def test_no_auth_env_nils_both_tiers(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "TOKEN_PATH", tmp_path / "review.txt")
    monkeypatch.setattr(A, "ADMIN_TOKEN_PATH", tmp_path / "admin.txt")
    monkeypatch.setenv("REVIEW_NO_AUTH", "1")
    monkeypatch.setenv("REVIEW_TOKEN", "SHOULD-IGNORE")
    monkeypatch.setenv("ADMIN_TOKEN", "SHOULD-IGNORE")
    monkeypatch.setenv("LG_ADMIN_LEGACY_SHARED", "1")
    assert A.load_token() is None
    assert A.load_admin_token() is None
    assert not (tmp_path / "review.txt").exists()
    assert not (tmp_path / "admin.txt").exists(), "显式关闸时不得自动生成令牌文件"


# ── ⑥ 兼容期开关：唯一的"共用"通道必须显式 ───────────────────

def test_legacy_shared_requires_explicit_switch(tmp_path, monkeypatch):
    """开关开 + 未显式配置 ADMIN_TOKEN → 管理档共用评审档令牌（仅此一条通道）。"""
    monkeypatch.setattr(A, "ADMIN_TOKEN_PATH", tmp_path / "admin.txt")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("REVIEW_NO_AUTH", raising=False)
    monkeypatch.setenv("LG_ADMIN_LEGACY_SHARED", "1")
    monkeypatch.setattr(A, "_TOKEN", "RV-TOKEN")
    assert A.load_admin_token() == "RV-TOKEN"


def test_legacy_off_never_returns_review_token(tmp_path, monkeypatch):
    """默认（无开关）绝不回退复用评审令牌：生成独立管理令牌。"""
    monkeypatch.setattr(A, "ADMIN_TOKEN_PATH", tmp_path / "admin.txt")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("REVIEW_NO_AUTH", raising=False)
    monkeypatch.delenv("LG_ADMIN_LEGACY_SHARED", raising=False)
    monkeypatch.setattr(A, "_TOKEN", "RV-TOKEN")
    ad = A.load_admin_token()
    assert ad and ad != "RV-TOKEN"


def test_legacy_switch_value_is_strict(monkeypatch):
    """取值口径从严：只有字面 "1" 算开（与 LG_LOCAL_BYPASS 同纪律）。"""
    monkeypatch.delenv("LG_ADMIN_LEGACY_SHARED", raising=False)
    assert A.admin_legacy_shared() is False
    for bad in ("0", "yes", "true", "on", " 1", "01"):
        monkeypatch.setenv("LG_ADMIN_LEGACY_SHARED", bad)
        assert A.admin_legacy_shared() is False, f"{bad!r} 不得算开"
    monkeypatch.setenv("LG_ADMIN_LEGACY_SHARED", "1")
    assert A.admin_legacy_shared() is True


def test_legacy_admin_cookie_passes_import(monkeypatch):
    """兼容期开关开时评审令牌也能进管理端点（显式迁移期的预期口径）。

    开关生效后 `load_admin_token()` 返回评审令牌值（= `_ADMIN_TOKEN == _TOKEN`），
    这里按该真实结果建模；加载协议本身由 ⑥ 的两个加载器用例单独钉死。"""
    monkeypatch.setenv("LG_ADMIN_LEGACY_SHARED", "1")
    c = _build(review="RV", admin="RV")
    c.cookies.set(A.COOKIE, "RV")
    assert c.post("/review/r1/verdict", json={}).status_code == 200
    assert c.post("/corpus/import-file", json={}).status_code == 200


# ── ⑦ 启动自检如实打印两档状态 ───────────────────────────────

def test_self_check_prints_both_tiers_and_status(monkeypatch, capsys):
    monkeypatch.delenv("LG_ADMIN_LEGACY_SHARED", raising=False)
    monkeypatch.setattr(A, "_TOKEN", "RV")
    monkeypatch.setattr(A, "_ADMIN_TOKEN", "AD")
    A.self_check()
    out = capsys.readouterr().out
    assert "REVIEW_TOKEN" in out and "ADMIN_TOKEN" in out
    assert "评审档" in out and "管理档" in out
    assert "已配置" in out
    assert "import-distiller" in out and "/run" in out


def test_self_check_loudly_prints_legacy_shared(monkeypatch, capsys):
    monkeypatch.setattr(A, "_TOKEN", "RV")
    monkeypatch.setattr(A, "_ADMIN_TOKEN", None)
    monkeypatch.setenv("LG_ADMIN_LEGACY_SHARED", "1")
    A.self_check()
    out = capsys.readouterr().out
    assert "LG_ADMIN_LEGACY_SHARED" in out, "兼容期开关必须响亮打印"
    assert "共用" in out


def test_self_check_no_auth_prints_both_disabled(monkeypatch, capsys):
    monkeypatch.setattr(A, "_TOKEN", None)
    monkeypatch.setattr(A, "_ADMIN_TOKEN", None)
    A.self_check()
    out = capsys.readouterr().out
    assert "REVIEW_NO_AUTH" in out
    assert "同时失效" in out
    assert "已关闭" in out