#!/usr/bin/env python3
"""
Evaluate ObjectProperty choices against RODI qpair-derived gold.

RODI does not ship a simple OP gold CSV. Its gold signal is the qpair file:
each qpair contains the reference SQL and the equivalent SPARQL.  This script
uses the conservative subset where categories include path-1 and the SQL has a
single join.  Those cases map cleanly to one FK/relation edge and one ontology
ObjectProperty.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from rdflib import Graph, RDF, RDFS, OWL


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RODI_ROOT = Path("/Users/douer_lucky/Documents/科研/rodi-master")

JOIN_RE = re.compile(
    r'JOIN\s+"([^"]+)"\s+ON\s+"([^"]+)"\."([^"]+)"\s*=\s*"([^"]+)"\."([^"]+)"',
    re.IGNORECASE | re.DOTALL,
)
SPARQL_PROP_RE = re.compile(r":([A-Za-z_][A-Za-z0-9_-]*)\b")


def local_name(uri: str | None) -> str:
    if not uri:
        return ""
    text = str(uri)
    if "#" in text:
        return text.rsplit("#", 1)[-1]
    return text.rstrip("/").rsplit("/", 1)[-1]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_qpair_text(text: str) -> str:
    return text.replace("\\n\\", "\n").replace("\\\n", "\n")


def load_object_properties(ontology_path: Path) -> tuple[set[str], dict[str, set[str]]]:
    graph = Graph()
    graph.parse(str(ontology_path), format="turtle")
    op_locals = {local_name(str(p)) for p in graph.subjects(RDF.type, OWL.ObjectProperty)}
    inverse_locals: dict[str, set[str]] = {name: set() for name in op_locals}
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        prop_local = local_name(str(prop))
        for inv in graph.objects(prop, OWL.inverseOf):
            inv_local = local_name(str(inv))
            inverse_locals.setdefault(prop_local, set()).add(inv_local)
            inverse_locals.setdefault(inv_local, set()).add(prop_local)
        for subj in graph.subjects(OWL.inverseOf, prop):
            subj_local = local_name(str(subj))
            inverse_locals.setdefault(prop_local, set()).add(subj_local)
            inverse_locals.setdefault(subj_local, set()).add(prop_local)
    return op_locals, inverse_locals


def extract_qpair_gold(database: str, op_mapping_keys: set[str], rodi_root: Path) -> list[dict[str, Any]]:
    qdir = rodi_root / "data" / database / "queries"
    ontology_path = PROJECT_ROOT / "input" / database / "ontology.ttl"
    if not qdir.exists():
        raise FileNotFoundError(f"RODI qpair directory not found: {qdir}")
    op_locals, inverse_locals = load_object_properties(ontology_path)

    rows = []
    for path in sorted(qdir.glob("*.qpair")):
        text = normalize_qpair_text(path.read_text(encoding="utf-8", errors="ignore"))
        if "path-1" not in text:
            continue
        joins = JOIN_RE.findall(text)
        if len(joins) != 1:
            continue

        _, left_table, left_col, right_table, right_col = joins[0]
        left_key = f"{left_table}.{left_col}"
        right_key = f"{right_table}.{right_col}"
        if left_key in op_mapping_keys:
            relation_key = left_key
            source = left_key
            target = right_key
        elif right_key in op_mapping_keys:
            relation_key = right_key
            source = right_key
            target = left_key
        else:
            continue

        props = [p for p in SPARQL_PROP_RE.findall(text) if p in op_locals]
        if not props:
            continue

        gold = set(props)
        for prop in props:
            gold.update(inverse_locals.get(prop, set()))

        rows.append({
            "qpair": path.name,
            "relation_key": relation_key,
            "source": source,
            "target": target,
            "sparql_props": sorted(set(props)),
            "gold_ops_with_inverse": sorted(gold),
        })
    return rows


def evaluate(database: str, prediction_json: Path | None, rodi_root: Path) -> dict[str, Any]:
    output_dir = PROJECT_ROOT / "output" / database
    op_mapping = read_json(output_dir / "op_mapping_step1_result.json")
    op_mapping_keys = set(op_mapping)
    gold_rows = extract_qpair_gold(database, op_mapping_keys, rodi_root)

    prediction_by_key: dict[str, dict[str, Any]] = {}
    if prediction_json:
        pred_data = read_json(prediction_json)
        prediction_by_key = {row["relation_key"].lower(): row for row in pred_data.get("results", [])}

    details = []
    current_correct = 0
    new_correct = 0
    safe_correct = 0
    new_answered = 0
    current_covered = 0
    new_covered = 0
    for gold in gold_rows:
        key = gold["relation_key"]
        gold_ops = set(gold["gold_ops_with_inverse"])
        current_uri = op_mapping.get(key, {}).get("object_prop_uri") or ""
        current_local = local_name(current_uri)
        current_ok = current_local in gold_ops
        current_correct += int(current_ok)
        current_covered += int(bool(current_local))

        pred_row = prediction_by_key.get(key.lower(), {})
        new_uri = pred_row.get("llm_selected_uri") or ""
        new_local = local_name(new_uri)
        new_ok = new_local in gold_ops
        safe_local = new_local or current_local
        safe_ok = safe_local in gold_ops
        if prediction_json:
            new_correct += int(new_ok)
            safe_correct += int(safe_ok)
            new_answered += int(bool(new_local))
            new_covered += int(bool(new_local))

        details.append({
            **gold,
            "current_op": current_local,
            "current_correct": current_ok,
            "new_op": new_local,
            "new_correct": new_ok if prediction_json else None,
            "safe_rerank_op": safe_local if prediction_json else None,
            "safe_rerank_correct": safe_ok if prediction_json else None,
            "prediction_error": pred_row.get("error"),
        })

    total = len(gold_rows)
    summary = {
        "database": database,
        "gold_source": "RODI qpair path-1 single-join subset",
        "gold_total": total,
        "current_correct": current_correct,
        "current_accuracy": current_correct / total if total else None,
        "current_coverage": current_covered / total if total else None,
    }
    if prediction_json:
        summary.update({
            "prediction_json": str(prediction_json),
            "new_correct": new_correct,
            "new_accuracy": new_correct / total if total else None,
            "op_module_correct": new_correct,
            "op_module_accuracy": new_correct / total if total else None,
            "new_answered": new_answered,
            "new_coverage": new_covered / total if total else None,
            "safe_rerank_correct": safe_correct,
            "safe_rerank_accuracy": safe_correct / total if total else None,
            "delta_accuracy": (new_correct - current_correct) / total if total else None,
            "safe_rerank_delta_accuracy": (safe_correct - current_correct) / total if total else None,
        })

    return {"summary": summary, "details": details}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--prediction-json")
    parser.add_argument("--rodi-root", default=str(DEFAULT_RODI_ROOT))
    parser.add_argument("--output-json")
    args = parser.parse_args()

    pred_path = Path(args.prediction_json) if args.prediction_json else None
    result = evaluate(args.database, pred_path, Path(args.rodi_root))
    out_path = Path(args.output_json) if args.output_json else PROJECT_ROOT / "output" / args.database / "op_accuracy_from_rodi_qpairs.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"Output: {out_path}")
    print("\nWrong or changed details:")
    for row in result["details"]:
        if not row["current_correct"] or (pred_path and row["current_op"] != row["new_op"]):
            print(
                f"{row['relation_key']}: gold={row['gold_ops_with_inverse']} "
                f"current={row['current_op']}({row['current_correct']}) "
                f"new={row['new_op']}({row['new_correct']}) "
                f"safe={row['safe_rerank_op']}({row['safe_rerank_correct']}) "
                f"qpair={row['qpair']}"
            )


if __name__ == "__main__":
    main()
