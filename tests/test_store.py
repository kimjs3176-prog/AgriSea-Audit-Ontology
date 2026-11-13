from concurrent.futures import ThreadPoolExecutor

from agrisea.pipeline import load_sample
from agrisea.store import Store


def test_concurrent_reads_share_one_connection(tmp_path):
    """웹 서버는 요청을 스레드 풀에서 처리한다: 연결 하나를 동시에 써도 깨지지 않아야 한다
    (대시보드가 /api/issues·/api/issue-trend·/api/overview 를 한꺼번에 부르면 500이 났음)."""
    s = Store(tmp_path / "db.sqlite3")
    load_sample(s)
    s.kv_set("overview", {"ok": True})

    def work(i: int):
        for _ in range(30):
            assert s.kv_get("overview") == {"ok": True}
            assert s.stats()["meetings"] == 4
            assert len(s.meetings()) == 4
            assert s.utterances(meeting_id="SAMPLE-2025-1014")
        return i

    with ThreadPoolExecutor(max_workers=12) as ex:
        assert sorted(ex.map(work, range(24))) == list(range(24))


def test_transaction_rolls_back_and_releases_lock(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    try:
        with s.tx() as c:
            c.execute("INSERT INTO kv(key, value) VALUES ('x', '1')")
            raise RuntimeError
    except RuntimeError:
        pass
    assert s.kv_get("x") is None
    s.kv_set("x", 2)  # 잠금이 풀려 있어야 다음 쓰기가 된다
    assert s.kv_get("x") == 2


def test_upsert_keeps_stored_agenda_order_when_api_reshuffles():
    """API가 같은 안건을 다른 순서로 주면 저장된 순서를 지킨다(내용 해시가 그대로라 재계산·데이터 커밋이 반복되지 않음).
    안건이 실제로 바뀌면 새 목록을 받는다."""
    from agrisea.store import Store
    s = Store(":memory:")
    base = {"meeting_id": "M1", "title": "제439회 제3차 소위원회", "date": "2026-11-03", "session_no": 439}
    s.upsert_meeting({**base, "agendas": ["1. 농협법(갑)", "2. 농협법(을)", "14. 기본법(병)"]})
    before = s.content_hash()
    s.upsert_meeting({**base, "agendas": ["14. 기본법(병)", "1. 농협법(갑)", "2. 농협법(을)"]})
    assert s.meeting("M1")["agendas"] == ["1. 농협법(갑)", "2. 농협법(을)", "14. 기본법(병)"]
    assert s.content_hash() == before
    s.upsert_meeting({**base, "agendas": ["1. 농협법(갑)", "3. 양곡법(정)"]})
    assert s.meeting("M1")["agendas"] == ["1. 농협법(갑)", "3. 양곡법(정)"]


def test_parsed_meeting_keeps_minutes_agendas():
    """회의록을 받으면 안건 목록은 회의록 첫머리 순서(발언 agenda_idx 기준)가 되고, 이후 API가 다른 순서·일부만 줘도
    바뀌지 않는다(예전에는 API 목록으로 덮여 발언에 다른 안건 제목이 붙었다)."""
    from agrisea.parser import parse_minutes
    from agrisea.store import Store
    s = Store(":memory:")
    base = {"meeting_id": "M1", "title": "제439회 제3차 소위원회", "date": "2026-11-03", "session_no": 439}
    s.upsert_meeting({**base, "agendas": ["2. 기본법(을)(의안번호 2200002)", "1. 농협법(갑)(의안번호 2200001)"]})
    s.save_minutes("M1", parse_minutes("의사일정\n1. 농협법(갑)(의안번호 2200001)\n2. 기본법(을)(의안번호 2200002)\n"
                                       "◯위원장 홍길동 상정합니다.\n2. 기본법(을)(의안번호 2200002)\n"
                                       "◯김가람 위원 기본법 질의입니다.\n"))
    want = ["1. 농협법(갑)(의안번호 2200001)", "2. 기본법(을)(의안번호 2200002)"]
    assert s.meeting("M1")["agendas"] == want
    assert [u["agenda_idx"] for u in s.utterances(meeting_id="M1")] == [0, 1]
    s.upsert_meeting({**base, "agendas": ["2. 기본법(을)(의안번호 2200002)"]})
    assert s.meeting("M1")["agendas"] == want
