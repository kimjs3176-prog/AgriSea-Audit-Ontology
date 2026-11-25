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


AGENDA_TEXT = """제439회국회
의사일정
1. 농업협동조합법 일부개정법률안(박덕흠 의원 대표발의)(의안번호 2209214)
2. 농업·농촌 및 식품산업 기본법 일부개정법률안(서천호 의원 대표발의)(의안번호
100. 업무현황보고
상정된 안건
1. 농업협동조합법 일부개정법률안(박덕흠 의원 대표발의)(의안번호 2209214) ··············· 1 1
100. 업 무 현 황 보 고 ·········· 3
◯위원장 홍길동 의사일정 제1항을 상정합니다.
◯김가람 위원 농협법 질의입니다. 언제 개정합니까?
100. 업무현황보고
◯농림축산식품부장관 정마루 업무현황을 보고드리겠습니다.
"""


def test_agendas_dedupe_and_three_digit_numbers():
    """의사일정과 상정된 안건에 띄어쓰기·점선·쪽 번호만 달리 나오는 같은 안건은 한 번만, 100번 이상 안건도 인식한다."""
    from agrisea.parser import align_agendas
    doc = parse_minutes(AGENDA_TEXT)
    assert doc.agendas == ["농업협동조합법 일부개정법률안(박덕흠 의원 대표발의)(의안번호 2209214)",
                           "농업·농촌 및 식품산업 기본법 일부개정법률안(서천호 의원 대표발의)(의안번호",
                           "업무현황보고"]
    assert [u.agenda_idx for u in doc.utterances] == [0, 0, 2]
    # 회의 안건 = 회의록 순서, 표기는 짝이 맞는 API 문구(의안번호 또는 앞부분이 같은 제목)
    api = ["14. 농업·농촌 및 식품산업 기본법 일부개정법률안(서천호 의원 대표발의)(의안번호 2213533)",
           "1. 농업협동조합법 일부개정법률안(박덕흠 의원 대표발의)(의안번호 2209214)", "가. 농림축산식품부"]
    assert align_agendas(doc.agendas, api) == [
        "1. 농업협동조합법 일부개정법률안(박덕흠 의원 대표발의)(의안번호 2209214)",
        "2. 농업·농촌 및 식품산업 기본법 일부개정법률안(서천호 의원 대표발의)(의안번호 2213533)",
        "3. 업무현황보고"]


def test_eopseoyo_statement_is_not_a_question():
    """'없어요'로 끝나는 진술은 질의가 아니다(물음표가 있을 때만 질의)."""
    from agrisea.lexicon import QUESTION_PATTERNS
    assert not QUESTION_PATTERNS.search("가공시설이 하나도 없어요. 대책을 세워 주세요.")
    assert QUESTION_PATTERNS.search("그런 계획은 없어요?")
    assert QUESTION_PATTERNS.search("대책이 있습니까")


def test_commitment_variants_and_hedged_sentences():
    """'검토해 보겠습니다'·'확인해 보겠습니다'·'보고드리도록 하겠습니다'도 약속이고, '하겠습니다마는 …'처럼
    단서가 붙은 문장은 앞만 떼어 약속으로 보지 않는다."""
    from agrisea.lexicon import is_commitment
    assert is_commitment("예, 그 부분은 저희가 검토해 보겠습니다")
    assert is_commitment("그건 다시 한번 확인해 보겠습니다")
    assert is_commitment("대책이 마련되는 대로 결과를 위원님께 보고드리도록 하겠습니다")
    assert is_commitment("관계 부처하고 협의해 보도록 하겠습니다")
    assert not is_commitment("검토를 하겠습니다마는 예산 사정상 어렵습니다")
    assert not is_commitment("오늘 업무현황을 보고드리겠습니다")   # 업무보고 인사는 여전히 제외


BATCH_TEXT = """제439회국회
상정된 안건
1. 2027년도 농림축산식품부 소관 세입세출예산안
2. 2027년도 농림축산식품부 소관 농산물가격안정기금 운용계획안
3. 농수산물 유통 및 가격안정에 관한 법률 일부개정법률안(이원택 의원 대표발의)(의안번호 2200001)
4. 농수산물 유통 및 가격안정에 관한 법률 일부개정법률안(이병진 의원 대표발의)(의안번호 2200002)
◯위원장 홍길동 의사일정 제1항부터 제2항까지 일괄하여 상정합니다.
1. 2027년도 농림축산식품부 소관 세입세출예산안
2. 2027년도 농림축산식품부 소관 농산물가격안정기금 운용
계획안
(10시05분)
◯김가람 위원 예산 질의입니다. 언제 집행합니까?
◯위원장 홍길동 다음 안건입니다.
4. 농수산물 유통 및 가격안정에 관한 법률 일부개정법률안(이병진 의원 대표발의)(의안번호
2200002)
◯이나래 위원 농안법 질의입니다.
"""


def test_batch_agendas_grouped_and_same_prefix_titles():
    """한꺼번에 상정한 안건 줄(줄바꿈된 제목·시각 표기가 끼어도)은 묶음으로 '첫 안건 외 N건'이 붙고,
    앞부분이 같은 안건(같은 법률 개정안 여러 건)은 번호로 가린다."""
    from agrisea.parser import agenda_labels
    doc = parse_minutes(BATCH_TEXT)
    assert [u.agenda_idx for u in doc.utterances] == [0, 1, 1, 3]
    assert doc.agenda_groups == {1: [0, 1]}
    labels = agenda_labels(doc.agendas, doc.agenda_groups)
    assert labels[1] == "2027년도 농림축산식품부 소관 세입세출예산안 외 1건" and labels[3] == doc.agendas[3]
    # 안건 줄 사이에 발언 문장이 있으면 묶음이 아니다(차례로 처리한 안건)
    seq = parse_minutes(BATCH_TEXT.replace("\n2. 2027년도 농림축산식품부 소관 농산물", "\n가결되었습니다.\n2. 2027년도 농림축산식품부 소관 농산물"))
    assert seq.agenda_groups == {}


TAIL_TEXT = """제439회국회
상정된 안건
1. 농수산물 유통 및 가격안정에 관한 법률 일부개정법률안(이원택 의원 대표발의)(의안번호 2200001)
2. 2027년도 농림축산식품부 소관 세입세출예산안
◯위원장 홍길동 의사일정 제1항을 상정합니다.
1. 농수산물 유통 및 가격안정에 관한 법률 일부개정법률안(이원택 의원 대표발의)(의안번호
2200001)
2202041)······························································
◯김가람 위원 유통 구조 질의입니다. 언제 개선합니까?
◯위원장 홍길동 다음 안건입니다.
2. 2027년도 농림축산식품부 소관 세입세출예산안
가. 농림축산식품부 소관
나. 농촌진흥청 소관
다. 산림청 소관
- 마산항 재해 취약지구 정비사업
◯이나래 위원 예산 질의입니다.
◯위원장 홍길동 나머지 안건은 계속 심사하겠습니다.
의원 대표발의)(의안번호 2202635)
번호2205814)
다음 회의는 공지하겠습니다.
"""


def test_title_fragments_removed_from_utterance_text():
    """안건 줄 뒤에 붙은 제목 조각(줄바꿈된 제목 끝·의안번호 조각·아래 항목·목차 점선)은 발언 본문에서 빠지고,
    발언 문장은 그대로 남는다."""
    doc = parse_minutes(TAIL_TEXT)
    texts = [u.text for u in doc.utterances]
    assert texts[0] == "의사일정 제1항을 상정합니다."
    assert texts[2] == "다음 안건입니다."
    assert texts[4] == "나머지 안건은 계속 심사하겠습니다. 다음 회의는 공지하겠습니다."
    assert not any(w in t for t in texts for w in ("2200001)", "2202041)", "소관", "마산항", "의안번호", "번호2205814"))
    assert [u.agenda_idx for u in doc.utterances] == [0, 0, 0, 1, 1]


# 첫 쪽이 두 단이라 PDF에서 본문 첫 단이 먼저 나오고 표지(제목·일시·의사일정·상정된 안건)가 첫 발언 속에 끼어 나온 회의록
LATE_COVER_TEXT = """제426회-농림축산식품해양수산소위제1차(2025년6월26일)  1
(10시04분개의)
◯소위원장 홍길동 의석을 정돈해 주시기 바랍니다.
 그러면 의사일정 제1항부터 제2항까지 일괄하여 상정합니다.
1.2025년도제2회추가경정예산안(의안번호2210977)
가.농림축산식품부소관
제426회국회
(임시회)
농림축산식품해양수산위원회회의록
(예산결산심사소위원회)
제1호
일 시 2025년6월26일(목)
 의사일정
1.2025년도제2회추가경정예산안(의안번호2210977)
2.2025년도농산물가격안정기금운용계획변경안(의안번호2210990)
 상정된안건
1.2025년도제2회추가경정예산안(의안번호2210977)···························································1
2  제426회-농림축산식품해양수산소위제1차(2025년6월26일)
2.2025년도농산물가격안정기금운용계획변경안(의안번호2210990)
◯김가람 위원 추경 질의입니다. 언제 집행합니까?
"""


def test_late_cover_moved_to_preamble():
    """첫 발언 뒤에 끼어 나온 표지는 회의록 첫머리로 옮겨, 안건 목록은 표지에서 읽고 발언 본문에는 남지 않는다.
    표지 뒤 쪽 머리말 다음에 이어진 본문(안건 줄)은 원래 발언으로 돌아간다."""
    doc = parse_minutes(LATE_COVER_TEXT)
    assert doc.agendas == ["2025년도제2회추가경정예산안(의안번호2210977)",
                           "2025년도농산물가격안정기금운용계획변경안(의안번호2210990)"]
    first = doc.utterances[0].text
    assert first == "의석을 정돈해 주시기 바랍니다. 그러면 의사일정 제1항부터 제2항까지 일괄하여 상정합니다."
    assert doc.agenda_groups == {1: [0, 1]} and [u.agenda_idx for u in doc.utterances] == [0, 1]
    assert parse_minutes(TEXT).agendas == ["농림축산식품부에 대한 국정감사", "농촌진흥청에 대한 국정감사"]  # 보통 회의록은 그대로


def test_strip_bill_noise_removes_standalone_fragments_only():
    """저장된 본문의 의안 목록 조각(번호 붙은 의안 제목·의안번호 조각)은 지우고, 문장 속 인용·발언 문장은 둔다."""
    from agrisea.parser import strip_bill_noise
    t = ("그러면 안건 심사를 시작하겠습니다. 2205633) 의원대표발의)(의안번호2204871) 번호2205633) (11시05분) "
         "규정을 두고 22.마리나항만의 조성 및 관리 등에 관한 법률 일부개정법률안(박성훈 의원 대표발의)(의안번호 2206145) "
         "23.굴산업진흥 및 지원에 관한 법률안(정점식 의원 대표발의)(의안번호2208310)···38 있음을 고려할 필요가 있습니다.")
    assert strip_bill_noise(t) == ("그러면 안건 심사를 시작하겠습니다. (11시05분) 규정을 두고 있음을 고려할 필요가 있습니다.")
    quoted = "기자재 지원법안(서천호 의원 대표발의)(의안번호2214537)에 대한 공청회를 열겠습니다."
    assert strip_bill_noise(quoted) == quoted
    assert strip_bill_noise("의안번호가 없는 보통 발언입니다.") == "의안번호가 없는 보통 발언입니다."


def test_strip_bill_fragments_fixes_stored_text():
    from agrisea.parser import MinutesDoc, Utterance
    from agrisea.precompute import strip_bill_fragments
    from agrisea.store import Store
    s = Store(":memory:")
    s.upsert_meeting({"meeting_id": "B1", "title": "제439회", "date": "2026-11-01", "session_no": 439})
    s.save_minutes("B1", MinutesDoc(utterances=[
        Utterance(0, "홍길동", "위원장", "chair", None, "계속심사하겠습니다. 의원대표발의)(의안번호2214898) 번호2205633)"),
        Utterance(1, "김가람", "위원", "member", None, "질의입니다.")]))
    assert strip_bill_fragments(s) == 1
    assert s.utterances(meeting_id="B1")[0]["text"] == "계속심사하겠습니다."
    assert strip_bill_fragments(s) == 0


def test_private_association_head_and_committee_office_are_not_officials():
    """소관 기관 목록에 없는 협회·연합회·학회의 장(공청회 진술 단체 대표)은 진술인·참고인으로, 위원회 행정실장은
    국회 직원으로 본다. 소관 기관인 협회(한국해양조사협회)의 장과 부처 실장은 그대로 정부·기관 관계자."""
    from agrisea.lexicon import classify_role
    assert classify_role("전국한우협회장") == "reference"
    assert classify_role("행정실장") == "staff"
    assert classify_role("한국해양조사협회장") == "official"
    assert classify_role("기획조정실장") == "official"
