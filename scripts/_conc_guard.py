"""旁路脚本并发闸的**唯一**实现（6 个脚本 + 后续批次共用，不再逐脚本复制）。

背景（2026-09-25 会审双席一致指出）：`lg-fix-script-conc-caps` 把
`limits.MAX_CONCURRENCY` 做成了上限单一真源，但 `_pool_workers` / `_check_conc`
这对 helper 本身在 6 个脚本里逐字复制了 6 份——改文案、改下界口径要同步 6 处，
必然漂移。本模块把这两件事收成一处：

  · `check_conc(parser, value, flag)` —— CLI 上/下界闸，越界**响亮报错退出**
    （不静默 clamp：命令行数值是操作者亲手声明的意图，静默改写会让人以为在
    200 并发实跑 16，对费用/限速护栏而言意图错位比失败危险）。
  · `pool_workers(conc, models=(), tag="conc", serial_check=is_serial_model)` ——
    运行时兜底：绕过 argparse 直接调函数也不能越界；命中单账号 CLI 通道
    （判定口径唯一：`app.gateway.is_serial_model`，`serial_check` 允许调用方
    注入自己在模块层持有的同一绑定，默认即网关真身）时 workers 恒 1 并显式打印。

下界口径（会审点名的缝隙）：`--conc 0/负数` 以前被 CLI 放行、运行时 `max(1,…)`
**静默**归一，与本设计「拒绝静默 clamp」的自述不一致。现在**下界也在 CLI 层报错**，
运行时兜底同样不再静默（打印并取 1）。

上限真源仍然只有 `app.limits.MAX_CONCURRENCY` 一处；本模块只读它，不另立常量。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import limits                      # noqa: E402
from app.gateway import is_serial_model     # noqa: E402

MIN_CONCURRENCY = 1


def _die(msg: str) -> None:
    """响亮报错退出（exit 2，与 argparse 的用法错误同码）。"""
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(2)


def check_conc(parser, value: int, flag: str = "--conc") -> int:
    """CLI 闸：上界 `limits.MAX_CONCURRENCY`、下界 `MIN_CONCURRENCY`，越界即报错退出。

    优先用 parser.error（argparse 规范出口：打印用法 + exit 2）；没有 parser 时
    回落到 _die。返回原值，方便 `args.conc = check_conc(...)` 写法。
    """
    def _fail(msg: str) -> None:
        if parser is not None and hasattr(parser, "error"):
            parser.error(msg)          # 内部 raise SystemExit(2)
        _die(msg)

    if value is None:
        return value
    try:
        value = int(value)
    except (TypeError, ValueError):
        _fail(f"{flag}={value!r} 不是整数")
    if value < MIN_CONCURRENCY:
        _fail(f"{flag}={value} 低于下界 {MIN_CONCURRENCY}；"
              f"本脚本拒绝静默归一（下界也是闸，越界即报错退出）")
    if value > limits.MAX_CONCURRENCY:
        _fail(f"{flag}={value} 超过上限 {limits.MAX_CONCURRENCY}"
              f"（单一真源 app/limits.py::MAX_CONCURRENCY）；"
              f"本脚本拒绝静默 clamp，越界即报错退出")
    return value


def pool_workers(conc, models=(), tag: str = "conc",
                 serial_check=is_serial_model) -> int:
    """运行时兜底 worker 数：夹到 [MIN, MAX_CONCURRENCY]；串行模型恒 1。

    界内正常值原样返回、零额外输出（默认路径与改前逐字一致）。
    越界或非正值不再静默：打印一行说明后取边界值。
    命中单账号 CLI 模型 → workers 恒 1 并打印「串行强制」。

    `serial_check` 是可注入的串行判定器：默认取模块层绑定的
    `app.gateway.is_serial_model`（判定口径唯一真身）；脚本侧把它换成自己在
    模块层持有的同一绑定（`serial_check=is_serial_model`），保证「替换复用函数
    判定跟着变」的注入入路不失效——判定函数只有一处实现，脚本只传引用。
    """
    try:
        requested = int(conc)
    except (TypeError, ValueError):
        print(f"[{tag}] 非法 conc={conc!r} → workers={MIN_CONCURRENCY}", flush=True)
        return MIN_CONCURRENCY

    workers = min(max(requested, MIN_CONCURRENCY), limits.MAX_CONCURRENCY)
    hits = [m for m in models if serial_check(m)]
    if hits:
        print(f"[{tag}] 串行强制：命中单账号 CLI 模型 {hits} → workers=1（请求 conc={conc}）",
              flush=True)
        return 1
    if workers != requested:
        print(f"[{tag}] 越界截断：conc={conc} → workers={workers}"
              f"（上限 app/limits.MAX_CONCURRENCY={limits.MAX_CONCURRENCY}）", flush=True)
    return workers
