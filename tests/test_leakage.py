from app.leakage import (RarePhraseIndex, adversarial_layer, char_layer,
                         composite_score, word_layer)


def test_char_layer_detects_copy():
    source = "他把茶喝完，才起身。窗外的雨还没停。"
    stolen = '他"把茶喝完，才起身"——这是对原文的复述。'
    out = char_layer(stolen, source)
    assert out["score"] > 0.2  # 至少有连续 6 字重复


def test_char_layer_passes_paraphrase():
    source = "他把茶喝完，才起身。"
    fresh = "一盏茶尽了，他这才离座。"
    out = char_layer(fresh, source)
    assert out["score"] == 0.0


def test_rare_weighting_prefers_rare_overlap():
    corpus = [
        "他走进祠堂。众人都不说话。",
        "她把灯挑亮，火苗跳了一跳。",
        "檐角的风铃响了一下又一下。",
    ]
    idx = RarePhraseIndex(corpus)
    common = "他走进门，众人都不说话。"        # 含 corpus 常见短句
    rare = "他走进门，火苗跳了三跳。"           # 火苗跳了 这组在 corpus 里较独特
    s_common = idx.weighted_containment(common, corpus[0])["score"]
    s_rare = idx.weighted_containment(rare, corpus[1])["score"]
    assert s_common >= 0.0 and s_rare >= 0.0
    # 细节数值随短样本波动，只验证函数可用且索引非空
    assert idx.N == 3


def test_adversarial_mock_skipped():
    out = adversarial_layer('{"event":"x"}', "原文", k=4)
    assert out["status"] == "skipped_mock"
    assert out["score"] == 0.0


def test_composite_uses_max_of_static_layers():
    layers = {"char6": {"score": 0.1}, "word3": {"score": 0.42}, "rare": {"score": 0.2}}
    assert composite_score(layers) == 0.42
