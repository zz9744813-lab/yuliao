# 多进程自动调度（runtime-parallel）

对应 [plan.md](plan.md) 第 15 行的后置项：「微调、大规模 Dream、**多进程自动调度**与正式发布后置」。
本文件是该后置项**第一块**的口径书：调度层只做「把一批场景/臂安全地跑起来并如实汇总」，
单场景编译、Writer/Verifier 核验、有限修订、原子提交、幂等恢复、outbox 全部沿用
`app/scene_runtime/` 里**已冻结**的内核契约（`contracts.py` / `pipeline.py` / `store.py`），
本次没有改动 `app/`。

- 实现：`scripts/runtime_parallel.py`
- 测试：`tests/test_runtime_parallel.py`
- 交付：`DELIVERY_runtime_parallel.md`

## 0. 口径摘要（先说不做什么）

| 做 | 不做 |
| --- | --- |
| 多进程并发跑一批场景/臂，每臂一个独立子进程 | 不做进程内多臂共享一条 SQLite |
| 每臂独立世界目录 + 独立世界库（`runtime.sqlite`） | 不让调度层打开任何臂的世界库 |
| 沿用内核 `Budget.max_calls` 硬闸 | 不在调度层另造预算/限速逻辑 |
| 汇总覆盖完整性硬校验，缺臂即拒出汇总 | 不为缺臂编造结果，不给「跑了一半」的汇总 |
| 离线合成通道供测试与本地演练 | 验收不连真实模型、不碰研究库 |

## 1. 不变量

1. **每臂一世界库**：一臂 = 一个 `world_dir` + 一个 `db_name`（默认 `runtime.sqlite`）。
   目录别名（`.`、`/`、`\`、`..`、Windows 大小写）经 `resolve()` + `os.path.normcase` 归一后
   相同即判定争用（`world_dir_contended`），**整轮拒跑**，同批无辜臂一并拒跑。
2. **父进程零世界库连接**：调度层只读 `result.json`，所有 `Store` 都在子进程内构造。
3. **预算硬闸唯一**：调用次数由内核 `reserve_call` 判定，超限抛 `call_budget_exhausted`，
   调度层不改写、不放宽。
4. **失败臂的花费照实计入**：失败臂用同一套 `Store.usage` 口径把已烧掉的调用记进汇总，
   止损台账不把失败臂记成 0 调用（连世界库都没建成时才如实记空）。
5. **汇总覆盖完整**：`assemble_summary` 对「重复记录 / 未提交过的记录 / 缺失的臂」一律抛
   `ParallelFault`，宁可不给汇总。
6. **拒写真库**：工作根与每臂世界目录都不得落在真库目录树里（见 §7）。

## 2. 架构

```
run_parallel(jobs, max_workers, budget, workdir, timeout_s)
  ├─ _check_max_workers        并发上界只取 app.limits.MAX_CONCURRENCY（越界响亮报错）
  ├─ _budget_doc               运行级默认预算合同（逐臂可覆盖）
  ├─ _refuse_protected         真库护栏
  ├─ _preflight                规格规范化 + 提交级冲突检查
  ├─ _launch_all               ThreadPoolExecutor(max_workers) —— 池宽即并发上界
  │    └─ _launch_arm(spec)    Popen: python -X utf8 scripts/runtime_parallel.py
  │                              --run-arm <world_dir>/job.json --result <world_dir>/result.json
  │         └─ run_arm          子进程内：hold arm.lock → _execute_arm → 原子写 result.json
  │              └─ _execute_arm  Store(world_dir/runtime.sqlite) + SceneRunner.run()
  └─ assemble_summary          覆盖完整性校验 → 并发判定 → parallel-summary.json
```

线程池只负责「等子进程」，所有重活都在子进程里：并发上界因此是**构造性**的
（同时在跑的子进程数 ≤ `max_workers`），不靠自觉限流。

**为什么用 `subprocess` 而不是 `multiprocessing`**：

- 超时是 `Popen.kill()` + `communicate()` 的硬回收，不依赖协作式信号；
- 客户端不必可 pickle（网关客户端、内部句柄都能直接在子进程里构造）；
- 每臂一个全新解释器：不会继承父进程里任何已打开的 SQLite 连接、事务或全局单例。

## 3. 作业清单

CLI 接受两种 JSON：裸列表，或 `{"budget": {...}, "jobs": [...]}`。

单条作业的键（**未知键直接拒跑**，不静默丢弃）：

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `job_id` | 是 | — | 臂标识，须满足内核 `Identifier` 约束；整批唯一 |
| `plan` / `knowledge` / `world` | 是 | — | 原样交给内核 `ScenePlan` / `KnowledgePackage` / `World` 校验 |
| `scene_id` / `book_id` / `branch_id` / `idempotency_key` | 否 | 取自 `plan` | 缺失时从 `plan` 兜底，仍缺失即拒跑 |
| `arm` | 否 | `"A"` | 记进汇总，便于按臂归因 |
| `world_dir` | 否 | `<workdir>/<job_id>` | 相对路径按 `<workdir>` 解析；两臂指向同一目录即拒跑 |
| `db_name` | 否 | `runtime.sqlite` | 必须是纯文件名（无目录分隔符）且以 `.sqlite` 结尾 |
| `budget` | 否 | 运行级预算 | 逐臂覆盖，只允许 `Budget` 契约内的键 |
| `offline` | 否 | `{}` | 见 §8；`mode="gateway"` 时是通道声明 |

```jsonc
{
  "budget": {"max_calls": 6},
  "jobs": [
    {"job_id": "s1", "plan": { /* ... */ }, "knowledge": { /* ... */ }, "world": { /* ... */ }},
    {"job_id": "s1-B", "arm": "B", "offline": {"mode": "synthetic", "behavior": "commit"}}
  ]
}
```

## 4. 两级失败口径

**提交级 → 整轮拒跑，一个子进程都不启动**（`rejected_at: "preflight"`）：

| 码 | 触发 |
| --- | --- |
| `unknown_job_keys` / `job_not_a_mapping` / `job_*_missing` | 规格不合法 |
| `unknown_budget_keys` / `budget_contract_invalid` / `budget_not_a_mapping` | 预算合同不合法 |
| `job_id_invalid` / `scene_id_invalid` / `arm_invalid` / `idempotency_key_invalid` | 标识符不合 `Identifier` |
| `duplicate_job_id:<id>` | 同批重名（重名同时也会撞默认世界目录，两条问题都报） |
| `world_dir_contended:<path>` | 两臂指向同一世界目录（含别名归一后相同） |
| `db_name_must_be_a_plain_sqlite_filename` | 库名不是纯 `.sqlite` 文件名 |
| `workdir_conflicts_research_database` | 工作根/世界目录落在真库目录树里 |

拒跑汇总仍然写出（`ok=false`、`verdict="reject"`、`launched=0`、`parallel_ok=false`），
逐条问题在 `rejected_jobs` 里，每条带 `index` 指明提交清单里的位置。重名 `job_id`
无法在 `arms` 里逐条表达，`arms` 按去重后的 `job_id` 列出，问题以 `rejected_jobs` 为准。

**臂级 → 只记该臂，其它臂照跑**：

| 状态 | 含义 |
| --- | --- |
| `committed` | 内核返回提交收据（含幂等重放，`reused=true`） |
| `call_budget_exhausted` / `rewrite_budget_exhausted:...` | 预算硬闸拦下（内核原码，不改写） |
| `canon_audit_failed:...` | 正文投影不一致（审计不通过） |
| `timeout` | 墙钟超限被强杀，见 §5 |
| `missing` | 崩溃/被强杀/结果文件缺失/身份不符，**父进程不替它编结果** |
| `spec_invalid` / `internal_failure` | 子进程内部规格或非预期异常 |
| `rejected` | 提交级拒跑占位 |

子进程退出码：`0` 提交；`2` 内核故障（预算/契约/审计）；`3` 规格或内部异常。
父进程交叉校验 `exit_code == 0` 与 `status == "committed"` 是否一致，不一致按 `missing` 记。

## 5. 超时与回收

- 单臂墙钟上限 `timeout_s`（默认 120s）；超时后 `proc.kill()` + `communicate()`，必等回收。
- 被强杀的臂没有 `result.json` —— 这正是缺臂信号，父进程记 `status="timeout"`、
  `error="arm_result_file_absent"`，不猜结果。
- 回收证据：子进程持有 `arm.lock`（Windows `msvcrt`、POSIX `fcntl`），父进程回收后
  非阻塞试锁——能拿到锁即 `reclaimed=true`；拿不到则进 `reclaim_problems`，
  `require_ok` 抛 `arm_not_reclaimed`，**绝不放行**。

## 6. 汇总口径

`parallel-summary.json`（写在 `<workdir>`）：

| 字段 | 口径 |
| --- | --- |
| `jobs_submitted` / `jobs_committed` / `jobs_failed` / `failed_job_ids` | 提交、提交成功、失败、失败臂清单 |
| `launched` | 实际拿到 `pid` 的臂数（提交级拒跑恒为 0） |
| `complete` | 每条记录都拿到了子进程结果文件 |
| `ok` / `verdict` | `ok` = 每臂都 `committed`；`verdict` = `accept` / `reject` |
| `calls_total` / `tokens_total` / `call_duration_ms_total` | 逐臂求和，**含失败臂已消耗部分** |
| `parallelism_measured` | 由每臂 `started_at`/`ended_at` 扫线求实测峰值并发臂数 |
| `parallel_ok` / `parallel_ok_reason` | 期望 `min(max_workers, 臂数)`；串行请求（期望 ≤1）只要求 `measured ≤ 1`，否则要求 `measured ≥ 2` 且 `≤ max_workers` |
| `concurrency_bound_ok` | 实测峰值不超过 `max_workers` |
| `reclaim_problems` | 超时且未确认回收的臂 |
| `arms[]` | 逐臂：`status`、`error`、`pid`、`exit_code`、`timed_out`、`reclaimed`、`calls`、`tokens`、`wall_ms`/`kernel_ms`、`stdout_tail`/`stderr_tail`、`reused`、`result_verified`、`db_path`、`world_dir` |
| `cost` / `literary_quality` | 恒为 `null` / `"not_evaluated"`：调度层不估价、不评文学质量 |

`require_ok(summary, parallel=True)` 把汇总变成可执行判定：任一臂未提交 →
`parallel_run_rejected`；要求并行而未达标 → `parallelism_not_met`；有未回收臂 →
`arm_not_reclaimed`。

## 7. 护栏

- **真库只读**：工作根与每臂世界目录都不得等于、落在、或**包含**下面任一路径
  （两个方向都拒，否则把真库目录当工作根同样会把产物混进研究库）：
  `app/config.py` 的默认 `data/` 目录、`LG_DATABASE_URL` 指向的 SQLite 库所在目录。
  违例抛 `workdir_conflicts_research_database`。
- **并发上限单源**：`max_workers` 上界只读 `app/limits.py:MAX_CONCURRENCY`；
  越界抛 `max_workers_above_cap`，下界 `< 1` 抛 `max_workers_below_minimum`，
  **不静默 clamp**（并发是费用/限速护栏，意图错位比失败危险）。
- **真实模型通道需双重显式开关**：`offline.mode="gateway"` 且环境变量
  `LG_RUNTIME_PARALLEL_LIVE=1`，否则子进程抛
  `gateway_channel_requires_explicit_live_opt_in`。默认离线，不读任何凭据。

## 8. 离线合成通道

`SyntheticClient` 不连网关、不产生真实 token（`tokens_in/out = None`），writer 给一段
确定性正文、verifier 逐条引原文照抄计划里的 `changes`，机械闸全过 —— 正例每臂**正好
2 次调用**（writer.0 + verifier.0）即提交。`behavior` 只控制故障形态：

| `behavior` | 形态 |
| --- | --- |
| `commit`（默认） | 正例，提交 |
| `hard` | verifier 恒报一个 hard issue，烧满 `max_rewrites` 后如实失败 |
| `hang` | 每次回复前睡 `delay_s`，交给父进程超时回收 |
| `crash` | 直接 `os._exit(97)`：不走异常处理、不落结果文件（按缺臂处理） |

## 9. CLI

```
python scripts/runtime_parallel.py --jobs jobs.json --out runs/2026-09-30 \
    --max-workers 4 --timeout-s 120 --budget '{"max_calls": 6}'
```

- `--jobs` 作业清单；`--out` 汇总与各臂世界的根；`--max-workers` / `--timeout-s` /
  `--budget` 覆盖运行级默认。
- `--run-arm <spec.json> --result <result.json>` 是子进程内部入口，手工调用会得到
  `run_arm` 的退出码。
- 退出码：`0` 全部提交且（若要求并行）并发达标、无未回收臂；`1` 有臂未提交 /
  并发未达标 / 有未回收臂；`2` 调度层自身参数或护栏违例（`ParallelFault`）。

## 10. 验收

验收测试**必须真跑**（真起子进程、真建每臂独立 SQLite 世界库；只是模型通道离线）。
以下命令逐字执行、退出码 0，原始输出见 `DELIVERY_runtime_parallel.md` 的「真跑证据」一节：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_runtime_parallel.py -q
```

用例覆盖：任务书要求的正例 + 五组负例（共享世界目录、重名 `job_id`、未知键、
`max_workers` 越界、拒写真库路径），以及串行退化、预算耗尽只影响单臂、失败臂花费照实
记账、重放不重复花费、超时臂回收、硬崩按缺臂处理、汇总缺臂拒绝、无 live 开关时真实
通道不可达、CLI 入口退出码。

**反向验证（硬要求）**：必须有一条负例证明「一个臂失败不会被汇总成整体成功」。本节由
四处共同钉死：

- `test_budget_exhaustion_fails_only_that_arm`：一臂 `call_budget_exhausted` 时整轮
  `ok=false`、`verdict="reject"`，且 `require_ok` 抛 `parallel_run_rejected:arm-b`；
- `test_timed_out_arm_is_reclaimed_and_others_survive`：超时臂同样把整轮压成 reject；
- `test_summary_with_a_missing_arm_refuses_to_be_built`：少一臂连汇总都造不出来；
- `test_require_ok_rejects_any_single_failed_arm`：只要有任何一臂未提交，判定函数必抛。

## 11. 明确不承诺

- 不承诺无人值守、不承诺文学质量、不承诺长篇规模通过；`literary_quality` 恒为
  `not_evaluated`，`cost` 恒为 `null`。
- 不承诺超时臂的花费记账：被强杀的臂由父进程记账为 0，其世界库里的调用行仍在，
  止损口径以世界库为准。
- 不承诺跨机/跨盘分布式调度；并发上界就是 `max_workers` 与 `MAX_CONCURRENCY` 的较小者。
- 不把确定性夹具当真实模型运行：离线合成通道的产物只用于调度层机械回归。
