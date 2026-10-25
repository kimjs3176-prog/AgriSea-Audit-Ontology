import json

import pytest
from fastapi.testclient import TestClient

from agrisea import external, followup
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


class _Resp:
    def __init__(self, data):
        self._data = data
        self.text = json.dumps(data, ensure_ascii=False)

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _Http:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if "open.assembly.go.kr" in url:
            return _Resp({"nzmimeepazxkubdpn": [
                {"head": [{"list_total_count": 3}, {"RESULT": {"CODE": "INFO-000", "MESSAGE": "정상"}}]},
                {"row": [
                    {"BILL_NO": "2201234", "BILL_NAME": "축산법 일부개정법률안", "PROPOSER": "홍길동의원 등 10인",
                     "PROPOSE_DT": "2025-03-02", "PROC_RESULT": "수정가결", "DETAIL_LINK": "https://likms.assembly.go.kr/x"},
                    {"BILL_NO": "2205555", "BILL_NAME": "축산법 일부개정법률안", "PROPOSER": "김가람의원 등 10인",
                     "PROPOSE_DT": "20250801", "PROC_RESULT": None},
                    {"BILL_NO": "2100001", "BILL_NAME": "축산법 일부개정법률안", "PROPOSE_DT": "2024-01-01",
                     "PROC_RESULT": "임기만료폐기"},  # 약속 전 발의: 제외
                ]}]})
        return _Resp({"data": [
            {"pbanc_sn": 1, "biz_pbanc_nm": "2025년 가축분뇨 퇴비화 지원사업 공고", "pbanc_ntrp_nm": "농식품부",
             "pbanc_rcpt_bgng_dt": "20250301", "pbanc_rcpt_end_dt": "20250331", "detl_pg_url": "https://k-startup.go.kr/1"},
            {"pbanc_sn": 2, "biz_pbanc_nm": "스마트팜 창업 공고", "pbanc_rcpt_bgng_dt": "20250301"},  # 검색어 없음
            {"pbanc_sn": 3, "biz_pbanc_nm": "가축분뇨 처리 시범사업", "pbanc_rcpt_bgng_dt": "20240101"},  # 약속 전
        ], "totalCount": 3})


def test_external_bills_and_programs(tmp_path):
    from agrisea import programs as P
    http = _Http()
    s = Settings(api_key="SECRET1", data_dir=tmp_path)
    progs = [a for a in map(P.normalize, [
        {"pbanc_sn": 1, "biz_pbanc_nm": "2025년 가축분뇨 퇴비화 지원사업 공고", "pbanc_ntrp_nm": "농식품부",
         "pbanc_rcpt_bgng_dt": "20250301", "pbanc_rcpt_end_dt": "20250331", "detl_pg_url": "https://k-startup.go.kr/1"},
        {"pbanc_sn": 2, "biz_pbanc_nm": "스마트팜 창업 공고", "pbanc_rcpt_bgng_dt": "20250301"},       # 대상어 없음
        {"pbanc_sn": 3, "biz_pbanc_nm": "가축분뇨 처리 시범사업", "pbanc_ntrp_nm": "농식품부",
         "pbanc_rcpt_bgng_dt": "20240101"},                                                         # 약속 전
    ]) if a]
    item = {"date": "2025-01-10", "laws": ["축산법"], "keywords": ["가축분뇨", "퇴비화"], "action": "budget",
            "issues": ["축산·가축방역"]}
    out = external.external_evidence(s, item, http, programs=progs)
    assert [x["status"] for x in out["sources"]] == ["ok", "ok"]
    bills = [e for e in out["evidence"] if e["source"] == "bill"]
    assert [(e["kind"], e["date"]) for e in bills] == [("done", "2025-03-02"), ("progress", "2025-08-01")]
    ann = [e for e in out["evidence"] if e["source"] == "program"]
    assert len(ann) == 1 and ann[0]["kind"] == "progress" and ann[0]["date"] == "2025-03-01"
    bill_call = next(p for u, p in http.calls if "assembly" in u)
    assert bill_call["BILL_NAME"] == "축산법" and bill_call["AGE"] == "22"
    assert not any("data.go.kr" in u for u, _ in http.calls)  # 공고는 수집해 둔 목록에서(실시간 API 없음)


def test_external_keeps_late_completion_beyond_many_records(tmp_path):
    """앞선 계류 의안이 많아도 뒤늦게 가결된 의안(판정을 바꾸는 근거)을 잘라 내지 않는다."""
    rows = [{"BILL_NO": str(i), "BILL_NAME": "축산법 일부개정법률안", "PROPOSE_DT": f"2025-02-{i + 1:02d}",
             "PROC_RESULT": None} for i in range(14)]
    rows.append({"BILL_NO": "99", "BILL_NAME": "축산법 일부개정법률안", "PROPOSE_DT": "2025-09-01", "PROC_RESULT": "원안가결"})

    class Http:
        def get(self, url, params=None, timeout=None):
            return _Resp({"nzmimeepazxkubdpn": [{"head": [{"list_total_count": len(rows)}]}, {"row": rows}]})

    item = {"id": 1, "date": "2025-01-10", "laws": ["축산법"], "keywords": [], "action": "law"}
    out = external.external_evidence(Settings(api_key="K", data_dir=tmp_path), item, Http())
    assert len(out["evidence"]) == 15
    assert followup.verdict(item["date"], out["evidence"], "2025-12-01") == "done"


def test_external_reads_later_pages(tmp_path):
    """맞는 의안이 첫 쪽(100건) 뒤에 있어도 찾는다."""
    bills_p1 = [{"BILL_NO": str(i), "BILL_NAME": "다른법 일부개정법률안", "PROPOSE_DT": "2025-02-01"} for i in range(100)]
    bill = {"BILL_NO": "777", "BILL_NAME": "축산법 일부개정법률안", "PROPOSE_DT": "2025-05-01", "PROC_RESULT": "원안가결"}

    class Http:
        def get(self, url, params=None, timeout=None):
            rows = bills_p1 if params["pIndex"] == 1 else [bill]
            return _Resp({"nzmimeepazxkubdpn": [{"head": [{"list_total_count": 101}]}, {"row": rows}]})

    item = {"id": 1, "date": "2025-01-10", "laws": ["축산법"], "keywords": ["가축분뇨"], "action": "budget"}
    out = external.external_evidence(Settings(api_key="K", data_dir=tmp_path), item, Http())
    assert {(e["source"], e["title"]) for e in out["evidence"]} == {("bill", "축산법 일부개정법률안")}


def test_parse_kstartup_envelopes():
    row = {"pbanc_sn": "7", "biz_pbanc_nm": "가축분뇨 공고"}
    assert external.parse_kstartup(json.dumps({"data": [row]})) == [row]
    # 공공데이터포털 표준 봉투: 한 건이면 item이 객체로 온다
    one = {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": row}, "totalCount": 1}}}
    assert external.parse_kstartup(json.dumps(one)) == [row]
    many = {"response": {"header": {"resultCode": "00"}, "body": {"items": [row, row]}}}
    assert len(external.parse_kstartup(json.dumps(many))) == 2
    xml = '<response><header><resultCode>00</resultCode></header><body><items><item>' \
          '<col name="pbanc_sn">7</col><col name="biz_pbanc_nm">가축분뇨 공고</col></item></items></body></response>'
    assert external.parse_kstartup(xml) == [row]
    with pytest.raises(RuntimeError, match="SERVICE_KEY"):
        external.parse_kstartup("<OpenAPI_ServiceResponse><cmmMsgHeader><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR"
                                "</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
    with pytest.raises(RuntimeError):
        external.parse_kstartup(json.dumps({"response": {"header": {"resultCode": "30", "resultMsg": "키 오류"}}}))


def test_external_skips_and_masks_keys(tmp_path):
    item = {"date": "2025-01-10", "laws": [], "keywords": ["가축분뇨"], "action": "report"}
    out = external.external_evidence(Settings(api_key="", kstartup_key="", data_dir=tmp_path), item)
    assert [x["status"] for x in out["sources"]] == ["skipped", "skipped"] and out["evidence"] == []

    class Boom:
        def get(self, url, params=None, timeout=None):
            import requests
            raise requests.ConnectionError(f"{url}?serviceKey={params.get('serviceKey')}&KEY={params.get('KEY')} 실패")

    item = {"date": "2025-01-10", "laws": ["축산법"], "keywords": ["가축분뇨"], "action": "budget"}
    out = external.external_evidence(Settings(api_key="SECRET1", data_dir=tmp_path), item, Boom())
    assert [x["status"] for x in out["sources"]] == ["error", "skipped"]  # 공고는 아직 수집 전
    assert "SECRET1" not in " ".join(x["message"] for x in out["sources"])


def test_followup_endpoints(tmp_path, monkeypatch):
    s = _tracked_store(tmp_path)
    c = TestClient(create_app(Settings(api_key="K", data_dir=tmp_path), s))
    d = c.get("/api/followups").json()
    assert d["total"] == 2 and d["counts"]["done"] == 1 and d["orgs"][0]["name"] == "농림축산식품부"
    assert c.get("/api/followups", params={"q": "퇴비화"}).json()["total"] == 1
    assert c.get("/api/followups", params={"action": "law"}).json()["total"] == 1
    manure = next(x for x in d["items"] if "가축분뇨" in x["keywords"])
    detail = c.get(f"/api/followups/{manure['id']}").json()
    assert detail["status"] == "reraised" and detail["external"] is None and detail["status_desc"]
    assert c.get("/api/followups/999999").status_code == 404

    # 외부 근거(지원사업 공고)가 진행 근거를 더하면 재판정, 결과는 저장해 다시 묻지 않는다
    calls = []

    def fake(settings, item, http=None, programs=None):
        calls.append(item["id"])
        return {"fetched_at": "2025-11-01T00:00:00+00:00", "sources": [],
                "evidence": [{"source": "kstartup", "kind": "progress", "date": "2025-10-20",
                              "title": "가축분뇨 퇴비화 지원사업", "text": "", "url": "https://k-startup.go.kr/1"}]}

    monkeypatch.setattr(external, "external_evidence", fake)
    r = c.get(f"/api/followups/{manure['id']}/external").json()
    assert r["status"] == "progress" and r["external"]["evidence"]
    c.get(f"/api/followups/{manure['id']}/external")
    assert len(calls) == 1
    assert c.get(f"/api/followups/{manure['id']}").json()["status"] == "progress"
    # 목록도 외부 근거까지 반영한 상태로 세고 거른다
    d = c.get("/api/followups").json()
    row = next(x for x in d["items"] if x["id"] == manure["id"])
    assert row["checked"] and row["status"] == "progress"
    assert d["counts"]["reraised"] == 0 and d["counts"]["progress"] == 1


def test_external_skipped_everywhere_is_not_marked_checked(tmp_path):
    """인증키가 없어 모든 출처를 건너뛰면 '외부 확인' 표시도, 하루 동안의 저장도 하지 않는다."""
    s = _tracked_store(tmp_path)
    c = TestClient(create_app(Settings(api_key="", data_dir=tmp_path), s))
    item = c.get("/api/followups").json()["items"][0]
    r = c.get(f"/api/followups/{item['id']}/external").json()
    assert all(x["status"] == "skipped" for x in r["external"]["sources"])
    assert "API_KEY" not in " ".join(x["message"] for x in r["external"]["sources"])  # 환경변수 이름 노출 없이
    assert not next(x for x in c.get("/api/followups").json()["items"] if x["id"] == item["id"])["checked"]
