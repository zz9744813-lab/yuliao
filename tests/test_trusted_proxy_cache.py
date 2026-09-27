"""可信代理白名单「解析缓存」回归（2026-09-27，R2b 收口：401 风暴放大面）。

背景（真事实）：`LG_TRUSTED_PROXIES` 白名单在改前**每次请求**都重跑
`os.environ.get` + 逐条 `ipaddress.ip_network(...)`——401 风暴（大量未授权请求）
下条目数一多就是**每请求重复解析**的放大面。本轮把解析结果**按 env 原文缓存**
（`app/access.py` 的 `_parse_trusted_proxies_cached` + `functools.lru_cache`），
同一份 env 原文只解析一次，原文一变（含改空）立即失效重算。

本文件钉死「缓存正确、且**没偷改判定语义**」两件事：

1. **①同一 env 连续调用 → 解析只发生 1 次**（用 `proxy_cache_info()` 的
   hits/misses 账本 + 命中返回**同一对象**双重钉死；misses 即真解析次数）；
2. **②env 原文变化 → 立即重算**（原文变 ⇒ 换键 ⇒ misses+1、命中不增，结果随新值变）；
3. **③env 清空 → 白名单空、`peer_is_trusted_proxy` 恒 False**（清空是换键的一种）；
4. **④`127.0.0.1/0` 仍落 bad 且永不放行**（防缓存把主机位置位写法洗白，
   反复命中后 bad 仍在——`strict=True` 语义不得因缓存而变）；
5. **⑤跨族 v4+v6 混填不抛**（沿用既有口径，缓存不得引入新异常面）。

另附两条加固：返回结构必须是**不可就地改的 tuple**（不得是被调用方改坏的 list）；
`proxy_cache_info()` 的计数**不得**是判定路径的副作用（读不读统计，放行结果一致）。

隔离：本文件只碰 `app/access.py` 的纯解析helpers 与只读计数，不 import
`app.main`/`app.db`；`conftest.py` 已置 `REVIEW_NO_AUTH=1`，不写真 `data/`。
每个用例前置 `proxy_cache_clear()` 把 lru_cache 账本归零，给定确定的计数起点。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import access as A   # noqa: E402

_ENV = A._TRUSTED_PROXIES_ENV   # "LG_TRUSTED_PROXIES"


@pytest.fixture(autouse=True)
def _cache_reset(monkeypatch):
    """每个用例：清空 lru_cache 账本（hits/misses 归零）+ 拉白名单回默认最严。

    `proxy_cache_clear()` 同时清条目与计数，故用例内 `proxy_cache_info()` 读到的
    hits/misses 就是**本用例自己**产生的增量，不受同轮其它模块调用污染。
    monkeypatch 负责用后还原 env，绝不把 LG_TRUSTED_PROXIES 漏给别的测试文件。
    """
    monkeypatch.delenv(_ENV, raising=False)
    A.proxy_cache_clear()
    yield
    A.proxy_cache_clear()


# ── ① 同一 env 原文连续调用 → 解析只发生 1 次 ──────────────────────────
def test_same_env_parses_exactly_once(monkeypatch):
    """核心：同一份 env 原文连调 50 次，真解析只跑 1 次（misses==1、hits==49）。

    401 风暴下每请求都调 `peer_is_trusted_proxy` —— 这里再用它跑三轮，确认
    解析次数**不随调用次数增长**（misses 恒为 1），且命中返回的是同一对象。
    """
    monkeypatch.setenv(_ENV, "127.0.0.1, 10.0.0.0/8 , 203.0.113.0/24")

    base = A.proxy_cache_info()
    assert (base.hits, base.misses) == (0, 0), "前置清空后账本应从 0 起算"

    first = A._parse_trusted_proxies()
    for _ in range(49):
        assert A._parse_trusted_proxies() is first, "命中缓存必须返回同一结果对象"

    # 再经判定入口压测三轮：不得触发任何一次新解析
    for host in ("127.0.0.1", "10.1.2.3", "203.0.113.9", "8.8.8.8"):
        for _ in range(10):
            A.peer_is_trusted_proxy(host)

    ci = A.proxy_cache_info()
    assert ci.misses == 1, f"同一原文只该真解析 1 次，实际 {ci.misses}"
    assert ci.hits == 49 + 40, f"后续调用应全部命中，实际 hits={ci.hits}"
    assert ci.currsize == 1, "只有 1 种 env 原文 ⇒ 缓存里只有 1 条"


# ── ② env 原文变化 → 立即失效重算（换键 ⇒ misses+1，结果随新值变）────────
def test_env_change_recomputes_immediately(monkeypatch):
    """原文 A→B：A 时 misses=1；换 B（新键）立即重算 ⇒ misses=2、命中不增，
    结果反映 B 的条目，A 的老 peer 不再命中。"""
    monkeypatch.setenv(_ENV, "127.0.0.1")
    good_a, _bad_a = A._parse_trusted_proxies()
    assert A.peer_is_trusted_proxy("127.0.0.1") is True
    ci_a = A.proxy_cache_info()
    assert (ci_a.hits, ci_a.misses) == (1, 1), "A 原文：1 次解析 + 1 次命中"

    # 换成另一份原文 ⇒ 换键 ⇒ 立刻重算（这一调用是 miss，不是 hit）
    monkeypatch.setenv(_ENV, "10.0.0.0/8")
    good_b, _bad_b = A._parse_trusted_proxies()
    ci_b = A.proxy_cache_info()
    assert ci_b.misses == 2, f"原文变了必须重算：misses 应增到 2，实际 {ci_b.misses}"
    assert ci_b.hits == ci_a.hits, "换键的那次调用是 miss，命中数不得增加"

    raws_b = tuple(r for r, _ in good_b)
    assert raws_b == ("10.0.0.0/8",), "结果必须随新值变"
    assert A.peer_is_trusted_proxy("10.1.2.3") is True
    assert A.peer_is_trusted_proxy("127.0.0.1") is False, "旧原文的条目不该再命中"


# ── ③ env 清空 → 白名单为空、peer_is_trusted_proxy 恒 False ─────────────
def test_env_cleared_yields_empty_and_always_false(monkeypatch):
    """先设非空并预热，再清空（含 "" 与 unset 两种写法）⇒ 白名单空、判定恒 False。

    清空是「换键到空原文」，缓存必须跟随新（空）原文重算，绝不把旧的合法条目
    残留在判定路径上。
    """
    monkeypatch.setenv(_ENV, "127.0.0.1")
    assert A.peer_is_trusted_proxy("127.0.0.1") is True

    for cleared in ("", "   ", None):
        if cleared is None:
            monkeypatch.delenv(_ENV, raising=False)
        else:
            monkeypatch.setenv(_ENV, cleared)
        good, bad = A._parse_trusted_proxies()
        assert good == () and bad == (), f"清空后白名单必须为空（cleared={cleared!r}）"
        assert A.trusted_proxies() == ()
        for host in ("127.0.0.1", "::1", "10.0.0.1", "localhost", ""):
            assert A.peer_is_trusted_proxy(host) is False, \
                f"env 清空后 peer_is_trusted_proxy 对 {host!r} 必须恒 False"
        # 判定链侧同一结论：带代理头的回环请求落 untrusted（需令牌）
        assert A.classify("127.0.0.1", True) == "untrusted_proxy"


# ── ④ 127.0.0.1/0 仍落 bad 且永不放行（防缓存把它洗白）──────────────────
def test_host_bits_entry_stays_bad_after_caching(monkeypatch):
    """`strict=True` 语义不得被缓存洗白：`127.0.0.1/0` 反复解析后始终落 bad、
    不进 good、不放行任何 v4 地址。"""
    monkeypatch.setenv(_ENV, "127.0.0.1/0")

    good1, bad1 = A._parse_trusted_proxies()
    assert good1 == (), "主机位被置位的条目绝不进白名单"
    assert len(bad1) == 1 and "127.0.0.1/0" in bad1[0]

    # 反复命中缓存后，bad 仍在（缓存不得把非法条目「洗」成合法或悄悄丢掉）
    for _ in range(20):
        good_n, bad_n = A._parse_trusted_proxies()
        assert good_n == () and bad_n == bad1

    # 归一放大若发生，任何 v4 都会命中——逐点钉死不放行
    for host in ("127.0.0.1", "10.1.2.3", "203.0.113.9", "255.255.255.255"):
        assert A.peer_is_trusted_proxy(host) is False

    ci = A.proxy_cache_info()
    assert ci.misses == 1, "非法条目的重算同样只发生一次（缓存覆盖 good+bad 整体）"


# ── ⑤ 跨族 v4+v6 混填不抛（缓存不得引入新异常面）────────────────────────
@pytest.mark.parametrize("mixed", ["127.0.0.1,::1/128", "::1/128,127.0.0.1"])
def test_cross_family_mixed_does_not_raise(monkeypatch, mixed):
    """白名单混填 v4+v6 时，跨族包含判定不得抛异常（否则中间件对全站 500）；
    且缓存路径与直解路径结论一致。"""
    monkeypatch.setenv(_ENV, mixed)
    assert A.peer_is_trusted_proxy("127.0.0.1") is True
    assert A.peer_is_trusted_proxy("::1") is True
    assert A.peer_is_trusted_proxy("203.0.113.9") is False

    # 跨族不得因异常被误判为可信：v6 网段不匹配 v4、v4 网段不匹配 v6
    monkeypatch.setenv(_ENV, "::1/128")
    assert A.peer_is_trusted_proxy("127.0.0.1") is False
    monkeypatch.setenv(_ENV, "127.0.0.0/8")
    assert A.peer_is_trusted_proxy("::1") is False


# ── 加固 1：返回结构不可被调用方就地改坏 ────────────────────────────────
def test_cached_result_is_immutable_tuple(monkeypatch):
    """缓存返回的必须是 tuple（元素也是 tuple），调用方无法就地 append 污染缓存。

    若哪天有人把它换成可变的 list，`good.append(...)` 会就地改掉缓存里的那份，
    下一个请求拿到被污染的结果——这是本用例要挡住的回归面。
    """
    monkeypatch.setenv(_ENV, "127.0.0.1, 10.0.0.0/8")
    good, bad = A._parse_trusted_proxies()
    assert isinstance(good, tuple) and isinstance(bad, tuple)
    assert all(isinstance(item, tuple) for item in good)
    with pytest.raises(AttributeError):
        good.append(("x", None))          # tuple 无 append ⇒ 就地污染不可能
    with pytest.raises(TypeError):
        bad[0] = "clobbered"              # tuple 不可下标赋值

    # 即便有人对返回元组「假装修改」，重新取仍是原值
    again = A._parse_trusted_proxies()
    assert again == (good, bad)


# ── 加固 2：命中计数不是判定路径的副作用 ────────────────────────────────
def test_cache_info_is_not_a_decision_side_effect(monkeypatch):
    """读不读 `proxy_cache_info()`，`peer_is_trusted_proxy` / `classify` 的结论逐字一致
    ——统计只读 lru_cache 自带账本，不在判定路径上挂任何计数器。"""
    monkeypatch.setenv(_ENV, "127.0.0.1, 10.0.0.0/8")

    probe_hosts = ("127.0.0.1", "10.9.9.9", "8.8.8.8", "::1")
    # 先「读统计」若干次，再取判定
    for _ in range(5):
        A.proxy_cache_info()
    with_stats = [(h, A.peer_is_trusted_proxy(h), A.classify(h, True)) for h in probe_hosts]
    # 清空账本重来：计数起点不同，但判定结论必须与读统计与否无关
    A.proxy_cache_clear()
    without_stats = [(h, A.peer_is_trusted_proxy(h), A.classify(h, True)) for h in probe_hosts]

    assert [r[1:] for r in with_stats] == [r[1:] for r in without_stats], \
        "判定结论不得受「有没有人查统计」影响"
