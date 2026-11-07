from datetime import date

from fastapi.testclient import TestClient

from agrisea.coverage import data_status
from agrisea.web.app import create_app


def test_data_status_range_months_and_gaps(store):
    d = data_status(store, meta={"exported_at": "2025-11-06T00:00:00+00:00", "kg_triples": 10},
                    today=date(2026, 1, 10))
    r = d["range"]
    assert r["meetings"] == 4 and r["parsed"] == 4 and r["samples"] == 4
    assert r["audit"] == 3   # 예시 데이터는 국정감사 회의 3건을 흉내 낸다
    assert r["plenary"] + r["subcommittee"] == r["meetings"]
    # 첫 회의 달부터 오늘이 속한 달까지 빠짐없이(회의 없는 달은 0)
    assert [m["month"] for m in d["months"]] == ["2025-10", "2025-11", "2025-12", "2026-01"]
    assert [m["meetings"] for m in d["months"]] == [3, 1, 0, 0]
    assert d["freshness"]["latest_meeting"] == "2025-11-05" and d["freshness"]["days_since"] == 66
    assert d["freshness"]["data_updated"] == "2025-11-06T00:00:00+00:00"
    titles = " / ".join(g["title"] for g in d["gaps"])
    assert "가상 예시" in titles and "66일간 새 회의록 없음" in titles and "국정감사" not in titles
    # 마지막 회의 뒤의 빈 달은 '수록되지 않은 달'로 세지 않는다(아직 열리지 않았을 수 있음)
    assert "수록되지 않은 달" not in titles and d["pending"] == []


def test_data_status_reports_pending_and_empty_months(store):
    store.upsert_meeting({"meeting_id": "X1", "title": "제431회 제1차 농림축산식품해양수산위원회",
                          "date": "2026-02-03", "session_no": 431})
    d = data_status(store, today=date(2026, 2, 10))
    assert d["range"]["parsed"] == 4 and d["range"]["meetings"] == 5
    assert [p["id"] for p in d["pending"]] == ["X1"]
    gaps = {g["title"]: g for g in d["gaps"]}
    assert "본문을 아직 받지 못한 회의 1건" in gaps
    empty = next(g for t, g in gaps.items() if t.startswith("회의가 수록되지 않은 달"))
    assert "2025.12" in empty["detail"] and "2026.01" in empty["detail"]
    assert not any("새 회의록 없음" in t for t in gaps)   # 최신 회의가 7일 전


def test_data_status_flags_missing_audit_minutes():
    from agrisea.store import Store
    s = Store(":memory:")
    s.upsert_meeting({"meeting_id": "A", "title": "제22대 제439회 제1차 농림축산식품해양수산위원회",
                      "date": "2026-09-02", "class_name": "상임위원회", "session_no": 439})
    d = data_status(s, today=date(2026, 9, 3))
    assert d["range"]["audit"] == 0 and d["quality"] is None
    assert any(g["level"] == "warn" and "국정감사" in g["title"] for g in d["gaps"])
    assert not any("지원사업" in g["title"] for g in d["gaps"])   # 화면에서 지원사업 섹션을 뺐다


def test_data_status_endpoint_and_dashboard_card(store, settings):
    c = TestClient(create_app(settings, store))
    d = c.get("/api/data-status").json()
    assert d["range"]["meetings"] == 4 and d["months"] and isinstance(d["gaps"], list)
    html = c.get("/").text
    for marker in ('id="dataStatus"', "function loadDataStatus", "function gotoDataStatus", 'tab === "data"',
                   "<h3>질의 분류</h3>", 'class="qsplit"'):
        assert marker in html, marker


def test_data_status_question_breakdown(store):
    store.kv_set("ontology", {"classes": [{"id": "Question", "count": 100},
                                          {"id": "ProceduralQuestion", "count": 7},
                                          {"id": "ExpertQuestion", "count": 3}],
                                "properties": [{"id": "likelyAddressedTo", "count": 5}]})
    store.kv_set("ontology_validation", {"counts": {"error": 0, "warn": 1, "info": 0, "pass": 1}, "checks": [
        {"group": "필수 관계", "title": "질의는 어느 기관을 향한다(회의 진행·진술인 질의 제외)",
         "severity": "warn", "count": 12, "total": 90}]})
    q = data_status(store, today=date(2026, 1, 10))["questions"]
    assert q == {"total": 100, "procedural": 7, "expert": 3, "agency": 90, "agency_unresolved": 12,
                 "agency_estimated": 5}


def test_data_status_without_kg_summary_has_no_breakdown():
    from agrisea.store import Store
    assert data_status(Store(":memory:"), today=date(2026, 1, 10))["questions"] is None
