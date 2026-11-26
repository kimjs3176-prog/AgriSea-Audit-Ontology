"""질의 대상 기관 식별: 사람 단위 소속, 연달은 질의, 직함 호칭."""
from agrisea.ontology import AG, build_graph, meeting_title_orgs, org_uri, titled_target, utt_uri
from agrisea.parser import MinutesDoc, Utterance
from agrisea.precompute import reclassify
from agrisea.store import Store


def _store() -> Store:
    s = Store(":memory:")
    for mid, date in (("M1", "2026-09-01"), ("M2", "2026-10-01")):
        s.upsert_meeting({"meeting_id": mid, "title": f"제439회 {mid} 농림축산식품해양수산위원회",
                          "date": date, "session_no": 439, "class_name": "상임위원회"})
    s.save_minutes("M1", MinutesDoc(utterances=[
        Utterance(0, "강도형", "해양수산부장관", "official", "해양수산부", "업무 현황을 보고드리겠습니다."),
        Utterance(1, "김위원", "위원", "member", None, "장관님, 어민 지원 대책은 언제 나옵니까?"),
        Utterance(2, "어위원", "위원장", "chair", None, "다음은 박위원님 질의하시겠습니다."),
    ]))
    s.save_minutes("M2", MinutesDoc(utterances=[
        Utterance(0, "박위원", "위원", "member", None, "첫째, 피해 규모는 얼마입니까?"),
        Utterance(1, "박위원", "위원", "member", None, "둘째, 보상은 언제 합니까?"),
        Utterance(2, "강도형", "증인", "witness", None, "피해 규모는 집계 중이고 보상 계획을 이달 중 마련하겠습니다."),
    ]))
    reclassify(s)
    return s


def test_witness_gets_unique_affiliation_from_other_meetings():
    s = _store()
    u = next(x for x in s.utterances(meeting_id="M2") if x["speaker_name"] == "강도형")
    assert u["speaker_type"] == "witness" and u["org"] == "해양수산부"


def test_question_targets_from_runs_and_titles():
    s = _store()
    g = build_graph(s)
    target = lambda mid, i: set(g.objects(utt_uri(mid, i), AG.addressedTo))  # noqa: E731
    sea = {org_uri("해양수산부")}
    # 같은 위원이 연달아 한 두 질의는 한 번에 받은 답변(증인으로 나온 장관)의 기관을 함께 받는다
    assert target("M2", 0) == sea and target("M2", 1) == sea
    # 답변이 없어도 '장관님'으로 불렀고 그 회의에 장관으로 출석한 기관이 하나뿐이면 그 기관
    assert target("M1", 1) == sea


def test_titled_target_rules():
    title_orgs = meeting_title_orgs([
        {"speaker_type": "official", "speaker_role": "농림축산식품부장관", "org": "농림축산식품부"},
        {"speaker_type": "official", "speaker_role": "해양수산부차관", "org": "해양수산부"},
        {"speaker_type": "official", "speaker_role": "해양경찰청장 직무대행", "org": "해양경찰청"},
    ])
    assert titled_target("장관님, 답변해 주십시오.", title_orgs) == "농림축산식품부"
    assert titled_target("청장님 생각은 어떠십니까?", title_orgs) == "해양경찰청"
    assert titled_target("위원장님, 1분만 더 주시겠어요?", title_orgs) is None   # '위원장'의 '원장'은 직함이 아님
    assert titled_target("장관님과 차관님 모두 답해 주세요.", title_orgs) is None  # 기관이 둘이면 정하지 않음


def test_question_kind_rules():
    from agrisea.ontology import question_kind
    assert question_kind("어떤 부분을 고쳐야 한다는 말씀이세요?", ["reference"]) == "expert"
    assert question_kind("3호에는 지역이 없지 않습니까?", ["staff", "chair"]) == "procedural"
    assert question_kind("위원장님, 1분만 더 주시겠어요?", ["chair"]) == "procedural"
    assert question_kind("몇 페이지요?", ["member"]) == "procedural"
    assert question_kind("1분도 안 주시렵니까? 마저 좀 마무리 하겠습니다.", ["chair"]) == "procedural"
    assert question_kind("마무리 하시지요. 6시 전에도 안 끝난다니까요.", ["member"]) == "procedural"
    assert question_kind("그 사업 마무리는 언제 됩니까?", ["chair"]) is None   # 사업의 마무리를 묻는 정부 질의
    # 정부에 던진 질의가 위원장의 시간 안내로 끊긴 경우는 회의 진행 질의가 아니다
    assert question_kind("급한 불을 꺼야 하지 않겠습니까?", ["chair"]) is None
    # 정부 직함을 부르거나 정부가 답했으면 회의 진행·진술인 질의가 아니다
    assert question_kind("장관, 1분만 더 답해 보시겠어요?", ["chair"]) is None
    assert question_kind("몇 페이지요?", ["official", "reference"]) is None
    # 긴 정부 질의 끝의 '1분만 더'는 진행 표현으로 보지 않는다
    assert question_kind("폐업지원금 " * 40 + "1분만 더 주시겠습니까?", ["chair"]) is None
    # 위원·위원장만 받아 이어 간 질의는 위원 간 논의 질의
    assert question_kind("토지를 제한한다는 것하고?", ["chair", "member"], "예.") == "deliberation"
    assert question_kind("관계없어요, 크게?", ["chair"], "국외 공급망하고 국내 공급망이 다 포함됩니다.") == "deliberation"
    # 위원장이 끊거나 다음 위원에게 차례를 넘겼으면 정부 질의가 끊긴 것이라 논의 질의가 아니다
    assert question_kind("급한 불을 꺼야 하지 않겠습니까?", ["chair"], "수고하셨습니다. 다음은 박위원님 질의하시겠습니다.") is None
    assert question_kind("왜 그런 정책을 폅니까?", ["chair", "member"], "그만 하세요. 그만 하세요.") is None
    assert question_kind("그 대책은 언제 나옵니까?", ["chair"], "임호선 위원님.") is None
    assert question_kind("장관님 생각은 어떠세요?", ["member"], "저는 반대합니다.") is None
    # 아무도 받지 않았어도 다른 위원의 의견(질의 아닌 발언)에 이어 던진 질의는 위원 간 논의 질의
    assert question_kind("어떤 것을 수용하는 거예요?", [], "", ("member", "이것은 일부는 수용하고……", False)) == "deliberation"
    # 위원장의 말(시간 안내·차례 넘김 등 진행 발언이 많다)이나 다른 위원의 질의 뒤는 논의로 보지 않는다
    assert question_kind("급한 불을 꺼야 하지 않겠습니까?", [], "", ("chair", "1분 드리겠습니다.", False)) is None
    assert question_kind("대책은 언제 나옵니까?", [], "",
                         ("chair", "다음은 김선교 위원님 질의해 주시기 바랍니다.", False)) is None
    assert question_kind("대책은 언제 나옵니까?", [], "", ("member", "예산은 얼마입니까?", True)) is None


def test_non_agency_questions_are_classified_and_excluded_from_check():
    from rdflib import RDF

    from agrisea.ontology import AGD, build_kg
    from agrisea.validate import validate
    s = _store()
    s.save_minutes("M1", MinutesDoc(utterances=[
        Utterance(0, "강도형", "해양수산부장관", "official", "해양수산부", "업무 현황을 보고드리겠습니다."),
        Utterance(1, "김위원", "위원", "member", None, "어떤 부분을 고쳐야 한다는 말씀이세요?"),
        Utterance(2, "이교수", "진술인", "reference", None, "제3조 범위가 넓습니다."),
        Utterance(3, "김위원", "위원", "member", None, "위원장님, 1분만 더 주시겠어요?"),
        Utterance(4, "어위원", "위원장", "chair", None, "1분 드리겠습니다."),
        Utterance(5, "김위원", "위원", "member", None, "급한 불을 꺼야 하지 않겠습니까?"),
        Utterance(6, "어위원", "위원장", "chair", None, "시간이 다 됐습니다."),
    ]))
    reclassify(s)
    g = build_graph(s)
    types = lambda i: set(g.objects(utt_uri("M1", i), RDF.type))  # noqa: E731
    assert AG.ExpertQuestion in types(1)
    assert set(g.objects(utt_uri("M1", 1), AG.addressedToPerson)) == {AGD["person/이교수"]}
    assert AG.ProceduralQuestion in types(3)
    assert not types(5) & {AG.ExpertQuestion, AG.ProceduralQuestion}   # 기관 대상 질의로 남는다
    report = validate(build_kg(s)[0])
    check = next(c for c in report["checks"] if c["title"].startswith("질의는 어느 기관을 향한다"))
    assert check["count"] == 1 and report["counts"]["error"] == 0   # 끊긴 정부 질의 하나만 남는다


def test_deliberation_question_links_to_replying_member():
    from rdflib import RDF

    from agrisea.ontology import AGD, build_kg
    from agrisea.validate import validate
    s = _store()
    s.save_minutes("M1", MinutesDoc(utterances=[
        Utterance(0, "강도형", "해양수산부장관", "official", "해양수산부", "업무 현황을 보고드리겠습니다."),
        Utterance(1, "김위원", "위원", "member", None, "이 조항은 의무로 둬야 하지 않습니까?"),
        Utterance(2, "박위원", "위원", "member", None, "재량으로 두는 게 맞다고 봅니다."),
        Utterance(3, "김위원", "위원", "member", None, "그러면 단서를 달까요?"),
        Utterance(4, "김위원", "위원", "member", None, "제 생각은 그렇습니다."),
    ]))
    reclassify(s)
    g = build_graph(s)
    types = lambda i: set(g.objects(utt_uri("M1", i), RDF.type))  # noqa: E731
    assert AG.DeliberationQuestion in types(1)
    assert set(g.objects(utt_uri("M1", 1), AG.addressedToPerson)) == {AGD["person/member/박위원"]}
    # 질의한 위원이 혼자 이어 말한 것은 응답이 아니지만, 이 질의는 박위원의 의견에 이어 던진 것이라 논의 질의다
    assert AG.DeliberationQuestion in types(3)
    assert set(g.objects(utt_uri("M1", 3), AG.addressedToPerson)) == {AGD["person/member/박위원"]}
    report = validate(build_kg(s)[0])
    check = next(c for c in report["checks"] if c["title"].startswith("질의는 어느 기관을 향한다"))
    assert check["count"] == 0 and report["counts"]["error"] == 0


def test_unanswered_question_after_official_targets_that_agency():
    """아무도 받지 않은 질의가 정부 관계자의 말 바로 뒤에 나왔으면 그 관계자의 기관을 향한 질의다."""
    s = _store()
    s.save_minutes("M1", MinutesDoc(utterances=[
        Utterance(0, "김위원", "위원", "member", None, "어민 지원 예산은 얼마입니까?"),
        Utterance(1, "강도형", "해양수산부장관", "official", "해양수산부", "의무 물량이 있어 냅니다."),
        Utterance(2, "김위원", "위원", "member", None, "그게 10만 톤인가 그렇지 않나요?"),
        Utterance(3, "박위원", "위원", "member", None, "얼마 내요, 양이?"),
        Utterance(4, "강도형", "해양수산부장관", "official", "해양수산부", "저희가 더 노력하겠습니다."),
        Utterance(5, "박위원", "위원", "member", None, "이걸 추경이라고 내놓습니까?"),
        Utterance(6, "어위원", "위원장", "chair", None, "수고하셨습니다. 다음은 이위원님 질의해 주시기 바랍니다."),
    ]))
    reclassify(s)
    g = build_graph(s)
    sea = {org_uri("해양수산부")}
    assert set(g.objects(utt_uri("M1", 2), AG.addressedTo)) == sea
    assert set(g.objects(utt_uri("M1", 5), AG.addressedTo)) == sea   # 위원장이 차례를 넘겨 끊긴 질의
    s.save_minutes("M1", MinutesDoc(utterances=[
        Utterance(0, "강도형", "해양수산부장관", "official", "해양수산부", "결의안 내용을……"),
        Utterance(1, "김위원", "위원", "member", None, "위원장님, 이 내용은 조정해 주시겠습니까?"),
        Utterance(2, "어위원", "위원장", "chair", None, "이위원님."),
    ]))
    reclassify(s)
    g = build_graph(s)   # 위원장을 부른 질의는 앞 관계자의 기관이 아니라 회의 진행 질의
    assert set(g.objects(utt_uri("M1", 1), AG.addressedTo)) == set()


def test_turn_estimates_needs_two_answered_questions_to_one_agency():
    from agrisea.ontology import turn_estimates
    sea, agri = {"해양수산부"}, {"농림축산식품부"}
    got = turn_estimates([
        ("q1", "김", sea), ("q2", "김", set()), ("q3", "김", sea),   # 김 위원 차례: 해수부 둘 → q2 추정
        ("q4", "박", agri), ("q5", "박", set()),                     # 박 위원 차례: 기관 찾은 질의가 하나뿐 → 추정 안 함
        ("q6", "이", sea), ("q7", "이", agri), ("q8", "이", set()),  # 이 위원 차례: 기관이 둘 → 추정 안 함
        ("q9", "최", sea), ("q10", "최", None), ("q11", "최", sea),  # 회의 진행 질의(None)에는 주지 않음
    ])
    assert got == {"q2": "해양수산부"}


def test_estimated_target_is_separate_from_confirmed_target():
    s = _store()
    s.save_minutes("M1", MinutesDoc(utterances=[
        Utterance(0, "김위원", "위원", "member", None, "해양수산부는 어민 지원을 언제 합니까?"),
        Utterance(1, "강도형", "해양수산부장관", "official", "해양수산부", "이달 중 검토하겠습니다."),
        Utterance(2, "어위원", "위원장", "chair", None, "답변은 짧게 해 주시기 바랍니다."),
        Utterance(3, "김위원", "위원", "member", None, "그게 다입니까?"),
        Utterance(4, "어위원", "위원장", "chair", None, "시간이 다 됐습니다."),
        Utterance(5, "김위원", "위원", "member", None, "해양수산부 예산은 얼마입니까?"),
        Utterance(6, "강도형", "해양수산부장관", "official", "해양수산부", "자료로 드리겠습니다."),
    ]))
    reclassify(s)
    g = build_graph(s)
    sea = org_uri("해양수산부")
    assert set(g.objects(utt_uri("M1", 3), AG.addressedTo)) == set()      # 확정 관계는 비워 둔다
    assert set(g.objects(utt_uri("M1", 3), AG.likelyAddressedTo)) == {sea}


def test_agenda_batch_linked_in_graph():
    """한꺼번에 상정한 안건 묶음: 묶음의 다른 안건은 대표 안건(묶음 끝)에 ag:jointlyTakenUpWith 로 이어지고,
    검증 오류(스키마에 없는 용어·정의역·치역)가 없다."""
    from agrisea.ontology import AGD, build_graph, build_kg
    from agrisea.parser import MinutesDoc, Utterance
    from agrisea.store import Store
    from agrisea.validate import validate
    s = Store(":memory:")
    s.upsert_meeting({"meeting_id": "J1", "title": "제439회 예산결산", "date": "2026-11-01", "session_no": 439})
    s.save_minutes("J1", MinutesDoc(agendas=["2027년도 예산안", "2027년도 기금운용계획안", "농지법 일부개정법률안"],
                                    agenda_groups={2: [0, 1, 2]},
                                    utterances=[Utterance(0, "김가람", "위원", "member", None, "예산 질의입니다.", 2)]))
    g = build_graph(s)
    a = lambda i: AGD[f"agenda/J1/{i}"]  # noqa: E731
    assert set(g.subjects(AG.jointlyTakenUpWith, a(2))) == {a(0), a(1)}
    assert (a(2), AG.jointlyTakenUpWith, a(2)) not in g
    assert validate(build_kg(s)[0])["counts"]["error"] == 0


def test_commitment_before_any_question_is_not_an_answer():
    """질의가 나오기 전 업무보고·인사말 속 약속은 약속(Commitment)일 뿐 답변(Answer)이 아니고, 질의 뒤의 약속은
    답변이기도 하며 그 질의를 가리킨다. 검증의 '답변은 어느 질의에 대한 답변이다' 경고가 생기지 않는다."""
    from rdflib import RDF

    from agrisea.ontology import build_kg
    from agrisea.validate import validate
    s = Store(":memory:")
    s.upsert_meeting({"meeting_id": "C1", "title": "제439회 업무보고", "date": "2026-11-01", "session_no": 439})
    s.save_minutes("C1", MinutesDoc(utterances=[
        Utterance(0, "정마루", "농림축산식품부장관", "official", "농림축산식품부", "쌀값 안정 대책을 추진하겠습니다.",
                  is_commitment=True),
        Utterance(1, "김가람", "위원", "member", None, "쌀값 대책은 언제 나옵니까?", is_question=True),
        Utterance(2, "정마루", "농림축산식품부장관", "official", "농림축산식품부", "이달 안에 마련하겠습니다.",
                  is_commitment=True)]))
    g = build_graph(s)
    types = lambda i: set(g.objects(utt_uri("C1", i), RDF.type))  # noqa: E731
    assert AG.Commitment in types(0) and AG.Answer not in types(0)
    assert {AG.Commitment, AG.Answer} <= types(2)
    assert set(g.objects(utt_uri("C1", 2), AG.respondsTo)) == {utt_uri("C1", 1)}
    report = validate(build_kg(s)[0])
    check = next(c for c in report["checks"] if c["title"].startswith("답변은 어느 질의"))
    assert check["count"] == 0 and report["counts"]["error"] == 0
