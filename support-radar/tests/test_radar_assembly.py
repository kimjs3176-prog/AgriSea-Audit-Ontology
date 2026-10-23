from pathlib import Path

import pytest

from radar import assembly
from radar.assembly import Extracts, collect_meetings, fetch_pending

SAMPLE = Path(__file__).resolve().parents[2] / "agrisea" / "data" / "sample" / "audit_mafra.txt"


class FakeClient:
    def __init__(self, by_month, fail=()):
        self.by_month, self.fail, self.calls = by_month, set(fail), []

    def committee_meetings(self, only_target=True, **params):
        from agrisea.api_client import AssemblyAPIError
        assert only_target is False                      # 위원회를 거르지 않는다
        self.calls.append(params["CONF_DATE"])
        if params["CONF_DATE"] in self.fail:
            raise AssemblyAPIError("HTTP", "timeout")
        return self.by_month.get(params["CONF_DATE"], [])


def meeting(mid, d, committee, url="https://record.assembly.go.kr/x?id=1"):
    return {"meeting_id": mid, "date": d, "title": f"{committee} 회의", "committee": committee,
            "pdf_url": url, "link_url": ""}


def test_collect_all_committees_monthly():
    client = FakeClient({
        "2025-10": [meeting("A", "2025-10-01", "산업통상자원중소벤처기업위원회"),
                    meeting("B", "2025-10-14", "농림축산식품해양수산위원회"),
                    meeting("D", "2025-09-30", "기획재정위원회")],          # 조회 범위 밖
        "2025-11": [meeting("C", "2025-11-03", "과학기술정보방송통신위원회")],
    }, fail={"2025-12"})
    ext = Extracts()
    log = []
    n = collect_meetings(ext, client, "2025-10-01", "2025-12-05", skip_ids={"B"}, progress=log.append)
    assert client.calls == ["2025-10", "2025-11", "2025-12"]
    assert n == 3 and ext.meetings["A"]["status"] == "pending" and ext.meetings["B"]["status"] == "agrisea"
    assert "D" not in ext.meetings and any("조회 실패" in x for x in log)
    assert collect_meetings(ext, client, "2025-10-01", "2025-10-31", progress=log.append) == 0  # 중복 없음


def test_collect_all_months_failing_raises():
    from agrisea.api_client import AssemblyAPIError
    with pytest.raises(AssemblyAPIError):
        collect_meetings(Extracts(), FakeClient({}, fail={"2025-10"}), "2025-10-01", "2025-10-31", progress=lambda _: None)


def test_fetch_pending_parses_and_retries(monkeypatch, tmp_path):
    import agrisea.pipeline as pipeline
    text = SAMPLE.read_text(encoding="utf-8")
    calls = []

    def fake_minutes(url, settings, name, http=None):
        calls.append(name)
        if name == "BAD":
            raise RuntimeError("PDF 서버 응답 없음")
        return text

    monkeypatch.setattr(pipeline, "minutes_text", fake_minutes)
    ext = Extracts(meetings={
        "OK": {"date": "2025-10-02", "title": "회의", "committee": "산업통상자원중소벤처기업위원회",
               "pdf_url": "u", "status": "pending", "attempts": 0},
        "BAD": {"date": "2025-10-01", "title": "회의", "committee": "기획재정위원회",
                "pdf_url": "u", "status": "pending", "attempts": 0},
        "NOURL": {"date": "2025-09-01", "title": "회의", "committee": "x", "status": "pending", "attempts": 0},
        "DONE": {"date": "2025-10-03", "title": "회의", "committee": "x", "pdf_url": "u", "status": "parsed"},
    })
    res = fetch_pending(ext, settings=None, limit=10, progress=lambda _: None, fix=str.strip)
    assert calls == ["OK", "BAD"] and res["parsed"] == 1 and res["failed"] == 1
    assert ext.meetings["OK"]["status"] == "parsed" and ext.meetings["OK"]["utterances"] > 10
    assert ext.meetings["BAD"] == {**ext.meetings["BAD"], "status": "failed", "attempts": 1}
    assert ext.meetings["NOURL"]["status"] == "metadata_only"
    ok = [u for u in ext.utterances if u["meeting_id"] == "OK"]
    assert ok and all(u["committee"] == "산업통상자원중소벤처기업위원회" for u in ok)
    # 실패는 MAX_ATTEMPTS 번까지만 다시 시도
    for _ in range(5):
        fetch_pending(ext, settings=None, limit=10, progress=lambda _: None)
    assert ext.meetings["BAD"]["attempts"] == assembly.MAX_ATTEMPTS


def test_save_load_roundtrip(tmp_path):
    ext = Extracts(meetings={"X": {"date": "2025-01-01", "title": "t", "committee": "c", "status": "pending"}},
                   meta={"collected_to": "2025-01-31"})
    ext.add_parsed("X", [{"speaker_name": "가", "speaker_role": "위원", "speaker_type": "member",
                          "text": "반려동물 산업 육성이 필요합니다.", "is_question": False, "is_commitment": False}])
    p = tmp_path / "e.json.gz"
    ext.save(p)
    first = p.read_bytes()
    back = Extracts.load(p)
    assert back.meetings == ext.meetings and back.utterances == ext.utterances
    back.save(p)
    assert p.read_bytes() == first          # 같은 내용이면 같은 파일(불필요한 커밋 방지)
    assert Extracts.load(tmp_path / "none.json.gz").meetings == {}


def test_update_resumes_and_refetches(monkeypatch):
    import agrisea.api_client as api_client
    import agrisea.pipeline as pipeline
    from datetime import date

    client = FakeClient({"2025-10": [meeting("A", "2025-10-01", "기획재정위원회")]})
    monkeypatch.setattr(api_client, "AssemblyClient", lambda settings: client)
    monkeypatch.setattr(pipeline, "minutes_text", lambda *a, **k: SAMPLE.read_text(encoding="utf-8"))
    ext = Extracts()
    res = assembly.update(ext, limit=5, date_from="2025-10-01", date_to="2025-10-31", progress=lambda _: None)
    assert res["new_meetings"] == 1 and res["parsed"] == 1 and ext.meta["collected_to"] == "2025-10-31"
    # 다음 실행은 마지막 조회일 7일 전부터 오늘까지
    client.calls.clear()
    assembly.update(ext, limit=0, progress=lambda _: None)
    assert client.calls[0] == "2025-10" and client.calls[-1] == date.today().isoformat()[:7]
    assert assembly.update(ext, limit=5, date_from="2025-10-01", date_to="2025-10-31",
                           refetch=True, progress=lambda _: None)["parsed"] == 1
