
from fastapi.testclient import TestClient

from agrisea import followup
from agrisea.config import Settings
from agrisea.parser import parse_minutes
from agrisea.store import Store
from agrisea.web.app import create_app


def _meeting(store: Store, mid: str, date: str, text: str, agendas: list[str] | None = None) -> None:
    store.upsert_meeting({"meeting_id": mid, "title": f"제420회 제1차 농림축산식품해양수산위원회 {mid}",
                          "date": date, "agendas": agendas or [], "session_no": 420})
    store.save_minutes(mid, parse_minutes(text))


def _tracked_store(tmp_path) -> Store:
    """약속 둘(산란계 사육밀도 → 이행 보고, 가축분뇨 퇴비화 → 재지적)과 대상어를 드물게 만들 일반 발언."""
    s = Store(tmp_path / "t.sqlite3", fts=False)
    filler = "\n".join(f"◯김가람 위원 오늘 회의 진행 순서에 대해서 {i}번째 의견을 드립니다." for i in range(1500))
    _meeting(s, "Z", "2024-12-01", filler)
    _meeting(s, "A", "2025-01-10", """
◯김가람 위원 장관님, 산란계가 걱정입니다. 산란계 사육밀도가 문제입니다. 산란계를 키우는 사육밀도를 보면 산란계의 사육밀도는 너무 높지 않습니까?
◯농림축산식품부장관 이하늘 산란계 사육밀도 기준을 높이기 위해 축산법 시행규칙 개정을 추진하겠습니다.
◯이나래 위원 가축분뇨가 넘칩니다. 가축분뇨를 퇴비화가 가능한 시설로 보내야 하는데 가축분뇨의 퇴비화를 지원할 계획이 있습니까? 퇴비화의 문제가 큽니다.
◯농림축산식품부장관 이하늘 가축분뇨 퇴비화 시설을 확충하도록 지원하겠습니다.
""")
    _meeting(s, "B", "2025-06-10", "◯위원장 홍길동 안건을 상정합니다.",
             agendas=["1. 축산법 일부개정법률안(홍길동 의원 대표발의)(의안번호 2201234)"])
    _meeting(s, "C", "2025-09-10", """
◯김가람 위원 그 뒤 어떻게 되었습니까?
◯농림축산식품부장관 이하늘 산란계 사육밀도 기준을 높이는 축산법 시행규칙 개정을 완료했습니다.
""")
    _meeting(s, "D", "2025-10-01", """
◯이나래 위원 작년에 가축분뇨 퇴비화 시설을 확충하겠다고 약속하셨는데 농식품부는 아직도 그대로입니까?
""")
    return s


def test_law_names_and_actions():
    assert followup.law_names("축산법 시행규칙을 개정하고 수의사법도 보겠습니다") == ["축산법", "수의사법"]
    assert followup.law_names("좋은 방법이 있고 불법 어업과 기본법은 다릅니다") == []
    assert followup.action_of("시행령 개정을 추진하겠습니다") == "law"
    assert followup.action_of("자료를 제출하겠습니다") == "report"
    assert followup.action_of("예산을 확보하겠습니다") == "budget"


def test_verdict_rules():
    ev = lambda kind, d: {"kind": kind, "date": d}
    assert followup.verdict("2025-01-01", [ev("progress", "2025-02-01"), ev("done", "2025-03-01")], "2025-12-01") == "done"
    # 재지적 뒤에 진행 근거가 없으면 재지적, 재지적 뒤에 진행 근거가 나오면 진행 중
    assert followup.verdict("2025-01-01", [ev("progress", "2025-02-01"), ev("reraised", "2025-03-01")], "2025-12-01") == "reraised"
    assert followup.verdict("2025-01-01", [ev("reraised", "2025-02-01"), ev("progress", "2025-03-01")], "2025-12-01") == "progress"
    assert followup.verdict("2025-11-01", [], "2025-12-01") == "early"
    assert followup.verdict("2025-01-01", [], "2025-12-01") == "none"


def test_started_work_is_progress_not_done():
    assert not followup.DONE.search("사육밀도 기준 개정 작업에 착수했습니다")
    assert followup.PROGRESS.search("사육밀도 기준 개정 작업에 착수했습니다")
    assert followup.DONE.search("사육밀도 기준 개정을 완료했습니다")


def test_track_all_finds_done_progress_and_reraised(tmp_path):
    r = followup.track_all(_tracked_store(tmp_path))
    by = {x["laws"][0] if x["laws"] else x["keywords"][0]: x for x in r["items"]}
    hen = by["축산법"]
    assert hen["status"] == "done" and hen["action"] == "law" and "산란계" in hen["keywords"]
    assert hen["question"]["member"] == "김가람"
    kinds = {(e["source"], e["kind"]) for e in hen["evidence"]}
    assert ("agenda", "progress") in kinds and ("minutes", "done") in kinds
    done = next(e for e in hen["evidence"] if e["kind"] == "done")
    assert done["meeting_id"] == "C" and "완료" in done["text"]
    manure = next(x for x in r["items"] if "가축분뇨" in x["keywords"])
    assert manure["status"] == "reraised"
    assert next(e for e in manure["evidence"] if e["kind"] == "reraised")["meeting_id"] == "D"
    assert r["counts"]["done"] == 1 and r["counts"]["reraised"] == 1


def test_track_all_on_sample_data(store):
    r = followup.track_all(store)
    assert r["items"] and set(r["counts"]) == set(followup.STATUS)
    assert sum(r["counts"].values()) == len(r["items"])
    for x in r["items"]:
        assert x["status"] in followup.STATUS and x["keywords"] or x["laws"]


def test_followup_endpoints(tmp_path):
    s = _tracked_store(tmp_path)
    c = TestClient(create_app(Settings(api_key="K", data_dir=tmp_path), s))
    d = c.get("/api/followups").json()
    assert d["total"] == 2 and d["counts"]["done"] == 1 and d["orgs"][0]["name"] == "농림축산식품부"
    assert c.get("/api/followups", params={"q": "퇴비화"}).json()["total"] == 1
    assert c.get("/api/followups", params={"action": "law"}).json()["total"] == 1
    manure = next(x for x in d["items"] if "가축분뇨" in x["keywords"])
    detail = c.get(f"/api/followups/{manure['id']}").json()
    assert detail["status"] == "reraised" and detail["status_desc"]
    assert c.get("/api/followups/999999").status_code == 404
    assert c.get(f"/api/followups/{manure['id']}/external").status_code == 404  # 외부 근거 확인은 없앰
