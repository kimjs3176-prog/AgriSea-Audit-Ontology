"""지식그래프 추론기: 온톨로지 공리와 규칙으로 새 사실을 도출해 그래프에 더한다(전방 추론, 물질화).

규칙은 두 종류다.
- 스키마 규칙: 온톨로지(ontology/agrisea.ttl)의 공리를 그대로 따른다. 하위 클래스(rdfs:subClassOf),
  하위 관계(rdfs:subPropertyOf), 역관계(owl:inverseOf), 대칭 관계(owl:SymmetricProperty),
  관계 연쇄(owl:propertyChainAxiom), SKOS 상위 개념(skos:broaderTransitive).
- 도메인 규칙: 국감 업무에서 쓰는 관계를 SPARQL 규칙(SWRL과 같은 역할)으로 정의한다.
  예) 의원의 질의가 쟁점을 다루면 → 그 의원은 그 쟁점을 질의했다(ag:questionedOn).

추론한 사실은 기본 그래프에도 넣고(질의가 바로 보도록), 규칙마다 이름 있는 그래프
<https://w3id.org/agrisea/id/graph/inferred/규칙ID> 에도 넣는다. 그래서 어떤 사실이 원 데이터인지,
어느 규칙으로 추론됐는지 구분할 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass

INFERRED_PREFIX = "https://w3id.org/agrisea/id/graph/inferred/"
PREFIXES = """
PREFIX ag: <https://w3id.org/agrisea/ontology#>
PREFIX agd: <https://w3id.org/agrisea/id/>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
"""
RELATED_MIN = 30  # 두 쟁점을 함께 다룬 발언이 이만큼 이상이면 '함께 논의된 쟁점'


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str        # schema | domain
    label: str       # 화면에 보일 이름
    desc: str        # 무엇을 추론하는지
    head: str        # 새로 생길 트리플 패턴
    body: str        # 조건

    def graph(self) -> str:
        return INFERRED_PREFIX + self.id

    def update(self) -> str:
        return (f"{PREFIXES}\nINSERT {{ {self.head} GRAPH <{self.graph()}> {{ {self.head} }} }}\n"
                f"WHERE {{ {self.body} FILTER NOT EXISTS {{ {self.head} }} }}")


RULES: list[Rule] = [
    Rule("subclass", "schema", "하위 클래스", "약속은 답변이고 답변은 발언이다처럼, 하위 클래스의 개체는 상위 클래스의 개체이기도 하다",
         "?x a ?super .",
         "?x a ?c . ?c rdfs:subClassOf+ ?super . FILTER(isIRI(?super) && ?super != owl:Thing)"),
    Rule("subproperty", "schema", "하위 관계", "하위 관계가 성립하면 상위 관계도 성립한다(예: 질의 대상 기관 → 언급 기관)",
         "?s ?sup ?o .", "?p rdfs:subPropertyOf+ ?sup . ?s ?p ?o ."),
    Rule("inverse", "schema", "역관계", "A가 B에 대해 성립하면 역관계는 B가 A에 대해 성립한다(예: 발언자 ↔ 한 발언)",
         "?o ?q ?s .", "{ ?p owl:inverseOf ?q } UNION { ?q owl:inverseOf ?p } ?s ?p ?o . FILTER(isIRI(?o))"),
    Rule("symmetric", "schema", "대칭 관계", "대칭 관계는 양방향으로 성립한다(예: 함께 논의된 쟁점)",
         "?o ?p ?s .", "?p a owl:SymmetricProperty . ?s ?p ?o ."),
    Rule("chain", "schema", "관계 연쇄", "두 관계를 이으면 새 관계가 된다(예: 회의의 안건 + 안건의 의안 → 회의가 심사한 의안)",
         "?x ?p ?z .",
         "?p owl:propertyChainAxiom ?l . ?l rdf:first ?p1 ; rdf:rest/rdf:first ?p2 . ?x ?p1 ?y . ?y ?p2 ?z ."),
    Rule("broader", "schema", "상위 쟁점", "쟁점 분류의 상위 개념을 모두 잇는다(skos:broaderTransitive)",
         "?a skos:broaderTransitive ?b .", "?a skos:broader+ ?b ."),
    Rule("topic", "domain", "상위 쟁점 주제", "하위 쟁점을 다룬 발언은 그 상위 쟁점 주제도 다룬다",
         "?u ag:concernsTopic ?t .", "?u ag:concernsIssue ?i . ?i skos:broader+ ?t ."),
    Rule("questionedOn", "domain", "의원이 질의한 쟁점", "의원의 질의가 쟁점을 다루면 그 의원은 그 쟁점을 질의했다",
         "?l ag:questionedOn ?i .",
         "?q a ag:Question ; ag:spokenBy ?l ; ag:concernsIssue ?i . ?l a ag:Legislator ."),
    Rule("questionedAgency", "domain", "의원이 질의한 기관", "의원의 질의가 기관을 향하면 그 의원은 그 기관에 질의했다",
         "?l ag:questionedAgency ?o .",
         "?q a ag:Question ; ag:spokenBy ?l ; ag:addressedTo ?o . ?l a ag:Legislator ."),
    Rule("respondedOn", "domain", "기관이 답변한 쟁점", "기관 소속 인물의 답변이 쟁점을 다루면 그 기관은 그 쟁점에 답변했다",
         "?o ag:respondedOn ?i .",
         "?a a ag:Answer ; ag:spokenBy ?p ; ag:concernsIssue ?i . ?p ag:affiliatedWith ?o ."),
    Rule("pledgedOn", "domain", "기관이 약속한 쟁점", "기관의 약속이 쟁점을 다루면 그 기관은 그 쟁점에 약속했다",
         "?o ag:pledgedOn ?i .", "?c a ag:Commitment ; ag:committedBy ?o ; ag:concernsIssue ?i ."),
    Rule("relatedIssue", "domain", "함께 논의된 쟁점",
         f"두 쟁점을 함께 다룬 발언이 {RELATED_MIN}건 이상이면 두 쟁점은 함께 논의된 쟁점이다",
         "?a ag:relatedIssue ?b .",
         "{ SELECT ?a ?b WHERE { ?u a ag:Utterance ; ag:concernsIssue ?a , ?b . FILTER(STR(?a) < STR(?b)) }"
         f" GROUP BY ?a ?b HAVING (COUNT(DISTINCT ?u) >= {RELATED_MIN}) }}"),
    Rule("supportProgram", "domain", "쟁점의 지원사업", "지원사업 공고가 쟁점을 다루면 그 쟁점의 지원사업이다",
         "?i ag:hasSupportProgram ?p .", "?p a ag:SupportProgram ; ag:concernsIssue ?i ."),
    Rule("openCommitment", "domain", "국감 확인 대상 약속",
         "이행 판정이 '재지적' 또는 '근거 미확인'인 약속은 국감에서 이행 여부를 물어볼 약속이다(ag:OpenCommitment)",
         "?c a ag:OpenCommitment .",
         "?c a ag:Commitment ; ag:followUpStatus ?s . VALUES ?s { <https://w3id.org/agrisea/id/status/reraised> <https://w3id.org/agrisea/id/status/none> }"),
    Rule("fulfilledCommitment", "domain", "이행 보고된 약속", "이행 판정이 '이행 보고'인 약속(ag:FulfilledCommitment)",
         "?c a ag:FulfilledCommitment .", "?c a ag:Commitment ; ag:followUpStatus <https://w3id.org/agrisea/id/status/done> ."),
]
RULE_BY_ID = {r.id: r for r in RULES}


def materialize(kg, max_passes: int = 6) -> dict:
    """규칙을 더 나올 사실이 없을 때까지 반복 적용한다. 반환: {규칙ID: 추론한 트리플 수, ...}."""
    import pyoxigraph as ox
    for _ in range(max_passes):
        before = len(kg)
        for r in RULES:
            kg.update(r.update())
        if len(kg) == before:
            break
    counts = {}
    for r in RULES:
        g = ox.NamedNode(r.graph())
        counts[r.id] = sum(1 for _ in kg.quads_for_pattern(None, None, None, g))
    return counts


def is_inferred_graph(g: str | None) -> bool:
    return bool(g) and g.startswith(INFERRED_PREFIX)
