# REVIEW_PROBE_LEDGER —— K5 晋升探针账本 schema 错配修复（fix/probe-ledger-schema）

日期：2026-09-26　工作树：`F:/agi/_scratch/worktrees/lg-probe-ledger-schema`
交付文件（白名单内）：`scripts/k5_promotion_wire_probe.py`（改）、
`tests/test_k5_promotion_wire_probe_ledger.py`（新）、本文档。

## 0. 验收门状态：被权限层阻断（先说清楚，不装绿）

本 worker 会话内**所有**解释器执行均被自动判定拒绝（形如
`Error: Allow Bash to run: ...?`，与本仓
`docs/K5晋升接线探针_20260925.md` §6 记录过的失败模式同型）。
被拒命令逐字记录：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k5_promotion_wire_probe_ledger.py -q
python -m pytest tests/test_k5_promotion_wire_probe_ledger.py -q
python -m pytest --version
python -c "import pytest, sys; print(pytest.__version__, sys.executable)"
```

（仅 `python --version` → `Python 3.11.16` 获准。）未尝试任何绕闸手段。
因此下述 §4/§5 的测试与反向验证为**静态复核过的预期行为**，
标注【未自跑·预期】，需主控在有权限会话复跑验收门后才能签收；
本文档不含任何伪造的实跑输出。

## 1. 改了什么（只动了探针与本测试+本文档，未触 k2_contrast_extract / 库 / 表）

`scripts/k5_promotion_wire_probe.py`：

1. **不再硬编码单一账本**（原第 45 行 `DEFAULT_LEDGER` 指向的是写手遥测账本）：
   - `DEFAULT_TELEMETRY_LEDGER = "F:/Hermes/team/judge/k2pairs_ledger.jsonl"`
     （:59，即旧路径，正名为「写手调用遥测」）；
   - `DEFAULT_GATE_LEDGER_REL = "k2_pairs.jsonl"`（:63，门账本缺省＝
     `k2_contrast_extract.py --pairs-ledger` 的仓内真实缺省名，live 跑时相对仓库根落盘）；
   - `run_probe` 两路同时找（:586-591）；CLI 新增 `--gate-ledger`（:626-628），
     `--ledger` 保留并正名（:622-625）。
2. **schema 分类器** `_classify_ledger_rows`（:435-451）：逐行按键集判定，
   门标记 `GATE_LEDGER_MARKERS=("pair_id","gates_ok","persist_outcome")`（:72，
   即 `ledger_entry()` 产物键），遥测标记 `("event","writer_model","cache","n_chars_ai")`
   （:73，即 `k2_pairs_gen.py:372/385` 追加的行键）。
   **判别只看标记、不看有没有行**——「有行就算门账本」正是被修的错。
   混档（同档既有门行又有遥测行）判 gate，且统计只数真带 `gates_ok:True`/
   `persist_outcome:"written"` 的行，遥测行不构成门证据。
3. `_parse_ledger`（:454-491）：输出新增 `kind`（`gate`/`writer_telemetry`/
   `unknown`）与 `schema_keys`（现场读的键集，非手抄）；
   **只有 `kind=="gate"` 才算 `n_gates_ok`/`n_written`**；遥测 → 两数保持
   `null` + `note` 明示「不参与门统计」；文件缺/空档/schema 未识别 →
   `null` + `error`（沿用 `_parse_pairs_file` 的「不猜」纪律）。
4. `a4_feasibility`（:494-572）：同时解析主账本与 `gate_ledger_file`，
   `a4.ledger` 优先取门账本（取不到则如实展示主账本读数），全部解析结果在
   `a4.all_ledgers`；读到遥测时 `missing` 写
   「探针未找到门账本：…是写手调用遥测账本…过门对数不可核（不猜；请用
   --gate-ledger 指定）」（:545-553），**绝不**产出「过门数=0」式断言。
   （库真读到 0 行时的「落库行数=0 <35」是 SQL 事实，保留原样。）
5. 既有硬约束原样：只读 `mode=ro`、`model_calls:0`、`git_writes:0`、
   `no_status_change_advice:true`、`advice_status_change:"none…"`（未触碰）。
   `a4_feasible_now` 判据仍＝ pairs≥48 ∧ 库内过门行≥35（本次错配的根源是
   ledger 读数被当成门证据，该路已断源）。

**兼容性**：旧签名向后兼容（`a4_feasibility` 第 5 参可选、`run_probe`
`gate_ledger_file` 可选、`--ledger` 语义保留），旧回归
`tests/test_k5_promotion_wire_probe.py` 的 13 条断言逐条静态对读无冲撞
（如 `test_a4_counts_pairs_ledger_and_db` 传门 schema 主账本 → kind=gate，
`n_gates_ok==35` 照旧；缺文件 → `n_rows is None` 照旧）。

## 2. 两种账本各读到什么（按本次分类器的口径）

| 输入 | kind | n_rows | n_gates_ok / n_written | a4.missing 相应文案 |
|---|---|---|---|---|
| 真遥测账本 `k2pairs_ledger.jsonl`（键=event/op/segment_id/writer_model/n_chars_ai/ts；cache_hit 行带 cache） | `writer_telemetry` | 照实报（如 144） | **null / null**（不猜） | 「探针未找到门账本：…遥测账本…不可核（不猜）」 |
| 门账本（`k2_contrast_extract.ledger_entry()` 行，含 gates_ok:true） | `gate` | 行数 | 真过门行数 / written 行数 | 无账本缺项 |
| 文件不存在 / 空档 / 两标记全无 | `unknown` | None 或 0 | null + `error` | 「门账本不可核（…不猜…）」 |
| 旧版 09-25 现场行为（对照） | —— | 144 | **恒 0 / 0**（错：把遥测行数当门证据） | 「落库行数=0…」被账本假零加持 ⇒ A4 判死 |

## 3. 新增测试（tests/test_k5_promotion_wire_probe_ledger.py，8 例，全离线 tmp_path）

- `test_writer_telemetry_ledger_recognised_and_not_counted` —— 验收 (a)：
  96 行遥测 → kind=writer_telemetry、n_gates_ok/n_written=null、
  schema_keys 含 event 不含 gates_ok、missing 只说「未找到门账本/不可核/不猜」，
  并逐条断言没有任何「过门/落库行数 + =0（且非不可核）」的确定性断言。
- `test_gate_ledger_missing_is_null_not_zero` —— 找不到门账本 →
  kind=unknown、null+「不猜」error。
- `test_synthetic_gate_ledger_counts_truth` —— 验收 (b)：合成 5 行
  （gates_ok:True×3，其中 written×2；False×2）→ n_gates_ok==3、
  n_written==2、n_rows==5，missing 无账本缺项。
- `test_probe_reads_both_ledgers_gate_wins_for_stats` —— 双账本同喂：
  统计取门账本真值，all_ledgers 两 kind 并列，遥测永不产门计数。
- `test_mixed_rows_classified_as_gate_not_diluted` —— 混档判 gate 不被稀释；
  空行集 unknown。
- `test_cli_gate_ledger_param_and_discipline` —— `--gate-ledger` 走通 CLI
  （main 全链，`--out ""`），discipline 四键原样，跑完 tmp 树零新增文件（只读）。
- `test_a4_key_contract_superset` —— 旧 a4 键契约不破 + kind/schema_keys/
  all_ledgers 新键在位 + 全 JSON 可序列化。

## 4. 验收门命令【未自跑·预期】

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k5_promotion_wire_probe_ledger.py -q
```

预期：**8 passed, exit 0**。另建议顺带复跑旧回归防串扰：
`... -m pytest tests/test_k5_promotion_wire_probe.py -q`（预期 13 passed）。

## 5. 反向验证（任务书硬性要求）——【操作已备好，因 §0 阻断未自跑】

变异点：`scripts/k5_promotion_wire_probe.py:445`
`if n_gate:` → `if rows:`（退化版「只要有行就算门账本」）。

预期转红：`test_writer_telemetry_ledger_recognised_and_not_counted`
（kind 变 gate、n_gates_ok 变 0，双重断言皆爆）与
`test_probe_reads_both_ledgers_gate_wins_for_stats` 中遥测侧
`n_gates_ok is None` 断言。恢复 `if n_gate:` 后预期复绿。
【本 worker 未执行任何变异编辑——交付物中该行为原始正确版，无 MUTATION 残留。】

## 6. 停报事项

- 需要主控复跑 §4 验收门 + §5 反向验证并回填真实输出（本文档 §0 已给逐字
  被拒命令清单），此为「工具需要额外权限」的如实停报；除此之外任务范围内
  改动全部完成，未触 `k2_contrast_extract.py`、数据库、表结构、其他 worktree。
