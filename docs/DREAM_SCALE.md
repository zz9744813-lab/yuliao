# DREAM_SCALE：大规模 Dream 批次层

`scripts/dream_scale.py`（`schema: dream-scale/1`）把「一场一场手动跑」升级为
**一次提交、成批自动跑完 N 场 × 双臂并汇总规模证据**。

## 用法

```bash
# 离线默认（零网络、零真实 token）：10 场 × A/B 双臂
python scripts/dream_scale.py --scenes 10 --out G:/lg_tmp/dream_scale_10

# 反向验证（故意让某场某臂失败 ⇒ 整批判负、非零退出）
python scripts/dream_scale.py --scenes 10 --out G:/lg_tmp/dream_scale_neg \
    --scene-json '{"3": {"arms": {"B": {"offline": {"behavior": "hard"}}}}}'
```

主要参数：`--scenes`（默认 10）、`--arms`（默认 `A,B`）、`--out`（批次工作根，必填）、
`--book-id`、`--max-workers`（默认 8）、`--timeout-s`、`--budget`、`--max-batch-calls`
（默认 2000）、`--arm-delay-s`、`--offline-mode`、`--offline-json`、`--scene-json`。

## 产物

| 文件 | 内容 |
|---|---|
| `dream-scale-summary.json` | 批次规模证据（见下） |
| `dream-scale-jobs.json` | 逐场逐臂作业矩阵（含 `job_id`、世界库路径、退出码） |
| `parallel-summary.json` | 调度层（`runtime_parallel`）汇总 |
| `scene-<NNN>-arm<A/B>/` | 每臂独立世界目录（含 `runtime.sqlite` 世界库） |

## 规模证据字段

`scenes`、`scenes_committed`、`arms_per_scene`、`jobs_submitted`、`jobs_committed`、
`jobs_failed`、`failed_job_ids`、`failed_arms`、`calls_total`、`calls_cap`
（`{max_batch_calls,planned_max,spent,ok}`）、`wall_ms`、`arms_wall_ms`、
`call_duration_ms_total`、`parallelism_measured`、`parallel_ok`、`parallel_ok_reason`、
`throughput_offline`、`reused_jobs`、`scene_matrix[].arms[].{status,error,db_path,commit_id}`。

## 判负口径（缺一臂不算全跑完）

- `coverage_ok=false`、`complete=false`、`jobs_failed>0` ⇒ `verdict="reject"`、`ok=false`、退出码 1，
  并在 stderr 打 `dream_scale_rejected:<job_id>`。
- 缺臂即拒复用 `runtime_parallel.assemble_summary` 的既有判据，本层**不改**其语义。

## 通道

- 离线合成（默认）：`offline_mode="synthetic"`、`live_channel_enabled=false`、`tokens_total=0`。
- 真模型通道：`offline.mode="gateway"` **且** `LG_DREAM_SCALE_LIVE=1`（双开关），默认关闭；
  缺一即拒（负例已覆盖）。

## 幂等

`job_id` 由场号 + 臂名派生，落在 `--out` 工作根下：同一工作根重放时已提交作业进
`reused_jobs`，`calls_total` 不增加（实测 20/20 复用、调用数仍 40）。

## 边界与已知限制

- 不承诺文学质量（`literary_quality="not_evaluated"`）与真模型吞吐；质量签认走
  `scripts/k45_acceptance.py` 的可核签认链。
- `parallelism_measured` 为各臂墙钟重叠窗口实测值，不是 CPU 利用率。
- 不写真库：`book_id="dream-scale-offline"` 为离线合成世界锚。
