# DELIVERY — segments 列级瘦身迁移（slim_segments）

工作树：`F:\agi\_scratch\worktrees\lg-segments-slim`（分支 `task/lg-segments-slim`）。
本轮**只在合成小副本（2000 行）上真跑**，**未在活库 `D:/language-genome-data/language_genome.db` 上执行任何写操作（含 VACUUM）**——工具默认拒绝活库路径（见下 D 段实测）。

## 交付物（全部在允许编辑白名单内）

| 文件 | 状态 | 说明 |
|---|---|---|
| `scripts/slim_segments.py` | 新增 | 离线迁移器：a cleaned 列 / b 归 NULL / c integrity 回填 / d VACUUM，分步、逐批打印行数与耗时；默认拒活库、`--dry-run`/`--limit`/`--vacuum`/`--batch`/`--force-live` |
| `tests/test_slim_segments.py` | 新增 | 真 SQLite 小副本（2000 行，等值/不等值/空 text_clean/未清洗/JSON+紧凑+带附加键 integrity/含中文换行）逐行回归 |
| `docs/SEGMENTS_SLIM.md` | 新增 | 口径、读侧回退逐条文件:行号、cleaned 列对 console 的影响、回滚、复核 SQL、遗留 |
| `DELIVERY_segments_slim.md` | 新增 | 本文件 |
| `app/console.py` | 改（1 处） | `_corpus`「已清洗」聚合改为 `SUM(cleaned)`，`cleaned` 列不存在时回落旧口径（保证测试库/迁移前不红）；其余 app/ 一行未动 |

## 验收命令与真跑输出原文

### 1) 单测（新增回归）—— **12 passed**

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_slim_segments.py -q
→ ............ [100%]  12 passed in 1.97s
```

断言逐条覆盖任务 §2.2 硬口径：行数不变；`id/work_id/ordinal/n_chars/created_at` 逐行不变；
`text` 逐字节不变；**有效正文两口径**（`text_clean if not None else text` 与 `text_clean or text`）逐字节不变；
`integrity` 前后 `loads_any` 等价 + `LIKE 'i1:1%'` 仍命中 + 带 `src_ok` 行原样不动；
`cleaned` 计数 == 迁移前 `SUM(text_clean IS NOT NULL)`；**反向用例**（改脏 text 后重跑仍为脏值、他行不动）；
活库路径不加 `--force-live` 拒绝执行；`--dry-run` 零写入（不建列、一行不 NULL）；`--limit` 只处理范围内行。

### 2) 相关回归（`test_models_content_anchor.py` 不存在，改用真实对应文件）—— **112 passed**

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest \
  tests/test_compact_integrity.py tests/test_console.py \
  tests/test_import_v2_textclean.py tests/test_anchor_check.py \
  tests/test_freeze_fingerprint_content.py -q
→ 112 passed, 2 warnings in 5.56s
```
（`app/models.py:125` 的内容锚口径真实落在 `scripts/register_work_sources._work_sha256` 与
`app/semantic_review.py`；本仓无 `test_models_content_anchor.py`，故按任务 §4 用现存回归文件替代：
integrity 紧凑编码 / console「已清洗」读数 / import v2 的 text_clean / 锚定与内容冻结。）

### 3) 小副本 dry-run（零写入）—— **exit=0**

```
$ python scripts/slim_segments.py --db /tmp/sc.db --dry-run
[slim] db=/tmp/sc.db
[slim] segments 总行数=2000；本轮处理范围 rowid<=∞ (全库)；文件大小=794,624 B；bytes/row=397.3
[slim][a] DRY-RUN：将 ADD COLUMN cleaned INTEGER NOT NULL DEFAULT 0
[slim][a] DRY-RUN：将 UPDATE cleaned=1，命中 1600 行（口径同 console「非 NULL 即已清洗」）
[slim][预] 将归 NULL 的等值 text_clean：800 行 / 82,400 B；将紧凑化的 JSON integrity：667 行 / 123,395 B；合计预估节省 205,795 B，耗时 0.01s
[slim][b/c/d] DRY-RUN：零写入，跳过实际 UPDATE / VACUUM
────────────────────────────────────────────────────────────────────
[slim][结果]（DRY-RUN 预估，未落盘）
  文件大小：前 794,624 B（0.8 MiB） → 后 794,624 B（0.8 MiB），实际差 0 B
  bytes/row：前 397.3 → 后 397.3（本机范围实测）
  逻辑节省字节（text_clean+integrity 载荷）：205,795 B ⇒ 折算 bytes/row 净减 102.9 B
  按 105,283,111 行折算全库预估：前 38.96 GiB → 瘦身后约 28.87 GiB（不含 VACUUM 回收的页内碎片）
```

### 4) 小副本正式跑 + VACUUM —— **exit=0，文件 794,624 → 561,152 B（bytes/row 397.3 → 280.6）**

```
$ python scripts/slim_segments.py --db /tmp/sc.db --vacuum
[slim] db=/tmp/sc.db
[slim] segments 总行数=2000；本轮处理范围 rowid<=∞ (全库)；文件大小=794,624 B；bytes/row=397.3
[slim][a] 已补列 cleaned INTEGER NOT NULL DEFAULT 0
[slim][a] cleaned=1 回填：写 1600 行 （范围内 text_clean 非 NULL 计 1600），耗时 0.01s
[slim][预] 将归 NULL 的等值 text_clean：800 行 / 82,400 B；将紧凑化的 JSON integrity：667 行 / 123,395 B；合计预估节省 205,795 B，耗时 0.01s
[slim][b] rowid [1..2000]：归 NULL 800 行（累计 800），耗时 0.01s
[slim][c] rowid [1..2000]：紧凑化 667 行（累计 667/667），耗时 0.01s
[slim][核] 范围内 cleaned=1 计数=1600（本轮迁移前 text_clean 非 NULL=1600，重跑时 cleaned 保留历史故可 ≥ 该值）；残留等值 text_clean=0（应=0）
[slim][d] VACUUM 完成，耗时 0.01s
────────────────────────────────────────────────────────────────────
[slim][结果]
  文件大小：前 794,624 B（0.8 MiB） → 后 561,152 B（0.5 MiB），实际差 233,472 B
  bytes/row：前 397.3 → 后 280.6（本机范围实测）
  逻辑节省字节（text_clean+integrity 载荷）：205,795 B ⇒ 折算 bytes/row 净减 102.9 B
  按 105,283,111 行折算全库预估：前 38.96 GiB → 瘦身后约 28.87 GiB（不含 VACUUM 回收的页内碎片）
```

跑后独立读回核对（另一次一次性查询）：
`rows=2000`（不变）、`残留等值 text_clean=0`、`cleaned=1 计数=1600`（== 迁移前非 NULL 数）、
`integrity LIKE 'i1:1%'` 命中 `1334`（>0，紧凑 eligible 前缀位仍可被 SQL 筛）。

### 5) `--limit`（小库验证：只处理前 N 行）—— **exit=0**

```
$ python scripts/slim_segments.py --db /tmp/sc2.db --limit 500
[slim] segments 总行数=2000；本轮处理范围 rowid<=500 (--limit 500)；文件大小=794,624 B；bytes/row=397.3
[slim][a] cleaned=1 回填：写 400 行 （范围内 text_clean 非 NULL 计 400），耗时 0.01s
[slim][b] rowid [1..500]：归 NULL 200 行（累计 200），耗时 0.00s
[slim][c] rowid [1..500]：紧凑化 167 行（累计 167/167），耗时 0.01s
[slim][核] 范围内 cleaned=1 计数=400；残留等值 text_clean=0（应=0）
```
范围外（rowid>500）的等值行仍原样保留（回归 `test_limit_scope_and_vacuum` 钉：`rowid>500` 等值行 > 0）。

### 6) 活库守卫（不加 `--force-live`）—— **exit=2，零写入**

```
$ python scripts/slim_segments.py --db "D:/language-genome-data/language_genome.db"
[slim] 活库需 --force-live：D:/language-genome-data/language_genome.db 判定为活库路径，默认拒绝对活库执行任何写操作（含 VACUUM）。
exit=2
```

## 前后读数（合成小副本，非活库）

| 指标 | 前 | 后 |
|---|---|---|
| 文件大小 | 794,624 B | 561,152 B |
| bytes/row | 397.3 | 280.6 |
| 行数 | 2000 | 2000（不变） |
| 归 NULL 的等值 text_clean | — | 800 行 / 82,400 B |
| 紧凑化 integrity | — | 667 行 / 123,395 B |

## 遗留 / 未做（如实）

- **未在活库上跑**：活库真实 before/after（14.9 MB/本 → 目标 ≤10 MB/本）**尚未在 `D:/…` 的拷贝上实测**。
  上表数字全部来自 2000 行**合成 fixture**，其 bytes/row 与真库语料负载不同；「按 105,283,111 行折算」
  那两行是对**合成 bytes/row** 的机械外推，**不等于**真库 56.5 GB 的实测收益，仅作口径演示。
  正式执行须在活库拷贝上重跑本工具、以工具打印的活库真实前后读数为准。
- `integrity` 带附加键（`src_ok`/`severity`/`clean_pending_llm`/…）的行永不紧凑（K5 裸 SQL JSON1 读的硬约束），非未做完。
- `cleaned` 列不进 ORM（`app/models.py` 未动）、不进 `app/db.py::_migrate`；`console.py` 的回落分支保证未补列的库仍按旧口径读数。
- 本轮只做「列级瘦身 + integrity 回填」，未做 PK 改造、未删表/重建表。
