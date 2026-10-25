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


def test_org_briefing_is_cached(store, settings):
    from agrisea.precompute import briefing_key
    c = TestClient(create_app(settings, store))
    b = c.get("/api/briefing", params={"org": "해양수산부"}).json()
    assert b["counts"]["utterances"] and store.kv_get(briefing_key("해양수산부")) == b
    assert c.get("/api/briefing", params={"org": "해양수산부"}).json() == b
    # 다른 조건이 붙거나 사전에 없는 기관은 저장하지 않고 그때그때 계산
    c.get("/api/briefing", params={"org": "해양수산부", "session": 430})
    c.get("/api/briefing", params={"org": "없는기관"})
    keys = {k for (k,) in store.conn.execute("SELECT key FROM kv WHERE key LIKE 'briefing:%'")}
    assert keys == {briefing_key("해양수산부")}
