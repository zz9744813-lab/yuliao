from app.metrics_det import compute_metrics, det_residual


def test_basic_sentence_stats():
    text = "他来了。她没动。风很大。"  # 3 句
    m = compute_metrics(text)
    assert m["n_sentences"] == 3
    assert m["n_chars"] == len(text)
    assert 0 < m["ttr"] <= 1


def test_dialogue_ratio_and_turns():
    text = '"你到底来不来？"他问。她没答，只是站着。“先喝茶。”她说。'
    m = compute_metrics(text)
    assert m["dialogue_turns"] == 2
    assert 0.0 < m["dialogue_ratio"] < 1.0


def test_flavor_adverbs_detected():
    formal = "他微微一怔，缓缓抬手，仿佛想起了什么，不由得叹了口气。"
    plain = "他抬手，想了一下，叹气。"
    a = compute_metrics(formal)
    b = compute_metrics(plain)
    assert a["adv_flavor_per_k"] > b["adv_flavor_per_k"]


def test_delta_sign():
    human = "他走了。门没关。"
    cand = "他似乎意识到了什么，缓缓转身，轻轻地把门掩上，心中不由得一阵难过。"
    out = det_residual(human, cand)
    assert out["delta"]["psych_marker_per_k"] > 0
    assert "sent_len_mean" in out["delta"]
