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
