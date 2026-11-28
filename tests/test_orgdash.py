from fastapi.testclient import TestClient

from agrisea import orgdash, precompute
from agrisea.web.app import create_app


def test_build_views_for_all_and_each_org(store):
    precompute.precompute(store)
    d = orgdash.build(store)
    assert "" in d["views"]
    total = d["views"][""]["counts"]["questions"]
    assert total > 0
    asked = [o for o in d["orgs"] if o["questions"]]
    assert asked, "질의를 받은 기관이 하나는 있어야 한다"
    # 기관별 질의는 전체 질의를 넘지 않고, 순위는 질의 수 내림차순
    assert all(o["questions"] <= total for o in d["orgs"])
    assert [o["questions"] for o in d["orgs"]] == sorted((o["questions"] for o in d["orgs"]), reverse=True)
    v = d["views"][asked[0]["name"]]
    assert v["counts"]["questions"] == asked[0]["questions"]
    assert sum(r["count"] for r in v["by_session"]) == v["counts"]["questions"]
    assert all(q["member"] and q["answer"] for q in v["recent_qa"])
    assert set(v["followups"]["counts"]) == set(v["followups"]["labels"])


def test_question_target_prefers_answering_org():
    pair = {"question": {"orgs": {"해양수산부": 1}}, "answers": [{"org": "농림축산식품부"}]}
    assert orgdash.question_targets(pair) == {"농림축산식품부"}
    pair = {"question": {"orgs": {"해양수산부": 1}}, "answers": [{"org": ""}]}
    assert orgdash.question_targets(pair) == {"해양수산부"}


def test_org_dashboard_endpoint(store, settings):
    c = TestClient(create_app(settings, store))
    r = c.get("/api/org-dashboard")
    assert r.status_code == 200 and r.json()["view"]["org"] == ""
    name = r.json()["orgs"][0]["name"]
    assert c.get("/api/org-dashboard", params={"org": name}).json()["view"]["org"] == name
    assert c.get("/api/org-dashboard", params={"org": "없는 기관"}).status_code == 404
