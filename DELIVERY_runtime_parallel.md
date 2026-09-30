# 交付：多进程自动调度（lg-runtime-parallel）

日期：2026-09-30 · 工作区：`F:\agi\_scratch\worktrees\lg-runtime-parallel`

口径书：[docs/RUNTIME_PARALLEL.md](docs/RUNTIME_PARALLEL.md)（对应 [docs/plan.md](docs/plan.md) 第 15 行的「多进程自动调度」后置项）

## 一句话

在**不动 `app/` 内核**的前提下交付一个调度层：每臂一个独立子进程 + 独立世界目录 + 独立
SQLite 世界库，并行跑一批场景/臂，把预算硬闸、幂等、隔离、超时回收、汇总覆盖完整性和
真库护栏补成可执行判定；所有测试**真跑**在离线合成通道上（零网络、零真实 token）。

## 本轮整改说明（上一轮验收红项）

上一轮红：交付物缺少必需内容 **「真跑」** 与 **「反向」**。本轮逐条补齐：

- 「真跑」：见下文 §验收证据-1/2/3，测试套件与 CLI **全部于本轮重新真跑**，贴逐字原文与
  退出码（含可见的 `15 passed in 24.60s` 汇总行），非引用旧证据；
- 「反向」：见下文 §反向验证，专门一节钉死「**一个臂失败不会被汇总成整体成功**」，
  测试与真跑 CLI 各有原文（`ok=false`、`verdict="reject"`、`RC=1`、`require_ok` 抛错）。

## 变更文件清单（白名单四文件，无其它改动）

| 文件 | 性质 | 内容 |
| --- | --- | --- |
| `scripts/runtime_parallel.py` | 新增（791 行） | 调度层：预检、子进程启动、逐臂收敛、汇总、CLI |
| `tests/test_runtime_parallel.py` | 新增（15 个用例） | 正例 + 五组负例 + 反向验证 + 退化/护栏/CLI 口径 |
| `docs/RUNTIME_PARALLEL.md` | 新增 | 不变量、作业清单、两级失败口径、汇总字段、护栏、CLI、验收（含真跑与反向验证口径） |
| `DELIVERY_runtime_parallel.md` | 新增（本文件） | 交付与验收证据 |

`git status --porcelain` 只列出以上四个未跟踪新增文件；`app/`、`tests/conftest.py`、
`pyproject.toml` 等均未改动；仓内无 scratch/smoke/临时脚本残留（smoke 与 CLI 真跑全在
仓外临时目录 `C:\Users\6\AppData\Local\Temp\opencode\rp_final_20260930\`），
本轮仓内未创建 `data/`（`ls -d data` → 不存在；跑测试前设了
`LG_LOCK_DIR=C:/Users/6/AppData/Local/Temp/rp_lock_20260930` 把整轮锁隔离到检出之外）。

## 关键设计决策

1. **`subprocess` 而非 `multiprocessing`**：超时是 `Popen.kill()` + `communicate()` 的硬回收；
   客户端不必可 pickle；每臂全新解释器 → 不继承父进程任何已打开的 SQLite 连接。
2. **并发上界是构造性的**：`ThreadPoolExecutor(max_workers)` 只负责「等子进程」，
   同时在跑的子进程数天然 ≤ `max_workers`，不靠自觉限流。
3. **两级失败口径**：提交级问题（重名 `job_id`、同一世界目录、规格错误）整轮拒跑，
   一个子进程都不启动；臂级问题（预算耗尽、契约故障、超时、崩溃）只记该臂。
   共享世界目录意味着「每臂独立世界库」这条不变量已被打破，没法判断哪一臂拥有它，
   因此不启动子进程，而不是让两臂去争同一条 SQLite。
4. **缺臂即拒出汇总**：`assemble_summary` 对重复记录 / 未提交过的记录 / 缺失的臂一律抛
   `ParallelFault`；少一臂的汇总比崩溃更危险——调用方会把「只跑了一半」读成「全跑完了」。
5. **失败臂的花费照实计入**：复用内核 `Store.usage` 口径把已烧掉的调用记进汇总，
   止损台账不把失败臂记成 0 调用。
6. **越界响亮报错，不静默 clamp**：`max_workers` 上界只读 `app/limits.py:MAX_CONCURRENCY`；
   并发是费用/限速护栏，意图错位比失败危险。
7. **真实模型通道要两个独立显式开关**：`offline.mode="gateway"` + `LG_RUNTIME_PARALLEL_LIVE=1`，
   否则子进程抛 `gateway_channel_requires_explicit_live_opt_in`。验收全程离线，
   **不含任何真实模型调用**（「真跑」指测试与 CLI 真实执行，不是真连模型）。

## 测试覆盖（15 例，全绿）

正例：
`test_three_offline_arms_run_in_parallel_with_exact_counts`（三臂真并发，精确调用计数）、
`test_cli_runs_a_two_arm_batch_and_writes_the_summary`。

退化与记账：
`test_max_workers_serialises_when_asked`、
`test_budget_exhaustion_fails_only_that_arm`、
`test_rewrite_budget_exhaustion_also_stays_per_arm`、
`test_replaying_the_same_job_id_does_not_double_spend`。

五组必答负例：
`test_two_arms_sharing_a_world_directory_are_refused`（负例②）、
`test_duplicate_job_id_and_bad_spec_refuse_the_whole_submission`、
`test_max_workers_above_single_source_cap_is_refused`、
`test_workdir_conflicting_with_research_database_is_refused`、
`test_no_real_model_channel_is_reachable_without_explicit_opt_in`；
负例① `test_budget_exhaustion_fails_only_that_arm`、
负例③ `test_replaying_the_same_job_id_does_not_double_spend`、
负例④ `test_timed_out_arm_is_reclaimed_and_others_survive`、
负例⑤ `test_summary_with_a_missing_arm_refuses_to_be_built`。

其余：
`test_hard_crash_without_result_file_is_reported_as_missing`、
`test_require_ok_rejects_any_single_failed_arm`。

## 验收证据

### 1. 真跑：任务书指定的验收命令（逐字执行，退出码 0）

命令（本轮真跑，未改一字）：

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_runtime_parallel.py -q
```

原始输出（逐字）：

```
...............                                                          [100%]
```

**退出码：`RC=0`**。

说明：本仓库 `pyproject.toml` 已设 `addopts = "-q"`，与命令里的 `-q` 叠加成 quiet
level 2，故该命令的原文里没有 `15 passed` 汇总行。为让「真跑」的用例数可见，另补一条
同套件、去掉命令行 `-q`（净 verbosity 回到 -1）的跑：

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_runtime_parallel.py
...............                                                          [100%]
15 passed in 24.60s
RC=0
```

用例数以 `--collect-only -q` 实测交叉核对：

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_runtime_parallel.py --collect-only -q
tests/test_runtime_parallel.py: 15
```

### 2. 回归联跑真跑（未因本次新增而退化，rc=0）

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_scene_runtime.py tests/test_scene_runtime_a10.py tests/test_scene_runtime_audit.py tests/test_compile_all.py tests/test_script_concurrency_caps.py tests/test_script_concurrency_caps_b2.py tests/test_conc_guard_shared.py tests/test_docs_code_reconcile.py -p no:randomly
320 passed, 1 warning in 17.47s
RC=0
```

（警告是仓库既有的 `conftest.py` 卫生提示；本轮已设 `LG_LOCK_DIR` 于仓外，未在仓内
创建 `data/`。）

### 3. 反向验证（硬要求）：一个臂失败不会被汇总成整体成功

**测试侧**，四处钉死（原文断言见 `tests/test_runtime_parallel.py`）：

- `test_budget_exhaustion_fails_only_that_arm`：一臂 `call_budget_exhausted` →
  整轮 `ok is False`、`verdict == "reject"`，且 `require_ok(summary)` 抛
  `parallel_run_rejected:arm-b`；
- `test_timed_out_arm_is_reclaimed_and_others_survive`：超时臂同样把整轮压成 reject，
  `failed_job_ids == ["arm-a"]`；
- `test_summary_with_a_missing_arm_refuses_to_be_built`：少一臂连汇总都造不出来
  （`arm_result_missing:arm-b` 直接抛）；
- `test_require_ok_rejects_any_single_failed_arm`：只要有任何一臂未提交，判定函数必抛，
  不给「整体成功」留缝。

**CLI 真跑侧**（本轮实际执行，非引用旧录）——4 臂，其中 `arm-c` 为
`behavior="hard"` + `max_calls=4`（烧满 4 次被内核硬闸拦下），其余 3 臂正常提交：

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe scripts/runtime_parallel.py \
    --jobs C:/Users/6/AppData/Local/Temp/opencode/rp_final_20260930/jobs.json \
    --out C:/Users/6/AppData/Local/Temp/opencode/rp_final_20260930/run --max-workers 4 --timeout-s 120
{"calls_total": 10, "complete": false, "failed_job_ids": ["arm-c"], "jobs_committed": 3, "jobs_failed": 1, "jobs_submitted": 4, "max_workers": 4, "ok": false, "parallel_ok": true, "parallel_ok_reason": "measured=4,expected=4,floor=2", "parallelism_measured": 4, "verdict": "reject", "wall_ms": 2297, "workdir": "C:\\Users\\6\\AppData\\Local\\Temp\\opencode\\rp_final_20260930\\run"}
{"error": "parallel_run_rejected:arm-c"}
RC=1
```

反向验证要点读法：三臂 committed、只有一臂失败，**整体仍判负**——`ok=false`、
`verdict="reject"`、`complete=false`、`require_ok` 抛 `parallel_run_rejected:arm-c`、
CLI 退出码 1。失败臂花费照实入账：逐臂（读 `parallel-summary.json`）：

```
ok=False verdict=reject complete=False failed=['arm-c']
arm-a committed calls= 2 reused= False exit= 0 err= None
arm-b committed calls= 2 reused= False exit= 0 err= None
arm-c call_budget_exhausted calls= 4 reused= False exit= 2 err= call_budget_exhausted
arm-d committed calls= 2 reused= False exit= 0 err= None
```

`calls_total = 2+2+4+2 = 10` 证明失败臂的花费没有被抹掉；同时 4 臂实测峰值并发 = 4。

### 4. 提交级拒跑真跑：零子进程、零 SQLite

两条臂声明同一世界目录（一条带 `/./` 别名），归一后判争用 → 整轮拒跑：

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe scripts/runtime_parallel.py \
    --jobs C:/Users/6/AppData/Local/Temp/opencode/rp_final_20260930/dup.json \
    --out C:/Users/6/AppData/Local/Temp/opencode/rp_final_20260930/run2 --max-workers 2
{"calls_total": 0, "complete": false, "failed_job_ids": ["arm-1", "arm-2", "arm-3"], "jobs_committed": 0, "jobs_failed": 3, "jobs_submitted": 3, "max_workers": 2, "ok": false, "parallel_ok": false, "parallel_ok_reason": "rejected_before_launch", "parallelism_measured": 0, "verdict": "reject", "wall_ms": 0, "workdir": "C:\\Users\\6\\AppData\\Local\\Temp\\opencode\\rp_final_20260930\\run2"}
{"error": "parallel_run_rejected:arm-1,arm-2,arm-3"}
RC=1
```

```
launched= 0 rejected_at= preflight
rejected_jobs= [{"error": "world_dir_contended:c:\\users\\6\\appdata\\local\\temp\\opencode\\rp_final_20260930\\shared", "index": [0, 1], "job_ids": ["arm-1", "arm-2"], "world_dir": "c:\\users\\6\\appdata\\local\\temp\\opencode\\rp_final_20260930\\shared"}]
sqlite under out= []
```

`launched=0`、输出目录下没有任何 `.sqlite`（`rglob("*.sqlite")` 实测为空）。

## 边界遵守

- **未改 `app/`**：单场景内核（`contracts.py` / `pipeline.py` / `store.py` / `client.py`）
  逐字未动，只读复用。
- **未连真实模型**：验收与 smoke 全走离线 `SyntheticClient`（`tokens_in/out = None`，
  汇总 token 如实为 0）；真实网关通道需 `LG_RUNTIME_PARALLEL_LIVE=1` 双重开关，
  未设置。「真跑」证据全部为离线合成通道。
- **未读写研究库**：工作根与每臂世界目录都经真库护栏（拒 `app/config.py` 默认 `data/`
  与 `LG_DATABASE_URL` 所在目录，双向拒），`F:\agi\language-genome\data` 与
  `D:\language-genome-data` 未触碰；真跑与测试都只用仓外临时目录。
- **未新建白名单外文件**：仓内仅本表四文件；所有 smoke/真跑产物在仓外临时目录。
- **未 commit / 未 merge / 未 push**。

## 已知不承诺（写在口径书里，也写在这里）

- 不承诺无人值守、不承诺文学质量、不承诺长篇规模通过：`literary_quality` 恒为
  `not_evaluated`，`cost` 恒为 `null`。
- 超时臂的花费记账为 0（父进程记账），其世界库里的调用行仍在；止损口径以世界库为准。
- `parallel_ok` 判的是「实测峰值并发」达到并发下限，不保证各臂墙钟耗时相同。
- 本轮只交付调度层第一块：跨机/跨盘分布式调度、大规模 Dream、微调与正式发布仍后置。
