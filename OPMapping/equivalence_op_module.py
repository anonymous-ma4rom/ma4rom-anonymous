from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from rdflib import BNode, Graph, OWL, RDF, RDFS

from config import DB_SCHEMA_NAME, ONTOLOGY_PATH, OUTPUT_DIR
from utils.db_utils import get_connection
from utils.llm_client import call_llm
from utils.ontology_utils import local_name


CONSTRUCTION_RE = re.compile(
    r"Foreign\s+key\s+([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*=>\s*([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)",
    re.IGNORECASE,
)


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _relation_key(table: str, column: str) -> str:
    return f"{table}.{column}"


def _split_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text))
    return {p.lower() for p in re.split(r"[^A-Za-z0-9]+", spaced) if p}


def _meaningful_tokens(text: str | None) -> set[str]:
    stop = {
        "fk",
        "id",
        "uri",
        "to",
        "by",
        "of",
        "has",
        "is",
        "the",
        "a",
        "an",
        "inv",
    }
    return {token for token in _split_tokens(text) if token not in stop and len(token) > 1}


def _read_json_if_exists(path: str | Path) -> Any:
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(data: Any, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _construction_predicate(graph: Graph):
    for predicate in set(graph.predicates()):
        if local_name(str(predicate)) == "construction":
            return predicate
    return None


def _table_predicate(graph: Graph):
    for predicate in set(graph.predicates()):
        if local_name(str(predicate)) == "table":
            return predicate
    return None


def _parse_ontology_metadata(ontology_path: str) -> dict[str, Any]:
    graph = Graph()
    graph.parse(ontology_path, format="turtle")
    construction_pred = _construction_predicate(graph)
    table_pred = _table_predicate(graph)

    ops: dict[str, dict[str, Any]] = {}
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        uri = str(prop)
        ops[uri] = {
            "uri": uri,
            "local_name": local_name(uri),
            "domain": [str(x) for x in graph.objects(prop, RDFS.domain)],
            "range": [str(x) for x in graph.objects(prop, RDFS.range)],
            "construction": [str(x) for x in graph.objects(prop, construction_pred)] if construction_pred else [],
            "inverse_of": sorted(
                {str(x) for x in graph.objects(prop, OWL.inverseOf)}
                | {str(s) for s in graph.subjects(OWL.inverseOf, prop)}
            ),
            "subproperty_of": [str(x) for x in graph.objects(prop, RDFS.subPropertyOf)],
            "restrictions": [],
        }

    class_tables: dict[str, list[str]] = {}
    if table_pred:
        for cls, _, table_value in graph.triples((None, table_pred, None)):
            if not isinstance(cls, BNode):
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
            ops[prop_uri]["restrictions"].append(
                {
                    "class": str(cls),
                    "class_local": local_name(str(cls)),
                    "class_tables": class_tables.get(str(cls), []),
                    "values": values,
                    "values_local": [local_name(v) for v in values],
                }
            )
    return {"ops": ops}


def _normalize_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fetch_distinct_values(conn, table: str, column: str, schema_name: str) -> set[str]:
    query = f'SELECT DISTINCT "{column}" FROM "{schema_name}"."{table}" WHERE "{column}" IS NOT NULL'
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            return {
                normalized
                for (value,) in cur.fetchall()
                if (normalized := _normalize_value(value)) is not None
            }
    except Exception as exc:
        print(f"  [WARN] 读取 {table}.{column} 失败: {exc}")
        conn.rollback()
        return set()


def _overlap_metrics(
    conn,
    source_table: str,
    source_column: str,
    target_table: str,
    target_column: str,
    schema_name: str,
) -> dict[str, Any]:
    source_values = _fetch_distinct_values(conn, source_table, source_column, schema_name)
    target_values = _fetch_distinct_values(conn, target_table, target_column, schema_name)
    intersection = len(source_values & target_values)
    union = len(source_values | target_values)
    source_distinct = len(source_values)
    target_distinct = len(target_values)
    source_in_target = intersection / source_distinct if source_distinct else 0.0
    target_in_source = intersection / target_distinct if target_distinct else 0.0
    manual_jaccard = intersection / union if union else 0.0
    if source_in_target >= 0.95 and target_in_source >= 0.95:
        evidence_type = "equivalence_column"
    elif source_in_target >= 0.95:
        evidence_type = "inclusion_column_source_to_target"
    elif manual_jaccard >= 0.80:
        evidence_type = "high_overlap_column"
    else:
        evidence_type = "weak_or_conflicting"
    return {
        "source_distinct": source_distinct,
        "target_distinct": target_distinct,
        "intersection": intersection,
        "union": union,
        "manual_jaccard": manual_jaccard,
        "source_in_target": source_in_target,
        "target_in_source": target_in_source,
        "evidence_type": evidence_type,
    }


def _edge_from_fk(source_table: str, fk: dict[str, Any]) -> dict[str, str] | None:
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
        "constraint_name": fk.get("constraint_name") or "",
    }


def _find_fk_reference_column(
    enriched_schema: dict[str, Any],
    table_name: str,
    column_name: str,
    ref_table: str | None = None,
) -> str:
    table_info = enriched_schema.get(table_name) or enriched_schema.get(table_name.lower()) or {}
    for fk in table_info.get("foreign_keys") or []:
        fk_col = fk.get("column")
        fk_ref_table = fk.get("references_table") or fk.get("ref_table")
        if fk_col != column_name:
            continue
        if ref_table and fk_ref_table and fk_ref_table != ref_table:
            continue
        return fk.get("references_column") or fk.get("ref_col") or ""
    return ""


def _find_fk_constraint_name(
    enriched_schema: dict[str, Any],
    table_name: str,
    column_name: str,
    ref_table: str | None = None,
) -> str:
    table_info = enriched_schema.get(table_name) or enriched_schema.get(table_name.lower()) or {}
    for fk in table_info.get("foreign_keys") or []:
        fk_col = fk.get("column")
        fk_ref_table = fk.get("references_table") or fk.get("ref_table")
        if fk_col != column_name:
            continue
        if ref_table and fk_ref_table and fk_ref_table != ref_table:
            continue
        return fk.get("constraint_name") or ""
    return ""


def _class_from_alignment_entry(entry: dict[str, Any] | None) -> str:
    if not isinstance(entry, dict):
        return ""
    for key in ("class_uri", "sub_class_uri", "parent_class_uri"):
        value = entry.get(key)
        if value:
            return value
    return ""


def _infer_class_uri_from_table(table_name: str, ontology: dict[str, Any]) -> str:
    table_norm = _norm(table_name)
    best_uri = ""
    best_score = -1
    for uri in ontology.get("classes", []):
        local_norm = _norm(local_name(uri))
        score = 0
        if local_norm == table_norm:
            score = 100
        elif local_norm and (local_norm in table_norm or table_norm in local_norm):
            score = min(len(local_norm), len(table_norm))
        if score > best_score:
            best_score = score
            best_uri = uri
    return best_uri if best_score > 0 else ""


def _normalized_name_variants(name: str | None) -> set[str]:
    raw = _norm(name)
    if not raw:
        return set()
    out = {raw}
    if raw.endswith("ies") and len(raw) > 4:
        out.add(raw[:-3] + "y")
    if raw.endswith("es") and len(raw) > 3:
        out.add(raw[:-2])
    if raw.endswith("s") and len(raw) > 3:
        out.add(raw[:-1])
    return out


def _repair_table_class_from_dp_and_name(
    table: str,
    entry: dict[str, Any] | None,
    current_class: str,
    ontology: dict[str, Any],
) -> str:
    """Use the same low-confidence SH repair evidence before OP endpoint pruning."""
    if not isinstance(entry, dict):
        return current_class
    if (entry.get("class_confidence") or "").lower() == "high":
        return current_class

    dp_map = ontology.get("datatype_properties", {}) or {}
    evidence: dict[str, int] = {}
    for col_info in (entry.get("columns") or {}).values():
        if not isinstance(col_info, dict) or col_info.get("role") != "data_attr":
            continue
        prop_uri = col_info.get("prop_uri")
        if not prop_uri:
            continue
        for domain_uri in (dp_map.get(prop_uri, {}) or {}).get("domain", []) or []:
            evidence[domain_uri] = evidence.get(domain_uri, 0) + 1
    if evidence:
        best_class, best_count = max(evidence.items(), key=lambda item: item[1])
        if best_count > evidence.get(current_class, 0):
            current_class = best_class

    table_variants = _normalized_name_variants(table)
    current_variants = _normalized_name_variants(local_name(current_class))
    if table_variants and not (table_variants & current_variants):
        for cls_uri in ontology.get("classes", []) or []:
            if table_variants & _normalized_name_variants(local_name(cls_uri)):
                return cls_uri
    return current_class


def _table_class(table: str, final_alignment: dict[str, Any], ontology: dict[str, Any]) -> str:
    lower_map = {str(k).lower(): v for k, v in final_alignment.items()}
    entry = final_alignment.get(table) or lower_map.get(table.lower())
    direct = _class_from_alignment_entry(entry)
    if direct:
        return _repair_table_class_from_dp_and_name(table, entry, direct, ontology)
    return _infer_class_uri_from_table(table, ontology)


def _column_class_hint(table: str, column: str, final_alignment: dict[str, Any], key: str) -> str:
    entry = final_alignment.get(table) or final_alignment.get(table.lower()) or {}
    col_entry = (entry.get("columns") or {}).get(column) or (entry.get("columns") or {}).get(column.lower()) or {}
    return col_entry.get(key) or ""


def _build_fk_tasks(
    enriched_schema: dict[str, Any],
    final_alignment: dict[str, Any],
    ontology: dict[str, Any],
    schema_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tasks: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    seen = set()
    conn = get_connection()
    try:
        for table_name, info in sorted(enriched_schema.items()):
            for fk in info.get("foreign_keys") or []:
                edge = _edge_from_fk(table_name, fk)
                if not edge:
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
                metrics = _overlap_metrics(
                    conn,
                    edge["source_table"],
                    edge["source_column"],
                    edge["target_table"],
                    edge["target_column"],
                    schema_name,
                )
                match_row = {**edge, **metrics, "relation_key": _relation_key(table_name, edge["source_column"])}
                evidence_rows.append(match_row)
                domain_class = (
                    _table_class(table_name, final_alignment, ontology)
                    or _column_class_hint(table_name, edge["source_column"], final_alignment, "domain_class_uri")
                )
                range_class = (
                    _table_class(edge["target_table"], final_alignment, ontology)
                    or _column_class_hint(table_name, edge["source_column"], final_alignment, "range_class_uri")
                )
                if not domain_class or not range_class:
                    continue
                tasks.append(
                    {
                        "task_type": "fk_obj",
                        "key": _relation_key(table_name, edge["source_column"]),
                        "name_hint": edge["source_column"],
                        "source_table": edge["source_table"],
                        "source_column": edge["source_column"],
                        "target_table": edge["target_table"],
                        "target_column": edge["target_column"],
                        "domain_class_uri": domain_class,
                        "range_class_uri": range_class,
                        "schema_matching": [match_row],
                    }
                )
    finally:
        conn.close()
    return tasks, evidence_rows


def _build_sr_tasks(
    final_alignment: dict[str, Any],
    enriched_schema: dict[str, Any],
    ontology: dict[str, Any],
    schema_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tasks: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    seen = set()
    conn = get_connection()
    try:
        for table_name, entry in sorted(final_alignment.items()):
            if not isinstance(entry, dict) or entry.get("pattern") != "SR":
                continue
            fk1 = entry.get("fk1") or {}
            fk2 = entry.get("fk2") or {}
            endpoints = []
            for fk in [fk1, fk2]:
                col = fk.get("column")
                ref_table = fk.get("ref_table")
                if not col or not ref_table:
                    continue
                ref_col = (
                    fk.get("ref_col")
                    or fk.get("references_column")
                    or _find_fk_reference_column(enriched_schema, table_name, col, ref_table)
                    or "ID"
                )
                metrics = _overlap_metrics(conn, table_name, col, ref_table, ref_col, schema_name)
                match_row = {
                    "source_table": table_name,
                    "source_column": col,
                    "target_table": ref_table,
                    "target_column": ref_col,
                    "constraint_name": fk.get("constraint_name")
                    or _find_fk_constraint_name(enriched_schema, table_name, col, ref_table),
                    "relation_key": table_name.lower(),
                    **metrics,
                }
                evidence_rows.append(match_row)
                endpoints.append((fk, match_row))
            if len(endpoints) < 2:
                continue
            domain_class = (
                _table_class(endpoints[0][1]["target_table"], final_alignment, ontology)
                or endpoints[0][0].get("domain_class_hint")
                or entry.get("domain_class_uri")
            )
            range_class = (
                _table_class(endpoints[1][1]["target_table"], final_alignment, ontology)
                or endpoints[1][0].get("range_class_hint")
                or entry.get("range_class_uri")
            )
            if not domain_class or not range_class:
                continue
            seen.add(table_name.lower())
            tasks.append(
                {
                    "task_type": "sr_relation",
                    "key": table_name,
                    "name_hint": table_name,
                    "source_table": table_name,
                    "source_column": endpoints[0][1]["source_column"],
                    "target_table": endpoints[1][1]["target_table"],
                    "target_column": endpoints[1][1]["target_column"],
                    "domain_class_uri": domain_class,
                    "range_class_uri": range_class,
                    "schema_matching": [x[1] for x in endpoints],
                }
            )
        for table_name, info in sorted(enriched_schema.items()):
            fks = [_edge_from_fk(table_name, fk) for fk in info.get("foreign_keys") or []]
            fks = [fk for fk in fks if fk]
            if len(fks) < 2 or table_name.lower() in seen:
                continue
            for left_index in range(len(fks)):
                for right_index in range(left_index + 1, len(fks)):
                    left = fks[left_index]
                    right = fks[right_index]
                    pair_key = (
                        table_name.lower(),
                        left["source_column"].lower(),
                        right["source_column"].lower(),
                    )
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)
                    endpoints = []
                    for edge in (left, right):
                        metrics = _overlap_metrics(
                            conn,
                            edge["source_table"],
                            edge["source_column"],
                            edge["target_table"],
                            edge["target_column"],
                            schema_name,
                        )
                        match_row = {
                            **edge,
                            **metrics,
                            "relation_key": table_name.lower(),
                        }
                        evidence_rows.append(match_row)
                        endpoints.append(match_row)
                    domain_class = _table_class(left["target_table"], final_alignment, ontology)
                    range_class = _table_class(right["target_table"], final_alignment, ontology)
                    if not domain_class or not range_class:
                        continue
                    tasks.append(
                        {
                            "task_type": "sr_relation_inferred",
                            "key": f"{table_name}::{left['source_column']}__{right['source_column']}",
                            "name_hint": table_name,
                            "source_table": table_name,
                            "source_column": left["source_column"],
                            "target_table": right["target_table"],
                            "target_column": right["target_column"],
                            "domain_class_uri": domain_class,
                            "range_class_uri": range_class,
                            "schema_matching": endpoints,
                        }
                    )
    finally:
        conn.close()
    return tasks, evidence_rows


def _class_match_kind(hint: str, prop_values: list[str], ontology: dict[str, Any]) -> str:
    if not hint:
        return "missing_hint"
    if not prop_values:
        return "op_has_no_declared_class"
    union_members = ontology.get("union_members", {})
    expanded = []
    for value in prop_values:
        expanded.extend(union_members.get(value) or [value])
    for value in expanded:
        if hint == value or local_name(hint).lower() == local_name(value).lower():
            return "exact"
    for value in expanded:
        if value in ontology.get("ancestors_of", {}).get(hint, []):
            return "table_class_is_subclass_of_op_class"
    for value in expanded:
        if hint in ontology.get("ancestors_of", {}).get(value, []):
            return "table_class_is_parent_of_op_class"
    return "conflict"


def _class_match_explanation(side: str, hint: str, prop_values: list[str], ontology: dict[str, Any]) -> str:
    kind = _class_match_kind(hint, prop_values, ontology)
    if kind == "exact":
        return f"{side}: exact {local_name(hint)}"
    if kind == "table_class_is_subclass_of_op_class":
        return f"{side}: compatible because table class {local_name(hint)} is subclass of OP class {[local_name(v) for v in prop_values]}"
    if kind == "table_class_is_parent_of_op_class":
        return f"{side}: OP class is more specific than table class; use only with strong name/role evidence"
    if kind == "op_has_no_declared_class":
        return f"{side}: OP has no declared class"
    return f"{side}: conflict with {[local_name(v) for v in prop_values]}"


def _class_compatible(hint: str, candidate: str, ontology: dict[str, Any]) -> bool:
    if not hint or not candidate:
        return False
    if hint == candidate or local_name(hint).lower() == local_name(candidate).lower():
        return True
    ancestors = ontology.get("ancestors_of", {})
    return candidate in ancestors.get(hint, []) or hint in ancestors.get(candidate, [])


def _restriction_endpoint_support(
    domain_hint: str,
    range_hint: str,
    op_meta: dict[str, Any],
    ontology: dict[str, Any],
) -> dict[str, Any]:
    """Return restriction evidence whose endpoints close under class hierarchy."""
    hits = []
    for item in op_meta.get("restrictions", []) or []:
        cls = item.get("class") or ""
        values = item.get("values") or []
        domain_ok = _class_compatible(domain_hint, cls, ontology)
        range_ok = any(_class_compatible(range_hint, value, ontology) for value in values)
        if domain_ok and range_ok:
            hits.append(
                {
                    "class": item.get("class_local") or local_name(cls),
                    "values": item.get("values_local") or [local_name(v) for v in values],
                }
            )
    return {"hits": hits[:3]}


def _matching_constructions(task: dict[str, Any], op_meta: dict[str, Any]) -> list[str]:
    edge_norms = [
        tuple(_norm(row.get(k)) for k in ("source_table", "source_column", "target_table", "target_column"))
        for row in task.get("schema_matching", [])
    ]
    construction_hits = []
    for text in op_meta.get("construction", []) or []:
        for match in CONSTRUCTION_RE.finditer(text):
            if tuple(_norm(x) for x in match.groups()) in edge_norms:
                construction_hits.append(match.group(0))
    return construction_hits


def _class_endpoint_compatible(hint: str, prop_values: list[str], ontology: dict[str, Any]) -> bool:
    return _class_match_kind(hint, prop_values, ontology) in {
        "exact",
        "table_class_is_subclass_of_op_class",
        "table_class_is_parent_of_op_class",
    }


def _filter_endpoint_candidates(
    task: dict[str, Any],
    ontology: dict[str, Any],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    name_hint = task["name_hint"]
    domain_hint = task["domain_class_uri"]
    range_hint = task["range_class_uri"]
    table_tokens = _meaningful_tokens(task.get("source_table"))
    column_tokens = _meaningful_tokens(task.get("source_column"))
    target_tokens = _meaningful_tokens(task.get("target_table")) | _meaningful_tokens(task.get("target_column"))
    constraint_tokens = set()
    for row in task.get("schema_matching", []) or []:
        constraint_tokens |= _meaningful_tokens(row.get("constraint_name"))
    rows = []
    for uri, info in ontology.get("object_properties", {}).items():
        local = local_name(uri)
        op_meta = metadata.get("ops", {}).get(uri, {})
        restriction_support = _restriction_endpoint_support(domain_hint, range_hint, op_meta, ontology)
        construction_hits = _matching_constructions(task, op_meta)
        op_tokens = _meaningful_tokens(local)
        declared_endpoint_match = (
            _class_endpoint_compatible(domain_hint, info.get("domain", []), ontology)
            and _class_endpoint_compatible(range_hint, info.get("range", []), ontology)
        )
        restriction_endpoint_match = bool(restriction_support["hits"])
        construction_match = bool(construction_hits)
        name_pattern_match = bool(
            op_tokens
            & (table_tokens | column_tokens | target_tokens | constraint_tokens)
        ) or _norm(local) in _norm(
            " ".join(
                [
                    str(name_hint),
                    str(task.get("source_table")),
                    str(task.get("source_column")),
                    str(task.get("target_table")),
                    str(task.get("target_column")),
                    " ".join(row.get("constraint_name", "") for row in task.get("schema_matching", []) or []),
                ]
            )
        )
        if not (
            declared_endpoint_match
            or restriction_endpoint_match
            or construction_match
            or name_pattern_match
        ):
            continue

        rows.append(
            {
                "uri": uri,
                "local_name": local,
                "domain": info.get("domain", []),
                "range": info.get("range", []),
                "declared_endpoint_match": declared_endpoint_match,
                "restriction_endpoint_match": restriction_endpoint_match,
                "restriction_endpoint_hits": restriction_support["hits"],
                "construction_match": construction_match,
                "matching_construction": construction_hits[:2],
                "name_pattern_match": name_pattern_match,
                "domain_match_kind": _class_match_kind(domain_hint, info.get("domain", []), ontology),
                "range_match_kind": _class_match_kind(range_hint, info.get("range", []), ontology),
                "domain_closure_explanation": _class_match_explanation("domain", domain_hint, info.get("domain", []), ontology),
                "range_closure_explanation": _class_match_explanation("range", range_hint, info.get("range", []), ontology),
                "name_exact_match": _norm(name_hint) == _norm(local),
                "table_role_tokens": sorted(table_tokens & op_tokens),
                "column_role_tokens": sorted(column_tokens & op_tokens),
                "target_role_tokens": sorted(target_tokens & op_tokens),
                "constraint_role_tokens": sorted(constraint_tokens & op_tokens),
                "_task": task,
            }
        )
    return rows


def _reverse_sr_task(task: dict[str, Any]) -> dict[str, Any]:
    rows = task.get("schema_matching") or []
    if len(rows) < 2:
        return task
    left, right = rows[0], rows[1]
    reversed_task = dict(task)
    reversed_task["source_column"] = right.get("source_column")
    reversed_task["target_table"] = left.get("target_table")
    reversed_task["target_column"] = left.get("target_column")
    reversed_task["domain_class_uri"] = task.get("range_class_uri")
    reversed_task["range_class_uri"] = task.get("domain_class_uri")
    reversed_task["schema_matching"] = [right, left]
    reversed_task["sr_direction"] = "reversed"
    return reversed_task


def _sr_direction_candidates(
    task: dict[str, Any],
    ontology: dict[str, Any],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    normal_task = {**task, "sr_direction": task.get("sr_direction") or "normal"}
    reversed_task = _reverse_sr_task(task)
    return _filter_endpoint_candidates(normal_task, ontology, metadata) + _filter_endpoint_candidates(
        reversed_task, ontology, metadata
    )


def _compact_op_info(op_uri: str, meta: dict[str, Any], scored: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    op_meta = meta["ops"].get(op_uri, {})
    construction_hits = _matching_constructions(task, op_meta)
    return {
        "uri": op_uri,
        "local_name": op_meta.get("local_name") or local_name(op_uri),
        "sr_direction": task.get("sr_direction"),
        "candidate_domain_class": local_name(task.get("domain_class_uri")),
        "candidate_range_class": local_name(task.get("range_class_uri")),
        "domain_local": [local_name(x) for x in op_meta.get("domain", [])],
        "range_local": [local_name(x) for x in op_meta.get("range", [])],
        "declared_endpoint_match": scored.get("declared_endpoint_match", False),
        "restriction_endpoint_match": scored.get("restriction_endpoint_match", False),
        "restriction_endpoint_hits": scored.get("restriction_endpoint_hits", []),
        "name_pattern_match": scored.get("name_pattern_match", False),
        "domain_match_kind": scored.get("domain_match_kind"),
        "range_match_kind": scored.get("range_match_kind"),
        "domain_closure_explanation": scored.get("domain_closure_explanation"),
        "range_closure_explanation": scored.get("range_closure_explanation"),
        "name_exact_match": scored.get("name_exact_match", False),
        "table_role_tokens": scored.get("table_role_tokens", []),
        "column_role_tokens": scored.get("column_role_tokens", []),
        "target_role_tokens": scored.get("target_role_tokens", []),
        "constraint_role_tokens": scored.get("constraint_role_tokens", []),
        "exact_sql_construction_match": bool(construction_hits),
        "matching_construction": construction_hits[:2],
        "inverse_of": [local_name(x) for x in op_meta.get("inverse_of", [])],
        "subproperty_of": [local_name(x) for x in op_meta.get("subproperty_of", [])],
        "restrictions": [
            {
                "class": x.get("class_local"),
                "class_tables": x.get("class_tables", [])[:2],
                "values": x.get("values_local", [])[:3],
            }
            for x in op_meta.get("restrictions", [])[:3]
        ],
    }


def _build_prompt(task: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    payload = {
        "task": "从候选 ontology ObjectProperty 里选择当前数据库关系最合适的 OP。只能选候选 URI 或 null。",
        "hard_rules": [
            "这是从零 OP 模块：候选来自 ontology 全部 ObjectProperty 的 domain/range endpoint closure，不来自旧 OP top-k。",
            "等价列/包含列证据说明当前数据库列真实值如何连接目标主键/候选键；它用来确认关系端点和方向，不等于 OP 名称匹配。",
            "FK constraint name 和表/列名只用于模式匹配召回候选；最终选择必须结合 endpoint、等价列/IND、construction/restriction 证据。",
            "普通 FK：source table class 是 domain，referenced table class 是 range。",
            "SR 关系表：关系表名是 role/name hint；候选里会同时给出 normal/reversed 两种方向，必须根据 endpoint 和证据选择方向。",
            "如果 ClassMapping top-1 与 FK constraint/table role 明显冲突，不要机械相信 top-1；说明冲突并选择证据链最闭合的候选。",
            "如果两个 OP endpoint 都兼容，优先选择 relation/table/column role 更具体的 OP。",
            "如果一个候选是另一个的 subPropertyOf，且名称/角色支持，选更具体的子属性。",
            "如果 endpoint 方向相反，只能在 inverseOf 明确支持时选择相应方向；否则不要硬选。",
            "如果所有候选都明显冲突，返回 null。",
        ],
        "relation": {
            "key": task["key"],
            "task_type": task["task_type"],
            "name_hint": task["name_hint"],
            "domain_class": local_name(task["domain_class_uri"]),
            "range_class": local_name(task["range_class_uri"]),
        },
        "schema_matching_evidence": [
            {
                "source": f"{row['source_table']}.{row['source_column']}",
                "target": f"{row['target_table']}.{row['target_column']}",
                "fk_constraint_name": row.get("constraint_name"),
                "manual_jaccard": row.get("manual_jaccard"),
                "source_in_target_IND": row.get("source_in_target"),
                "target_in_source": row.get("target_in_source"),
                "source_distinct": row.get("source_distinct"),
                "target_distinct": row.get("target_distinct"),
                "intersection": row.get("intersection"),
                "evidence_type": row.get("evidence_type"),
            }
            for row in task.get("schema_matching", [])
        ],
        "candidate_object_properties": candidates,
        "output_schema": {
            "selected_uri": "candidate uri or null",
            "selected_direction": "normal|reversed|null; only required for SR candidates",
            "selected_local_name": "local name or empty",
            "confidence": "high|medium|low",
            "reason": "简短中文理由，必须提到 endpoint、name/role、等价列/IND 证据是否支持",
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _selected_is_candidate(selected_uri: str, candidates: list[dict[str, Any]]) -> bool:
    return bool(selected_uri) and selected_uri in {c["uri"] for c in candidates}


def _strong_equivalence_support(task: dict[str, Any]) -> bool:
    rows = task.get("schema_matching") or []
    if not rows:
        return False
    return any(
        row.get("evidence_type") == "equivalence_column"
        or float(row.get("source_in_target") or 0.0) >= 0.95
        for row in rows
    )


def run_equivalence_op_module(
    final_alignment: dict[str, Any],
    ontology: dict[str, Any],
    enriched_schema: dict[str, Any],
    *,
    schema_name: str = DB_SCHEMA_NAME,
    output_dir: str = OUTPUT_DIR,
    ontology_path: str = ONTOLOGY_PATH,
    min_endpoint_score: float | None = None,
    sleep_seconds: float | None = None,
) -> dict[str, Any]:
    """Run the clean OP module and return a OP-step1-compatible result."""
    min_endpoint_score = min_endpoint_score if min_endpoint_score is not None else float(os.getenv("MAMG_EQUIV_OP_MIN_ENDPOINT_SCORE", "0.5"))
    sleep_seconds = sleep_seconds if sleep_seconds is not None else float(os.getenv("MAMG_EQUIV_OP_LLM_SLEEP", "0.15"))

    fk_tasks, fk_evidence = _build_fk_tasks(enriched_schema, final_alignment, ontology, schema_name)
    sr_tasks, sr_evidence = _build_sr_tasks(final_alignment, enriched_schema, ontology, schema_name)
    tasks = fk_tasks + sr_tasks
    metadata = _parse_ontology_metadata(ontology_path)

    _write_json(
        {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "schema_name": schema_name,
            "evidence_rows": fk_evidence + sr_evidence,
        },
        Path(output_dir) / "schema_matching_equivalence_op.json",
    )

    result: dict[str, Any] = {}
    prediction_rows = []
    system = (
        "You are an OBDA ontology mapping expert. "
        "Return strict JSON only. Do not use Markdown. "
        "Select ObjectProperty only from candidates."
    )

    print(f"  等价列 OP 模块任务数: FK={len(fk_tasks)}, SR={len(sr_tasks)}, total={len(tasks)}")
    for idx, task in enumerate(tasks, start=1):
        if str(task.get("task_type", "")).startswith("sr_relation"):
            filtered_candidates = _sr_direction_candidates(task, ontology, metadata)
        else:
            filtered_candidates = _filter_endpoint_candidates(task, ontology, metadata)
        candidates = [
            _compact_op_info(item["uri"], metadata, item, item.get("_task") or task)
            for item in filtered_candidates
            if item.get("uri")
        ]
        print(f"  [{idx}/{len(tasks)}] EquivOP selecting {task['key']} candidates={len(candidates)}")
        selected_uri = ""
        selected_direction = ""
        response: dict[str, Any]
        error = None
        if candidates:
            try:
                response = call_llm(_build_prompt(task, candidates), system=system, prefer_fast=False)
                selected_uri = str(response.get("selected_uri") or "")
                selected_direction = str(response.get("selected_direction") or "")
                if not _selected_is_candidate(selected_uri, candidates):
                    selected_uri = ""
            except Exception as exc:
                response = {"error": str(exc)}
                error = str(exc)
        else:
            response = {"selected_uri": None, "reason": "No endpoint-compatible ontology ObjectProperty candidates."}

        selected_candidate = None
        if selected_uri:
            direction_candidates = [
                c for c in candidates
                if c["uri"] == selected_uri and (
                    not selected_direction
                    or not c.get("sr_direction")
                    or c.get("sr_direction") == selected_direction
                )
            ]
            selected_candidate = direction_candidates[0] if direction_candidates else None
            if selected_candidate and selected_candidate.get("sr_direction"):
                selected_direction = selected_candidate["sr_direction"]

        selected_local = local_name(selected_uri) if selected_uri else ""
        is_sr_task = str(task.get("task_type", "")).startswith("sr_relation")
        schema_rows = task.get("schema_matching") or []
        row0 = schema_rows[0] if len(schema_rows) >= 1 else {}
        row1 = schema_rows[1] if len(schema_rows) >= 2 else row0
        entry = {
            "object_prop_uri": selected_uri or None,
            "confidence": response.get("confidence") or ("medium" if selected_uri else "low"),
            "method": "equivalence_column_pattern_matching_llm",
            "scenario_type": task["task_type"],
            "domain_class_uri": task["domain_class_uri"],
            "range_class_uri": task["range_class_uri"],
            "name_hint": task["name_hint"],
            "sr_direction": selected_direction or task.get("sr_direction"),
            "sr_subject_column": (
                row1.get("source_column")
                if selected_direction == "reversed"
                else task.get("source_column")
            ),
            "sr_object_column": (
                row0.get("source_column")
                if selected_direction == "reversed"
                else row1.get("source_column")
            )
            if is_sr_task
            else None,
            "sr_subject_ref_table": (
                row1.get("target_table")
                if selected_direction == "reversed"
                else row0.get("target_table")
            )
            if is_sr_task
            else None,
            "sr_object_ref_table": (
                row0.get("target_table")
                if selected_direction == "reversed"
                else row1.get("target_table")
            )
            if is_sr_task
            else None,
            "schema_matching": task.get("schema_matching", []),
            "candidates_used": candidates,
            "llm_response": response,
            "error": error,
        }
        result[task["key"]] = entry
        prediction_rows.append(
            {
                "relation_key": task["key"],
                "task_type": task["task_type"],
                "source": f"{task.get('source_table')}.{task.get('source_column')}",
                "target": f"{task.get('target_table')}.{task.get('target_column')}",
                "llm_selected_uri": selected_uri,
                "llm_selected_local_name": selected_local,
                "llm_selected_direction": selected_direction,
                "candidate_count": len(candidates),
                "schema_matching": task.get("schema_matching", []),
                "llm_response": response,
                "error": error,
            }
        )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    _write_json(
        {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "setting": "clean OP module: hard domain/range filtering + equivalence/inclusion column evidence + ontology endpoint candidates + LLM",
            "candidate_policy": "no weighted OP scoring; candidates are retained by endpoint closure, restriction closure, or exact sql:construction",
            "summary": {
                "total": len(prediction_rows),
                "answered": sum(1 for row in prediction_rows if row.get("llm_selected_uri")),
                "no_answer": sum(1 for row in prediction_rows if not row.get("llm_selected_uri")),
            },
            "results": prediction_rows,
        },
        Path(output_dir) / "equivalence_op_module_predictions.json",
    )
    return result
