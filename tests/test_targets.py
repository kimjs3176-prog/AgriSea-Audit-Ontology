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
