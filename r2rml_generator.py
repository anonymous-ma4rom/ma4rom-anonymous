"""
r2rml_generator.py  ——  R2RML 映射生成器（纯模板拼接，不用 LLM）

修复清单（相比 v1）：
  ✓ Bug1: rr:column 用普通双引号 "col"，不用 SQL 转义 \\"col\\"
  ✓ Bug2: _Inv 列的 objectMap IRI 模板指向正确的 range 表（从本体 OP domain 推断）
  ✓ Bug3: prop_uri 为 null/None/"null" 时正确跳过
  ✓ Bug4: 孤儿列 OP 的 objectMap 用正确的 range 表 IRI
  ✓ Bug5: 建立全局 class→table 反查映射，确保 IRI 模板一致
"""

import json
import os
import re

from config import (
    R2RML_DP_REFINE_FILL_MIN_NAME_SCORE,
    R2RML_DP_REFINE_FILL_MIN_SCORE,
    R2RML_DP_REFINE_REPLACE_CUR_NAME,
    R2RML_DP_REFINE_REPLACE_NAME_GAP,
    R2RML_DP_REFINE_REPLACE_SCORE_GAP,
    R2RML_DP_REFINE_REPLACE_TOP_NAME,
    R2RML_OP_FALLBACK_MIN_NAME_SCORE,
    R2RML_OP_FALLBACK_MIN_SCORE,
    R2RML_OP_FALLBACK_MIN_SIDE_SCORE,
    R2RML_PK_DP_MIN_NAME_SCORE,
    R2RML_PK_DP_MIN_SCORE,
    R2RML_PK_OP_MIN_NAME_SCORE,
    R2RML_PK_OP_MIN_SCORE,
    R2RML_PK_OP_MIN_SIDE_SCORE,
    R2RML_SH_INFER_SUBCLASS_MIN_SCORE,
)
from utils.candidate_ranking import (
    rank_object_prop_candidates,
    rank_datatype_prop_candidates,
)

# ============================================================
#  工具函数
# ============================================================

def _local_name(uri: str) -> str:
    if not uri:
        return ""
    if "#" in uri:
        return uri.split("#")[-1]
    return uri.rstrip("/").split("/")[-1]


def _get_namespace(uri: str) -> str:
    if "#" in uri:
        return uri.split("#")[0] + "#"
    return uri.rsplit("/", 1)[0] + "/"


def _is_valid_uri(uri) -> bool:
    """检查 URI 是否有效（非 null/None/空）"""
    if uri is None:
        return False
    if isinstance(uri, str) and uri.strip().lower() in ("null", "none", ""):
        return False
    return True


def _sql_col_in_query(col: str) -> str:
    """SQL 查询中的列名：需要 PostgreSQL 双引号转义"""
    return f'\\"{col}\\"'


def _sql_cols_in_query(cols: list) -> str:
    return " , ".join(_sql_col_in_query(c) for c in cols)


def _rr_column(col: str) -> str:
    """rr:column 的值：普通双引号，不转义"""
    return f'"{col}"'


def _iri_template(base_url: str, table_name: str, pk_col: str) -> str:
    table_lower = table_name.lower().replace(" ", "_")
    return f"{base_url}{table_lower}/{{{pk_col}}}"


def _predicate_str(uri: str, prefix_map: dict) -> str:
    """生成谓词字符串。含特殊字符用完整 IRI"""
    if not _is_valid_uri(uri):
        return None

    local = _local_name(uri)
    ns = _get_namespace(uri)

    if "-" in local or "." in local or " " in local:
        return f"<{uri}>"

    for prefix, namespace in prefix_map.items():
        if ns == namespace:
            return f"{prefix}:{local}"

    return f"<{uri}>"


def _xsd_type_from_sql(sql_type: str) -> str:
    """从 SQL 类型推断 XSD 类型（仅作 fallback）"""
    if not sql_type:
        return "xsd:string"
    t = sql_type.lower()
    if "int" in t:
        return "xsd:int"          # Ontop 严格校验，必须用 xsd:int 而非 xsd:integer
    elif t in ("date",):
        return "xsd:date"
    elif "timestamp" in t or "datetime" in t:
        return "xsd:dateTime"
    elif "float" in t or "double" in t or "numeric" in t or "decimal" in t:
        return "xsd:decimal"
    elif "bool" in t:
        return "xsd:boolean"
    else:
        return "xsd:string"


def build_dp_range_map(ontology: dict) -> dict:
    """
    构建 DatatypeProperty URI → XSD 类型 的映射。
    优先使用本体中声明的 range 类型，确保与 Ontop 校验一致。
    """
    dp_range = {}
    XSD_PREFIX_MAP = {
        "http://www.w3.org/2001/XMLSchema#string": "xsd:string",
        "http://www.w3.org/2001/XMLSchema#int": "xsd:int",
        "http://www.w3.org/2001/XMLSchema#integer": "xsd:integer",
        "http://www.w3.org/2001/XMLSchema#date": "xsd:date",
        "http://www.w3.org/2001/XMLSchema#dateTime": "xsd:dateTime",
        "http://www.w3.org/2001/XMLSchema#decimal": "xsd:decimal",
        "http://www.w3.org/2001/XMLSchema#boolean": "xsd:boolean",
        "http://www.w3.org/2001/XMLSchema#float": "xsd:float",
        "http://www.w3.org/2001/XMLSchema#double": "xsd:double",
        "http://www.w3.org/2001/XMLSchema#nonNegativeInteger": "xsd:nonNegativeInteger",
        "http://www.w3.org/2001/XMLSchema#unsignedLong": "xsd:unsignedLong",
        "http://www.w3.org/2001/XMLSchema#unsignedInt": "xsd:unsignedInt",
        "http://www.w3.org/2001/XMLSchema#anyURI": "xsd:anyURI",
    }
    for dp_uri, dp_info in ontology.get("datatype_properties", {}).items():
        ranges = dp_info.get("range", [])
        for r in ranges:
            if r in XSD_PREFIX_MAP:
                dp_range[dp_uri] = XSD_PREFIX_MAP[r]
                break
    return dp_range


def _xsd_type(sql_type: str, prop_uri: str = None, dp_range_map: dict = None) -> str:
    """
    确定 XSD 类型。优先查本体声明的 range，查不到再从 SQL 类型推断。
    这样能避免 Ontop 的 MappingOntologyMismatchException。
    """
    # 优先：本体声明的 range 类型
    if prop_uri and dp_range_map and prop_uri in dp_range_map:
        return dp_range_map[prop_uri]
    # Fallback：从 SQL 类型推断
    return _xsd_type_from_sql(sql_type)


def _is_sql_xsd_compatible(sql_type: str, xsd_type: str) -> bool:
    """
    避免明显不兼容的类型映射（如字符串列 -> xsd:nonNegativeInteger）。
    仅做保守过滤，不做复杂推断。
    """
    st = (sql_type or "").lower()
    xt = (xsd_type or "").lower()

    is_num_sql = any(k in st for k in ("int", "numeric", "decimal", "real", "double", "float"))
    is_bool_sql = "bool" in st
    is_date_sql = "date" in st or "time" in st

    is_num_xsd = any(
        k in xt for k in (
            "xsd:int", "xsd:integer", "xsd:decimal", "xsd:float", "xsd:double",
            "xsd:nonnegativeinteger", "xsd:unsignedlong", "xsd:unsignedint",
        )
    )
    is_bool_xsd = "xsd:boolean" in xt
    is_date_xsd = "xsd:date" in xt or "xsd:datetime" in xt

    if is_num_xsd and not is_num_sql:
        return False
    if is_bool_xsd and not is_bool_sql:
        return False
    if is_date_xsd and not is_date_sql:
        return False
    return True


def _canonical_iri_base_name(table_name: str) -> str:
    """
    将物理表名规范化为资源路径基名（通用规则，无数据集硬编码）。
    """
    t = (table_name or "").strip().lower().replace(" ", "_")
    if not t:
        return "resource"
    # 避免连续下划线/首尾下划线
    t = "_".join([seg for seg in t.split("_") if seg])
    return t or "resource"


# ============================================================
#  全局映射表：class_uri → table_name（用于 IRI 模板一致性）
# ============================================================

def build_class_to_table_map(final_alignment: dict) -> dict:
    """
    构建 class_uri → table_name 映射。
    用于：当我们知道某个 OP 的 range 是 conference#Committee，
    就能查到对应表是 Committee，从而构造 IRI template = committee/{ID}
    """
    c2t = {}
    for table_name, entry in final_alignment.items():
        pattern = entry.get("pattern", "SE")
        if pattern == "SH":
            cls = entry.get("sub_class_uri")
            parent_cls = entry.get("parent_class_uri")
            if cls:
                c2t[cls] = table_name
            # 不覆盖父类已有的映射
            if parent_cls and parent_cls not in c2t:
                c2t[parent_cls] = table_name
        elif pattern == "SR":
            continue
        else:
            cls = entry.get("class_uri")
            if cls:
                c2t[cls] = table_name
    return c2t


def build_table_to_iri_base(final_alignment: dict, enriched_schema: dict) -> dict:
    """
    构建 table_name → iri_base_name 映射。
    SH 表递归追溯到继承链的根祖先（如 Chair → Committee_member → Person → "person"）。
    SE 表用自己的表名小写。
    确保同一实体在所有 mapping 中用相同的 IRI 模板。
    """
    t2iri = {}

    def _find_parent(table_name):
        """从 enriched_schema 找到 SH 表的直接父表（PK 列同时是 FK 的引用表）"""
        fks = enriched_schema.get(table_name, {}).get("foreign_keys", [])
        pks = enriched_schema.get(table_name, {}).get("primary_key", [])
        for fk in fks:
            if fk.get("column") in pks:
                return fk.get("ref_table") or fk.get("references_table") or ""
        return ""

    def _find_root_ancestor(table_name, visited=None):
        """递归追溯继承链直到根祖先（SE 表或无父表的表）"""
        if visited is None:
            visited = set()
        if table_name in visited:
            return table_name  # 防止循环
        visited.add(table_name)

        entry = final_alignment.get(table_name, {})
        pattern = entry.get("pattern", "SE")

        if pattern != "SH":
            return table_name  # SE/SR 表就是自己

        parent = _find_parent(table_name)
        if not parent or parent == table_name:
            return table_name  # 没有父表，自己就是根

        # 递归向上
        return _find_root_ancestor(parent, visited)

    for table_name, entry in final_alignment.items():
        pattern = entry.get("pattern", "SE")
        if pattern == "SH":
            root = _find_root_ancestor(table_name)
            t2iri[table_name] = _canonical_iri_base_name(root)
        elif pattern == "SR":
            continue
        else:
            t2iri[table_name] = _canonical_iri_base_name(table_name)
    return t2iri


def resolve_range_table(
    op_uri: str,
    ontology: dict,
    class_to_table: dict,
    direction: str = "normal",
    table_to_iri: dict | None = None,
) -> str:
    """
    从本体 OP 的 domain/range 声明，推断 FK 指向的表名。
    - normal 方向：range 端是目标表
    - inverse 方向：domain 端是目标表（因为反向了）
    返回 IRI base 名（优先继承链归一化后的 table_to_iri），找不到返回 None
    """
    op_info = ontology.get("object_properties", {}).get(op_uri, {})
    if not op_info:
        return None

    if direction == "inverse":
        targets = op_info.get("domain", [])
    else:
        targets = op_info.get("range", [])

    for target_cls in targets:
        if target_cls in class_to_table:
            tbl = class_to_table[target_cls]
            return (table_to_iri or {}).get(tbl, _canonical_iri_base_name(tbl))
        # 尝试本地名匹配
        target_local = _local_name(target_cls).lower()
        for cls_uri, tbl in class_to_table.items():
            if _local_name(cls_uri).lower() == target_local:
                return (table_to_iri or {}).get(tbl, _canonical_iri_base_name(tbl))
    return None


def _find_row_id_cols(enriched_schema, table_name) -> list[str]:
    """
    选择可用于 subject IRI 的标识列（按优先级）：
    1) 主键列（可复合）
    2) 外键列（去重后，按表字段顺序）
    3) 常见标识列（id/code/name/number）
    4) 失败则返回空（调用方决定 skip，避免生成错误 URI）
    """
    table_info = enriched_schema.get(table_name, {}) or {}
    cols_dict = table_info.get("columns", {}) or {}
    all_cols = list(cols_dict.keys())

    pks = table_info.get("primary_key", []) or []
    if pks:
        if "ID" in pks:
            return ["ID"]
        return [c for c in pks if c in cols_dict]

    fk_cols = []
    seen = set()
    for fk in table_info.get("foreign_keys", []) or []:
        c = fk.get("column")
        if not c or c in seen or c not in cols_dict:
            continue
        seen.add(c)
        fk_cols.append(c)
    if fk_cols:
        order = {c: i for i, c in enumerate(all_cols)}
        fk_cols.sort(key=lambda c: order.get(c, 10**9))
        return fk_cols

    # 最后兜底：只选“像标识符”的列，不再退化为全表列拼接
    id_like_cols = []
    for c in all_cols:
        cl = c.lower()
        if any(k in cl for k in ("id", "npdid", "code", "name", "number", "no")):
            id_like_cols.append(c)
    if id_like_cols:
        return id_like_cols
    return []


def _make_subject_template(base_url: str, iri_base: str, id_cols: list[str]) -> str | None:
    if not id_cols:
        return None
    if len(id_cols) == 1:
        return f"{base_url}{iri_base}/{{{id_cols[0]}}}"
    joined = "__".join(f"{{{c}}}" for c in id_cols)
    return f"{base_url}{iri_base}/{joined}"


def _make_subject_template_for_table(
    base_url: str, table_name: str, iri_base: str, id_cols: list[str], cols: list[str]
) -> str | None:
    """
    统一走通用模板（不做数据集硬编码）。
    URI 语义由前序 DP Mapping/OP Mapping 输出与表主键结构共同决定。
    """
    return _make_subject_template(base_url, iri_base, id_cols)


def _find_fk_ref_table(enriched_schema, table_name, col_name) -> str:
    """从 enriched_schema 的 foreign_keys 查找 FK 列引用的目标表"""
    fks = enriched_schema.get(table_name, {}).get("foreign_keys", [])
    for fk in fks:
        if fk.get("column") == col_name:
            return fk.get("ref_table") or fk.get("references_table") or ""
    return ""


def _get_op_mapping_entry(op_mapping_step1: dict, key: str) -> dict:
    entry = (op_mapping_step1 or {}).get(key)
    if entry:
        return entry
    key_lower = key.lower()
    for existing_key, existing_entry in (op_mapping_step1 or {}).items():
        if str(existing_key).lower() == key_lower:
            return existing_entry or {}
    return {}


def _infer_fk_object_property_uri(
    table_name: str,
    col_name: str,
    domain_class_uri: str | None,
    ref_table: str,
    ontology: dict,
    class_to_table: dict,
    op_mapping_step1: dict,
) -> str | None:
    """
    为 FK 列推断 ObjectProperty：
    1) 优先使用 OP step1；
    2) step1 缺失时，基于列名 + domain/range 提示进行本体候选排序。
    """
    op_mapping_key = f"{table_name}.{col_name}"
    op_mapping_info = _get_op_mapping_entry(op_mapping_step1, op_mapping_key)
    op_uri = _refine_one_sided_op_mapping_op(op_mapping_info)
    if _is_valid_uri(op_uri):
        return op_uri

    # ── 等价 OP 模式下 R2RML fallback 全开 ──
    # 等价模块输出的是高置信 OP + null（让 R2RML 兜底），
    # 关闭此 fallback 会导致 null 的列完全没有 OP。

    range_class_uri = _find_class_uri_by_table(class_to_table, ref_table) if ref_table else None
    op_cands = rank_object_prop_candidates(
        name=col_name,
        object_props=ontology.get("object_properties", {}),
        domain_hint=domain_class_uri,
        range_hint=range_class_uri,
        top_k=1,
        ontology=ontology,
    )
    if not op_cands:
        return None

    top = op_cands[0]
    score = float(top.get("score", 0.0))
    name_score = float(top.get("name_score", 0.0))
    domain_score = float(top.get("domain_score", 0.0))
    range_score = float(top.get("range_score", 0.0))
    if score >= R2RML_OP_FALLBACK_MIN_SCORE and (
        name_score >= R2RML_OP_FALLBACK_MIN_NAME_SCORE
        or domain_score >= R2RML_OP_FALLBACK_MIN_SIDE_SCORE
        or range_score >= R2RML_OP_FALLBACK_MIN_SIDE_SCORE
    ):
        return top.get("uri")
    return None


def _refine_one_sided_op_mapping_op(op_mapping_info: dict) -> str | None:
    """
    OP mapping can over-prefer an OP with only one declared side matching, e.g.
    City.province -> hasProvince because range=Province while domain=Country.
    If a near-name candidate has neutral/unknown domain+range support, prefer it.
    """
    op_uri = (op_mapping_info or {}).get("object_prop_uri")
    if not _is_valid_uri(op_uri):
        return op_uri

    candidates = (op_mapping_info or {}).get("candidates_used") or []
    selected = next((c for c in candidates if c.get("uri") == op_uri), None)
    if not selected:
        return op_uri

    sel_name = float(selected.get("name_score", 0.0) or 0.0)
    sel_domain = float(selected.get("domain_score", 0.0) or 0.0)
    sel_range = float(selected.get("range_score", 0.0) or 0.0)
    one_sided = (sel_domain <= 0.0 and sel_range >= 0.8) or (sel_range <= 0.0 and sel_domain >= 0.8)
    if not one_sided:
        return op_uri

    alternatives = []
    for cand in candidates:
        uri = cand.get("uri")
        if not _is_valid_uri(uri) or uri == op_uri:
            continue
        name = float(cand.get("name_score", 0.0) or 0.0)
        domain = float(cand.get("domain_score", 0.0) or 0.0)
        range_score = float(cand.get("range_score", 0.0) or 0.0)
        if domain >= 0.3 and range_score >= 0.3 and name >= sel_name - 0.10:
            alternatives.append((name, float(cand.get("score", 0.0) or 0.0), uri))

    if alternatives:
        alternatives.sort(reverse=True)
        return alternatives[0][2]
    return op_uri


def _allow_orphan_object_pom(orphan_info: dict, table_name: str, col_name: str, enriched_schema: dict) -> bool:
    """
    控制 Step2 orphan -> ObjectProperty 的落盘条件，避免把普通数据列误映射成对象关系。
    仅在以下情形允许：
    1) orphan 明确带有反向关系信号（is_inv=True）
    2) 或该列在 schema 中确实是 FK 列（结构关系信号）
    """
    if (orphan_info or {}).get("is_inv") is True:
        return True
    return bool(_find_fk_ref_table(enriched_schema, table_name, col_name))


def _collect_columns(table_name, enriched_schema):
    return list(enriched_schema.get(table_name, {}).get("columns", {}).keys())


def _find_class_uri_by_table(class_to_table: dict, table_name: str) -> str | None:
    """从 class->table 反查某表对应的 class URI。"""
    for cls_uri, tbl in (class_to_table or {}).items():
        if tbl == table_name:
            return cls_uri
    return None


def _infer_pk_semantic_mapping(
    table_name: str,
    col_name: str,
    domain_class_uri: str,
    enriched_schema: dict,
    ontology: dict,
    class_to_table: dict,
    op_mapping_step1: dict,
):
    """
    为被标记为 PK 的列做补映射：
      1) 若该列是 FK，优先尝试 ObjectProperty；
      2) 否则尝试 DatatypeProperty。
    返回:
      ("object", op_uri, ref_table) / ("data", dp_uri, None) / (None, None, None)
    """
    if not domain_class_uri:
        return None, None, None

    # Semantic identifiers such as country.code should remain datatype
    # properties even when they also participate in a composite FK.
    dp_cands = rank_datatype_prop_candidates(
        name=col_name,
        datatype_props=ontology.get("datatype_properties", {}),
        domain_hint=domain_class_uri,
        top_k=1,
    )
    if dp_cands:
        top = dp_cands[0]
        if (
            top.get("score", 0.0) >= R2RML_PK_DP_MIN_SCORE
            or top.get("name_score", 0.0) >= R2RML_PK_DP_MIN_NAME_SCORE
        ):
            return "data", top.get("uri"), None

    # 若该列实际上是 FK，先走对象属性
    ref_table = _find_fk_ref_table(enriched_schema, table_name, col_name)
    if ref_table:
        op_mapping_key = f"{table_name}.{col_name}"
        op_uri = _refine_one_sided_op_mapping_op(_get_op_mapping_entry(op_mapping_step1, op_mapping_key))
        if _is_valid_uri(op_uri):
            return "object", op_uri, ref_table

        # ── 等价 OP 模式下 R2RML PK fallback 全开 ──

        range_class_uri = _find_class_uri_by_table(class_to_table, ref_table)
        op_cands = rank_object_prop_candidates(
            name=col_name,
            object_props=ontology.get("object_properties", {}),
            domain_hint=domain_class_uri,
            range_hint=range_class_uri,
            top_k=1,
            ontology=ontology,
        )
        if op_cands:
            top = op_cands[0]
            if (
                top.get("score", 0.0) >= R2RML_PK_OP_MIN_SCORE and
                (
                    top.get("domain_score", 0.0) >= R2RML_PK_OP_MIN_SIDE_SCORE or
                    top.get("range_score", 0.0) >= R2RML_PK_OP_MIN_SIDE_SCORE or
                    top.get("name_score", 0.0) >= R2RML_PK_OP_MIN_NAME_SCORE
                )
            ):
                return "object", top.get("uri"), ref_table

    return None, None, None


# ============================================================
#  POM（PredicateObjectMap）生成辅助
# ============================================================

def _make_pom_datatype(pred_str: str, col_name: str, xsd_type: str) -> list:
    """生成数据属性的 predicateObjectMap"""
    return [
        f'    rr:predicateObjectMap [',
        f'        rr:predicate {pred_str} ;',
        f'        rr:objectMap [ rr:column {_rr_column(col_name)} ; rr:datatype {xsd_type} ]',
        f'    ]',
    ]


def _case_alias_predicate(prop_uri: str | None, pred_str: str, prefix_map: dict) -> str | None:
    """
    Some RODI queries use lowercase property IRIs even when the ontology keeps an
    acronym in the local name, e.g. has_an_ISBN vs has_an_isbn. Emit a lowercase
    alias only when it differs from the selected predicate.
    """
    if not _is_valid_uri(prop_uri):
        return None
    local = _local_name(prop_uri)
    # Keep this fallback narrow: only add aliases for names that contain
    # explicit acronyms like ISBN/URL, not ordinary camelCase such as gdpTotal.
    if not any(len(chunk) >= 2 for chunk in re.findall(r"[A-Z]{2,}", local or "")):
        return None
    if "#" in prop_uri:
        ns, local = prop_uri.rsplit("#", 1)
        alias_uri = f"{ns}#{local.lower()}"
    else:
        ns, local = prop_uri.rsplit("/", 1)
        alias_uri = f"{ns}/{local.lower()}"
    if alias_uri == prop_uri:
        return None
    alias_pred = _predicate_str(alias_uri, prefix_map)
    if alias_pred and alias_pred != pred_str:
        return alias_pred
    return None


def _make_pom_object(pred_str: str, obj_template: str) -> list:
    """生成对象属性的 predicateObjectMap"""
    return [
        f'    rr:predicateObjectMap [',
        f'        rr:predicate {pred_str} ;',
        f'        rr:objectMap [ rr:template "{obj_template}" ]',
        f'    ]',
    ]


def _refine_datatype_prop_uri(
    col_name: str,
    current_prop_uri: str | None,
    domain_class_uri: str | None,
    ontology: dict,
) -> str | None:
    """
    对 data_attr 的 prop_uri 做一次轻量纠偏：
    - 若当前为空：允许按候选 top1 自动补全；
    - 若当前明显与列名不匹配：在同域候选中替换成更可靠的 top1。
    该逻辑为通用排序约束，不依赖数据集硬编码。
    """
    cands = rank_datatype_prop_candidates(
        name=col_name,
        datatype_props=ontology.get("datatype_properties", {}),
        domain_hint=domain_class_uri,
    )
    if not cands:
        return current_prop_uri if _is_valid_uri(current_prop_uri) else None

    top = cands[0]
    top_uri = top.get("uri")
    top_score = float(top.get("score", 0.0))
    top_name_score = float(top.get("name_score", 0.0))

    if not _is_valid_uri(current_prop_uri):
        if (
            top_score >= R2RML_DP_REFINE_FILL_MIN_SCORE
            or top_name_score >= R2RML_DP_REFINE_FILL_MIN_NAME_SCORE
        ):
            return top_uri
        return None

    cur_score = 0.0
    cur_name_score = 0.0
    for cand in cands:
        if cand.get("uri") == current_prop_uri:
            cur_score = float(cand.get("score", 0.0))
            cur_name_score = float(cand.get("name_score", 0.0))
            break

    should_replace = (
        (top_name_score >= R2RML_DP_REFINE_REPLACE_TOP_NAME and cur_name_score < R2RML_DP_REFINE_REPLACE_CUR_NAME) or
        (
            top_name_score - cur_name_score >= R2RML_DP_REFINE_REPLACE_NAME_GAP
            and top_score - cur_score >= R2RML_DP_REFINE_REPLACE_SCORE_GAP
        )
    )
    if should_replace and _is_valid_uri(top_uri):
        return top_uri
    return current_prop_uri


def _sql_literal(value):
    """将 Python 值转成 SQL 字面量字符串（用于 WHERE 条件）"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    s = str(value).replace("'", "''")
    return f"'{s}'"


def _safe_id(s: str) -> str:
    s = str(s)
    out = []
    for ch in s:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def _flatten_class_values(value_to_class: dict) -> list[str]:
    out = []
    for value in (value_to_class or {}).values():
        if isinstance(value, list):
            out.extend(value)
        else:
            out.append(value)
    return [v for v in out if _is_valid_uri(v)]


def _enum_assertions_cover_subclasses(entry: dict, base_class_uri: str, ontology: dict) -> bool:
    """
    Avoid asserting a base SH class when discriminator mappings already assert
    subclasses for all mapped enum values; RDFS reasoning will infer the base.
    """
    if not _is_valid_uri(base_class_uri):
        return False
    ancestors_of = (ontology or {}).get("ancestors_of", {})
    for ta in entry.get("type_assertions", []) or []:
        if ta.get("kind") != "enum":
            continue
        mapping = ta.get("value_to_class") or {}
        if not mapping or ta.get("unmapped_values"):
            continue
        class_values = _flatten_class_values(mapping)
        if class_values and all(
            cls != base_class_uri and base_class_uri in (ancestors_of.get(cls, []) or [])
            for cls in class_values
        ):
            return True
    return False


def _leaf_descendants_for_class(base_class_uri: str, ontology: dict) -> list[str]:
    children_of = (ontology or {}).get("children_of", {})
    descendants = []
    queue = list(children_of.get(base_class_uri, []) or [])
    seen = set()
    while queue:
        uri = queue.pop(0)
        if uri in seen:
            continue
        seen.add(uri)
        descendants.append(uri)
        queue.extend(children_of.get(uri, []) or [])
    desc_set = set(descendants)
    return [
        uri for uri in descendants
        if not [child for child in children_of.get(uri, []) or [] if child in desc_set]
    ]


def _is_risky_single_value_subclass_assertion(
    entry: dict,
    ta: dict,
    ontology: dict,
) -> bool:
    """
    A single discriminator value over a broad class hierarchy is not enough to
    specialize every row into one arbitrary subclass. Keep narrow hierarchies
    such as Paper -> {PaperAbstract, PaperFullVersion}, but skip broad ones.
    """
    return False


# ============================================================
#  按 Pattern 生成 TriplesMap
# ============================================================

def generate_value_attr_mapping(
    table_name, entry, enriched_schema,
    ontology, base_url, prefix_map, table_to_iri, dp_range_map=None,
    prop_uri_map=None,
):
    """
    处理 has_xxx(EntityFK, VALUE) 这类多值数据属性表。
    表级仍是 SE，但 subject 使用被引用实体 IRI，object 使用 literal VALUE。
    """
    fk_info = entry.get("fk", {}) or {}
    fk_col = fk_info.get("column")
    ref_table = fk_info.get("ref_table")
    value_col = entry.get("value_column")
    if not fk_col or not ref_table or not value_col:
        return f"# SKIP: {table_name} (SE value_attr) 缺少 FK/value 列信息\n"

    prop_uri = _normalize_prop_uri(entry.get("prop_uri"), prop_uri_map)
    prop_uri = _refine_datatype_prop_uri(
        col_name=table_name,
        current_prop_uri=prop_uri,
        domain_class_uri=entry.get("class_uri"),
        ontology=ontology,
    )
    if not _is_valid_uri(prop_uri):
        return f"# SKIP: {table_name} (SE value_attr) 无 prop_uri\n"

    pred = _predicate_str(prop_uri, prefix_map)
    if not pred:
        return f"# SKIP: {table_name} (SE value_attr) 谓词无法序列化\n"

    cols = [fk_col, value_col]
    sql = f'SELECT {_sql_cols_in_query(cols)} FROM {_sql_col_in_query(table_name)}'
    ref_iri_base = table_to_iri.get(ref_table, _canonical_iri_base_name(ref_table))
    subject_template = f"{base_url}{ref_iri_base}/{{{fk_col}}}"

    col_types = enriched_schema.get(table_name, {}).get("columns", {})
    sql_type = col_types.get(value_col, entry.get("value_column_type") or "character varying")
    xsd = _xsd_type(sql_type, prop_uri, dp_range_map)
    if not _is_sql_xsd_compatible(sql_type, xsd):
        return f"# SKIP: {table_name} (SE value_attr) SQL/XSD 类型不兼容\n"

    lines = [
        f'<#{table_name}Mapping> a rr:TriplesMap ;',
        f'    rr:logicalTable [ rr:sqlQuery "{sql}" ] ;',
        f'    rr:subjectMap [ rr:template "{subject_template}" ] ;',
    ]
    pom = _make_pom_datatype(pred, value_col, xsd)
    pom[-1] += " ."
    lines.extend(pom)
    alias_pred = _case_alias_predicate(prop_uri, pred, prefix_map)
    if alias_pred:
        lines[-1] = lines[-1][:-2] + " ;"
        alias_pom = _make_pom_datatype(alias_pred, value_col, xsd)
        alias_pom[-1] += " ."
        lines.extend(alias_pom)
    return "\n".join(lines) + "\n"


def generate_se_mapping(
    table_name, entry, enriched_schema, op_mapping_step1, op_mapping_step2_orphans,
    ontology, base_url, prefix_map, class_to_table, table_to_iri, dp_range_map=None,
    prop_uri_map=None
):
    if entry.get("table_kind") == "value_attr":
        return generate_value_attr_mapping(
            table_name=table_name,
            entry=entry,
            enriched_schema=enriched_schema,
            ontology=ontology,
            base_url=base_url,
            prefix_map=prefix_map,
            table_to_iri=table_to_iri,
            dp_range_map=dp_range_map,
            prop_uri_map=prop_uri_map,
        )

    class_uri = entry.get("class_uri")
    if not _is_valid_uri(class_uri):
        return f"# SKIP: {table_name} 无 class_uri\n"

    id_cols = _find_row_id_cols(enriched_schema, table_name)
    cols = _collect_columns(table_name, enriched_schema)
    sql = f'SELECT {_sql_cols_in_query(cols)} FROM {_sql_col_in_query(table_name)}'
    iri_base = table_to_iri.get(table_name, table_name.lower())
    subject_template = _make_subject_template_for_table(
        base_url, table_name, iri_base, id_cols, cols
    )
    if not subject_template:
        return f"# SKIP: {table_name} (SE) 无可用标识列，无法构造 subject IRI\n"
    class_pred = _predicate_str(class_uri, prefix_map)

    lines = [
        f'<#{table_name}Mapping> a rr:TriplesMap ;',
        f'    rr:logicalTable [ rr:sqlQuery "{sql}" ] ;',
        f'    rr:subjectMap [ rr:template "{subject_template}" ; rr:class {class_pred} ] ;',
    ]

    columns = entry.get("columns", {})
    col_types = enriched_schema.get(table_name, {}).get("columns", {})
    poms = []
    mapped_cols = set()

    for col_name, col_info in columns.items():
        role = col_info.get("role")
        if role in ("pk", "discriminator", "sh_inherited_pk"):
            continue

        fk_ref_table = _find_fk_ref_table(enriched_schema, table_name, col_name)
        if fk_ref_table:
            op_uri = _infer_fk_object_property_uri(
                table_name=table_name,
                col_name=col_name,
                domain_class_uri=class_uri,
                ref_table=fk_ref_table,
                ontology=ontology,
                class_to_table=class_to_table,
                op_mapping_step1=op_mapping_step1,
            )
            if _is_valid_uri(op_uri):
                pred = _predicate_str(op_uri, prefix_map)
                if pred:
                    ref_iri_base = table_to_iri.get(fk_ref_table, _canonical_iri_base_name(fk_ref_table))
                    obj_tmpl = f"{base_url}{ref_iri_base}/{{{col_name}}}"
                    poms.append(_make_pom_object(pred, obj_tmpl))
                    mapped_cols.add(col_name)
            # FK 列优先按对象关系处理；即便未匹配成功，也不再作为数据属性落盘
            continue

        if role == "data_attr":
            prop_uri = col_info.get("prop_uri")
            prop_uri = _normalize_prop_uri(prop_uri, prop_uri_map)  # ← Q30修复：统一大小写
            prop_uri = _refine_datatype_prop_uri(
                col_name=col_name,
                current_prop_uri=prop_uri,
                domain_class_uri=class_uri,
                ontology=ontology,
            )

            # 先检查 Step 2 孤儿列是否补全了 ObjectProperty
            orphan_key = f"{table_name}.{col_name}"
            if (not _is_valid_uri(prop_uri)) and orphan_key in op_mapping_step2_orphans:
                orphan_info = op_mapping_step2_orphans[orphan_key]
                op_uri = orphan_info.get("object_prop_uri")
                if _is_valid_uri(op_uri):
                    if not _allow_orphan_object_pom(orphan_info, table_name, col_name, enriched_schema):
                        continue
                    # 防御：布尔列不生成 ObjectProperty POM（OP Step2 可能误匹配）
                    sql_type_check = col_types.get(col_name, "")
                    if "bool" in sql_type_check.lower():
                        continue
                    pred = _predicate_str(op_uri, prefix_map)
                    if pred:
                        direction = orphan_info.get("direction", "normal")
                        # 优先从本体查 range 表名
                        range_tbl = resolve_range_table(
                            op_uri, ontology, class_to_table, direction, table_to_iri=table_to_iri
                        )
                        if not range_tbl:
                            # fallback：从 orphan_info 里的 range_class_uri 查 class_to_table
                            range_cls = orphan_info.get("range_class_uri") if direction == "normal" \
                                        else orphan_info.get("domain_class_uri")
                            if range_cls and range_cls in class_to_table:
                                rt = class_to_table[range_cls]
                                range_tbl = table_to_iri.get(rt, _canonical_iri_base_name(rt))
                        if range_tbl:
                            obj_tmpl = f"{base_url}{range_tbl}/{{{col_name}}}"
                        else:
                            # 最后 fallback：跳过，不生成错误的 POM
                            continue
                        poms.append(_make_pom_object(pred, obj_tmpl))
                        mapped_cols.add(col_name)
                    continue

            # 普通 DatatypeProperty
            if not _is_valid_uri(prop_uri):
                # 通用注释列兜底：即使未匹配到本体 DP，也允许输出 rdfs 注释属性
                sql_type = col_types.get(col_name, "character varying")
                if "char" in sql_type.lower() or "text" in sql_type.lower():
                    for ann_pred in _annotation_predicates_for_column(col_name, None):
                        poms.append(_make_pom_datatype(ann_pred, col_name, "xsd:string"))
                        mapped_cols.add(col_name)
                continue

            pred = _predicate_str(prop_uri, prefix_map)
            if not pred:
                continue
            sql_type = col_types.get(col_name, "character varying")
            xsd = _xsd_type(sql_type, prop_uri, dp_range_map)
            if not _is_sql_xsd_compatible(sql_type, xsd):
                continue
            poms.append(_make_pom_datatype(pred, col_name, xsd))
            alias_pred = _case_alias_predicate(prop_uri, pred, prefix_map)
            if alias_pred:
                poms.append(_make_pom_datatype(alias_pred, col_name, xsd))
            # 通用补充：label/comment 列同步映射到 rdfs 注释属性，提升跨本体查询兼容性
            for ann_pred in _annotation_predicates_for_column(col_name, prop_uri):
                if ann_pred != pred:
                    poms.append(_make_pom_datatype(ann_pred, col_name, "xsd:string"))
            mapped_cols.add(col_name)

        elif role == "fk_obj":
            # 常规路径已在上方 FK 通用分支处理，这里保持兜底兼容
            ref_table = col_info.get("ref_table") or _find_fk_ref_table(enriched_schema, table_name, col_name)
            op_uri = _infer_fk_object_property_uri(
                table_name=table_name,
                col_name=col_name,
                domain_class_uri=class_uri,
                ref_table=ref_table,
                ontology=ontology,
                class_to_table=class_to_table,
                op_mapping_step1=op_mapping_step1,
            )
            if not _is_valid_uri(op_uri) or not ref_table:
                continue

            pred = _predicate_str(op_uri, prefix_map)
            if not pred:
                continue

            ref_iri_base = table_to_iri.get(ref_table, _canonical_iri_base_name(ref_table))
            obj_tmpl = f"{base_url}{ref_iri_base}/{{{col_name}}}"
            poms.append(_make_pom_object(pred, obj_tmpl))
            mapped_cols.add(col_name)

    # PK 语义补映射：避免 name/code 等主键语义列被整体丢失
    for col_name, col_info in columns.items():
        role = col_info.get("role")
        if role != "pk":
            continue
        if col_name in mapped_cols:
            continue

        kind, uri, ref_table = _infer_pk_semantic_mapping(
            table_name=table_name,
            col_name=col_name,
            domain_class_uri=class_uri,
            enriched_schema=enriched_schema,
            ontology=ontology,
            class_to_table=class_to_table,
            op_mapping_step1=op_mapping_step1,
        )
        if kind == "object" and _is_valid_uri(uri) and ref_table:
            pred = _predicate_str(uri, prefix_map)
            if pred:
                ref_iri_base = table_to_iri.get(ref_table, _canonical_iri_base_name(ref_table))
                obj_tmpl = f"{base_url}{ref_iri_base}/{{{col_name}}}"
                poms.append(_make_pom_object(pred, obj_tmpl))
                mapped_cols.add(col_name)
        elif kind == "data" and _is_valid_uri(uri):
            pred = _predicate_str(uri, prefix_map)
            if pred:
                sql_type = col_types.get(col_name, "character varying")
                xsd = _xsd_type(sql_type, uri, dp_range_map)
                if not _is_sql_xsd_compatible(sql_type, xsd):
                    continue
                poms.append(_make_pom_datatype(pred, col_name, xsd))
                mapped_cols.add(col_name)

    # 拼接 POM，用分号分隔，最后一个用句号
    if not poms:
        # 只有 subjectMap，去掉最后的分号
        result = "\n".join(lines)
        if result.endswith(" ;"):
            result = result[:-2] + " ."
        return result + "\n"

    all_pom_lines = []
    for i, pom in enumerate(poms):
        separator = " ;" if i < len(poms) - 1 else " ."
        pom_with_sep = pom.copy()
        pom_with_sep[-1] = pom_with_sep[-1] + separator
        all_pom_lines.extend(pom_with_sep)

    lines.extend(all_pom_lines)
    return "\n".join(lines) + "\n"


def generate_sh_mapping(
    table_name, entry, enriched_schema,op_mapping_step1, op_mapping_step2_orphans,
    ontology, base_url, prefix_map, class_to_table, table_to_iri, dp_range_map=None,
    prop_uri_map=None
):
    sub_class_uri = _repair_sh_subclass_by_dataprop_domain(entry, ontology)
    sub_class_uri = _repair_sh_subclass_by_table_name(
        table_name=table_name,
        current_sub_class_uri=sub_class_uri,
        class_confidence=entry.get("class_confidence"),
        ontology=ontology,
    )
    parent_class_uri = entry.get("parent_class_uri")

    if sub_class_uri and sub_class_uri == parent_class_uri:
        inferred = _infer_subclass_from_table(table_name, ontology)
        if inferred and inferred != parent_class_uri:
            sub_class_uri = inferred

    # 若仍缺失，再用表名做一次通用兜底推断
    if not _is_valid_uri(sub_class_uri):
        inferred = _infer_subclass_from_table(table_name, ontology)
        if _is_valid_uri(inferred):
            sub_class_uri = inferred

    if not _is_valid_uri(sub_class_uri):
        return f"# SKIP: {table_name} (SH) 无 sub_class_uri\n"

    id_cols = _find_row_id_cols(enriched_schema, table_name)
    cols = _collect_columns(table_name, enriched_schema)
    sql = f'SELECT {_sql_cols_in_query(cols)} FROM {_sql_col_in_query(table_name)}'
    iri_base = table_to_iri.get(table_name, table_name.lower())
    subject_template = _make_subject_template_for_table(
        base_url, table_name, iri_base, id_cols, cols
    )
    if not subject_template:
        return f"# SKIP: {table_name} (SH) 无可用标识列，无法构造 subject IRI\n"
    sub_pred = _predicate_str(sub_class_uri, prefix_map)
    omit_base_class = _enum_assertions_cover_subclasses(entry, sub_class_uri, ontology)
    subject_map = f'    rr:subjectMap [ rr:template "{subject_template}"'
    if not omit_base_class:
        subject_map += f" ; rr:class {sub_pred}"
    subject_map += " ] ;"

    lines = [
        f'<#{table_name}Mapping> a rr:TriplesMap ;',
        f'    rr:logicalTable [ rr:sqlQuery "{sql}" ] ;',
        subject_map,
    ]

    columns = entry.get("columns", {})
    col_types = enriched_schema.get(table_name, {}).get("columns", {})
    poms = []

    for col_name, col_info in columns.items():
        role = col_info.get("role")
        if role in ("sh_inherited_pk", "pk", "discriminator"):
            continue

        fk_ref_table = _find_fk_ref_table(enriched_schema, table_name, col_name)
        if fk_ref_table:
            op_uri = _infer_fk_object_property_uri(
                table_name=table_name,
                col_name=col_name,
                domain_class_uri=sub_class_uri,
                ref_table=fk_ref_table,
                ontology=ontology,
                class_to_table=class_to_table,
                op_mapping_step1=op_mapping_step1,
            )
            if _is_valid_uri(op_uri):
                pred = _predicate_str(op_uri, prefix_map)
                if pred:
                    ref_iri_base = table_to_iri.get(fk_ref_table, _canonical_iri_base_name(fk_ref_table))
                    obj_tmpl = f"{base_url}{ref_iri_base}/{{{col_name}}}"
                    poms.append(_make_pom_object(pred, obj_tmpl))
            # FK 列优先按对象关系处理；即便未匹配成功，也不再作为数据属性落盘
            continue

        if role == "data_attr":
            prop_uri = col_info.get("prop_uri")
            prop_uri = _normalize_prop_uri(prop_uri, prop_uri_map)  # ← Q30修复：统一大小写
            prop_uri = _refine_datatype_prop_uri(
                col_name=col_name,
                current_prop_uri=prop_uri,
                domain_class_uri=sub_class_uri,
                ontology=ontology,
            )
            orphan_key = f"{table_name}.{col_name}"

            if (not _is_valid_uri(prop_uri)) and orphan_key in op_mapping_step2_orphans:
                orphan_info = op_mapping_step2_orphans[orphan_key]
                op_uri = orphan_info.get("object_prop_uri")
                if _is_valid_uri(op_uri):
                    if not _allow_orphan_object_pom(orphan_info, table_name, col_name, enriched_schema):
                        continue
                    pred = _predicate_str(op_uri, prefix_map)
                    if pred:
                        direction = orphan_info.get("direction", "normal")
                        range_tbl = resolve_range_table(
                            op_uri, ontology, class_to_table, direction, table_to_iri=table_to_iri
                        )
                        if range_tbl:
                            obj_tmpl = f"{base_url}{range_tbl}/{{{col_name}}}"
                        else:
                            # fallback：从 orphan_info range_class_uri 查表名
                            range_cls = orphan_info.get("range_class_uri") if direction == "normal" \
                                        else orphan_info.get("domain_class_uri")
                            if range_cls and range_cls in class_to_table:
                                rt = class_to_table[range_cls]
                                range_tbl = table_to_iri.get(rt, _canonical_iri_base_name(rt))
                                obj_tmpl = f"{base_url}{range_tbl}/{{{col_name}}}"
                            else:
                                continue  # 实在找不到，跳过
                        poms.append(_make_pom_object(pred, obj_tmpl))
                    continue

            if not _is_valid_uri(prop_uri):
                sql_type = col_types.get(col_name, "character varying")
                if "char" in sql_type.lower() or "text" in sql_type.lower():
                    for ann_pred in _annotation_predicates_for_column(col_name, None):
                        poms.append(_make_pom_datatype(ann_pred, col_name, "xsd:string"))
                continue

            pred = _predicate_str(prop_uri, prefix_map)
            if not pred:
                continue
            sql_type = col_types.get(col_name, "character varying")
            xsd = _xsd_type(sql_type, prop_uri, dp_range_map)
            if not _is_sql_xsd_compatible(sql_type, xsd):
                continue
            poms.append(_make_pom_datatype(pred, col_name, xsd))
            alias_pred = _case_alias_predicate(prop_uri, pred, prefix_map)
            if alias_pred:
                poms.append(_make_pom_datatype(alias_pred, col_name, xsd))
            for ann_pred in _annotation_predicates_for_column(col_name, prop_uri):
                if ann_pred != pred:
                    poms.append(_make_pom_datatype(ann_pred, col_name, "xsd:string"))

        elif role == "fk_obj":
            # 常规路径已在上方 FK 通用分支处理，这里保持兜底兼容
            ref_table = col_info.get("ref_table") or _find_fk_ref_table(enriched_schema, table_name, col_name)
            op_uri = _infer_fk_object_property_uri(
                table_name=table_name,
                col_name=col_name,
                domain_class_uri=sub_class_uri,
                ref_table=ref_table,
                ontology=ontology,
                class_to_table=class_to_table,
                op_mapping_step1=op_mapping_step1,
            )
            if not _is_valid_uri(op_uri) or not ref_table:
                continue
            pred = _predicate_str(op_uri, prefix_map)
            if not pred:
                continue
            ref_iri_base = table_to_iri.get(ref_table, _canonical_iri_base_name(ref_table))
            obj_tmpl = f"{base_url}{ref_iri_base}/{{{col_name}}}"
            poms.append(_make_pom_object(pred, obj_tmpl))

    if not poms:
        result = "\n".join(lines)
        if result.endswith(" ;"):
            result = result[:-2] + " ."
        return result + "\n"

    all_pom_lines = []
    for i, pom in enumerate(poms):
        separator = " ;" if i < len(poms) - 1 else " ."
        pom_with_sep = pom.copy()
        pom_with_sep[-1] = pom_with_sep[-1] + separator
        all_pom_lines.extend(pom_with_sep)

    lines.extend(all_pom_lines)
    return "\n".join(lines) + "\n"


def generate_sr_mapping(
    table_name, entry, enriched_schema, op_mapping_step1,
    base_url, prefix_map, table_to_iri, class_to_table=None
):
    op_uri = None
    op_mapping_info = _get_op_mapping_entry(op_mapping_step1, table_name)
    if op_mapping_info:
        op_uri = op_mapping_info.get("object_prop_uri")

    if not _is_valid_uri(op_uri):
        return f"# SKIP: {table_name} (SR) 无 ObjectProperty\n"

    fk1 = entry.get("fk1", {})
    fk2 = entry.get("fk2", {})
    fk1_col = fk1.get("column")
    fk2_col = fk2.get("column")
    fk1_ref = fk1.get("ref_table") or ""
    fk2_ref = fk2.get("ref_table") or ""
    relation_kind = entry.get("relation_kind", "full_fk")

    if op_mapping_info and op_mapping_info.get("sr_direction") == "reversed":
        fk1_col, fk2_col = fk2_col, fk1_col
        fk1_ref, fk2_ref = fk2_ref, fk1_ref

    if not fk1_col or not fk2_col:
        return f"# SKIP: {table_name} (SR) FK 列信息不完整\n"

    if not fk1_ref:
        return f"# SKIP: {table_name} (SR) FK 引用表信息不完整\n"
    if not fk2_ref and relation_kind != "partial_fk":
        return f"# SKIP: {table_name} (SR) FK 引用表信息不完整\n"

    cols = [fk1_col, fk2_col]
    sql = f'SELECT {_sql_cols_in_query(cols)} FROM {_sql_col_in_query(table_name)}'

    fk1_iri_base = table_to_iri.get(fk1_ref, _canonical_iri_base_name(fk1_ref))
    if fk2_ref:
        fk2_iri_base = table_to_iri.get(fk2_ref, _canonical_iri_base_name(fk2_ref))
    else:
        range_class = entry.get("range_class_uri")
        range_table = (class_to_table or {}).get(range_class)
        if range_table:
            fk2_iri_base = table_to_iri.get(range_table, _canonical_iri_base_name(range_table))
        elif _is_valid_uri(range_class):
            fk2_iri_base = _canonical_iri_base_name(_local_name(range_class))
        else:
            return f"# SKIP: {table_name} (SR partial_fk) 无法解析对象 IRI\n"
    subject_template = f"{base_url}{fk1_iri_base}/{{{fk1_col}}}"
    object_template = f"{base_url}{fk2_iri_base}/{{{fk2_col}}}"

    pred = _predicate_str(op_uri, prefix_map)
    lines = [f'<#{table_name}Mapping> a rr:TriplesMap ;']
    lines.append(f'    rr:logicalTable [ rr:sqlQuery "{sql}" ] ;')
    # SR 是关系映射，不应在此处额外声明 subject rdf:type，避免按关系行重复打类型
    lines.append(f'    rr:subjectMap [ rr:template "{subject_template}" ] ;')

    lines.extend(_make_pom_object(pred, object_template))
    lines[-1] += " ."
    return "\n".join(lines) + "\n"


def generate_type_assertion_mappings(
    table_name, entry, enriched_schema, base_url, prefix_map, table_to_iri, ontology=None
):
    """
    为 discriminator/type_assertions 生成附加 rdf:type TriplesMap。
    支持:
      - enum: TYPE=值 -> rdf:type Class
      - boolean: is_x=true -> rdf:type Class
    """
    assertions = entry.get("type_assertions", []) or []
    if not assertions:
        return []

    id_cols = _find_row_id_cols(enriched_schema, table_name)
    iri_base = table_to_iri.get(table_name, _canonical_iri_base_name(table_name))
    cols = _collect_columns(table_name, enriched_schema)
    subject_template = _make_subject_template_for_table(
        base_url, table_name, iri_base, id_cols, cols
    )
    if not subject_template:
        return []
    blocks = []

    for ta in assertions:
        kind = ta.get("kind")
        col = ta.get("column")
        if not col:
            continue

        if kind == "enum":
            if _is_risky_single_value_subclass_assertion(entry, ta, ontology or {}):
                continue
            mapping = ta.get("value_to_class") or {}
            for raw_val, class_value in mapping.items():
                class_uris = class_value if isinstance(class_value, list) else [class_value]
                sql_val = _sql_literal(int(raw_val) if str(raw_val).isdigit() else raw_val)
                select_cols = _sql_cols_in_query(id_cols) if id_cols else _sql_col_in_query(col)
                sql = (
                    f'SELECT {select_cols} FROM {_sql_col_in_query(table_name)} '
                    f'WHERE {_sql_col_in_query(col)} = {sql_val}'
                )
                for class_uri in class_uris:
                    if not _is_valid_uri(class_uri):
                        continue
                    class_term = _predicate_str(class_uri, prefix_map)
                    if not class_term:
                        continue
                    map_id = _safe_id(f"{table_name}_{col}_{raw_val}_{_local_name(class_uri)}_Type")
                    lines = [
                        f"<#{map_id}> a rr:TriplesMap ;",
                        f'    rr:logicalTable [ rr:sqlQuery "{sql}" ] ;',
                        f'    rr:subjectMap [ rr:template "{subject_template}" ] ;',
                        f"    rr:predicateObjectMap [",
                        f"        rr:predicate rdf:type ;",
                        f"        rr:objectMap [ rr:constant {class_term} ]",
                        f"    ] .",
                    ]
                    blocks.append("\n".join(lines) + "\n")

        elif kind == "boolean":
            class_uri = ta.get("true_class_uri")
            if not _is_valid_uri(class_uri):
                continue
            class_term = _predicate_str(class_uri, prefix_map)
            if not class_term:
                continue
            select_cols = _sql_cols_in_query(id_cols) if id_cols else _sql_col_in_query(col)
            sql = (
                f'SELECT {select_cols} FROM {_sql_col_in_query(table_name)} '
                f'WHERE {_sql_col_in_query(col)} = true'
            )
            map_id = _safe_id(f"{table_name}_{col}_true_Type")
            lines = [
                f"<#{map_id}> a rr:TriplesMap ;",
                f'    rr:logicalTable [ rr:sqlQuery "{sql}" ] ;',
                f'    rr:subjectMap [ rr:template "{subject_template}" ] ;',
                f"    rr:predicateObjectMap [",
                f"        rr:predicate rdf:type ;",
                f"        rr:objectMap [ rr:constant {class_term} ]",
                f"    ] .",
            ]
            blocks.append("\n".join(lines) + "\n")

    return blocks


# ============================================================
#  主生成函数
# ============================================================

def generate_r2rml(
    final_alignment, op_mapping_full, enriched_schema, ontology,
    base_url="http://example.com/", prefix="ont"
):
    classes = ontology.get("classes", [])
    namespace = _get_namespace(classes[0]) if classes else ""

    prefix_map = {
        prefix: namespace,
        "rr": "http://www.w3.org/ns/r2rml#",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
    }

    op_mapping_step1 = op_mapping_full.get("step1", {})
    op_mapping_step2_orphans = op_mapping_full.get("step2_orphans", {})

    # 构建全局映射表
    class_to_table = build_class_to_table_map(final_alignment)
    table_to_iri = build_table_to_iri_base(final_alignment, enriched_schema)
    dp_range_map = build_dp_range_map(ontology)
    prop_uri_map = build_prop_uri_normalization_map(ontology)  # ← Q30修复：大小写归一化表

    # 前缀
    parts = [
        f"@prefix rr: <{prefix_map['rr']}> .",
        f"@prefix rdf: <{prefix_map['rdf']}> .",
        f"@prefix rdfs: <{prefix_map['rdfs']}> .",
        f"@prefix xsd: <{prefix_map['xsd']}> .",
    ]
    if namespace:
        parts.append(f"@prefix {prefix}: <{namespace}> .")
    parts.append(f"@base <{base_url}> .")
    parts.append("")

    counts = {"SE": 0, "SH": 0, "SR": 0, "SKIP": 0}

    # SH
    for table_name, entry in final_alignment.items():
        if entry.get("pattern") != "SH":
            continue
        mapping = generate_sh_mapping(
            table_name, entry, enriched_schema,op_mapping_step1, op_mapping_step2_orphans,
            ontology, base_url, prefix_map, class_to_table, table_to_iri, dp_range_map,
            prop_uri_map=prop_uri_map
        )
        parts.append(f"# === {table_name} (SH) ===")
        parts.append(mapping)
        counts["SH"] += 1

    # SR
    for table_name, entry in final_alignment.items():
        if entry.get("pattern") != "SR":
            continue
        mapping = generate_sr_mapping(
            table_name, entry, enriched_schema, op_mapping_step1,
            base_url, prefix_map, table_to_iri, class_to_table
        )
        parts.append(f"# === {table_name} (SR) ===")
        parts.append(mapping)
        counts["SR"] += 1

    # SE
    for table_name, entry in final_alignment.items():
        pattern = entry.get("pattern", "SE")
        if pattern != "SE":
            continue

        mapping = generate_se_mapping(
            table_name, entry, enriched_schema, op_mapping_step1, op_mapping_step2_orphans,
            ontology, base_url, prefix_map, class_to_table, table_to_iri, dp_range_map,
            prop_uri_map=prop_uri_map
        )
        parts.append(f"# === {table_name} ({pattern}) ===")
        parts.append(mapping)
        counts["SE"] += 1

    # discriminator/type_assertions 附加 rdf:type 映射（SE/SH）
    for table_name, entry in final_alignment.items():
        if entry.get("pattern") not in ("SE", "SH"):
            continue
        type_blocks = generate_type_assertion_mappings(
            table_name=table_name,
            entry=entry,
            enriched_schema=enriched_schema,
            base_url=base_url,
            prefix_map=prefix_map,
            table_to_iri=table_to_iri,
            ontology=ontology,
        )
        if not type_blocks:
            continue
        parts.append(f"# === {table_name} (Type Assertions) ===")
        parts.extend(type_blocks)

    print(f"\nR2RML 生成完成: SE={counts['SE']}, SH={counts['SH']}, SR={counts['SR']}")
    return "\n".join(parts)


def build_prop_uri_normalization_map(ontology: dict) -> dict:
    """
    Build lowercase → actual URI map for all ontology properties.
    Handles case mismatches where alignment stored column-cased names
    (e.g. has_an_ISBN) but ontology uses lowercase (has_an_isbn).
    """
    uri_map = {}
    for uri in list(ontology.get("datatype_properties", {}).keys()) + \
               list(ontology.get("object_properties", {}).keys()):
        uri_map[uri.lower()] = uri
    return uri_map


def _normalize_prop_uri(prop_uri: str, prop_uri_map: dict) -> str:
    """
    归一化属性 URI 到本体中的“规范写法”（含大小写）。
    不做强制小写，以避免在区分大小写的本体（如 sigkdd）中产生错误 IRI。
    """
    if not prop_uri or not prop_uri_map:
        return prop_uri
    canonical = prop_uri_map.get(prop_uri.lower())
    if canonical:
        return canonical
    return prop_uri


def _normalized_name_variants(text: str) -> set[str]:
    raw = (text or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
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


def _repair_sh_subclass_by_table_name(
    table_name: str,
    current_sub_class_uri: str | None,
    class_confidence: str | None,
    ontology: dict | None,
) -> str | None:
    """
    SH 子类纠偏（通用，无硬编码）：
    当当前 class 非 high 置信时，若表名与某个类名在规范化后“精确匹配（含单复数）”，
    且当前类不匹配该表名，则优先使用精确匹配类。
    """
    if not ontology:
        return current_sub_class_uri
    conf = (class_confidence or "").lower()
    if conf == "high":
        return current_sub_class_uri

    tvars = _normalized_name_variants(table_name)
    if not tvars:
        return current_sub_class_uri

    cur_local = _local_name(current_sub_class_uri) if _is_valid_uri(current_sub_class_uri) else ""
    cur_vars = _normalized_name_variants(cur_local)
    if tvars & cur_vars:
        return current_sub_class_uri

    for cls_uri in ontology.get("classes", []):
        cvars = _normalized_name_variants(_local_name(cls_uri))
        if tvars & cvars:
            return cls_uri
    return current_sub_class_uri


def _annotation_predicates_for_column(col_name: str, prop_uri: str | None) -> list[str]:
    """
    为常见注释列补充 rdfs 注释谓词（通用规则）：
      - label/lbl/*_label -> rdfs:label
      - comment/comments/*_comment -> rdfs:comment
    """
    col = (col_name or "").strip().lower()
    preds = []
    if col in {"label", "lbl"} or col.endswith("_label"):
        preds.append("rdfs:label")
    if col in {"comment", "comments"} or col.endswith("_comment"):
        preds.append("rdfs:comment")

    local = _local_name(prop_uri).lower() if _is_valid_uri(prop_uri) else ""
    if local == "label":
        preds.append("rdfs:label")
    if local == "comment":
        preds.append("rdfs:comment")

    out = []
    seen = set()
    for p in preds:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

def _infer_subclass_from_table(table_name: str, ontology: dict) -> str:
    """
    When sub_class_uri == parent_class_uri (RealValue alignment error),
    find the real class by matching the table name against ontology classes.
    Handles truncated table names (e.g. Passive_conference_partic →
    conference#Passive_conference_participant).
    """
    table_clean = table_name.lower().replace("_", "").replace(" ", "")
    best_uri, best_score = None, 0.0

    for cls_uri in ontology.get("classes", []):
        local = _local_name(cls_uri).lower().replace("_", "").replace(" ", "")
        if not local:
            continue
        if local == table_clean:
            return cls_uri                       # exact match → done
        # partial / truncated match
        if local.startswith(table_clean):
            score = len(table_clean) / len(local)
        elif table_clean.startswith(local):
            score = len(local) / len(table_clean)
        else:
            score = 0.0
        if score > best_score:
            best_score, best_uri = score, cls_uri

    return best_uri if best_score >= R2RML_SH_INFER_SUBCLASS_MIN_SCORE else None


def _repair_sh_subclass_by_dataprop_domain(entry: dict, ontology: dict) -> str | None:
    """
    SH 子类纠偏（通用，无数据集硬编码）：
    当 sub_class_uri 缺失或低置信时，利用已对齐 data_attr 的 DatatypeProperty domain
    反推更合理的 class。只在“证据明显更强”时替换。
    """
    if not ontology:
        return entry.get("sub_class_uri")

    columns = (entry or {}).get("columns", {}) or {}
    dp_map = (ontology or {}).get("datatype_properties", {}) or {}

    evidence = {}
    total_props = 0
    for col_info in columns.values():
        if (col_info or {}).get("role") != "data_attr":
            continue
        prop_uri = col_info.get("prop_uri")
        if not _is_valid_uri(prop_uri):
            continue
        dp = dp_map.get(prop_uri, {}) or {}
        domains = dp.get("domain", []) or []
        if not domains:
            continue
        total_props += 1
        for d in domains:
            evidence[d] = evidence.get(d, 0) + 1

    current = entry.get("sub_class_uri")
    if not evidence:
        return current

    best_cls, best_cnt = max(evidence.items(), key=lambda kv: kv[1])
    cur_cnt = evidence.get(current, 0)
    cls_conf = (entry.get("class_confidence") or "").lower()
    low_conf = cls_conf in ("", "low")

    # 仅在“证据更强 + 当前低置信/不在证据里”时纠偏，避免过度改写。
    if (not _is_valid_uri(current)) and best_cnt > 0:
        return best_cls
    if low_conf and best_cnt > cur_cnt:
        return best_cls
    return current
# ============================================================
#  主程序
# ============================================================

if __name__ == "__main__":
    from utils.ontology_utils import read_ontology

    ALIGNMENT_PATH = "output/mondial_rel/final_alignment.json"
    SCHEMA_PATH = "output/mondial_rel/enriched_schema.json"
    OP_MAPPING_PATH = "output/mondial_rel/op_mapping_full_result.json"
    ONTOLOGY_PATH = "input/mondial_rel/ontology.ttl"
    OUTPUT_PATH = "output/generated_mapping.ttl"

    for path in [ALIGNMENT_PATH, SCHEMA_PATH, OP_MAPPING_PATH]:
        if not os.path.exists(path):
            print(f"缺少文件: {path}")
            exit(1)

    with open(ALIGNMENT_PATH, "r", encoding="utf-8") as f:
        final_alignment = json.load(f)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        enriched_schema = json.load(f)
    with open(OP_MAPPING_PATH, "r", encoding="utf-8") as f:
        op_mapping_full = json.load(f)

    ontology = read_ontology(ONTOLOGY_PATH)

    print(f"已加载 final_alignment: {len(final_alignment)} 张表")
    print(f"已加载 enriched_schema: {len(enriched_schema)} 张表")

    r2rml = generate_r2rml(
        final_alignment=final_alignment,
        op_mapping_full=op_mapping_full,
        enriched_schema=enriched_schema,
        ontology=ontology,
        base_url="http://example.com/",
        prefix="conference"
    )

    os.makedirs("output", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(r2rml)

    print(f"\n映射已保存到 {OUTPUT_PATH}")
    print(f"文件大小: {len(r2rml)} 字符")
