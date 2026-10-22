import json
import sqlite3

import pytest

from radar.policy import background, build_signals, radar, topic_detail


@pytest.fixture()
def agrisea_db(tmp_path):
    """국회 회의록 DB와 같은 테이블 구조의 가상 데이터."""
    path = tmp_path / "agrisea.sqlite3"
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE meetings (id TEXT PRIMARY KEY, title TEXT, date TEXT, text_status TEXT, is_sample INTEGER);
    CREATE TABLE utterances (id INTEGER PRIMARY KEY, meeting_id TEXT, idx INTEGER, speaker_name TEXT,
        speaker_role TEXT, speaker_type TEXT, org TEXT, text TEXT, is_question INTEGER,
        is_commitment INTEGER, orgs_json TEXT);
    """)
    c.executemany("INSERT INTO meetings VALUES (?,?,?,?,?)", [
        ("M1", "테스트 회의 1", "2025-03-10", "parsed", 0),
        ("M2", "테스트 회의 2", "2025-11-05", "parsed", 0),
        ("S1", "[예시] 회의", "2025-11-05", "parsed", 1),
    ])
    rows = [
        ("M1", 1, "가위원", "위원", "member", "", "스마트팜 보급이 왜 이렇게 늦습니까?", 1, 0, "{}"),
        ("M2", 1, "나위원", "위원", "member", "", "청년농 스마트 팜 지원 확대 계획이 있습니까?", 1, 0, "{}"),
        ("M2", 2, "다장관", "농림축산식품부장관", "official", "농림축산식품부",
         "말씀하신 대로 스마트팜 창업 지원을 내년에 확대하겠습니다. 감사합니다.", 0, 1,
         json.dumps({"농림축산식품부": 1})),
        ("S1", 1, "라위원", "위원", "member", "", "스마트팜 예시 발언입니까?", 1, 0, "{}"),
    ]
    c.executemany("INSERT INTO utterances (meeting_id, idx, speaker_name, speaker_role, speaker_type, org, "
                  "text, is_question, is_commitment, orgs_json) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return path


def test_build_signals(agrisea_db):
    s = build_signals(agrisea_db)
    t = s["topics"]["smartfarm"]
    assert s["source"]["meetings"] == 2                       # 예시 회의 제외
    assert t["mentions"] == 3 and t["questions"] == 2
    assert t["monthly"] == {"2025-03": 1, "2025-11": 2}
    assert len(t["commitments"]) == 1 and "확대하겠습니다" in t["commitments"][0]["text"]
    assert t["commitments"][0]["org"] == "농림축산식품부"
    assert {m["name"] for m in t["members"]} == {"가위원", "나위원"}
    assert s["topics"]["youthfarm"]["mentions"] == 1


def test_radar_and_tracking(agrisea_db, anns):
    s = build_signals(agrisea_db)
    r = radar(s, anns)
    row = next(x for x in r["topics"] if x["id"] == "smartfarm")
    assert row["supply"] >= 2 and row["open"] == 2 and row["commitments"] == 1
    assert row["quadrant"] in ("gap", "active", "supply", "latent")
    d = topic_detail(s, anns, "smartfarm")
    assert d["tracking"][0]["followups"] == 2               # 약속 이후 스마트팜 공고 2건
    assert {a["id"] for a in d["open_announcements"]} == {"A1", "A4"}
    bg = background(s, anns[0])
    assert bg and bg[0]["topic"] == "smartfarm" and bg[0]["commitments"]


def test_radar_without_signals(anns):
    r = radar(None, anns)
    assert len(r["topics"]) > 5 and all(x["demand"] == 0 for x in r["topics"])
