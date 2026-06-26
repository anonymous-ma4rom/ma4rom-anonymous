from rdflib import Graph, RDF, RDFS, OWL, BNode
from rdflib.collection import Collection
import json
from utils.name_similarity import name_overlap


def _transitive_closure(direct_map: dict, universe: set[str]) -> dict:
    """
    direct_map: node -> [neighbors]
    返回每个节点的传递闭包（不含自身）。
    """
    closure = {}

    def dfs(node: str, seen: set[str]):
        for nxt in direct_map.get(node, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            dfs(nxt, seen)

    for node in universe:
        visited = set()
        dfs(node, visited)
        closure[node] = sorted(visited)
    return closure


def _build_union_members(g: Graph) -> dict:
    """
    提取出所有出现在 domain/range 里的 union class expression 成员。
    返回:
      {
        "blank_node_id": ["ClassA", "ClassB", ...]
      }
    """
    expr_nodes = set()
    for _, _, obj in g.triples((None, RDFS.domain, None)):
        expr_nodes.add(obj)
    for _, _, obj in g.triples((None, RDFS.range, None)):
        expr_nodes.add(obj)

    union_members = {}
    for expr in expr_nodes:
        for list_node in g.objects(expr, OWL.unionOf):
            members = []
            try:
                for m in Collection(g, list_node):
                    if isinstance(m, BNode):
                        continue
                    members.append(str(m))
            except Exception:
                continue
            if members:
                union_members[str(expr)] = sorted(set(members))

    return union_members


def read_ontology(path: str) -> dict:
    """
    解析本体文件（Turtle 格式），返回 classes / object_properties / datatype_properties。

    参数:
        path: 本体文件路径（从 config.ONTOLOGY_PATH 传入，不在此处硬编码）
    """
    g = Graph()
    g.parse(path, format="turtle")

    classes = set()
    object_properties = {}
    datatype_properties = {}
    subclass_of = {}
    children_of = {}

    # 读取 Classes（显式声明）
    for s in g.subjects(RDF.type, OWL.Class):
        classes.add(str(s))

    # 兼容很多本体的“隐式类声明”写法：
    # 仅通过 rdfs:subClassOf 出现，但没有显式 rdf:type owl:Class
    for child, parent in g.subject_objects(RDFS.subClassOf):
        if not isinstance(child, BNode):
            classes.add(str(child))
        if not isinstance(parent, BNode):
            classes.add(str(parent))

    # 兼容通过 domain/range 间接出现的命名类
    # （跳过 blank node class expression）
    for _, _, d in g.triples((None, RDFS.domain, None)):
        if not isinstance(d, BNode):
            classes.add(str(d))
    for _, _, r in g.triples((None, RDFS.range, None)):
        if not isinstance(r, BNode):
            classes.add(str(r))

    # 读取 class hierarchy (child -> parent)
    # 仅保留命名 Class（跳过 blank node / 限制表达式）
    for child, parent in g.subject_objects(RDFS.subClassOf):
        if isinstance(child, BNode) or isinstance(parent, BNode):
            continue
        child_uri = str(child)
        parent_uri = str(parent)
        if child_uri not in subclass_of:
            subclass_of[child_uri] = []
        if parent_uri not in subclass_of[child_uri]:
            subclass_of[child_uri].append(parent_uri)

        if parent_uri not in children_of:
            children_of[parent_uri] = []
        if child_uri not in children_of[parent_uri]:
            children_of[parent_uri].append(child_uri)

    # 读取 Object Properties + domain + range
    for prop in g.subjects(RDF.type, OWL.ObjectProperty):
        domain = [str(d) for d in g.objects(prop, RDFS.domain)]
        range_ = [str(r) for r in g.objects(prop, RDFS.range)]

        object_properties[str(prop)] = {
            "domain": domain,
            "range": range_
        }

    # 读取 Datatype Properties + domain + range
    for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
        domain = [str(d) for d in g.objects(prop, RDFS.domain)]
        range_ = [str(r) for r in g.objects(prop, RDFS.range)]

        datatype_properties[str(prop)] = {
            "domain": domain,
            "range": range_
        }

    # 传递闭包：child -> all ancestors / parent -> all descendants
    ancestors_of = _transitive_closure(subclass_of, classes)
    descendants_of = _transitive_closure(children_of, classes)

    union_members = _build_union_members(g)

    return {
        "classes": sorted(classes),
        "object_properties": object_properties,
        "datatype_properties": datatype_properties,
        "subclass_of": subclass_of,
        "children_of": children_of,
        "ancestors_of": ancestors_of,
        "descendants_of": descendants_of,
        "union_members": union_members,
    }


def local_name(uri: str) -> str:
    """
    从 URI 中提取本地名称。

    "http://conference#Paper"    → "Paper"
    "http://conference#hasTitle" → "hasTitle"
    "http://example.org/Person"  → "Person"
    """
    if "#" in uri:
        return uri.split("#")[-1]
    return uri.rstrip("/").split("/")[-1]


def _semantic_class_match_score(hint: str, candidate: str, ontology: dict | None = None) -> float:
    """
    单个 class 间匹配分：
      - 精确匹配: 1.0
      - 子类/父类可达: 0.85
      - 本地名相似: 0.5
      - 不匹配: 0.0
    """
    if not hint or not candidate:
        return 0.0

    if candidate == hint:
        return 1.0

    hint_local = local_name(hint)
    cand_local = local_name(candidate)
    if cand_local.lower() == hint_local.lower():
        return 1.0

    if ontology:
        ancestors_of = ontology.get("ancestors_of", {})
        if candidate in ancestors_of.get(hint, []):
            return 0.85
        if hint in ancestors_of.get(candidate, []):
            return 0.85

    if name_overlap(hint_local, cand_local) > 0.6:
        return 0.5
    return 0.0


def hint_match(hint: str, prop_values: list, ontology: dict | None = None) -> float:
    """
    判断 hint（Class URI）是否出现在属性的 domain/range 声明列表中。

    打分规则：
      精确匹配（URI 完全相同）    → 1.0
      本地名完全匹配（忽略大小写）→ 1.0
      名称部分相似（>0.6）        → 0.5
      属性无 domain/range 声明    → 0.3（不惩罚，本体可能不完整）
      完全不匹配                  → 0.0
    """
    if not hint:
        return 0.3
    if not prop_values:
        return 0.3

    union_members = (ontology or {}).get("union_members", {})
    best = 0.0

    for val in prop_values:
        # 若声明是 union class expression，展开其成员再匹配
        members = union_members.get(val, [])
        if members:
            member_best = 0.0
            for m in members:
                member_best = max(member_best, _semantic_class_match_score(hint, m, ontology))
            best = max(best, member_best)
            continue

        best = max(best, _semantic_class_match_score(hint, val, ontology))

    return best


if __name__ == "__main__":
    from config import ONTOLOGY_PATH   # ← 路径从 config 读取
    ontology = read_ontology(ONTOLOGY_PATH)
    print(json.dumps(ontology, indent=4))
