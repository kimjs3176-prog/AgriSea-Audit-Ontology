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
    CREATE TABLE meetings (id TEXT PRIMARY KEY, title TEXT, committee TEXT, date TEXT, text_status TEXT,
        is_sample INTEGER);
    CREATE TABLE utterances (id INTEGER PRIMARY KEY, meeting_id TEXT, idx INTEGER, speaker_name TEXT,
        speaker_role TEXT, speaker_type TEXT, org TEXT, text TEXT, is_question INTEGER,
        is_commitment INTEGER, orgs_json TEXT);
    """)
    c.executemany("INSERT INTO meetings VALUES (?,?,?,?,?,?)", [
        ("M1", "테스트 회의 1", "", "2025-03-10", "parsed", 0),
        ("M2", "테스트 회의 2", "농림축산식품해양수산위원회", "2025-11-05", "parsed", 0),
        ("S1", "[예시] 회의", "", "2025-11-05", "parsed", 1),
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
    assert t["committees"] == [{"name": "농림축산식품해양수산위원회", "count": 3}]


def test_build_signals_with_other_committees(agrisea_db):
    from radar.assembly import Extracts
    ext = Extracts(meetings={
        "M2": {"date": "2025-11-05", "title": "중복", "committee": "농림축산식품해양수산위원회", "status": "pending"},
        "X1": {"date": "2025-11-20", "title": "산자중기위 회의", "committee": "산업통상자원중소벤처기업위원회",
               "status": "pending"},
    })
    # 회의록 서비스 DB에도 있는 회의(M2)가 발췌에도 들어 있어도 두 번 세지 않는다
    ext.add_parsed("M2", [{"speaker_name": "가위원", "speaker_role": "위원", "speaker_type": "member", "org": "",
                           "text": "스마트팜 질의입니까?", "is_question": True, "is_commitment": False}])
    ext.add_parsed("X1", [
        {"speaker_name": "마위원", "speaker_role": "위원", "speaker_type": "member", "org": "",
         "text": "스마트팜 창업기업 지원이 부족하지 않습니까? 다른 이야기입니다.", "is_question": True,
         "is_commitment": False},
        {"speaker_name": "바장관", "speaker_role": "중소벤처기업부장관", "speaker_type": "official", "org": "",
         "text": "푸드테크 스타트업 전용 보증을 내년에 도입하겠습니다.", "is_question": False, "is_commitment": True},
        {"speaker_name": "사위원", "speaker_role": "위원", "speaker_type": "member", "org": "",
         "text": "주제와 무관한 발언입니다.", "is_question": False, "is_commitment": False},
    ])
    assert len(ext.utterances) == 3 and ext.meetings["X1"]["utterances"] == 3
    x1 = [u for u in ext.utterances if u["meeting_id"] == "X1"]
    assert x1[0]["text"] == "스마트팜 창업기업 지원이 부족하지 않습니까?"   # 주제 문장만 발췌
    s = build_signals(agrisea_db, ext)
    assert s["source"]["meetings"] == 3 and s["source"]["committees"] == 2
    sf = s["topics"]["smartfarm"]
    assert sf["mentions"] == 4 and {c["name"] for c in sf["committees"]} == {
        "농림축산식품해양수산위원회", "산업통상자원중소벤처기업위원회"}
    assert s["monthly_total"]["2025-11"] == 2 + 3     # 농해수위 M2 발언 2(예시 제외) + 산자중기위 발언 3
    ft = s["topics"]["foodtech"]
    assert ft["commitments"][0]["committee"] == "산업통상자원중소벤처기업위원회"


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
