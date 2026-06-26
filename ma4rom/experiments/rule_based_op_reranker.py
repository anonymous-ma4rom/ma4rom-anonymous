#!/usr/bin/env python3
"""
Rule-based ObjectProperty reranking with schema-matching evidence.

This is the no-ML baseline:
  - uses FK/enriched-schema pruned column pairs
  - uses Jaccard/IND/COMA as connection evidence
  - reranks OP mapping candidates by fixed interpretable scores
  - evaluates when ontology sql:construction gold is available

It does not modify mapping files.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

from rdflib import Graph


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONSTRUCTION_RE = re.compile(
    r"Foreign\s+key\s+([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*=>\s*([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)",
    re.IGNORECASE,
)


def local_name(uri: str | None) -> str:
    if not uri:
        return ""
    text = str(uri)
    if "#" in text:
        return text.rsplit("#", 1)[-1]
    return text.rstrip("/").rsplit("/", 1)[-1]


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def relation_key(table: str, column: str) -> str:
    return f"{(table or '').lower()}.{(column or '').lower()}"


def edge_key(source_table: str, source_column: str, target_table: str, target_column: str) -> str:
    return ".".join([norm(source_table), norm(source_column), norm(target_table), norm(target_column)])


def split_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    parts = re.split(r"[^A-Za-z0-9]+", spaced)
    tokens = {p.lower() for p in parts if p}
    compact = norm(text)
    for marker in [
        "licensee", "operator", "reserves", "reserve", "status", "transfer",
        "task", "owner", "wellbore", "field", "licence", "company", "baa",
        "city", "country", "province", "capital", "border", "located",
        "island", "river", "lake", "sea", "mountain", "member", "source",
        "estuary", "organization", "flows", "through", "airport",
    ]:
        if marker in compact:
            tokens.add(marker)
    return tokens


def token_jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def construction_predicate(graph: Graph):
    for predicate in set(graph.predicates()):
        if local_name(str(predicate)) == "construction":
            return predicate
    return None


def parse_gold_constructions(ontology_path: Path) -> dict[str, set[str]]:
    if not ontology_path.exists():
        return {}
    graph = Graph()
    graph.parse(str(ontology_path), format="turtle")
    pred = construction_predicate(graph)
    if pred is None:
        return {}
    gold: dict[str, set[str]] = {}
    for subject, _, obj in graph.triples((None, pred, None)):
        for match in CONSTRUCTION_RE.finditer(str(obj)):
            src_table, src_col, tgt_table, tgt_col = match.groups()
            key = edge_key(src_table, src_col, tgt_table, tgt_col)
            gold.setdefault(key, set()).add(str(subject))
    return gold


def schema_match_by_relation(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for row in rows:
        key = relation_key(row["source_table"], row["source_column"])
        prev = out.get(key)
        if prev is None or float(row.get("source_in_target") or 0) > float(prev.get("source_in_target") or 0):
            out[key] = row
    return out


def candidate_rule_score(match_row: dict[str, Any], candidate: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    op_local = candidate.get("local_name") or local_name(candidate.get("uri") or "")
    table_tokens = split_tokens(match_row["source_table"])
    column_tokens = split_tokens(match_row["source_column"])
    target_tokens = split_tokens(match_row["target_table"]) | split_tokens(match_row["target_column"])
    op_tokens = split_tokens(op_local)

    name_score = float(candidate.get("name_score") or 0.0)
    domain_score = float(candidate.get("domain_score") or 0.0)
    range_score = float(candidate.get("range_score") or 0.0)
    table_role = token_jaccard(table_tokens, op_tokens)
    column_role = token_jaccard(column_tokens, op_tokens)
    target_role = token_jaccard(target_tokens, op_tokens)
    manual_jaccard = float(match_row.get("manual_jaccard") or 0.0)
    source_in_target = float(match_row.get("source_in_target") or 0.0)
    target_in_source = float(match_row.get("target_in_source") or 0.0)
    valentine_coma = float(match_row.get("valentine_coma_score") or 0.0)

    score = (
        weights["name"] * name_score
        + weights["domain"] * domain_score
        + weights["range"] * range_score
        + weights["table_role"] * table_role
        + weights["column_role"] * column_role
        + weights["target_role"] * target_role
        + weights["jaccard"] * manual_jaccard
        + weights["inclusion"] * source_in_target
        + weights["reverse_inclusion"] * target_in_source
        + weights["coma"] * valentine_coma
    )
    return {
        "candidate_op_uri": candidate.get("uri") or "",
        "candidate_op_local_name": op_local,
        "rule_score": score,
        "name_score": name_score,
        "domain_score": domain_score,
        "range_score": range_score,
        "table_role_score": table_role,
        "column_role_score": column_role,
        "target_role_score": target_role,
        "manual_jaccard": manual_jaccard,
        "source_in_target": source_in_target,
        "target_in_source": target_in_source,
        "valentine_coma_score": valentine_coma,
    }


def rerank_database(database: str, weights: dict[str, float]) -> dict[str, Any]:
    output_dir = PROJECT_ROOT / "output" / database
    schema_rows = read_json(output_dir / "schema_matching_valentine_fk.json")
    op_mapping_step1 = read_json(output_dir / "op_mapping_step1_result.json")
    gold_by_edge = parse_gold_constructions(PROJECT_ROOT / "input" / database / "ontology.ttl")
    match_by_rel = schema_match_by_relation(schema_rows)

    summaries = []
    candidate_rows = []
    eval_total = 0
    current_correct = 0
    rule_correct = 0

    for rel_key, entry in sorted(op_mapping_step1.items()):
        match_row = match_by_rel.get(rel_key.lower())
        if not match_row:
            continue
        candidates = entry.get("candidates_used") or []
        if not candidates:
            continue
        scored = [candidate_rule_score(match_row, cand, weights) for cand in candidates]
        best = max(scored, key=lambda row: row["rule_score"])
        current_uri = entry.get("object_prop_uri") or ""
        gold_key = edge_key(
            match_row["source_table"],
            match_row["source_column"],
            match_row["target_table"],
            match_row["target_column"],
        )
        gold_uris = gold_by_edge.get(gold_key, set())
        current_is_correct = current_uri in gold_uris if gold_uris else None
        rule_is_correct = best["candidate_op_uri"] in gold_uris if gold_uris else None
        if gold_uris:
            eval_total += 1
            current_correct += int(bool(current_is_correct))
            rule_correct += int(bool(rule_is_correct))

        summary = {
            "relation_key": rel_key,
            "source": f"{match_row['source_table']}.{match_row['source_column']}",
            "target": f"{match_row['target_table']}.{match_row['target_column']}",
            "current_op": local_name(current_uri),
            "rule_op": best["candidate_op_local_name"],
            "changed": current_uri != best["candidate_op_uri"],
            "gold_ops": "|".join(sorted(local_name(uri) for uri in gold_uris)),
            "current_correct": current_is_correct,
            "rule_correct": rule_is_correct,
            **{k: best[k] for k in [
                "rule_score",
                "name_score",
                "domain_score",
                "range_score",
                "table_role_score",
                "manual_jaccard",
                "source_in_target",
                "valentine_coma_score",
            ]},
        }
        summaries.append(summary)
        for row in scored:
            candidate_rows.append({"relation_key": rel_key, **row})

    result = {
        "database": database,
        "weights": weights,
        "relations": len(summaries),
        "changed_top1": sum(1 for row in summaries if row["changed"]),
        "eval_total_with_construction_gold": eval_total,
        "current_accuracy": current_correct / eval_total if eval_total else None,
        "rule_accuracy": rule_correct / eval_total if eval_total else None,
        "summary": summaries,
        "candidate_rows": candidate_rows,
    }
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--output-prefix", default="rule_based_op_rerank")
    parser.add_argument("--w-name", type=float, default=0.25)
    parser.add_argument("--w-domain", type=float, default=0.15)
    parser.add_argument("--w-range", type=float, default=0.20)
    parser.add_argument("--w-table-role", type=float, default=0.20)
    parser.add_argument("--w-column-role", type=float, default=0.05)
    parser.add_argument("--w-target-role", type=float, default=0.05)
    parser.add_argument("--w-jaccard", type=float, default=0.03)
    parser.add_argument("--w-inclusion", type=float, default=0.03)
    parser.add_argument("--w-reverse-inclusion", type=float, default=0.02)
    parser.add_argument("--w-coma", type=float, default=0.02)
    args = parser.parse_args()

    weights = {
        "name": args.w_name,
        "domain": args.w_domain,
        "range": args.w_range,
        "table_role": args.w_table_role,
        "column_role": args.w_column_role,
        "target_role": args.w_target_role,
        "jaccard": args.w_jaccard,
        "inclusion": args.w_inclusion,
        "reverse_inclusion": args.w_reverse_inclusion,
        "coma": args.w_coma,
    }
    result = rerank_database(args.database, weights)
    output_dir = PROJECT_ROOT / "output" / args.database
    json_path = output_dir / f"{args.output_prefix}.json"
    csv_path = output_dir / f"{args.output_prefix}.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, result["summary"])

    print("Done.")
    print(f"  database: {args.database}")
    print(f"  relations reranked: {result['relations']}")
    print(f"  changed top1: {result['changed_top1']}")
    print(f"  eval_total_with_construction_gold: {result['eval_total_with_construction_gold']}")
    print(f"  current_accuracy: {result['current_accuracy']}")
    print(f"  rule_accuracy: {result['rule_accuracy']}")
    print(f"  CSV:  {csv_path}")
    print(f"  JSON: {json_path}")
    print("\nChanged examples:")
    for row in [r for r in result["summary"] if r["changed"]][:20]:
        print(f"  {row['relation_key']}: {row['current_op']} -> {row['rule_op']} edge={row['source']} -> {row['target']}")


if __name__ == "__main__":
    main()
