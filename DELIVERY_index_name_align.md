# 交付：segments.work_id 索引命名对齐（lg-index-name-align）

日期：2026-09-30 · 工作区：`F:\agi\_scratch\worktrees\lg-index-name-align`（分支 `task/index-name-align`）

## 改动

### 1. `app/models.py` — Segment 索引声明改名（仅索引声明，字段/类型/外键未动）

改前（`app/models.py:155`）：

```python
work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), index=True)
```

SQLAlchemy 默认索引名为 `ix_segments_work_id`，与真库现状 `idx_segments_work_id`（2026-09-30 12:5x
手工建，578s，+1.57 GiB，EQP 确认 `SEARCH segments USING INDEX`）不同名。

改后：

```python
__table_args__ = (Index("idx_segments_work_id", "work_id"),)

work_id: Mapped[str] = mapped_column(ForeignKey("works.id"))
```

`Index` 早已在文件头部导入（`from sqlalchemy import ... Index ...`），无新增依赖。

### 2. `tests/test_segment_index_name.py`（新增测试文件，验收命令指定路径）

三条断言：
1. `Segment.__table__.indexes` 中 work_id 上的索引**恰好一个**，名字 == `idx_segments_work_id`，列序 `["work_id"]`；
2. `create_all` 到临时 sqlite 后 `PRAGMA index_list('segments')`（并用 `inspect().get_indexes` 复核）
   **没有** `ix_segments_work_id`，且 `idx_segments_work_id` 在列；
3. 临时库灌 200 行后 `EXPLAIN QUERY PLAN SELECT ... WHERE work_id = ?` 实测走
   `SEARCH segments USING INDEX idx_segments_work_id (work_id=?)`（本机实测命中强分支；
   若优化器另选计划则退化为核索引存在+列序，不造假——见测试内注释）。

## 为什么改名字而不是重建索引

- 真库索引已经存在且被查询计划实际使用（手工建，578s 建成，+1.57 GiB 已付出）；
  重命名/删除重建都是对 45 GB、盘上余量仅 ~24 GiB 的**真库做写操作**，风险与耗时都无必要。
- 差异只在**模型声明侧的命名**：SQLAlchemy 默认 `ix_<表>_<列>`，真库用的是 `idx_` 前缀。
  把声明名对齐真库是零成本、零风险的收敛。
- 不对齐的后果是持续性的：任何人在真库上跑 `init_db`/`create_all` 或 alembic autogenerate
  对账，都会因"库里没有名为 `ix_segments_work_id` 的索引"而**再建一个 ~1.5 GiB 的重复索引**，
  在 D 盘余量下这近乎不可承受的浪费。

## 真库不需要任何动作

真库已有 `idx_segments_work_id`；本次改名后模型声明与真库**同名同列**。
后续任何 `create_all` 见到同名索引即跳过（`checkfirst` 语义），autogenerate 对账也不再报缺——
不需要建、不需要删、不需要重命名，不碰真库任何对象。

## 验收证据

### `python -m pytest tests/test_segment_index_name.py -q`（rc=0）

验收命令原文（venv python：`F:/Hermes/hermes-agent/venv/Scripts/python.exe`）：

```
$ python -m pytest tests/test_segment_index_name.py -q
...                                                                      [100%]
RC=0
```

（3 passed；本仓库 pytest 配置下 `-q` 无额外 summary 行，rc=0 如上。）

回归联跑（同 venv）：

```
$ python -m pytest tests/test_segment_index_name.py tests/test_compact_integrity.py tests/test_corpus_v2_isolation.py -q
RC=0
```

### `Segment.__table__.indexes` 索引名集合（`python -c` 实测）

```
$ python -c "from app.models import Segment; print(sorted(ix.name for ix in Segment.__table__.indexes))"
['idx_segments_work_id']
```

### 临时库实测（create_all 后；证明无 `ix_segments_work_id` 且 EQP 走新名索引）

```
index_list: ['idx_segments_work_id', 'sqlite_autoindex_segments_1']
EQP: ['SEARCH segments USING INDEX idx_segments_work_id (work_id=?)']
```

## 边界遵守

- 未对真库 `D:\language-genome-data\language_genome.db` 做任何读写；
- 未改 `scripts/`；未 git commit/push/merge；改动仅限白名单三文件；
- `app/models.py` 中除 Segment 索引声明外零改动（`work_id` 字段类型、外键、可空性原样）。

## 变更文件清单

- `app/models.py`（Segment：`index=True` ⇒ `__table_args__ = (Index("idx_segments_work_id", "work_id"),)`）
- `tests/test_segment_index_name.py`（新增）
- `DELIVERY_index_name_align.md`（本文件）
