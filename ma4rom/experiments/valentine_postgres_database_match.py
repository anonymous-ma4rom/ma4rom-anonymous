#!/usr/bin/env python3
"""
Batch schema matching evidence for a PostgreSQL database.

Default behavior:
  - read output/<database>/enriched_schema.json
  - use its FK edges as candidate pruning
  - compute Valentine COMA/Jaccard plus exact Jaccard/inclusion for each edge
  - write CSV + JSON under output/<database>/

This is an experiment entry point. It only reads PostgreSQL and existing
intermediate files; it does not modify the MAMG pipeline.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg2
from psycopg2 import sql
from valentine import valentine_match
from valentine.algorithms import Coma, JaccardDistanceMatcher


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres",
    "password": "postgres",
}


def quoted_table(schema_name: str, table_name: str) -> sql.SQL:
    return sql.SQL("{}.{}").format(sql.Identifier(schema_name), sql.Identifier(table_name))


def normalize_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_enriched_schema(database: str) -> dict[str, Any]:
    path = PROJECT_ROOT / "output" / database / "enriched_schema.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Run the MAMG pipeline first, or pass a database that has output/<db>/enriched_schema.json."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_fk_edge(source_table: str, fk: dict[str, Any]) -> dict[str, str] | None:
    source_column = fk.get("column")
    target_table = fk.get("references_table") or fk.get("ref_table")
    target_column = fk.get("references_column") or fk.get("ref_col")
    if not source_column or not target_table or not target_column:
        return None
    return {
        "source_table": source_table,
        "source_column": source_column,
        "target_table": target_table,
        "target_column": target_column,
        "fk_source": str(fk.get("source") or fk.get("constraint_name") or "schema"),
        "ind_score_from_pipeline": fk.get("ind_score"),
        "confidence_from_pipeline": fk.get("confidence"),
    }


def fk_edges_from_enriched_schema(enriched_schema: dict[str, Any]) -> list[dict[str, str]]:
    edges = []
    seen = set()
    for table_name, info in sorted(enriched_schema.items()):
        for fk in info.get("foreign_keys") or []:
            edge = normalize_fk_edge(table_name, fk)
            if edge is None:
                continue
            key = (
                edge["source_table"].lower(),
                edge["source_column"].lower(),
                edge["target_table"].lower(),
                edge["target_column"].lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            edges.append(edge)
    return edges


def fetch_one_column_df(
    conn,
    schema_name: str,
    table_name: str,
    column_name: str,
    limit: int,
) -> pd.DataFrame:
    query = sql.SQL("""
        SELECT {col}
        FROM {table}
        WHERE {col} IS NOT NULL
        LIMIT %s
    """).format(
        col=sql.Identifier(column_name),
        table=quoted_table(schema_name, table_name),
    )
    with conn.cursor() as cur:
        cur.execute(query, (limit,))
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=[column_name])


def fetch_distinct_values(conn, schema_name: str, table_name: str, column_name: str) -> set[str]:
    query = sql.SQL("""
        SELECT DISTINCT {col}
        FROM {table}
        WHERE {col} IS NOT NULL
    """).format(
        col=sql.Identifier(column_name),
        table=quoted_table(schema_name, table_name),
    )
    with conn.cursor() as cur:
        cur.execute(query)
        return {
            normalized
            for (value,) in cur.fetchall()
            if (normalized := normalize_value(value)) is not None
        }


def exact_overlap_metrics(
    conn,
    schema_name: str,
    source_table: str,
    source_column: str,
    target_table: str,
    target_column: str,
) -> dict[str, Any]:
    source_values = fetch_distinct_values(conn, schema_name, source_table, source_column)
    target_values = fetch_distinct_values(conn, schema_name, target_table, target_column)
    intersection = len(source_values & target_values)
    union = len(source_values | target_values)
    source_distinct = len(source_values)
    target_distinct = len(target_values)
    return {
        "source_distinct": source_distinct,
        "target_distinct": target_distinct,
        "intersection": intersection,
        "union": union,
        "manual_jaccard": (intersection / union) if union else 0.0,
        "source_in_target": (intersection / source_distinct) if source_distinct else 0.0,
        "target_in_source": (intersection / target_distinct) if target_distinct else 0.0,
    }


def first_match_score(matches) -> float:
    if not matches:
        return 0.0
    return float(max(matches.values()))


def valentine_scores(
    conn,
    schema_name: str,
    source_table: str,
    source_column: str,
    target_table: str,
    target_column: str,
    limit: int,
) -> dict[str, float]:
    source_df = fetch_one_column_df(conn, schema_name, source_table, source_column, limit)
    target_df = fetch_one_column_df(conn, schema_name, target_table, target_column, limit)
    if source_df.empty or target_df.empty:
        return {
            "valentine_coma_score": 0.0,
            "valentine_jaccard_score": 0.0,
            "sample_source_rows": len(source_df),
            "sample_target_rows": len(target_df),
        }

    df_names = [source_table, target_table]
    coma_matches = valentine_match(
        [source_df, target_df],
        Coma(use_instances=True, use_schema=True),
        df_names=df_names,
    )
    jaccard_matches = valentine_match(
        [source_df, target_df],
        JaccardDistanceMatcher(),
        df_names=df_names,
    )
    return {
        "valentine_coma_score": first_match_score(coma_matches),
        "valentine_jaccard_score": first_match_score(jaccard_matches),
        "sample_source_rows": len(source_df),
        "sample_target_rows": len(target_df),
    }


def classify_evidence(row: dict[str, Any], inclusion_threshold: float, jaccard_threshold: float) -> str:
    if row["source_in_target"] >= inclusion_threshold and row["target_in_source"] >= inclusion_threshold:
        return "equivalence_column"
    if row["source_in_target"] >= inclusion_threshold:
        return "inclusion_column_source_to_target"
    if row["manual_jaccard"] >= jaccard_threshold:
        return "high_overlap_column"
    return "weak_or_conflicting"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch-run Valentine schema matching over FK-pruned PostgreSQL column pairs."
    )
    parser.add_argument("--database", required=True, help="PostgreSQL/MAMG database name, e.g. npd_atomic_tests")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--limit", type=int, default=5000, help="Non-null sample rows per column for Valentine")
    parser.add_argument("--max-edges", type=int, default=0, help="Debug limit. 0 means all FK edges.")
    parser.add_argument("--output-prefix", default="schema_matching_valentine_fk")
    parser.add_argument("--inclusion-threshold", type=float, default=0.95)
    parser.add_argument("--jaccard-threshold", type=float, default=0.80)
    parser.add_argument("--host", default=DEFAULT_DB_CONFIG["host"])
    parser.add_argument("--port", type=int, default=DEFAULT_DB_CONFIG["port"])
    parser.add_argument("--user", default=DEFAULT_DB_CONFIG["user"])
    parser.add_argument("--password", default=DEFAULT_DB_CONFIG["password"])
    args = parser.parse_args()

    enriched_schema = load_enriched_schema(args.database)
    edges = fk_edges_from_enriched_schema(enriched_schema)
    if args.max_edges and args.max_edges > 0:
        edges = edges[: args.max_edges]

    output_dir = PROJECT_ROOT / "output" / args.database
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{args.output_prefix}.csv"
    json_path = output_dir / f"{args.output_prefix}.json"

    conn = psycopg2.connect(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=args.password,
    )

    rows: list[dict[str, Any]] = []
    try:
        for idx, edge in enumerate(edges, start=1):
            source_table = edge["source_table"]
            source_column = edge["source_column"]
            target_table = edge["target_table"]
            target_column = edge["target_column"]
            print(
                f"[{idx}/{len(edges)}] "
                f"{source_table}.{source_column} -> {target_table}.{target_column}"
            )

            overlap = exact_overlap_metrics(
                conn,
                args.schema,
                source_table,
                source_column,
                target_table,
                target_column,
            )
            scores = valentine_scores(
                conn,
                args.schema,
                source_table,
                source_column,
                target_table,
                target_column,
                args.limit,
            )
            row = {
                **edge,
                **overlap,
                **scores,
            }
            row["evidence_type"] = classify_evidence(
                row,
                inclusion_threshold=args.inclusion_threshold,
                jaccard_threshold=args.jaccard_threshold,
            )
            rows.append(row)
    finally:
        conn.close()

    write_csv(csv_path, rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["evidence_type"]] = counts.get(row["evidence_type"], 0) + 1

    print("\nDone.")
    print(f"  database: {args.database}")
    print(f"  FK-pruned edges: {len(rows)}")
    print(f"  evidence counts: {counts}")
    print(f"  CSV:  {csv_path}")
    print(f"  JSON: {json_path}")


if __name__ == "__main__":
    main()
