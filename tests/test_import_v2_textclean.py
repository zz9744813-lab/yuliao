"""导入 v2 `--clean` 开关回归（任务 lg-corpus-v2-textclean，2026-09-24）。

背景（主控实跑证据）：本脚本旧口径落段只写 text、从不写 text_clean，而 K2
抽段侧一律按 `Segment.text_clean` 非空取段 ⇒ 新导入的书天然进不了抽取池
（唯一合规试点源《覆汉》在 K2 池中 0 对）。

钉死五条：
1. 默认（不开开关）text_clean 仍全 NULL——默认行为逐字不变的回归红线；
2. 开开关后 text_clean 非空且 == clean_rules(text)（站点水印/空括号样本实证
   规则清洗真的发生）；清洗规则单源复用 scripts/clean_text.py，不在此另写一套；
3. 含拼音的段：text_clean 保留拼音（本步不送 LLM）+ 该段 integrity JSON 键
   clean_pending_llm 为真；干净的段不得混入该键；
4. 幂等：同一源跑两次不重写已有段（行 id / text_clean 原样）；partial 续跑
   路径写入口径与首导一致（补写的新段同样带 text_clean，已提交段 id 不变）；
5. --caveats 与 --clean 共存：各写各的键（anchors.front_matter / 段.chapter 归
   caveats；段.text_clean / integrity.clean_pending_llm 归 clean；truncated 与
   clean_pending_llm 同段落库互不覆盖）。

自包含：conftest 的临时 sqlite + tmp_path 造书，零网络、零真实库。
"""
from __future__ import annotations

import importlib.util as _u
import json
import sys
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = _u.spec_from_file_location("ic_tc", ROOT / "scripts" / "import_corpus_v2.py")
IC = _u.module_from_spec(_spec); _spec.loader.exec_module(IC)

_spec2 = _u.spec_from_file_location("ct_tc", ROOT / "scripts" / "clean_text.py")
CT = _u.module_from_spec(_spec2); _spec2.loader.exec_module(CT)

from app import db, segment_integrity as si  # noqa: E402
from app.models import Segment, Work          # noqa: E402

Row = namedtuple("Row", "id ordinal text chapter text_clean integrity")
_n = [0]


def _para(i) -> str:
    """干净的正文段（≥40 字、终止标点收尾、非风险开场、无引号）——同 caveats 测试口径。"""
    return (f"甲{i}：他把茶盏搁回去，半天没有说话，外头风声一阵紧过一阵。"
            f"隔壁屋的灯还亮着，影子在窗纸上晃了两下。")


def _wm_para(i) -> str:
    """带站点水印 + 空括号残留的段（clean_rules 实测能修的形态）。"""
    return (f"乙{i}：他把茶盏搁回去，(手打中文网7*24小时不间断更新纯txt手打小说m)"
            f"半天没有说话，外头风声一阵紧过一阵，()隔壁屋的灯还亮着。")


def _py_para(i) -> str:
    """拼音替换段（规则修不掉、须留待 LLM 的形态）：带调拼音 sè/lù。"""
    return (f"丙{i}：白sè的雾气从河面上lù出来，他神sè平静地看着远处的灯火，"
            f"站了很久也没有挪动一步。")


def _book(tmp_path: Path, paras: list[str]) -> str:
    _n[0] += 1
    fp = tmp_path / f"tc-{_n[0]}.txt"
    fp.write_text("\n\n".join(paras), encoding="utf-8")
    return str(fp)


def _work(path: str) -> Work:
    with db.session() as s:
        return s.query(Work).filter_by(source=f"file:{path}").one()


def _segs(work_id: str) -> list[Row]:
    """session 内取纯值（ORM 对象出 session 属性访问不可靠）。"""
    with db.session() as s:
        return [Row(g.id, g.ordinal, g.text, g.chapter, g.text_clean, g.integrity)
                for g in (s.query(Segment).filter(Segment.work_id == work_id)
                          .order_by(Segment.ordinal).all())]


# ── ① 回归红线：默认（不开开关）text_clean 仍全 NULL ─────────

def test_default_leaves_text_clean_null(tmp_path):
    path = _book(tmp_path, [_para(1), _wm_para(2), _py_para(3)])
    assert IC.main([path, "默认本", "训练语料"]) == 0
    segs = _segs(_work(path).id)
    assert len(segs) == 3
    assert all(g.text_clean is None for g in segs), \
        "不开开关必须逐字保持旧行为：text_clean 留 NULL（等 clean_text.py --rules）"
    base_keys = set(si.analyze("x", ordinal=0))
    for g in segs:
        assert set(json.loads(g.integrity)) == base_keys, \
            "默认路径 integrity 不得混入 clean_pending_llm 等新键"


# ── ② 开开关：text_clean == clean_rules(text) 且清洗真的发生 ──

def test_clean_flag_writes_rule_cleaned_text(tmp_path):
    path = _book(tmp_path, [_para(1), _wm_para(2)])
    assert IC.main([path, "清洗本", "训练语料", "--clean"]) == 0
    segs = _segs(_work(path).id)
    assert len(segs) == 2
    for g in segs:
        assert g.text_clean, "开开关后 text_clean 不得为 NULL/空串"
        assert g.text_clean == CT.clean_rules(g.text), \
            "text_clean 必须逐字等于单源 clean_rules 的结果，不许第二套口径"
    wm = next(g for g in segs if "手打中文网" in g.text)
    assert "手打中文网" not in wm.text_clean and "()" not in wm.text_clean, \
        "站点水印与空括号必须被规则洗掉"
    assert len(wm.text_clean) < len(wm.text)
    assert wm.text == _wm_para(2), "原文 text 不得被清洗改动（可审计口径）"
    # 本步不碰 LLM：默认关的 mock 网关都不该被调用——纯规则路径由上一条
    # text_clean == clean_rules(text) 精确相等即可证明（LLM 介入必然破坏该等式）


# ── ③ 拼音段：保留拼音 + integrity.clean_pending_llm ─────────

def test_pinyin_segment_kept_and_marked_pending(tmp_path):
    path = _book(tmp_path, [_para(1), _py_para(3)])
    assert IC.main([path, "拼音本", "训练语料", "--clean"]) == 0
    segs = _segs(_work(path).id)
    py = next(g for g in segs if "白sè" in g.text)
    assert "白sè" in py.text_clean and "lù" in py.text_clean, \
        "规则洗不掉的拼音必须原样保留——本步绝不送 LLM"
    assert py.text_clean == CT.clean_rules(py.text)
    assert CT.needs_llm(py.text_clean), "该段须被单源 needs_llm 判为待 LLM"
    flags = json.loads(py.integrity)
    assert flags.get("clean_pending_llm") is True, "待 LLM 段 integrity 必须留 JSON 标记"
    clean = next(g for g in segs if "白sè" not in g.text)
    assert "clean_pending_llm" not in json.loads(clean.integrity), \
        "规则已洗干净的段不得混入 clean_pending_llm 键（只标记真待洗的）"


# ── ④ 幂等：重复导入不重写；续跑路径口径一致 ─────────────────

def test_rerun_does_not_rewrite(tmp_path, capsys):
    path = _book(tmp_path, [_para(1), _wm_para(2), _py_para(3)])
    assert IC.main([path, "幂等本", "训练语料", "--clean"]) == 0
    before = _segs(_work(path).id)
    assert all(g.text_clean for g in before)
    capsys.readouterr()
    assert IC.main([path, "幂等本", "训练语料", "--clean"]) == 0
    assert "已导入过" in capsys.readouterr().out
    after = _segs(_work(path).id)
    assert after == before, "完整本重跑：段数、行 id、text_clean 必须一字不动"


def test_resume_path_writes_text_clean_consistently(tmp_path, capsys):
    """模拟分批提交炸掉留下的半本：已提交段（行 id 不变）不得重写，
    续跑补写的新段与首导同口径（text_clean == clean_rules(text)）。"""
    paras = [_para(1), _wm_para(2), _py_para(3), _para(4)]
    path = _book(tmp_path, paras)
    assert IC.main([path, "半本", "训练语料", "--clean"]) == 0
    w = _work(path)
    full = _segs(w.id)
    capsys.readouterr()
    # 人为回退成半本：删掉最后一段 + note 翻 partial
    with db.session() as s:
        (s.query(Segment).filter(Segment.work_id == w.id, Segment.ordinal == 3)
         .delete(synchronize_session=False))
        row = s.get(Work, w.id)
        row.note = IC._note("训练语料", IC.PARTIAL)
        s.commit()
    assert IC.main([path, "半本", "训练语料", "--clean"]) == 0
    assert "续跑" in capsys.readouterr().out
    after = _segs(w.id)
    assert len(after) == 4
    assert [g.id for g in after[:3]] == [g.id for g in full[:3]], \
        "已提交段不重写（行 id 不变即未被删插重做）"
    assert after[:3] == full[:3]
    tail = after[3]
    assert tail.text_clean == CT.clean_rules(tail.text) == CT.clean_rules(_para(4)), \
        "续跑补写段的 text_clean 口径必须与首导一致"
    assert "clean_pending_llm" not in json.loads(tail.integrity)
    assert IC.import_state(_work(path)) == IC.COMPLETE


# ── ⑤ --caveats 与 --clean 共存，各写各的键 ──────────────────

FRONT = ["覆汉", "作者：榴弹怕水", "内容简介：", "努力闻达于诸侯，以求苟全性命于乱世！"]
TAIL_TRUNC = "双方从上午战到日落，孙文台"   # 末段无句读 → caveats 的 truncated 键


def test_caveats_and_clean_coexist(tmp_path):
    paras = FRONT + ["", "第1章 初临汉末", _wm_para(10), _py_para(11), TAIL_TRUNC]
    _n[0] += 1
    fp = tmp_path / f"both-{_n[0]}.txt"
    fp.write_text("\n".join(paras), encoding="utf-8")
    path = str(fp)
    assert IC.main([path, "覆汉", "训练语料", "--caveats", "--clean"]) == 0
    w = _work(path)
    segs = _segs(w.id)
    assert segs and not any("内容简介" in g.text or g.text.startswith("作者：")
                            for g in segs), "caveats 的元数据剥离照常生效"
    a = json.loads(w.anchors)
    assert a["front_matter"]["author"] == "榴弹怕水", "caveats 写 anchors，不被 clean 干扰"
    assert a["last_truncated"] is True
    assert all(g.text_clean for g in segs), "clean 写 text_clean，不被 caveats 干扰"
    for g in segs:
        assert g.text_clean == CT.clean_rules(g.text)
    py = next(g for g in segs if "白sè" in g.text)
    assert json.loads(py.integrity).get("clean_pending_llm") is True
    last = segs[-1]
    fl = json.loads(last.integrity)
    assert fl.get("truncated") is True, "clean 不得覆盖 caveats 的 truncated 键"
    assert "clean_pending_llm" not in fl or fl["clean_pending_llm"] is bool(
        CT.needs_llm(last.text_clean)), "两键各写各的，互不抹除"
    # 反向：只开 caveats 不碰 text_clean（①的开关独立性）
    _n[0] += 1
    fp2 = tmp_path / f"both2-{_n[0]}.txt"
    fp2.write_text("\n".join(paras), encoding="utf-8")
    assert IC.main([str(fp2), "覆汉2", "训练语料", "--caveats"]) == 0
    assert all(g.text_clean is None for g in _segs(_work(str(fp2)).id))


# ── CLI：开关解析与用法回显 ──────────────────────────────────

def test_usage_lists_clean_flag(capsys):
    assert IC.main(["--clean"]) == 2
    err = capsys.readouterr().err
    assert "用法" in err and "--clean" in err
