from fastapi.testclient import TestClient

from agrisea.web.app import create_app


def test_web_endpoints(store, settings):
    c = TestClient(create_app(settings, store))
    assert c.get("/").status_code == 200
    assert c.get("/api/stats").json()["meetings"] == 4
    assert c.get("/api/search", params={"q": "오염수"}).json()["count"] >= 1
    s = c.get("/api/meetings/SAMPLE-2025-1014/summary").json()
    assert s["overview"] and s["llm"] is None
    assert c.get("/api/meetings/nope/summary").status_code == 404
    assert c.get("/api/briefing", params={"org": "해양수산부", "format": "md"}).text.startswith("# 국정감사")
    assert c.get("/api/graph").json()["nodes"]
    r = c.post("/api/sparql", json={"query": "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"})
    assert int(r.json()["rows"][0][0]) > 100
    bad = c.post("/api/sparql", json={"query": "SELECT * WHERE { SERVICE <http://x> { ?s ?p ?o } }"})
    assert bad.status_code == 400


def test_sessions_overview_and_issue_detail(store, settings):
    c = TestClient(create_app(settings, store))
    sessions = c.get("/api/sessions").json()
    assert sessions and sessions[0]["session_no"] == 430 and sessions[0]["kind"] == "임시회"
    assert c.get("/api/meetings", params={"session": 430}).json()
    assert c.get("/api/meetings", params={"session": 999}).json() == []
    assert c.get("/api/search", params={"q": "쌀값", "session": 430}).json()["count"] >= 1
    assert c.get("/api/search", params={"q": "쌀값", "session": 999}).json()["count"] == 0
    trend = c.get("/api/issue-trend").json()
    assert trend["sessions"] == [430] and trend["issues"]
    ov = c.get("/api/overview").json()
    assert ov["meetings"] and ov["commitments"]
    d = c.get("/api/issues/rice").json()
    assert d["label"] == "쌀·식량안보" and d["count"] > 0 and d["questions"]
    assert c.get("/api/issues/sea").json()["children"]
    assert c.get("/api/issues/nope").status_code == 404
    b = c.get("/api/briefing", params={"org": "해양수산부", "session": 430}).json()
    assert b["filters"]["session"] == 430 and b["counts"]["meetings"] == 1


def test_issue_dialogue_pairs_question_and_answer(store, settings):
    d = TestClient(create_app(settings, store)).get("/api/issues/rice").json()
    assert d["dialogue"]
    t = d["dialogue"][0]
    assert t["turns"][0]["side"] == "q" and t["turns"][0]["speaker"].endswith("위원")
    assert any(x["side"] == "a" for x in t["turns"])


def test_meeting_export_markdown(store, settings):
    c = TestClient(create_app(settings, store))
    r = c.get("/api/meetings/SAMPLE-2025-1014/export.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "## 회의록 전문" in r.text and "**" in r.text
    d = c.get("/api/meetings/SAMPLE-2025-1014/export.md", params={"download": 1})
    assert "attachment" in d.headers["content-disposition"]
    assert c.get("/api/meetings/nope/export.md").status_code == 404


def test_index_has_flow_graph_and_transcript_tools(store, settings):
    """대시보드 관계 흐름도(기본)·3D 전환, 회의록 발언자 거르기·안건 이동·발언 흐름 미니맵."""
    html = TestClient(create_app(settings, store)).get("/").text
    for marker in ('id="netFlow"', 'id="netMode"', 'id="tSpeaker"', 'id="tAgenda"', 'id="tFlow"',
                   "function flowRender", "function jumpToUtt"):
        assert marker in html, marker


def test_index_has_no_governance_display(store, settings):
    """운영 관문(위험평가·경로배정·결정 원장·평가 데이터·정책)은 화면에 표시하지 않고 API로만 제공한다."""
    html = TestClient(create_app(settings, store)).get("/").text
    for marker in ('data-tab="gov"', "govFlow", "govChip", "govchip", "fugov", "/api/governance"):
        assert marker not in html, marker


def test_index_has_no_support_program_sections(store, settings):
    """대시보드·쟁점 상세·국감 브리핑·데이터 현황에서 지원사업 공고 섹션을 뺐다."""
    html = TestClient(create_app(settings, store)).get("/").text
    for marker in ('id="progDash"', "function progList", "function progCard", "/api/programs/overview",
                   '"pg", `지원사업 공고', '["지원사업 공고"'):
        assert marker not in html, marker


def test_index_has_no_ontology_wording(store, settings):
    """화면 문구에는 '온톨로지' 대신 '지식그래프'를 쓴다. 공유 주소는 #/kg(예전 #/ontology 도 열림)."""
    html = TestClient(create_app(settings, store)).get("/").text
    assert "온톨로지" not in html
    assert '<button data-tab="ontology">지식그래프</button>' in html
    assert 'if(tab === "kg") tab = "ontology"' in html and 'setHash("kg/"' in html


def test_index_handlers_and_tooltips_do_not_run_data_as_code(store, settings):
    """회의록·API·주소창 해시에서 온 값을 onclick JS 문자열에 넣을 때 esc만 쓰면 &#39; 가 속성에서 ' 로 풀려
    문자열을 벗어난다(#/kg/…issue/x'+alert(1)+' 같은 링크). 툴팁도 data-tip을 그대로 innerHTML에 넣으면 안 된다."""
    import re
    html = TestClient(create_app(settings, store)).get("/").text
    assert "const jsq = s => esc(JSON.stringify(" in html
    assert not re.search(r"'\$\{esc\(", html)                      # onclick="fn('${esc(x)}')" 형태가 남지 않게
    assert "decodeURIComponent('${encodeURIComponent(" not in html  # encodeURIComponent 는 ' 를 그대로 둔다
    assert "tip.innerHTML = t.dataset.tip" not in html
    assert 'tip.innerHTML = esc(t.dataset.tip).replace(/&lt;(\\/?b|br)&gt;/g, "<$1>")' in html
    assert 'const dot = d => esc(d || "")' in html
    assert "${safeUrl(m.pdf_url) ?" in html


def test_admin_collect_params_cannot_override_api_key_or_paging(store, settings, monkeypatch):
    """직접 수집의 params 는 국회 API 거르기 인자만: 인증키·쪽 번호·파이썬 인자를 바꿔 쓰지 못한다."""
    import agrisea.pipeline as pipeline
    seen = {}

    def fake_collect(store, settings, params, progress=print, **kw):
        seen.update(params)
        return []

    monkeypatch.setattr(pipeline, "collect", fake_collect)
    c = TestClient(create_app(settings, store))
    r = c.post("/api/admin/collect", json={"date": "2026-09-01", "params": {
        "KEY": "attacker", "Type": "xml", "pIndex": "9", "pSize": "100000", "p_size": "1", "CMIT_NM": "농해수위"}})
    assert r.status_code == 200
    assert seen == {"DAE_NUM": "22", "CMIT_NM": "농해수위", "CONF_DATE": "2026-09-01"}
    assert c.post("/api/admin/collect", json={"date": "2026-09-01", "params": "KEY=x"}).status_code == 400


def test_security_headers_and_param_bounds(store, settings):
    c = TestClient(create_app(settings, store))
    assert c.get("/").headers["x-content-type-options"] == "nosniff"
    assert c.get("/api/stats").headers["x-content-type-options"] == "nosniff"
    # 중계는 json·xml 형식만(공공데이터포털 오류 페이지가 임의 값을 되비추지 않게)
    assert c.get("/api/kstartup", params={"returnType": "<script>"}).status_code == 422
    # 음수 개수는 SPARQL 문법 오류(500) 대신 입력 오류
    assert c.get("/api/ontology/instances", params={"cls": "https://w3id.org/agrisea/ontology#Meeting",
                                                    "limit": -1}).status_code == 422
