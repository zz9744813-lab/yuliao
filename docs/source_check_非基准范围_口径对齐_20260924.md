# `source_check --scope nonbench` 与 K2 试点供给口径对齐（排除 fixture/synthetic/commentary）

日期：2026-09-24
范围：仅改 `scripts/source_check.py`；回归测试追加于 `tests/test_source_check_nonbench_scope.py`；本文档为交付证据。
未改：`app/knowledge_query.py`、`scripts/k2_extract_backfill.py` 语义；`app/knowledge_query.py` 仅被**单源引用**。

## 1. 对齐结论（一句话）

`source_check.py` 的 `nonbench` 口径现在与 K2 试点供给侧**逐字一致**：在既有的
`nonbenchmark_compliant_source` 白名单之上，**追加**排除集
`source_type ∈ {fixture, synthetic, commentary}`，且该排除集**单源复用**
`app.knowledge_query.DEFAULT_EXCLUDED_SOURCE_TYPES`（K2 侧 `k2_extract_backfill`
经 `from app import knowledge_query as KQ` → `KQ.DEFAULT_EXCLUDED_SOURCE_TYPES` 同源消费），
`import` 不到即 fail-closed 报错退出，绝不在 `source_check` 里另写一套。

## 2. 单源复用落点（不许另写一套）

| 项 | 落点 | 说明 |
| --- | --- | --- |
| 白名单 | `k2_extract_backfill.nonbenchmark_compliant_source` | 已在 `source_check` 复用（`import k2_extract_backfill as k2b`），未改 |
| 排除集 | `app.knowledge_query.DEFAULT_EXCLUDED_SOURCE_TYPES` | 新增长常量 `NONBENCH_EXCLUDED_SOURCE_TYPES = _KQ.DEFAULT_EXCLUDED_SOURCE_TYPES`（`_KQ = from app import knowledge_query as _KQ`），`is` 同一对象 |
| 同源链 | `k2_extract_backfill` 内 `KQ.DEFAULT_EXCLUDED_SOURCE_TYPES` | 与 `source_check` 指向同一个 `app.knowledge_query.DEFAULT_EXCLUDED_SOURCE_TYPES` |

fail-closed 写法（模块级，import 阶段即生效）：

```python
from app import knowledge_query as _KQ
try:
    NONBENCH_EXCLUDED_SOURCE_TYPES = _KQ.DEFAULT_EXCLUDED_SOURCE_TYPES
except AttributeError as _e:
    raise RuntimeError(
        "无法单源复用 K2 侧排除集常量 "
        "(app.knowledge_query.DEFAULT_EXCLUDED_SOURCE_TYPES)："
        f"{_e}。nonbench 口径必须与 K2 同源，已 fail-closed 退出，禁止另写一套。"
    ) from _e
```

nonbench 分支（双闸同判据）：

```python
compliant = {wid for wid, ws in reg.items()
             if k2b.nonbenchmark_compliant_source(ws.source_type)
             and (ws.source_type or "") not in NONBENCH_EXCLUDED_SOURCE_TYPES}
```

## 3. 真实运行数字（只读复演，零生产库写入、零真实 LLM 调用）

机制：与 `tests/conftest.py` 同——临时 SQLite + mock LLM，直接跑真实
`source_check.targets()` 代码路径（SELECT 只读，不提交任何写入）。种子库覆盖
合规人类语料（`human_fiction` ×train/NULL、`production_nonbenchmark_k2v2` ×train）、
排除集反面（`fixture` / `synthetic` / `commentary` ×train）、以及边界
（`human_fiction` ×benchmark）。

| 指标 | 值 |
| --- | --- |
| `nonbench` 计数（对齐前，仅白名单） | **3** |
| `nonbench` 计数（对齐后，白名单 + 排除集） | **3** |
| `--work-id WK-0d48cc2e`（fixture 作品）收窄计数（对齐后） | **0** |
| 排除集实际值 | `commentary` / `fixture` / `synthetic` |
| `source_check` 常量 `is` `app.knowledge_query` 常量（单源同源） | **True** |

读：`WK-0d48cc2e` 是 fixture 来源作品，对齐后 `--work-id` 收窄结果为**空集**，
符合任务书「after alignment fixture = 0」。

### 关于「before == after」的诚实说明

对齐前后 `nonbench` 计数均为 3，是因为排除集 `{fixture, synthetic, commentary}`
与白名单 `{human_fiction, production_nonbenchmark_*}` 对**真实来源类型值**无交集
（fixture/synthetic/commentary 本就不命中白名单，白名单也绝不产生这三类）。
故本次对齐对真实数据的选中集合**不产生行为差异**——它是 K2 侧 K3 证据闸
（`k3_evidence_preview` 中 `KQ.DEFAULT_EXCLUDED_SOURCE_TYPES` 同款排除）的
**口径镜像与防御性双闸**，目的是让 `source_check` 与 K2 侧**逐字一致**、并在未来
出现「前缀匹配白名单却属合成派生」的异常来源类型时不被放开。这一点已在回归测试
`test_nonbench_exclusion_single_source_no_drift` 中以「单源 `is` 同一对象 + 不另写字面量」
钉死，杜绝静默抄写漂移。

## 4. 回归测试（追加 3 例，既有 7 例语义未动）

文件：`tests/test_source_check_nonbench_scope.py`（原 7 例 + 新增 3 例 = 10 例）

新增三条：
1. `test_nonbench_fixture_excluded_via_work_id_narrow` —— fixture 作品经 `--work-id`
   收窄仍为空集（不报错、不放行）；合规作品收窄正常返回，证明未误伤。
2. `test_nonbench_compliant_human_corpus_still_selected` —— 正向面：合规人类语料
   （human_fiction 的 train/NULL、production_nonbenchmark_*）仍全进池；排除集反面
   （fixture/synthetic/commentary）一律不进；与独立参考实现逐项相等。
3. `test_nonbench_exclusion_single_source_no_drift` —— 直读 `app/knowledge_query.py`
   与 `scripts/k2_extract_backfill.py` 字面量比对：排除集值 = {fixture, synthetic,
   commentary}；`source_check.NONBENCH_EXCLUDED_SOURCE_TYPES is app.knowledge_query.
   DEFAULT_EXCLUDED_SOURCE_TYPES`（单源同源）；`k2_extract_backfill` 经
   `KQ.DEFAULT_EXCLUDED_SOURCE_TYPES` 同源消费；`source_check` 本体不得出现第二套
   排除集字面量（`{"fixture", "synthetic", "commentary"}` 与 `frozenset({"fixture"` 均不得出现）。

既有 7 例（含 `test_nonbench_compliance_single_source_no_drift` 对白名单单源的钉死、
`test_cli_scope_choices_and_help_write_semantics` 对 help 语义的钉死）全部保持绿灯，
且 help 文本已同步写入「排除集 DEFAULT_EXCLUDED_SOURCE_TYPES（fixture / synthetic /
commentary，与 K2 侧同源）」。

## 5. 验收

命令：
```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest \
    tests/test_source_check_nonbench_scope.py tests/test_source_check_strict_bool.py -q
```
结果：**21 passed，exit 0**（nonbench_scope 10 例 + strict_bool 11 例）。

纪律复核：
- 零真实 LLM 调用（`LG_LLM_MODE=mock`；未跑 `--run`）。
- 零生产库写入（仅临时 SQLite + SELECT 只读复演；未提交任何 INSERT/UPDATE）。
- 仅 3 个文件变动：`scripts/source_check.py`、`tests/test_source_check_nonbench_scope.py`、
  `docs/source_check_非基准范围_口径对齐_20260924.md`。未提交、未推送。
