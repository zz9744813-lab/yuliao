"""任务六执行：对现有 336 候选 + 30 human 段跑七维自然度 v2（同规模重判，不扩任何量）。

幂等：已存在的 (subject_type, subject_id, kind='naturalness_v2', model) 跳过（含 failed 重试）。
"""
import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from app import db
from app.gateway import is_serial_model
from app.judges import NATURALNESS_V2_PROMPT_VERSION, judge_naturalness_v2
from app.models import Candidate, Experiment, JudgeRun, Segment
from _conc_guard import check_conc, pool_workers  # noqa: E402  # 并发闸共用入口（上界 app/limits.MAX_CONCURRENCY + 运行时兜底）

# 位置参数口径与改前一致：argv[1]=实验号 argv[2]=评委模型 argv[3]=并发（默认 6）。
# 解析挪进 parse_cli（作为脚本执行时才跑），import 本模块不再有 argv 副作用；
# conc 越界由并发闸响亮报错退出（不再无界直开线程池）。
EXP = "EXP-0911-B82D"
JUDGE_MODEL = "moonshotai/kimi-k3"
CONC = 6


def parse_cli(argv: list[str]) -> tuple[str, str, int]:
    exp = argv[1] if len(argv) > 1 else EXP
    model = argv[2] if len(argv) > 2 else JUDGE_MODEL
    ap = argparse.ArgumentParser(prog=argv[0] if argv else "run_nat_v2.py",
                                 add_help=False)
    conc = check_conc(ap, argv[3] if len(argv) > 3 else CONC, "conc（第 3 位置参数）")
    return exp, model, conc

_counter = {"n": 0}
_lock = threading.Lock()


def work(subject_type: str, subject_id: str, text: str) -> dict:
    with db.session() as s:
        done = {j.subject_id for j in s.query(JudgeRun)
                .filter_by(experiment_id=EXP, subject_type=subject_type,
                           judge_kind="naturalness_v2", model=JUDGE_MODEL)
                .filter(JudgeRun.status == "ok").all()}
    if subject_id in done:
        return {"skip": True}
    out = judge_naturalness_v2(text=text, model=JUDGE_MODEL)
    verdict = out.get("verdict")
    with db.session() as s:
        s.add(JudgeRun(experiment_id=EXP, subject_type=subject_type, subject_id=subject_id,
                       judge_kind="naturalness_v2", model=JUDGE_MODEL,
                       prompt_version=NATURALNESS_V2_PROMPT_VERSION,
                       verdict=verdict, confidence=out.get("confidence"),
                       abstain=out.get("abstain", False), status=out["status"]))
        s.commit()
    with _lock:
        _counter["n"] += 1
        if _counter["n"] % 25 == 0:
            print(f"progress: {_counter['n']}", flush=True)
    return {"ok": out["status"] == "ok"}


def main():
    jobs = []
    with db.session() as s:
        exp = s.get(Experiment, EXP)
        for sid in exp.config["segment_ids"]:
            seg = s.get(Segment, sid)
            if seg:
                jobs.append(("human_segment", seg.id, seg.text))
        for c in s.query(Candidate).filter_by(experiment_id=EXP, status="ok").all():
            jobs.append(("candidate", c.id, c.text))
    print(f"nat_v2 subjects: {len(jobs)} model={JUDGE_MODEL} conc={CONC}", flush=True)
    errs = 0
    workers = pool_workers(CONC, [JUDGE_MODEL], serial_check=is_serial_model)   # 运行时兜底：上限截断 + 串行强制
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(lambda j: work(*j), jobs):
            if r.get("ok") is False:
                errs += 1
    print(f"done, judged={_counter['n']} errors={errs}")


if __name__ == "__main__":
    EXP, JUDGE_MODEL, CONC = parse_cli(sys.argv)
    main()
