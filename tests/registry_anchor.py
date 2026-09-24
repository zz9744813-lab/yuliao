"""带内容锚的登记行——测试侧的**单一**哈希来源。

登记行的 ``text_sha256`` 是「登记时的事实」锚：
``verify_work_registry.verify()`` 的锚复核按当前库内容重算比对
（``(r.text_sha256 or None) != (sha or None)``，见
scripts/verify_work_registry.py:180）。测试夹具若给**有分段内容**的作品建
``WorkSource`` 却不带锚（列默认 NULL），在全量套件里会被 work_registry 的
对账判成 ``anchor_drift``——这是跨文件顺序依赖的假红根因：别的测试文件建的
遗留登记行（无锚）被 work_registry 的 clean_tree 扫到，而 clean_tree 的占位
登记**只补无登记行的 Work**（tests/test_work_registry.py:90-91 的
``if _worksource_exists: continue``），带锚缺失的遗留行不会被修复，于是每条
各报一次 ``anchor_drift``，把 ``n_mismatch`` 顶到非零。

修法只有一条纪律：所有「建登记行」的测试路径都经本模块取锚，杜绝再漏。

哈希口径唯一：全部复用生产实现 ``register_work_sources._work_sha256``，
测试侧绝不另写一份——分隔符 / text_clean 回退 / ordinal 序任一处不同都会让
锚与登记错位，造成假 PASS 或假漂移（与会审三轮「单一口径」同源）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import register_work_sources as REG  # noqa: E402


def work_sha256(s, work_id):
    """当前库内容对应的内容锚 ``(text_sha256, n_segments)``——单一来源。

    **必须在段已 flush 之后调用**：``app.db.SessionLocal`` 是
    ``autoflush=False``，未 flush 的 pending 段不会被本函数的查询看到，
    锚会恒为 ``None``（假锚）——主控实测：段在 pending 时 ``(None, 0)``，
    ``s.flush()`` 后才是真锚。0 段作品如实返回 ``(None, 0)``——锚为 NULL
    不是缺失，verify 的锚复核对 ``(None) != (None)`` 判为一致，不误报漂移。
    """
    return REG._work_sha256(s, work_id)


def anchor(s, work_id):
    """只要锚串本身，供 ``WorkSource(text_sha256=anchor(s, wid), ...)`` 用。"""
    return work_sha256(s, work_id)[0]


def refresh(s, work_id):
    """按当前库内容重算并写回**既有**登记行的锚。

    用于「先建登记行、之后才补段」的夹具（如 source_check 的 _seed_seg）——
    建行之时锚还对应不上后来的内容，补段后必须重锚，否则 verify 判漂移。
    无登记行则 no-op，返回是否刷新。
    """
    from app.models import WorkSource
    row = s.query(WorkSource).filter_by(work_id=work_id).first()
    if row is None:
        return False
    row.text_sha256 = anchor(s, work_id)
    s.flush()
    return True
