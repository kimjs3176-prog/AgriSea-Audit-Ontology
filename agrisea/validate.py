"""온톨로지 검증: 지식그래프가 스키마(ontology/agrisea.ttl)의 개념 정의를 지키는지 점검한다.

OWL은 열린 세계 가정이라 정의역·치역이 어긋나도 '모순'이 아니라 '새 타입 추론'이 된다
(예: 안건이 ag:mentionsOrganization 의 주어면 안건이 발언으로 추론됨). 그래서 여기서는 SHACL처럼
닫힌 세계로 보고, 데이터가 개념 정의를 벗어난 곳을 위반으로 센다.

- 오류(error): 공리와 어긋남. 서로 배타인 클래스에 함께 속함, 함수 속성 값 여럿, 정의역·치역·자료형 위반,
  스키마에 없는 용어, 관계 의미 모순(답변이 질의보다 앞섬 등), SKOS 순환·이름 중복.
- 경고(warn): 개념상 있어야 할 관계가 빠짐(약속의 약속 주체 기관 등). 원문에 정보가 없을 수 있다.
- 참고(info): 같은 이름 인물처럼 사람이 확인할 만한 것.
"""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import combinations

from .reasoner import PREFIXES

AG = "https://w3id.org/agrisea/ontology#"
EXAMPLES = 3
XSD_WORD = {"date": "날짜", "integer": "정수", "anyURI": "인터넷 주소", "boolean": "참·거짓", "string": "글자"}


def _rows(kg, q: str) -> list[list]:
    return [[getattr(x, "value", None) for x in r] for r in kg.query(PREFIXES + q)]


def _num(kg, q: str) -> int:
    return int(_rows(kg, q)[0][0])


def _short(iri: str | None) -> str:
    return (iri or "").replace(AG, "ag:")


class Report:
    def __init__(self, kg):
        self.kg = kg
        self.labels = dict(_rows(kg, f"SELECT ?x ?l WHERE {{ ?x rdfs:label ?l FILTER(STRSTARTS(STR(?x), '{AG}')) }}"))
        self.checks: list[dict] = []

    def name(self, iri: str) -> str:
        return self.labels.get(iri, _short(iri))

    def add(self, group: str, title: str, severity: str, count: int, total: int | None = None,
            where: str | None = None, desc: str = ""):
        """where: ?x 를 묶는 SPARQL 패턴(예시 개체를 뽑을 때 씀)."""
        ex = []
        if count and where:
            ex = [{"iri": x, "label": (l or x)[:80]} for x, l in _rows(
                self.kg, f"SELECT DISTINCT ?x (SAMPLE(?l0) AS ?l) WHERE {{ {where} OPTIONAL {{ ?x rdfs:label|ag:text ?l0 }} }}"
                         f" GROUP BY ?x LIMIT {EXAMPLES}")]
        self.checks.append({"group": group, "title": title, "desc": desc, "severity": severity if count else "pass",
                            "level": severity, "count": count, "total": total, "examples": ex})


def _classes_of(kg, node: str) -> list[str]:
    """정의역·치역 값 → 클래스 목록(owl:unionOf 면 그 구성원)."""
    if node.startswith(AG):
        return [node]
    return [r[0] for r in _rows(kg, f"SELECT ?c WHERE {{ <{node}> owl:unionOf/rdf:rest*/rdf:first ?c }}")]


def _in_any(var: str, classes: list[str]) -> str:
    return " UNION ".join(f"{{ {var} a <{c}> }}" for c in classes)


def check_schema(r: Report):
    kg = r.kg
    decl = {x for x, in _rows(kg, f"""SELECT DISTINCT ?x WHERE {{ ?x a ?t
        VALUES ?t {{ owl:Class owl:ObjectProperty owl:DatatypeProperty skos:ConceptScheme }} FILTER(STRSTARTS(STR(?x), '{AG}')) }}""")}
    used = {x for x, in _rows(kg, f"""SELECT DISTINCT ?x WHERE {{ {{ ?s ?x ?o }} UNION {{ ?s a ?x }}
        FILTER(STRSTARTS(STR(?x), '{AG}')) }}""")}
    undeclared = sorted(used - decl)
    r.checks.append({"group": "용어", "title": "정의되지 않은 용어를 데이터가 씀", "level": "error",
                     "severity": "error" if undeclared else "pass", "count": len(undeclared), "total": None, "desc": "",
                     "examples": [{"iri": x, "label": _short(x)} for x in undeclared[:EXAMPLES]]})
    r.add("용어", "이름이 없는 분류·관계", "warn",
          _num(kg, f"""SELECT (COUNT(*) AS ?n) WHERE {{ ?x a ?t VALUES ?t {{ owl:Class owl:ObjectProperty owl:DatatypeProperty }}
              FILTER(STRSTARTS(STR(?x), '{AG}')) FILTER NOT EXISTS {{ ?x rdfs:label ?l }} }}"""),
          where=f"?x a owl:Class FILTER(STRSTARTS(STR(?x), '{AG}')) FILTER NOT EXISTS {{ ?x rdfs:label ?l }}")


def check_axioms(r: Report):
    kg = r.kg
    # 서로 배타인 클래스
    pairs = set()
    for lst, in _rows(kg, "SELECT ?l WHERE { ?d a owl:AllDisjointClasses ; owl:members ?l }"):
        members = [c for c, in _rows(kg, f"SELECT ?c WHERE {{ <{lst}> rdf:rest*/rdf:first ?c }}")]
        pairs |= {tuple(sorted(p)) for p in combinations(members, 2)}
    pairs |= {tuple(sorted(p)) for p in _rows(kg, "SELECT ?a ?b WHERE { ?a owl:disjointWith ?b }")}
    total = 0
    bad = []
    for a, b in sorted(pairs):
        n = _num(kg, f"SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ ?x a <{a}>, <{b}> }}")
        if n:
            bad.append((a, b, n))
            total += n
    r.add("분류", f"함께 속할 수 없는 두 분류에 동시에 속한 항목({len(pairs)}쌍 점검)", "error", total,
          where=f"?x a <{bad[0][0]}>, <{bad[0][1]}>" if bad else None,
          desc=", ".join(f"{r.name(a)}∩{r.name(b)} {n}" for a, b, n in bad))
    # 함수 속성
    for p, in _rows(kg, "SELECT ?p WHERE { ?p a owl:FunctionalProperty }"):
        n = _num(kg, f"SELECT (COUNT(*) AS ?n) WHERE {{ SELECT ?x WHERE {{ ?x <{p}> ?o }} GROUP BY ?x HAVING(COUNT(DISTINCT ?o) > 1) }}")
        r.add("분류", f"값이 하나여야 하는 관계 '{r.name(p)}'에 값이 여럿", "error", n,
              where=f"{{ SELECT ?x WHERE {{ ?x <{p}> ?o }} GROUP BY ?x HAVING(COUNT(DISTINCT ?o) > 1) }}")
    # 정의역·치역
    for p, d in _rows(kg, f"SELECT ?p ?d WHERE {{ ?p rdfs:domain ?d FILTER(STRSTARTS(STR(?p), '{AG}')) }}"):
        cs = _classes_of(kg, d)
        if not cs:
            continue
        where = f"?x <{p}> ?o FILTER NOT EXISTS {{ {_in_any('?x', cs)} }}"
        n = _num(kg, f"SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ {where} }}")
        r.add("관계 대상", f"'{r.name(p)}'의 주어가 {' 또는 '.join(map(r.name, cs))}이(가) 아님", "error", n,
              _num(kg, f"SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ ?x <{p}> ?o }}"), where)
    for p, rg in _rows(kg, f"SELECT ?p ?r WHERE {{ ?p a owl:ObjectProperty ; rdfs:range ?r FILTER(STRSTARTS(STR(?p), '{AG}')) }}"):
        cs = _classes_of(kg, rg)
        if not cs:
            continue
        where = f"?s <{p}> ?x FILTER NOT EXISTS {{ {_in_any('?x', cs)} }}"
        n = _num(kg, f"SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ {where} }}")
        r.add("관계 대상", f"'{r.name(p)}'의 대상이 {' 또는 '.join(map(r.name, cs))}이(가) 아님", "error", n,
              _num(kg, f"SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ ?s <{p}> ?x }}"), where)
    for p, dt in _rows(kg, f"SELECT ?p ?r WHERE {{ ?p a owl:DatatypeProperty ; rdfs:range ?r FILTER(STRSTARTS(STR(?p), '{AG}')) }}"):
        where = f"?x <{p}> ?o FILTER(!isLiteral(?o) || DATATYPE(?o) != <{dt}>)"
        n = _num(kg, f"SELECT (COUNT(*) AS ?n) WHERE {{ {where} }}")
        kind = XSD_WORD.get(dt.rsplit("#", 1)[-1], dt.rsplit("#", 1)[-1])
        r.add("관계 대상", f"'{r.name(p)}' 값이 {kind} 형식이 아님",
              "error", n, None, where)


# 개념상 있어야 할 관계: (클래스, 관계, 설명)
REQUIRED = [
    ("Utterance", "ag:spokenBy", "발언에는 발언자가 있다"),
    ("Utterance", "ag:inMeeting", "발언은 어느 회의의 발언이다"),
    ("Utterance", "ag:text", "발언에는 내용이 있다"),
    ("Meeting", "ag:meetingDate", "회의에는 회의일자가 있다"),
    ("Meeting", "ag:inSession", "회의는 어느 회기에 열린다"),
    ("Answer", "ag:respondsTo", "답변은 어느 질의에 대한 답변이다"),
    ("Commitment", "ag:committedBy", "약속에는 약속한 기관이 있다"),
    ("Commitment", "ag:followUpStatus", "약속에는 이행 판정이 하나 있다"),
    ("Question", "ag:addressedTo", "질의는 어느 기관을 향한다"),
    ("AuditMeeting", "ag:audited", "국정감사 회의에는 감사 대상 기관이 있다"),
    ("Bill", "rdfs:label", "의안에는 이름이 있다"),
    ("MemberBill", "ag:proposedBy", "의원 발의 의안에는 대표발의 의원이 있다"),
    ("GovernmentOfficial", "ag:affiliatedWith", "정부·기관 관계자는 어느 기관 소속이다"),
    ("Issue", "skos:inScheme", "쟁점은 쟁점 분류체계에 속한다"),
    ("FollowUpEvidence", "ag:evidenceIn", "이행 근거는 어느 회의에서 나왔다"),
]


def check_required(r: Report):
    for cls, p, desc in REQUIRED:
        total = _num(r.kg, f"SELECT (COUNT(*) AS ?n) WHERE {{ ?x a ag:{cls} }}")
        where = f"?x a ag:{cls} FILTER NOT EXISTS {{ ?x {p} ?v }}"
        r.add("필수 관계", desc, "warn", _num(r.kg, f"SELECT (COUNT(*) AS ?n) WHERE {{ {where} }}"), total, where)


# 관계의 뜻끼리 모순: (설명, ?x 를 묶는 패턴)
SEMANTIC = [
    ("답변이 다른 회의의 질의에 답함", "?x ag:respondsTo ?q ; ag:inMeeting ?m1 . ?q ag:inMeeting ?m2 FILTER(?m1 != ?m2)"),
    ("답변이 질의보다 먼저 나옴", "?x ag:respondsTo ?q ; ag:orderIndex ?i . ?q ag:orderIndex ?j FILTER(?i <= ?j)"),
    ("질의한 사람이 국회의원이 아님", "?x a ag:Question ; ag:spokenBy ?p FILTER NOT EXISTS { ?p a ag:Legislator }"),
    ("국회의원이 답변함", "?x a ag:Answer ; ag:spokenBy ?p . ?p a ag:Legislator"),
    ("약속을 이끈 사람이 국회의원이 아님", "?x ag:raisedBy ?p FILTER NOT EXISTS { ?p a ag:Legislator }"),
    ("이행 근거가 약속보다 앞선 날짜", "?x ag:hasEvidence ?e ; ag:inMeeting ?m . ?m ag:meetingDate ?d . ?e ag:evidenceDate ?ed FILTER(?ed < ?d)"),
    ("이행 근거가 약속한 회의 자체", "?x ag:hasEvidence ?e ; ag:inMeeting ?m . ?e ag:evidenceIn ?m"),
    ("쟁점이 자기 자신과 함께 논의됨", "?x ag:relatedIssue ?x"),
]


def check_semantic(r: Report):
    for title, where in SEMANTIC:
        r.add("관계 의미", title, "error", _num(r.kg, f"SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ {where} }}"), None, where)


def check_skos(r: Report):
    kg = r.kg
    r.add("분류체계", "상위 분류가 돌고 돎", "error",
          _num(kg, "SELECT (COUNT(*) AS ?n) WHERE { ?x skos:broader+ ?x }"), None, "?x skos:broader+ ?x")
    r.add("분류체계", "한 분류에 대표 이름이 둘 이상", "error",
          _num(kg, "SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE { ?x skos:prefLabel ?a, ?b FILTER(?a != ?b && LANG(?a) = LANG(?b)) }"),
          None, "?x skos:prefLabel ?a, ?b FILTER(?a != ?b && LANG(?a) = LANG(?b))")
    r.add("분류체계", "같은 체계 안에서 대표 이름이 겹침", "error",
          _num(kg, """SELECT (COUNT(*) AS ?n) WHERE { SELECT ?s ?l WHERE { ?c skos:prefLabel ?l ; skos:inScheme ?s }
                      GROUP BY ?s ?l HAVING(COUNT(?c) > 1) }"""))
    where = "?x skos:inScheme ?s FILTER NOT EXISTS { ?x skos:topConceptOf ?s } FILTER NOT EXISTS { ?s skos:hasTopConcept ?x } FILTER NOT EXISTS { ?x skos:broader ?b }"
    r.add("분류체계", "맨 위 분류인데 체계의 맨 위로 등록되지 않음", "warn",
          _num(kg, f"SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ {where} }}"), None, where)


def check_identity(r: Report):
    kg = r.kg
    where = """?x a ag:Person ; rdfs:label ?l . ?y a ag:Person ; rdfs:label ?l FILTER(?x != ?y)"""
    r.add("동일 항목", "같은 이름인데 서로 다른 인물(국회의원과 정부 관계자 동명이인 또는 같은 사람)", "info",
          _num(kg, f"SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ {where} }}"), None, where)
    where = "?x rdfs:label ?a, ?b FILTER(?a != ?b && LANG(?a) = LANG(?b)) FILTER NOT EXISTS { ?x a owl:Class }"
    r.add("동일 항목", "한 항목에 이름이 둘 이상", "warn",
          _num(kg, f"SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ {where} }}"), None, where)


def validate(kg) -> dict:
    r = Report(kg)
    for step in (check_schema, check_axioms, check_required, check_semantic, check_skos, check_identity):
        step(r)
    counts = {k: sum(1 for c in r.checks if c["severity"] == k) for k in ("error", "warn", "info", "pass")}
    return {"as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "counts": counts, "checks": r.checks}


def errors(report: dict) -> list[dict]:
    return [c for c in report["checks"] if c["severity"] == "error"]
