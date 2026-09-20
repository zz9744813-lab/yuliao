"""Read-only adapter for existing LG strategy metadata, excluding reference prose."""
from pathlib import Path
import json
import sqlite3

from .contracts import KnowledgePackage, RuntimeFault, Technique, digest


def genome_package(db_path: Path, book_id: str, strategy_ids: list[str]) -> KnowledgePackage:
    if not strategy_ids or len(strategy_ids) > 3 or len(set(strategy_ids)) != len(strategy_ids):
        raise RuntimeFault("select_one_to_three_unique_strategies")
    techniques = []
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        for strategy_id in strategy_ids:
            row = db.execute("SELECT name,conditions,recommended,avoid,version FROM expression_strategies WHERE id=?", (strategy_id,)).fetchone()
            if row is None:
                raise RuntimeFault("strategy_not_found")
            name, conditions, recommended, avoid, version = row
            try:
                conditions, recommended, avoid = map(json.loads, (conditions, recommended, avoid))
                if not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in (conditions, recommended, avoid)):
                    raise ValueError("invalid strategy fields")
            except (ValueError, TypeError) as exc:
                raise RuntimeFault("invalid_strategy_metadata") from exc
            fingerprint = digest([strategy_id, *row])
            techniques.append(Technique(id=strategy_id, operation=name + "：" + "；".join(recommended) + "。避免：" + "；".join(avoid),
                conditions=conditions, exceptions=["条件不匹配时不采用；不得改变事实或人物意图。", "必要的明确说明、心理过程和说话人归属仍应保留。"],
                source_refs=[f"language-genome:expression_strategies/{strategy_id}@{version}#sha256={fingerprint}"],
                evidence_status="hypothesis"))
    return KnowledgePackage(package_id="genome-" + digest([t.model_dump() for t in techniques])[:20],
                            book_id=book_id, source_kind="genome_snapshot", techniques=techniques)
