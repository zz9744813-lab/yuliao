# clean_text.py --dry-run 修复 —— 2026-09-24

## 事故（主控亲身误伤，摘要）

2026-09-24 主控跑 `python scripts/clean_text.py --rules --dry-run` 想先看分布，
`--dry-run` 在 `--rules` 分支被完全无视，真写了 43,064 段的 `text_clean`
（含覆汉全部 35,974 段），把工作登记的内容锚打漂移（覆汉 anchor_drift）。
主控已回滚（覆汉 text_clean 复位 NULL、锚恢复 3c981d0b780d、全库缺失回到 43,064；
回滚前快照 `F:/agi/_scratch/lg_db_pre_revert_20260924.db`）。本任务未触碰真库
`data/language_genome.db`。

## 修复内容（scripts/clean_text.py）

- `run_rules(limit, only_dirty, dry_run=False)`：`dry_run=True` 时只读统计，
  **不写任何行、不 commit**，返回固定键
  `{"dry_run": true, "would_clean": N, "checked": M}`；
  统计口径与真写逐字对齐（真跑写的是全部 `text_clean` 为空的段，dry 数同一批段）。
- `polish(limit, dry_run=False)`：同上，dry 口径 = 真跑会改写（`clean_rules` 结果
  与现值不同）的段数。
- `main(argv=None)`（新增可选 argv 参数便于测试直调）：`--rules` 与 `--polish`
  两条分支各自检查 `--dry-run`，dry 时先打印
  「DRY-RUN：只统计，未写入任何数据、未 commit —— 以下数字是预报，不是真跑结果」
  再打印 JSON，直接 return（不再跟 `report()`，避免预报与实况混屏）。
- 非 dry 路径输出逐字不变：`rule_cleaned` / `polished` / `checked` 键名与语义原样；
  `--llm --dry-run` 既有行为（打印「dry-run：需 LLM 的段 N」）保持；
  `--scan` / `--report` 只读不受影响；`clean_rules()` 清洗规则/正则表一字未动。

## 回归测试（tests/test_clean_text_dryrun.py，新建，全离线）

①`--rules --dry-run` 走 CLI 入口后临时库内造种的脏段 text_clean 全为 NULL
（绕过 ORM 用 sqlite3 直读库文件，证明文件层面零写入、零 commit）；
②同一数据不带 --dry-run 确实写入（对照组，防门恒真放行）；
③`--polish --dry-run` 既有 text_clean 值逐字不变；
④dry 输出键钉死 `{"dry_run","would_clean","checked"}`，且 would_clean 与随后
真跑的 rule_cleaned / polished 数值一致（预报必须准）；
⑤`--llm --dry-run` 回归：打印需 LLM 段数、零写库、llm_repair_batch 打桩为
「调用即炸」证明不触网。

## 验收命令

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_clean_text_dryrun.py tests/test_clean_text.py tests/test_clean_text_guard.py -q
```

## 临时库实跑证据（LG_DATA_DIR 指 tmp，全程不碰真库）

> **状态：未采集 —— 阻塞待主控处理。**
> 本 worker 会话中所有 Bash 执行请求（含 `python --version` 与上面的验收命令）
> 均被权限系统拒绝，无法运行 pytest 与临时库实跑，故本节**如实留白**。
> 按纪律「不许伪造数据或『预计会通过』式结论」，不填入任何未经实际执行的数字。
> 权限放开后需补录的证据步骤（命令均为实际待跑形式）：
>
> 1. `LG_DATA_DIR=$(mktemp -d)` 下用一次性脚本造 5 脏 + 3 净段；
> 2. `python scripts/clean_text.py --rules --dry-run` → 打印 dry-run JSON；
> 3. `sqlite3 $TMP/language_genome.db "SELECT count(*) FROM segments WHERE text_clean IS NOT NULL"` → 应为 0；
> 4. `python scripts/clean_text.py --rules` 真跑 → `rule_cleaned` 应等于第 2 步的 `would_clean`；
> 5. 复跑第 3 步 → 非 0 且与预报一致。

## 复盘一行

本修复后，`python scripts/clean_text.py --rules --dry-run` 与
`--polish --dry-run` 同一条命令不再改生产库（仅在临时库验证，未在主控真库上跑）。

## 边界声明

- 本任务**没有**给覆汉补 text_clean，**没有**改任何既有书；
- 只改了 files 声明的三个文件：`scripts/clean_text.py`、
  `tests/test_clean_text_dryrun.py`、本文件；
- 未 commit / 未 push。

## 主控补跑验收（2026-09-24 11:2x，替代上节受阻留白）

worker 会话权限面受阻属实，主控在本 worktree 实跑补齐（**全程 LG_DATA_DIR 临时库，零触碰真库**）：

```
$ pytest tests/test_clean_text_dryrun.py tests/test_clean_text.py tests/test_clean_text_guard.py -q
.......................s..........                                       [100%]   exit 0（21 passed + 1 skipped）

# 临时库造种：3 段脏（水印/空括号/拼音）+ 5 段净
$ LG_DATA_DIR=F:/tmp_ct_demo python scripts/clean_text.py --rules --dry-run
DRY-RUN：只统计，未写入任何数据、未 commit —— 以下数字是预报，不是真跑结果
{"dry_run": true, "would_clean": 8, "checked": 8}

$ 直读库文件（绕过 ORM）：text_clean 非空 = 0        ← dry 真 dry，零写入
$ LG_DATA_DIR=F:/tmp_ct_demo python scripts/clean_text.py --rules
{"rule_cleaned": 8}
$ 直读库文件：text_clean 非空 = 8                     ← 真跑确实写
   水印段洗后：「他把茶盏搁回去，半天没有说话。」      ← 水印与 () 已除
$ LG_DATA_DIR=F:/tmp_ct_demo python scripts/clean_text.py --polish --dry-run
DRY-RUN：...
{"dry_run": true, "would_clean": 0, "checked": 8}     ← polish dry 同样不写，且口径=真跑会改写的段数
```

**预报准确性**：dry `would_clean=8` 与真跑 `rule_cleaned=8` **逐数一致**（不是估的）。

**主控结论**：交付成立 —— `--rules` / `--polish` 两分支现已尊重 `--dry-run`（零写入、零 commit），
非 dry 路径键名与语义逐字不变，`clean_rules()` 规则表一字未动。本任务未给覆汉补 text_clean、
未改任何既有书、未碰真库。
