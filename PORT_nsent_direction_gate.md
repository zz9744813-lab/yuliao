# PORT — n_sentences 回填文档「方向门」＋ 供给口径生成器假证据收口（2026-09-26）

工作树：`F:\agi\_scratch\worktrees\nsent-verify-gate`
分支：`task/nsent-verify-gate`（**未 commit / merge / push**，按任务硬约束）
任务号：lg-nsent-verify-gate｜上游判定：`F:/Hermes/team/reviews/REVIEW_audit_residuals_round2_20260925.md`（判 BLOCK）

## 0. 一句话结论

复核报告的两条同型残留已收口：①新增 `tests/test_nsent_doc_direction.py`（14 例，**零真库、纯文本 + 子进程**），
让门**直接消费** `scripts/backfill_v2_sentences.py --verify` 的 exit 0 与「残留（`n_sentences=0` 且文本非空）=0」，
并把文档结论词/残留数与实跑残留**同向**绑死（不再靠报告转述，对应复核 §4.4 建议 4 / §5 R4）；
②`scripts/k5_supply_recount.py` 的 `render_markdown` 里两行硬编码 `→ exit 0` 已删除，
改为 `render_markdown(res, run_rc=…)` **由实跑返回码派生**、拿不到 rc 时显式渲染为
「未实跑（模板占位，非本次实跑证据）」（对应复核 §3 注记 2）。
验收门 `40 passed / exit 0`；三次**反向验证**（变异点 A、B、C）全部**判红**，复原后复绿。

## 1. 交付文件（白名单 4/4，无越界新建）

| 路径 | 状态 |
|---|---|
| `tests/test_nsent_doc_direction.py` | **新建**，423 行 / 14 例：文档侧锚定解析（§4 `--verify` 输出块 + §2 实测现状块，**两侧共用同一行正则**）+ 实跑侧子进程真跑 `--verify`（临时夹具库）+ 方向判据 + 3 条反向验证 + 4 条 fail-closed |
| `tests/test_k5_supply_recount.py` | 追加 5 例（§0 exit 状态与传入 rc 逐格一致 / 未传 rc ⇒ 占位非证据 / 源码级反硬编码 / CLI 落盘文档 exit == 返回码 / 失败实跑不落盘带 exit 的文档） |
| `scripts/k5_supply_recount.py` | 改：`render_markdown(res, run_rc=None)`、新增 `PLACEHOLDER_EXIT` / `EXTERNAL_EXIT` / `_exit_cell()`；`main()` 传实跑 rc；**未动任何口径常量** |
| `PORT_nsent_direction_gate.md` | 本文件 |

零新建临时/自测脚本：反向验证的备份件写在仓外 `C:/Users/6/AppData/Local/Temp/opencode/`（非本树），
仓内 `git status` 只有上表 3 项（2 改 1 新）+ 本文件。

## 2. 新门怎么钉方向（`tests/test_nsent_doc_direction.py`）

观测面＝**已提交的文档**（这是上一轮 BLOCK 的根因：没有任何门读已提交文档），不是当场生成的临时产物。

1. **文档侧**（纯文本）：严格锚定 §4（`## 4.` 起、到下一个 `## ` 止）里**承载 `--verify` 输出的那个围栏块**，
   用与解析真实 stdout **完全相同**的正则取逐版本五元组
   `seg_version / count / n_sentences>0 / avg / 残留(n_sentences=0 且文本非空)`，
   再取块内唯一结论行（`验证合格` / `验证明不合格` ＋ `（exit N）`）。
   另解析 §2「主控实测现状」块做**文档内两处对账**（`count − sum(>0)` 必须等于 §4 残留列）。
2. **实跑侧**（子进程）：`sys.executable scripts/backfill_v2_sentences.py --verify`，
   库指向 `tmp_path` 下的**临时夹具库**（`LG_DATABASE_URL` 覆盖），**零真库、零模型调用**。
   干净夹具 ⇒ `exit 0` ＋ 逐版本与合计「残留」全 0；脏夹具（多留一行非空文本 0 值）⇒ `exit 1` ＋ 残留 1。
3. **方向判据（唯一变异点）**：`conclusion_agrees()`（结论词比对）∧ `residual_agrees()`（残留数比对），
   两者都要求与实跑残留**同向**，且是**双射**（实测残留=0 ⇔ 文档给肯定结论）——
   任一侧单独翻转都判红；「实测有残留且文档同步改反向」时判**绿**（证明不是「见残留就红」的万能红）。
4. **fail-closed**：缺文档 / 缺脚本 / §4 锚点小节被删 / 围栏块数 ≠ 1 / 逐版本行解析不到 ⇒ 一律 `assert` 失败，
   **不 skip、不静默放行**。

| 用例（14 例，全绿） | 钉的是 |
|---|---|
| `test_gate_targets_exist_inside_repo` | 观测对象在本仓内且存在 |
| `test_fail_closed_when_doc_missing` / `..._script_missing` / `..._anchor_section_is_gone` | 缺件/漂移 fail-closed |
| `test_doc_claim_parses_residual_zero_rows_and_pass_conclusion` | 文档 §4 逐版本残留=0 ＋ 结论词肯定方向 ＋ `（exit 0）` |
| `test_doc_status_block_agrees_with_verify_block` | 文档内 §2↔§4 对账；`count−sum(>0)==残留` |
| `test_verify_subprocess_run_is_exit0_with_zero_residual` | **实跑** exit 0 ＋ 残留全 0 ＋ 逐版本 v1/v2 |
| `test_verify_subprocess_run_flags_residual_with_nonzero_exit` | **实跑** exit 1 ＋ 残留 1 ＋ 样本 id |
| `test_verify_subprocess_run_is_repeatable_and_read_only` | `--verify` 反复跑 stdout/exit 逐字不变（只读） |
| `test_doc_direction_matches_real_verify_run` | **核心**：文档结论方向 ⇄ 实跑残留同向 |
| `test_direction_gate_is_green_on_matching_failure_state` | 双射的另一半（反向态判绿） |
| `test_direction_gate_rejects_flipped_conclusion_word` | 结论词翻成反向说法 ⇒ 红 |
| `test_direction_gate_rejects_flipped_residual_count` | 残留 0 改成 7 ⇒ 红 |
| `test_direction_gate_rejects_optimistic_doc_when_real_residual_found` | 实跑有残留而文档仍说合格 ⇒ 红 |

口径边界（不夸大效力）：本门**只钉方向**，不核对文档里的 `14124` / `424294` 等绝对数字与真库是否逐位相符
（那需要真库只读复算，属主控验收会话职责）；夹具库与真库段数量级无关，故**不**断言两侧 `count` 相等
（断言了就是「拿夹具冒充真库」的假证据）。

## 3. 生成器假证据收口（`scripts/k5_supply_recount.py`）

旧 §0 是两行写死的 `→ exit 0`（复核 §3 注记 2：无论实跑成败都会印出 ⇒ 不构成执行证据）。现在：

- `render_markdown(res, run_rc=None)`：§0 改成两行表格，exit 单元格由 `_exit_cell(run_rc)` 渲染——
  传了 rc 就 `str(rc)`，没传就 `未实跑（模板占位，非本次实跑证据）`。
- 外部 pytest 命令恒为 `未取得（外部验收命令）`：**生成器不自证另一个进程的退出码**，不预置任何值。
- `main()` 把本次实跑返回码 `run_rc` 传进去；取不到库的失败路径在渲染前就 `return 2`，
  故带 exit 行的文档只在复算真正跑通后落盘（新增用例钉死「失败实跑不落盘」）。
- **未放宽任何既有门**：`--min-per-work`、口径 A/B/C 三档、`--print-only` 零写入、严格布尔 `src_ok` 口径
  （`COND_A` / `COND_A_STRICT` / `COND_B` / `COND_C` / `wl_legacy_len` 对照列）全部**逐字未动**
  （`git diff` 见 §1 表，本文件改动只落在 `render_markdown` 的 §0 块与 `main()` 的传参一行）。

新增 5 例（`tests/test_k5_supply_recount.py`）：

| 用例 | 钉的是 |
|---|---|
| `test_markdown_exit_status_is_derived_from_run_rc` | §0 的 exit 单元格 == 传入 rc（rc ∈ {0,1,2,7,255} 逐个验）＋ 外部行恒「未取得」 |
| `test_markdown_without_run_rc_is_marked_placeholder_not_evidence` | 未传 rc ⇒ 占位文案，**§0 里不得出现任何 `exit 0` 数值** |
| `test_generator_source_has_no_hardcoded_exit_zero_claim` | 源码级反硬编码：生成器里不许再出现 `→ exit` / `exit 0（` 字面量 |
| `test_cli_written_doc_exit_status_matches_return_code` | CLI 落盘文档 §0 的 exit == `main()` 返回码（且真实读数证据仍在） |
| `test_cli_failed_run_writes_no_doc_with_exit_row` | `--db` 不存在 ⇒ rc≠0 且**不落盘**带 exit 行的文档 |

## 4. 验收门实跑输出（原文粘贴）

命令（任务书给定，未改）：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_nsent_doc_direction.py tests/test_k5_supply_recount.py -q
```

**绿态（exit 0）**——注意本仓 `pyproject.toml` 有 `addopts = "-q"`，命令行再给 `-q` 净效果是 `-qq`
（全绿时 pytest 不打计数行），故另附一条 `-v` 形态给出计数：

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_nsent_doc_direction.py tests/test_k5_supply_recount.py -q
........................................                                 [100%]
RC=0

$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_nsent_doc_direction.py tests/test_k5_supply_recount.py -v
collected 40 items

tests\test_nsent_doc_direction.py ..............                         [ 35%]
tests\test_k5_supply_recount.py ..........................               [100%]

============================= 40 passed in 7.41s =============================
RC=0
```

回归面（顺带跑的既有门，从干净树跑、复现两次全绿）：

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k5_criteria_check.py \
    tests/test_k5_doc_conclusion_direction.py tests/test_k5_promotion_wire_probe.py \
    tests/test_k5_promotion_wire_probe_ledger.py tests/test_k5_sourcecheck_coverage.py \
    tests/test_backfill_v2_sentences_scope.py tests/test_import_v2_sentcount.py \
    tests/test_compile_all.py tests/test_brief_cost_numbers.py -q
........................................................................ [ 75%]
........................                                                 [100%]
============================== warnings summary ===============================
tests/test_compile_all.py::test_all_python_files_compile
  F:\agi\_scratch\worktrees\nsent-verify-gate\app\api.py:880: DeprecationWarning: invalid escape sequence '\_'
    Candidate.prompt_version.like("corrupt\_%", escape="\\"))

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
RC=0
```

**如实记录一处环境坑（与本次改动无关，但会影响复核者复跑）**：在本工作树跑**全量**套件
（`pytest -q`，约 200+ 文件）会自造一个**空的** `data/language_genome.db`（0 字节，落在 `.gitignore` 的
`data/` 下，疑似某既有测试未清 `LG_DATABASE_URL` 就让 `app.db` 落到默认路径）。此后**任何**后续 pytest
会话里 `test_k5_criteria_check.py` / `test_benchmark_subs.py` / `test_factorial_commit_guards.py` /
`test_stratified_batch.py` 共 14 例会以 `sqlite3.OperationalError: no such table: knowledge_packages` 变红。
本任务与该现象无关，两条独立证据：

1. `pytest -q` 与 `pytest -q --ignore=tests/test_nsent_doc_direction.py` 两次全量跑的 `FAILED` 列表
   **逐条相同**（各 14 条，本任务新增 0 条红灯）；
2. 删掉那个空库后，上面的 9 文件回归组复跑 **96 passed / exit 0**（连跑两次均绿）。

该空库与 `.pytest_cache/` 均由 pytest 自身产生（live-guard 锁目录 + pytest 缓存），本 worker 已在交付前
清理；**下次跑本任务的验收命令会重新生成 `data/`（空目录）与 `.pytest_cache/`**，属 pytest 固有产物。


## 5. 反向验证（任务书硬性要求）：变异 → 判红 → 复原 → 复绿

三次变异全部只改工作树内白名单文件、**只临时改**、跑完立即复原；复原证据见 §5.4。
命令与 §4 相同（`… -q`），输出原文粘贴。

### 5.1 变异点 A：结论词比对退化成恒真

改 `tests/test_nsent_doc_direction.py`：

```python
 def conclusion_agrees(measured_residual: int, claim: DocClaim) -> bool:
     """结论词比对（反向验证变异点 A）：结论词与 `（exit N）` 标记同向于实跑残留。"""
-    expected_pass = measured_residual == 0
-    return ((claim.conclusion == PASS_WORD) == expected_pass
-            and (claim.exit_marker == 0) == expected_pass)
+    return True   # 退化成恒真
```

输出原文（**2 条红、exit 1**）：

```
...........F.F..........................                                 [100%]
================================== FAILURES ===================================
_____________ test_direction_gate_rejects_flipped_conclusion_word _____________
…
>       assert conclusion_agrees(clean_run.residual_total, claim) is False, \
            "结论词翻成反向说法后本门仍判绿 ⇒ 门钉不住方向"
E       AssertionError: 结论词翻成反向说法后本门仍判绿 ⇒ 门钉不住方向
E       assert True is False
E        +  where True = conclusion_agrees(0, DocClaim(rows={1: {'count': 14124, 'pos': 14124, 'res': 0}, 2: {'count': 424294, 'pos': 424294, 'res': 0}}, …, conclusion='验证明不合格', exit_marker=1))
tests\test_nsent_doc_direction.py:400: AssertionError
_____ test_direction_gate_rejects_optimistic_doc_when_real_residual_found _____
…
>       assert conclusion_agrees(dirty_run.residual_total, claim) is False, \
            "实跑有残留而文档结论词仍是肯定方向，本门仍判绿 ⇒ 门钉不住方向"
E       AssertionError: 实跑有残留而文档结论词仍是肯定方向，本门仍判绿 ⇒ 门钉不住方向
E       assert True is False
tests\test_nsent_doc_direction.py:421: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_nsent_doc_direction.py::test_direction_gate_rejects_flipped_conclusion_word
FAILED tests/test_nsent_doc_direction.py::test_direction_gate_rejects_optimistic_doc_when_real_residual_found
RC=1
```

（首条红里 `conclusion='验证明不合格', exit_marker=1` 就是**把文档结论词手工改成反向说法**后的解析结果；
第二条红里 `VerifyRun(rc=1, … 'res': 1)` 是**实跑真出现残留**而文档仍说合格。）

### 5.2 变异点 B：残留数比对退化成恒真

```python
 def residual_agrees(measured_residual: int, claim: DocClaim) -> bool:
     """残留数比对（反向验证变异点 B）：文档 §4 残留列的「是否全 0」同向于实跑残留。"""
-    return all(r == 0 for r in claim.residuals.values()) == (measured_residual == 0)
+    return True   # 退化成恒真
```

输出原文（**1 条红、exit 1**）：

```
............F...........................                                 [100%]
================================== FAILURES ===================================
_____________ test_direction_gate_rejects_flipped_residual_count ______________
…
>       assert residual_agrees(clean_run.residual_total, claim) is False, \
            "残留数被改成非 0 而实跑残留=0，本门仍判绿 ⇒ 门钉不住方向"
E       AssertionError: 残留数被改成非 0 而实跑残留=0，本门仍判绿 ⇒ 门钉不住方向
E       assert True is False
E        +  where True = residual_agrees(0, DocClaim(rows={1: {'count': 14124, 'pos': 14124, 'res': 7}, 2: {'count': 424294, 'pos': 424294, 'res': 0}}, …, conclusion='验证合格', exit_marker=0))
tests\test_nsent_doc_direction.py:412: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_nsent_doc_direction.py::test_direction_gate_rejects_flipped_residual_count
RC=1
```

（`'res': 7` = 把文档 §4 的残留 0 手工改成 7；实跑 `residual_total=0`。）

### 5.3 变异点 C：把生成器的硬编码 `→ exit 0` 放回去

```python
 def _exit_cell(run_rc: int | None) -> str:
-    return PLACEHOLDER_EXIT if run_rc is None else str(run_rc)
+    return "→ exit 0（stdout 与本文件同口径逐作品输出，可复算）"
```

输出原文（**4 条红、exit 1**）：

```
...................................FFFF.                                 [100%]
================================== FAILURES ===================================
______________ test_markdown_exit_status_is_derived_from_run_rc _______________
…
>           assert cells == [str(rc), k5sr.EXTERNAL_EXIT], \
                f"传入 rc={rc} 时 §0 的 exit 单元格应逐字等于它（自跑行）+ 未取得（外部行）"
E           AssertionError: 传入 rc=0 时 §0 的 exit 单元格应逐字等于它（自跑行）+ 未取得（外部行）
E           assert ['→ exit 0（st...'未取得（外部验收命令）'] == ['0', '未取得（外部验收命令）']
E             At index 0 diff: '→ exit 0（stdout 与本文件同口径逐作品输出，可复算）' != '0'
tests\test_k5_supply_recount.py:433: AssertionError
（中段 110 行为既有 k5 夹具用例的 stdout 回显，与本变异无关，此处从略）
=========================== short test summary info ===========================
FAILED tests/test_k5_supply_recount.py::test_markdown_exit_status_is_derived_from_run_rc
FAILED tests/test_k5_supply_recount.py::test_markdown_without_run_rc_is_marked_placeholder_not_evidence
FAILED tests/test_k5_supply_recount.py::test_generator_source_has_no_hardcoded_exit_zero_claim
FAILED tests/test_k5_supply_recount.py::test_cli_written_doc_exit_status_matches_return_code
RC=1
```

### 5.4 复原证据（`git diff` 复原后为空、标记清零、复绿）

```
$ git status --short
 M scripts/k5_supply_recount.py        ← 本任务的最终改动（非变异残留）
 M tests/test_k5_supply_recount.py      ← 本任务的最终改动（非变异残留）
?? tests/test_nsent_doc_direction.py    ← 本任务新建
?? PORT_nsent_direction_gate.md         ← 本文件

$ grep -c MUTATION tests/test_nsent_doc_direction.py
MUTATION=0
$ grep -c "MUT-C" scripts/k5_supply_recount.py
MUT-C=0

$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_nsent_doc_direction.py tests/test_k5_supply_recount.py -v
collected 40 items

tests\test_nsent_doc_direction.py ..............                         [ 35%]
tests\test_k5_supply_recount.py ..........................               [100%]

============================= 40 passed in 7.41s =============================
RC=0
```

（三个变异文件均以仓外备份件逐字节回写；`git diff --stat` 只剩 §1 表所列的两处**最终**改动，
无任何变异残留。）

## 6. 纪律声明

- **零真库**：新门全部子进程都把 `LG_DATABASE_URL` / `LG_DATA_DIR` 覆盖到 `tmp_path`；
  未读、未写 `F:/agi/language-genome/data/language_genome.db`；`k5_supply_recount.py` 只对
  `tmp_path` 夹具库以 `mode=ro` 打开。
- **零模型网关调用**：新门不 import 任何网关模块；子进程环境 `LG_LLM_MODE=mock`。
- **零 git 写操作**：只用过 `status` / `diff` / `log`；未 commit / merge / push / stash。
- **未越界编辑**：只改白名单 4 文件；仓内无 scratch / smoke / repro / 临时脚本
  （`git status` 只有上表 4 项；反向验证的备份与输出落在仓外
  `C:/Users/6/AppData/Local/Temp/opencode/`）。
- **未改主仓与其他 worktree**；未动 `docs/n_sentences回填口径_20260925.md`
  （反向验证的「改结论词」在门内以**内存副本**完成，零落盘，仓库文件字节数不变）。

## 7. 留给主控的边界与后续（如实列）

1. **已提交文档 `docs/K5供给口径真计数_20260925.md` 的 §0 仍是旧形态**（两行写死的 `→ exit 0`）——
   本任务白名单不含该文档，未改。下次真跑 `scripts/k5_supply_recount.py`（默认模式）会用新 §0 覆写它；
   **在此之前**该文档的 §0 exit 仍不构成执行证据（其 §1/§2/§3/§5 数字由 `res` 真跑派生，方向正确）。
2. **本门钉方向不钉绝对数字**：文档 §2/§4 的 `14124` / `424294` / `avg` 与真库是否逐位相符，
   仍需主控在授权会话跑真库只读复算（`k5_supply_recount.py --print-only` 或裸 SQL）核对；
   复核报告 §1.1/§1.3 的真库读数（v2 `n_sentences=0` 行数 = 0、avg 2.430675427887267）与本门
   实跑方向一致，但本 worker **未亲跑真库**，不在此替主控背书。
3. `docs/n_sentences回填口径_20260925.md` §4 自陈「本席未取得真库原始输出」、§5 自陈「未跑」——
   本门只保证「文档结论方向与 `--verify` 实跑方向同向」，不替该文档把「预期格式」升级为「实跑原文」。
4. 全量套件在本工作树的自污染坑（跑一次全量 → 留下空 `data/language_genome.db` → 之后 14 例
   `no such table: knowledge_packages` 红灯，§4 末段已给两条独立归因证据）**未修**：
   定位并修它需要动白名单外的既有测试或 `app/db.py` 的默认库解析，越界，留给主控处置。
