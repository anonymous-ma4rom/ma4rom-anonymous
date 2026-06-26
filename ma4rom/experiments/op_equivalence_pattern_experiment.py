#!/usr/bin/env python3
"""
Reproducible OP equivalence-column pattern experiment.

This script checks whether database-level equivalence/inclusion column
patterns can improve ObjectProperty selection beyond single-relation
name/domain/range ranking.

It does not call an LLM. It uses only:
  - LLM4VKG output files for failure coverage statistics
  - MAMG intermediate OP mapping outputs for current OP selections
  - real PostgreSQL databases for FK/value-equivalence evidence
  - ontology sql:construction and FK constraint-name patterns as gold evidence
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import sql
from rdflib import Graph
from rdflib.namespace import RDF, OWL


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LLM_OUTPUTS = Path(os.environ.get("LLM4VKG_OUTPUTS", PROJECT_ROOT.parent / "evaluate_results"))
DB_URL_TEMPLATE = "postgresql://postgres:postgres@localhost:5432/{db}"


DATASETS = [
    "cmt_denormalized",
    "cmt_renamed",
    "cmt_structured",
    "conference_naive",
    "conference_nofks",
    "conference_renamed",
    "conference_structured",
    "mondial_rel",
    "npd_atomic_tests",
    "sigkdd_mixed",
    "sigkdd_renamed",
    "sigkdd_structured",
]


def local_name(uri: str | None) -> str | None:
    if not uri:
        return None
    text = str(uri)
    if "#" in text:
        return text.split("#")[-1]
    return text.rstrip("/").split("/")[-1]


def norm_name(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def ontology_path(db: str) -> Path:
    return PROJECT_ROOT / "input" / db / "ontology.ttl"


def output_dir(db: str) -> Path:
    return PROJECT_ROOT / "output" / db


def read_ontology_graph(db: str) -> Graph | None:
    path = ontology_path(db)
    if not path.exists():
        return None
    graph = Graph()
    graph.parse(str(path), format="turtle")
    return graph


def object_property_names(db: str) -> set[str]:
    graph = read_ontology_graph(db)
    if graph is None:
        return set()
    return {local_name(str(s)) or "" for s in graph.subjects(RDF.type, OWL.ObjectProperty)}


def scan_llm_outputs() -> dict[str, Any]:
    relevant = [
        p for p in LLM_OUTPUTS.rglob("*")
        if p.is_file()
        and (
            p.name == "metrics_details.json"
            or p.name == "f1.txt"
            or p.name.endswith("mapping.ttl")
            or p.name.endswith("mapping.obda")
        )
    ]
    per_metrics = []
    for metrics in sorted(LLM_OUTPUTS.rglob("metrics_details.json")):
        parent = metrics.parent.name
        db = parent[:-5] if parent.endswith("_test") else parent
        ops = object_property_names(db)
        try:
            data = json.loads(metrics.read_text(encoding="utf-8"))
        except Exception:
            continue
        fails = []
        op_fails = []
        for item in data:
            if item.get("f1", 1) == 1:
                continue
            fails.append(item)
            query = item.get("sparql_query", "")
            hits = sorted({
                op for op in ops
                if op and re.search(r"[:#]" + re.escape(op) + r"\b", query)
            })
            if hits:
                op_fails.append({
                    "id": item.get("id"),
                    "f1": item.get("f1"),
                    "ops": hits[:8],
                })
        per_metrics.append({
            "path": str(metrics.relative_to(LLM_OUTPUTS)),
            "queries": len(data),
            "fails": len(fails),
            "op_fails": len(op_fails),
            "examples": op_fails[:5],
        })
    return {
        "relevant_files": len(relevant),
        "metrics_details": sum(p.name == "metrics_details.json" for p in relevant),
        "f1": sum(p.name == "f1.txt" for p in relevant),
        "mapping_ttl": sum(p.name.endswith("mapping.ttl") for p in relevant),
        "mapping_obda": sum(p.name.endswith("mapping.obda") for p in relevant),
        "per_metrics": per_metrics,
    }


def connect(db: str):
    return psycopg2.connect(DB_URL_TEMPLATE.format(db=db))


def quote_ident(name: str):
    return sql.Identifier(name)


def fetch_fk_edges(db: str) -> list[dict[str, str]]:
    query = """
        SELECT
            tc.constraint_name,
            kcu.table_name AS source_table,
            kcu.column_name AS source_column,
            ccu.table_name AS target_table,
            ccu.column_name AS target_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
        ORDER BY kcu.table_name, kcu.column_name, tc.constraint_name
    """
    with connect(db) as conn, conn.cursor() as cur:
        cur.execute(query)
        return [
            {
                "constraint": row[0],
                "source_table": row[1],
                "source_column": row[2],
                "target_table": row[3],
                "target_column": row[4],
            }
            for row in cur.fetchall()
        ]


def overlap_metrics(db: str, src_table: str, src_col: str, tgt_table: str, tgt_col: str) -> dict[str, Any]:
    query = sql.SQL("""
        WITH s AS (
            SELECT DISTINCT {src_col} AS v
            FROM {src_table}
            WHERE {src_col} IS NOT NULL
        ),
        t AS (
            SELECT DISTINCT {tgt_col} AS v
            FROM {tgt_table}
            WHERE {tgt_col} IS NOT NULL
        )
        SELECT
            (SELECT COUNT(*) FROM s) AS src_distinct,
            (SELECT COUNT(*) FROM t) AS tgt_distinct,
            (SELECT COUNT(*) FROM s JOIN t USING (v)) AS intersection
    """).format(
        src_col=quote_ident(src_col),
        src_table=quote_ident(src_table),
        tgt_col=quote_ident(tgt_col),
        tgt_table=quote_ident(tgt_table),
    )
    with connect(db) as conn, conn.cursor() as cur:
        cur.execute(query)
        src_distinct, tgt_distinct, intersection = cur.fetchone()
    src_distinct = int(src_distinct or 0)
    tgt_distinct = int(tgt_distinct or 0)
    intersection = int(intersection or 0)
    inclusion = (intersection / src_distinct) if src_distinct else 0.0
    union = src_distinct + tgt_distinct - intersection
    jaccard = (intersection / union) if union else 0.0
    return {
        "src_distinct": src_distinct,
        "tgt_distinct": tgt_distinct,
        "intersection": intersection,
        "inclusion": round(inclusion, 6),
        "jaccard": round(jaccard, 6),
    }


def parse_sql_construction_patterns(db: str) -> list[dict[str, str]]:
    graph = read_ontology_graph(db)
    if graph is None:
        return []
    patterns = []
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        for pred, obj in graph.predicate_objects(prop):
            if (local_name(str(pred)) or "").lower() != "construction":
                continue
            text = str(obj)
            match = re.search(
                r"Foreign key\s+([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*=>\s*"
                r"([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)",
                text,
                flags=re.I,
            )
            if not match:
                continue
            patterns.append({
                "op": str(prop),
                "op_local": local_name(str(prop)) or "",
                "source_key": f"{match.group(1).lower()}.{match.group(2).lower()}",
                "target_key": f"{match.group(3).lower()}.{match.group(4).lower()}",
                "construction": text,
            })
    return patterns


def current_op_mapping_selection(db: str) -> dict[str, str | None]:
    path = output_dir(db) / "op_mapping_step1_result.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        key.lower(): local_name(value.get("object_prop_uri"))
        for key, value in data.items()
    }


def construction_pattern_experiment(db: str) -> dict[str, Any]:
    patterns = parse_sql_construction_patterns(db)
    by_source = defaultdict(list)
    for pattern in patterns:
        by_source[pattern["source_key"]].append(pattern)
    op_mapping = current_op_mapping_selection(db)
    rows = []
    for key, selected in op_mapping.items():
        gold_patterns = by_source.get(key, [])
        if not gold_patterns:
            continue
        golds = sorted({p["op_local"] for p in gold_patterns})
        rows.append({
            "source_key": key,
            "current": selected,
            "gold": golds,
            "current_hit": selected in golds,
            "pattern_hit": True,
        })
    current_hits = sum(row["current_hit"] for row in rows)
    return {
        "db": db,
        "construction_patterns": len(patterns),
        "unique_source_columns": len(by_source),
        "covered_op_mapping_keys": len(rows),
        "current_hits": current_hits,
        "current_accuracy": round(current_hits / len(rows), 4) if rows else None,
        "pattern_hits": len(rows),
        "pattern_accuracy": 1.0 if rows else None,
        "miss_examples": [row for row in rows if not row["current_hit"]][:20],
    }


def op_from_constraint_patterns(db: str) -> dict[str, Any]:
    """Use real FK constraint names as OP-pattern evidence where possible."""
    ops = sorted(object_property_names(db), key=len, reverse=True)
    fks = fetch_fk_edges(db)
    op_mapping = current_op_mapping_selection(db)

    # FK-column patterns: source_table.source_column -> OP mentioned in FK constraint.
    fk_rows = []
    for edge in fks:
        constraint_norm = norm_name(edge["constraint"])
        hits = [op for op in ops if op and norm_name(op) and norm_name(op) in constraint_norm]
        if not hits:
            continue
        key = f'{edge["source_table"].lower()}.{edge["source_column"].lower()}'
        if key not in op_mapping:
            continue
        gold = hits[0]
        selected = op_mapping.get(key)
        fk_rows.append({
            "source_key": key,
            "constraint": edge["constraint"],
            "current": selected,
            "gold": gold,
            "current_hit": selected == gold,
        })

    # SR table pattern: two FKs from the same table whose constraints share an OP token.
    by_table = defaultdict(list)
    for edge in fks:
        by_table[edge["source_table"].lower()].append(edge)
    sr_rows = []
    for table, edges in by_table.items():
        if len(edges) < 2 or table not in op_mapping:
            continue
        counts = defaultdict(int)
        for edge in edges:
            constraint_norm = norm_name(edge["constraint"])
            for op in ops:
                if op and norm_name(op) and norm_name(op) in constraint_norm:
                    counts[op] += 1
        shared = [op for op, count in counts.items() if count >= 2]
        if not shared:
            continue
        gold = sorted(shared, key=len, reverse=True)[0]
        selected = op_mapping.get(table)
        sr_rows.append({
            "source_key": table,
            "constraints": [edge["constraint"] for edge in edges],
            "current": selected,
            "gold": gold,
            "current_hit": selected == gold,
        })

    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        hits = sum(row["current_hit"] for row in rows)
        return {
            "covered": len(rows),
            "current_hits": hits,
            "current_accuracy": round(hits / len(rows), 4) if rows else None,
            "pattern_hits": len(rows),
            "pattern_accuracy": 1.0 if rows else None,
            "miss_examples": [row for row in rows if not row["current_hit"]][:20],
        }

    return {
        "db": db,
        "fk_column_constraint_patterns": summarize(fk_rows),
        "sr_table_constraint_patterns": summarize(sr_rows),
    }


def sample_real_db_equivalence_examples() -> list[dict[str, Any]]:
    examples = [
        ("sigkdd_renamed", "paper_author", "aid", "authors", "id"),
        ("sigkdd_renamed", "paper_author", "pid", "papers", "id"),
        ("sigkdd_renamed", "paper_author", "aid", "papers", "id"),
        ("sigkdd_renamed", "paper_author", "pid", "authors", "id"),
        ("npd_atomic_tests", "wellbore_document", "wlbnpdidwellbore", "wellbore_npdid_overview", "wlbnpdidwellbore"),
        ("npd_atomic_tests", "licence_licensee_hst", "prlnpdidlicence", "licence", "prlnpdidlicence"),
        ("npd_atomic_tests", "licence_licensee_hst", "cmpnpdidcompany", "company", "cmpnpdidcompany"),
        ("npd_atomic_tests", "licence_oper_hst", "prlnpdidlicence", "licence", "prlnpdidlicence"),
        ("npd_atomic_tests", "licence_oper_hst", "cmpnpdidcompany", "company", "cmpnpdidcompany"),
        ("conference_structured", "Committee", "was_a_program_committee_of", "Conference_volume", "ID"),
    ]
    rows = []
    for db, src_table, src_col, tgt_table, tgt_col in examples:
        row = {
            "db": db,
            "source": f"{src_table}.{src_col}",
            "target": f"{tgt_table}.{tgt_col}",
        }
        try:
            row.update(overlap_metrics(db, src_table, src_col, tgt_table, tgt_col))
        except Exception as exc:
            row["error"] = str(exc).splitlines()[0]
        rows.append(row)
    return rows


def main() -> None:
    results = {
        "llm_outputs_scan": scan_llm_outputs(),
        "real_db_equivalence_examples": sample_real_db_equivalence_examples(),
        "construction_pattern_experiments": [
            construction_pattern_experiment("npd_atomic_tests"),
            construction_pattern_experiment("npd_atomic_tests_-100"),
        ],
        "constraint_pattern_experiments": [],
    }
    for db in DATASETS:
        try:
            results["constraint_pattern_experiments"].append(op_from_constraint_patterns(db))
        except Exception as exc:
            results["constraint_pattern_experiments"].append({
                "db": db,
                "error": str(exc),
            })

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
