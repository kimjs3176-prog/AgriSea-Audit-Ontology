"""지식그래프 조회: 온톨로지 요약(클래스·관계·규칙·개념체계), 개체 설명, 이름 검색.

화면의 '온톨로지' 탭이 쓴다. 모든 값은 지식그래프(pyoxigraph)에 SPARQL로 묻는다.
추론으로 생긴 사실은 규칙별 이름 있는 그래프에도 있으므로, 사실마다 원 데이터인지 추론인지 표시한다.
"""
from __future__ import annotations

from .reasoner import INFERRED_PREFIX, PREFIXES, RULE_BY_ID, RULES

AG = "https://w3id.org/agrisea/ontology#"
INF = f'STRSTARTS(STR(?g), "{INFERRED_PREFIX}")'


def _rows(kg, q: str) -> list[list]:
    res = kg.query(PREFIXES + q)
    cols = [v.value for v in res.variables]
    out = []
    for sol in res:
        out.append([(sol[c].value if sol[c] is not None else None) for c in cols])
    return out


def _short(iri: str) -> str:
    return iri.split("#")[-1] if "#" in iri else iri.rstrip("/").split("/")[-1]


def summary(kg) -> dict:
    """클래스(원 데이터·추론 개체 수), 관계(원 데이터·추론 사실 수), 규칙, 개념체계."""
    labels = dict(_rows(kg, "SELECT ?x ?l WHERE { ?x rdfs:label ?l . FILTER(STRSTARTS(STR(?x), STR(ag:))) }"))
    comments = dict(_rows(kg, "SELECT ?x ?c WHERE { ?x rdfs:comment ?c . FILTER(STRSTARTS(STR(?x), STR(ag:))) }"))
    classes = [r[0] for r in _rows(kg, "SELECT ?c WHERE { ?c a owl:Class . FILTER(isIRI(?c) && STRSTARTS(STR(?c), STR(ag:))) }")]
    parents: dict[str, str] = {}
    for c, p in _rows(kg, "SELECT ?c ?p WHERE { ?c rdfs:subClassOf ?p . FILTER(isIRI(?p)) }"):
        if c in classes and p.startswith(AG) and c not in parents:
            parents[c] = p
    total = dict(_rows(kg, "SELECT ?c (COUNT(DISTINCT ?x) AS ?n) WHERE { ?x a ?c . FILTER(STRSTARTS(STR(?c), STR(ag:))) } GROUP BY ?c"))
    inferred = dict(_rows(kg, f"SELECT ?c (COUNT(DISTINCT ?x) AS ?n) WHERE {{ GRAPH ?g {{ ?x a ?c }} FILTER({INF}) }} GROUP BY ?c"))
    cls = [{"iri": c, "id": _short(c), "label": labels.get(c, _short(c)), "comment": comments.get(c, ""),
            "parent": parents.get(c), "count": int(total.get(c, 0)), "inferred": int(inferred.get(c, 0))}
           for c in classes]

    props = _rows(kg, "SELECT ?p ?t WHERE { ?p a ?t . VALUES ?t { owl:ObjectProperty owl:DatatypeProperty } "
                      "FILTER(STRSTARTS(STR(?p), STR(ag:))) }")
    ptotal = dict(_rows(kg, "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o . FILTER(STRSTARTS(STR(?p), STR(ag:))) } GROUP BY ?p"))
    pinf = dict(_rows(kg, f"SELECT ?p (COUNT(*) AS ?n) WHERE {{ GRAPH ?g {{ ?s ?p ?o }} FILTER({INF}) }} GROUP BY ?p"))
    dom = dict(_rows(kg, "SELECT ?p ?d WHERE { ?p rdfs:domain ?d . FILTER(isIRI(?d)) }"))
    rng = dict(_rows(kg, "SELECT ?p ?r WHERE { ?p rdfs:range ?r . FILTER(isIRI(?r)) }"))
    def cls_name(c: str) -> str:
        # owl:unionOf 로 정의한 정의역·치역은 구성 클래스를 '또는'으로 잇는다
        if c.startswith(str(AG)):
            return labels.get(c, _short(c))
        ms = [m for (m,) in _rows(kg, f"SELECT ?m WHERE {{ <{c}> owl:unionOf/rdf:rest*/rdf:first ?m }}")]
        return " 또는 ".join(labels.get(m, _short(m)) for m in ms) or _short(c)
    axioms: dict[str, list[str]] = {}
    for p, q in _rows(kg, "SELECT ?p ?q WHERE { ?p owl:inverseOf ?q }"):
        axioms.setdefault(p, []).append(f"역관계: {labels.get(q, _short(q))}")
        axioms.setdefault(q, []).append(f"역관계: {labels.get(p, _short(p))}")
    for p, q in _rows(kg, "SELECT ?p ?q WHERE { ?p rdfs:subPropertyOf ?q . FILTER(STRSTARTS(STR(?p), STR(ag:))) }"):
        axioms.setdefault(p, []).append(f"상위 관계: {labels.get(q, _short(q))}")
    for (p,) in _rows(kg, "SELECT ?p WHERE { ?p a owl:SymmetricProperty }"):
        axioms.setdefault(p, []).append("대칭")
    for (p,) in _rows(kg, "SELECT ?p WHERE { ?p a owl:FunctionalProperty }"):
        axioms.setdefault(p, []).append("값 하나(함수)")
    for (p,) in _rows(kg, "SELECT ?p WHERE { ?p owl:propertyChainAxiom ?l }"):
        axioms.setdefault(p, []).append("관계 연쇄")
    prop = [{"iri": p, "id": _short(p), "label": labels.get(p, _short(p)),
             "kind": "object" if t.endswith("ObjectProperty") else "data",
             "domain": cls_name(dom[p]) if p in dom else "",
             "range": cls_name(rng[p]) if p in rng else "",
             "count": int(ptotal.get(p, 0)), "inferred": int(pinf.get(p, 0)), "axioms": sorted(set(axioms.get(p, [])))}
            for p, t in props]
    prop.sort(key=lambda x: (x["kind"], -x["count"]))

    rule_counts = dict(_rows(kg, "SELECT ?r ?n WHERE { ?r a ag:InferenceRule ; ag:inferredCount ?n }"))
    rules = [{"id": r.id, "kind": r.kind, "label": r.label, "desc": r.desc, "head": r.head.strip(),
              "count": int(rule_counts.get(f"https://w3id.org/agrisea/id/rule/{r.id}", 0))} for r in RULES]

    schemes = []
    for scheme, title in (("IssueScheme", "쟁점 분류체계"), ("FollowUpStatusScheme", "약속 이행 판정"),
                          ("ActionTypeScheme", "약속 조치 유형")):
        rows = _rows(kg, f"""SELECT ?c ?l ?b WHERE {{ ?c skos:inScheme ag:{scheme} ; skos:prefLabel ?l .
                             OPTIONAL {{ ?c skos:broader ?b }} }}""")
        use = {"IssueScheme": "ag:concernsIssue", "FollowUpStatusScheme": "ag:followUpStatus",
               "ActionTypeScheme": "ag:actionType"}[scheme]
        uses = dict(_rows(kg, f"SELECT ?c (COUNT(?s) AS ?n) WHERE {{ ?s {use} ?c }} GROUP BY ?c"))
        schemes.append({"id": scheme, "label": title, "concepts": [
            {"iri": c, "label": l, "broader": b, "count": int(uses.get(c, 0))} for c, l, b in rows]})

    n_all = int(_rows(kg, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")[0][0])
    n_inf = int(_rows(kg, f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH ?g {{ ?s ?p ?o }} FILTER({INF}) }}")[0][0])
    return {"triples": n_all, "inferred": n_inf, "asserted": n_all - n_inf,
            "classes": cls, "properties": prop, "rules": rules, "schemes": schemes}


LABEL = """OPTIONAL { ?X rdfs:label ?lab0 } OPTIONAL { ?X skos:prefLabel ?lab1 }
OPTIONAL { ?X ag:text ?lab2 } BIND(COALESCE(?lab1, ?lab0, SUBSTR(?lab2, 1, 90)) AS ?LBL)"""


def _label_of(kg, iri: str) -> str:
    r = _rows(kg, f"SELECT ?LBL WHERE {{ BIND(<{iri}> AS ?X) {LABEL} }} LIMIT 1")
    return (r and r[0][0]) or _short(iri)


def _types(kg, iri: str) -> list[dict]:
    rows = _rows(kg, f"""SELECT ?t ?l (SAMPLE(?g) AS ?gg) WHERE {{ <{iri}> a ?t . OPTIONAL {{ ?t rdfs:label ?l }}
                          OPTIONAL {{ GRAPH ?g {{ <{iri}> a ?t }} FILTER({INF}) }}
                          FILTER(STRSTARTS(STR(?t), STR(ag:))) }} GROUP BY ?t ?l""")
    return [{"iri": t, "label": l or _short(t), "inferred": bool(g),
             "rule": g[len(INFERRED_PREFIX):] if g else None} for t, l, g in rows]


def describe(kg, iri: str, per_prop: int = 12) -> dict:
    """개체 한 개: 이름, 유형(원 데이터·추론), 나가는 관계와 들어오는 관계(관계별 개수와 일부)."""
    if not iri or any(c in iri for c in '<>"{}| \\^`'):
        raise ValueError("잘못된 개체 주소")
    plabels = dict(_rows(kg, "SELECT ?p ?l WHERE { ?p rdfs:label ?l . ?p a ?t . VALUES ?t { owl:ObjectProperty owl:DatatypeProperty rdf:Property } }"))
    plabels.update({"http://www.w3.org/2000/01/rdf-schema#label": "이름",
                    "http://www.w3.org/2004/02/skos/core#prefLabel": "대표 이름",
                    "http://www.w3.org/2004/02/skos/core#altLabel": "다른 이름",
                    "http://www.w3.org/2004/02/skos/core#broader": "상위 개념",
                    "http://www.w3.org/2004/02/skos/core#narrower": "하위 개념",
                    "http://www.w3.org/2004/02/skos/core#broaderTransitive": "상위 개념(전이, 추론)",
                    "http://www.w3.org/2004/02/skos/core#inScheme": "개념체계",
                    "http://www.w3.org/2004/02/skos/core#definition": "정의",
                    "http://purl.org/dc/terms/identifier": "식별자",
                    "http://www.w3.org/2000/01/rdf-schema#comment": "설명"})
    skip = {"http://www.w3.org/1999/02/22-rdf-syntax-ns#type"}

    def side(direction: str) -> list[dict]:
        pat = f"<{iri}> ?p ?X" if direction == "out" else f"?X ?p <{iri}>"
        counts = _rows(kg, f"SELECT ?p (COUNT(*) AS ?n) WHERE {{ {pat} }} GROUP BY ?p ORDER BY DESC(?n)")
        out = []
        for p, n in counts:
            if p in skip:
                continue
            q = f"""SELECT ?X ?LBL (SAMPLE(?g) AS ?gg) (isLiteral(?X) AS ?lit) WHERE {{ {pat.replace('?p', f'<{p}>')}
                    {LABEL} OPTIONAL {{ GRAPH ?g {{ {pat.replace('?p', f'<{p}>')} }} FILTER({INF}) }} }}
                    GROUP BY ?X ?LBL LIMIT {per_prop}"""
            items = []
            for x, lbl, g, lit in _rows(kg, q):
                is_lit = str(lit).lower() == "true"
                items.append({"value": x, "label": x if is_lit else (lbl or _short(x)),
                              "literal": is_lit, "inferred": bool(g),
                              "rule": g[len(INFERRED_PREFIX):] if g else None})
            out.append({"iri": p, "label": plabels.get(p, _short(p)), "count": int(n), "items": items})
        return out

    return {"iri": iri, "label": _label_of(kg, iri), "types": _types(kg, iri),
            "out": side("out"), "in": side("in"),
            "rules": {r.id: r.label for r in RULE_BY_ID.values()}}


def search(kg, q: str, limit: int = 40) -> list[dict]:
    """이름으로 개체 찾기(쟁점·기관·인물·의안·회의·안건·지원사업)."""
    q = q.strip().replace("\\", "").replace('"', "")
    if not q:
        return []
    # 가장 구체적인 클래스 하나만(의원 발의 의안이면 상위의 '의안'은 빼고), 같은 이름의 의안은 의안번호로 구분
    rows = _rows(kg, f"""SELECT ?s ?l (SAMPLE(?tl) AS ?type) (SAMPLE(?no) AS ?id) WHERE {{
        ?s rdfs:label ?l . FILTER(CONTAINS(LCASE(STR(?l)), LCASE("{q}")))
        ?s a ?t . ?t rdfs:label ?tl . FILTER(STRSTARTS(STR(?t), STR(ag:)) && ?t != ag:Utterance && ?t != ag:Answer)
        FILTER NOT EXISTS {{ ?s a ?t2 . ?t2 rdfs:subClassOf+ ?t . FILTER(?t2 != ?t) }}
        FILTER(!STRSTARTS(STR(?s), STR(ag:)))
        OPTIONAL {{ ?s ag:billNumber ?no }}
    }} GROUP BY ?s ?l ORDER BY STRLEN(STR(?l)) DESC(?id) LIMIT {int(limit)}""")
    return [{"iri": s, "label": l, "type": t, "note": f"의안번호 {n}" if n else ""} for s, l, t, n in rows]


def instances(kg, cls: str, limit: int = 80) -> dict:
    """클래스의 개체 목록(이름순 일부)과 전체 수. 추론으로 그 클래스가 된 개체도 포함."""
    if not cls.startswith(AG) or any(c in cls for c in '<>"{}| \\^`'):
        raise ValueError("잘못된 클래스")
    total = int(_rows(kg, f"SELECT (COUNT(DISTINCT ?X) AS ?n) WHERE {{ ?X a <{cls}> }}")[0][0])
    rows = _rows(kg, f"""SELECT ?X ?LBL (SAMPLE(?g) AS ?gg) WHERE {{ ?X a <{cls}> {LABEL}
                         OPTIONAL {{ GRAPH ?g {{ ?X a <{cls}> }} FILTER({INF}) }} }}
                         GROUP BY ?X ?LBL ORDER BY ?LBL LIMIT {int(limit)}""")
    return {"class": cls, "label": _label_of(kg, cls), "total": total,
            "items": [{"iri": x, "label": lbl or _short(x), "inferred": bool(g)} for x, lbl, g in rows]}
