from rdflib import RDF

from agrisea.ontology import AG, PRESET_QUERIES, build_graph, kg_from_store, run_sparql


def test_graph_contains_core_classes(store):
    g = build_graph(store)
    assert len(list(g.subjects(RDF.type, AG.AuditMeeting))) == 3
    assert len(list(g.subjects(RDF.type, AG.PlenaryCommitteeMeeting))) == 1
    assert len(list(g.subjects(RDF.type, AG.Commitment))) > 0
    assert len(list(g.subjects(AG.respondsTo, None))) > 0


def _with_program(store):
    """쟁점이 붙은 지원사업 공고 하나(공고 질의 예시가 결과를 내도록)."""
    from agrisea import programs
    a = {"id": "900", "title": "가축분뇨 퇴비화 지원사업 공고", "program": "", "org": "농식품부",
         "supervisor": "", "agency": "농림축산식품부", "category": "사업화", "target": "", "region": "전국",
         "start": "2099-01-01", "end": "2099-02-01", "url": "https://k-startup.go.kr/x", "content": "",
         "issues": {next(iter(programs.ISSUES)): 1}}
    store.save_programs([a])
    return store


def test_presets_run(store):
    g = kg_from_store(_with_program(store))
    for name, q in PRESET_QUERIES.items():
        res = run_sparql(g, q)
        assert res["rows"], name


def test_skos_hierarchy_query(store):
    g = kg_from_store(store)
    res = run_sparql(g, PRESET_QUERIES["하위 쟁점까지 포함한 '해양·수산' 발언"])
    labels = {r[0] for r in res["rows"]}
    assert "수산업·어촌" in labels


def test_ask_and_construct(store):
    kg = kg_from_store(store)
    assert run_sparql(kg, "ASK { ?s a <https://w3id.org/agrisea/ontology#Commitment> }")["rows"] == [[True]]
    res = run_sparql(kg, "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o } LIMIT 5")
    assert res["columns"] == ["s", "p", "o"] and len(res["rows"]) == 5


def test_core_graph_is_smaller(store):
    assert len(build_graph(store, core=True, text_limit=50)) < len(build_graph(store))
