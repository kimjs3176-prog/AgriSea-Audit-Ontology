import pyoxigraph as ox

from agrisea import ontology, validate
from agrisea.ontology import parse_bill, person_class, person_uri

AG = "https://w3id.org/agrisea/ontology#"


def _kg(store):
    kg, _ = ontology.build_kg(store)
    return kg


def test_sample_graph_has_no_errors(store):
    rep = validate.validate(_kg(store))
    assert validate.errors(rep) == [], [c["title"] for c in validate.errors(rep)]
    assert rep["counts"]["pass"] > 20


def test_detects_disjoint_and_domain_violations(store):
    kg = _kg(store)
    org = next(iter(kg.quads_for_pattern(None, ox.NamedNode(AG + "affiliatedWith"), None))).object
    kg.add(ox.Quad(org, ox.NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), ox.NamedNode(AG + "Person")))
    bad = {c["title"] for c in validate.errors(validate.validate(kg))}
    assert any("서로 배타" in t for t in bad)


def test_detects_functional_violation(store):
    kg = _kg(store)
    c = next(iter(kg.quads_for_pattern(None, ox.NamedNode(AG + "followUpStatus"), None)), None)
    if c is None:
        return
    kg.add(ox.Quad(c.subject, c.predicate, ox.NamedNode("https://w3id.org/agrisea/id/status/done")))
    kg.add(ox.Quad(c.subject, c.predicate, ox.NamedNode("https://w3id.org/agrisea/id/status/none")))
    assert any("값이 여럿" in x["title"] for x in validate.errors(validate.validate(kg)))


def test_every_commitment_has_one_status(store):
    from agrisea import precompute
    precompute.precompute(store)
    kg = _kg(store)
    rep = validate.validate(kg)
    row = next(c for c in rep["checks"] if c["title"] == "약속에는 이행 판정이 하나 있다")
    assert row["count"] == 0


def test_bill_parsing_variants():
    b = parse_bill("78. 쌀값 정상화 촉구 결의안(문금주 의원 등 21인 발의)(의안번호 2205744)")
    assert b["kind"] == "member" and b["proposer"] == "문금주" and b["title"] == "쌀값 정상화 촉구 결의안"
    assert parse_bill("3. 녹색해운항로 구축 지원 특별법안`#``#`에 대한 입법공청회(의안번호 2204480)")["title"] == "녹색해운항로 구축 지원 특별법안"
    assert parse_bill("- 수산기자재산업 육성에 관한 법률안(의안번호 2200146)")["title"] == "수산기자재산업 육성에 관한 법률안"


def test_person_identity():
    # 위원장 직무대리는 의원, 장관 직무대행은 정부 관계자, 정부 관계자·증인은 한 개체
    assert person_uri("이원택", "other", "위원장 직무대리") == person_uri("이원택", "member")
    assert person_class("other", "해양수산부장관 직무대행").endswith("GovernmentOfficial")
    assert person_uri("송미령", "official") == person_uri("송미령", "witness")
    assert person_uri("송미령", "official") != person_uri("송미령", "member")


def test_audit_meeting_needs_audit_agenda():
    assert ontology.meeting_class({"title": "제5차 위원회", "agendas": ["1. 2024년도 국정감사계획서 채택의 건"]}) != ontology.AG.AuditMeeting
    assert ontology.meeting_class({"title": "x", "agendas": ["1. 농림축산식품부에 대한 국정감사"]}) == ontology.AG.AuditMeeting
