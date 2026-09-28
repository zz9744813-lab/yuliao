#!/usr/bin/env python3
"""Read-only K4/K5 scene-bundle structure check; never authorizes live use."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.scene_runtime.offline_scene_bundle import (  # noqa: E402
    SceneBundleError, check_offline_scene_bundle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--expected-pack-sha256", required=True,
                        help="从作品包独立核验得到的 SHA-256；只比较声明，不读取作品包")
    args = parser.parse_args()
    try:
        report = check_offline_scene_bundle(
            args.bundle, expected_pack_sha256=args.expected_pack_sha256)
    except SceneBundleError as exc:
        print(json.dumps({"structure_pass": False, "live_ready": False,
                          "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
