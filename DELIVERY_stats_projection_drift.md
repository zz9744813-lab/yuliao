# 策略统计投影漂移对账器 · 交付报告（lg-stats-projection-drift）

- 工作树：`F:/agi/_scratch/worktrees/stats-projection-drift`（分支 `task/stats-projection-drift`，基点 `bdb2b68`）
- 日期：2026-09-27
- 交付物形态：**只读**对账器 + 回归钉 + 本报告，共三个文件，全部落在派工白名单内

## ① 交付文件（whitelist 逐一对应，无新增第四个文件）

| 路径 | 作用 |
| --- | --- |
| `scripts/stats_projection_drift.py` | 策略统计投影漂移对账器：读库内 `strategy_stats` 现值 vs `strategy_stats_rebuild.run(apply=False)` 现算值，逐策略报「缺失键 / 键值不一致 / 快照超龄」 |
| `tests/test_stats_projection_drift.py` | 9 例回归钉（临时 sqlite 夹具造库，不碰真库；真库仅第 ⑤ 例只读冒烟） |
| `DELIVERY_stats_projection_drift.md` | 本报告 |

未 commit、未 push、未合并、未对真库做任何写入、未动源检出与其它工作树。

## ② 只读承诺在**连接层**成立（本实现唯一值得复审的技术点）

派工要求「输入 sqlite 库路径 + `mode=ro` 打开、绝不写」**且**「调 `scripts/strategy_stats_rebuild.py` 的 `run(apply=False)`」。
这两条在既有代码里天然打架：

- `app/db.py` 的 `engine` 在 **import 期**就按 `LG_DATABASE_URL` 绑死，且带
  `PRAGMA journal_mode=WAL` 的 connect 钩子；
- `strategy_stats_rebuild.run()` 内部写死 `with db.session() as s`，**不接受**外部 session。

于是「对账 A 库、实际算 B 库」是默认结果，而那个 B 库连接还是可写的（WAL 钩子对旧
schema 库会真动文件）。本对账器的处置：

1. 投影行读取：`sqlite3.connect("file:<db>?mode=ro", uri=True)`（与仓内
   `k5_*` 只读工具同一字面口径）；
2. 现算值：调 `ssr.run(apply=False)` 期间，把 `app.db.SessionLocal` **临时改绑**
   到**同一个库**的 `mode=ro` 只读 engine（`create_engine("sqlite://", creator=…)`），
   `finally` 无条件还原并 `engine.dispose()`；
3. 全程不调 `db.init_db()`、不 `create_all` ⇒ 连「零数据写 ≠ 零 DDL」这条
   重建器自己声明的豁免也不需要援引；
4. **不写库、不自动 rebuild**：重建是主控决策，对账器只报差。

`mode=ro` 下任何写尝试由 SQLite 直接抛异常，不依赖代码自觉。

## ③ CLI 与报告口径

```
python scripts/stats_projection_drift.py [--db PATH] [--out report.json] [--max-age-days N]
```

- `--db` 缺省 `F:/agi/language-genome/data/language_genome.db`；
- `--max-age-days` 缺省 7，`snapshot_at` 距今超过该阈值 ⇒ 该行 `stale=True`；
  时间戳解析不了 ⇒ `stale=None` 并计入 `snapshot_unparseable_rows`（**不把
  「读不懂」判成「没超龄」**）；
- 退出码：`0` = 对账跑完（**漂移是读数不是故障，有漂移也 0**）；`2` = 跑不起来
  （库不存在 / 表缺失 / mode=ro 被拒 / 现算异常），此时**不**输出「没漂移」的假结论。

逐策略字段：`strategy_key` / `snapshot_at` / `snapshot_age_days` / `stale` /
`extras_keys` / `missing_keys`（缺失键名单）/ `value_mismatch`
（`{键: {"projection": 旧值, "recomputed": 现算值}}`，两值并列给出）/
`foreign_extras_keys` / `valid_projection|valid_recomputed` / `drift`。

期望键集**不硬编码**在本脚本里：直接取 `ssr.CANONICAL_EXTRAS_KEYS`（单一真值，
重建器加键则对账器自动跟随；本脚本内的 `FALLBACK_EXTRAS_KEYS` 只在重建器加载
失败的异常路径兜底）。

汇总行（`summary_line`）字面形态：

```
drift_rows=N / missing_key_rows=M / total=8
```

三个计数分别意味着：`total` = 库内 `strategy_stats` 行数；
`missing_key_rows` = 至少有**一个**期望 extras 键未写进投影的行数；
`drift_rows` = 缺键 **或** 键值不等 **或** `valid` 列不等的行数。
另有 `stale_rows` / `value_mismatch_rows` / `snapshot_unparseable_rows`，
以及 `orphan_strategy_ids`（有投影、策略已不在）与
`unprojected_strategy_ids`（现算有、库里没投影行）。

## ④ 本会话执行状态：**未自跑**（权限门拒绝，如实标注，不伪造读数）

派工给的验收命令原样为：

```
F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe -m pytest tests/test_stats_projection_drift.py -q -p no:warnings
```

本会话内该命令**被权限门拒绝执行**，被拒原文（工具返回逐字）：

```
Error: Allow Bash to run: F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe -m pytest tests/test_stats_projection_drift.py -q -p no:warnings?
```

连 `python.exe --version`、`python --version`、`python -m py_compile <两个新文件>`
也一并被拒（同一形态的 `Allow Bash to run: …?` 提示）。按派工纪律「如实写明
未自跑，不要伪造读数」，本报告**不**声称任何 pytest 读数、**不**声称观察过
rc=0：预期的干净读数应为 `9 passed`（真库不可读时为 `8 passed, 1 skipped`），
末行 rc=0——但那是**主控复跑的结果**，不是本会话的实测。为免权限门反复被触发，
本会话未继续重试，也未使用任何绕开门禁的手段（未写临时脚本、未换解释器路径、
未改配置）。派工的 `F:/Hermes/tools/python-3.14.7…` 无 pytest，按提示未采用。

因此本会话的自检只到「静态」这一层：两文件的语法/引号（全仓
`tests/test_compile_all.py` 会编译检查）、夹具构造字段与仓内既有可用写法
（`tests/test_strategy_stats_rebuild.py` 的 `ExpressionStrategyV2(...)` 同形）、
`StrategyStats` 各列默认值与 NOT NULL 覆盖、`compute()` 对空实例/空登记的安全性、
`run(apply=False)` 返回 `mode == "dry_run"`、临时库不以 WAL 建立（避免 `-wal/-shm`
副作用文件与哈希抖动）——均逐项比对过源码，未凭记忆。

## ⑤ 回归钉清单（`tests/test_stats_projection_drift.py`，9 例）

| # | 用例 | 钉住的判据 |
| --- | --- | --- |
| ① | `test_only_by_root_work_projection_names_all_four_missing_keys` | 复刻真库现况（extras 只有 `by_root_work`）⇒ `usable_evidence` / `benchmark_stripped` / `k3_eligible_instances` / `k3_eligible_root_works` **逐个点名**；两条通道互不冒名——缺的键不进值不一致，在场但变了的那一个（`by_root_work`）才进 |
| ② | `test_present_but_stale_value_goes_to_mismatch_not_missing` | 键齐值不同 ⇒ 只进值不一致，`missing_key_rows=0`，旧值/现算值并列可见 |
| ②b | `test_valid_column_drift_is_reported_separately` | `valid` 列不等单独可见，不混进键差 |
| ③ | `test_snapshot_age_stale_true_and_false_both_directions` | 超龄 `True`、未超龄 `False` **两个方向都钉**，并钉阈值跟随 `--max-age-days`（300 天 / 0 天各一次） |
| ③b | `test_unparseable_snapshot_is_flagged_not_silently_fresh` | 读不懂的快照 ⇒ `stale=None` + 计数，不静默判「新鲜」 |
| ④ | `test_cli_run_leaves_fixture_db_byte_identical` | **零写入**：走 CLI（`main([...])`）跑完，夹具库分块 sha256 逐字节不变；`SessionLocal` 还原（`is` 同一对象）；目录里除 `fixture.db`/`report.json` 不外溢任何文件。夹具里「缺键行」与「值漂移行」各占一行，`missing_key_rows` 与 `value_mismatch_rows` 因此可分辨（都挤在同一行上就测不出两个计数各管什么） |
| ④b | `test_missing_db_path_exits_2` | 库不存在 ⇒ rc=2 且不在磁盘上留文件 |
| ⑤ | `test_real_db_readonly_smoke` | 真库只读冒烟：`mode=ro` 连得上、现算跑通、`total == 直查行数 == 8`、真库文件未被本例写过 |
| ⑥ | `test_reverse_self_check_consistent_projection_reports_zero_drift` | 见下节 |

④ 例里**不**钉 `stale_rows`：`main()` 用真实时钟，夹具快照
`2026-09-23T07:24:05Z` 距回放当天迟早会越过 7 天线，钉它就是埋一条会自然转红的
假回归——超龄判据改由 ③ 例用固定 `now` 双向钉死。这是有意的取舍，不是漏钉。

⑤ 例的 skip 条件与真库现况：真库不在 / `mode=ro` 打不开 ⇒ skip 并带原因文本，
**不伪造成通过**；若运行期间 size/mtime 变了，说明是**别的**进程在写（live 或人工
rebuild），本例全程只持只读句柄、无从归因，如实 skip；size/mtime 未变而内容哈希
变了 ⇒ 硬失败，不许 skip 掩盖。

## 反向自检

只测「有问题时报得出」是不够的：一个**逢库必报漂移**的坏工具，在 ①②⑤ 三例下
同样全绿，红绿信号就废了。所以本交付自带反方向的一条——把投影写成「与现算值
逐键相等 + 快照不超龄 + `valid` 相等」的形态，对账器必须交出
`drift_rows=0 / missing_key_rows=0 / total=2`，且该行 `stale is False`、
`missing_keys == []`、`value_mismatch == {}`、`foreign_extras_keys == []`。
这条 ⑥ 用例（`test_reverse_self_check_consistent_projection_reports_zero_drift`）
即闭环的另一半：正向能报红、反向一例不红。缺了它，①② 的绿是廉价的。

同一节里刻意留了一个坑并把它钉住：反向夹具的 `by_root_work` 必须写成 `{}`
而不是 `0`——现算值那里是 dict，写 `0` 会凭空造出一条值漂移，反向自检就失去
意义（辅助函数 `_consistent()` 的 docstring 记了这个原因）。

派工背景里的主控读数也自带一个反向对照事实：库里 8 行 `snapshot_at` 全是
`2026-09-23T07:24:05Z`、`extras` 只有 `by_root_work` 一个键，而 dry-run 现算
`usable_evidence` 5~12、`benchmark_stripped` 9~12、`k3_eligible_root_works` 全 2
——逐策略都不等。对账器对该库的**预期**读数因此是（本会话未自跑，此为推算，
非实测）：`drift_rows=8 / missing_key_rows=8 / total=8`；`value_mismatch_rows`
取决于旧投影 `by_root_work` 与 `valid` 是否同步，不预判；`stale_rows` 今天是 0
（`2026-09-23T07:24:05Z` 距今约 4.2 天 < 7），自 `2026-09-30T07:24:05Z` 起变 8。

## ⑥ 已知边界与下一步（不替主控做决定）

1. 本对账器**只报差不重建**。要让库里真出现那四个分档键，需要主控决定跑
   `python scripts/strategy_stats_rebuild.py --apply`（写库操作，派工明令本任务
   不得触及）；`docs/策略统计证据分档_20260926.md` 与真库现况的失配，在此之前
   一直成立，对账器每次都会把它报成 `missing_key_rows=8`。
2. 重建之后再跑一次对账器，预期落回 `drift_rows=0`——那才是 ⑤ 里那句「行数 8」
   之外最硬的正向证据；`dropped_foreign_extras`（`lg-foreign-extras-notice`
   已落地）只在 `--apply` 路径出现，本对账器全程 dry-run，故不读该键，改为逐行
   报 `foreign_extras_keys`（同一事实的只读视角）。
3. `total == 8` 是**行数事实**而非漂移事实。若日后策略集变化（新增/退役策略），
   ⑤ 例会红并提示同步读数——这是刻意的显式失配，不做静默兼容。
4. 未改任何既有用例、未删任何断言、未改 `strategy_stats_rebuild.py`、`app/**`、
   `docs/**`；新文件只有白名单里三个。

## 主控复跑清单（本会话被门禁挡住，未能自证）

```bash
# cwd 必须是本工作树（tests/conftest.py 以本树 app/ 为准，且 R6 整轮锁位在本树 data/）
cd F:/agi/_scratch/worktrees/stats-projection-drift

# 1) 派工验收命令（预期 9 passed；真库不可读时 8 passed + 1 skipped；末行 rc=0）
F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe -m pytest tests/test_stats_projection_drift.py -q -p no:warnings

# 2) 真库只读跑一遍（不改库；预期汇总行 drift_rows=8 / missing_key_rows=8 / total=8）
F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe scripts/stats_projection_drift.py --out "$TEMP/spdr_real.json"

# 3) 零写入旁证：第 2 步前后真库 size/mtime 不变
```
