import pytest

from radar.kstartup import (KStartupClient, KStartupError, normalize_announcement, parse_response,
                            status, to_iso)

JSON = ('{"currentCount": 2, "matchCount": 3, "page": 1, "perPage": 2, "totalCount": 3, "data": ['
        '{"pbanc_sn": 101, "biz_pbanc_nm": "공고 1", "pbanc_rcpt_bgng_dt": "20260101", '
        '"pbanc_rcpt_end_dt": "20261231", "rcrt_prgs_yn": "Y", "supt_regin": "전국"},'
        '{"pbanc_sn": 102, "biz_pbanc_nm": "공고 2", "rcrt_prgs_yn": "N"}]}')
XML_ITEMS = ("<results><totalCount>1</totalCount><data><item><pbanc_sn>7</pbanc_sn>"
             "<biz_pbanc_nm>XML 공고</biz_pbanc_nm></item></data></results>")
XML_COLS = ('<results><data><item><col name="pbanc_sn">8</col><col name="biz_pbanc_nm">열 공고</col>'
            '</item></data><totalCount>1</totalCount></results>')
GATEWAY = ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
           "<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
           "<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")


def test_parse_json_and_xml():
    recs, total = parse_response(JSON)
    assert total == 3 and recs[0]["biz_pbanc_nm"] == "공고 1"
    recs, total = parse_response(XML_ITEMS)
    assert total == 1 and recs == [{"pbanc_sn": "7", "biz_pbanc_nm": "XML 공고"}]
    recs, _ = parse_response(XML_COLS)
    assert recs[0]["biz_pbanc_nm"] == "열 공고"


def test_gateway_error():
    with pytest.raises(KStartupError, match="SERVICE_KEY_IS_NOT_REGISTERED"):
        parse_response(GATEWAY)


def test_normalize_and_status():
    a = normalize_announcement({"pbanc_sn": 5, "biz_pbanc_nm": " 공고  A ", "pbanc_rcpt_bgng_dt": "2026.01.02",
                                "pbanc_rcpt_end_dt": "20260131", "rcrt_prgs_yn": "Y"})
    assert a["id"] == "5" and a["title"] == "공고 A" and a["start"] == "2026-01-02" and a["recruiting"] is True
    assert to_iso("") == ""
    from datetime import date
    assert status(a, date(2026, 1, 10)) == "open"
    assert status(a, date(2026, 2, 1)) == "closed"
    assert status(a, date(2025, 12, 1)) == "upcoming"
    assert status({**a, "recruiting": False}, date(2026, 1, 10)) == "closed"


class FakeResp:
    def __init__(self, text, code=200):
        self.text, self.status_code = text, code


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        return self.responses.pop(0)


def page(ids, total):
    import json
    return json.dumps({"totalCount": total, "data": [
        {"pbanc_sn": i, "biz_pbanc_nm": f"공고 {i}", "pbanc_rcpt_bgng_dt": "20260101",
         "pbanc_rcpt_end_dt": "20991231", "rcrt_prgs_yn": "Y"} for i in ids]})


def test_pagination_retry_and_cond():
    s = FakeSession([FakeResp("busy", 503), FakeResp(page([1, 2], 3)), FakeResp(page([3], 3))])
    c = KStartupClient("SECRET", session=s, sleep=lambda _: None)
    out = c.announcements(since="2026-01-01", open_only=True, max_pages=10, per_page=2)
    assert [a["id"] for a in out] == ["1", "2", "3"]
    url, params = s.calls[0]
    assert url.endswith("/getAnnouncementInformation01") and params["serviceKey"] == "SECRET"
    assert params["cond[rcrt_prgs_yn::EQ]"] == "Y" and params["cond[pbanc_rcpt_bgng_dt::GTE]"] == "20260101"
    assert len(s.calls) == 3


def test_auth_error_not_retried_and_key_masked():
    s = FakeSession([FakeResp(GATEWAY)])
    c = KStartupClient("SECRET", session=s, sleep=lambda _: None)
    with pytest.raises(KStartupError) as e:
        c.fetch_page("getAnnouncementInformation01")
    assert "SECRET" not in str(e.value) and len(s.calls) == 1


def test_proxy_omits_key_and_requires_config():
    s = FakeSession([FakeResp(page([1], 1))])
    c = KStartupClient(proxy_url="https://deploy.test/api/kstartup/", session=s)
    c.fetch_page("getBusinessInformation01")
    url, params = s.calls[0]
    assert url == "https://deploy.test/api/kstartup/getBusinessInformation01" and "serviceKey" not in params
    with pytest.raises(KStartupError):
        KStartupClient("")
