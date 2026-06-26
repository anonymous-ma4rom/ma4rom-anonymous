"""
Data Property Mapping 前的 Pattern 约束候选生成
根据每张表的Pattern类型（SE/SH/SR），为每张表和每列
生成本体候选集合，缩小后续LLM匹配的搜索空间。

输入:
  - enriched_schema: FKCompletion后的schema对象
  - pattern_result:  classify_agent输出的 {table: SE/SH/SR}
  - ontology:        read_ontology()解析出的本体对象

输出:
  - candidates: 每张表/每列的候选集合（dict结构）

列级 FK→OP 说明:
  LLM4VKG/Calvanese 原文把 SRm 作为独立 mapping pattern；本系统当前将
  表级输出收敛为 SE/SH/SR，因此 SE/SH 表内的非PK FK列会被标记为
  role: "fk_obj"，在 OP 映射阶段选择 ObjectProperty。
"""

import json
from utils.ontology_utils import local_name as _local_name
from utils.candidate_ranking import (
    rank_class_candidates as _rank_class_candidates,
    rank_object_prop_candidates as _rank_object_prop_candidates,
    rank_datatype_prop_candidates as _rank_datatype_prop_candidates,
)

def generate_candidates(
    enriched_schema: dict,
    pattern_result: dict,
    ontology: dict,
    disabled_patterns: set[str] | None = None,
) -> dict:
    """
    为每张表和其每列，根据 Pattern 约束生成候选集合。
    返回结构示例:
    {
      "Paper": {
        "pattern": "SE",
        "table_class_candidates": [...],
        "columns": {
          "has_a_paper_title": {
            "role": "data_attr",
            "candidates": [...]
          },
          "has_an_abstract": {
            "role": "fk_obj",
            "ref_table": "Abstract",
            "candidates": [...]
          }
        }
      },
      "has_members": {
        "pattern": "SR",
        "sr_prop_candidates": [...],
        "fk1": {...},
        "fk2": {...}
      }
    }
    """
    classes        = ontology["classes"]
    object_props   = ontology["object_properties"]
    datatype_props = ontology["datatype_properties"]

    disabled_patterns = {p.upper() for p in (disabled_patterns or set())}
    candidates = {}

    for table_name, table_info in enriched_schema.items():
        pattern = pattern_result.get(table_name, "SE")
        pattern_norm = (pattern or "SE")
        if pattern_norm.upper() in disabled_patterns:
            continue
        cols    = table_info.get("columns", {})
        pks     = set(table_info.get("primary_key", []))
        fks     = table_info.get("foreign_keys", [])

        # 统一 FK 字段名
        for fk in fks:
            if "references_table" in fk and "ref_table" not in fk:
                fk["ref_table"] = fk["references_table"]
            if "references_column" in fk and "ref_col" not in fk:
                fk["ref_col"] = fk["references_column"]

        fk_cols = {fk["column"]: fk for fk in fks}

        column_op_enabled = "COLUMN_OP" not in disabled_patterns

        # SE：主实体表；也包括 has_xxx(EntityFK, VALUE) 这种多值数据属性表
        if pattern == "SE":
            if _is_value_attr_table(cols, pks, fk_cols):
                entry = _handle_value_attr_table(
                    table_name, cols, pks, fk_cols,
                    classes, datatype_props,
                )
            else:
                entry = _handle_SE(
                    table_name, cols, pks, fk_cols,
                    classes, object_props, datatype_props,
                    ontology,
                    enable_column_op=column_op_enabled,
                )

        #SH：子类继承表
        elif pattern == "SH":
            entry = _handle_SH(
                table_name, cols, pks, fk_cols,
                classes, object_props, datatype_props,
                enriched_schema,
                ontology,
                enable_column_op=column_op_enabled,
            )

        #SR：纯关联表
        elif pattern == "SR":
            entry = _handle_SR(
                table_name, cols, pks, fk_cols,
                classes, object_props,
                enriched_schema,
                ontology,
            )

        else:
            entry = {"pattern": pattern, "note": "未知 Pattern，跳过"}

        candidates[table_name] = entry

    return candidates

# 各 Pattern 处理函数

def _is_literal_value_type(col_type: str | None) -> bool:
    t = (col_type or "").lower()
    return any(k in t for k in (
        "char", "text", "string", "date", "time", "bool", "json", "xml"
    ))


def _is_value_attr_table(cols: dict, pks: set, fk_cols: dict) -> bool:
    """
    多值数据属性表：一列 FK 指向实体，一列 literal 值，两列通常共同组成 PK。
    这不是 SR/SRm；表级仍为 SE，生成时挂到被引用实体的 DataProperty 上。
    """
    col_names = set(cols.keys())
    if len(col_names) != 2 or pks != col_names or len(fk_cols) != 1:
        return False
    value_cols = [c for c in col_names if c not in fk_cols]
    return len(value_cols) == 1 and _is_literal_value_type(cols.get(value_cols[0]))


def _handle_value_attr_table(table_name, cols, pks, fk_cols, classes, datatype_props):
    fk_col, fk_info = next(iter(fk_cols.items()))
    value_col = next(c for c in cols if c != fk_col)
    ref_table = fk_info.get("ref_table", "")
    owner_class_candidates = _rank_class_candidates(
        ref_table,
        classes,
        top_k=3,
    )
    owner_class_uri = owner_class_candidates[0]["uri"] if owner_class_candidates else None

    # 属性名优先来自表名：has_an_email(Person, VALUE) 表达的是 :has_an_email。
    prop_candidates = _rank_datatype_prop_candidates(
        table_name,
        datatype_props,
        domain_hint=owner_class_uri,
    )
    if not prop_candidates:
        prop_candidates = _rank_datatype_prop_candidates(
            value_col,
            datatype_props,
            domain_hint=owner_class_uri,
        )

    return {
        "pattern": "SE",
        "table_kind": "value_attr",
        "fk": {
            "column": fk_col,
            "ref_table": ref_table,
            "owner_class_candidates": owner_class_candidates,
            "column_type": cols.get(fk_col),
        },
        "value_column": value_col,
        "value_column_type": cols.get(value_col),
        "property_candidates": prop_candidates,
    }

def _handle_SE(table_name, cols, pks, fk_cols,
               classes, object_props, datatype_props,
               ontology=None, enable_column_op=True):
    """
    SE: 表 → Class；非FK列 → dataProperty；FK列 → objectProperty
    数据库里最常见的表，如 Person、Paper
    """
    # 1. 表 → Class 候选
    table_cls_candidates = _rank_class_candidates(table_name, classes)
    best_class_uri = table_cls_candidates[0]["uri"] if table_cls_candidates else None

    col_entries = {}
    for col_name in cols:
        if col_name in pks:
            # PK 列：生成 IRI 模板用，不需要本体属性候选
            col_entries[col_name] = {
                "role": "pk",
                "candidates": [],
                "column_type": cols.get(col_name)
            }
        elif col_name in fk_cols:
            if not enable_column_op:
                col_entries[col_name] = {
                    "role": "fk_disabled",
                    "ref_table": fk_cols[col_name].get("ref_table", ""),
                    "candidates": [],
                    "column_type": cols.get(col_name)
                }
                continue
            # FK 列 → objectProperty 候选
            ref_table = fk_cols[col_name].get("ref_table", "")
            ref_class_candidates = _rank_class_candidates(ref_table, classes, top_k=3)
            best_ref_class = ref_class_candidates[0]["uri"] if ref_class_candidates else None

            op_candidates = _rank_object_prop_candidates(
                col_name, object_props,
                domain_hint=best_class_uri,
                range_hint=best_ref_class,
                ontology=ontology,
            )
            col_entries[col_name] = {
                "role": "fk_obj",
                "ref_table": ref_table,
                "ref_class_candidates": ref_class_candidates,
                "candidates": op_candidates,
                "column_type": cols.get(col_name)
            }
        else:
            # 普通数据列 → dataProperty 候选
            dp_candidates = _rank_datatype_prop_candidates(
                col_name, datatype_props,
                domain_hint=best_class_uri,
            )
            col_entries[col_name] = {
                "role": "data_attr",
                "candidates": dp_candidates,
                # 为 BOOL/TYPE 判别列预留 class 候选（供 DP 映射/真实值增强使用）
                "class_candidates": _rank_class_candidates(col_name, classes, top_k=20),
                "column_type": cols.get(col_name)
            }

    return {
        "pattern": "SE",
        "table_class_candidates": table_cls_candidates,
        "columns": col_entries
    }


def _handle_SH(table_name, cols, pks, fk_cols,
               classes, object_props, datatype_props,
               enriched_schema,
               ontology=None, enable_column_op=True):
    """
    SH: 表 → 子类（subClassOf 某父类）；
        PK 列继承父类 IRI，不单独映射；
        非PK非FK列 → dataProperty；
        非PK的FK列 → objectProperty。
    """
    # 找父类：SH 表的 PK 就是指向父表的 FK
    parent_table = None
    for pk in pks:
        if pk in fk_cols:
            parent_table = fk_cols[pk].get("ref_table", "")
            break

    # 子类候选
    sub_class_candidates = _rank_class_candidates(table_name, classes)

    # 父类候选
    parent_class_candidates = []
    if parent_table:
        parent_class_candidates = _rank_class_candidates(parent_table, classes, top_k=3)

    best_sub_class = sub_class_candidates[0]["uri"] if sub_class_candidates else None

    col_entries = {}
    for col_name in cols:
        if col_name in pks:
            # SH 的 PK 是继承用的，不需要独立属性候选
            col_entries[col_name] = {
                "role": "sh_inherited_pk",
                "note": "使用父类 IRI 模板，不单独映射",
                "candidates": [],
                "column_type": cols.get(col_name)
            }
        elif col_name in fk_cols:
            if not enable_column_op:
                col_entries[col_name] = {
                    "role": "fk_disabled",
                    "ref_table": fk_cols[col_name].get("ref_table", ""),
                    "candidates": [],
                    "column_type": cols.get(col_name)
                }
                continue
            # 非PK的FK列 → objectProperty
            ref_table = fk_cols[col_name].get("ref_table", "")
            ref_class_candidates = _rank_class_candidates(ref_table, classes, top_k=3)
            best_ref_class = ref_class_candidates[0]["uri"] if ref_class_candidates else None
            op_candidates = _rank_object_prop_candidates(
                col_name, object_props,
                domain_hint=best_sub_class,
                range_hint=best_ref_class,
                ontology=ontology,
            )
            col_entries[col_name] = {
                "role": "fk_obj",
                "ref_table": ref_table,
                "ref_class_candidates": ref_class_candidates,
                "candidates": op_candidates,
                "column_type": cols.get(col_name)
            }
        else:
            # 普通数据列 → dataProperty
            dp_candidates = _rank_datatype_prop_candidates(
                col_name, datatype_props,
                domain_hint=best_sub_class,
            )
            col_entries[col_name] = {
                "role": "data_attr",
                "candidates": dp_candidates,
                "class_candidates": _rank_class_candidates(col_name, classes, top_k=20),
                "column_type": cols.get(col_name)
            }

    return {
        "pattern": "SH",
        "sub_class_candidates": sub_class_candidates,
        "parent_table": parent_table,
        "parent_class_candidates": parent_class_candidates,
        "columns": col_entries
    }


def _handle_SR(table_name, cols, pks, fk_cols,
               classes, object_props,
               enriched_schema,
               ontology=None):
    """
    SR: 整张表 → 一个 objectProperty。
        domain ≈ 第一个FK引用表的Class；
        range  ≈ 第二个FK引用表的Class。
    """
    fk_list = list(fk_cols.values())
    partial_fk = False
    value_col = None

    domain_hint, range_hint = None, None
    fk1_info, fk2_info = None, None

    if len(fk_list) >= 2:
        fk1_info = fk_list[0]
        fk2_info = fk_list[1]
        ref1 = fk1_info.get("ref_table", "")
        ref2 = fk2_info.get("ref_table", "")
        ref1_cls = _rank_class_candidates(ref1, classes, top_k=1)
        ref2_cls = _rank_class_candidates(ref2, classes, top_k=1)
        domain_hint = ref1_cls[0]["uri"] if ref1_cls else None
        range_hint  = ref2_cls[0]["uri"] if ref2_cls else None
    elif len(fk_list) == 1:
        fk1_info = fk_list[0]
        ref1 = fk1_info.get("ref_table", "")
        ref1_cls = _rank_class_candidates(ref1, classes, top_k=1)
        domain_hint = ref1_cls[0]["uri"] if ref1_cls else None
        if len(cols) == 2:
            partial_fk = True
            non_fk_cols = [c for c in cols if c not in fk_cols]
            value_col = non_fk_cols[0] if non_fk_cols else None

    # 整张表 → objectProperty 候选
    op_candidates = _rank_object_prop_candidates(
        table_name, object_props,
        domain_hint=domain_hint,
        range_hint=range_hint,
        ontology=ontology,
    )

    return {
        "pattern": "SR",
        "relation_kind": "partial_fk" if partial_fk else "full_fk",
        "fk1": {
            "column": fk1_info["column"] if fk1_info else None,
            "ref_table": fk1_info.get("ref_table") if fk1_info else None,
            "domain_class_hint": domain_hint
        },
        "fk2": {
            "column": fk2_info["column"] if fk2_info else value_col,
            "ref_table": fk2_info.get("ref_table") if fk2_info else None,
            "range_class_hint": range_hint
        },
        "partial_value_column": value_col,
        "sr_prop_candidates": op_candidates
    }


if __name__ == "__main__":
    from utils.db_utils import read_schema
    from utils.ontology_utils import read_ontology
    from FKCompletion_agent import allocate_targets_and_shooters, discover_implicit_foreign_keys
    from utils.merge_fks import merge_fks_into_schema

    ONTOLOGY_PATH = "input/conference_nofks/ontology.ttl"

    # 1. 读取并补全 schema
    schema = read_schema()
    allocation = allocate_targets_and_shooters(schema)
    discovered_fks = discover_implicit_foreign_keys(allocation, schema_name="public")
    enriched_schema = merge_fks_into_schema(schema, discovered_fks)

    # 2. 读取本体
    ontology = read_ontology(ONTOLOGY_PATH)

    # 3. 使用 classify_agent 输出的 pattern
    pattern_result = {
        'Abstract': 'SH', 'Accepted_contribution': 'SH',
        'Active_conference_partici': 'SH', 'Call_for_paper': 'SH',
        'Call_for_participation': 'SH', 'Camera_ready_contribution': 'SH',
        'Chair': 'SH', 'Co-chair': 'SH',
        'Committee': 'SE', 'Committee_member': 'SH',
        'Conference': 'SE', 'Conference_announcement': 'SH',
        'Conference_applicant': 'SH', 'Conference_contribution': 'SH',
        'Conference_contributor': 'SH', 'Conference_document': 'SE',
        'Conference_fees': 'SE', 'Conference_part': 'SE',
        'Conference_participant': 'SH', 'Conference_proceedings': 'SE',
        'Conference_volume': 'SH', 'Conference_www': 'SH',
        'Contribution_1th-author': 'SH', 'Contribution_co-author': 'SH',
        'Early_paid_applicant': 'SH', 'Extended_abstract': 'SH',
        'Important_dates': 'SE', 'Information_for_participa': 'SH',
        'Invited_speaker': 'SH', 'Invited_talk': 'SH',
        'Late_paid_applicant': 'SH', 'Organization': 'SE',
        'Organizer': 'SH', 'Organizing_committee': 'SH',
        'Paid_applicant': 'SH', 'Paper': 'SH',
        'Passive_conference_partic': 'SH', 'Person': 'SE',
        'Poster': 'SH', 'Presentation': 'SH',
        'Program_committee': 'SH', 'Publisher': 'SE',
        'Registeered_applicant': 'SH', 'Regular_author': 'SH',
        'Regular_contribution': 'SH', 'Rejected_contribution': 'SH',
        'Review': 'SH', 'Review_expertise': 'SE',
        'Review_preference': 'SE', 'Reviewed_contribution': 'SH',
        'Reviewer': 'SH', 'Steering_committee': 'SH',
        'Submitted_contribution': 'SH', 'Topic': 'SE',
        'Track': 'SH', 'Track-workshop_chair': 'SH',
        'Tutorial': 'SH', 'Workshop': 'SH',
        'Written_contribution': 'SH',
        'belongs_to_reviewers': 'SE',
        'contributes': 'SR',
        'has_a_committee_co-chair': 'SR',
        'has_a_track-workshop-tuto': 'SR',
        'has_an_email': 'SE',
        'has_members': 'SR',
        'invited_by': 'SR'
    }

    # 4. 生成候选
    candidates = generate_candidates(enriched_schema, pattern_result, ontology)

    # 5. 打印结果
    demo_tables = ["Paper", "has_members", "Organizer", "has_an_email", "belongs_to_reviewers"]
    for t in demo_tables:
        if t in candidates:
            print(f"\n{'='*60}")
            print(f"表: {t}")
            print(json.dumps(candidates[t], indent=2, ensure_ascii=False))

    # 输出完整结果
    with open("DPMapping/candidates_output.json", "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2, ensure_ascii=False)
    print("\n\n完整候选集已保存到 candidates_output.json")
