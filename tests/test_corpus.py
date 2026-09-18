from app.corpus import make_segments


def test_never_cuts_mid_sentence():
    text = "第一句长一点的句子，带标点。第二句。第三句也不短，真的。第四句！第五句？"
    segs = make_segments(text, target_sentences=2, min_chars=5, max_chars=60)
    for s in segs:
        assert s.endswith(("。", "！", "？", "”")), f"段尾不是句末：{s}"
    assert "".join(segs) == text.replace("\n", "")


def test_paragraph_boundary_preferred():
    text = "甲段一。甲段二。\n乙段一。乙段二。乙段三很长很长很长很长很长很长。"
    segs = make_segments(text, min_chars=3, target_sentences=2)
    # 第一段应成一段；第二段单独处理
    assert any("甲段一。甲段二。" in s for s in segs)


def test_short_tail_merged():
    text = "正常长度的句子甲，对吧。正常长度的句子乙，对。短尾。"
    segs = make_segments(text, min_chars=8, target_sentences=2)
    assert len(segs) == 1
    assert segs[0].endswith("。")
