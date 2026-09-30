# `segments.integrity` 紧凑编码（2026-09-30，语料容量阻断）

## 为什么动它

K3 入册撞墙的**唯一真阻断**是库体积：`works 1,843 / segments 5,865 万 / 库 41 GiB`
（≈750 B/段），而目标 `works 3,605` 要 ~1.15 亿段 ⇒ 现行存储要 ~85 GB，本机没有任何
一个卷装得下（C 16 / D 17 / E 48 / F 44 GiB）。

字段级实测（2026-09-30，真库采样）：

| 项 | 每段 | 全库（5,865 万段） |
|---|---|---|
| `text` | ~58 B | ~3.4 GB |
| `text_clean` | ~58 B | ~3.4 GB |
| **`integrity`** | **196 B** | **~11.5 GB** ← 单项最大，且 96.15% 的行只有 8 个固定键 |
| 行/索引/页开销 | ~440 B | ~26 GB |

⇒ `integrity` 是「不损语义就能砍掉的最大一块」：8 键行的取值集是**有限穷举**的，
195 B JSON 里装的信息量不到 3 字节。

## 编码口径（逐位无损，不做有损量化）

- 恰好 8 个基键（`analyze()` 的原始输出）⇒ 压成 **10 字符**：
  `i1:` + `eligible` 位（`1`/`0`）+ 22 bit 十六进制
  （六指标按下标存 2/1/2/1/3/3 bit；`dialogue_ratio` 千分位整数 10 bit）。
- **带附加键的行一个字节都不动**：`src_ok` / `severity` / `defects` /
  `clean_pending_llm` / `truncated` / `corpus_v2_source` / `preserve_gate` …
  ⇒ 走裸 SQL + JSON1 读 `$.src_ok` 的存量脚本（`k5_supply_recount`、
  `k5_sourcecheck_coverage`、`k5_freeze_followup_u1u9`、`k5_s4_readonly_evidence`）
  行为**完全不变**。
- 取值不在穷举集合内（以后改了 `analyze()` 口径）、NaN/±inf ratio、非法 JSON
  ⇒ **原样回退原始 JSON**，绝不猜、绝不抛（`pack_raw` 挂在 ORM 写侧，抛异常会
  打断整本入册）。
- `eligible` 放在前缀后第 1 个字符：这是 SQL 侧唯一要过滤的语义
  （`near_dup.train_sampling_pool(eligible_only=True)`），放进 hex 就没法用 `LIKE` 筛。

## 读侧约定

- **ORM 路径无感**：`app/models.py::CompactIntegrity` 装饰器写侧压、读侧还原成与
  压缩前逐字节相同的 JSON 文本 ⇒ `json.loads(seg.integrity)` 等二十多处读者不用改。
- **裸 SQL 路径**必须走单点解析层：`app/segment_integrity.unpack()` /
  `loads_any()`（非法一律抛）/ `unpack_or_none()`（空={}、解不出=None，同
  `clean_text._integrity_flags` 三态）。已接入：`scripts/k2_extract_backfill.integrity_dict`
  （K2/K3 读取侧唯一解析层）、`scripts/k5_promotion_write._src_ok_true`、
  `scripts/k5_nonbench_supply_gap.src_state`。
- **SQL `LIKE` 双口径**：`si.eligible_like_patterns()` 返回
  `('%"eligible": true%', 'i1:1%')` —— 历史 JSON 行与新紧凑行**长期并存**，
  只挂一个就是静默漏段（采样域悄悄变小，不报错）。
- `canonical_json()` 对**解不开的紧凑串原样返回**（不糊成 `"{}"`）：坏数据要在
  下游炸出来，不能变成「合法但全空」。

## 存量与迁移（本提交只改写侧，不回填）

本提交**不批量改写存量 5,865 万行**（避免不可逆批量改写）。存量靠独立搬迁脚本
`F:\Hermes\scripts\lg_compact_migrate.py`（老库只读 ATTACH + `INSERT INTO … SELECT
lgpack(integrity)` 建**新库**，分批提交 + 每批 checkpoint，按 rowid 区间记账可续跑，
单实例锁）：

```
python F:\Hermes\scripts\lg_compact_migrate.py --apply --batch 1000000   # 搬迁
python F:\Hermes\scripts\lg_compact_migrate.py --aux                     # 灌完补索引/触发器
python F:\Hermes\scripts\lg_compact_migrate.py --verify                  # 逐表行数+quick_check+抽样解码
```

实测（2026-09-30 09:0x）：~10.9k 行/秒，**555 B/段**（老库 750 B/段，-26%）；
5,966 万段 ≈ 33 GiB。搬完**不自动换挂**——`F:\agi\language-genome\data` 的 junction
指向新库、删老库都是独立步骤（必须先停入册与 API，且 `--verify` 通过）。

## 收益与残余风险

- 容量：每段 -186 B ⇒ 全量 1.15 亿段省 **~21 GB**；配合换挂把库挪到 D 盘后，
  K3 的 1,905 本缺口（≈31 GB）装得下。
- 残余：`scripts/source_check.py::stamp_integrity` 只写 `{src_ok, defects}` 两键
  ⇒ 经它写回的段永久非 8 键、永不压缩（**存量行为，本提交未改**），
  `json_valid(integrity)=1` 覆盖率随语料单调下降，容量收益按比例缩水。
- 分段粒度（2026-09-30 另案）：实测 `avg n_chars = 53`、`n_sentences = 2.0`，
  即 1–2 句/段 —— 行开销 × 3.2 万段/本才是库膨胀的根因；粒度回到设计口径
  （1~10 句）可再省一个量级，属**独立评审项**，本提交不动。
