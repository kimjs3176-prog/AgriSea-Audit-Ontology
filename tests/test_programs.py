import json

import pytest
from fastapi.testclient import TestClient

from agrisea import programs as P
from agrisea.config import Settings
from agrisea.web.app import create_app


def _raw(sn, title, start, end="", org="", content=""):
    return {"pbanc_sn": sn, "biz_pbanc_nm": title, "pbanc_rcpt_bgng_dt": start.replace("-", ""),
            "pbanc_rcpt_end_dt": end.replace("-", ""), "pbanc_ntrp_nm": org, "pbanc_ctnt": content,
            "detl_pg_url": f"https://www.k-startup.go.kr/{sn}"}


def test_normalize_keeps_only_agri_sea_programs():
    farm = P.normalize(_raw(1, "2026 스마트팜 창업 지원", "2026-03-01", "2026-03-31"))
    assert farm["issues"] == {"smartagri": 3} and farm["start"] == "2026-03-01"
    agency = P.normalize(_raw(2, "2026년 벤처육성 지원사업", "2026-01-20", org="한국농업기술진흥원"))
    assert agency["agency"] == "한국농업기술진흥원" and agency["issues"] == {"agri": 1}  # 피감기관 공고
    sea = P.normalize(_raw(3, "청년 어촌창업 공모전", "2026-10-07", org="한국어촌어항공단"))
    assert sea["agency"] == "한국어촌어항공단" and "fishery" in sea["issues"]
    # 창업 공고 전반의 넓은 말('해운대'·'관세'·'R&D')로는 농해수 공고로 보지 않는다
    for title in ("해운대 청년창업공간 운영기관 모집", "관세·ESG 리스크 교육", "강원 R&D 특구 배치 프로그램"):
        assert P.normalize(_raw(9, title, "2026-01-01")) is None
    assert P.status({"start": "2026-03-01", "end": "2026-03-31"}, "2026-03-10") == "open"
    assert P.status({"start": "2026-03-01", "end": "2026-03-31"}, "2026-04-01") == "closed"
    assert P.status({"start": "2026-05-01", "end": "2026-05-31"}, "2026-04-01") == "upcoming"


class _Resp:
    def __init__(self, data, code=200):
        self.text, self.status_code = json.dumps(data), code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_fetch_pages_until_older_than_since_and_masks_key(monkeypatch):
    monkeypatch.setattr(P, "PAGE_SIZE", 2)
    pages = {1: [_raw(5, "스마트팜 A", "2026-05-01"), _raw(4, "다른 공고", "2026-04-01")],
             2: [_raw(3, "스마트팜 B", "2025-01-01"), _raw(2, "다른 공고", "2024-01-01")],
             3: [_raw(1, "스마트팜 C", "2023-01-01"), _raw(0, "x", "2023-01-01")],
             4: [_raw(-1, "스마트팜 D", "2022-01-01")]}
    calls = []

    class Http:
        def get(self, url, params=None, timeout=None):
            calls.append((url, dict(params)))
            return _Resp({"data": pages[params["page"]], "totalCount": 7})

    rows = P.fetch(Settings(kstartup_key="SECRET"), since="2024-05-30", http=Http(), progress=lambda m: None)
    assert sorted(r["title"] for r in rows) == ["스마트팜 A", "스마트팜 B"]  # since 이전 공고 제외
    assert [p["page"] for _, p in calls] == [1, 2, 3]  # 한 쪽 전체가 since 이전이면 멈춤
    assert calls[0][0] == P.API_URL and calls[0][1]["serviceKey"] == "SECRET"

    class Boom:
        def get(self, url, params=None, timeout=None):
            import requests
            raise requests.ConnectionError(f"{url}?serviceKey={params.get('serviceKey')} reset")

    with pytest.raises(P.ProgramError) as e:
        P.fetch(Settings(kstartup_key="SECRET"), http=Boom(), progress=lambda m: None, sleep=lambda s: None)
    assert "SECRET" not in str(e.value)

    relay = []

    class Relay:
        def get(self, url, params=None, timeout=None):
            relay.append((url, dict(params)))
            return _Resp({"data": []})

    P.fetch(Settings(kstartup_key="", kstartup_proxy="https://x.vercel.app/api/kstartup"), http=Relay(),
            progress=lambda m: None)
    assert relay[0][0] == "https://x.vercel.app/api/kstartup" and "serviceKey" not in relay[0][1]  # 키는 중계 서버가


def test_programs_in_issue_briefing_dashboard(tmp_path):
    from test_followup import _tracked_store
    s = _tracked_store(tmp_path)
    s.save_programs([P.normalize(_raw(10, "가축분뇨 퇴비화 스마트팜 지원사업", "2099-01-01", "2099-02-01",
                                      org="농림축산식품부"))])
    c = TestClient(create_app(Settings(api_key="K", data_dir=tmp_path), s))
    d = c.get("/api/issues/smartagri").json()["programs"]
    assert d["total"] == 1 and d["items"][0]["status"] == "upcoming" and d["signal"]["programs_total"] == 1
    assert c.get("/api/issues/agri").json()["programs"]["total"] == 1  # 상위 쟁점은 하위 공고까지
    ov = c.get("/api/programs/overview").json()
    assert ov["total"] == 1 and ov["upcoming"] == 1
    b = c.get("/api/briefing", params={"org": "농림축산식품부"}).json()
    assert b["programs"]["total"] == 1
    md = c.get("/api/briefing", params={"org": "농림축산식품부", "format": "md"}).text
    assert "지원사업 공고" in md and "가축분뇨 퇴비화" in md
    assert c.get("/api/kstartup").status_code == 400  # 배포본에 키가 없으면 중계하지 않음
    assert c.get("/api/stats").json()["programs"] == 1


def test_parse_kstartup_envelopes():
    row = {"pbanc_sn": "7", "biz_pbanc_nm": "가축분뇨 공고"}
    assert P.parse_kstartup(json.dumps({"data": [row]})) == [row]
    # 공공데이터포털 표준 봉투: 한 건이면 item이 객체로 온다
    one = {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": row}, "totalCount": 1}}}
    assert P.parse_kstartup(json.dumps(one)) == [row]
    many = {"response": {"header": {"resultCode": "00"}, "body": {"items": [row, row]}}}
    assert len(P.parse_kstartup(json.dumps(many))) == 2
    xml = '<response><header><resultCode>00</resultCode></header><body><items><item>' \
          '<col name="pbanc_sn">7</col><col name="biz_pbanc_nm">가축분뇨 공고</col></item></items></body></response>'
    assert P.parse_kstartup(xml) == [row]
    with pytest.raises(RuntimeError, match="SERVICE_KEY"):
        P.parse_kstartup("<OpenAPI_ServiceResponse><cmmMsgHeader><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR"
                                "</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
    with pytest.raises(RuntimeError):
        P.parse_kstartup(json.dumps({"response": {"header": {"resultCode": "30", "resultMsg": "키 오류"}}}))
