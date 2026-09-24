# source_check `--scope nonbench` 落地证据（口径 + 真库只读计数）

> **说明（如实标注来源）**：本文件由**主控 Hermes 代跑补齐**。
> 执行者（opencode）在 1500s 硬截止（exit 124）时，代码与测试均已落盘且过门，
> 唯本文档未写完；数字全部是**主控本机实跑/只读 SELECT 的真实输出**，无估算、无"预计"。

- worktree：`F:/agi/_scratch/worktrees/sourcecheck-workscope`（分支 `fix/sourcecheck-workscope`，基线 `main=0c98c6c`）
- 改动：`scripts/source_check.py`（+99/−10）、`tests/test_source_check_nonbench_scope.py`（新建，7 例）

## 1. 新增范围语义（写死在 `--help` 与模块 docstring）

`--scope nonbench`：候选段 = `role != 'benchmark'`（**NULL 也算非基准**，判据是"不等于"）
**且**所属作品在 `work_sources` 登记为合规人类语料（`human_fiction` / `production_nonbenchmark_*`，
判定**单源复用** `scripts/k2_extract_backfill.py::nonbenchmark_compliant_source`，import 不到即 fail-closed）
**且** `text_clean` 非空。

`--work-id <WK-...>`（可重复 / 逗号分隔）：把范围**收窄**到指定作品，结果恒 ⊆ 该 scope 自身选取集；
给不合规/无关 work_id = **空集**，不报错也不放行。

## 2. 真跑证据（主控实跑，命令与输出原文）

### 2.1 离线回归
```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_source_check_nonbench_scope.py -q
```
→ `7 passed`，**exit 0**

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/ -k "source_check or sourcecheck or strict_bool" -o addopts=""
```
→ `19 passed, 1048 deselected, 2 warnings in 2.40s`，**exit 0**
（默认三档 `used` / `all-frames` / `bench` 的回归在 `test_three_legacy_scopes_verbatim` 内，逐字不变）

### 2.2 真库只读计数（`SELECT` only，零写库、零模型调用）
合规来源 7 本：`WK-6e5d2623 / WK-a052258c / WK-6c5ea9081547 / WK-8e8e0459284d / WK-d999c2c8ea26 / WK-3631b4b3dd44 / WK-dc90993434e9`

| 范围 | 命中段数 |
|---|---|
| `nonbench`（全池） | **394,510** |
| `nonbench --work-id WK-dc90993434e9`（覆汉） | **0** |
| `nonbench --work-id WK-0d48cc2e`（fixture 作品） | **7** |

## 3. 「本任务没有给覆汉补 src_ok / text_clean」的事实与原因

**没有补，是刻意的**：本任务是**检查面**（只读），补数据属主控授权面。

补不补的判据，主控另跑了 dry-run 取证：
```
F:/Hermes/hermes-agent/venv/Scripts/python.exe scripts/clean_text.py --rules --dry-run --limit 6000
→ {"dry_run": true, "would_clean": 0, "checked": 6000}
```
覆汉正文**已经是干净的**（0/6000 需要改），`text_clean` 全 NULL 是 v2 导入时**该列未接线**，
不是"需要重洗"。全库 `src_ok` 分布：NULL 431,627 / `0`→1,919 / `1`→4,872；覆汉 35,974 段**全部为 NULL**。

⇒ 覆汉缺的是**回填**（`text_clean` 从正文复制 + `src_ok` 复核打标），**不是重洗**；
此前的"补洗会让锚漂移"顾虑**已被实跑证据推翻**。是否开跑仍待朱十一拍板。

## 4. ⚠️ 口径差（**主控 2026-09-24 19:0x 更正：本条前提为假，已撤回**）

> **更正声明**：本节原称「本 scope 不过滤 fixture/synthetic/commentary，会把 fixture
> 段算进来（`--work-id WK-0d48cc2e` 命中 7 段）」——**该结论错误，是主控测量口径
> 错误**，现予撤回并留痕（不删原文口径，改标为已推翻）。
>
> 主控只读复核（真库 `data/language_genome.db`，两版代码分别 import 后跑真实
> `targets("nonbench")`）：
> - `nonbenchmark_compliant_source` 是**白名单**（`human_fiction` / 前缀
>   `production_nonbenchmark_*`），对 `fixture` / `synthetic` / `commentary`
>   恒为 `False` ⇒ 这些来源**从未进入** compliant 集合，也就从未进过 nonbench 池。
> - 两版 compliant 作品集**逐项相等**（7 本，双向差集为空）；
>   `WK-0d48cc2e` / `WK-648c2920` 两本 fixture 作品在两版中**都不在池内**。
> - 「7 段」是**原始段计数**（该作品 `role=NULL` 且 `text_clean` 非空的段数），
>   被误当成 `--scope nonbench --work-id` 的**选中数**写进了本表——即
>   把「库里有多少段」当成了「scope 选了多少段」。
>
> 结论：`source_check --scope nonbench` 与 K2 侧 `segment_universe('nonbenchmark')`
> 在真库上**选中集合本来就完全一致**，不存在「把 fixture 算进来」的风险。
> 后续 `fix/nonbench-source-type`（提交见 merge 记录）的真实价值是
> **口径镜像 + 防御性双闸 + 单源复用钉死**（防未来出现「前缀命中白名单却属合成
> 派生」的异常来源类型被放开），**不是行为修复**。

原表（口径已更正，数值仅作历史留痕）：`--work-id WK-0d48cc2e` 的 **7** 是**原始段数**，
不是本 scope 的选中数；本 scope 对 fixture 作品的选中数（两版一致）为 **0**。
