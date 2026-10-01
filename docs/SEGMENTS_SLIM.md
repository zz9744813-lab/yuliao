# segments 表列级瘦身（scripts/slim_segments.py）

> 一句话：**离线**、在**副本**上跑、把「14.9 MB/本」打到「≤10 MB/本」的口径文档。
> 判据不是「看起来更小」，而是：**每一行的「有效正文」逐字不变 + 行数不变 + 内容锚不变。**

## 0. 为什么要瘦（真账）

主库 `language_genome.db` 现 ~56.5 GB / works=3788 / segments=105,283,111 行 ⇒ ~14.9 MB/本，
把入册闸（需 8 GiB）卡死。逐列实测（各抽样 30 万行，`avg(length(...))`）：

| 列 | 均值 | 处置 |
|---|---|---|
| `text` | 59.2 字符 | 正文，**不可动** |
| `text_clean` | 58.8 字符 | 94–99% 与 `text` 逐字相同 ⇒ **本轮唯一大项**：等值行归 NULL |
| `integrity` | 14.5 字符 | 97% 已是紧凑 `i1:…`；剩 2–3% 仍是 183+ 字节 JSON ⇒ 回填紧凑 |
| `id`/`work_id`/`created_at` | — | 外键/主键/占比小，**不动** |
| `chapter`/`role` | 0.0 | 空列，删列牵动 ORM，**不动** |

冗余主体 = `text_clean` 那份与 `text` 相同的拷贝（约占行负载 33%）。本轮**只做列级瘦身 +
integrity 回填**，不做 PK 改造、不删表、不重建表。

## 1. 迁移器做了什么（分步，每步可单跑、打印行数与耗时）

命令面：`--db`（必填）、`--dry-run`（零写入）、`--limit N`（只处理按 rowid 升序前 N 行）、
`--vacuum`（默认关：耗时且要 2 倍空间）、`--batch`（默认 20000）、`--force-live`。

- **a. `cleaned` 列**：`ALTER TABLE segments ADD COLUMN cleaned INTEGER NOT NULL DEFAULT 0`（若不存在），
  再 `UPDATE segments SET cleaned=1 WHERE text_clean IS NOT NULL`（含空串——与 console 旧口径逐行等价）。
- **b. 归 NULL**（分批 rowid 区间）：`UPDATE segments SET text_clean=NULL WHERE text_clean IS NOT NULL AND text_clean = text`。
  **`text_clean != text` 的行（真清洗结果、含空串清洗）原样保留。**
- **c. integrity 回填**：仍是基键 JSON 的行用 `app.segment_integrity.pack_raw()` 压成紧凑串；
  带附加键（`src_ok`/`severity`/…）、取值越界、非 JSON 的行 pack_raw **原样返回** ⇒ 不写。
- **d. `--vacuum`** 才 `VACUUM`；最后打印文件大小前/后、`bytes/row` 前/后、按 105,283,111 行折算的全库预估。

**默认拒绝对活库动手**：路径解析后等于 `D:/language-genome-data/language_genome.db` 或
`F:/agi/language-genome/data/language_genome.db`（大小写/斜杠归一比对）直接退出码 2 并打印
「活库需 --force-live」；只有显式 `--force-live` 才继续。

## 2. 为什么 `text_clean` 可以归 NULL —— 读侧处处是回退

**核心不变量**：迁移**只 NULL 那些 `text_clean == text` 的行**。对被 NULL 的行，
两种回退口径都落回 `text`；对没被 NULL 的行（`text_clean != text`）一个字不动。于是两种口径逐字节不变：

- 口径 A `text_clean if text_clean is not None else text`
  - 等值行：前 = `text_clean`（= text），后 = `text` ⇒ 相同。
- 口径 B `text_clean or text`（`""` 也回退到 text）
  - 等值行：前 = `text or text` = text，后 = `NULL or text` = text ⇒ 相同。
  - `text_clean = ""` 且 `text` 非空 ⇒ `"" != text` ⇒ **不 NULL**，两口径均不变。

**读侧消费者（逐条文件:行号）**：

| 位置 | 口径 | 迁移后 |
|---|---|---|
| `app/api.py:907` | `getattr(seg,"text_clean",None) or seg.text or ""` | B ⇒ 不变 |
| `app/semantic_review.py:182 / 193 / 262` | `text_clean if text_clean is not None else text` | A ⇒ 不变 |
| `app/models.py:125`（WorkSource.text_sha256 注释） | 内容锚「缺失用 text」 | 由下行实现 |
| `scripts/register_work_sources.py::_work_sha256`（:250） | `text_clean or text or ""`（按 ordinal 流式拼接的**内容锚**） | B ⇒ 不变 |
| `scripts/benchmark_build.py:93/274/414/469`、`export_training.py:566/860/960`、`goldpick_build.py`、`normalize_typos.py`、`corpus_fix_v2.py`、`k2_rereview_queue.py:223` | `seg.text_clean or seg.text` | B ⇒ 不变 |

内容锚（`work_sources.text_sha256`）按 ordinal 拼接每段 `text_clean or text`——被 NULL 的等值行贡献 `text`，
未动的行贡献原 `text_clean`，**逐段字节相同 ⇒ 整本 sha256 相同**。

## 3. `cleaned` 列对 `console.py` 的影响（唯一 SQL 聚合点）

`app/console.py::_corpus` 是全仓**唯一**在 SQL 侧聚合「已清洗」的地方（grep 复核：旧表达式
`SUM(text_clean IS NOT NULL)` 仅出现在 console.py:131；`scripts/clean_text.py:540` 是另一脚本的
ORM 计数，不受影响、不改）。

改法：迁移后等值行 `text_clean` 已归 NULL，旧口径会漏计 ⇒ `_corpus` 用
`PRAGMA table_info(segments)` 探测 `cleaned` 列：**存在则 `SUM(cleaned)`，否则回落
`SUM(text_clean IS NOT NULL)`**。回落分支保证：

- 迁移前 / 未跑迁移的库（ORM `create_all` 不声明 `cleaned`，含全部测试库）——口径与改动前**逐行相同**，`test_console.py` 不红；
- 迁移后——`SUM(cleaned)` 正是「迁移前 text_clean 非 NULL（含空串）」的逐行等价计数。

> `cleaned` 列**只由本迁移器 ALTER 添加**，不进 ORM（`app/models.py` 一行未动）、不进 `app/db.py::_migrate`；
> 因此它是「离线副本上按需补列」的旁路列，`create_all`/`Segment` 查询按名列选择，多这一列无害。

## 4. 回滚

1. **副本先跑**：本工具默认拒绝活库，正式迁移只在 `D:/…` 的**拷贝**上执行；原活库在切换前**整文件保留**，回滚 = 换回原文件（零风险）。
2. **列级回滚**（不换文件时）：
   - `text_clean`：被归 NULL 的都是 `text_clean == text` 的行，可无损还原
     `UPDATE segments SET text_clean = text WHERE cleaned = 1 AND text_clean IS NULL;`
     （真清洗行 `text_clean != text` 从未被动，无需还原）。
   - `integrity`：紧凑串 `loads_any` 逐位无损解回原字典，无需还原；若要还原原始 JSON 文本，`canonical_json()` 即读侧等价。
   - `cleaned`：`ALTER TABLE … DROP COLUMN`（SQLite ≥3.35）或直接忽略（不影响任何读侧）。
3. 迁移**从不改 `text` / `id` / `ordinal` / `n_chars` / `created_at`**，也不删表、不重建表。

## 5. 复核 SQL（切换前后各跑一遍，逐项相等即通过）

```sql
SELECT COUNT(*) FROM segments;                                    -- 行数不变
SELECT COUNT(*) FROM segments WHERE text_clean IS NOT NULL
     AND text_clean = text;                                       -- 应 = 0（等值行已归 NULL）
SELECT COUNT(*) FROM segments WHERE cleaned=1;                    -- == 迁移前 SUM(text_clean IS NOT NULL)
SELECT COUNT(*) FROM segments WHERE integrity NOT LIKE 'i1:%'
     AND integrity IS NOT NULL AND json_valid(integrity)
     AND integrity NOT LIKE '%src_ok%';                           -- 应 = 0（基键 JSON 全已紧凑）
SELECT COUNT(*) FROM segments WHERE integrity LIKE 'i1:1%';       -- > 0（eligible 前缀位仍可 LIKE 命中）
```

`text` 逐字节不变、有效正文两口径不变、integrity `loads_any` 前后等价，由
`tests/test_slim_segments.py` 在真库小副本（2000 行混合类别）上逐行断言钉死。

## 6. 遗留 / 未做（如实声明）

- **未在活库 `D:/language-genome-data/language_genome.db` 上跑任何写操作**（含 VACUUM）——
  本工具默认拒绝，正式交付只在小副本上真跑（见 `DELIVERY_segments_slim.md` 的真跑输出）。
- `integrity` 里带附加键（`src_ok`/`severity`/`clean_pending_llm`/…）的行**永不紧凑**，
  这是 K5 裸 SQL（JSON1 读 `$.src_ok`）的硬约束，不是没做完。
- `bytes/row` 的**小副本数字是合成 fixture**，与真库语料负载不同；真库前后读数须在活库拷贝上另测。
