"""
object_property_mapping_agent.py  ——  Object Property Mapping 阶段

三步设计：
  Step 0: 场景判定（SMD / SSD / SLD）  → scenario_judgement.py
  Step 1: 从表出发 —— 独立生成 OP 候选 + 场景特定权重打分 + LLM 精选
  Step 2: 从本体出发 —— 收集孤儿列 + 未覆盖 OP 交叉匹配 + LLM 确认
"""

import json
import os
from utils.llm_client import call_llm as _call_llm
from utils.db_utils import get_conn as _get_conn, fetch_sample_rows
from utils.ontology_utils import local_name as _local_name, hint_match
from utils.candidate_ranking import (
    rank_class_candidates,
    rank_object_prop_candidates,
    rank_datatype_prop_candidates,
)
from utils.ontology_utils import read_ontology
from config import (
    OP_MAPPING_FASTTRACK_GAP,
    OP_MAPPING_RESCUE_MIN_GAP,
    OP_MAPPING_RESCUE_MIN_SCORE,
    OP_MAPPING_RESCUE_MIN_SIDE_SCORE,
    OP_MAPPING_STEP2_FASTTRACK_DOMAIN,
    OP_MAPPING_STEP2_FASTTRACK_GAP,
    OP_MAPPING_STEP2_FASTTRACK_NAME,
    OP_MAPPING_STEP2_MIN_SCORE,
)
try:
    from scenario_judgement import determine_op_mapping_scenarios
except ImportError:
    from OPMapping.scenario_judgement import determine_op_mapping_scenarios
from utils.name_similarity import name_overlap as _name_overlap


def _safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except BrokenPipeError:
        # 某些 IDE profiler/debugger 会提前关闭 stdout 管道；日志应降级为静默。
        return


def _safe_dump_json(path: str, data: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _safe_print(f"  [WARN] checkpoint 写入失败: {e}")


# ============================================================
#  Step 1: OP 候选生成 + name/domain/range 打分 + LLM 精选
# ============================================================


def generate_op_candidates(
    name_hint: str,
    domain_class_uri: str,
    range_class_uri: str,
    ontology: dict,
    scenario: str = "SMD",
) -> list:
    object_props = ontology["object_properties"]
    w_name, w_domain, w_range = 0.5, 0.25, 0.25

    scored = []
    for uri, info in object_props.items():
        local = _local_name(uri)
        name_score = _name_overlap(name_hint, local)
        domain_score = hint_match(domain_class_uri, info.get("domain", []))
        range_score = hint_match(range_class_uri, info.get("range", []))

        total = (name_score * w_name +
                 domain_score * w_domain +
                 range_score * w_range)

        scored.append({
            "uri": uri,
            "local_name": local,
            "score": round(total, 4),
            "name_score": round(name_score, 3),
            "domain": info.get("domain", []),
            "range": info.get("range", []),
            "domain_score": round(domain_score, 3),
            "range_score": round(range_score, 3),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:5]


def _llm_select_op_smd(name_hint, domain_class, range_class, candidates):
    prompt = f"""
## 任务
为数据库中的外键关系选择最匹配的 OWL ObjectProperty。

## 关系信息（仅元数据）
- 列名 / 表名: `{name_hint}`
- Domain Class (已确认): {domain_class}
- Range Class (已确认): {range_class}

## 候选 ObjectProperty（按综合分排序）
{json.dumps(candidates, indent=2, ensure_ascii=False)}

## 输出格式（严格 JSON）
{{
  "selected_uri": "选中的 URI（必须来自候选列表，或 null）",
  "confidence": "high / medium / low",
  "reason": "一句话理由"
}}
"""
    return _call_llm(prompt)


# ============================================================
#  Step 1 主函数
# ============================================================

def run_object_property_mapping_step1(
    scenarios: dict,
    ontology: dict,
    enriched_schema: dict,
    checkpoint_path: str | None = None,
) -> dict:
    result = {}
    if checkpoint_path and os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                result = loaded
                _safe_print(f"  检测到 Step1 checkpoint，已加载 {len(result)} 条，继续续跑。")
        except Exception as e:
            _safe_print(f"  [WARN] 读取 Step1 checkpoint 失败，忽略续跑: {e}")
    total = len(scenarios)

    for idx, (key, info) in enumerate(scenarios.items(), 1):
        if key in result:
            _safe_print(f"\n[OP Mapping {idx}/{total}] {key} 已完成，跳过（checkpoint）")
            continue

        scenario = info["scenario"]
        rel_type = info["type"]
        domain_class = info.get("domain_class_uri", "")
        range_class = info.get("range_class_uri", "")

        if rel_type == "fk_obj":
            name_hint = key.split(".", 1)[1] if "." in key else key
        else:
            name_hint = key

        _safe_print(f"\n[OP Mapping {idx}/{total}] {key} (scenario={scenario}, type={rel_type})")
        _safe_print(f"  domain={domain_class}, range={range_class}")

        candidates = generate_op_candidates(
            name_hint=name_hint,
            domain_class_uri=domain_class,
            range_class_uri=range_class,
            ontology=ontology,
            scenario=scenario,
        )

        if not candidates:
            _safe_print(f"  无候选，跳过")
            result[key] = {
                "object_prop_uri": None,
                "confidence": "low",
                "scenario": scenario,
                "method": "no_candidates"
            }
            if checkpoint_path:
                _safe_dump_json(checkpoint_path, result)
            continue

        top3 = candidates[:3]
        for c in top3:
            _safe_print(
                f"  候选: {c['local_name']} (score={c['score']}, name={c['name_score']}, "
                f"dom={c['domain_score']}, rng={c['range_score']})"
            )

        top1 = candidates[0]
        top2_score = candidates[1]["score"] if len(candidates) > 1 else 0
        gap = top1["score"] - top2_score

        fast_track = (
            top1["domain_score"] >= 1.0 and
            top1["range_score"] >= 1.0 and
            gap >= OP_MAPPING_FASTTRACK_GAP
        )

        if fast_track:
            _safe_print(f"  → 快速通道: {top1['local_name']} (gap={gap:.3f})")
            result[key] = {
                "object_prop_uri": top1["uri"],
                "confidence": "high",
                "scenario": scenario,
                "method": "fast_track",
                "candidates_used": top3
            }
            if checkpoint_path:
                _safe_dump_json(checkpoint_path, result)
            continue

        try:
            llm_result = _llm_select_op_smd(
                name_hint, domain_class, range_class, top3
            )

            selected_uri = llm_result.get("selected_uri")
            confidence = llm_result.get("confidence", "medium")
            reason = llm_result.get("reason", "")

            allowed_uris = {c.get("uri") for c in top3 if c.get("uri")}
            if selected_uri and selected_uri not in allowed_uris:
                reason = (reason + "；LLM 返回 URI 不在候选集内，已回退 top1").strip("；")
                selected_uri = top1["uri"]
                confidence = "medium"

            if not selected_uri:
                # 受控回退：避免关系边大面积为 null（会直接导致查询挂掉）
                if rel_type == "SR":
                    rescue = True
                else:
                    rescue = (
                        top1["score"] >= OP_MAPPING_RESCUE_MIN_SCORE and
                        (
                            top1["domain_score"] >= OP_MAPPING_RESCUE_MIN_SIDE_SCORE or
                            top1["range_score"] >= OP_MAPPING_RESCUE_MIN_SIDE_SCORE or
                            gap >= OP_MAPPING_RESCUE_MIN_GAP
                        )
                    )
                if rescue:
                    reason = (reason + "；LLM 未给出结果，依据结构/语义分数回退 top1").strip("；")
                    selected_uri = top1["uri"]
                    confidence = "medium"

            _safe_print(f"  → LLM 选择: {_local_name(selected_uri) if selected_uri else 'null'} [{confidence}]")
            _safe_print(f"    理由: {reason}")

            result[key] = {
                "object_prop_uri": selected_uri,
                "confidence": confidence,
                "scenario": scenario,
                "method": "llm_select",
                "reason": reason,
                "candidates_used": top3
            }
            if checkpoint_path:
                _safe_dump_json(checkpoint_path, result)

        except Exception as e:
            _safe_print(f"  [WARN] LLM 选择失败: {e}，回退到 top-1")
            result[key] = {
                "object_prop_uri": top1["uri"],
                "confidence": "low",
                "scenario": scenario,
                "method": "fallback_top1",
                "candidates_used": top3
            }
            if checkpoint_path:
                _safe_dump_json(checkpoint_path, result)

    fast_count = sum(1 for v in result.values() if v["method"] == "fast_track")
    llm_count = sum(1 for v in result.values() if v["method"] == "llm_select")
    fallback_count = sum(1 for v in result.values() if v["method"] == "fallback_top1")
    _safe_print(f"\n\nOP Mapping Step 1 完成: 快速通道={fast_count}, LLM精选={llm_count}, 回退={fallback_count}")

    return result


# ============================================================
#  Step 2: 从本体出发 —— 补全孤儿列
# ============================================================

def _collect_orphan_columns(final_alignment: dict) -> list:
    orphans = []
    for table_name, entry in final_alignment.items():
        pattern = entry.get("pattern", "SE")
        if pattern == "SR":
            continue

        if pattern == "SH":
            table_class = entry.get("sub_class_uri")
        else:
            table_class = entry.get("class_uri")

        columns = entry.get("columns", {})
        for col_name, col_info in columns.items():
            if col_info.get("role") != "data_attr":
                continue
            if col_info.get("prop_uri") is not None:
                continue

            is_inv = col_name.lower().endswith("_inv") or "_inv" in col_name.lower()

            orphans.append({
                "table": table_name,
                "column": col_name,
                "table_class_uri": table_class,
                "pattern": pattern,
                "is_inv": is_inv
            })

    return orphans


def _column_is_fk(enriched_schema: dict, table_name: str, col_name: str) -> bool:
    table_info = (enriched_schema or {}).get(table_name, {}) or {}
    for fk in table_info.get("foreign_keys", []) or []:
        if fk.get("column") == col_name:
            return True
    return False


def _get_covered_ops(step1_result: dict) -> set:
    covered = set()
    for key, info in step1_result.items():
        uri = info.get("object_prop_uri")
        if uri:
            covered.add(uri)
    return covered


def _get_covered_dps(final_alignment: dict) -> set:
    covered = set()
    for table_name, entry in final_alignment.items():
        for col_name, col_info in entry.get("columns", {}).items():
            uri = col_info.get("prop_uri")
            if uri:
                covered.add(uri)
    return covered


def _score_orphan_vs_op(orphan, op_uri, op_info, all_table_classes):
    col_name = orphan["column"]
    table_class = orphan["table_class_uri"]
    is_inv = orphan["is_inv"]
    op_local = _local_name(op_uri)

    name_score = _name_overlap(col_name, op_local)

    if is_inv:
        col_base = col_name.lower().replace("_inv", "").strip("_")
        inv_name_score = _name_overlap(col_base, op_local)
        name_score = max(name_score, inv_name_score)

    domain_score = hint_match(table_class, op_info.get("domain", []))

    op_ranges = op_info.get("range", [])
    range_score = 0.0
    for r in op_ranges:
        r_local = _local_name(r)
        if r_local in all_table_classes:
            range_score = 1.0
            break
        for tbl, cls in all_table_classes.items():
            if cls and _name_overlap(r_local, _local_name(cls)) > 0.7:
                range_score = 0.5
                break

    inv_bonus = 0.0
    if is_inv:
        for r in op_ranges:
            if table_class and (r == table_class or
                _local_name(r).lower() == _local_name(table_class).lower()):
                inv_bonus = 1.0
                break
        if inv_bonus == 0:
            for d in op_info.get("domain", []):
                if d in [cls for cls in all_table_classes.values() if cls]:
                    inv_bonus = 0.5
                    break

    total = name_score * 0.4 + domain_score * 0.3 + range_score * 0.2 + inv_bonus * 0.1
    return round(total, 4), {
        "name_score": round(name_score, 3),
        "domain_score": round(domain_score, 3),
        "range_score": round(range_score, 3),
        "inv_bonus": round(inv_bonus, 3)
    }


def _llm_confirm_orphan_op(orphan, candidates):
    col_name = orphan["column"]
    table_name = orphan["table"]
    table_class = orphan["table_class_uri"]
    is_inv = orphan["is_inv"]

    inv_note = ""
    if is_inv:
        inv_note = (
            "\n注意：该列名含 '_Inv' 后缀，强烈暗示这是某个 ObjectProperty 的反向引用。"
            "\n如果选择了某个 OP，请说明方向（正向 or 反向）。"
        )

    prompt = f"""
## 任务
表 `{table_name}`（Class: {table_class}）中，列 `{col_name}` 在 DP 映射阶段未找到对应的 DatatypeProperty。
现在从本体的 ObjectProperty 中寻找可能的匹配。
{inv_note}

## 候选 ObjectProperty（按匹配分排序）
{json.dumps(candidates, indent=2, ensure_ascii=False)}

## 输出格式（严格 JSON）
{{
  "selected_uri": "选中的 URI（必须来自候选列表，或 null）",
  "direction": "normal / inverse",
  "confidence": "high / medium / low",
  "reason": "一句话理由"
}}
"""
    return _call_llm(prompt)


"""
Object Property Mapping 采用的是双向匹配思想。第一轮从数据库 schema 出发匹配本体属性；第二轮再从本体未覆盖属性反向检查数据库剩余列，做兜底补全，减少漏映射。
"""
def run_object_property_mapping_step2(
    final_alignment: dict,
    ontology: dict,
    enriched_schema: dict,
    step1_result: dict
) -> dict:
    conn = _get_conn()

    orphans = _collect_orphan_columns(final_alignment)

    print(f"\n  孤儿列数量: {len(orphans)}")

    covered_ops = _get_covered_ops(step1_result)
    covered_dps = _get_covered_dps(final_alignment)

    all_ops = ontology["object_properties"]
    all_dps = ontology["datatype_properties"]

    uncovered_ops = {uri: info for uri, info in all_ops.items() if uri not in covered_ops}
    uncovered_dps = {uri: info for uri, info in all_dps.items() if uri not in covered_dps}

    print(f"  未覆盖 ObjectProperty: {len(uncovered_ops)}/{len(all_ops)}")
    print(f"  未覆盖 DatatypeProperty: {len(uncovered_dps)}/{len(all_dps)}")

    all_table_classes = {}
    for tbl, entry in final_alignment.items():
        if entry.get("pattern") == "SH":
            all_table_classes[tbl] = entry.get("sub_class_uri")
        elif entry.get("pattern") == "SR":
            continue
        else:
            all_table_classes[tbl] = entry.get("class_uri")

    orphan_result = {}

    for i, orphan in enumerate(orphans, 1):
        key = f"{orphan['table']}.{orphan['column']}"
        print(f"\n[Step2 孤儿 {i}/{len(orphans)}] {key} (is_inv={orphan['is_inv']})")

        # 只允许有结构信号的孤儿列走 ObjectProperty 兜底：
        # 1) 列名含 _inv（反向关系信号）
        # 2) 或该列本身是 FK（结构关系信号）
        # 普通 data_attr（非 inv 且非 FK）不做 OP 兜底，避免把数值/文本属性误映射成对象关系。
        is_fk_col = _column_is_fk(enriched_schema, orphan["table"], orphan["column"])
        if (not orphan["is_inv"]) and (not is_fk_col):
            print("  → 无结构关系信号（非_inv且非FK），跳过 ObjectProperty 兜底")
            orphan_result[key] = {
                "object_prop_uri": None,
                "confidence": "low",
                "method": "skip_non_relational_orphan",
                "reason": "non_inv_non_fk_orphan",
                "is_inv": False,
                "is_fk_col": False
            }
            continue

        scored = []
        for op_uri, op_info in uncovered_ops.items():
            score, details = _score_orphan_vs_op(orphan, op_uri, op_info, all_table_classes)
            scored.append({
                "uri": op_uri,
                "local_name": _local_name(op_uri),
                "score": score,
                "domain": op_info.get("domain", []),
                "range": op_info.get("range", []),
                **details
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        top3 = scored[:3]

        for c in top3:
            print(f"  候选: {c['local_name']} (score={c['score']}, name={c['name_score']}, "
                  f"dom={c['domain_score']}, rng={c['range_score']}, inv={c['inv_bonus']})")

        if not top3 or top3[0]["score"] < OP_MAPPING_STEP2_MIN_SCORE:
            print(f"  → 无合适候选 (top1={top3[0]['score'] if top3 else 0}), 跳过")
            orphan_result[key] = {
                "object_prop_uri": None,
                "confidence": "low",
                "method": "no_match",
                "reason": "候选分数过低",
                "is_inv": orphan["is_inv"],
                "is_fk_col": is_fk_col
            }
            continue

        top1 = top3[0]
        gap = top1["score"] - (top3[1]["score"] if len(top3) > 1 else 0)
        if (
            top1["name_score"] >= OP_MAPPING_STEP2_FASTTRACK_NAME
            and top1["domain_score"] >= OP_MAPPING_STEP2_FASTTRACK_DOMAIN
            and gap >= OP_MAPPING_STEP2_FASTTRACK_GAP
        ):
            direction = "inverse" if orphan["is_inv"] else "normal"
            print(f"  → 快速通道: {top1['local_name']} (gap={gap:.3f}, direction={direction})")
            orphan_result[key] = {
                "object_prop_uri": top1["uri"],
                "direction": direction,
                "confidence": "high",
                "method": "fast_track",
                "candidates_used": top3,
                "is_inv": orphan["is_inv"],
                "is_fk_col": is_fk_col
            }
            continue

        try:
            llm_result = _llm_confirm_orphan_op(orphan, top3)
            selected = llm_result.get("selected_uri")
            direction = llm_result.get("direction", "normal")
            confidence = llm_result.get("confidence", "medium")
            reason = llm_result.get("reason", "")

            local = _local_name(selected) if selected else "null"
            print(f"  → LLM: {local} [{confidence}] (direction={direction})")
            print(f"    理由: {reason}")

            orphan_result[key] = {
                "object_prop_uri": selected,
                "direction": direction,
                "confidence": confidence,
                "method": "llm_select",
                "reason": reason,
                "candidates_used": top3,
                "is_inv": orphan["is_inv"],
                "is_fk_col": is_fk_col
            }
        except Exception as e:
            print(f"  [WARN] LLM 失败: {e}, 回退 top-1")
            orphan_result[key] = {
                "object_prop_uri": top1["uri"],
                "direction": "inverse" if orphan["is_inv"] else "normal",
                "confidence": "low",
                "method": "fallback_top1",
                "candidates_used": top3,
                "is_inv": orphan["is_inv"],
                "is_fk_col": is_fk_col
            }

    conn.close()

    orphan_matched = sum(1 for v in orphan_result.values() if v.get("object_prop_uri"))
    print(f"\n\nStep 2 完成: 孤儿列匹配={orphan_matched}/{len(orphans)}")

    return {
        "orphan_matches": orphan_result
    }


# ============================================================
#  主程序
# ============================================================

if __name__ == "__main__":
    from config import OUTPUT_DIR, ONTOLOGY_PATH   # ← 路径从 config 读取

    ALIGNMENT_PATH = os.path.join(OUTPUT_DIR, "final_alignment.json")
    SCHEMA_PATH    = os.path.join(OUTPUT_DIR, "enriched_schema.json")

    if not os.path.exists(ALIGNMENT_PATH) or not os.path.exists(SCHEMA_PATH):
        print("缺少上一阶段的输出文件，请先运行 real_value_enhancement_agent.py")
        exit(1)

    with open(ALIGNMENT_PATH, "r", encoding="utf-8") as f:
        final_alignment = json.load(f)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        enriched_schema = json.load(f)

    ontology = read_ontology(ONTOLOGY_PATH)

    print(f"已加载 final_alignment: {len(final_alignment)} 张表")
    print(f"已加载 enriched_schema: {len(enriched_schema)} 张表")
    print(f"本体 ObjectProperty 数量: {len(ontology['object_properties'])}")

    print("\n" + "=" * 60)
    print("OP Mapping Step 0: 场景判定")
    print("=" * 60)
    scenarios = determine_op_mapping_scenarios(final_alignment, enriched_schema)

    print("\n" + "=" * 60)
    print("OP Mapping Step 1: ObjectProperty 选择")
    print("=" * 60)
    checkpoint_path = os.path.join(OUTPUT_DIR, "op_mapping_step1_result.partial.json")
    op_mapping_result = run_object_property_mapping_step1(
        scenarios,
        ontology,
        enriched_schema,
        checkpoint_path=checkpoint_path,
    )

    print("\n" + "=" * 60)
    print("OP Mapping Step 2: 从本体出发补全")
    print("=" * 60)
    step2_result = run_object_property_mapping_step2(final_alignment, ontology, enriched_schema, op_mapping_result)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    json.dump(op_mapping_result, 
              open(os.path.join(OUTPUT_DIR, "op_mapping_step1_result.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    if os.path.exists(checkpoint_path):
        try:
            os.remove(checkpoint_path)
            _safe_print("已清理 Step1 checkpoint。")
        except Exception as e:
            _safe_print(f"[WARN] 清理 Step1 checkpoint 失败: {e}")
    json.dump(step2_result,
              open(os.path.join(OUTPUT_DIR, "op_mapping_step2_result.json"), "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)

    full_op_mapping = {
        "step1": op_mapping_result,
        "step2_orphans": step2_result["orphan_matches"]
    }
    full_path = os.path.join(OUTPUT_DIR, "op_mapping_full_result.json")
    json.dump(full_op_mapping, open(full_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    print(f"\n已保存到 {full_path}")
