from agrisea.parser import parse_minutes, parse_speaker

TEXT = """제418회국회
의사일정
1. 농림축산식품부에 대한 국정감사
2. 농촌진흥청에 대한 국정감사 ········ 12
(10시00분 개의)
◯위원장 홍길동 감사를 시작하겠습니다.
◯김가람 위원 장관님, 쌀값 대책이 무엇입니까? 관련 자료를 제출해 주십시오.
◯농림축산식품부장관 정마루 쌀 수급 대책을 검토하겠습니다.
3
2. 농촌진흥청에 대한 국정감사
◯위원장 홍길동 다음 기관입니다.
◯이나래위원 농촌진흥청장님, 스마트팜 R&D 성과는요?
◯농촌진흥청장 윤지후 성과를 말씀드리겠습니다. 스마트팜 보급이 늘었습니다.
(12시00분 산회)
"""


def test_parse_speaker_variants():
    assert parse_speaker("위원장 홍길동 성원이")[:2] == ("위원장", "홍길동")
    assert parse_speaker("김가람 위원 장관님")[:2] == ("위원", "김가람")
    assert parse_speaker("이나래위원 장관님")[:2] == ("위원", "이나래")
    assert parse_speaker("해양수산부장관 문채원 답변")[:2] == ("해양수산부장관", "문채원")
    assert parse_speaker("증인 홍길순 네")[:2] == ("증인", "홍길순")


def test_parse_minutes_structure():
    doc = parse_minutes(TEXT)
    assert doc.agendas == ["농림축산식품부에 대한 국정감사", "농촌진흥청에 대한 국정감사"]
    assert [u.speaker_type for u in doc.utterances] == [
        "chair", "member", "official", "chair", "member", "official"]
    q, a = doc.utterances[1], doc.utterances[2]
    assert q.is_question and q.is_data_request
    assert a.is_commitment and a.org == "농림축산식품부"
    # 안건 전환 인식
    assert [u.agenda_idx for u in doc.utterances] == [0, 0, 0, 1, 1, 1]
    # 의례적 표현('말씀드리겠습니다')은 이행약속이 아님
    assert not doc.utterances[5].is_commitment
    assert doc.utterances[5].org == "농촌진흥청"
    # 페이지 번호/시간 표기 제거
    assert "3" not in a.text.split() and "산회" not in doc.utterances[-1].text


def test_clean_utterance_text_drops_pdf_glyph_residue():
    from agrisea.parser import clean_utterance_text
    raw = "지원 부탁드립니다. ਘ\x13\x11ੌ \x01 \x01 \x14 감사합니다. 민간단체에 ਘ\x13\x11ੌ 대한 지원\x07"
    assert clean_utterance_text(raw) == "지원 부탁드립니다. 감사합니다. 민간단체에 대한 지원"


# 회의록 끝 부록(산회 뒤 명단): 마지막 위원장 발언에 붙지 않고, 부록 속 '◯출석 전문위원'도 발언자가 되지 않는다
APPENDIX_TEXT = """제439회국회
(10시00분 개의)
◯위원장 홍길동 회의를 시작하겠습니다.
◯김가람 위원 청장님, 산불 피해 보상은 언제 됩니까?
◯산림청장 정마루 이달 안에 보상 계획을 마련하겠습니다.
◯위원장 홍길동 이상으로 오늘 회의를 마치겠습니다. 산 회를 선포합니다.
(12시05분 산회)
일반증인 및 참고인 명단
증인(1인) 성명 직업 출석일 관련 피감기관 신문요지 이산림 산림조합중앙회 회장
출석 위원(18인)
김가람 홍길동
◯출석 전문위원
수석전문위원 최용훈
정부측 및 기타 참석자
산림청 청장 정마루 농업협동조합중앙회 회장 강호동
【보고사항】
의안 회부 가축전염병 예방법 일부개정법률안
"""


def test_parse_minutes_drops_attendance_appendix():
    doc = parse_minutes(APPENDIX_TEXT)
    assert [u.speaker_type for u in doc.utterances] == ["chair", "member", "official", "chair"]
    last = doc.utterances[-1].text
    assert last.endswith("산 회를 선포합니다.")
    assert not any(w in u.text for u in doc.utterances for w in ("출석", "산림조합중앙회", "가축전염병", "최용훈"))


def test_split_appendix_cut_points():
    from agrisea.parser import split_appendix
    # 산회 선포와 명단 사이에 표만 있으면 선포 바로 뒤에서 자른다
    body, appx = split_appendix("마치겠습니다. 산회를 선포합니다. 소위원회 구성 명단 ◎표시는 소위원장 임 출석위원(9인) 가 나")
    assert body == "마치겠습니다. 산회를 선포합니다." and appx.startswith("소위원회 구성 명단")
    # 정회 뒤 계속 개의되지 않은 회의는 그 표시까지 본문
    body, _ = split_appendix("정회를 선포합니다. (계속 개의되지 않았음) 출석위원(11인) 가 나")
    assert body == "정회를 선포합니다. (계속 개의되지 않았음)"
    # PDF 원문처럼 사이에 시각 표기가 끼어 있어도 같다(발언으로 나눈 뒤 시각 표기만 지워진다)
    raw = "◯소위원장 이원택 정회를선포합니다.\n(11시40분회의중지)\n(계속개의되지않았음)\n◯출석 위원(11인)\n강명구"
    assert parse_minutes(raw).utterances[-1].text == "정회를선포합니다. (계속개의되지않았음)"
    # 중간 정회 뒤 발언이 이어졌으면 그 정회에서 자르지 않고 명단 앞에서 자른다
    body, _ = split_appendix("정회를 선포합니다. 다시 질의하겠습니다. 끝 출석위원(3인) 가 나")
    assert body == "정회를 선포합니다. 다시 질의하겠습니다. 끝"
    # 부록 자리 뒤에 발언 문장이 있으면(회의가 끝나지 않음) 자르지 않는다
    text = "출석위원(3인) 가 나 다 위원장 다시 시작하겠습니다."
    assert split_appendix(text) == (text, "")
    assert split_appendix("부록 없는 발언입니다.") == ("부록 없는 발언입니다.", "")


def test_strip_appendix_fixes_stored_minutes():
    """이미 저장된 회의록: 부록이 붙은 마지막 발언은 본문만 남기고, 부록에서 갈라진 가짜 발언자 행은 지운다."""
    from agrisea.parser import MinutesDoc, Utterance
    from agrisea.precompute import strip_appendix
    from agrisea.store import Store
    s = Store(":memory:")
    for mid in ("M1", "M2"):
        s.upsert_meeting({"meeting_id": mid, "title": f"제439회 {mid} 농림축산식품해양수산위원회",
                          "date": "2026-10-01", "session_no": 439, "class_name": "상임위원회"})
    s.save_minutes("M1", MinutesDoc(utterances=[
        Utterance(0, "김가람", "위원", "member", None, "청장님, 산불 피해 보상은 언제 됩니까?"),
        Utterance(1, "홍길동", "위원장", "chair", None,
                  "오늘 회의를 마치겠습니다. 산회를 선포합니다. 출석위원(2인) 김가람 홍길동 "
                  "정부측 및 기타 참석자 산림조합중앙회 회장 최창호"),
        Utterance(2, "전문위원", "출석전문위원", "staff", None, "수석전문위원 최용훈 산림조합중앙회"),
    ]))
    keep = "출석위원(3인) 명단을 확인하겠습니다. 다음 안건입니다."   # 명단 뒤에 발언이 이어짐 → 그대로
    s.save_minutes("M2", MinutesDoc(utterances=[Utterance(0, "홍길동", "위원장", "chair", None, keep)]))
    assert strip_appendix(s) == 2
    m1 = s.utterances(meeting_id="M1")
    assert [u["idx"] for u in m1] == [0, 1] and m1[-1]["text"] == "오늘 회의를 마치겠습니다. 산회를 선포합니다."
    assert s.utterances(meeting_id="M2")[0]["text"] == keep
    assert not s.search("산림조합중앙회")   # 전문검색 색인에서도 빠진다
    assert strip_appendix(s) == 0           # 다시 돌려도 바뀌지 않는다
