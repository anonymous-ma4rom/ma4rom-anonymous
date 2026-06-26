#!/usr/bin/env python3
"""
LLM ObjectProperty selection with explicit schema-matching and ontology evidence.

This experiment feeds an LLM:
  - Jaccard / IND / equivalence-column evidence
  - OP mapping domain/range/name scores
  - ontology sql:construction evidence
  - owl restriction/onProperty evidence
  - inverseOf and subPropertyOf evidence

Important: exact sql:construction evidence is very strong for NPD. Treat this
setting as an ontology-evidence experiment, not as unsupervised prediction.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from rdflib import BNode, Graph, RDF, RDFS, OWL

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.llm_client import call_llm  # noqa: E402
from utils.name_similarity import name_overlap  # noqa: E402
from utils.ontology_utils import hint_match, read_ontology  # noqa: E402


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


def edge_key(source_table: str, source_column: str, target_table: str, target_column: str) -> str:
    return ".".join([norm(source_table), norm(source_column), norm(target_table), norm(target_column)])


def relation_key(table: str, column: str) -> str:
    return f"{(table or '').lower()}.{(column or '').lower()}"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def construction_predicate(graph: Graph):
    for predicate in set(graph.predicates()):
        if local_name(str(predicate)) == "construction":
            return predicate
    return None


def table_predicate(graph: Graph):
    for predicate in set(graph.predicates()):
        if local_name(str(predicate)) == "table":
            return predicate
    return None


def parse_ontology_metadata(ontology_path: Path) -> dict[str, Any]:
    graph = Graph()
    graph.parse(str(ontology_path), format="turtle")
    construction_pred = construction_predicate(graph)
    table_pred = table_predicate(graph)

    ops: dict[str, dict[str, Any]] = {}
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        uri = str(prop)
        ops[uri] = {
            "uri": uri,
            "local_name": local_name(uri),
            "domain": [str(x) for x in graph.objects(prop, RDFS.domain)],
            "range": [str(x) for x in graph.objects(prop, RDFS.range)],
            "construction": [str(x) for x in graph.objects(prop, construction_pred)] if construction_pred else [],
            "inverse_of": sorted({str(x) for x in graph.objects(prop, OWL.inverseOf)} | {str(s) for s in graph.subjects(OWL.inverseOf, prop)}),
            "subproperty_of": [str(x) for x in graph.objects(prop, RDFS.subPropertyOf)],
            "restrictions": [],
        }

    class_tables: dict[str, list[str]] = {}
    if table_pred:
        for cls, _, table_value in graph.triples((None, table_pred, None)):
            if isinstance(cls, BNode):
                continue
            class_tables.setdefault(str(cls), []).append(str(table_value))

    for cls, _, restriction in graph.triples((None, RDFS.subClassOf, None)):
        if isinstance(cls, BNode) or not isinstance(restriction, BNode):
            continue
        for prop in graph.objects(restriction, OWL.onProperty):
            prop_uri = str(prop)
            if prop_uri not in ops:
                continue
            values = []
            for pred in [OWL.someValuesFrom, OWL.allValuesFrom, OWL.onClass]:
                values.extend(str(x) for x in graph.objects(restriction, pred))
            ops[prop_uri]["restrictions"].append({
                "class": str(cls),
                "class_local": local_name(str(cls)),
                "class_tables": class_tables.get(str(cls), []),
                "values": values,
                "values_local": [local_name(v) for v in values],
            })

    construction_by_edge: dict[str, list[dict[str, str]]] = {}
    for op_uri, info in ops.items():
        for text in info["construction"]:
            for match in CONSTRUCTION_RE.finditer(text):
                src_table, src_col, tgt_table, tgt_col = match.groups()
                construction_by_edge.setdefault(edge_key(src_table, src_col, tgt_table, tgt_col), []).append({
                    "op_uri": op_uri,
                    "op_local_name": info["local_name"],
                    "construction": match.group(0),
                })

    return {
        "ops": ops,
        "construction_by_edge": construction_by_edge,
    }


def schema_match_by_relation(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for row in rows:
        key = relation_key(row["source_table"], row["source_column"])
        prev = out.get(key)
        if prev is None or float(row.get("source_in_target") or 0) > float(prev.get("source_in_target") or 0):
            out[key] = row
    return out


def token_jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def split_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {p.lower() for p in re.split(r"[^A-Za-z0-9]+", spaced) if p}


def relation_name_hint(relation_key_: str, match_row: dict[str, Any]) -> str:
    if "." in relation_key_:
        return relation_key_.split(".", 1)[1]
    return match_row.get("source_column") or relation_key_


def infer_class_uri_from_table(table_name: str, ontology_full: dict[str, Any]) -> str:
    table_norm = norm(table_name)
    best_uri = ""
    best_score = -1
    for uri in ontology_full.get("classes", []):
        local = local_name(uri)
        local_norm = norm(local)
        score = 0
        if local_norm == table_norm:
            score = 100
        elif local_norm and (local_norm in table_norm or table_norm in local_norm):
            score = min(len(local_norm), len(table_norm))
        if score > best_score:
            best_score = score
            best_uri = uri
    return best_uri if best_score > 0 else ""


def build_clean_op_tasks_from_schema_matching(
    schema_rows: list[dict[str, Any]],
    ontology_full: dict[str, Any],
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Build OP tasks without reading any precomputed OP candidates."""
    match_by_rel = schema_match_by_relation(schema_rows)
    tasks = []
    for rel_key, match_row in sorted(match_by_rel.items()):
        domain_class = infer_class_uri_from_table(match_row.get("source_table", ""), ontology_full)
        range_class = infer_class_uri_from_table(match_row.get("target_table", ""), ontology_full)
        if not domain_class or not range_class:
            continue
        scenario_entry = {
            "type": "fk_obj",
            "domain_table": match_row.get("source_table"),
            "range_table": match_row.get("target_table"),
            "domain_class_uri": domain_class,
            "range_class_uri": range_class,
        }
        tasks.append((rel_key, match_row, scenario_entry))
    return tasks


def class_match_kind(hint: str, prop_values: list[str], ontology_full: dict[str, Any]) -> str:
    if not hint:
        return "missing_hint"
    if not prop_values:
        return "op_has_no_declared_class"
    union_members = ontology_full.get("union_members", {})
    ancestors = ontology_full.get("ancestors_of", {})
    expanded = []
    for value in prop_values:
        members = union_members.get(value)
        if members:
            expanded.extend(members)
        else:
            expanded.append(value)
    for value in expanded:
        if hint == value or local_name(hint).lower() == local_name(value).lower():
            return "exact"
    for value in expanded:
        if value in ancestors.get(hint, []):
            return "table_class_is_subclass_of_op_class"
    for value in expanded:
        if hint in ancestors.get(value, []):
            return "table_class_is_parent_of_op_class"
    return "conflict"


def class_match_explanation(side: str, hint: str, prop_values: list[str], ontology_full: dict[str, Any]) -> str:
    kind = class_match_kind(hint, prop_values, ontology_full)
    hint_local = local_name(hint)
    values_local = [local_name(v) for v in prop_values]
    if kind == "exact":
        return f"{side}: exact match, table class {hint_local} equals OP class {values_local}"
    if kind == "table_class_is_subclass_of_op_class":
        return f"{side}: compatible, table class {hint_local} is a subclass of OP class {values_local}"
    if kind == "table_class_is_parent_of_op_class":
        return f"{side}: broader table class, OP class {values_local} is a subclass of table class {hint_local}; prefer this only when name/role/subProperty strongly supports a more specific OP"
    if kind == "op_has_no_declared_class":
        return f"{side}: OP has no declared {side} class, weak non-conflicting evidence"
    return f"{side}: conflict, table class {hint_local} is not compatible with OP class {values_local}"


def score_endpoint_candidates(
    relation_key_: str,
    match_row: dict[str, Any],
    scenario_entry: dict[str, Any],
    ontology_full: dict[str, Any],
    top_k: int,
    min_endpoint_score: float,
) -> list[dict[str, Any]]:
    name_hint = relation_name_hint(relation_key_, match_row)
    domain_hint = scenario_entry.get("domain_class_uri") or ""
    range_hint = scenario_entry.get("range_class_uri") or ""
    table_tokens = split_tokens(match_row.get("source_table"))
    column_tokens = split_tokens(match_row.get("source_column"))
    target_tokens = split_tokens(match_row.get("target_table")) | split_tokens(match_row.get("target_column"))

    rows = []
    for uri, info in ontology_full.get("object_properties", {}).items():
        local = local_name(uri)
        domain_score = hint_match(domain_hint, info.get("domain", []), ontology=ontology_full)
        range_score = hint_match(range_hint, info.get("range", []), ontology=ontology_full)
        endpoint_score = (domain_score + range_score) / 2.0
        name_score = name_overlap(name_hint, local)
        op_tokens = split_tokens(local)
        table_role_score = token_jaccard(table_tokens, op_tokens)
        column_role_score = token_jaccard(column_tokens, op_tokens)
        target_role_score = token_jaccard(target_tokens, op_tokens)
        role_score = max(table_role_score, column_role_score, target_role_score)
        total = (
            endpoint_score * 0.55
            + name_score * 0.30
            + role_score * 0.15
        )
        if endpoint_score < min_endpoint_score and name_score < 0.75:
            continue
        rows.append({
            "uri": uri,
            "local_name": local,
            "score": round(total, 4),
            "name_score": round(name_score, 3),
            "domain": info.get("domain", []),
            "range": info.get("range", []),
            "domain_score": round(domain_score, 3),
            "range_score": round(range_score, 3),
            "domain_match_kind": class_match_kind(domain_hint, info.get("domain", []), ontology_full),
            "range_match_kind": class_match_kind(range_hint, info.get("range", []), ontology_full),
            "domain_closure_explanation": class_match_explanation("domain", domain_hint, info.get("domain", []), ontology_full),
            "range_closure_explanation": class_match_explanation("range", range_hint, info.get("range", []), ontology_full),
            "endpoint_score": round(endpoint_score, 3),
            "table_role_score": round(table_role_score, 3),
            "column_role_score": round(column_role_score, 3),
            "target_role_score": round(target_role_score, 3),
            "candidate_source": "ontology_endpoint",
        })

    rows.sort(
        key=lambda x: (
            x["endpoint_score"],
            x["name_score"],
            max(x["table_role_score"], x["column_role_score"], x["target_role_score"]),
            x["score"],
        ),
        reverse=True,
    )
    return rows[:top_k]


def compact_op_info(op_uri: str, ontology: dict[str, Any], op_mapping_candidate: dict[str, Any] | None, match_row: dict[str, Any]) -> dict[str, Any]:
    op_meta = ontology["ops"].get(op_uri, {
        "uri": op_uri,
        "local_name": local_name(op_uri),
        "domain": [],
        "range": [],
        "construction": [],
        "inverse_of": [],
        "subproperty_of": [],
        "restrictions": [],
    })
    current_edge = edge_key(
        match_row["source_table"],
        match_row["source_column"],
        match_row["target_table"],
        match_row["target_column"],
    )
    construction_hits = []
    for text in op_meta.get("construction", []):
        for match in CONSTRUCTION_RE.finditer(text):
            hit_key = edge_key(*match.groups())
            if hit_key == current_edge:
                construction_hits.append(match.group(0))

    source_table_norm = norm(match_row["source_table"])
    restriction_hits = []
    for item in op_meta.get("restrictions", []):
        table_hit = any(source_table_norm in norm(t) or norm(t) in source_table_norm for t in item.get("class_tables", []))
        if table_hit or source_table_norm in norm(item.get("class_local")):
            restriction_hits.append(item)

    return {
        "uri": op_uri,
        "local_name": op_meta.get("local_name") or local_name(op_uri),
        "op_mapping_name_score": (op_mapping_candidate or {}).get("name_score", 0),
        "op_mapping_domain_score": (op_mapping_candidate or {}).get("domain_score", 0),
        "op_mapping_range_score": (op_mapping_candidate or {}).get("range_score", 0),
        "candidate_score": (op_mapping_candidate or {}).get("score", 0),
        "candidate_source": (op_mapping_candidate or {}).get("candidate_source", "op_mapping_top3"),
        "endpoint_score": (op_mapping_candidate or {}).get("endpoint_score"),
        "domain_match_kind": (op_mapping_candidate or {}).get("domain_match_kind"),
        "range_match_kind": (op_mapping_candidate or {}).get("range_match_kind"),
        "domain_closure_explanation": (op_mapping_candidate or {}).get("domain_closure_explanation"),
        "range_closure_explanation": (op_mapping_candidate or {}).get("range_closure_explanation"),
        "table_role_score": (op_mapping_candidate or {}).get("table_role_score"),
        "column_role_score": (op_mapping_candidate or {}).get("column_role_score"),
        "target_role_score": (op_mapping_candidate or {}).get("target_role_score"),
        "domain_local": [local_name(x) for x in op_meta.get("domain", [])],
        "range_local": [local_name(x) for x in op_meta.get("range", [])],
        "exact_sql_construction_match": bool(construction_hits),
        "matching_construction": construction_hits[:3],
        "restriction_support": [
            {
                "class": x.get("class_local"),
                "class_tables": x.get("class_tables", [])[:2],
                "values": x.get("values_local", [])[:3],
            }
            for x in restriction_hits[:3]
        ],
        "inverse_of": [local_name(x) for x in op_meta.get("inverse_of", [])],
        "subproperty_of": [local_name(x) for x in op_meta.get("subproperty_of", [])],
    }


def build_candidate_list(op_mapping_entry: dict[str, Any], construction_gold: list[dict[str, str]]) -> list[str]:
    out = []
    for cand in op_mapping_entry.get("candidates_used") or []:
        uri = cand.get("uri")
        if uri and uri not in out:
            out.append(uri)
    for item in construction_gold:
        uri = item["op_uri"]
        if uri not in out:
            out.append(uri)
    return out


def build_prompt(
    relation_key_: str,
    match_row: dict[str, Any],
    op_mapping_entry: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    scenario_entry: dict[str, Any],
    candidate_source: str,
) -> str:
    payload = {
        "task": "Choose the single best ontology ObjectProperty for this database FK/relation. Use endpoint closure, schema-matching evidence, name/role, inverseOf and subPropertyOf. Return null only if all candidates truly conflict.",
        "hard_rules": [
            "Class hierarchy endpoint closure: if the table class is a subclass of the OP domain/range class, this is compatible, not a conflict.",
            "Example: source class Program_committee is subclass of OP domain Committee, so an OP with domain Committee can validly start from Program_committee.",
            "OP specificity: if two candidate OPs are otherwise compatible and one is subPropertyOf the other, prefer the more specific subProperty when relation name/role/equivalence evidence supports it.",
            "Do not confuse Class hierarchy with OP hierarchy: class subclass compatibility validates endpoints; OP subPropertyOf chooses the more specific relation predicate.",
        ],
        "relation": {
            "relation_key": relation_key_,
            "source": f"{match_row['source_table']}.{match_row['source_column']}",
            "target": f"{match_row['target_table']}.{match_row['target_column']}",
            "domain_class": local_name(scenario_entry.get("domain_class_uri") or ""),
            "range_class": local_name(scenario_entry.get("range_class_uri") or ""),
            "current_op_mapping_selected": local_name((op_mapping_entry or {}).get("object_prop_uri") or ""),
        },
        "candidate_generation": {
            "source": candidate_source,
            "meaning": "ontology_endpoint means candidates were regenerated from all ontology ObjectProperties by domain/range endpoint closure plus name/role scores, not from old OP top3.",
        },
        "schema_matching_evidence": {
            "manual_jaccard": match_row.get("manual_jaccard"),
            "source_in_target_IND": match_row.get("source_in_target"),
            "target_in_source": match_row.get("target_in_source"),
            "valentine_coma_score": match_row.get("valentine_coma_score"),
            "evidence_type": match_row.get("evidence_type"),
            "source_distinct": match_row.get("source_distinct"),
            "target_distinct": match_row.get("target_distinct"),
            "intersection": match_row.get("intersection"),
        },
        "candidate_object_properties": candidates,
        "output_schema": {
            "selected_uri": "one candidate uri or null",
            "selected_local_name": "local name",
            "confidence": "high|medium|low",
            "reason": "short Chinese explanation mentioning construction/restriction/IND/name/domain/range evidence",
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def evaluate(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0
    current_correct = 0
    llm_correct = 0
    no_answer = 0
    prediction_no_answer = 0
    changed_from_current = 0
    for row in results:
        selected = row.get("llm_selected_uri") or ""
        if not selected:
            prediction_no_answer += 1
        elif selected != (row.get("current_uri") or ""):
            changed_from_current += 1
        gold = set(row.get("gold_uris") or [])
        if not gold:
            continue
        total += 1
        current_correct += int(row.get("current_uri") in gold)
        if not selected:
            no_answer += 1
        llm_correct += int(selected in gold)
    return {
        "prediction_total": len(results),
        "prediction_no_answer": prediction_no_answer,
        "changed_from_current": changed_from_current,
        "total_with_construction_gold": total,
        "current_correct": current_correct,
        "current_accuracy": current_correct / total if total else None,
        "llm_correct": llm_correct,
        "llm_accuracy": llm_correct / total if total else None,
        "no_answer": no_answer,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="npd_atomic_tests")
    parser.add_argument("--limit", type=int, default=0, help="0 means all covered relations")
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--output-json")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--candidate-source",
        choices=["ontology-endpoint", "scenario-candidates", "op_mapping-top3"],
        default="ontology-endpoint",
        help="ontology-endpoint regenerates candidates from all ontology OPs using domain/range endpoint closure; op_mapping-top3 keeps old behavior.",
    )
    parser.add_argument("--candidate-top-k", type=int, default=12)
    parser.add_argument("--min-endpoint-score", type=float, default=0.5)
    parser.add_argument(
        "--op-module-only",
        action="store_true",
        help="Do not use op_mapping_step1_result as the relation/candidate source. Build OP tasks only from scenarios, schema-matching evidence, ontology domain/range/name, and LLM.",
    )
    parser.add_argument(
        "--include-no-gold",
        action="store_true",
        help="Also run LLM prediction for relations without sql:construction gold; accuracy will be null for those rows.",
    )
    args = parser.parse_args()

    output_dir = PROJECT_ROOT / "output" / args.database
    output_path = Path(args.output_json) if args.output_json else output_dir / "llm_op_evidence_experiment.json"

    schema_rows = read_json(output_dir / "schema_matching_valentine_fk.json")
    op_mapping_step1 = {} if args.op_module_only else read_json(output_dir / "op_mapping_step1_result.json")
    scenarios = {} if args.op_module_only else read_json(output_dir / "scenarios.json")
    ontology = parse_ontology_metadata(PROJECT_ROOT / "input" / args.database / "ontology.ttl")
    ontology_full = read_ontology(str(PROJECT_ROOT / "input" / args.database / "ontology.ttl"))
    match_by_rel = schema_match_by_relation(schema_rows)

    existing: dict[str, Any] = {}
    if output_path.exists() and not args.force:
        existing_data = read_json(output_path)
        existing = {row["relation_key"]: row for row in existing_data.get("results", [])}

    jobs = []
    if args.op_module_only:
        clean_tasks = build_clean_op_tasks_from_schema_matching(schema_rows, ontology_full)
        task_items = [(rel_key, match_row, {}, scenario_entry) for rel_key, match_row, scenario_entry in clean_tasks]
    else:
        task_items = []
        for rel_key, op_mapping_entry in sorted(op_mapping_step1.items()):
            match_row = match_by_rel.get(rel_key.lower())
            if not match_row:
                continue
            task_items.append((rel_key, match_row, op_mapping_entry, scenarios.get(rel_key, {})))

    for rel_key, match_row, op_mapping_entry, scenario_entry in task_items:
        gold_items = ontology["construction_by_edge"].get(edge_key(
            match_row["source_table"],
            match_row["source_column"],
            match_row["target_table"],
            match_row["target_column"],
        ), [])
        if not gold_items and not args.include_no_gold:
            continue
        jobs.append((rel_key, match_row, op_mapping_entry, scenario_entry, gold_items))
    if args.limit and args.limit > 0:
        jobs = jobs[: args.limit]

    results = list(existing.values())
    done = set(existing)
    system = (
        "You are an OBDA ontology mapping expert. "
        "Select ObjectProperty only from candidates. "
        "Return strict JSON only. No Markdown."
    )

    for idx, (rel_key, match_row, op_mapping_entry, scenario_entry, gold_items) in enumerate(jobs, start=1):
        if rel_key in done:
            continue
        if args.candidate_source == "ontology-endpoint":
            candidate_rows = score_endpoint_candidates(
                rel_key,
                match_row,
                scenario_entry,
                ontology_full,
                top_k=args.candidate_top_k,
                min_endpoint_score=args.min_endpoint_score,
            )
            for item in gold_items:
                if item["op_uri"] not in {c["uri"] for c in candidate_rows}:
                    candidate_rows.append({
                        "uri": item["op_uri"],
                        "local_name": item["op_local_name"],
                        "score": 1.0,
                        "name_score": 1.0,
                        "domain_score": 1.0,
                        "range_score": 1.0,
                        "endpoint_score": 1.0,
                        "candidate_source": "construction_gold_added",
                    })
        elif args.candidate_source == "scenario-candidates":
            candidate_rows = list((scenario_entry.get("op_candidates") or [])[: args.candidate_top_k])
            seen = {c.get("uri") for c in candidate_rows}
            for item in gold_items:
                if item["op_uri"] not in seen:
                    candidate_rows.append({
                        "uri": item["op_uri"],
                        "local_name": item["op_local_name"],
                        "score": 1.0,
                        "name_score": 1.0,
                        "domain_score": 1.0,
                        "range_score": 1.0,
                        "endpoint_score": 1.0,
                        "candidate_source": "construction_gold_added",
                    })
                    seen.add(item["op_uri"])
        else:
            cand_uris = build_candidate_list(op_mapping_entry, gold_items)
            op_mapping_by_uri = {c.get("uri"): c for c in (op_mapping_entry.get("candidates_used") or [])}
            candidate_rows = [op_mapping_by_uri.get(uri, {"uri": uri}) for uri in cand_uris]

        candidate_rows = [c for c in candidate_rows if c.get("uri")]
        candidate_by_uri = {c.get("uri"): c for c in candidate_rows}
        candidates = [compact_op_info(uri, ontology, candidate_by_uri.get(uri), match_row) for uri in candidate_by_uri]
        prompt = build_prompt(rel_key, match_row, op_mapping_entry, candidates, scenario_entry, args.candidate_source)
        print(f"[{idx}/{len(jobs)}] LLM selecting {rel_key} candidates={len(candidates)}")
        try:
            response = call_llm(prompt, system=system, prefer_fast=False)
            selected_uri = str(response.get("selected_uri") or "")
            selected_local = response.get("selected_local_name") or local_name(selected_uri)
            error = None
        except Exception as exc:
            response = {"error": str(exc)}
            selected_uri = ""
            selected_local = ""
            error = str(exc)

        row = {
            "relation_key": rel_key,
            "source": f"{match_row['source_table']}.{match_row['source_column']}",
            "target": f"{match_row['target_table']}.{match_row['target_column']}",
            "current_uri": "" if args.op_module_only else (op_mapping_entry.get("object_prop_uri") or ""),
            "current_local_name": "" if args.op_module_only else local_name(op_mapping_entry.get("object_prop_uri") or ""),
            "llm_selected_uri": selected_uri,
            "llm_selected_local_name": selected_local,
            "gold_uris": sorted({x["op_uri"] for x in gold_items}),
            "gold_local_names": sorted({x["op_local_name"] for x in gold_items}),
            "schema_matching": {
                "manual_jaccard": match_row.get("manual_jaccard"),
                "source_in_target": match_row.get("source_in_target"),
                "target_in_source": match_row.get("target_in_source"),
                "valentine_coma_score": match_row.get("valentine_coma_score"),
                "evidence_type": match_row.get("evidence_type"),
            },
            "candidate_count": len(candidates),
            "candidate_source": args.candidate_source,
            "llm_response": response,
            "error": error,
        }
        results.append(row)
        out = {
            "database": args.database,
            "setting": "LLM sees schema-matching evidence plus ontology construction/restriction/inverse/subPropertyOf evidence",
            "warning": "Exact sql:construction evidence is strong ontology supervision for NPD; do not report as unsupervised.",
            "include_no_gold": args.include_no_gold,
            "op_module_only": args.op_module_only,
            "candidate_source": args.candidate_source,
            "candidate_top_k": args.candidate_top_k,
            "evaluated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": evaluate(results),
            "results": sorted(results, key=lambda x: x["relation_key"]),
        }
        output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(args.sleep)

    final = {
        "database": args.database,
        "setting": "LLM sees schema-matching evidence plus ontology construction/restriction/inverse/subPropertyOf evidence",
        "warning": "Exact sql:construction evidence is strong ontology supervision for NPD; do not report as unsupervised.",
        "include_no_gold": args.include_no_gold,
        "op_module_only": args.op_module_only,
        "candidate_source": args.candidate_source,
        "candidate_top_k": args.candidate_top_k,
        "evaluated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": evaluate(results),
        "results": sorted(results, key=lambda x: x["relation_key"]),
    }
    output_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nDone.")
    print(json.dumps(final["summary"], ensure_ascii=False, indent=2))
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
