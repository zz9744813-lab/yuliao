"""本机微调最小闭环（`scripts/finetune_min.py` / `scripts/finetune_compare.py`）回归测试。

验收口径（任务书）：**在没装训练栈的 venv 里也要跑得动**——两个脚本把
torch/transformers/peft 全部懒加载（`--dry-run` 与所有纯逻辑函数不 import torch），
所以本文件的绝大多数用例只锁四件可核的事：

1. **数据构造**：入池闸门的 fail-closed 语义（src_ok 三态 / 基准段隔离 /
   水印伪影 / 长度界 / 候选事实重合）、段去重、切分与确定性；
2. **训练配置校验**：规模硬上限（≤300 步、≤2000 条）、设备-精度组合、
   落盘路径纪律（禁 C 盘、禁落检出内）；
3. **adapter 落盘**：`train_log.json` 的关键字段硬闸（没 loss 序列=没训练证据）、
   adapter 文件清单如实入档；
4. **对比脚本解码参数固定**：贪心口径唯一出处、`generate` 只收贪心键。

真训练本身标在 `slow`（见文件末尾）：默认跳过，设 `LG_FT_RUN_SLOW=1` 且在装了
训练栈的 venv 里才跑。**真实训练的一次性逐字证据不在这里，在
`DELIVERY_finetune_local.md`**——本文件只证明"闸门不会被悄悄放宽"。

测试全部写在 pytest 临时目录（TEMP 已指到 F:），不落检出、不落 C 盘。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import finetune_min as FM            # noqa: E402
import finetune_compare as FC        # noqa: E402

# ── 落盘夹具：一律 F:/G: 盘外置目录，绝不写 C 盘、绝不写检出 ────────────────
# pytest 自带的 tmp_path 落在 `tempfile.gettempdir()`，本机即
# `C:\Users\6\AppData\Local\Temp`——本任务的硬边界是"产物禁落 C 盘"，
# 脚本侧的 `guard_path` 也确实会把它拒了。所以文件类用例统一用下面这个
# 目录（默认 F:/Hermes/cache/scratch，可用 LG_FT_TEST_TMP 覆盖），
# 用例结束只删自己刚创建的那一个子目录。
_SCRATCH_ROOT = Path(os.environ.get("LG_FT_TEST_TMP")
                     or "F:/Hermes/cache/scratch")
_TMP_PREFIX = "lg_ft_test_"


@pytest.fixture()
def run_dir():
    if not _SCRATCH_ROOT.is_dir():
        pytest.skip(f"外置临时根不存在：{_SCRATCH_ROOT}（设 LG_FT_TEST_TMP 指到 F:/G:）")
    if FM.drive_of(_SCRATCH_ROOT) not in FM.ALLOWED_DRIVES:
        pytest.skip(f"外置临时根不在 F:/G:：{_SCRATCH_ROOT}")
    d = Path(tempfile.mkdtemp(prefix=_TMP_PREFIX, dir=_SCRATCH_ROOT))
    try:
        yield d
    finally:
        target = d.resolve()
        inside = target.is_relative_to(_SCRATCH_ROOT.resolve())
        if inside and target.name.startswith(_TMP_PREFIX):
            shutil.rmtree(target, ignore_errors=True)


# ── 造库夹具：内存库（不落盘），只建脚本实际读到的列 ──────────────────────

OK_INTEGRITY = json.dumps({"src_ok": True, "quote_integrity": "ok"},
                          ensure_ascii=False)

# 纯汉字：`looks_watermarked` 见到 ≥2 个拉丁字母就判水印，测试文本一律不含 ASCII。
HUMAN = ("他推开柴门，院里积了半寸厚的雪，石阶上那盏灯还亮着，"
         "风一吹就摇出细碎的影子，远处狗叫了两声又安静下来。")
HUMAN_OTHER = ("檐下的水滴连成一线，青石板上洇出深色的圆，她把伞收起来贴在胸口，"
               "站在门口听了一会儿屋里没有动静。")
AI_NEAR = ("他推开柴门，看见院里积了半寸厚的雪，石阶上那盏灯还亮着。风一吹，"
           "就摇出细碎的影子。远处有狗叫了两声，然后又安静下来。")     # 与 HUMAN 高重合
AI_DRIFT = ("她站在明亮的落地窗前，端起一杯温度刚好的咖啡，望向窗外那座繁忙的城市，"
            "心里充满了对未来的期待与憧憬。")                        # 与 HUMAN 几乎无重合
WATERMARKED = "他推开柴门院里积了半寸厚的雪ab石阶上那盏灯还亮着风一吹就摇出细碎的影子"


def _schema(con: sqlite3.Connection) -> None:
    con.executescript("""
    create table segments(id text primary key, work_id text, text text,
                          integrity text, role text);
    create table frames(id text primary key, segment_id text, payload text,
                        status text);
    create table controlled_corruptions(id text primary key, segment_id text,
                        corruption_type text, variable text, text text,
                        status text, fact_consistent integer, drift_ok integer);
    create table candidates(id text primary key, segment_id text, text text,
                        model text, status text);
    create table benchmark_items(id text primary key, text_a text, text_b text,
                        context text);
    """)
    con.commit()


def add_segment(con, sid, text=HUMAN, integrity=OK_INTEGRITY, role=None,
                work="WK1"):
    con.execute("insert into segments(id, work_id, text, integrity, role) "
                "values(?,?,?,?,?)", (sid, work, text, integrity, role))
    con.commit()


def add_frame(con, fid, sid, payload=None, status="ok"):
    body = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    con.execute("insert into frames(id, segment_id, payload, status) "
                "values(?,?,?,?)", (fid, sid, body, status))
    con.commit()


def add_candidate(con, cid, sid, text=AI_NEAR, model="some-llm", status="ok"):
    con.execute("insert into candidates(id, segment_id, text, model, status) "
                "values(?,?,?,?,?)", (cid, sid, text, model, status))
    con.commit()


def add_corruption(con, cid, sid, bad="他推开柴门。院里下雪了。灯亮着。",
                   variable="节奏拉平", status="ok", fact=1, drift=1):
    con.execute("insert into controlled_corruptions(id, segment_id, "
                "corruption_type, variable, text, status, fact_consistent, "
                "drift_ok) values(?,?,?,?,?,?,?,?)",
                (cid, sid, "rhythm", variable, bad, status, fact, drift))
    con.commit()


def add_benchmark(con, bid, text_a=HUMAN, text_b=HUMAN_OTHER, context=""):
    con.execute("insert into benchmark_items(id, text_a, text_b, context) "
                "values(?,?,?,?)", (bid, text_a, text_b, context))
    con.commit()


@pytest.fixture()
def db():
    """内存库：造出的表与真库同名同列，但**不占任何磁盘**。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row          # 与 FM.open_db_ro 同一读取形态
    _schema(con)
    yield con
    con.close()


def _extract(con, **kw):
    kw.setdefault("limit", 20)
    kw.setdefault("eval_size", 2)
    return FM.extract_pairs(con, **kw)


# ── 1. 数据构造：闸门是 fail-closed 的 ──────────────────────────────────

@pytest.mark.parametrize("bad_integrity", [
    None, "", "not json", "[]", '"ok"', "42",
    json.dumps({"src_ok": "true"}),          # 字符串 true 不算布尔
    json.dumps({"src_ok": 1}),               # 1 不算布尔
    json.dumps({"quote_integrity": "ok"}),   # 缺 src_ok 键
])
def test_src_ok_gate_is_fail_closed(bad_integrity):
    """只有 JSON 布尔 true 才过闸；其余一律 None（未校验）——不许"看着像就放行"。"""
    assert FM.integrity_src_ok_state(bad_integrity) is not True


def test_src_ok_true_passes_and_packed_rows_stay_closed():
    assert FM.integrity_src_ok_state(OK_INTEGRITY) is True
    # 紧凑编码（app/segment_integrity 的 i1: 形态）只编 8 个基键、不含 src_ok：
    # 还原后依旧不过闸，但理由是"键缺失=未校验"，不是"坏 JSON"。
    packed = "i1:0abcdef"
    assert FM.integrity_src_ok_state(packed) is None


def test_unverified_segment_never_enters_pool(db):
    add_segment(db, "SEG_A", integrity=json.dumps({"src_ok": False}))
    add_frame(db, "FR_A", "SEG_A", {"event": "她推门进屋"})
    add_segment(db, "SEG_B")                       # 已校验
    add_frame(db, "FR_B", "SEG_B", {"event": "他点起一盏灯"})
    pairs, stats = _extract(db)
    assert [p["id"] for p in pairs] == ["FT-FR_B"]
    assert stats["skipped"]["src_ok_not_true"] == 1


def test_benchmark_role_segment_is_excluded(db):
    add_segment(db, "SEG_BM", role="benchmark")
    add_frame(db, "FR_BM", "SEG_BM", {"event": "基准段"})
    add_segment(db, "SEG_OK")
    add_frame(db, "FR_OK", "SEG_OK", {"event": "训练段"})
    pairs, stats = _extract(db)
    assert {p["id"] for p in pairs} == {"FT-FR_OK"}
    assert stats["skipped"]["benchmark_role"] == 1


def test_benchmark_frozen_text_excluded_and_switch_honours_it(db):
    """段本身是训练角色，但正文与 benchmark_items 某一侧逐字相同 ⇒ 仍要排除
    （§14：基准文本被训练读到就不再是基准）。`exclude_bench=False` 才放它进来。"""
    add_segment(db, "SEG_T", text=HUMAN)
    add_frame(db, "FR_T", "SEG_T", {"event": "雪夜推门"})
    add_benchmark(db, "BM1", text_a=HUMAN)
    pairs, stats = _extract(db)
    assert pairs == [] and stats["skipped"]["bench_overlap"] == 1
    kept, _ = _extract(db, exclude_bench=False)
    assert [p["id"] for p in kept] == ["FT-FR_T"]


def test_bench_hash_counts_context_pieces_over_fifty_chars(db):
    """口径抄 `export_training._bench_hashes`：a/b 无长度下限，
    context 及其 \\n\\n 组成段要 ≥50 字才算——短碎片不算基准内容。"""
    add_benchmark(db, "BM1", text_a="短侧一", text_b="短侧二",
                  context="前半只有一点点字\n\n" + ("后" * 60))
    con_hash = FM.bench_text_hashes(db)
    assert FM.content_hash("短侧一") in con_hash
    assert FM.content_hash("短侧二") in con_hash
    assert FM.content_hash("前半只有一点点字") not in con_hash
    assert FM.content_hash("后" * 60) in con_hash


def test_watermarked_target_excluded(db):
    add_segment(db, "SEG_WM", text=WATERMARKED)
    add_frame(db, "FR_WM", "SEG_WM", {"event": "推门"})
    pairs, stats = _extract(db)
    assert pairs == [] and stats["skipped"]["watermark"] == 1
    assert FM.looks_watermarked(WATERMARKED) and not FM.looks_watermarked(HUMAN)


@pytest.mark.parametrize("text", ["太短。", "字" * (FM.MAX_TARGET_CHARS + 1)])
def test_target_length_bounds(db, text):
    add_segment(db, "SEG_LEN", text=text)
    add_frame(db, "FR_LEN", "SEG_LEN", {"event": "边界"})
    pairs, stats = _extract(db)
    assert pairs == [] and stats["skipped"]["target_len"] == 1


def test_candidate_without_fact_flag_needs_content_overlap(db):
    """candidates 表没有 fact_consistent 字段 ⇒ 用二元组重合兜"事实漂移"：
    重合过低的机器稿会把模型教成编事实，一律不进。"""
    assert FM.bigram_jaccard(AI_NEAR, HUMAN) >= FM.MIN_OVERLAP
    assert FM.bigram_jaccard(AI_DRIFT, HUMAN) < FM.MIN_OVERLAP
    add_segment(db, "SEG_C1")
    add_candidate(db, "CND1", "SEG_C1", text=AI_NEAR)
    add_segment(db, "SEG_C2", text=HUMAN_OTHER)
    add_candidate(db, "CND2", "SEG_C2", text=AI_DRIFT)
    pairs, stats = _extract(db)
    assert [p["id"] for p in pairs] == ["FT-CND1"]
    assert stats["skipped"]["low_overlap"] == 1


def test_frame_payload_without_points_makes_no_pair(db):
    add_segment(db, "SEG_F1")
    add_frame(db, "FR_F1", "SEG_F1", {"event": "", "reader_effect": "   "})
    add_segment(db, "SEG_F2")
    add_frame(db, "FR_F2", "SEG_F2", None)          # payload 空
    pairs, stats = _extract(db)
    assert pairs == [] and stats["skipped"]["no_instruction"] == 2


def test_frame_instruction_uses_both_intent_spellings():
    """导出侧见过 intention / intent 两种键，指令拼接必须都认。"""
    a = FM._frame_instruction({"event": "雪夜推门", "intention": "建立压迫感",
                               "reader_effect": "发冷"})
    b = FM._frame_instruction({"event": "雪夜推门", "intent": "建立压迫感",
                               "reader_effect": "发冷"})
    assert a == b
    assert "事件：雪夜推门" in a and "人物意图：建立压迫感" in a \
        and "读者感受：发冷" in a
    assert FM._frame_instruction({"other": "x"}) is None


def test_one_segment_contributes_one_pair(db):
    """同一段同时有帧与候选 ⇒ 只留一条（段级去重），不拿同一句自证两遍。"""
    add_segment(db, "SEG_D")
    add_frame(db, "FR_D", "SEG_D", {"event": "推门"})
    add_candidate(db, "CND_D", "SEG_D", text=AI_NEAR)
    pairs, stats = _extract(db)
    assert len(pairs) == 1
    assert stats["skipped"]["dup_segment"] == 1


def test_selection_is_deterministic_per_seed(db):
    for i in range(6):
        add_segment(db, f"SEG_{i}", text=HUMAN if i % 2 else HUMAN_OTHER)
        add_frame(db, f"FR_{i}", f"SEG_{i}", {"event": f"要点{i}"})
    a, _ = _extract(db, seed=11)
    b, _ = _extract(db, seed=11)
    c, _ = _extract(db, seed=12)
    assert [p["id"] for p in a] == [p["id"] for p in b]
    assert [p["id"] for p in a] != [p["id"] for p in c]


def test_heldout_split_sizes_and_membership(db):
    for i in range(8):
        add_segment(db, f"SEG_{i}", text=HUMAN if i % 2 else HUMAN_OTHER)
        add_frame(db, f"FR_{i}", f"SEG_{i}", {"event": f"要点{i}"})
    pairs, stats = _extract(db, limit=8, eval_size=3)
    assert stats["train"] == 5 and stats["eval"] == 3
    assert sum(1 for p in pairs if p["split"] == "eval") == 3
    assert {p["split"] for p in pairs} == {"train", "eval"}


def test_every_pair_carries_lineage(db):
    add_segment(db, "SEG_L", work="WK7")
    add_frame(db, "FR_L", "SEG_L", {"event": "点灯"})
    pairs, _ = _extract(db)
    p = pairs[0]
    assert p["template"] == FM.TEMPLATE_A
    assert p["segment_id"] == "SEG_L" and p["work_id"] == "WK7"
    assert p["instruction"] and p["output"] == HUMAN
    assert p["source"]                      # 数据血缘要能追到表与列


@pytest.mark.parametrize("kw,msg", [
    ({"limit": 0}, "limit"),
    ({"limit": FM.MAX_PAIRS_HARD_CAP + 1}, "上限"),
    ({"limit": 10, "eval_size": 0}, "eval_size"),
    ({"limit": 10, "eval_size": 10}, "eval_size"),      # 全划去训没有
    ({"limit": 10, "eval_size": 257}, "eval_size"),
])
def test_extract_pairs_hard_caps(db, kw, msg):
    with pytest.raises(ValueError) as e:
        FM.extract_pairs(db, **kw)
    assert msg in str(e.value)


def test_pairs_jsonl_roundtrip(run_dir):
    rows = [{"id": "FT-1", "split": "train", "template": FM.TEMPLATE_A,
             "instruction": "指\n令", "output": "正文", "segment_id": "SEG1"},
            {"id": "FT-2", "split": "eval", "template": FM.TEMPLATE_C,
             "instruction": "另一条", "output": "另一段正文", "segment_id": "SEG2"}]
    path = FM.write_pairs_jsonl(run_dir / "train_pairs.jsonl", rows)
    assert FM.read_pairs_jsonl(path) == rows


# ── 2. 训练配置校验 ─────────────────────────────────────────────────────

def test_validate_config_accepts_defaults_outside_checkout(run_dir):
    cfg = FM.TrainConfig(out_dir=run_dir / "run")
    assert FM.validate_config(cfg) is cfg


@pytest.mark.parametrize("field,value", [
    ("steps", 0), ("steps", FM.MAX_STEPS_HARD_CAP + 1),
    ("micro_batch", 0), ("grad_accum", 0),
    ("max_len", 16), ("max_len", 2048),
    ("lr", 0.0), ("lr", 2.0),
    ("max_pairs", 0), ("max_pairs", FM.MAX_PAIRS_HARD_CAP + 1),
    ("eval_size", 0),
    ("lora_r", 0), ("lora_alpha", 0),
    ("warmup_ratio", 0.9), ("grad_clip", -1.0), ("log_every", 0),
    ("device", "mps"), ("dtype", "fp8"), ("model", "   "),
])
def test_validate_config_rejects_out_of_range(run_dir, field, value):
    cfg = FM.TrainConfig(out_dir=run_dir / "run")
    setattr(cfg, field, value)
    if field == "eval_size":
        cfg.max_pairs = 8                       # 让 eval_size 的界独立成为失败原因
    with pytest.raises(ValueError) as e:
        FM.validate_config(cfg)
    assert field in str(e.value) or value in str(e.value)


def test_validate_config_rejects_cpu_fp16(run_dir):
    """CPU + fp16 会在中途炸（CPU 端 fp16 算子大面积缺失）⇒ 配置层就拒。"""
    cfg = FM.TrainConfig(out_dir=run_dir / "run", device="cpu", dtype="fp16")
    with pytest.raises(ValueError, match="fp16"):
        FM.validate_config(cfg)


@pytest.mark.parametrize("path", ["C:/Windows/temp/run",
                                  "C:/Users/anyone/.cache/run"])
def test_guard_rejects_c_drive(path):
    """硬边界：模型/数据/缓存/产物一律 F:/G:，C 盘一律拒。"""
    with pytest.raises(ValueError, match="F:/G:"):
        FM.guard_out_dir(Path(path))
    with pytest.raises(ValueError, match="F:/G:"):
        FM.guard_path(Path(path), what="--cache-root")


def test_guard_rejects_paths_inside_checkout():
    """产物不进版本库：指向检出内的 out-dir 直接拒（.gitignore 挡不住 adapter/jsonl）。"""
    with pytest.raises(ValueError, match="检出内"):
        FM.guard_out_dir(ROOT / "build" / "ft")
    with pytest.raises(ValueError, match="检出内"):
        FM.guard_out_dir(ROOT / "scripts" / "out")


def test_guard_accepts_f_drive_outside_checkout(run_dir):
    p = run_dir / "run"                      # run_dir 夹具固定在 F:/G:（见文件头）
    assert FM.drive_of(p) in FM.ALLOWED_DRIVES
    assert FM.guard_out_dir(p) == p


def test_cache_env_never_points_at_c():
    env = FM.cache_env()
    assert env["HF_HOME"].upper().startswith("F:")
    assert env["HF_HUB_CACHE"].upper().startswith("F:")
    assert env["TRANSFORMERS_CACHE"].upper().startswith("F:")
    assert env["TORCH_HOME"].upper().startswith("F:")
    assert env["PIP_CACHE_DIR"].upper().startswith("F:")
    assert all("C:\\" not in v and "c:/" not in v.lower() for v in env.values())
    with pytest.raises(ValueError):
        FM.cache_env(cache_root="C:/Users/6/.cache/hf")


def test_apply_cache_env_sets_environment(monkeypatch):
    monkeypatch.setenv("HF_HOME", "")
    env = FM.apply_cache_env(FM.cache_env())
    assert os.environ["HF_HOME"] == env["HF_HOME"]
    assert os.environ["TOKENIZERS_PARALLELISM"] == "false"


class _FakeCuda:
    def __init__(self, available, bf16=False):
        self._available, self._bf16 = available, bf16

    def is_available(self):
        return self._available

    def is_bf16_supported(self):
        return self._bf16


class _FakeTorch:
    def __init__(self, cuda):
        self.cuda = cuda


def test_resolve_device_dtype_auto_paths(run_dir):
    """auto 的落法只有一处：cuda 有 bf16→bf16（免 GradScaler），否则 fp16；CPU→fp32。"""
    gpu_bf16 = _FakeTorch(_FakeCuda(True, bf16=True))
    gpu_plain = _FakeTorch(_FakeCuda(True, bf16=False))
    cpu = _FakeTorch(_FakeCuda(False))
    mk = lambda **kw: FM.TrainConfig(out_dir=run_dir / "run", **kw)
    assert FM.resolve_device_dtype(mk(device="auto"), gpu_bf16) == ("cuda", "bf16")
    assert FM.resolve_device_dtype(mk(device="auto"), gpu_plain) == ("cuda", "fp16")
    assert FM.resolve_device_dtype(mk(device="auto"), cpu) == ("cpu", "fp32")
    assert FM.resolve_device_dtype(mk(device="cpu"), cpu) == ("cpu", "fp32")
    with pytest.raises(ValueError, match="fp16"):
        FM.resolve_device_dtype(mk(device="cpu", dtype="fp16"), cpu)


def test_config_dict_is_json_serialisable(run_dir):
    cfg = FM.validate_config(FM.TrainConfig(out_dir=run_dir / "run"))
    dumped = json.dumps(cfg.as_dict())        # train_log.json 直接吃它
    back = json.loads(dumped)
    assert back["out_dir"] == str(run_dir / "run")   # Path 已降为字符串
    assert back["steps"] <= FM.MAX_STEPS_HARD_CAP
    assert back["max_pairs"] <= FM.MAX_PAIRS_HARD_CAP


# ── 3. 编码：只学 completion，padding 不学 ──────────────────────────────

class CharTok:
    """逐字符 tokenizer：不需要 torch，够把 labels 掩码口径钉死。"""
    eos_token = "</e>"
    eos_token_id = 1
    pad_token_id = 0

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [ord(c) for c in text]}

    def decode(self, ids, skip_special_tokens=True):
        return "".join(chr(i) for i in ids)


TOK = CharTok()


def test_build_prompt_falls_back_without_chat_template():
    text, mode = FM.build_prompt(TOK, "写一段正文")
    assert mode == "fallback_text"
    assert text == FM.fallback_prompt("写一段正文")


def test_encode_pair_masks_every_prompt_token():
    pair = {"instruction": "把这段改回自然中文", "output": "檐下的水滴连成一线"}
    ids, labels = FM.encode_pair(TOK, pair, max_len=512)
    assert len(ids) == len(labels)
    prompt, _ = FM.build_prompt(TOK, pair["instruction"])
    masked = sum(1 for v in labels if v == -100)
    assert masked == len(prompt)                       # prompt 一个都不学
    tail = [t for t, l in zip(ids, labels) if l != -100]
    assert tail == [ord(c) for c in pair["output"] + CharTok.eos_token]


def test_encode_pair_truncation_keeps_completion_tail():
    """截断砍的是 prompt 头部，不是要学的输出——砍掉输出等于这一轮白跑。"""
    eos = [ord(c) for c in CharTok.eos_token]
    pair = {"instruction": "指" * 60, "output": "文" * 40}
    ids, labels = FM.encode_pair(TOK, pair, max_len=32)
    assert len(ids) == 32
    learned = [t for t, l in zip(ids, labels) if l != -100]
    assert learned, "截断后必须还有可学 token"
    assert learned == [ord("文")] * (32 - len(eos)) + eos
    # 截断窗口里一个 prompt token 都不该被学着（prompt 全落在 keep_from 之前）
    assert -100 not in labels


def test_encode_pair_empty_output_never_learns_prompt():
    """输出为空 ⇒ 可学的只剩收尾 EOS：prompt 一个 token 都不进 labels。
    （掩码口径只跟"prompt 有多长"有关，跟输出是否为空无关——这里钉死。）"""
    pair = {"instruction": "把这段改回自然中文", "output": ""}
    ids, labels = FM.encode_pair(TOK, pair, max_len=512)
    prompt, _ = FM.build_prompt(TOK, pair["instruction"])
    assert [t for t, l in zip(ids, labels) if l != -100] == \
        [ord(c) for c in CharTok.eos_token]
    assert sum(1 for v in labels if v == -100) == len(prompt)


def test_make_batches_left_pads_and_masks_pad_labels():
    items = [{"input_ids": [7, 8, 9], "labels": [-100, 8, 9]},
             {"input_ids": [5], "labels": [5]}]
    b, = FM.make_batches(items, TOK, max_len=32, batch_size=2)
    assert b["input_ids"] == [[7, 8, 9], [0, 0, 5]]          # pad 在左侧
    assert b["labels"] == [[-100, 8, 9], [-100, -100, 5]]    # pad 位不学
    assert b["attention_mask"] == [[1, 1, 1], [0, 0, 1]]


def test_make_batches_respects_batch_size():
    items = [{"input_ids": [i], "labels": [i]} for i in range(5)]
    batches = FM.make_batches(items, TOK, max_len=32, batch_size=2)
    assert [len(x["input_ids"]) for x in batches] == [2, 2, 1]


# ── 4. adapter 落盘与 train_log 硬闸 ────────────────────────────────────

def _minimal_log_payload(**over):
    payload = {k: None for k in FM.TRAIN_LOG_KEYS}
    payload.update(model="Qwen/Qwen2.5-0.5B-Instruct", steps=2, device="cpu",
                   dtype="fp32", seed=1, losses=[2.4, 1.9],
                   eval_loss_before=2.3, eval_loss_after=2.0,
                   elapsed_sec=1.5, peak_vram_mib=None,
                   train_pairs=8, eval_pairs=2)
    payload.update(over)
    return payload


def test_train_log_requires_key_fields_and_non_empty_losses(run_dir):
    """`train_log.json` 缺键或 losses 为空 ⇒ 当场 ValueError。
    "打印了日志"不等于"训练过"——没 loss 序列的日志按任务书就是无效交付。"""
    with pytest.raises(ValueError, match="缺关键字段"):
        FM.write_train_log(run_dir, {"model": "x"})
    for broken in ([], "not-a-list", None):
        with pytest.raises(ValueError, match="losses"):
            FM.write_train_log(run_dir, _minimal_log_payload(losses=broken))


def test_train_log_and_adapter_dir_are_written_outside_checkout(run_dir):
    """adapter 目录 + train_log.json 落盘：清单如实入档，且路径必须 F:/G: 且检出外。"""
    run = run_dir / "run"
    adapter = run / "adapter"
    adapter.mkdir(parents=True)
    (adapter / "adapter_config.json").write_text('{"r": 16}', encoding="utf-8")
    (adapter / "adapter_model.safetensors").write_bytes(b"\x00fake-weights")
    payload = _minimal_log_payload(
        adapter_dir=str(adapter),
        adapter_files=sorted(p.name for p in adapter.iterdir()))
    path = FM.write_train_log(run, payload)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["adapter_files"] == ["adapter_config.json",
                                      "adapter_model.safetensors"]
    assert saved["losses"] == [2.4, 1.9]
    assert FM.guard_out_dir(run) == run                     # F: 盘 + 检出外
    with pytest.raises(ValueError):
        FM.guard_out_dir(ROOT / "adapter")                  # 检出内一律拒


def test_environment_snapshot_records_cache_dirs():
    FM.apply_cache_env(FM.cache_env())
    snap = FM.environment_snapshot()                        # 无 torch 也要出得来
    assert snap["cache_env"]["HF_HOME"].upper().startswith("F:")
    assert snap["python"] and snap["platform"]
    assert "torch" not in snap                              # 没训练栈时不谎报


# ── 5. 对比脚本：解码口径唯一且为贪心 ──────────────────────────────────

def test_decode_constants_are_greedy():
    """同提示词对照只允许一套解码口径：贪心（temperature=0 语义）。
    任一侧偷偷开采样/beam，"变好"就可能是解码噪声而不是权重差别。"""
    assert FC.DECODE["temperature"] == 0.0
    assert FC.DECODE["do_sample"] is False
    assert FC.DECODE["num_beams"] == 1
    assert FC.DECODE["top_k"] == 0 and FC.DECODE["top_p"] == 1.0
    assert FC.DECODE["repetition_penalty"] == 1.0


def test_generate_kwargs_only_greedy_keys_and_is_stable():
    kw = FC.generate_kwargs(64)
    assert kw["do_sample"] is False and kw["num_beams"] == 1
    assert kw["max_new_tokens"] == 64
    assert "temperature" not in kw          # 非采样模式下 generate 会校验并抛
    assert "top_p" not in kw and "top_k" not in kw
    assert FC.generate_kwargs(64) == kw     # 与调用方状态无关
    assert FC.generate_kwargs(1)["max_new_tokens"] == 1


def test_compare_rejects_out_of_checkout_only_paths(run_dir):
    """对比产物同样只准落 F:/G: 且检出外（复用训练侧同一道闸）。"""
    with pytest.raises(ValueError, match="F:/G:"):
        FM.guard_out_dir(Path("C:/Users/6/compare"))
    with pytest.raises(ValueError, match="检出内"):
        FM.guard_out_dir(ROOT / "compare")
    assert FM.guard_out_dir(run_dir / "compare") == run_dir / "compare"


def test_adapter_fingerprint_tracks_bytes(run_dir):
    d = run_dir / "adapter"
    d.mkdir()
    (d / "adapter_model.safetensors").write_bytes(b"v1")
    (d / "adapter_config.json").write_text("{}", encoding="utf-8")
    fp_a = FC.adapter_fingerprint(d)
    fp_b = FC.adapter_fingerprint(d)
    assert fp_a == fp_b and fp_a["file_count"] == 2
    (d / "adapter_model.safetensors").write_bytes(b"v2")     # 换权重
    assert FC.adapter_fingerprint(d)["files"] != fp_a["files"]


def test_adapter_fingerprint_missing_dir_fails_loudly(run_dir):
    with pytest.raises(SystemExit):
        FC.adapter_fingerprint(run_dir / "nope")


def test_build_eval_prompts_excludes_trained_segments(db):
    """提示词必须来自**没训过**的段：train/eval 两侧的 segment_id 都要排除，
    否则"base vs tuned"就是拿训过的句子自证。"""
    add_segment(db, "SEG_TR", text=HUMAN)
    add_corruption(db, "CC_TR", "SEG_TR")
    add_segment(db, "SEG_NEW", text=HUMAN_OTHER)
    add_corruption(db, "CC_NEW", "SEG_NEW")
    prompts = FC.build_eval_prompts(db, {"SEG_TR"}, n_prompts=4)
    assert {p["segment_id"] for p in prompts} == {"SEG_NEW"}
    assert prompts[0]["kind"] == "rewrite_deai"
    assert "原句：" in prompts[0]["instruction"]


def test_build_eval_prompts_mixes_kinds_and_is_deterministic(db):
    for i in range(4):
        add_segment(db, f"SEG_R{i}", text=HUMAN if i % 2 else HUMAN_OTHER)
        add_corruption(db, f"CC_{i}", f"SEG_R{i}")
        add_segment(db, f"SEG_F{i}", text=HUMAN_OTHER if i % 2 else HUMAN)
        add_frame(db, f"FR_{i}", f"SEG_F{i}", {"event": f"要点{i}，风把灯吹得摇晃"})
    a = FC.build_eval_prompts(db, set(), n_prompts=4, seed=5)
    b = FC.build_eval_prompts(db, set(), n_prompts=4, seed=5)
    assert [p["segment_id"] for p in a] == [p["segment_id"] for p in b]
    assert {p["kind"] for p in a} == {"rewrite_deai", "frame_to_text"}
    with pytest.raises(ValueError):
        FC.build_eval_prompts(db, set(), n_prompts=0)


def test_build_eval_prompts_skips_benchmark_texts(db):
    add_segment(db, "SEG_B", text=HUMAN)
    add_frame(db, "FR_B", "SEG_B", {"event": "推门看雪，灯影摇动"})
    add_benchmark(db, "BM1", text_a=HUMAN)
    assert FC.build_eval_prompts(db, set(), n_prompts=2) == []
    assert len(FC.build_eval_prompts(db, set(), n_prompts=2,
                                     exclude_bench=False)) == 1


def test_render_markdown_states_greedy_and_disclaimer(run_dir):
    payload = {
        "model": "Qwen/Qwen2.5-0.5B-Instruct",
        "adapter": {"path": str(run_dir / "adapter"), "file_count": 2},
        "adapter_fingerprint": "deadbeef",
        "decode": dict(FC.DECODE, max_new_tokens=64),
        "seed": 1, "device": "cpu", "dtype": "fp32", "elapsed_sec": 3.0,
        "n_prompts": 1, "excluded_segment_ids": 12,
        "summary": {"identical": 0, "differ": 1, "base_chars_median": 30,
                    "tuned_chars_median": 28, "by_kind": {}},
        "items": [{"index": 1, "kind": "rewrite_deai", "segment_id": "SEG1",
                   "work_id": "WK1", "prompt_text": "P", "instruction": "P",
                   "base_text": "B", "tuned_text": "T", "reference": "R",
                   "identical": False, "meta": {}}],
    }
    md = FC.render_markdown(payload)
    assert "temperature=0.0" in md and "do_sample=False" in md
    assert "num_beams=1" in md
    assert "不承诺质量提升" in md
    assert "**base**" in md and "**tuned**" in md
    assert "deadbeef" in md
    assert "已排除 train_pairs.jsonl 里的 12 个段 id" in md


# ── 6. CLI 接线（不 import torch 的那一半）─────────────────────────────

def test_cli_dry_run_writes_pairs_and_skips_training(run_dir, capsys):
    db_path = run_dir / "cli.db"
    con = sqlite3.connect(db_path)
    _schema(con)
    add_segment(con, "SEG_X", text=HUMAN)
    add_frame(con, "FR_X", "SEG_X", {"event": "推门看雪"})
    add_segment(con, "SEG_Y", text=HUMAN_OTHER)
    add_frame(con, "FR_Y", "SEG_Y", {"event": "收伞听屋"})
    con.close()
    run = run_dir / "run"
    code = FM.main(["--stage", "data", "--dry-run", "--db", str(db_path),
                    "--out-dir", str(run), "--max-pairs", "4", "--eval-size", "1"])
    assert code == 0
    rows = FM.read_pairs_jsonl(run / "train_pairs.jsonl")
    assert len(rows) == 2
    assert "dry-run" in capsys.readouterr().out
    assert (run / "train_log.json").exists() is False      # 没训练就不许有日志


def test_cli_rejects_bad_config_before_touching_torch(run_dir, capsys):
    code = None
    with pytest.raises(SystemExit) as e:
        FM.main(["--stage", "data", "--dry-run", "--steps", "5000",
                 "--db", str(run_dir / "none.db"),
                 "--out-dir", str(run_dir / "run")])
    assert e.value.code == 2
    assert "steps" in capsys.readouterr().err


def test_cli_requires_existing_db(run_dir, capsys):
    with pytest.raises(SystemExit) as e:
        FM.main(["--stage", "data", "--dry-run",
                 "--db", str(run_dir / "absent.db"),
                 "--out-dir", str(run_dir / "run")])
    assert e.value.code == 2
    assert "库不存在" in capsys.readouterr().err


def test_parser_exposes_the_sized_knobs():
    def opts(parser):
        return {s for a in parser._actions for s in a.option_strings}
    assert {"--steps", "--max-pairs", "--device", "--dtype", "--micro-batch",
            "--grad-accum", "--eval-size"} <= opts(FM.build_parser())
    assert {"--n-prompts", "--max-new-tokens", "--adapter"} <= opts(
        FC.build_parser())


# ── 7. slow：真训练（默认跳过）─────────────────────────────────────────

def _torch_available() -> bool:
    try:
        import torch                                    # noqa: F401
        import peft                                     # noqa: F401
        import transformers                             # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.slow
@pytest.mark.skipif(not _torch_available(),
                    reason="训练栈（torch/peft/transformers）只在 F:/G: 专用 venv 里")
@pytest.mark.skipif(os.environ.get("LG_FT_RUN_SLOW") != "1",
                    reason="真训练要下载基座并占显存：设 LG_FT_RUN_SLOW=1 再跑")
def test_real_two_step_training_lands_adapter_and_log(run_dir):
    """两步步的真训练：断言 adapter 真的落盘、loss 序列真的非空。
    这是**测试用**的最小训练，交付要求的真跑证据在 DELIVERY_finetune_local.md。"""
    db_path = run_dir / "slow.db"
    con = sqlite3.connect(db_path)
    _schema(con)
    for i in range(4):
        add_segment(con, f"SEG_{i}", text=HUMAN if i % 2 else HUMAN_OTHER)
        add_frame(con, f"FR_{i}", f"SEG_{i}", {"event": f"要点{i}，雪落无声"})
    con.close()
    run = run_dir / "slow_run"
    cfg = FM.validate_config(FM.TrainConfig(
        out_dir=run, db=db_path, steps=2, max_pairs=4, eval_size=1,
        max_len=64, micro_batch=1, grad_accum=1, device="cpu", dtype="fp32"))
    pairs, _ = FM.extract_pairs(FM.open_db_ro(db_path), limit=cfg.max_pairs,
                                seed=cfg.seed, eval_size=cfg.eval_size)
    payload = FM.train(cfg, pairs)
    assert payload["losses"] and len(payload["losses"]) == 2
    assert (run / "train_log.json").exists()
    assert "adapter_model.safetensors" in payload["adapter_files"]
    assert payload["device"] == "cpu" and payload["dtype"] == "fp32"
