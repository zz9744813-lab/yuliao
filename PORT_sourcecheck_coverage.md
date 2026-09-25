# PORT — lg-sourcecheck-coverage（工作树 sourcecheck-coverage，2026-09-25）

## 状态

4 个白名单文件已交付，均在 `F:/agi/_scratch/worktrees/sourcecheck-coverage`；未修改任何既有文件，也未执行 commit/merge/push。

| 路径 | 状态 |
|---|---|
| `scripts/k5_sourcecheck_coverage.py` | 新增，836 行；真库 `sqlite3 mode=ro`，零模型调用、零 git 写 |
| `tests/test_k5_sourcecheck_coverage.py` | 新增，19 条离线回归；全部使用 `tmp_path` SQLite 夹具 |
| `docs/K5源校验覆盖盘查_20260925.md` | 新增；真库 stdout 原样片段、逐节数字和未自跑清单 |
| `PORT_sourcecheck_coverage.md` | 本交付说明 |

## 验收原始输出

### 回归

命令：

```text
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_k5_sourcecheck_coverage.py -q
```

退出码：`0`。原始输出：

```text
...................                                                      [100%]
```

### 真库盘查

命令：

```text
F:/Hermes/hermes-agent/venv/Scripts/python.exe scripts/k5_sourcecheck_coverage.py
```

退出码：`0`。原始输出：

```text
[k5_sourcecheck_coverage] db=F:/agi/language-genome/data/language_genome.db sqlite=3.53.1 (mode=ro 只读、零模型调用)
[覆盖] 全部 438,418｜已校验(严格布尔) 6,791（true 4,872 / false 1,919）｜未校验 431,627（98.45%，缺键 431,617/不严 0/非法 10）｜IS TRUE 松口径掺水 0
[代价] 调用次数 下界 411,738 ~ 上界 431,627（零 LLM 路径可跳过 19,889）；批次 conc=8: 51,468~53,954，串行: 411,738~431,627
[口径] A(松)=4,220 A(严)=4,220 B=401,611；未校验∧A=0 未校验∧B=395,648（保持未校验则 A/B 不变）
  · WK-6c5ea9081547 凡人修仙传（忘语）：未校验 153,792/154,809（99.34%）
  · WK-d999c2c8ea26 将夜（猫腻）（corpus v2）：未校验 67,789/68,626（98.78%）
  · WK-a052258c 将夜（猫腻）：未校验 67,574/68,626（98.47%）
  · WK-3631b4b3dd44 斗罗大陆（唐家三少）（corpus v2）：未校验 36,736/37,863（97.02%）
  · WK-8e8e0459284d 斗罗大陆（唐家三少）：未校验 36,528/37,863（96.47%）
[结论] 可校验｜未校验 431,627 段（占 98.45%）全部有可判定的 integrity 口径（严格布尔三态），段文本与 source_check.py 的选取通道存在——是「可被校验、但从未校验」的存量，非「判坏」。
[k5_sourcecheck_coverage] 已写 F:/Hermes/team/reports/k5_sourcecheck_coverage_20260925.json
[k5_sourcecheck_coverage] 已写 F:/agi/_scratch/worktrees/sourcecheck-coverage/docs/K5源校验覆盖盘查_20260925.md
```

真库读数与主控背景一致：438,418 段、`src_ok=true` 4,872、`src_ok=false` 1,919、未校验 431,627；严格三态没有把数字或字符串当作已校验。默认 JSON 报告路径是任务指定的 `F:/Hermes/team/reports/k5_sourcecheck_coverage_20260925.json`。

## 反向验证

临时将 `scripts/k5_sourcecheck_coverage.py` 的 `state_expr` 真分支从：

```text
WHEN json_type({col},'$.src_ok')='true' THEN 'true'
```

替换为：

```text
WHEN json_extract({col},'$.src_ok') IS TRUE THEN 'true'
```

运行同一回归命令得到预期红灯；原始失败摘要：

```text
FAILED tests/test_k5_sourcecheck_coverage.py::test_totals_strict_tristate - a...
FAILED tests/test_k5_sourcecheck_coverage.py::test_unverified_membership - As...
FAILED tests/test_k5_sourcecheck_coverage.py::test_loose_values_not_counted_checked
FAILED tests/test_k5_sourcecheck_coverage.py::test_cost_arithmetic - assert 1...
FAILED tests/test_k5_sourcecheck_coverage.py::test_caliber_impact - assert 0 ...
```

首个红点为 `tests/test_k5_sourcecheck_coverage.py:124`：`assert (4 == 3)`；数字 `1` 被错误算入 `src_true`。随后精确恢复严格分支，重新运行回归，退出码 `0`，输出恢复为：

```text
...................                                                      [100%]
```

## 实现核对

- 严格覆盖判定只用 `json_type(integrity,'$.src_ok')` 的 `true/false`；缺键、类型不严、`null`、非法 JSON/NULL 归未校验。`json_extract(...) IS TRUE` 只用于口径影响对照，绝不用于覆盖计数。
- 逐作品盘查按未校验数降序，输出 `source_type`、role 分布、段数、严格已查、true/false、未校验数及占比；`seg_version`、`work_id`、`text_version` 三张分布表只报告数字，不作因果推断。
- 代价常量和行号从 `scripts/source_check.py` 读取：模型 L113、熔断 L202-L203、`check_one` 一次 `chat(` L262、取数块 L325、串行池 L473、`--conc=8` L539、短文本闸 L429、错字表 L235-L239；读取失败则 `verdict=证据不足`。
- `mode=ro` 夹具回归核对内容 SHA-256、mtime 和无 WAL 副作；只读连接写入必抛 `sqlite3.OperationalError`。

## 未自跑

- 未执行 `scripts/source_check.py --run`，因此没有源校验、模型调用或费用。
- 未写入真库；盘查使用只读连接，`data/` 文件未作修改。
- 未做 git 写操作。
- 未创建仓内临时、冒烟或复现脚本。
- 未把导入批次或版本分布解释成因果结论。

## 仓状态

最后检查只看到以下 4 个白名单路径为未跟踪新增文件：

```text
?? PORT_sourcecheck_coverage.md
?? docs/K5源校验覆盖盘查_20260925.md
?? scripts/k5_sourcecheck_coverage.py
?? tests/test_k5_sourcecheck_coverage.py
```
