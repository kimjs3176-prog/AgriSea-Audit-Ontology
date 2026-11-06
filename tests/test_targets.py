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
    # 정부에 던진 질의가 위원장의 시간 안내로 끊긴 경우는 회의 진행 질의가 아니다
    assert question_kind("급한 불을 꺼야 하지 않겠습니까?", ["chair"]) is None
    # 정부 직함을 부르거나 정부가 답했으면 회의 진행·진술인 질의가 아니다
    assert question_kind("장관, 1분만 더 답해 보시겠어요?", ["chair"]) is None
    assert question_kind("몇 페이지요?", ["official", "reference"]) is None
    # 긴 정부 질의 끝의 '1분만 더'는 진행 표현으로 보지 않는다
    assert question_kind("폐업지원금 " * 40 + "1분만 더 주시겠습니까?", ["chair"]) is None


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
        Utterance(2, "김위원", "위원", "member", None, "그게 다입니까?"),
        Utterance(3, "어위원", "위원장", "chair", None, "시간이 다 됐습니다."),
        Utterance(4, "김위원", "위원", "member", None, "해양수산부 예산은 얼마입니까?"),
        Utterance(5, "강도형", "해양수산부장관", "official", "해양수산부", "자료로 드리겠습니다."),
    ]))
    reclassify(s)
    g = build_graph(s)
    sea = org_uri("해양수산부")
    assert set(g.objects(utt_uri("M1", 2), AG.addressedTo)) == set()      # 확정 관계는 비워 둔다
    assert set(g.objects(utt_uri("M1", 2), AG.likelyAddressedTo)) == {sea}
