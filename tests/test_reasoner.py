"""온톨로지 v0.2와 추론기: 의안 추출, 공리·규칙 추론, 추론 표시, 조회(요약·개체·검색)."""
from fastapi.testclient import TestClient

from agrisea import followup, kgview, reasoner
from agrisea.ontology import build_kg, parse_bill, run_sparql
from agrisea.web.app import create_app

P = "PREFIX ag: <https://w3id.org/agrisea/ontology#> PREFIX agd: <https://w3id.org/agrisea/id/> "


def _kg(store):
    store.kv_set("followups", followup.track_all(store))  # 이행 판정도 그래프에 들어가게
    return build_kg(store)


def test_parse_bill():
    b = parse_bill("14. 농수산물 유통 및 가격안정에 관한 법률 일부개정법률안(어기구 의원 대표발의)(의안번호 2200569)")
    assert b == {"no": "2200569", "title": "농수산물 유통 및 가격안정에 관한 법률 일부개정법률안",
                 "kind": "member", "proposer": "어기구"}
    assert parse_bill("항만법 일부개정법률안(정부 제출)(의안번호 2202187)")["kind"] == "government"
    assert parse_bill("5. 농업·농촌 및 식품산업 기본법 일부개정법률안(대안)")["kind"] == "alternative"
    assert parse_bill("2026년도 국정감사계획서 채택의 건") is None


def test_schema_axioms_are_materialized(store):
    kg, counts = _kg(store)
    # 하위 클래스: 약속은 발언이기도 하다
    assert run_sparql(kg, P + "ASK { ?c a ag:Commitment . ?c a ag:Utterance }")["rows"] == [[True]]
    # 역관계: 발언자 → 한 발언
    assert run_sparql(kg, P + "ASK { ?p ag:speaks ?u . ?u ag:spokenBy ?p }")["rows"] == [[True]]
    # 하위 관계: 관련 쟁점 → 다룬 주제
    assert run_sparql(kg, P + "ASK { ?u ag:concernsIssue ?i ; ag:concernsTopic ?i }")["rows"] == [[True]]
    assert counts["inverse"] > 0 and counts["subclass"] > 0


def test_domain_rules(store):
    kg, counts = _kg(store)
    # 위원의 질의가 쟁점을 다루면 → 그 위원은 그 쟁점을 질의했다(역관계로 쟁점 쪽에서도 보임)
    assert run_sparql(kg, P + "ASK { ?l a ag:Legislator ; ag:questionedOn ?i . ?i ag:questionedBy ?l }")["rows"] == [[True]]
    # 하위 쟁점 발언은 상위 쟁점 주제도 다룬다
    res = run_sparql(kg, P + "SELECT ?t WHERE { ?u ag:concernsTopic ?t . ?t a ag:Issue . "
                     "FILTER NOT EXISTS { ?t <http://www.w3.org/2004/02/skos/core#broader> ?x } } LIMIT 1")
    assert res["rows"]
    assert counts["questionedOn"] > 0 and counts["topic"] > 0


def test_inferred_facts_are_marked_and_rules_are_entities(store):
    kg, counts = _kg(store)
    g = reasoner.RULE_BY_ID["questionedOn"].graph()
    n = run_sparql(kg, P + f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{g}> {{ ?s ?p ?o }} }}")["rows"][0][0]
    assert int(n) == counts["questionedOn"]
    # 원 데이터에는 이름 있는 그래프가 없다(추론만 규칙별 그래프에)
    rows = run_sparql(kg, P + "SELECT ?r ?n WHERE { ?r a ag:InferenceRule ; ag:inferredCount ?n }")["rows"]
    assert len(rows) == len(reasoner.RULES)
    # 다시 돌려도 더 생기지 않는다(고정점)
    assert sum(reasoner.materialize(kg).values()) == sum(counts.values())


def test_kgview(store):
    kg, _ = _kg(store)
    s = kgview.summary(kg)
    assert s["inferred"] > 0 and s["asserted"] > 0
    commit = next(c for c in s["classes"] if c["id"] == "Commitment")
    assert commit["count"] > 0
    assert any(p["id"] == "questionedOn" and p["inferred"] > 0 for p in s["properties"])
    assert {sc["id"] for sc in s["schemes"]} == {"IssueScheme", "FollowUpStatusScheme", "ActionTypeScheme"}
    hit = kgview.search(kg, "농림축산식품부")[0]
    d = kgview.describe(kg, hit["iri"])
    assert d["label"] == "농림축산식품부"
    assert any(it["inferred"] for g in d["out"] + d["in"] for it in g["items"])  # 규칙으로 이어 낸 관계도 보인다


def test_ontology_endpoints(store, settings):
    store.kv_set("followups", followup.track_all(store))
    c = TestClient(create_app(settings, store))
    s = c.get("/api/ontology/summary").json()
    assert s["rules"] and s["classes"]
    hits = c.get("/api/ontology/search", params={"q": "쌀"}).json()
    assert hits
    d = c.get("/api/ontology/entity", params={"iri": hits[0]["iri"]}).json()
    assert d["iri"] == hits[0]["iri"] and d["types"]
    assert c.get("/api/ontology/entity", params={"iri": "https://x/<bad>"}).status_code == 400
