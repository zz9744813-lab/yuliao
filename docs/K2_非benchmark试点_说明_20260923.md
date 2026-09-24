# K2 非 benchmark 试点 · 说明（2026-09-23）

派工：审计 `language-genome-code-audit-20260923.md` P0 主线第 1 条。
落点：`scripts/k2_extract_backfill.py`（抽取侧）+ `tests/test_k2_extract_nonbenchmark.py`。
**本任务只做抽取侧供给：`app/knowledge_query.py`（K3）一行未改、不放宽。**

---

## 1. 要解决的口径问题（不重新论证，直接入账）

K2 放量驱动的历史口径：合格段宇宙 = `segments.role == 'benchmark'` 且
`integrity.src_ok is True` 且 `text_clean` 非空——**只看 role，对「benchmark 段」
没有显式区分与开关**。而 K3 侧（`app/knowledge_query.py::_evidence_for`）把
`role == 'benchmark'` 的实例**硬剔除**（基准上下文泄漏）。两侧合起来的结果：

> 真库 82 条 StrategyInstance 100% 来自 benchmark 段 ⇒ **可进 K3 的生产证据 = 0**。

因此新增**显式来源口径开关** `--source-scope`，默认关闭新通道（现状不变）。

## 2. `--source-scope` 的两值语义（与 `--help` 一致）

| 取值 | 段准入 | 来源准入 | 说明 |
|---|---|---|---|
| `benchmark`（默认） | `role == 'benchmark'` | 无额外要求（既有口径） | **逐字保持现状**：取段集合、排序、幂等位、预算闸、双闸门全部不变 |
| `nonbenchmark` | `role != 'benchmark'`（NULL / train 都算非基准） | `work_sources.source_type` ∈ 显式白名单 | 试点通道：合规人类语料才收；被排除的来源与段数逐个留痕 |

**两条准入都必须显式判定，缺一不可：**

1. **来源白名单（唯一入口 `nonbenchmark_compliant_source()`）**：
   · 精确值 `human_fiction`（K1-A 登记的根/镜像人类源）；
   · 前缀 `production_nonbenchmark_`（真库试点源，如 `production_nonbenchmark_k2v2`）。
   其余（`fixture` / `synthetic` / `commentary` / 空 / 未登记 / 大小写或拼写变体）
   一律**不收**——判定前对 `source_type` 去首尾空白，不做大小写归一。
2. **段 role（`_role_passes_scope()`）**：判据是「**不等于** `benchmark`」，
   与 K3 的硬口径同词表；**不是**「role 为 null 就算非基准」的模糊口径。
   SQL 侧为 `or_(role IS NULL, role != 'benchmark')`（避免 `!=` 在 SQL 里静默
   吞掉 NULL 造成口径漂移），Python 侧再复核一遍并计数（双保险留痕）。

`src_ok` / `text_clean` / `text_version`（K1-A 登记）三道既有闸**照旧**生效——
新通道只加来源口径，不放松任何既有门。

## 3. 命令

```bash
PY=F:/Hermes/hermes-agent/venv/Scripts/python.exe

# 预演（零真实调用、零库写）：默认口径 = 现状
$PY scripts/k2_extract_backfill.py --dry-run

# 预演：非 benchmark 试点通道（本任务新增）
$PY scripts/k2_extract_backfill.py --dry-run --source-scope nonbenchmark

# 真跑（待主控拍板；本任务未跑，禁止在未拍板时跑）
K2_ALLOW_LIVE=1 LG_LLM_MODE=real $PY scripts/k2_extract_backfill.py --live \
    --source-scope nonbenchmark --extractor-model deepseek-v4.1-flash --limit 1
```

`--live` 双闸保持 fail-closed：`K2_ALLOW_LIVE=1` **且** `LG_LLM_MODE=real`
（再加 `--extractor-model` + 池预检 + R6 互斥锁前置）——缺任一即 `SystemExit`
且零调用；`--dry-run`/`--live` 二者必显式取其一。

## 4. `--dry-run` 报告的核对点

`main()` 末行摘要 + 完整 JSON（`indent=1`）。JSON 里与本任务相关的键：

```
source_scope                 本次口径（benchmark | nonbenchmark）
n_candidate_sources          候选来源数（进入判定的 work 数）
n_pending_pairs              可配对总数（轮转取对的原料）
would_attempt                limit 内本轮会尝试的对数
skips.n_eligible_segments    合格段数
skips.source_scope           同 source_scope
skips.n_sources_seen / n_sources_qualified / n_sources_excluded
skips.excluded_sources[]     每个被排除来源一行：work_id / title / source_type /
                             text_version / n_segments / n_segments_in_scope /
                             n_segments_dropped_by_scope /
                             n_segments_dropped_by_source_gate / n_segments_kept /
                             segments_by_role / reason
skips.qualified_sources[]    合格来源同字段（reason 缺省）——看「收进来的为什么是这些」
skips.excluded_segments{}    段级排除计数：role_benchmark / src_ok_not_true /
                             text_clean_empty
skips.segments_by_role{}     入池段的 role 分布（nonbenchmark 口径下 benchmark 恒 0）
skips.segments_dropped_by_scope  被口径（role）挡在门外的段数
k3_preview                   只读预演 K3：{pairs, would_reach_k3, stripped{reason:n}}
```

来源明细上限 `MAX_TRACE_SOURCES=500`，超出计入 `*_sources_truncated`；
局部核对（只看试点源）用 `segment_universe(..., work_filter=(work_id, ...))`
——纯确定性过滤器，不改任何判定口径。

**排除原因码**（`skips.excluded_sources[].reason`）：

| reason | 含义 |
|---|---|
| `no_registry` | 该 work 无 `work_sources` 登记行（未登记=不可判定=不收） |
| `source_type_not_compliant:<type>` | 来源类型不在非基准白名单（含 fixture/synthetic/commentary） |
| `no_nonbenchmark_role_segment` | 登记合规但没有 `role != 'benchmark'` 的段 |
| `no_benchmark_role_segment` | 默认口径的对称项：没有基准段 |
| `source_gate_failed:src_ok_or_text_clean` | 有口径内段，但全被 `src_ok` / `text_clean` 闸挡下 |
| `no_segments` | 该 work 没有段 |

「为什么 82 条全是 benchmark」的核对路径：`--dry-run`（默认口径）看
`k3_preview.stripped == {"benchmark_source": N}` 且 `would_reach_k3 == 0`；
再 `--source-scope nonbenchmark` 看候选来源与各来源的排除原因。

## 5. `k3_preview` 的边界（重要）

`k3_evidence_preview()` 是**只读预演**：按 `app/knowledge_query._evidence_for`
的**同一顺序、同一常量**（`DEFAULT_EXCLUDED_SOURCE_TYPES` /
`DEFAULT_EXCLUDED_USES` / `DEFAULT_ALLOWED_TEXT_VERSIONS`）统计「抽到的对能不能
进 K3」。它：

- 不参与本驱动任何过滤决策（不改抽取行为）；
- 不修改、不放宽 K3 一行（K3 的 benchmark 段硬剔除保持原样）；
- 不复制 K3 的统计层口径（镜像 canonical 去重、区间合并、`allowed_text_versions`
  的调用方交集收窄）——只报来源闸命中数，宁可少报也不猜。

## 6. 试点源与**下游剩余门槛**（不粉饰）

已有材料：`WK-dc90993434e9《覆汉》`（263.6 万字 / 35,974 段 / 来源
`E:\小说_可分析` / `source_type=production_nonbenchmark_k2v2`，`human_fiction` 根已登记）。
抽取侧现在能为它开出非基准配对；但**能不能进 K3 不由本任务决定**，K3 侧还剩：

1. `segments.role != 'benchmark'` ——本通道的准入条件，已对齐（同词表）。
2. `work_sources.text_version ∈ {corpus-v1, corpus-v2-mirror}`
   （`DEFAULT_ALLOWED_TEXT_VERSIONS`，服务端**交集封顶**，调用方只能收窄）——
   试点源登记的 text_version 若不在这个集合里，抽出的实例**照样被剔除**。
   这是拍板项：要么按 K1-A 口径补登记版本标签，要么由主控显式扩 K3 的允许集
   （本任务无权、也没有顺手放宽）。`--dry-run` 的 `k3_preview.stripped` 会把它报出来。
3. `source_type ∉ {fixture, synthetic, commentary}` ——白名单天然满足。
4. `license_purposes ∩ excluded_uses == ∅` ——训练/再分发授权面是集霸的决策位。

另记一处**列宽风险**（本任务不改 `app/models.py`，越界）：
`work_sources.source_type` 声明 `String(20)`，而试点值
`production_nonbenchmark_k2v2` 为 28 字符、白名单前缀本身 24 字符。
SQLite 不校验长度（真库现状可跑），换 Postgres 时该值会撞列宽——
建议主控在切库前统一加宽列或改用短标签，并把这条纳入登记口径对账。

## 7. 回归与验收

```bash
$PY -m pytest tests/test_k2_extract_nonbenchmark.py -q          # 本任务新增，13 例
$PY -m pytest tests/ -q -k "k2 or extract"                      # 无旁路破坏
```

用例覆盖：① 默认口径与改造前历史实现逐项相等（含顺序）+ 既有报告键不变；
② nonbenchmark 收合规人类源（train 与 NULL 都算非基准）、排基准段、
排 fixture/synthetic/commentary/未登记，且排除账逐条留痕可核；
③ `--dry-run` 两口径零调用零库写（`extract_segment` 计数为 0 +
StrategyInstance 行数不变）并报出候选来源/合格段/可配对/逐来源原因；
④ `--live` 缺 `K2_ALLOW_LIVE` 或缺 `LG_LLM_MODE=real` → `SystemExit`
且预检/建队列/抽取零触达；⑤ 幂等与预算闸在 `nonbenchmark` 下照旧
（重跑 `skipped_done`、超限显式 `blocked_budget` 且已抽候选保留）；
⑥ CLI：`--source-scope` 只接两值、`--help` 写清两值语义、开关确实传到
`run_backfill`；⑦ `k3_preview` 复现 P0「恒 0」并如实报 text_version 剔除；
⑧ 缺登记/缺 text_version 的合规源照样 skip（不绕开 K1-A 闸）。
