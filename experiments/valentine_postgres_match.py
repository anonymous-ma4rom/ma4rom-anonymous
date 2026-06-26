#!/usr/bin/env python3
"""
Run Valentine schema matching on real PostgreSQL tables.

This is a standalone experiment helper. It does not modify MAMG outputs or
pipeline code. Use it to inspect equivalence/inclusion-column evidence before
plugging the scores into ObjectProperty ranking.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from typing import Iterable

import pandas as pd
import psycopg2
from psycopg2 import sql
from valentine import valentine_match
from valentine.algorithms import Coma, JaccardDistanceMatcher


DEFAULT_DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres",
    "password": "postgres",
}


@dataclass(frozen=True)
class PairMetrics:
    source_distinct: int
    target_distinct: int
    intersection: int
    union: int
    jaccard: float
    source_in_target: float
    target_in_source: float


def parse_columns(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    cols = [part.strip() for part in raw.split(",") if part.strip()]
    return cols or None


def normalize_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return text


def quoted_table(schema_name: str, table_name: str) -> sql.SQL:
    return sql.SQL("{}.{}").format(sql.Identifier(schema_name), sql.Identifier(table_name))


def fetch_columns(conn, schema_name: str, table_name: str) -> list[str]:
    query = """
        SELECT a.attname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = %s
          AND c.relname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema_name, table_name))
        return [row[0] for row in cur.fetchall()]


def fetch_dataframe(
    conn,
    schema_name: str,
    table_name: str,
    columns: Iterable[str],
    limit: int,
) -> pd.DataFrame:
    selected = list(columns)
    if not selected:
        raise ValueError(f"{table_name} has no selected columns")

    query = sql.SQL("SELECT {cols} FROM {table} LIMIT %s").format(
        cols=sql.SQL(", ").join(sql.Identifier(col) for col in selected),
        table=quoted_table(schema_name, table_name),
    )
    with conn.cursor() as cur:
        cur.execute(query, (limit,))
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=selected)


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


def compute_pair_metrics(
    conn,
    schema_name: str,
    source_table: str,
    source_column: str,
    target_table: str,
    target_column: str,
) -> PairMetrics:
    source_values = fetch_distinct_values(conn, schema_name, source_table, source_column)
    target_values = fetch_distinct_values(conn, schema_name, target_table, target_column)
    intersection = len(source_values & target_values)
    union = len(source_values | target_values)
    source_distinct = len(source_values)
    target_distinct = len(target_values)
    return PairMetrics(
        source_distinct=source_distinct,
        target_distinct=target_distinct,
        intersection=intersection,
        union=union,
        jaccard=(intersection / union) if union else 0.0,
        source_in_target=(intersection / source_distinct) if source_distinct else 0.0,
        target_in_source=(intersection / target_distinct) if target_distinct else 0.0,
    )


def short_score(score: float) -> str:
    return f"{score:.4f}"


def print_matches(title: str, matches, top_k: int) -> None:
    print(f"\n[{title}] top {top_k}")
    sorted_matches = sorted(matches.items(), key=lambda item: item[1], reverse=True)
    for pair, score in sorted_matches[:top_k]:
        print(
            f"  {pair.source_table}.{pair.source_column}"
            f" <-> {pair.target_table}.{pair.target_column}"
            f"  score={short_score(score)}"
        )


def resolve_selected_columns(conn, schema_name: str, table_name: str, raw_cols: str | None) -> list[str]:
    available = fetch_columns(conn, schema_name, table_name)
    requested = parse_columns(raw_cols)
    if requested is None:
        return available

    available_lookup = {col.lower(): col for col in available}
    resolved = []
    missing = []
    for col in requested:
        exact = available_lookup.get(col.lower())
        if exact is None:
            missing.append(col)
        else:
            resolved.append(exact)
    if missing:
        raise ValueError(
            f"Columns not found in {table_name}: {missing}. "
            f"Available columns: {available}"
        )
    return resolved


def resolve_one_column(conn, schema_name: str, table_name: str, column_name: str | None) -> str | None:
    if not column_name:
        return None
    available = fetch_columns(conn, schema_name, table_name)
    available_lookup = {col.lower(): col for col in available}
    resolved = available_lookup.get(column_name.lower())
    if resolved is None:
        raise ValueError(
            f"Column not found in {table_name}: {column_name}. "
            f"Available columns: {available}"
        )
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Use Valentine to match columns between two PostgreSQL tables."
    )
    parser.add_argument("--database", required=True, help="PostgreSQL database name, e.g. npd_atomic_tests")
    parser.add_argument("--schema", default="public", help="PostgreSQL schema name")
    parser.add_argument("--source-table", required=True)
    parser.add_argument("--target-table", required=True)
    parser.add_argument("--source-columns", help="Comma-separated source columns. Default: all columns.")
    parser.add_argument("--target-columns", help="Comma-separated target columns. Default: all columns.")
    parser.add_argument("--source-column", help="Single source column for explicit Jaccard/inclusion report.")
    parser.add_argument("--target-column", help="Single target column for explicit Jaccard/inclusion report.")
    parser.add_argument("--limit", type=int, default=5000, help="Rows sampled per table for Valentine.")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--host", default=DEFAULT_DB_CONFIG["host"])
    parser.add_argument("--port", type=int, default=DEFAULT_DB_CONFIG["port"])
    parser.add_argument("--user", default=DEFAULT_DB_CONFIG["user"])
    parser.add_argument("--password", default=DEFAULT_DB_CONFIG["password"])
    args = parser.parse_args()

    conn = psycopg2.connect(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=args.password,
    )
    try:
        source_columns = resolve_selected_columns(conn, args.schema, args.source_table, args.source_columns)
        target_columns = resolve_selected_columns(conn, args.schema, args.target_table, args.target_columns)
        source_column = resolve_one_column(conn, args.schema, args.source_table, args.source_column)
        target_column = resolve_one_column(conn, args.schema, args.target_table, args.target_column)

        if source_column and source_column not in source_columns:
            source_columns.append(source_column)
        if target_column and target_column not in target_columns:
            target_columns.append(target_column)

        source_df = fetch_dataframe(conn, args.schema, args.source_table, source_columns, args.limit)
        target_df = fetch_dataframe(conn, args.schema, args.target_table, target_columns, args.limit)

        print("Database:", args.database)
        print("Source:", f"{args.source_table} columns={source_columns} rows={len(source_df)}")
        print("Target:", f"{args.target_table} columns={target_columns} rows={len(target_df)}")

        coma = Coma(use_instances=True, use_schema=True)
        coma_matches = valentine_match(
            [source_df, target_df],
            coma,
            df_names=[args.source_table, args.target_table],
        )
        print_matches("Valentine COMA(name + real values)", coma_matches, args.top_k)

        jaccard = JaccardDistanceMatcher()
        jaccard_matches = valentine_match(
            [source_df, target_df],
            jaccard,
            df_names=[args.source_table, args.target_table],
        )
        print_matches("Valentine Jaccard/Tversky(value-set)", jaccard_matches, args.top_k)

        if source_column and target_column:
            metrics = compute_pair_metrics(
                conn,
                args.schema,
                args.source_table,
                source_column,
                args.target_table,
                target_column,
            )
            print("\n[Explicit pair evidence]")
            print(f"  pair: {args.source_table}.{source_column} -> {args.target_table}.{target_column}")
            print(f"  source distinct: {metrics.source_distinct}")
            print(f"  target distinct: {metrics.target_distinct}")
            print(f"  intersection: {metrics.intersection}")
            print(f"  union: {metrics.union}")
            print(f"  Jaccard symmetric overlap: {short_score(metrics.jaccard)}")
            print(f"  inclusion source⊆target: {short_score(metrics.source_in_target)}")
            print(f"  inclusion target⊆source: {short_score(metrics.target_in_source)}")

            if metrics.source_in_target >= 0.95:
                print("  decision: strong inclusion-column evidence for FK/IND direction")
            elif metrics.jaccard >= 0.80:
                print("  decision: strong equivalence-column evidence")
            else:
                print("  decision: weak column-match evidence")

        print("\nMeaning:")
        print("  COMA score uses column names plus instance values.")
        print("  Jaccard/Tversky score uses real value-set overlap.")
        print("  inclusion source⊆target is the FK/IND-style directional evidence.")
        print("  These scores are features for OP ranking; they are not the final OP by themselves.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
