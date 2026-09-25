"""并发上限的单一真源：HTTP 入参闸与执行侧线程池共用同一个常量。

为什么要有这个模块（2026-09-25 审计「实验并发上限只有 HTTP 入口有闸」）：
上限原先**只**写在 `app/api.py` 的 `ExperimentIn.concurrency`（`ge=1,
le=_MAX_CONCURRENCY`），挡得住 `POST /experiments`；但旁路脚本直接往
`exp.config["concurrency"]` 塞值（`scripts/scale_corpus.py:121` 用命令行参数、
`scripts/run_calibration.py:52/178` 的 `--concurrency` 无上界校验），
执行侧 `app/experiments.py::_pool_map` 原样 `max(1, int(concurrency))` 开线程池
⇒ 绕开 API 就能把并发拉满。常量分散两处必然再次漂移，故收在本模块，
**入参侧与执行侧都从这里取值，不许各写一份**。

`MAX_CONCURRENCY = 16` 的依据（沿用 api.py:179-180 的原始说明，未重新定价）：
  · 既有全部实验的 concurrency 取值 = 4：`DEFAULT_CONFIG`（experiments.py:66）
    与旁路脚本 `bench_recon_setup.py:62`、`gen_new_segments.py:101`、
    `controlled_corruption.py:914` 全是 4，`run_calibration.py:52` 默认也是 4；
  · 网关单次超时 300s（`config.HTTP_TIMEOUT_S`，config.py:124）→ 留 4× 余量 = 16；
  · 定位是**费用/限速护栏**（挡持令牌者把并发拉满放大花费、撞网关限速），
    不是吞吐调优上限；要调高只改这一处，两边同时跟随。
"""
from __future__ import annotations

# 实验 stage 线程池的 worker 上界（HTTP 入参 `concurrency` 上限 = 执行侧上限）。
MAX_CONCURRENCY = 16
