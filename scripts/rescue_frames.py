"""M-gap 根因修复的存量救活：把 failed frame 里「内容合格、只是字段别名」的行
经 normalize_frame_payload 归一后翻成 repaired。零 API 调用。

依据（2026-09-14）：四语料 20 个失败 M 里 18 个是 facts[].content→statement 改名；
repair 路径原本为此再烧一次 LLM。V2 prompt + validate_frame 归一层堵住增量，
本脚本只处理存量。冻结实验（EXP-0911-B82D / EXP-0913-BA19）不在处理范围。

用法：python scripts/rescue_frames.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.frames_schema import validate_frame
from app.models import Frame

TARGET_EXPS = ["EXP-0914-C812", "EXP-0914-FF7B", "EXP-0914-EE18", "EXP-0914-6DD3"]
MARKER = "[rescued:normalize-alias] "


def _try_payloads(gran: str, raw: str | None, payload: dict | None):
    """候选 payload：先看已解析的 payload，再从 raw_output 兜底剥 fence 解析。"""
    cands = []
    if isinstance(payload, dict):
        cands.append(payload)
    if raw:
        t = raw.strip()
        for fence in ("```json", "```"):
            if t.startswith(fence):
                t = t[len(fence):]
        if t.endswith("```"):
            t = t[:-3]
        try:
            p = json.loads(t.strip())
            if isinstance(p, dict):
                cands.append(p)
        except (ValueError, TypeError):
            pass
    for p in cands:
        m, err = validate_frame(gran, p)
        if m is not None:
            return p
    return None


def main() -> None:
    stats = {}
    with db.session() as s:
        rows = (s.query(Frame)
                .filter(Frame.experiment_id.in_(TARGET_EXPS),
                        Frame.status == "failed",
                        Frame.granularity.in_(["M", "L"])).all())
        for f in rows:
            fixed = _try_payloads(f.granularity, f.raw_output, f.payload)
            if fixed is None:
                stats[(f.experiment_id, "still_failed")] = stats.get((f.experiment_id, "still_failed"), 0) + 1
                continue
            f.payload = fixed
            f.status = "repaired"
            if not (f.raw_output or "").startswith(MARKER):
                f.raw_output = MARKER + (f.raw_output or "")
            stats[(f.experiment_id, "rescued")] = stats.get((f.experiment_id, "rescued"), 0) + 1
        s.commit()
    for eid in TARGET_EXPS:
        r = stats.get((eid, "rescued"), 0)
        st = stats.get((eid, "still_failed"), 0)
        print(f"{eid}: rescued={r} still_failed={st}")


if __name__ == "__main__":
    main()
