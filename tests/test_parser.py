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
