"""
real_value_enhancement_agent.py  ——  真实值上下文增强

真实值增强的职责：
  ✓ 表级 Class 低置信 → 拉数据重判 Class
  ✓ data_attr 列低置信 → 拉数据值重判 DatatypeProperty
  ✓ fk_obj 列低置信 → 拉数据重判 range Class URI（不判 ObjectProperty）
  ✓ SR 表 domain/range Class 低置信 → 拉数据重判两端 Class
  ✗ 不判断任何 ObjectProperty（那是 OP 映射的职责）
"""

import json
import re
import copy
import sys
from pathlib import Path

# 兼容 PyCharm profiler / run_path：确保项目根目录在 sys.path 里
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.llm_client import call_llm as _call_llm
from utils.db_utils import get_conn as _get_conn, fetch_sample_rows
from config import (
    REAL_VALUE_ANCESTOR_MAX_DEPTH,
    REAL_VALUE_BOOL_MAX_VALUES,
    REAL_VALUE_DATA_ATTR_NULL_FALLBACK_MIN_SCORE,
    REAL_VALUE_ENUM_DISTINCT_MAX_FOR_CODE,
    REAL_VALUE_ENUM_MAX_VALUES,
    REAL_VALUE_ENUM_NUMERIC_RATIO_THRESHOLD,
    REAL_VALUE_ENUM_PER_VALUE_LIMIT,
    REAL_VALUE_ENUM_SAMPLE_DISTINCT_RATIO_THRESHOLD,
    REAL_VALUE_FK_CONTEXT_MAX_INCOMING,
    REAL_VALUE_RULE_FALLBACK_DISTINCT_MAX,
    REAL_VALUE_RULE_FALLBACK_REPEATED_RATIO,
    REAL_VALUE_RULE_STRUCT_SIGNAL_THRESHOLD,
    REAL_VALUE_SAMPLE_ROWS_LIMIT,
    REAL_VALUE_TYPE_HIGH_GAP,
    REAL_VALUE_TYPE_HIGH_SCORE,
    REAL_VALUE_TYPE_MEDIUM_GAP,
    REAL_VALUE_TYPE_MEDIUM_SCORE,
    REAL_VALUE_TYPE_WEAK_SCORE,
)


def _ns_from_uri(uri: str) -> str:
    if not uri:
        return ""
    if "#" in uri:
        return uri.split("#")[0] + "#"
    return uri.rsplit("/", 1)[0] + "/"


def _fetch_distinct_value_profiles(
    table_name: str,
    col_name: str,
    per_value_limit: int = REAL_VALUE_ENUM_PER_VALUE_LIMIT,
    max_values: int = REAL_VALUE_ENUM_MAX_VALUES,
) -> tuple[list[str], dict[str, list[dict]]]:
    """
    先 DISTINCT 再分值抽样：
      1) 读取列的所有 distinct 值（受 max_values 限制）
      2) 每个值抽样若干行（受 per_value_limit 限制）
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT DISTINCT "{col_name}" FROM "{table_name}" '
                f'WHERE "{col_name}" IS NOT NULL ORDER BY 1 LIMIT %s',
                (max_values,),
            )
            vals = [row[0] for row in cur.fetchall()]

            value_profiles = {}
            for v in vals:
                cur.execute(
                    f'SELECT * FROM "{table_name}" WHERE "{col_name}" = %s ORDER BY random() LIMIT %s',
                    (v, per_value_limit),
                )
                cols = [desc[0] for desc in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                value_profiles[str(v)] = rows

            return [str(v) for v in vals], value_profiles
    except Exception as e:
        print(f"  [WARN] DISTINCT+分值抽样失败 {table_name}.{col_name}: {e}")
        conn.rollback()
        return [], {}
    finally:
        conn.close()


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _trim_context_row(row: dict | None, max_items: int = 12) -> dict:
    if not isinstance(row, dict):
        return {}
    out = {}
    for idx, (k, v) in enumerate(row.items()):
        if idx >= max_items:
            break
        out[k] = v
    return out


def _build_type_group_context(
    table_name: str,
    type_col: str,
    value_profiles: dict[str, list[dict]],
    enriched_schema: dict | None,
    fk_context: dict | None,
    per_value_rows: int = 3,
    ref_rows_limit: int = 2,
) -> dict:
    """
    TYPE 值 -> 当前表样本行 -> FK 引用行 / incoming 关系行。
    这是给 LLM 的 instance-level group context，不依赖具体数据集名称。
    """
    pk_col = _first_pk(enriched_schema or {}, table_name)
    outgoing = (fk_context or {}).get("outgoing_fks", []) or []
    incoming = (fk_context or {}).get("incoming_fks", []) or []
    context = {
        "table": table_name,
        "type_column": type_col,
        "pk_column": pk_col,
        "groups": {},
    }

    try:
        conn = _get_conn()
    except Exception as e:
        context["warning"] = f"无法连接数据库补充 FK 上下文: {e}"
        return context

    try:
        with conn.cursor() as cur:
            for raw_value, rows in (value_profiles or {}).items():
                group_rows = []
                for row in (rows or [])[:per_value_rows]:
                    row_ctx = {"self": _trim_context_row(row)}

                    fk_refs = {}
                    for fk in outgoing:
                        col = fk.get("column")
                        ref_table = fk.get("ref_table")
                        ref_col = fk.get("ref_col") or _first_pk(enriched_schema or {}, ref_table)
                        if not col or not ref_table or not ref_col or row.get(col) is None:
                            continue
                        try:
                            cur.execute(
                                f"SELECT * FROM {_quote_ident(ref_table)} "
                                f"WHERE {_quote_ident(ref_col)} = %s LIMIT {int(ref_rows_limit)}",
                                (row.get(col),),
                            )
                            cols = [d[0] for d in cur.description]
                            fk_refs[f"{col}->{ref_table}.{ref_col}"] = [
                                _trim_context_row(dict(zip(cols, r))) for r in cur.fetchall()
                            ]
                        except Exception:
                            conn.rollback()

                    incoming_refs = {}
                    if pk_col and row.get(pk_col) is not None:
                        for rel in incoming:
                            rel_table = rel.get("from_table")
                            rel_col = rel.get("from_column")
                            if not rel_table or not rel_col:
                                continue
                            try:
                                cur.execute(
                                    f"SELECT * FROM {_quote_ident(rel_table)} "
                                    f"WHERE {_quote_ident(rel_col)} = %s LIMIT {int(ref_rows_limit)}",
                                    (row.get(pk_col),),
                                )
                                cols = [d[0] for d in cur.description]
                                incoming_refs[f"{rel_table}.{rel_col}->{table_name}.{pk_col}"] = [
                                    _trim_context_row(dict(zip(cols, r))) for r in cur.fetchall()
                                ]
                            except Exception:
                                conn.rollback()

                    if fk_refs:
                        row_ctx["outgoing_fk_rows"] = fk_refs
                    if incoming_refs:
                        row_ctx["incoming_relation_rows"] = incoming_refs
                    group_rows.append(row_ctx)

                context["groups"][str(raw_value)] = group_rows
    finally:
        conn.close()

    return context


def _collect_descendants(root_uri: str, children_of: dict) -> list[str]:
    if not root_uri or not children_of:
        return []
    out = []
    queue = [root_uri]
    seen = {root_uri}
    while queue:
        cur = queue.pop(0)
        for child in children_of.get(cur, []):
            if child in seen:
                continue
            seen.add(child)
            out.append(child)
            queue.append(child)
    return out


def _local_name(uri: str) -> str:
    if not uri:
        return ""
    return uri.split("#")[-1].split("/")[-1]


def _norm_token(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _strip_dp_name_wrapper(local_name: str) -> str:
    """
    DatatypeProperty 常见命名：has_a_name / has_an_email / has_the_first_name。
    真实值增强只用它做保守锁定，避免实例值把同名列误改成别的属性。
    """
    norm = _norm_token(local_name)
    for prefix in ("hasa", "hasan", "hasthe", "has"):
        if norm.startswith(prefix) and len(norm) > len(prefix):
            return norm[len(prefix):]
    return norm


def _dp_range_xsd(ontology: dict | None, prop_uri: str | None) -> str | None:
    if not ontology or not prop_uri:
        return None
    ranges = ((ontology.get("datatype_properties", {}) or {}).get(prop_uri, {}) or {}).get("range", []) or []
    for r in ranges:
        local = _local_name(r).lower()
        if local:
            return local
    return None


def _sql_type_compatible_with_dp(sql_type: str, ontology: dict | None, prop_uri: str | None) -> bool:
    xsd = _dp_range_xsd(ontology, prop_uri)
    if not xsd:
        return True

    st = (sql_type or "").lower()
    is_num_sql = any(k in st for k in ("int", "numeric", "decimal", "real", "double", "float"))
    is_bool_sql = "bool" in st
    is_date_sql = "date" in st or "time" in st

    is_num_xsd = xsd in {
        "int", "integer", "decimal", "float", "double",
        "nonnegativeinteger", "unsignedlong", "unsignedint",
    }
    is_bool_xsd = xsd == "boolean"
    is_date_xsd = xsd in {"date", "datetime"}

    if is_num_xsd and not is_num_sql:
        return False
    if is_bool_xsd and not is_bool_sql:
        return False
    if is_date_xsd and not is_date_sql:
        return False
    return True


def _find_schema_locked_dp(col_name: str, col_cands: list, sql_type: str, ontology: dict | None) -> str | None:
    norm_col = _norm_token(col_name)
    if not norm_col:
        return None

    # 第一优先级：列名和属性 local name 完全一致。
    for c in col_cands:
        uri = c.get("uri")
        if not uri or not _sql_type_compatible_with_dp(sql_type, ontology, uri):
            continue
        if norm_col == _norm_token(c.get("local_name") or _local_name(uri)):
            return uri

    # 第二优先级：has_a/has_an/has_the 等本体属性命名包装后等于列名。
    for c in col_cands:
        uri = c.get("uri")
        if not uri or not _sql_type_compatible_with_dp(sql_type, ontology, uri):
            continue
        local = c.get("local_name") or _local_name(uri)
        if norm_col == _strip_dp_name_wrapper(local):
            return uri

    return None


def _leaf_descendants(descendant_uris: list[str] | None, children_of: dict) -> list[str]:
    leaves = []
    descendant_set = set(descendant_uris or [])
    for uri in descendant_uris or []:
        children = [c for c in children_of.get(uri, []) if c in descendant_set]
        if not children:
            leaves.append(uri)
    return leaves


def _first_pk(enriched_schema: dict, table_name: str) -> str | None:
    pks = (enriched_schema.get(table_name, {}) or {}).get("primary_key", []) or []
    return pks[0] if pks else None


def _iter_foreign_keys(table_info: dict) -> list[dict]:
    out = []
    for fk in (table_info or {}).get("foreign_keys", []) or []:
        col = fk.get("column")
        ref_table = fk.get("ref_table") or fk.get("references_table")
        ref_col = fk.get("ref_col") or fk.get("references_column")
        if col and ref_table:
            out.append({"column": col, "ref_table": ref_table, "ref_col": ref_col})
    return out


def _uri_local_name(uri: str | None) -> str:
    if not uri:
        return ""
    if "#" in uri:
        return uri.split("#")[-1]
    return uri.rsplit("/", 1)[-1]


def _norm_entity_name(text: str) -> str:
    raw = (text or "").lower()
    return "".join(ch for ch in raw if ch.isalnum())


def _add_class_hint(class_hints: dict, uri: str | None, score: float, source: str) -> None:
    if not uri or not isinstance(uri, str) or not uri.startswith("http"):
        return
    s = float(score or 0.0)
    prev = class_hints.get(uri)
    if prev is None or s > prev.get("score", 0.0):
        class_hints[uri] = {
            "uri": uri,
            "local_name": _uri_local_name(uri),
            "score": round(s, 3),
            "source": source,
        }


def _relation_semantic_hints_from_candidates(
    rel_table: str, fk_col: str, candidates: dict
) -> dict:
    """
    从候选集中抽取 FK 语义提示：
      - relation_hints: 关系名提示（local_name）
      - class_hints: 与该 FK 侧相关的 class 提示（带分数）
    """
    entry = (candidates or {}).get(rel_table, {}) or {}
    pattern = entry.get("pattern")
    rel_hints = []
    rel_seen = set()
    class_hints = {}

    if pattern == "SR":
        fk1_col = ((entry.get("fk1") or {}) or {}).get("column")
        fk2_col = ((entry.get("fk2") or {}) or {}).get("column")
        side = "domain" if fk_col == fk1_col else "range" if fk_col == fk2_col else None

        for c in entry.get("sr_prop_candidates", [])[:5]:
            local = c.get("local_name") or _uri_local_name(c.get("uri"))
            if local and local not in rel_seen:
                rel_seen.add(local)
                rel_hints.append(local)

            if side:
                for cls_uri in c.get(side, []) or []:
                    _add_class_hint(
                        class_hints,
                        cls_uri,
                        c.get("score", 0.0),
                        source=f"sr_{side}:{local or ''}",
                    )
            else:
                for cls_uri in (c.get("domain", []) or []) + (c.get("range", []) or []):
                    _add_class_hint(
                        class_hints,
                        cls_uri,
                        c.get("score", 0.0) * 0.7,
                        source=f"sr_any:{local or ''}",
                    )
    else:
        col_entry = (entry.get("columns", {}) or {}).get(fk_col, {}) or {}
        role = col_entry.get("role")
        for c in col_entry.get("candidates", [])[:5]:
            local = c.get("local_name") or _uri_local_name(c.get("uri"))
            if local and local not in rel_seen:
                rel_seen.add(local)
                rel_hints.append(local)

            # 非 SR 表中，FK 通常对应 relation 的 range 端；保留 domain 作为弱提示兜底
            for cls_uri in c.get("range", []) or []:
                _add_class_hint(
                    class_hints,
                    cls_uri,
                    c.get("score", 0.0),
                    source=f"range:{local or ''}",
                )
            for cls_uri in c.get("domain", []) or []:
                _add_class_hint(
                    class_hints,
                    cls_uri,
                    c.get("score", 0.0) * 0.45,
                    source=f"domain:{local or ''}",
                )

        # SH 继承表的 inherited PK 指向父类表时，用子类候选作为强提示
        if pattern == "SH" and role in ("sh_inherited_pk", "pk"):
            for c in entry.get("sub_class_candidates", [])[:5]:
                uri = c.get("uri")
                if uri:
                    _add_class_hint(
                        class_hints,
                        uri,
                        c.get("score", 0.0) or 0.7,
                        source="sh_subclass",
                    )
                    local = c.get("local_name") or _uri_local_name(uri)
                    if local and local not in rel_seen:
                        rel_seen.add(local)
                        rel_hints.append(local)

    return {
        "relation_hints": rel_hints,
        "class_hints": sorted(
            class_hints.values(), key=lambda x: x.get("score", 0.0), reverse=True
        ),
    }


def _build_fk_semantic_context(
    table_name: str,
    enriched_schema: dict | None,
    candidates: dict | None,
    implicit_relations: dict | None = None,
    group_col: str | None = None,
    group_values: list[str] | None = None,
    max_incoming: int = REAL_VALUE_FK_CONTEXT_MAX_INCOMING,
) -> dict:
    """
    基于 schema/FK 图构建语义上下文，供真实值增强的 TYPE/BOOL 判断使用。
    包含：
      - 当前表 outgoing FK
      - 指向当前表的 incoming FK（含关系候选提示）
      - 若给定 group_col/group_values，则给出按取值分组的 incoming 覆盖率
    """
    if not enriched_schema or table_name not in enriched_schema:
        return {}

    def _is_discriminator_fk(src_table: str, src_col: str) -> bool:
        col_entry = (((candidates or {}).get(src_table, {}) or {}).get("columns", {}) or {}).get(src_col, {}) or {}
        return col_entry.get("role") == "discriminator"

    table_info = enriched_schema.get(table_name, {}) or {}
    pk_col = _first_pk(enriched_schema, table_name)

    outgoing = []
    for fk in _iter_foreign_keys(table_info):
        if _is_discriminator_fk(table_name, fk["column"]):
            continue
        outgoing.append({
            "column": fk["column"],
            "ref_table": fk["ref_table"],
            "ref_col": fk.get("ref_col"),
            "source": "schema_fk",
        })

    incoming = []
    incoming_seen = set()
    for rel_table, rel_info in (enriched_schema or {}).items():
        for fk in _iter_foreign_keys(rel_info):
            if fk.get("ref_table") != table_name:
                continue
            if _is_discriminator_fk(rel_table, fk.get("column")):
                continue
            rel_sem_hints = _relation_semantic_hints_from_candidates(
                rel_table, fk.get("column"), candidates or {}
            )
            k = (rel_table, fk.get("column"), fk.get("ref_col"))
            incoming_seen.add(k)
            incoming.append({
                "from_table": rel_table,
                "from_column": fk.get("column"),
                "to_table": table_name,
                "to_column": fk.get("ref_col"),
                "from_pattern": ((candidates or {}).get(rel_table, {}) or {}).get("pattern"),
                "relation_hints": rel_sem_hints.get("relation_hints", []),
                "class_hints": rel_sem_hints.get("class_hints", []),
                "source": "schema_fk",
            })

    for edge in (implicit_relations or {}).get("edges", []) or []:
        src_table = edge.get("source_table")
        src_col = edge.get("source_column")
        tgt_table = edge.get("target_table")
        tgt_col = edge.get("target_column")
        if not src_table or not src_col or not tgt_table:
            continue

        if src_table == table_name:
            if _is_discriminator_fk(src_table, src_col):
                continue
            outgoing.append({
                "column": src_col,
                "ref_table": tgt_table,
                "ref_col": tgt_col,
                "source": "implicit",
                "evidence_score": edge.get("evidence_score"),
            })

        if tgt_table != table_name:
            continue
        if _is_discriminator_fk(src_table, src_col):
            continue
        k = (src_table, src_col, tgt_col)
        if k in incoming_seen:
            continue
        incoming_seen.add(k)
        rel_sem_hints = _relation_semantic_hints_from_candidates(
            src_table, src_col, candidates or {}
        )
        incoming.append({
            "from_table": src_table,
            "from_column": src_col,
            "to_table": table_name,
            "to_column": tgt_col,
            "from_pattern": ((candidates or {}).get(src_table, {}) or {}).get("pattern"),
            "relation_hints": rel_sem_hints.get("relation_hints", []),
            "class_hints": rel_sem_hints.get("class_hints", []),
            "source": "implicit",
            "evidence_score": edge.get("evidence_score"),
        })

    incoming = incoming[:max_incoming]

    coverage_by_value = {}
    if pk_col and group_col and group_values and incoming:
        try:
            conn = _get_conn()
        except Exception as e:
            print(f"  [WARN] 无法连接数据库，跳过分组覆盖率统计 {table_name}.{group_col}: {e}")
            conn = None
        if not conn:
            return {
                "table": table_name,
                "pk_column": pk_col,
                "outgoing_fks": outgoing,
                "incoming_fks": incoming,
                "group_col": group_col,
                "coverage_by_value": coverage_by_value,
            }
        try:
            with conn.cursor() as cur:
                for rel in incoming:
                    rel_table = rel["from_table"]
                    rel_col = rel["from_column"]
                    rel_key = f"{rel_table}.{rel_col}"
                    coverage_by_value[rel_key] = {}
                    for raw_val in group_values:
                        v = str(raw_val)
                        # total distinct entities in current group
                        cur.execute(
                            f'SELECT COUNT(DISTINCT "{pk_col}") '
                            f'FROM "{table_name}" '
                            f'WHERE CAST("{group_col}" AS TEXT) = %s',
                            (v,),
                        )
                        total = int(cur.fetchone()[0] or 0)

                        if total == 0:
                            coverage_by_value[rel_key][v] = {"total": 0, "linked": 0, "ratio": 0.0}
                            continue

                        # linked entities in current group via this incoming FK
                        cur.execute(
                            f'SELECT COUNT(DISTINCT t."{pk_col}") '
                            f'FROM "{table_name}" t '
                            f'WHERE CAST(t."{group_col}" AS TEXT) = %s '
                            f'  AND EXISTS ('
                            f'    SELECT 1 FROM "{rel_table}" r '
                            f'    WHERE r."{rel_col}" = t."{pk_col}"'
                            f'  )',
                            (v,),
                        )
                        linked = int(cur.fetchone()[0] or 0)
                        ratio = round(linked / total, 4) if total else 0.0
                        coverage_by_value[rel_key][v] = {
                            "total": total,
                            "linked": linked,
                            "ratio": ratio,
                        }
        except Exception as e:
            print(f"  [WARN] 构建 FK 语义上下文失败 {table_name}.{group_col}: {e}")
            conn.rollback()
        finally:
            conn.close()

    return {
        "table": table_name,
        "pk_column": pk_col,
        "outgoing_fks": outgoing,
        "incoming_fks": incoming,
        "group_col": group_col,
        "coverage_by_value": coverage_by_value,
    }


def _expand_enum_class_candidates(
    current_class_uri: str, class_candidates: list[dict], ontology: dict | None
) -> list[dict]:
    """
    通用候选扩展：
      - 保留 matcher 传入候选
      - 追加当前类在本体里的所有子类（若有）
      - 追加当前类父类下面的兄弟/侄子类，覆盖 WellboreType 这类“同父类分流”枚举
    """
    scored = []
    seen = set()
    current_ns = _ns_from_uri(current_class_uri)
    children_of = (ontology or {}).get("children_of", {})
    ancestors_of = (ontology or {}).get("ancestors_of", {})
    descendants = _collect_descendants(current_class_uri, children_of)
    subtree = set(descendants)
    if current_class_uri:
        subtree.add(current_class_uri)

    # 很多真实 discriminator 表达的是同一父类下的兄弟子类：
    # e.g. DEVELOPMENT/EXPLORATION/SHALLOW -> Wellbore 的不同子类。
    sibling_subtree = set()
    for ancestor_uri in (ancestors_of.get(current_class_uri, []) or [])[:4]:
        if not ancestor_uri or ancestor_uri.endswith("#Thing"):
            continue
        if current_ns and _ns_from_uri(ancestor_uri) != current_ns:
            continue
        sibling_subtree.add(ancestor_uri)
        sibling_subtree.update(_collect_descendants(ancestor_uri, children_of))

    allowed_subtree = subtree | sibling_subtree
    restrict_to_subtree = bool(allowed_subtree)

    for c in class_candidates or []:
        uri = c.get("uri")
        if not uri or uri in seen:
            continue
        if current_ns and _ns_from_uri(uri) != current_ns:
            continue
        if restrict_to_subtree and uri not in allowed_subtree:
            continue
        seen.add(uri)
        scored.append(
            {"uri": uri, "local_name": c.get("local_name", uri.split("#")[-1]), "score": c.get("score", 0.6)}
        )

    # 规则轨道优先走“父类 + 子类”子树；补齐子类候选，避免 enum 跑到无关 Class。
    for child_uri in descendants:
        if child_uri in seen:
            continue
        if current_ns and _ns_from_uri(child_uri) != current_ns:
            continue
        seen.add(child_uri)
        scored.append(
            {"uri": child_uri, "local_name": child_uri.split("#")[-1], "score": 0.55}
        )

    for child_uri in sibling_subtree:
        if child_uri in seen or child_uri == current_class_uri:
            continue
        if current_ns and _ns_from_uri(child_uri) != current_ns:
            continue
        seen.add(child_uri)
        scored.append(
            {"uri": child_uri, "local_name": _uri_local_name(child_uri), "score": 0.5}
        )

    if current_class_uri and current_class_uri not in seen:
        scored.append(
            {
                "uri": current_class_uri,
                "local_name": _uri_local_name(current_class_uri),
                "score": 0.6,
            }
        )
        seen.add(current_class_uri)

    return scored


# 真实值增强函数
def _real_value_table_class(
    table_name: str,
    class_cands: list,
    sample_rows: list,
    *,
    force_llm: bool = False,
) -> dict:
    """表级 Class 重判：用真实数据判断这张表对应哪个 Class。"""
    table_norm = _norm_entity_name(table_name)
    if table_norm.endswith("s") and len(table_norm) > 3:
        table_norm_alt = table_norm[:-1]
    else:
        table_norm_alt = table_norm

    if not force_llm:
        for c in class_cands or []:
            uri = c.get("uri")
            local = c.get("local_name") or _uri_local_name(uri)
            ln = _norm_entity_name(local)
            if not ln:
                continue
            if ln == table_norm or ln == table_norm_alt:
                return {
                    "selected_uri": uri,
                    "confidence": "high",
                    "reason": "表名与候选类名在规范化后精确匹配，优先锁定该 Class。",
                }

    prompt = f"""
## 任务
为表 `{table_name}` 找到本体中最对应的 OWL Class。
之前仅凭表名置信度不足，现在结合真实数据重新判断。

## 真实数据样本（{len(sample_rows)} 行）
{json.dumps(sample_rows, indent=2, ensure_ascii=False, default=str)}

## 候选 Class（按相似度预排序）
{json.dumps(class_cands, indent=2, ensure_ascii=False)}

## 判断要点
- 观察数据内容，判断它更像哪个 Class 的实例
- 数据为空时，仅凭候选名称语义判断

## 输出格式（严格 JSON）
{{
  "selected_uri": "选中的 URI（必须来自候选列表，或 null）",
  "confidence": "high / medium / low",
  "reason": "一句话理由，说明数据如何支持这个判断"
}}
"""
    return _call_llm(prompt)


def _real_value_data_attr(table_name: str, col_name: str, class_uri: str,
                   col_cands: list, col_values: list) -> dict:
    """数据属性列重判：用真实值判断对应哪个 DatatypeProperty。"""
    prompt = f"""
## 任务
表 `{table_name}`（Class: {class_uri}）中，列 `{col_name}` 是数据属性列。
之前置信度不足，现在结合真实数据值重新判断对应哪个 DatatypeProperty。

## 该列的真实数据值样本
{json.dumps(col_values, ensure_ascii=False, default=str)}

## 候选 DatatypeProperty（按相似度+domain排序）
{json.dumps(col_cands, indent=2, ensure_ascii=False)}

## 判断要点
- 观察数据值的格式和语义（日期？姓名？描述文字？布尔？数字？）
- 结合列名和数据值共同判断
- 若候选中无合适项，选 null

## 输出格式（严格 JSON）
{{
  "selected_uri": "选中的 URI（必须来自候选列表，或 null）",
  "confidence": "high / medium / low",
  "reason": "一句话理由"
}}
"""
    return _call_llm(prompt)

def _real_value_fk_range_class(table_name: str, col_name: str,
                        ref_table: str, ref_class_cands: list,
                        col_values: list) -> dict:
    """
    FK列 range Class 重判：用真实 FK 值辅助确认引用表对应哪个 Class。
    """
    prompt = f"""
## 任务
表 `{table_name}` 中，FK列 `{col_name}` 引用表 `{ref_table}`。
之前 range Class 置信度不足，现在结合 FK 值重新确认引用表对应哪个 OWL Class。

## 该列的真实 FK 值样本（ID值）
{json.dumps(col_values, ensure_ascii=False, default=str)}

## 引用表的 Class 候选
{json.dumps(ref_class_cands, indent=2, ensure_ascii=False)}

## 判断要点
- FK 值本身是 ID，语义有限，重点看引用表名与候选 Class 名的语义匹配
- 选最符合引用表语义的 Class URI

## 输出格式（严格 JSON）
{{
  "selected_uri": "选中的 Class URI（必须来自候选列表，或 null）",
  "confidence": "high / medium / low",
  "reason": "一句话理由"
}}
"""
    return _call_llm(prompt)


def _real_value_sr_classes(table_name: str, fk1: dict, fk2: dict,
                    sample_rows: list, alignment_entry: dict) -> dict:
    """
    SR 表 domain/range Class 重判。
    只确认两端的 Class URI，不判断 ObjectProperty。
    """
    current_domain = alignment_entry.get("domain_class_uri")
    current_range  = alignment_entry.get("range_class_uri")

    prompt = f"""
## 任务
关联表 `{table_name}` 是纯关联表（SR Pattern），连接两个实体。
之前 domain/range Class 确认置信度不足，现在结合真实数据重新确认两端的 Class。

## 真实数据样本（{len(sample_rows)} 行）
{json.dumps(sample_rows, indent=2, ensure_ascii=False, default=str)}

## FK 关联信息
- FK1: 列 `{fk1.get('column')}` 引用表 `{fk1.get('ref_table')}` → 当前 domain 推断: {current_domain}
- FK2: 列 `{fk2.get('column')}` 引用表 `{fk2.get('ref_table')}` → 当前 range 推断: {current_range}

## 判断要点
- 通过数据值确认 FK1 对应哪个 Class（domain），FK2 对应哪个 Class（range）
- 如果当前推断合理，可以保持不变
- 只输出 Class URI，不要判断 ObjectProperty

## 输出格式（严格 JSON）
{{
  "domain_class_uri": "domain 端 Class URI（保持或修正）",
  "range_class_uri": "range 端 Class URI（保持或修正）",
  "confidence": "high / medium / low",
  "reason": "一句话理由"
}}
"""
    return _call_llm(prompt)


def _tokenize_semantic_name(text: str | None) -> set[str]:
    if not text:
        return set()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text))
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    tokens = set()
    for tok in re.split(r"[^a-z0-9]+", text.lower()):
        if not tok:
            continue
        tokens.add(tok)
        if len(tok) > 3 and tok.endswith("s"):
            tokens.add(tok[:-1])
    return tokens


def _token_jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _safe_ratio(numer: float, denom: float) -> float:
    return float(numer) / float(denom) if denom else 0.0


def _ancestor_distance(
    child_uri: str,
    ancestor_uri: str,
    subclass_of: dict | None,
    max_depth: int = REAL_VALUE_ANCESTOR_MAX_DEPTH,
) -> int | None:
    if not child_uri or not ancestor_uri or not subclass_of:
        return None
    if child_uri == ancestor_uri:
        return 0
    queue = [(child_uri, 0)]
    seen = {child_uri}
    while queue:
        cur, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for p in (subclass_of.get(cur, []) or []):
            if p == ancestor_uri:
                return depth + 1
            if p in seen:
                continue
            seen.add(p)
            queue.append((p, depth + 1))
    return None


def _is_true_like(v) -> bool | None:
    s = str(v).strip().lower()
    if s in {"1", "t", "true", "y", "yes"}:
        return True
    if s in {"0", "f", "false", "n", "no"}:
        return False
    return None


def _is_likely_enum_discriminator(values: list[str], value_profiles: dict[str, list[dict]]) -> bool:
    distinct = len(values)
    if distinct < 2 or distinct > 30:
        return False

    sample_rows = sum(len(rows or []) for rows in (value_profiles or {}).values())
    sample_distinct_ratio = _safe_ratio(distinct, max(sample_rows, distinct))

    short_codes = sum(1 for v in values if len(str(v)) <= 16 and " " not in str(v))
    numeric_like = sum(1 for v in values if re.fullmatch(r"-?\d+", str(v)))
    short_ratio = _safe_ratio(short_codes, distinct)
    numeric_ratio = _safe_ratio(numeric_like, distinct)

    if short_ratio < 0.8:
        return False
    if numeric_ratio >= REAL_VALUE_ENUM_NUMERIC_RATIO_THRESHOLD:
        return True
    return sample_distinct_ratio <= REAL_VALUE_ENUM_SAMPLE_DISTINCT_RATIO_THRESHOLD and distinct <= REAL_VALUE_ENUM_DISTINCT_MAX_FOR_CODE


def _enum_structure_signal(
    values: list[str],
    fk_context: dict | None,
) -> float:
    """
    估计“枚举值是否携带结构语义”的强度。
    基于 incoming FK 的按值覆盖率差异（coverage_by_value）计算，返回 0~1。
    """
    if not values or not fk_context:
        return 0.0

    cov = (fk_context or {}).get("coverage_by_value", {}) or {}
    if not cov:
        return 0.0

    best = 0.0
    value_set = {str(v) for v in values}
    for rel_key, by_val in cov.items():
        if not isinstance(by_val, dict):
            continue
        ratios = []
        totals = 0
        for v, stat in by_val.items():
            if str(v) not in value_set:
                continue
            if not isinstance(stat, dict):
                continue
            total = int(stat.get("total", 0) or 0)
            ratio = float(stat.get("ratio", 0.0) or 0.0)
            if total <= 0:
                continue
            totals += total
            ratios.append(ratio)

        if len(ratios) < 2:
            continue

        gap = max(ratios) - min(ratios)
        active = sum(1 for r in ratios if r >= 0.2)
        active_ratio = _safe_ratio(active, len(ratios))
        sample_factor = min(1.0, totals / 20.0)
        signal = min(1.0, (0.7 * gap + 0.3 * active_ratio) * sample_factor)
        best = max(best, signal)

    return round(best, 4)


def _should_rule_first_for_enum(
    values: list[str],
    value_profiles: dict[str, list[dict]],
    fk_context: dict | None,
    descendant_uris: list[str] | None = None,
) -> tuple[bool, dict]:
    """
    规则轨道触发门：
      1) 先满足“低基数枚举”基础条件
      2) 再满足结构证据强，或“码值风格 + 存在子类空间”
    """
    is_enum = _is_likely_enum_discriminator(values, value_profiles)
    distinct = len(values or [])
    desc_cnt = len(descendant_uris or [])
    sample_rows = sum(len(rows or []) for rows in (value_profiles or {}).values())
    sample_distinct_ratio = _safe_ratio(distinct, max(sample_rows, distinct))
    repeated_ratio = max(0.0, 1.0 - sample_distinct_ratio)

    short_codes = sum(1 for v in values if len(str(v)) <= 16 and " " not in str(v))
    numeric_like = sum(1 for v in values if re.fullmatch(r"-?\d+", str(v)))
    short_ratio = _safe_ratio(short_codes, max(distinct, 1))
    numeric_ratio = _safe_ratio(numeric_like, max(distinct, 1))
    code_like = short_ratio >= 0.8 and numeric_ratio >= 0.4

    struct_signal = _enum_structure_signal(values, fk_context)
    struct_strong = struct_signal >= REAL_VALUE_RULE_STRUCT_SIGNAL_THRESHOLD
    fallback_signal = (
        code_like
        and repeated_ratio >= REAL_VALUE_RULE_FALLBACK_REPEATED_RATIO
        and desc_cnt >= 1
        and distinct <= REAL_VALUE_RULE_FALLBACK_DISTINCT_MAX
    )
    apply_rule = bool(is_enum and (struct_strong or fallback_signal))

    diagnostics = {
        "is_enum_like": is_enum,
        "distinct": distinct,
        "sample_rows": sample_rows,
        "repeated_ratio": round(repeated_ratio, 4),
        "short_ratio": round(short_ratio, 4),
        "numeric_ratio": round(numeric_ratio, 4),
        "descendant_count": desc_cnt,
        "structure_signal": struct_signal,
        "rule_first": apply_rule,
    }
    return apply_rule, diagnostics


def _score_enum_value_by_rules(
    value: str,
    rows: list[dict],
    class_candidates: list[dict],
    fk_context: dict | None,
    type_col: str | None = None,
    bool_hint_classes: list[str] | None = None,
    bool_assertions: list[dict] | None = None,
    class_ancestors: dict | None = None,
    class_subclass_of: dict | None = None,
    sh_value_evidence: dict | None = None,
    current_class_uri: str | None = None,
    descendant_uris: list[str] | None = None,
) -> list[dict]:
    bool_hint_set = set(bool_hint_classes or [])
    descendant_set = set(descendant_uris or [])
    coverage = (fk_context or {}).get("coverage_by_value", {}) or {}
    incoming = (fk_context or {}).get("incoming_fks", []) or []

    profiles = []
    for c in class_candidates or []:
        uri = c.get("uri")
        if not uri:
            continue
        local = c.get("local_name") or _uri_local_name(uri)
        base = float(c.get("score", 0.0) or 0.0)
        prior = 0.25 + 0.75 * min(base, 1.0)
        if uri in descendant_set:
            prior *= 1.06
        if current_class_uri and descendant_set and uri == current_class_uri:
            prior *= 0.35
        if uri in bool_hint_set:
            prior *= 0.9
        profiles.append({
            "uri": uri,
            "local_name": local,
            "tokens": _tokenize_semantic_name(local),
            "base": base,
            "score": prior * 0.28,
        })

    # -1) 枚举值本身的语义证据：
    #     DEVELOPMENT -> DevelopmentWellbore, FORMATION -> Formation,
    #     SEMISUB STEEL -> SemisubSteelFacility。
    value_tokens = _tokenize_semantic_name(value)
    value_norm = _norm_entity_name(value)
    value_truth = _is_true_like(value)
    col_tokens = _tokenize_semantic_name(type_col)
    col_norm = _norm_entity_name(type_col or "")
    for p in profiles:
        local_norm = _norm_entity_name(p.get("local_name"))
        sim = _token_jaccard(value_tokens, p["tokens"])
        if sim > 0:
            p["score"] += 0.42 + 0.58 * sim
        if value_norm and local_norm and (value_norm == local_norm or value_norm in local_norm):
            p["score"] += 0.72

        # YES/NO 这种布尔型枚举：true-like 值用列名语义补充。
        if value_truth is True and col_tokens:
            col_sim = _token_jaccard(col_tokens, p["tokens"])
            if col_sim > 0:
                p["score"] += 0.32 + 0.46 * col_sim
            if col_norm and local_norm and (col_norm in local_norm or local_norm in col_norm):
                p["score"] += 0.68

    # 0) TYPE 专用证据 A：布尔判别列（column -> true_class）对父类的回溯支持
    #    目的：把 Author_of_xxx 这类细粒度信号，稳定回传到 Author，而不是被 Speaker 之类关系名盖过去。
    bool_assertions = bool_assertions or []
    class_ancestors = class_ancestors or {}
    class_subclass_of = class_subclass_of or {}
    for ba in bool_assertions:
        bcol = ba.get("column")
        true_class = ba.get("true_class_uri")
        if not bcol or not true_class:
            continue
        local_true = _uri_local_name(true_class)

        total = 0
        tcnt = 0
        for row in rows or []:
            if not isinstance(row, dict) or bcol not in row:
                continue
            total += 1
            tv = _is_true_like(row.get(bcol))
            if tv is True:
                tcnt += 1
        if total == 0:
            continue
        true_ratio = _safe_ratio(tcnt, total)
        if true_ratio <= 0.0:
            continue

        for p in profiles:
            p_uri = p.get("uri")
            if not p_uri:
                continue
            # exact 子类命中（弱）
            if p_uri == true_class:
                p["score"] += 0.22 * true_ratio
                continue

            # 祖先命中（按层级距离衰减）
            if p_uri in (class_ancestors.get(true_class, []) or []):
                dist = _ancestor_distance(true_class, p_uri, class_subclass_of)
                if dist is None:
                    continue
                p["score"] += (0.34 / max(1, dist)) * true_ratio

    # 0.5) TYPE 专用证据 B：SH 子类成员证据（按值分组）
    #      若某个值几乎都出现在某个 SH 子表中，则该值应强支持该子类。
    sh_bucket = (sh_value_evidence or {}).get(str(value), []) or []
    for ev in sh_bucket:
        cls_uri = ev.get("class_uri")
        ratio = float(ev.get("ratio", 0.0) or 0.0)
        if not cls_uri:
            continue
        for p in profiles:
            p_uri = p.get("uri")
            if not p_uri:
                continue
            if p_uri == cls_uri:
                if ratio > 0:
                    p["score"] += 1.05 * ratio
                else:
                    # 当前 value 在该 SH 子类中完全不出现，给负证据
                    p["score"] -= 0.32
                continue
            # 对同一值下“明显非该 SH 子类”的候选做轻惩罚，防止 Review 泄漏到别的 type 值
            if ratio >= 0.7 and p_uri != cls_uri:
                p["score"] -= 0.18 * ratio

    # 1) 结构证据：按 value 分组覆盖率 × relation/class 语义匹配
    for rel in incoming:
        rel_table = rel.get("from_table")
        rel_col = rel.get("from_column")
        if not rel_table or not rel_col:
            continue

        rel_key = f"{rel_table}.{rel_col}"
        rel_cov = (coverage.get(rel_key) or {}).get(str(value), {}) or {}
        ratio = float(rel_cov.get("ratio", 0.0) or 0.0)
        if ratio <= 0:
            continue

        src = rel.get("source")
        edge_score = 1.0
        if src == "implicit":
            edge_score = max(0.55, min(1.0, float(rel.get("evidence_score", 0.7) or 0.7)))

        rel_hints = rel.get("relation_hints", []) or []
        class_hints = rel.get("class_hints", []) or []

        for p in profiles:
            best_match = 0.0

            for h in class_hints:
                h_uri = h.get("uri")
                h_local = h.get("local_name")
                h_score = float(h.get("score", 0.0) or 0.0)

                if h_uri and h_uri == p["uri"]:
                    best_match = max(best_match, 0.72 + 0.28 * min(h_score, 1.0))

                sim = _token_jaccard(p["tokens"], _tokenize_semantic_name(h_local))
                if sim > 0:
                    best_match = max(best_match, 0.2 + 0.55 * sim + 0.25 * min(h_score, 1.0))

            for hint in rel_hints:
                sim = _token_jaccard(p["tokens"], _tokenize_semantic_name(hint))
                if sim > 0:
                    best_match = max(best_match, 0.12 + 0.58 * sim)

            if best_match > 0:
                p["score"] += ratio * edge_score * best_match

    # 2) 值样本中的布尔列协同信号（不依赖列名前缀）
    bool_cols = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for col, v in row.items():
            t = _is_true_like(v)
            if t is None:
                continue
            stat = bool_cols.setdefault(col, {"true": 0, "total": 0})
            stat["total"] += 1
            if t:
                stat["true"] += 1

    for col, stat in bool_cols.items():
        true_ratio = _safe_ratio(stat["true"], stat["total"])
        if true_ratio < 0.5:
            continue
        col_tokens = _tokenize_semantic_name(col)
        col_norm = _norm_entity_name(col)
        for p in profiles:
            sim = _token_jaccard(p["tokens"], col_tokens)
            if sim > 0:
                p["score"] += (0.14 + 0.24 * sim) * true_ratio
            if col_norm and col_norm == _norm_entity_name(p["local_name"]):
                p["score"] += 0.45 * true_ratio

    profiles.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return profiles


def _is_numeric_enum_value(value: str) -> bool:
    return bool(re.fullmatch(r"-?\d+", str(value)))


def _has_strong_sh_support(value: str, class_uri: str | None, sh_value_evidence: dict | None) -> bool:
    if not class_uri:
        return False
    for ev in ((sh_value_evidence or {}).get(str(value), []) or []):
        if ev.get("class_uri") == class_uri and float(ev.get("ratio", 0.0) or 0.0) >= 0.7:
            return True
    return False


def _is_boolean_asserted_class(class_uri: str | None, bool_hint_classes: list[str] | None) -> bool:
    return bool(class_uri and class_uri in set(bool_hint_classes or []))


def _real_value_type_mapping(
    table_name: str,
    type_col: str,
    value_profiles: dict[str, list[dict]],
    class_candidates: list[dict],
    bool_hint_classes: list[str] | None = None,
    bool_assertions: list[dict] | None = None,
    class_ancestors: dict | None = None,
    class_subclass_of: dict | None = None,
    sh_value_evidence: dict | None = None,
    current_class_uri: str | None = None,
    descendant_uris: list[str] | None = None,
    fk_context: dict | None = None,
    group_context: dict | None = None,
) -> dict:
    """
    TYPE 列重判：规则优先，LLM 兜底。
    仅对判别列（低基数枚举）启用规则映射；非判别列保留 LLM 主判。
    """
    values = sorted((str(k) for k in (value_profiles or {}).keys()), key=lambda x: (len(x), x))
    if not values:
        return {
            "value_to_class": {},
            "unmapped_values": [],
            "confidence": "low",
            "reason": f"{type_col} 无有效取值样本",
        }

    allowed = {c.get("uri") for c in class_candidates if c.get("uri")}
    value_to_class: dict[str, str] = {}
    locked_by_rule: set[str] = set()
    ranking_snapshot = {}
    mapped_by_rule = 0

    rule_first, rule_diag = _should_rule_first_for_enum(
        values=values,
        value_profiles=value_profiles,
        fk_context=fk_context,
        descendant_uris=descendant_uris,
    )
    if rule_first:
        for v in values:
            ranking = _score_enum_value_by_rules(
                value=v,
                rows=value_profiles.get(v, []) or [],
                class_candidates=class_candidates,
                fk_context=fk_context,
                type_col=type_col,
                bool_hint_classes=bool_hint_classes,
                bool_assertions=bool_assertions,
                class_ancestors=class_ancestors,
                class_subclass_of=class_subclass_of,
                sh_value_evidence=sh_value_evidence,
                current_class_uri=current_class_uri,
                descendant_uris=descendant_uris,
            )
            ranking_snapshot[v] = [
                {"uri": r.get("uri"), "local_name": r.get("local_name"), "score": round(r.get("score", 0.0), 3)}
                for r in ranking[:5]
            ]
            if not ranking:
                continue
            top = ranking[0]
            sec = ranking[1] if len(ranking) > 1 else {"score": 0.0}
            top_score = float(top.get("score", 0.0) or 0.0)
            gap = top_score - float(sec.get("score", 0.0) or 0.0)
            top_uri = top.get("uri")
            if top_uri not in allowed:
                continue
            if _is_numeric_enum_value(v) and _is_boolean_asserted_class(top_uri, bool_hint_classes):
                continue

            high = top_score >= REAL_VALUE_TYPE_HIGH_SCORE and gap >= REAL_VALUE_TYPE_HIGH_GAP
            medium = top_score >= REAL_VALUE_TYPE_MEDIUM_SCORE and gap >= REAL_VALUE_TYPE_MEDIUM_GAP
            weak = (
                not _is_numeric_enum_value(v)
                and top_score >= REAL_VALUE_TYPE_WEAK_SCORE
                and gap >= 0.0
            )
            if high or medium or weak:
                value_to_class[v] = top_uri
                mapped_by_rule += 1
                if high:
                    locked_by_rule.add(v)

        # 若枚举值与子类近似一一对应，补齐最后一个缺失值（避免 LLM 随机漂移）
        desc = [u for u in (descendant_uris or []) if u in allowed and u != current_class_uri]
        remaining_values = [v for v in values if v not in value_to_class]
        used_desc = {u for u in value_to_class.values() if u in set(desc)}
        remaining_desc = [u for u in desc if u not in used_desc]
        if (
            len(remaining_values) == 1
            and len(remaining_desc) == 1
            and not _is_numeric_enum_value(remaining_values[0])
        ):
            value_to_class[remaining_values[0]] = remaining_desc[0]
            locked_by_rule.add(remaining_values[0])
            mapped_by_rule += 1

    # 非高置信锁定值交给 LLM 结合分组上下文复核；高置信规则值不可覆盖。
    review_values = [v for v in values if v not in locked_by_rule]
    llm_reason = ""
    llm_conf = "medium"
    if review_values:
        rule_mode_text = "规则先验 + 分组上下文 LLM复核" if rule_first else "分组上下文 LLM主判"
        review_profiles = {v: value_profiles.get(v, []) for v in review_values}
        locked_map = {v: uri for v, uri in value_to_class.items() if v in locked_by_rule}
        tentative_map = {v: uri for v, uri in value_to_class.items() if v not in locked_by_rule}
        prompt = f"""
## 任务
表 `{table_name}` 中，列 `{type_col}` 是类型判别列（discriminator）。
当前采用：{rule_mode_text}。
请根据每个 TYPE 值的分组样本、FK 上下文、incoming/outgoing 关系证据，为每个待复核值选择最合适的 Class。
若证据不足，不要硬选，放到 unmapped_values。

## TYPE 待复核取值与本表样本
{json.dumps(review_profiles, ensure_ascii=False, indent=2, default=str)}

## 分组上下文大表（本行样本 + FK引用行 + incoming关系行）
{json.dumps(group_context or {}, ensure_ascii=False, indent=2, default=str)}

## 高置信规则锁定映射（不可改）
{json.dumps(locked_map, ensure_ascii=False, indent=2)}

## 规则暂定映射（必须重新审查，可以改）
{json.dumps(tentative_map, ensure_ascii=False, indent=2)}

## 规则先验（用于参考）
{json.dumps(ranking_snapshot, ensure_ascii=False, indent=2)}

## 规则触发诊断（用于参考）
{json.dumps(rule_diag, ensure_ascii=False, indent=2)}

## FK 语义上下文（来自真实 schema）
{json.dumps(fk_context or {}, ensure_ascii=False, indent=2, default=str)}

## 候选 Class（必须从这里选）
{json.dumps(class_candidates, ensure_ascii=False, indent=2)}

## 该表已由布尔判别列直接表达的语义类（仅供约束参考）
{json.dumps(bool_hint_classes or [], ensure_ascii=False, indent=2)}

## 要求
1. 只为待复核取值输出映射，不要输出高置信锁定值。
2. 数字 TYPE 值本身没有语义，必须主要依据分组样本和 FK/关系上下文判断。
3. 如果某个 Class 已经由布尔列直接表达，除非分组上下文强支持，不要再用 TYPE 重复映射到该 Class。
4. 如果证据不足，不要硬选，放到 unmapped_values。
3. 只允许输出候选列表中的 URI。

## 输出格式（严格 JSON）
{{
  "value_to_class": {{
    "值": "Class URI"
  }},
  "unmapped_values": [值或字符串值],
  "confidence": "high / medium / low",
  "reason": "一句话说明"
}}
"""
        m = _call_llm(prompt)
        llm_reason = m.get("reason", "")
        llm_conf = m.get("confidence", "medium")
        raw_map = m.get("value_to_class", {}) or {}

        value_to_class = dict(locked_map)
        for k, uri in raw_map.items():
            k = str(k)
            if k not in review_values or k in locked_by_rule:
                continue
            if _is_numeric_enum_value(k) and _is_boolean_asserted_class(uri, bool_hint_classes):
                continue
            if uri in allowed:
                value_to_class[k] = uri

    # 若 TYPE 错映射到“当前父类”，优先提升为尚未使用的子类
    if rule_first and current_class_uri and descendant_uris:
        descendant_uris = [u for u in descendant_uris if u in allowed]
        if descendant_uris:
            score_map = {c.get("uri"): c.get("score", 0.0) for c in class_candidates if c.get("uri")}
            descendants_sorted = sorted(
                descendant_uris,
                key=lambda u: score_map.get(u, 0.0),
                reverse=True,
            )
            used = {uri for uri in value_to_class.values() if uri != current_class_uri}
            for v, uri in list(value_to_class.items()):
                if uri != current_class_uri:
                    continue
                replacement = next((d for d in descendants_sorted if d not in used), None)
                if replacement:
                    value_to_class[v] = replacement
                    used.add(replacement)
                else:
                    del value_to_class[v]

    explicit_unmapped = {v for v in values if v not in value_to_class}

    norm_unmapped = []
    for x in sorted(explicit_unmapped):
        if x.isdigit():
            norm_unmapped.append(int(x))
        else:
            norm_unmapped.append(x)

    if not norm_unmapped and mapped_by_rule >= max(1, len(values) - 1):
        conf = "high"
    elif len(value_to_class) >= 1:
        conf = "medium"
    else:
        conf = llm_conf if review_values else "low"

    reason_parts = []

    if mapped_by_rule:
        reason_parts.append(f"规则映射 {mapped_by_rule} 个取值")
    if not rule_first:
        reason_parts.append("该列未触发规则轨道，采用 LLM 主判")
    if review_values:
        reason_parts.append("非锁定取值由 LLM 结合分组上下文复核")
    if llm_reason:
        reason_parts.append(llm_reason)

    return {
        "value_to_class": value_to_class,
        "unmapped_values": norm_unmapped,
        "confidence": conf,
        "reason": "；".join(reason_parts) if reason_parts else "",
    }


def _real_value_boolean_mapping(
    table_name: str,
    bool_col: str,
    value_profiles: dict[str, list[dict]],
    class_candidates: list[dict],
    fk_context: dict | None = None,
) -> dict:
    """
    用 LLM 判断布尔判别列：true 时最可能代表哪个语义类。
    返回:
    {
      "true_class_uri": "...#Program_Chair" 或 null,
      "confidence": "high|medium|low",
      "reason": "..."
    }
    """
    prompt = f"""
## 任务
表 `{table_name}` 中，列 `{bool_col}` 是布尔判别列（boolean discriminator）。
请判断当 `{bool_col} = true` 时，对应的最合适 OWL Class（若无法确定可返回 null）。

## 取值样本（按 true/false 分组）
{json.dumps(value_profiles, ensure_ascii=False, indent=2, default=str)}

## FK 语义上下文（来自真实 schema）
{json.dumps(fk_context or {}, ensure_ascii=False, indent=2, default=str)}

## 候选 Class（必须从这里选）
{json.dumps(class_candidates, ensure_ascii=False, indent=2)}

## 输出格式（严格 JSON）
{{
  "selected_true_class_uri": "Class URI（必须来自候选列表，或 null）",
  "confidence": "high / medium / low",
  "reason": "一句话说明"
}}
"""
    m = _call_llm(prompt)
    allowed = {c.get("uri") for c in class_candidates if c.get("uri")}
    uri = m.get("selected_true_class_uri")
    if uri not in allowed:
        uri = None
    return {
        "true_class_uri": uri,
        "confidence": m.get("confidence", "medium"),
        "reason": m.get("reason", ""),
    }


def _build_sh_value_evidence(
    table_name: str,
    type_col: str,
    enriched_schema: dict | None,
    candidates: dict | None,
    alignment: dict | None,
    group_values: list[str] | None,
) -> dict:
    """
    构建 TYPE 值 -> SH 子类证据:
      value -> [{class_uri, ratio, child_table}]
    ratio = 在该 value 下，出现在子类表中的实体占比。
    """
    if not enriched_schema or table_name not in (enriched_schema or {}):
        return {}
    parent_pk = _first_pk(enriched_schema, table_name)
    if not parent_pk or not group_values:
        return {}

    out: dict[str, list[dict]] = {str(v): [] for v in group_values}
    conn = None
    try:
        conn = _get_conn()
    except Exception as e:
        print(f"  [WARN] 无法连接数据库，跳过 SH 值证据 {table_name}.{type_col}: {e}")
        return out

    try:
        with conn.cursor() as cur:
            for child_table, cand in (candidates or {}).items():
                if (cand or {}).get("pattern") != "SH":
                    continue
                if (cand or {}).get("parent_table") != table_name:
                    continue

                child_pk = _first_pk(enriched_schema, child_table)
                if not child_pk:
                    continue
                child_class = ((alignment or {}).get(child_table, {}) or {}).get("sub_class_uri")
                if not child_class:
                    continue

                # 每个值单独统计，避免复杂 SQL 方言差异
                for v in group_values:
                    vs = str(v)
                    cur.execute(
                        f'SELECT COUNT(DISTINCT p."{parent_pk}") '
                        f'FROM "{table_name}" p '
                        f'WHERE CAST(p."{type_col}" AS TEXT) = %s',
                        (vs,),
                    )
                    total = int(cur.fetchone()[0] or 0)
                    if total == 0:
                        continue

                    cur.execute(
                        f'SELECT COUNT(DISTINCT p."{parent_pk}") '
                        f'FROM "{table_name}" p '
                        f'WHERE CAST(p."{type_col}" AS TEXT) = %s '
                        f'  AND EXISTS ('
                        f'    SELECT 1 FROM "{child_table}" c '
                        f'    WHERE c."{child_pk}" = p."{parent_pk}"'
                        f'  )',
                        (vs,),
                    )
                    linked = int(cur.fetchone()[0] or 0)
                    ratio = round(_safe_ratio(linked, total), 4)
                    out[vs].append(
                        {
                            "class_uri": child_class,
                            "child_table": child_table,
                            "ratio": ratio,
                            "linked": linked,
                            "total": total,
                        }
                    )
    except Exception as e:
        print(f"  [WARN] 构建 SH 值证据失败 {table_name}.{type_col}: {e}")
        conn.rollback()
    finally:
        conn.close()

    return out



# 主函数
def run_real_value_enhancement(
    alignment: dict,
    low_conf_report: dict,
    candidates: dict,
    ontology: dict | None = None,
    enriched_schema: dict | None = None,
    force_all_context: bool = False,
) -> dict:
    """
    真实值上下文增强主函数。
    只处理 Class 和 DatatypeProperty 的低置信条目。
    fk_obj 列只重判 range Class，不判 ObjectProperty。
    SR 表只重判 domain/range Class，不判 ObjectProperty。
    """
    result = copy.deepcopy(alignment)
    total  = len(low_conf_report)
    # 按流程约束：隐式关系挖掘仅在 OP 映射阶段执行，真实值增强仅使用显式 FK 上下文。
    implicit_relations = None

    for idx, (table_name, low_info) in enumerate(low_conf_report.items(), 1):
        table_entry = result.get(table_name, {})
        table_cands = candidates.get(table_name, {})
        pattern     = table_entry.get("pattern", "SE")
        table_low   = low_info.get("table_low", False)
        columns_low = low_info.get("columns_low", [])

        print(f"\n[RealValue {idx}/{total}] {table_name} (Pattern: {pattern})")

        # 拉真实数据
        sample_rows = fetch_sample_rows(table_name, limit=REAL_VALUE_SAMPLE_ROWS_LIMIT)
        if not sample_rows:
            print(f"  空表，跳过真实值增强")
            continue
        print(f"  拉到 {len(sample_rows)} 行数据")

        current_class_uri = (
            result[table_name].get("sub_class_uri")
            if pattern == "SH"
            else result[table_name].get("class_uri")
        )

        type_assertions = result[table_name].get("type_assertions", [])
        if type_assertions:
            base_class_cands = (
                table_cands.get("sub_class_candidates", [])
                if pattern == "SH"
                else table_cands.get("table_class_candidates", [])
            )
            bool_hint_classes = [
                ta.get("true_class_uri")
                for ta in type_assertions
                if ta.get("kind") == "boolean" and ta.get("true_class_uri")
            ]
            bool_assertions = [
                {"column": ta.get("column"), "true_class_uri": ta.get("true_class_uri")}
                for ta in type_assertions
                if ta.get("kind") == "boolean" and ta.get("column") and ta.get("true_class_uri")
            ]
            class_ancestors = (ontology or {}).get("ancestors_of", {})
            class_subclass_of = (ontology or {}).get("subclass_of", {})
            for ta in type_assertions:
                type_col = ta.get("column")
                kind = ta.get("kind")
                if not type_col or kind not in ("enum", "boolean"):
                    continue

                class_cands = ta.get("class_candidates", [])
                if not class_cands:
                    class_cands = _expand_enum_class_candidates(
                        current_class_uri=current_class_uri,
                        class_candidates=base_class_cands,
                        ontology=ontology,
                    )
                    ta["class_candidates"] = class_cands

                if kind == "enum":
                    _, value_profiles = _fetch_distinct_value_profiles(table_name, type_col)
                    if not value_profiles:
                        value_profiles = {
                            str(row.get(type_col)): [row]
                            for row in sample_rows if row.get(type_col) is not None
                        }

                    expanded_class_cands = _expand_enum_class_candidates(
                        current_class_uri=current_class_uri,
                        class_candidates=class_cands,
                        ontology=ontology,
                    )
                    descendant_uris = _collect_descendants(
                        current_class_uri,
                        (ontology or {}).get("children_of", {}),
                    )
                    fk_context = _build_fk_semantic_context(
                        table_name=table_name,
                        enriched_schema=enriched_schema,
                        candidates=candidates,
                        implicit_relations=implicit_relations,
                        group_col=type_col,
                        group_values=sorted(value_profiles.keys()),
                    )
                    group_context = _build_type_group_context(
                        table_name=table_name,
                        type_col=type_col,
                        value_profiles=value_profiles,
                        enriched_schema=enriched_schema,
                        fk_context=fk_context,
                    )
                    sh_value_evidence = _build_sh_value_evidence(
                        table_name=table_name,
                        type_col=type_col,
                        enriched_schema=enriched_schema,
                        candidates=candidates,
                        alignment=result,
                        group_values=sorted(value_profiles.keys()),
                    )

                    re_map = _real_value_type_mapping(
                        table_name=table_name,
                        type_col=type_col,
                        value_profiles=value_profiles,
                        class_candidates=expanded_class_cands,
                        bool_hint_classes=bool_hint_classes,
                        bool_assertions=bool_assertions,
                        class_ancestors=class_ancestors,
                        class_subclass_of=class_subclass_of,
                        sh_value_evidence=sh_value_evidence,
                        current_class_uri=current_class_uri,
                        descendant_uris=descendant_uris,
                        fk_context=fk_context,
                        group_context=group_context,
                    )
                    ta["value_to_class"] = re_map.get("value_to_class", {})
                    ta["unmapped_values"] = re_map.get("unmapped_values", [])
                    ta["confidence"] = re_map.get("confidence", "medium")
                    ta["reason"] = re_map.get("reason", "")
                    ta["class_candidates"] = expanded_class_cands
                    print(
                        f"  TYPE 列 {type_col} 的值→Class 映射重判完成，"
                        f"映射了 {len(ta['value_to_class'])} 个值，未映射 {len(ta['unmapped_values'])} 个值"
                    )
                else:
                    # BOOL discriminator：基于样本+FK语义上下文重判 true_class_uri
                    _, bool_profiles = _fetch_distinct_value_profiles(
                        table_name,
                        type_col,
                        per_value_limit=REAL_VALUE_ENUM_PER_VALUE_LIMIT,
                        max_values=REAL_VALUE_BOOL_MAX_VALUES,
                    )
                    if not bool_profiles:
                        bool_profiles = {"true": [], "false": []}
                        for row in sample_rows:
                            val = row.get(type_col)
                            if val is None:
                                continue
                            key = str(val).lower()
                            if key in {"t", "true", "1"}:
                                bool_profiles["true"].append(row)
                            elif key in {"f", "false", "0"}:
                                bool_profiles["false"].append(row)

                    fk_context = _build_fk_semantic_context(
                        table_name=table_name,
                        enriched_schema=enriched_schema,
                        candidates=candidates,
                        implicit_relations=implicit_relations,
                        group_col=type_col,
                        group_values=sorted(bool_profiles.keys()),
                    )
                    re_map = _real_value_boolean_mapping(
                        table_name=table_name,
                        bool_col=type_col,
                        value_profiles=bool_profiles,
                        class_candidates=class_cands,
                        fk_context=fk_context,
                    )

                    ta["true_class_uri"] = re_map.get("true_class_uri")
                    ta["confidence"] = re_map.get("confidence", "medium")
                    ta["reason"] = re_map.get("reason", "布尔判别列映射为 true 时的语义类")
                    if ta.get("true_class_uri"):
                        bool_hint_classes.append(ta["true_class_uri"])
                    print(
                        f"  BOOL 列 {type_col} 语义类补全 -> {ta.get('true_class_uri')} [{ta.get('confidence')}]"
                    )

        # ── SR 表：重判两端 Class ──
        if pattern == "SR" and table_low:
            fk1 = table_entry.get("fk1", {})
            fk2 = table_entry.get("fk2", {})
            try:
                re_match = _real_value_sr_classes(table_name, fk1, fk2, sample_rows, table_entry)
                result[table_name]["domain_class_uri"] = re_match.get("domain_class_uri")
                result[table_name]["range_class_uri"]  = re_match.get("range_class_uri")
                result[table_name]["confidence"]       = re_match.get("confidence", "medium")
                print(f"  SR domain→{re_match.get('domain_class_uri')} range→{re_match.get('range_class_uri')} [{re_match.get('confidence')}]")
                print(f"  理由: {re_match.get('reason', '')}")
            except Exception as e:
                print(f"  [WARN] SR 真实值增强失败: {e}，保留原结果")
            continue

        # ── SE / SH 表：表级 Class 重判 ──
        if table_low:
            if pattern == "SH":
                class_cands = table_cands.get("sub_class_candidates", [])
            else:
                class_cands = table_cands.get("table_class_candidates", [])

            if class_cands:
                try:
                    re_match = _real_value_table_class(
                        table_name,
                        class_cands,
                        sample_rows,
                        force_llm=force_all_context,
                    )
                    allowed = {c.get("uri") for c in class_cands if c.get("uri")}
                    new_uri  = re_match.get("selected_uri")
                    if new_uri not in allowed:
                        new_uri = class_cands[0].get("uri")
                    new_conf = re_match.get("confidence", "medium")
                    if pattern == "SH":
                        result[table_name]["sub_class_uri"]    = new_uri
                        result[table_name]["class_confidence"] = new_conf
                    else:
                        result[table_name]["class_uri"]        = new_uri
                        result[table_name]["class_confidence"] = new_conf
                    print(f"  表级 Class 重判 → {new_uri} [{new_conf}]")
                    print(f"  理由: {re_match.get('reason', '')}")
                except Exception as e:
                    print(f"  [WARN] 表级真实值增强失败: {e}，保留原结果")

        if not columns_low:
            continue

        # 列级重判
        # 预提取列值
        col_values_cache = {
            col: [row.get(col) for row in sample_rows if row.get(col) is not None]
            for col in columns_low
        }

        # 取当前表的 class_uri 作为上下文
        current_class_uri = (
            result[table_name].get("sub_class_uri")
            if pattern == "SH"
            else result[table_name].get("class_uri")
        )

        for col_name in columns_low:
            col_entry       = result[table_name].get("columns", {}).get(col_name, {})
            col_cands_entry = table_cands.get("columns", {}).get(col_name, {})
            role            = col_entry.get("role", "data_attr")
            col_cands       = col_cands_entry.get("candidates", [])
            col_vals        = col_values_cache.get(col_name, [])

            # discriminator 列跳过
            if role == "discriminator":
                continue

            if not col_cands:
                print(f"  列 {col_name}: 无候选集，跳过")
                continue

            try:
                if role == "fk_obj":
                    # 只重判 range Class，不判 ObjectProperty
                    ref_table = col_entry.get("ref_table", "")
                    ref_class_cands = col_cands_entry.get("ref_class_candidates", [])
                    if not ref_class_cands:
                        # 从候选集的 range 字段尝试构造
                        ref_class_cands = [
                            {"uri": r, "local_name": r.split("#")[-1], "score": 0.5}
                            for c in col_cands for r in c.get("range", [])
                        ]
                    re_match = _real_value_fk_range_class(
                        table_name, col_name, ref_table, ref_class_cands, col_vals
                    )
                    allowed = {c.get("uri") for c in ref_class_cands if c.get("uri")}
                    new_uri = re_match.get("selected_uri")
                    if new_uri not in allowed:
                        new_uri = ref_class_cands[0].get("uri") if ref_class_cands else None
                    result[table_name]["columns"][col_name]["range_class_uri"] = new_uri
                    result[table_name]["columns"][col_name]["confidence"]      = re_match.get("confidence", "medium")
                    print(f"  列 {col_name} (fk_obj) range Class 重判 → {new_uri} [{re_match.get('confidence')}]")

                else:  # data_attr
                    col_type = col_cands_entry.get("column_type", "")
                    # 列名与候选属性名/常见 has_a_* 包装匹配时锁定，避免真实值增强用实例值误改 schema 语义。
                    locked_uri = None
                    if not force_all_context:
                        locked_uri = _find_schema_locked_dp(
                            col_name,
                            col_cands,
                            col_type,
                            ontology,
                        )
                    if locked_uri:
                        result[table_name]["columns"][col_name]["prop_uri"] = locked_uri
                        result[table_name]["columns"][col_name]["confidence"] = "high"
                        print(f"  列 {col_name} (data_attr) schema 名称锁定 → {locked_uri} [high]")
                        continue

                    re_match = _real_value_data_attr(
                        table_name, col_name, current_class_uri, col_cands, col_vals
                    )
                    allowed = {c.get("uri") for c in col_cands if c.get("uri")}
                    new_uri = re_match.get("selected_uri")
                    old_uri = col_entry.get("prop_uri")
                    if new_uri not in allowed:
                        if new_uri is None and col_cands and (col_cands[0].get("score", 0.0) >= REAL_VALUE_DATA_ATTR_NULL_FALLBACK_MIN_SCORE):
                            new_uri = col_cands[0].get("uri")
                        elif new_uri is not None:
                            new_uri = col_entry.get("prop_uri")
                    # 保守回退：真实值增强返回 null 时，不删除已有可用映射。
                    # 真实值增强的目标是增强而非“抹掉”已有对齐，尤其是低置信条目。
                    if new_uri is None and old_uri in allowed:
                        new_uri = old_uri
                    if (
                        old_uri in allowed
                        and new_uri != old_uri
                        and not _sql_type_compatible_with_dp(col_type, ontology, new_uri)
                        and _sql_type_compatible_with_dp(col_type, ontology, old_uri)
                    ):
                        print(f"  列 {col_name} (data_attr) 真实值增强类型不兼容，保留原映射 → {old_uri}")
                        new_uri = old_uri
                    result[table_name]["columns"][col_name]["prop_uri"]   = new_uri
                    result[table_name]["columns"][col_name]["confidence"] = re_match.get("confidence", "medium")
                    print(f"  列 {col_name} (data_attr) 重判 → {new_uri} [{re_match.get('confidence')}]")

                print(f"  理由: {re_match.get('reason', '')}")

            except Exception as e:
                print(f"  [WARN] 列 {col_name} 真实值增强失败: {e}，保留原结果")

    return result


# 主程序
if __name__ == "__main__":
    from utils.db_utils import read_schema
    from utils.ontology_utils import read_ontology
    from utils.merge_fks import merge_fks_into_schema, merge_llm_fks_into_schema
    from FKCompletion_agent import allocate_targets_and_shooters, discover_implicit_foreign_keys
    from classify_agent import classify_rule, classify_agent, find_difference, cal_num_fks, battle_layer
    from candidate_generation import generate_candidates
    try:
        from data_property_mapping_agent import run_data_property_mapping, collect_low_confidence_data_property_mappings
    except ModuleNotFoundError:
        from DPMapping.data_property_mapping_agent import run_data_property_mapping, collect_low_confidence_data_property_mappings

    from config import ONTOLOGY_PATH, OUTPUT_DIR   # ← 路径从 config 读取
    import os

    # 读取schema
    schema = read_schema()

    # FK补全（IND）
    allocation = allocate_targets_and_shooters(schema)
    discovered_fks = discover_implicit_foreign_keys(allocation)   # schema_name 从 config 自动读取
    enriched_schema = merge_fks_into_schema(schema, discovered_fks)

    # 分类（规则 + LLM battle）
    rule_result  = classify_rule(enriched_schema)
    agent_result = classify_agent(enriched_schema)
    fks_count    = cal_num_fks(enriched_schema)
    diff         = find_difference(rule_result, agent_result)
    pattern_result = battle_layer(diff, rule_result, agent_result, fks_count, enriched_schema)

    # ④ 把 LLM 推断的 FK 也合并进 schema（修复 SH parent_class_uri = null 的问题）
    enriched_schema = merge_llm_fks_into_schema(enriched_schema, agent_result)

    # ⑤ 候选生成
    ontology   = read_ontology(ONTOLOGY_PATH)
    candidates = generate_candidates(enriched_schema, pattern_result, ontology)

    # ⑥ LLM Matcher
    alignment   = run_data_property_mapping(candidates)
    low_conf    = collect_low_confidence_data_property_mappings(alignment)

    print(f"\n低置信条目数: {len(low_conf)} 张表")

    # ⑦ 真实值上下文增强（本文件）
    final_alignment = run_real_value_enhancement(
        alignment,
        low_conf,
        candidates,
        ontology=ontology,
        enriched_schema=enriched_schema,
    )

    # ⑧ 输出结果
    print("\n\n=== 最终 Alignment（真实值增强后）===")
    import os

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    json.dump(final_alignment,
              open(os.path.join(OUTPUT_DIR, "final_alignment.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False, default=str)
    json.dump(enriched_schema,
              open(os.path.join(OUTPUT_DIR, "enriched_schema.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
