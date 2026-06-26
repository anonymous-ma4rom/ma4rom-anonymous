import json
import copy
from utils.db_utils import get_conn as _get_conn, get_table_row_count as _get_table_row_count
from config import OP_MAPPING_PEER_MAX_EDGES, OP_MAPPING_SCENARIO_SSD_MAX_ROWS
try:
    from implicit_relation_miner import mine_implicit_relations, lookup_implicit_edge
except ImportError:
    from OPMapping.implicit_relation_miner import mine_implicit_relations, lookup_implicit_edge


def _get_fk_source(
    enriched_schema: dict,
    table_name: str,
    col_name: str,
    ref_table: str | None = None,
    implicit_relations: dict | None = None,
) -> dict:
    """
    从 enriched_schema 中查找某条 FK 的来源信息。
    返回 {"source": "physical"/"IND"/"LLM", "ind_score": float or None}
    """
    table_info = enriched_schema.get(table_name, {})
    implicit_edge = None
    if ref_table:
        implicit_edge = lookup_implicit_edge(
            implicit_relations,
            source_table=table_name,
            source_column=col_name,
            target_table=ref_table,
        )

    for fk in table_info.get("foreign_keys", []):
        if fk.get("column") == col_name:
            source = fk.get("source", "physical")
            ind_score = fk.get("ind_score", None)
            return {
                "source": source,
                "ind_score": ind_score,
                "implicit_edge": implicit_edge,
                "implicit_score": implicit_edge.get("evidence_score") if implicit_edge else None,
            }

    if implicit_edge:
        return {
            "source": "implicit",
            "ind_score": implicit_edge.get("evidence_score"),
            "implicit_edge": implicit_edge,
            "implicit_score": implicit_edge.get("evidence_score"),
        }
    return {"source": "unknown", "ind_score": None}


def _peer_implicit_support(
    table_name: str,
    col_name: str,
    ref_table: str,
    implicit_relations: dict | None = None,
    max_peers: int = OP_MAPPING_PEER_MAX_EDGES,
) -> tuple[float | None, list[dict]]:
    """
    同表“其他列”指向同一 ref_table 的隐边证据（仅增强，不扣分）。
    返回：
      - peer_score: 最强同伴证据分（None 表示无证据）
      - peer_edges: 证据明细（最多 max_peers 条）
    """
    if not implicit_relations or not table_name or not ref_table:
        return None, []

    edges = (implicit_relations.get("edges", []) or [])
    peers = []
    for edge in edges:
        if edge.get("source_table") != table_name:
            continue
        if edge.get("target_table") != ref_table:
            continue
        if edge.get("source_column") == col_name:
            continue
        score = float(edge.get("evidence_score") or 0.0)
        if score <= 0.0:
            continue
        peers.append({
            "source_column": edge.get("source_column"),
            "target_column": edge.get("target_column"),
            "evidence_score": round(score, 6),
            "inclusion_score": edge.get("inclusion_score"),
            "intersection": edge.get("intersection"),
        })

    if not peers:
        return None, []

    peers.sort(key=lambda x: x.get("evidence_score", 0.0), reverse=True)
    top_peers = peers[:max_peers]
    top_score = top_peers[0].get("evidence_score")
    return top_score, top_peers


def determine_op_mapping_scenarios(
        final_alignment: dict,
        enriched_schema: dict
) -> dict:
    """
    场景判定主函数。

    对 final_alignment 中每条需要 OP 映射处理的 FK 关系，
    判定其属于 SLD / SSD / SMD 三种场景之一。

    判定规则
      SMD: 无实例数据（相关端行数都为 0）
      SSD: 有实例数据但规模小（相关端最小非零行数 <= 5）
      SLD: 有实例数据且规模大（相关端最小非零行数 > 5）

    说明：
      - IND / implicit 证据仍会记录到输出中，供后续打分参考；
      - 但不再决定 SMD/SSD/SLD 场景标签。
    """
    conn = _get_conn()
    implicit_relations = mine_implicit_relations(enriched_schema)
    implicit_edges = implicit_relations.get("edges", []) if implicit_relations else []
    print(f"隐式关系挖掘完成: {len(implicit_edges)} 条候选边")

    row_count_cache = {}

    def get_row_count(table_name):
        if table_name not in row_count_cache:
            row_count_cache[table_name] = _get_table_row_count(conn, table_name)
        return row_count_cache[table_name]

    scenarios = {}

    def _table_class_uri(table_name: str) -> str | None:
        entry = (final_alignment or {}).get(table_name, {}) or {}
        pattern = entry.get("pattern", "SE")
        if pattern == "SH":
            return entry.get("sub_class_uri") or entry.get("class_uri")
        if pattern == "SR":
            return None
        return entry.get("class_uri") or entry.get("sub_class_uri")

    def classify_by_volume(counts: list[int]) -> str:
        non_zero = [c for c in counts if c and c > 0]
        if not non_zero:
            return "SMD"
        if min(non_zero) <= OP_MAPPING_SCENARIO_SSD_MAX_ROWS:
            return "SSD"
        return "SLD"

    for table_name, entry in final_alignment.items():
        pattern = entry.get("pattern", "SE")

        # value_attr 是 SE 内部的多值 DatatypeProperty 映射，不参与 ObjectProperty 映射。
        if entry.get("table_kind") == "value_attr":
            continue

        # ---- SR 表：整张表对应一条 ObjectProperty ----
        if pattern == "SR":
            fk1 = entry.get("fk1", {})
            fk2 = entry.get("fk2", {})
            op_candidates = entry.get("op_candidates", [])

            domain_table = fk1.get("ref_table")
            range_table = fk2.get("ref_table")

            domain_rows = get_row_count(domain_table) if domain_table else 0
            range_rows = get_row_count(range_table) if range_table else 0
            self_rows = get_row_count(table_name)

            fk1_source = _get_fk_source(
                enriched_schema,
                table_name,
                fk1.get("column"),
                ref_table=fk1.get("ref_table"),
                implicit_relations=implicit_relations,
            ) if fk1.get("column") else {"source": "unknown", "ind_score": None}
            fk2_source = _get_fk_source(
                enriched_schema,
                table_name,
                fk2.get("column"),
                ref_table=fk2.get("ref_table"),
                implicit_relations=implicit_relations,
            ) if fk2.get("column") else {"source": "unknown", "ind_score": None}

            ind1 = fk1_source.get("ind_score")
            ind2 = fk2_source.get("ind_score")
            imp1 = fk1_source.get("implicit_score")
            imp2 = fk2_source.get("implicit_score")
            best_ind = max(ind1 or 0.0, ind2 or 0.0)
            best_implicit = max(imp1 or 0.0, imp2 or 0.0)
            has_ind = (fk1_source["source"] == "IND" or fk2_source["source"] == "IND")
            has_implicit = (
                fk1_source["source"] == "implicit" or
                fk2_source["source"] == "implicit" or
                bool(imp1) or bool(imp2)
            )
            scenario = classify_by_volume([self_rows, domain_rows, range_rows])

            scenarios[table_name] = {
                "scenario": scenario,
                "type": "SR",
                "fk_source": f"fk1={fk1_source['source']}, fk2={fk2_source['source']}",
                "ind_score": best_ind if has_ind else None,
                "implicit_score": best_implicit if has_implicit else None,
                "domain_table": domain_table,
                "domain_rows": domain_rows,
                "range_table": range_table,
                "range_rows": range_rows,
                "self_rows": self_rows,
                "domain_class_uri": entry.get("domain_class_uri"),
                "range_class_uri": entry.get("range_class_uri"),
                "op_candidates": op_candidates,
                "fk1_evidence": fk1_source.get("implicit_edge"),
                "fk2_evidence": fk2_source.get("implicit_edge"),
            }
            continue

        # ---- SE / SH 表：逐列检查 FK（不依赖 role=fk_obj，避免漏掉真实外键） ----
        columns = entry.get("columns", {})
        if pattern == "SH":
            table_class_uri = entry.get("sub_class_uri")
        else:
            table_class_uri = entry.get("class_uri")

        physical_fks = (enriched_schema.get(table_name, {}) or {}).get("foreign_keys", []) or []
        fk_by_col = {}
        for fk in physical_fks:
            col = fk.get("column")
            if not col:
                continue
            ref_table = fk.get("ref_table") or fk.get("references_table") or ""
            fk_by_col[col] = {
                "ref_table": ref_table,
                "ref_col": fk.get("ref_col") or fk.get("references_column"),
            }

        target_fk_cols = set(fk_by_col.keys())
        for col_name, col_info in columns.items():
            if col_info.get("role") == "fk_obj":
                target_fk_cols.add(col_name)

        for col_name in sorted(target_fk_cols):
            col_info = columns.get(col_name, {}) or {}
            ref_table = col_info.get("ref_table") or fk_by_col.get(col_name, {}).get("ref_table", "")
            domain_class_uri = col_info.get("domain_class_uri") or table_class_uri
            range_class_uri = col_info.get("range_class_uri") or _table_class_uri(ref_table)
            op_candidates = col_info.get("op_candidates", [])

            domain_rows = get_row_count(table_name)
            range_rows = get_row_count(ref_table) if ref_table else 0

            fk_source = _get_fk_source(
                enriched_schema,
                table_name,
                col_name,
                ref_table=ref_table,
                implicit_relations=implicit_relations,
            )
            source = fk_source["source"]
            ind_score = fk_source.get("ind_score")
            implicit_score = fk_source.get("implicit_score")
            peer_score, peer_edges = _peer_implicit_support(
                table_name=table_name,
                col_name=col_name,
                ref_table=ref_table,
                implicit_relations=implicit_relations,
            )

            scenario = classify_by_volume([domain_rows, range_rows])

            key = f"{table_name}.{col_name}"
            scenarios[key] = {
                "scenario": scenario,
                "type": "fk_obj",
                "fk_source": source,
                "ind_score": ind_score,
                "implicit_score": implicit_score,
                "peer_implicit_score": peer_score,
                "domain_table": table_name,
                "domain_rows": domain_rows,
                "range_table": ref_table,
                "range_rows": range_rows,
                "domain_class_uri": domain_class_uri,
                "range_class_uri": range_class_uri,
                "op_candidates": op_candidates,
                "fk_evidence": fk_source.get("implicit_edge"),
                "peer_implicit_evidence": peer_edges,
            }

    conn.close()

    sld_count = sum(1 for v in scenarios.values() if v["scenario"] == "SLD")
    ssd_count = sum(1 for v in scenarios.values() if v["scenario"] == "SSD")
    smd_count = sum(1 for v in scenarios.values() if v["scenario"] == "SMD")
    print(f"\n场景判定完成: SLD={sld_count}, SSD={ssd_count}, SMD={smd_count}, 共 {len(scenarios)} 条 FK 关系")

    return scenarios


if __name__ == "__main__":
    import os
    from config import OUTPUT_DIR          # ← 路径从 config 读取

    ALIGNMENT_PATH = os.path.join(OUTPUT_DIR, "final_alignment.json")
    SCHEMA_PATH    = os.path.join(OUTPUT_DIR, "enriched_schema.json")

    if not os.path.exists(ALIGNMENT_PATH) or not os.path.exists(SCHEMA_PATH):
        print("缺少上一阶段的输出文件，请先运行 real_value_enhancement_agent.py：")
        print(f"  - {ALIGNMENT_PATH}")
        print(f"  - {SCHEMA_PATH}")
        exit(1)

    with open(ALIGNMENT_PATH, "r", encoding="utf-8") as f:
        final_alignment = json.load(f)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        enriched_schema = json.load(f)

    print(f"已加载 final_alignment: {len(final_alignment)} 张表")
    print(f"已加载 enriched_schema: {len(enriched_schema)} 张表")

    print("\n" + "=" * 60)
    print("OP Mapping Step 0: 场景判定")
    print("=" * 60)

    scenarios = determine_op_mapping_scenarios(final_alignment, enriched_schema)

    for scenario_type in ["SLD", "SSD", "SMD"]:
        items = {k: v for k, v in scenarios.items() if v["scenario"] == scenario_type}
        if items:
            print(f"\n--- {scenario_type} ({len(items)} 条) ---")
            for key, info in items.items():
                source_str = info['fk_source']
                ind_str = f", IND={info['ind_score']}" if info.get('ind_score') else ""
                rows_str = f"domain={info['domain_rows']}行, range={info['range_rows']}行"
                if info['type'] == 'SR':
                    rows_str += f", self={info['self_rows']}行"
                print(f"  {key}: [{info['type']}] source={source_str}{ind_str} | {rows_str}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    scenarios_summary = {}
    for key, info in scenarios.items():
        scenarios_summary[key] = {
            "scenario": info["scenario"],
            "type": info["type"],
            "fk_source": info["fk_source"],
            "ind_score": info.get("ind_score"),
            "implicit_score": info.get("implicit_score"),
            "peer_implicit_score": info.get("peer_implicit_score"),
            "domain_table": info.get("domain_table"),
            "range_table": info.get("range_table"),
            "domain_rows": info.get("domain_rows"),
            "range_rows": info.get("range_rows"),
            "domain_class_uri": info.get("domain_class_uri"),
            "range_class_uri": info.get("range_class_uri"),
            "num_candidates": len(info.get("op_candidates", []))
        }

    out_path = os.path.join(OUTPUT_DIR, "scenarios.json")
    json.dump(scenarios_summary, open(out_path, "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)

    print("\n\n完整场景判定结果：")
    print(json.dumps(scenarios_summary, indent=2, ensure_ascii=False))
    print(f"\n已保存到 {out_path}")
