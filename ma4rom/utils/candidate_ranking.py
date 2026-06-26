"""
utils/candidate_ranking.py  ——  候选集排序工具函数

candidate_generation.py 和当前映射流程都需要对 Class / ObjectProperty /
DatatypeProperty 候选进行相似度排序，统一放这里避免重复。

用法：
    from utils.candidate_ranking import (
        rank_class_candidates,
        rank_object_prop_candidates,
        rank_datatype_prop_candidates,
    )
"""

import re

from config import (
    DP_MAPPING_CANDIDATE_DOMAIN_WEIGHT,
    DP_MAPPING_CANDIDATE_TEXT_WEIGHT,
)
from utils.name_similarity import name_overlap
from utils.ontology_utils import local_name, hint_match


STOP_TOKENS = {
    "hst", "all", "inc", "npdid", "ncs", "totalt", "poly", "petreg", "id"
}


def _camel_split_tokens(name: str) -> set[str]:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return {t.lower() for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) >= 2}


def _table_tokens(table_name: str) -> set[str]:
    raw = {t.lower() for t in re.split(r"[^a-z0-9]+", table_name.lower()) if len(t) >= 2}
    return raw - STOP_TOKENS


def _token_overlap_score(name: str, class_local_name: str) -> float:
    t_toks = _table_tokens(name)
    c_toks = _camel_split_tokens(class_local_name)
    if not t_toks or not c_toks:
        return 0.0
    covered = 0
    for tt in t_toks:
        hit = False
        for ct in c_toks:
            if tt == ct:
                hit = True
                break
            if len(tt) >= 4 and len(ct) >= 4 and (tt.startswith(ct) or ct.startswith(tt)):
                hit = True
                break
        if hit:
            covered += 1
    return covered / len(t_toks)


def rank_class_candidates(name: str, classes: list, top_k: int = 5) -> list:
    """
    在所有 OWL Class 里，按名称相似度对 name 排序，返回 top_k。

    参数:
        name:    待匹配的表名或列名
        classes: 本体 class URI 列表（来自 read_ontology()["classes"]）
        top_k:   返回候选数量

    返回:
        [{"uri": ..., "local_name": ..., "score": float}, ...]
    """
    scored = []
    for uri in classes:
        lname = local_name(uri)
        syntax_score = name_overlap(name, lname)
        token_score = _token_overlap_score(name, lname)
        score = max(syntax_score, token_score)
        scored.append({
            "uri": uri,
            "local_name": lname,
            "score": round(score, 3),
            "syntax_score": round(syntax_score, 3),
            "token_score": round(token_score, 3),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def rank_object_prop_candidates(
    name: str,
    object_props: dict,
    domain_hint: str,
    range_hint: str,
    top_k: int = 5,
    ontology: dict | None = None,
) -> list:
    """
    在 ObjectProperty 里做“双赛道”排序：
      - 语法赛道: syntax_score（名称相似）
      - 语义赛道: semantic_score（domain/range 语义可满足）

    最终输出保留 top_k，但由两赛道共同入围（避免互相湮灭）：
      1) 分别计算 syntax_top_k 与 semantic_top_k
      2) 合并去重
      3) 在候选上附带两赛道排名与来源
      4) 再截断到 top_k

    参数:
        name:         待匹配的表名或列名
        object_props: 本体 ObjectProperty dict（来自 read_ontology()["object_properties"]）
        domain_hint:  DP 映射确认的 domain Class URI（可为 None）
        range_hint:   DP 映射确认的 range Class URI（可为 None）
        top_k:        返回候选数量
        ontology:     read_ontology() 的完整结果（用于 subclass/union 语义匹配）

    返回:
        [{"uri", "local_name", "score", "name_score",
          "domain", "range", "domain_score", "range_score"}, ...]
    """
    scored = []
    for uri, info in object_props.items():
        lname = local_name(uri)
        syntax_score = name_overlap(name, lname)
        prop_domains = info.get("domain", [])
        prop_ranges  = info.get("range", [])
        domain_score = hint_match(domain_hint, prop_domains, ontology=ontology)
        range_score  = hint_match(range_hint, prop_ranges, ontology=ontology)

        semantic_score = (domain_score + range_score) / 2.0
        total = syntax_score * 0.5 + semantic_score * 0.5

        scored.append({
            "uri":          uri,
            "local_name":   lname,
            "score":        round(total, 3),
            "name_score":   round(syntax_score, 3),  # 兼容旧字段
            "syntax_score": round(syntax_score, 3),
            "semantic_score": round(semantic_score, 3),
            "domain":       prop_domains,
            "range":        prop_ranges,
            "domain_score": round(domain_score, 3),
            "range_score":  round(range_score, 3),
        })

    # 语法赛道排名
    syntax_sorted = sorted(
        scored,
        key=lambda x: (x["syntax_score"], x["semantic_score"], x["score"]),
        reverse=True,
    )
    for idx, c in enumerate(syntax_sorted, 1):
        c["syntax_rank"] = idx

    # 语义赛道排名
    semantic_sorted = sorted(
        scored,
        key=lambda x: (x["semantic_score"], x["domain_score"], x["range_score"], x["syntax_score"]),
        reverse=True,
    )
    for idx, c in enumerate(semantic_sorted, 1):
        c["semantic_rank"] = idx

    syntax_pool = {c["uri"] for c in syntax_sorted[:top_k]}
    semantic_pool = {c["uri"] for c in semantic_sorted[:top_k]}
    pool = syntax_pool | semantic_pool

    selected = [c for c in scored if c["uri"] in pool]
    for c in selected:
        in_s = c["uri"] in syntax_pool
        in_m = c["uri"] in semantic_pool
        if in_s and in_m:
            c["track_source"] = "both"
        elif in_s:
            c["track_source"] = "syntax"
        else:
            c["track_source"] = "semantic"

    ordered = sorted(
        selected,
        key=lambda x: (
            0 if x["track_source"] == "both" else 1,
            min(x["syntax_rank"], x["semantic_rank"]),
            x["syntax_rank"] + x["semantic_rank"],
            -x["semantic_score"],
            -x["syntax_score"],
            -x["score"],
        ),
    )

    # 保证两赛道头部候选至少有机会进入最终 top_k
    final = []
    seen = set()

    def _push(candidate):
        if not candidate:
            return
        uri = candidate.get("uri")
        if not uri or uri in seen:
            return
        seen.add(uri)
        final.append(candidate)

    syntax_best = next((c for c in syntax_sorted if c["uri"] in pool), None)
    semantic_best = next((c for c in semantic_sorted if c["uri"] in pool), None)
    _push(syntax_best)
    _push(semantic_best)

    for c in ordered:
        if len(final) >= top_k:
            break
        _push(c)

    if len(final) < top_k:
        for c in syntax_sorted:
            if len(final) >= top_k:
                break
            _push(c)

    return final[:top_k]


def rank_datatype_prop_candidates(
    name: str,
    datatype_props: dict,
    domain_hint: str,
    top_k: int = 5,
) -> list:
    """
    在 DatatypeProperty 里，综合名称相似度 + domain 匹配度排序。

    得分公式：
        total = 名称相似度 × λ_text + domain 匹配 × λ_dom

    参数:
        name:           待匹配的表名或列名
        datatype_props: 本体 DatatypeProperty dict（来自 read_ontology()）
        domain_hint:    DP 映射确认的 domain Class URI（可为 None）
        top_k:          返回候选数量

    返回:
        [{"uri", "local_name", "score", "name_score",
          "domain", "domain_score"}, ...]
    """
    scored = []
    for uri, info in datatype_props.items():
        lname = local_name(uri)
        name_score   = name_overlap(name, lname)
        prop_domains = info.get("domain", [])
        domain_score = hint_match(domain_hint, prop_domains)
        total = (
            name_score * DP_MAPPING_CANDIDATE_TEXT_WEIGHT
            + domain_score * DP_MAPPING_CANDIDATE_DOMAIN_WEIGHT
        )
        scored.append({
            "uri":          uri,
            "local_name":   lname,
            "score":        round(total, 3),
            "name_score":   round(name_score, 3),
            "domain":       prop_domains,
            "domain_score": round(domain_score, 3),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
