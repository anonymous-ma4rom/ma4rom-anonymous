"""
implicit_relation_miner.py

隐式关系挖掘器：
基于列值集合重叠/包含关系，挖掘 source_col -> target_col 的虚拟外键边
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from config import (
    IMPLICIT_REL_CARDINALITY_ONE_THRESHOLD,
    IMPLICIT_REL_IDLIKE_SHORT_MAXLEN,
    IMPLICIT_REL_MAX_COLUMNS_PER_TABLE,
    IMPLICIT_REL_MAX_PAIR_EVALUATIONS,
    IMPLICIT_REL_MIN_INCLUSION,
    IMPLICIT_REL_MIN_INTERSECTION,
    IMPLICIT_REL_MIN_NON_NULL,
    IMPLICIT_REL_TARGET_MIN_UNIQUENESS,
)
from utils.db_utils import get_conn as _get_conn


_NUMERIC_TYPES = {
    "smallint",
    "integer",
    "bigint",
    "numeric",
    "decimal",
    "real",
    "double precision",
    "int2",
    "int4",
    "int8",
    "float4",
    "float8",
}

_TEXT_TYPES = {
    "character varying",
    "character",
    "text",
    "varchar",
    "char",
    "uuid",
}


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _norm_type(col_type: str | None) -> str:
    return (col_type or "").strip().lower()


def _type_family(col_type: str | None) -> str:
    t = _norm_type(col_type)
    if t in _NUMERIC_TYPES:
        return "numeric"
    if t in _TEXT_TYPES:
        return "text"
    if "timestamp" in t or t == "date":
        return "datetime"
    if t in {"boolean", "bool"}:
        return "bool"
    return "other"


def _compatible_types(t1: str | None, t2: str | None) -> bool:
    f1 = _type_family(t1)
    f2 = _type_family(t2)
    if f1 == "other" or f2 == "other":
        return False
    return f1 == f2


def _tokenize(name: str) -> list[str]:
    raw = str(name or "").lower()
    token = []
    out = []
    for ch in raw:
        if ch.isalnum():
            token.append(ch)
        else:
            if token:
                out.append("".join(token))
                token = []
    if token:
        out.append("".join(token))
    return out


def _is_id_like(table_name: str, col_name: str) -> bool:
    col = str(col_name or "").lower()
    if col == "id" or col.endswith("_id") or col.startswith("id_"):
        return True
    if col.endswith("id") and len(col) <= IMPLICIT_REL_IDLIKE_SHORT_MAXLEN:
        return True

    table = str(table_name or "").lower()
    singular = table[:-1] if table.endswith("s") and len(table) > 1 else table
    if col in {f"{table}_id", f"{singular}_id", f"{table}id", f"{singular}id"}:
        return True

    toks = set(_tokenize(col))
    if "id" in toks or "ref" in toks or "key" in toks:
        return True
    return False


def _fetch_column_profile(conn, table_name: str, col_name: str) -> dict[str, Any]:
    query = (
        f"SELECT COUNT(*)::bigint, "
        f"COUNT({_q(col_name)})::bigint, "
        f"COUNT(DISTINCT {_q(col_name)})::bigint "
        f"FROM {_q(table_name)}"
    )
    with conn.cursor() as cur:
        cur.execute(query)
        total, non_null, distinct_cnt = cur.fetchone()

    total = int(total or 0)
    non_null = int(non_null or 0)
    distinct_cnt = int(distinct_cnt or 0)
    uniqueness = (distinct_cnt / non_null) if non_null else 0.0
    null_ratio = ((total - non_null) / total) if total else 1.0
    return {
        "total_rows": total,
        "non_null_rows": non_null,
        "distinct_cnt": distinct_cnt,
        "uniqueness": round(uniqueness, 6),
        "null_ratio": round(null_ratio, 6),
    }


def _fetch_overlap_metrics(
    conn,
    src_table: str,
    src_col: str,
    tgt_table: str,
    tgt_col: str,
) -> tuple[int, int, int]:
    query = (
        f"WITH s AS ("
        f"  SELECT DISTINCT {_q(src_col)} AS v FROM {_q(src_table)} "
        f"  WHERE {_q(src_col)} IS NOT NULL"
        f"),"
        f" t AS ("
        f"  SELECT DISTINCT {_q(tgt_col)} AS v FROM {_q(tgt_table)} "
        f"  WHERE {_q(tgt_col)} IS NOT NULL"
        f") "
        f"SELECT "
        f"  (SELECT COUNT(*)::bigint FROM s),"
        f"  (SELECT COUNT(*)::bigint FROM t),"
        f"  (SELECT COUNT(*)::bigint FROM s JOIN t USING (v))"
    )
    with conn.cursor() as cur:
        cur.execute(query)
        s_cnt, t_cnt, inter_cnt = cur.fetchone()
    return int(s_cnt or 0), int(t_cnt or 0), int(inter_cnt or 0)


def _cardinality_pattern(src_uniqueness: float, tgt_uniqueness: float) -> str:
    src_one = src_uniqueness >= IMPLICIT_REL_CARDINALITY_ONE_THRESHOLD
    tgt_one = tgt_uniqueness >= IMPLICIT_REL_CARDINALITY_ONE_THRESHOLD
    if src_one and tgt_one:
        return "1-1"
    if not src_one and tgt_one:
        return "n-1"
    if src_one and not tgt_one:
        return "1-n"
    return "n-n"


def _edge_key(source_table: str, source_column: str, target_table: str) -> str:
    return f"{source_table}.{source_column}->{target_table}"


def mine_implicit_relations(
    enriched_schema: dict,
    min_inclusion: float = IMPLICIT_REL_MIN_INCLUSION,
    min_intersection: int = IMPLICIT_REL_MIN_INTERSECTION,
    min_non_null: int = IMPLICIT_REL_MIN_NON_NULL,
    target_min_uniqueness: float = IMPLICIT_REL_TARGET_MIN_UNIQUENESS,
    max_columns_per_table: int = IMPLICIT_REL_MAX_COLUMNS_PER_TABLE,
    max_pair_evaluations: int = IMPLICIT_REL_MAX_PAIR_EVALUATIONS,
) -> dict:
    """
    在全库范围挖掘隐式关系边 source_col -> target_col。

    返回:
    {
      "edges": [...],
      "by_source_target": {"A.c->B": edge},
      "num_profiles": N,
      "num_pairs_evaluated": M
    }
    """
    conn = _get_conn()

    profiles: list[dict[str, Any]] = []
    by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)

    try:
        for table_name, table_info in (enriched_schema or {}).items():
            columns = (table_info or {}).get("columns", {}) or {}
            pks = set((table_info or {}).get("primary_key", []) or [])
            fk_cols = {
                (fk.get("column") or "")
                for fk in ((table_info or {}).get("foreign_keys", []) or [])
            }

            ranked = []
            for col_name, col_type in columns.items():
                try:
                    prof = _fetch_column_profile(conn, table_name, col_name)
                except Exception:
                    conn.rollback()
                    continue
                non_null = prof["non_null_rows"]
                if non_null < min_non_null:
                    continue

                is_pk = col_name in pks
                is_fk = col_name in fk_cols
                id_like = _is_id_like(table_name, col_name)
                uniqueness = prof["uniqueness"]

                source_score = (
                    (2.0 if is_pk else 0.0) +
                    (1.2 if is_fk else 0.0) +
                    (1.0 if id_like else 0.0) +
                    min(1.0, uniqueness)
                )
                target_score = source_score + (
                    1.0 if uniqueness >= IMPLICIT_REL_CARDINALITY_ONE_THRESHOLD else 0.0
                )

                rec = {
                    "table": table_name,
                    "column": col_name,
                    "column_type": col_type,
                    "type_family": _type_family(col_type),
                    "is_pk": is_pk,
                    "is_fk": is_fk,
                    "id_like": id_like,
                    **prof,
                    "source_score": round(source_score, 6),
                    "target_score": round(target_score, 6),
                }
                ranked.append(rec)

            ranked.sort(key=lambda x: x["source_score"], reverse=True)
            selected = ranked[:max_columns_per_table]
            by_table[table_name].extend(selected)
            profiles.extend(selected)

        targets = [
            c for c in profiles
            if c["distinct_cnt"] >= min_non_null
            and (c["is_pk"] or c["uniqueness"] >= target_min_uniqueness)
        ]

        edges = []
        pair_evals = 0

        for src in profiles:
            if src["distinct_cnt"] < min_non_null:
                continue

            for tgt in targets:
                if src["table"] == tgt["table"] and src["column"] == tgt["column"]:
                    continue
                if src["table"] == tgt["table"]:
                    continue
                if not _compatible_types(src["column_type"], tgt["column_type"]):
                    continue

                pair_evals += 1
                if pair_evals > max_pair_evaluations:
                    break

                try:
                    s_cnt, t_cnt, inter = _fetch_overlap_metrics(
                        conn,
                        src["table"],
                        src["column"],
                        tgt["table"],
                        tgt["column"],
                    )
                except Exception:
                    conn.rollback()
                    continue

                if s_cnt <= 0 or t_cnt <= 0 or inter < min_intersection:
                    continue

                inclusion = inter / s_cnt
                if inclusion < min_inclusion:
                    continue

                reverse_inclusion = inter / t_cnt
                union = s_cnt + t_cnt - inter
                jaccard = (inter / union) if union > 0 else 0.0

                evidence = (
                    inclusion * 0.65 +
                    reverse_inclusion * 0.25 +
                    jaccard * 0.10
                )

                edge = {
                    "source_table": src["table"],
                    "source_column": src["column"],
                    "target_table": tgt["table"],
                    "target_column": tgt["column"],
                    "source_type": src["column_type"],
                    "target_type": tgt["column_type"],
                    "source_distinct": s_cnt,
                    "target_distinct": t_cnt,
                    "intersection": inter,
                    "inclusion_score": round(inclusion, 6),
                    "reverse_inclusion": round(reverse_inclusion, 6),
                    "jaccard": round(jaccard, 6),
                    "evidence_score": round(evidence, 6),
                    "cardinality_pattern": _cardinality_pattern(
                        src["uniqueness"],
                        tgt["uniqueness"],
                    ),
                }
                edges.append(edge)

            if pair_evals > max_pair_evaluations:
                break

        edges.sort(
            key=lambda x: (
                x["evidence_score"],
                x["inclusion_score"],
                x["intersection"],
            ),
            reverse=True,
        )

        by_source_target = {}
        for edge in edges:
            key = _edge_key(edge["source_table"], edge["source_column"], edge["target_table"])
            prev = by_source_target.get(key)
            if prev is None or edge["evidence_score"] > prev["evidence_score"]:
                by_source_target[key] = edge

        return {
            "edges": edges,
            "by_source_target": by_source_target,
            "num_profiles": len(profiles),
            "num_pairs_evaluated": pair_evals,
        }

    finally:
        conn.close()


def lookup_implicit_edge(
    implicit_relations: dict | None,
    source_table: str,
    source_column: str,
    target_table: str | None = None,
) -> dict | None:
    if not implicit_relations:
        return None
    by_source_target = implicit_relations.get("by_source_target", {}) or {}

    if target_table:
        return by_source_target.get(_edge_key(source_table, source_column, target_table))

    prefix = f"{source_table}.{source_column}->"
    cands = [
        edge for key, edge in by_source_target.items()
        if key.startswith(prefix)
    ]
    if not cands:
        return None
    cands.sort(key=lambda x: x.get("evidence_score", 0.0), reverse=True)
    return cands[0]
