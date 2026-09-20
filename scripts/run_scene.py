"""Bounded live scene CLI. Default validates only; --live makes bounded requests.

  python -X utf8 scripts/run_scene.py --bundle examples/scene_runtime/ferry.json --out ...
  Add --live --writer-model MODEL --verifier-model MODEL --take 1 (then 3).
  Repeating the identical command reuses frozen packages and committed receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.scene_runtime import RUNTIME_VERSION
from app.scene_runtime.contracts import Budget, KnowledgePackage, RuntimeFault, ScenePlan, World, canonical, digest
from app.scene_runtime.knowledge import genome_package
from app.scene_runtime.store import Store


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--take", type=int, default=1)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--writer-model")
    parser.add_argument("--verifier-model")
    parser.add_argument("--genome-db", type=Path)
    parser.add_argument("--strategy", action="append", default=[])
    parser.add_argument("--stop-after-verified", action="store_true")
    parser.add_argument("--upgrade-checkpoint", metavar="REASON",
                        help="Explicit audited implementation upgrade; keeps frozen inputs, calls and canon")
    args = parser.parse_args(argv)
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    world = World.model_validate(bundle["world"])
    budget = Budget.model_validate(bundle["budget"])
    plans = [ScenePlan.model_validate(p) for p in bundle["scenes"]]
    if not 1 <= args.take <= len(plans):
        parser.error("--take must select at least one available scene")
    if bool(args.genome_db) != bool(args.strategy):
        parser.error("--genome-db and --strategy must be supplied together")
    if args.live and not (args.writer_model and args.verifier_model):
        parser.error("--live requires both model bindings")
    args.out.mkdir(parents=True, exist_ok=True)
    package_path = args.out / "knowledge-package.json"
    binding_path = args.out / "knowledge-source.json"
    binding = {"bundle_hash": digest(bundle), "strategy_ids": args.strategy,
               "genome_db": str(args.genome_db.resolve()) if args.genome_db else None}
    if package_path.exists():
        if not binding_path.exists() or json.loads(binding_path.read_text(encoding="utf-8")) != binding:
            raise RuntimeFault("output_directory_input_conflict")
        knowledge = KnowledgePackage.model_validate(json.loads(package_path.read_text(encoding="utf-8")))
    else:
        knowledge = (genome_package(args.genome_db, world.book_id, args.strategy) if args.genome_db
                     else KnowledgePackage.model_validate(bundle["knowledge"]))
        write_json(package_path, knowledge.model_dump())
        write_json(binding_path, binding)
    store = Store(args.out / "runtime.sqlite")
    store.create_world(world)
    files = sorted((ROOT / "app/scene_runtime").glob("*.py")) + [Path(__file__)]
    manifest = {"runtime": RUNTIME_VERSION, "bundle_hash": digest(bundle), "knowledge_hash": digest(knowledge),
                "implementation": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                "budget_per_scene": budget.model_dump(), "selected_scenes": args.take,
                "requested_models": {"writer": args.writer_model, "verifier": args.verifier_model},
                "mode": "live" if args.live else "validate_only", "literary_quality": "not_evaluated"}
    # An existing execution is pinned to its implementation, not just a version label.
    manifest_path = args.out / "manifest.json"
    previous = None
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous["mode"] == "live" and previous["implementation"] != manifest["implementation"]:
            if not args.upgrade_checkpoint:
                raise RuntimeFault("implementation_changed_use_new_output_directory_or_audited_upgrade")
            audit = store.audit(world.book_id)
            if not audit["ok"]:
                raise RuntimeFault("cannot_upgrade_inconsistent_canon")
            history = args.out / "implementation-upgrades.jsonl"
            with history.open("a", encoding="utf-8") as f:
                from app.scene_runtime.store import utcnow
                f.write(canonical({"at": utcnow(), "reason": args.upgrade_checkpoint,
                    "before": previous, "after": manifest, "canon_audit": audit}) + "\n")
    if args.live or previous is None or previous["mode"] != "live":
        write_json(manifest_path, manifest)
    results = []
    error = None
    if args.live:
        from app.scene_runtime.client import GatewayClient
        from app.scene_runtime.pipeline import SceneRunner
        runner = SceneRunner(store, GatewayClient(args.writer_model, args.verifier_model))
        for plan in plans[:args.take]:
            try:
                result = runner.run(plan, knowledge, budget, stop_after_verified=args.stop_after_verified)
                results.append(result)
                print(canonical({"scene": plan.scene_id, "status": result["status"], "reused": result.get("reused"),
                                 "calls": result["usage"]["calls"]}), flush=True)
                if result["status"] == "verified":
                    break
            except RuntimeFault as exc:
                error = str(exc)
                print(canonical({"scene": plan.scene_id, "error": error}), flush=True)
                break
        store.project()
    else:
        from app.scene_runtime.contracts import validate_plan
        validate_plan(plans[0], world, knowledge)
    scenes = store.export(world.book_id)
    for scene in scenes:
        (args.out / (scene["scene_id"] + ".md")).write_text(scene["text"] + "\n", encoding="utf-8")
    (args.out / "连续场景.md").write_text("\n\n".join(f"## {s['scene_id']}\n\n{s['text']}" for s in scenes), encoding="utf-8")
    report = {"results": results, "audit": store.audit(world.book_id), "error": error,
              "scenes": scenes, "world": store.snapshot(world.book_id).model_dump(),
              "literary_quality": "not_evaluated", "cost": None}
    write_json(args.out / "report.json", report)
    print(canonical({"audit": report["audit"], "error": error, "output": str(args.out)}), flush=True)
    return 1 if error or not report["audit"]["ok"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeFault as exc:
        print(canonical({"error": str(exc)}), flush=True)
        raise SystemExit(2)
