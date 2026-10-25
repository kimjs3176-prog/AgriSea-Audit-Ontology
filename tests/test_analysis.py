from agrisea.analysis import briefing, briefing_markdown, issue_overview, summarize_meeting
from agrisea.nlp import extractive_summary, tokenize


def test_tokenize_strips_josa_and_keeps_domain_terms():
    toks = tokenize("쌀값이 떨어지고 수산물 소비가 위축되었습니다")
    assert "쌀값" in toks and "수산물 소비" in toks


def test_extractive_summary_keeps_order():
    text = "첫 문장은 인사입니다. 쌀값 폭락으로 농가소득이 20% 줄었습니다. 날씨가 좋습니다. 양곡관리법 개정이 필요합니다."
    out = extractive_summary(text, 2)
    assert out == ["쌀값 폭락으로 농가소득이 20% 줄었습니다.", "양곡관리법 개정이 필요합니다."]


def test_store_search(store):
    assert store.search("시장격리")  # FTS(3글자 이상)
    assert store.search("쌀값")  # LIKE(2글자)
    assert all("오염수" in r["text"] for r in store.search("오염수"))
    assert store.search("존재하지않는단어") == []


def test_summarize_meeting(store):
    s = summarize_meeting(store, "SAMPLE-2025-1024")
    assert s["key_issues"][0]["label"] == "스마트농업·R&D"
    assert len(s["agenda_summaries"]) == 3
    assert s["agenda_summaries"][1]["speakers"][0] == "정하늘"
    assert s["commitments"] and s["qa_highlights"]


def test_briefing_by_org_and_issue(store):
    b = briefing(store, org="한국농업기술진흥원")
    assert b["counts"]["commitments"] == 2
    assert {m["name"] for m in b["active_members"]} == {"정하늘"}
    b2 = briefing(store, issue="sea")
    assert all(m["date"] == "2025-10-20" for m in b2["meetings"])
    md = briefing_markdown(b2)
    assert "이행약속 추적표" in md and "해양수산부장관" in md


def test_issue_overview_rolls_up(store):
    ov = {i["id"]: i for i in issue_overview(store)}
    assert ov["sea"]["count"] >= ov["fishery"]["count"] > 0


def test_role_classification_and_commitments():
    from agrisea.lexicon import classify_role, commitment_sentences, match_issues
    assert classify_role("소위원장") == "chair" and classify_role("농촌진흥청장") == "official"
    assert classify_role("진술인") == "reference" and classify_role("농협손해보험대표이사") == "official"
    assert commitment_sentences("추가 시장격리 여부를 이달 중 검토하겠습니다.")
    assert not commitment_sentences("바쁘신 일정 중에도 의결해 주셔서 깊이 감사드립니다. 최선을 다하겠습니다.")
    assert not commitment_sentences("어촌어항재생과장 보고드리겠습니다.")
    # 기관·위원회명 속 키워드는 쟁점으로 세지 않음
    assert "livestock" not in match_issues("농림축산식품부장관님께 묻겠습니다")
    assert "livestock" in match_issues("축산 농가 방역 대책")


def test_reclassify_fixes_stored_rows(store):
    from agrisea.precompute import reclassify
    with store.tx() as c:
        c.execute("UPDATE utterances SET speaker_type='official', "
                  "text=text || ' 16 제430회-농림축산식품해양수산제1차(2025년10월14일)' "
                  "WHERE speaker_role='위원장'")
    assert reclassify(store) > 0
    chair = [u for u in store.utterances() if u["speaker_role"] == "위원장"]
    assert chair and all(u["speaker_type"] == "chair" and "제430회-" not in u["text"] for u in chair)


def test_running_header_removed():
    from agrisea.parser import clean_utterance_text
    assert clean_utterance_text("들어온 다음에 16 제439회-농림축산식품해양수산제2차(2026년9월17일) 그동안") \
        == "들어온 다음에 그동안"


def test_summary_skips_bill_lists_and_short_replies():
    from agrisea.nlp import extractive_summary
    bills = " ".join(f"{d}일 회부됨 농지법 일부개정법률안 (2026.8.{d}.김종양 의원 대표발의)(의안번호22205{d:02d})"
                     for d in range(1, 12))
    text = (f"쌀값 하락에 대한 정부 대책을 묻습니다. {bills}. 3번 사업과 같은 내용입니다. "
            "예, 맞습니다. 수확기 쌀값 안정을 위해 정부가 시장격리 물량을 추가로 검토하겠습니다.")
    out = extractive_summary(text, 3)
    assert out and not any("의안번호" in s or s.startswith(("3번", "예,")) for s in out)


def test_role_qualifier_moves_to_role():
    from agrisea.parser import fix_role_qualifier
    assert fix_role_qualifier("해양경찰청장", "직무대", "행장인식 존경하는 위원장님") == \
        ("해양경찰청장 직무대행", "장인식", "존경하는 위원장님")
    assert fix_role_qualifier("해양수산부장관", "후보자", "황종우 위원님 말씀 주신 대로") == \
        ("해양수산부장관 후보자", "황종우", "위원님 말씀 주신 대로")
    assert fix_role_qualifier("해양경찰청장", "김용진", "예, 동의합니다.")[1] == "김용진"


def test_agenda_headline_and_specific_promise():
    from agrisea.analysis import _specific_promise, agenda_headline
    assert agenda_headline(["1. 2025회계연도 결산(의안번호 2219079)", "가. 농림축산식품부 소관",
                            "2. 동물보호법 일부개정법률안(신영대 의원 대표발의)(의안번호 2200730)"]) \
        == "2025회계연도 결산 외 1건"
    assert not _specific_promise("지적하신 취지에는 공감하고 시정하겠습니다")
    assert _specific_promise("수확기 쌀값 안정을 위해 시장격리 물량을 추가 검토하겠습니다")


def test_search_prefers_substantive_speakers(store):
    rows = store.search("쌀값", limit=3)
    assert rows and rows[0]["speaker_type"] != "chair"


def test_key_point_items_carry_speaker(store):
    from agrisea.analysis import summarize_meeting
    s = summarize_meeting(store, "SAMPLE-2025-1014")
    assert s["key_point_items"] and all(p["speaker"] for p in s["key_point_items"])
    assert set(s["composition"]) == {"member", "gov", "chair", "other"}


def test_search_ignores_spacing_and_treats_wildcards_literally():
    from agrisea.pipeline import load_sample
    from agrisea.store import Store
    s = Store(":memory:", fts=False)  # 배포 환경과 같은 LIKE 검색
    load_sample(s)
    row = s.search("쌀값")[0]
    s.conn.execute("UPDATE utterances SET text=? WHERE id=?", ("가나다 라마바사 점검을 100% 완료_했습니다", row["id"]))
    assert [r["id"] for r in s.search("가나다라마바사")] == [row["id"]]  # PDF 띄어쓰기 복원 차이 무시
    assert all("100%" in r["text"] for r in s.search("100%"))            # %는 글자 그대로
    assert [r["id"] for r in s.search("료_했")] == [row["id"]]             # _도 글자 그대로
    assert s.search("\x00") == [] and s.search("%") == [r for r in s.search("%") if "%" in r["text"]]
    st = s.search_stats("쌀값")
    assert st["total"] == len(s.search("쌀값", limit=500)) and sum(st["by_session"].values()) <= st["total"]


def test_briefing_unknown_issue_and_org_commitments(store):
    import pytest
    with pytest.raises(KeyError):
        briefing(store, issue="없는쟁점")
    b = briefing(store, org="해양수산부")
    assert b["commitments"] and all(c["org"] == "해양수산부" for c in b["commitments"])
