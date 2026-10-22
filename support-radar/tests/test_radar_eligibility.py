from datetime import date, timedelta

from radar.eligibility import Profile, amount_won, evaluate, match


def founded(years: float) -> str:
    return (date.today() - timedelta(days=int(years * 365.25))).isoformat()


def items(ev):
    return {c["item"]: c["result"] for c in ev["checks"]}


def test_eligible_company(anns):
    p = Profile(kind="company", founded=founded(1.5), age=33, region="경기")
    ev = evaluate(anns[0], p)
    assert ev["verdict"] == "eligible", ev
    assert items(ev)["창업 기간"] == "eligible" and items(ev)["대상 연령"] == "eligible"


def test_stage_and_age_mismatch(anns):
    assert items(evaluate(anns[0], Profile(founded=founded(5), age=33)))["창업 기간"] == "ineligible"
    ev = evaluate(anns[0], Profile(founded=founded(1), age=45))
    assert ev["verdict"] == "ineligible" and items(ev)["대상 연령"] == "ineligible"


def test_missing_info_is_conditional(anns):
    ev = evaluate(anns[0], Profile(kind="company"))
    assert ev["verdict"] == "conditional"


def test_region_and_special_target(anns):
    female_pre = Profile(kind="pre", age=30, region="서울", female=True)
    assert evaluate(anns[1], female_pre)["verdict"] == "eligible"
    assert evaluate(anns[1], Profile(kind="pre", age=30, region="서울"))["verdict"] == "ineligible"
    assert evaluate(anns[1], Profile(kind="pre", age=30, region="부산", female=True))["verdict"] == "ineligible"


def test_text_conditions(anns):
    a = {**anns[3], "target_text": "업력 7년 이내, 대표자 만 39세 이하"}
    assert items(evaluate(a, Profile(founded=founded(8), age=30)))["업력 조건"] == "ineligible"
    assert items(evaluate(a, Profile(founded=founded(2), age=41)))["연령 조건"] == "ineligible"
    assert evaluate(a, Profile(founded=founded(2), age=30))["verdict"] == "eligible"


def test_everyone_age_buckets_ignored(anns):
    a = {**anns[3], "ages": "만 20세 미만,만 20세 이상 ~ 만 39세 이하,만 40세 이상"}
    assert "대상 연령" not in items(evaluate(a, Profile(founded=founded(2))))


def test_llm_rules_used(anns):
    a = {**anns[3], "rules": {"max_years": None, "max_age": None, "min_age": None,
                              "requires": ["restart"], "other_conditions": ["농업경영체 등록"]}}
    ev = evaluate(a, Profile(founded=founded(2)))
    assert ev["verdict"] == "ineligible" and any(c["detail"] == "농업경영체 등록" for c in ev["checks"])


def test_amount():
    assert amount_won("최대 1억 5천만원 지원") == 150_000_000
    assert amount_won("기업당 최대 5,000만원 이내") == 50_000_000
    assert amount_won("상금 최대 3천만원") == 30_000_000
    assert amount_won("총 사업비 300억원 규모") is None   # '최대·한도' 맥락 없음
    assert amount_won("") is None


def test_match_ranking_and_summary(anns):
    p = Profile(kind="company", founded=founded(1.5), age=33, region="경기", keywords="스마트팜", categories=["사업화"])
    out = match(anns, p)
    ids = [r["id"] for r in out["results"]]
    assert ids[0] == "A1" and "A3" not in ids          # 마감 제외
    assert out["summary"]["eligible"] >= 2 and out["summary"]["amount_total"] == 150_000_000
    assert "smartfarm" in out["results"][0]["topics"]
    assert "A3" in [r["id"] for r in match(anns, Profile(include_closed=True, keywords="푸드테크"))["results"]]
