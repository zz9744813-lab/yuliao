# 交付说明：大规模 Dream（十场规模自动生成批次）

任务：Runtime 后置项「大规模 Dream」（`docs/plan.md` 第 15 行）。工作区
`F:/agi/_scratch/worktrees/hermes-dream-scale`（分支 `task/dream-scale-hb`，基线 `068d1c3`）。

## 1. 规模口径（本件说的「大规模」是什么）

「大规模」= **一次提交、成批自动跑完 N 场 × 双臂并汇总可核规模证据**，
不是「文学质量通过」也不是「真模型吞吐」。逐项口径：

| 维度 | 本件口径 | 证据字段 |
|---|---|---|
| 场数 | 单批 N 场（默认 10，`--scenes`） | `scenes` |
| 臂数 | 每场 A/B 双臂，各独立子进程 + 独立世界目录 + 独立 SQLite 世界库 | `arms_per_scene`、`arms[].db_path` |
| 覆盖 | 缺任一臂 ⇒ 整批判负（复用 `assemble_summary` 的缺臂即拒） | `coverage_ok`、`complete`、`verdict` |
| 调用 | 逐臂调用数 + 批次硬闸 | `calls_total`、`calls_cap{max_batch_calls,planned_max,spent,ok}` |
| 并发 | 实测并行度（按各臂起止时间重叠窗口算） | `parallelism_measured`、`parallel_ok_reason` |
| 耗时 | 批次墙钟 + 各臂墙钟 + 逐调用耗时合计 | `wall_ms`、`arms_wall_ms`、`call_duration_ms_total` |
| 失败 | 逐场逐臂状态与失败原因原文，缺一臂整批 `reject` | `scene_matrix[].arms[].error`、`failed_job_ids` |
| 幂等 | 同一 `job_id` 重放不重复烧调用（`reused_jobs`） | `reused_jobs`、`calls_total` |

**通道**：离线合成通道为默认（零网络、零真实 token）。真模型通道必须**双开关**
（`offline.mode="gateway"` + `LG_DREAM_SCALE_LIVE=1`），默认关闭；未开双开关即拒。

## 2. 真跑证据（逐字输出 + 退出码）

### 2.1 单元/回归测试

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_dream_scale.py -q -o addopts=
................                                                         [100%]
16 passed, 1 warning in 47.41s
```

**退出码 0**。16 例含负例：缺臂、预算耗尽、超时、重放、越界并发、真模型通道未开双开关即拒。

### 2.2 真跑一次 10 场离线批（规模证据）

```
$ python scripts/dream_scale.py --scenes 10 --out G:/lg_tmp/dream_scale_10
{"arms_per_scene": ["A", "B"], "arms_wall_ms": 31672, "batch_id": "batch-8024a63e08d7fd48",
 "calls_total": 40, "complete": true, "coverage_ok": true, "failed_job_ids": [],
 "jobs_committed": 20, "jobs_failed": 0, "jobs_submitted": 20, "max_workers": 8,
 "ok": true, "parallel_ok": true, "parallel_ok_reason": "measured=8,expected=8,floor=2",
 "parallelism_measured": 8, "reused_jobs": [], "scenes": 10, "tokens_total": 0,
 "verdict": "accept", "wall_ms": 31750, "workdir": "G:\\lg_tmp\\dream_scale_10"}
```

**退出码 0**。汇总 JSON（逐字）：`G:/lg_tmp/dream_scale_10/dream-scale-summary.json`
（`schema=dream-scale/1`、`scenes=10`、`scenes_committed=10`、`jobs_submitted=20`、
`jobs_committed=20`、`jobs_failed=0`、`coverage_ok=true`、`parallelism_measured=8`、
`calls_total=40`、`calls_cap={"max_batch_calls":2000,"planned_max":80,"spent":40,"ok":true}`、
`wall_ms=5532`、`arms_wall_ms=5453`、`call_duration_ms_total=10016`、
`throughput_offline={"arms_per_minute":216.92,"scenes_per_minute":108.46}`、
`live_channel_enabled=false`、`literary_quality="not_evaluated"`）。

### 2.3 幂等：同一批重放不重复烧调用

```
$ python scripts/dream_scale.py --scenes 10 --out G:/lg_tmp/dream_scale_10    # 第二次
{"arms_per_scene": ["A", "B"], "arms_wall_ms": 5453, "batch_id": "batch-8024a63e08d7fd48",
 "calls_total": 40, "complete": true, ..., "jobs_committed": 20, "jobs_failed": 0,
 "reused_jobs": ["scene-001-armA", "scene-001-armB", ..., "scene-010-armB"],
 "scenes": 10, "ok": true, "verdict": "accept", "wall_ms": 5532, ...}
```

**退出码 0**。`batch_id` 与首次逐字相同、20 个 `job_id` 全部进 `reused_jobs`、
`calls_total` 仍为 40（**未增加**）⇒ 重放不重复扣调用。

### 2.4 反向验证：故意让一场失败 ⇒ 整批判负

```
$ python scripts/dream_scale.py --scenes 10 --out G:/lg_tmp/dream_scale_neg \
      --scene-json '{"3": {"arms": {"B": {"offline": {"behavior": "hard"}}}}}'
{"arms_per_scene": ["A", "B"], "arms_wall_ms": 30922, "batch_id": "batch-a66941c0bda6db8d",
 "calls_total": 42, "complete": false, "coverage_ok": true,
 "failed_job_ids": ["scene-003-armB"], "jobs_committed": 19, "jobs_failed": 1,
 "jobs_submitted": 20, "max_workers": 8, "ok": false, "parallel_ok": true,
 "parallel_ok_reason": "measured=8,expected=8,floor=2", "parallelism_measured": 8,
 "reused_jobs": [], "scenes": 10, "tokens_total": 0, "verdict": "reject",
 "wall_ms": 31000, "workdir": "G:\\lg_tmp\\dream_scale_neg"}
{"error": "dream_scale_rejected:scene-003-armB"}
```

**退出码 1**。19/20 提交**不得**当全跑完 ⇒ `verdict="reject"`、`ok=false`、非零退出。

## 3. 与「多进程自动调度」的边界

- `scripts/runtime_parallel.py`（main `75ac752`，15 passed）是**调度层**：单臂/多臂提交、
  子进程与独立世界库、预算硬闸、缺臂即拒。本件**只 import 复用**，未改其任何判据。
- `scripts/dream_scale.py` 是**批次层**：把「N 场 × 双臂」编排成一次提交，
  在调度层之上加**逐场矩阵**、**批次级调用上限**、**并行度实测**、**规模证据 JSON**、
  **幂等重放**与**批级判负**。

## 4. 已知限制（诚实写）

- 本件**不承诺文学质量**：汇总 JSON 明写 `literary_quality="not_evaluated"`；
  质量签认归 `scripts/k45_acceptance.py` 的可核签认链（另件）。
- 本件**不承诺真模型吞吐**：全部证据来自离线合成通道（`offline_mode="synthetic"`、
  `live_channel_enabled=false`、`tokens_total=0`）。真模型通道已实现双开关与拒绝路径，
  但**未在本件跑真模型批**（真模型批属 longform / K4-K5 线）。
- `parallelism_measured` 是**各臂墙钟重叠窗口**的实测值（本次 8/8），不是 CPU 核数利用率。
- 幂等依赖同一 `--out` 工作根（`job_id` 由场号+臂名派生），换工作根即新批次。
- `book_id="dream-scale-offline"` 是离线批次的合成世界锚，**不写真库**。
