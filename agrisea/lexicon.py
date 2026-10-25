"""농해수위 도메인 사전: 쟁점(이슈) 분류체계, 피감기관, 답변 유형 패턴.

온톨로지의 SKOS 개념체계(ag:Issue)와 조직(ag:Organization) 인스턴스의 원천이다.
키워드를 추가/수정하면 재색인(`python -m agrisea build`) 시 그래프에 반영된다.
"""
from __future__ import annotations

import re

# id: (한글명, 상위 id 또는 None, 키워드들)
ISSUES: dict[str, tuple[str, str | None, tuple[str, ...]]] = {
    # ── 농업 ─────────────────────────────────────────────
    "agri": ("농업·농촌", None, ()),
    "rice": ("쌀·식량안보", "agri", ("쌀값", "쌀 수급", "양곡관리법", "공공비축", "식량자급률",
                               "식량안보", "논 타작물", "전략작물", "쌀 소비", "수확기")),
    "income": ("농가소득·경영안정", "agri", ("농가소득", "공익직불", "직불금", "기본직불",
                                     "경영안정", "수입보장", "농업소득", "부채", "정책자금")),
    "price": ("농산물 가격·유통", "agri", ("농산물 가격", "가격안정", "유통구조", "도매시장",
                                    "온라인도매시장", "할당관세", "물가", "배추", "사과",
                                    "금사과", "수급 불안", "가격 폭락", "경매")),
    "livestock": ("축산·가축방역", "agri", ("가축전염병", "아프리카돼지열병", "ASF", "구제역",
                                      "조류인플루엔자", "고병원성", "럼피스킨", "방역", "살처분",
                                      "한우", "소값", "사료값", "축산")),
    "rural": ("농촌소멸·농업인력", "agri", ("농촌소멸", "지방소멸", "고령화", "청년농",
                                      "후계농", "계절근로자", "외국인 근로자", "인력난", "귀농",
                                      "농촌 공간")),
    "smartagri": ("스마트농업·R&D", "agri", ("스마트팜", "스마트농업", "디지털 농업", "농업 R&D",
                                        "연구개발", "종자", "푸드테크", "그린바이오", "기술이전",
                                        "기술사업화")),
    "disaster": ("기후위기·농업재해", "agri", ("기후변화", "기후위기", "이상기후", "폭염", "집중호우",
                                        "가뭄", "냉해", "재해보험", "농업재해", "재해 복구",
                                        "탄소중립", "저탄소")),
    "foodsafety": ("식품·통상·검역", "agri", ("식품안전", "원산지", "FTA", "CPTPP", "수입 농산물",
                                        "검역", "통상", "관세", "외식", "식품산업")),
    "animal": ("동물복지·반려동물", "agri", ("동물복지", "반려동물", "개식용", "유기동물",
                                       "동물보호")),
    "forest": ("산림·산불", "agri", ("산불", "산림", "산사태", "목재", "임업", "숲가꾸기")),
    # ── 해양수산 ───────────────────────────────────────────
    "sea": ("해양·수산", None, ()),
    "fishery": ("수산업·어촌", "sea", ("어업인", "어민", "어촌", "어선", "연근해", "수산자원",
                                   "양식", "수산물 소비", "수산물 가격", "어획량", "어촌소멸",
                                   "면세유")),
    "marineenv": ("해양환경·방사능", "sea", ("오염수", "후쿠시마", "방사능", "해양쓰레기",
                                        "해양환경", "적조", "고수온", "해양생태")),
    "maritime": ("해운·항만·물류", "sea", ("해운", "항만", "HMM", "선사", "물류", "부산항",
                                       "신항", "컨테이너", "조선")),
    "safety": ("해양안전·해경", "sea", ("해양안전", "해양경찰", "해경", "선박 사고", "인명구조",
                                     "여객선", "불법조업", "중국 어선")),
    # ── 공통 ───────────────────────────────────────────────
    "gov": ("공공기관·예산·조직운영", None, ("예산", "결산", "집행률", "공공기관", "방만",
                                    "낙하산", "임원", "성과급", "감사원", "비리", "갑질",
                                    "채용", "부채비율", "농협", "수협", "지배구조")),
}

# 피감기관(정규명: 별칭)
ORGANIZATIONS: dict[str, tuple[str, ...]] = {
    "농림축산식품부": ("농식품부", "농림부", "농림축산식품부장관", "농림축산식품부차관"),
    "해양수산부": ("해수부", "해양수산부장관", "해양수산부차관"),
    "농촌진흥청": ("농진청", "농촌진흥청장"),
    "산림청": ("산림청장",),
    "해양경찰청": ("해경청", "해양경찰청장"),
    "농림축산검역본부": ("검역본부",),
    "국립농산물품질관리원": ("농관원",),
    "국립수산물품질관리원": ("수품원",),
    "국립수산과학원": ("수과원",),
    "한국농어촌공사": ("농어촌공사",),
    "한국농수산식품유통공사": ("aT", "농수산식품유통공사"),
    "농업협동조합중앙회": ("농협중앙회", "농협"),
    "수산업협동조합중앙회": ("수협중앙회", "수협"),
    "산림조합중앙회": ("산림조합",),
    "한국마사회": ("마사회",),
    "한국농업기술진흥원": ("농진원", "농업기술진흥원"),
    "농림식품기술기획평가원": ("농기평",),
    "농업정책보험금융원": ("농금원",),
    "축산물품질평가원": ("축평원",),
    "한국임업진흥원": ("임업진흥원",),
    "한국해양진흥공사": ("해진공",),
    "해양환경공단": (),
    "한국수산자원공단": ("수산자원공단",),
    "한국해양조사협회": (),
    "한국어촌어항공단": ("어촌어항공단",),
    "부산항만공사": (),
    "인천항만공사": (),
    "여수광양항만공사": (),
    "울산항만공사": (),
    "한국해양과학기술원": ("KIOST",),
    "한국해양수산개발원": ("KMI",),
    "국립해양조사원": (),
}

# 답변 중 '이행 약속'으로 볼 수 있는 표현(국감 사후관리 대상): 의례적 표현을 제외한 '~하겠습니다'
COMMITMENT_PATTERNS = re.compile(r"(하겠습니다|드리겠습니다|되도록\s*하겠습니다)")
NON_COMMITMENT = re.compile(r"(말씀드리겠습니다|답변드리겠습니다|설명드리겠습니다|시작하겠습니다|"
                            r"마치겠습니다|상정하겠습니다|속개하겠습니다|듣겠습니다|"
                            r"인사드리겠습니다|보고를\s*드리겠습니다)")


# 국감 사후 점검 대상이 되는 '구체적 조치' 약속: 조치 동사 + (하/드리)겠습니다
COMMITMENT_CONCRETE = re.compile(
    r"(검토|재검토|개선|보완|추진|마련|제출|보고|점검|시정|협의|확인|반영|조치|확대|강화|도입|구축|"
    r"정비|지원|개정|파악|조사|해결|수립|배정|편성|확보|정리|재정비|보강|설치|감독|관리|논의)"
    r"(을|를|이|가|도|하도록|되도록|토록|해서|하여)?\s*(하|드리|되도록\s*하|토록\s*하|해\s*나가|해\s*드리)겠습니다")
# 인사·의례 문장(업무보고 인사말, 감사 인사 등)은 약속으로 보지 않는다
COURTESY_SENT = re.compile(r"(감사(드립|합)니다|감사의 말씀|존경하는 .{0,30}(위원장|위원)님|바쁘신 (일정|가운데)|"
                           r"최선을 다하겠습니다|노력하겠습니다|인사(를|말씀)|수고(하셨|많으셨))")
REPORT_FOLLOWUP = re.compile(r"(별도|서면|추후|나중에|결과|자료|다시|따로|정리해|정리하여|파악해|확인해|검토해|"
                             r"조사해|알아보고|챙겨서|말씀하신)")
_SENT_END = re.compile(r"(?<=[.?!])\s+|(?<=습니다)\s+|(?<=겠습니다)\s*")


def commitment_sentences(text: str) -> list[str]:
    """발언에서 구체적 조치 약속이 담긴 문장만 추출."""
    out = []
    for s in _SENT_END.split(text):
        s = s.strip()
        m = COMMITMENT_CONCRETE.search(s) if len(s) >= 12 else None
        if not m or COURTESY_SENT.search(s):
            continue
        # '보고드리겠습니다'는 업무보고 시작 인사로도 쓰이므로 사후 보고 맥락이 있을 때만 약속으로 본다
        if m.group(1) == "보고" and not REPORT_FOLLOWUP.search(s):
            continue
        out.append(s)
    return out


def is_commitment(text: str) -> bool:
    return bool(commitment_sentences(text))


# 자료 요구
DATA_REQUEST_PATTERNS = re.compile(
    r"(자료(를|로)?\s*(제출|요구|요청)|서면으로\s*(제출|답변)|(제출|보고)해\s*주(십시오|세요|시기))"
)
# 질의 판별
QUESTION_PATTERNS = re.compile(
    r"(\?|습니까|합니까|입니까|겁니까|건가요|나요|까요|십니까|없어요|있어요\?|죠\?)"
)

# 위원 질의 관점(무엇을 따지는가): 쟁점(무엇에 대해)과 별개로, 같은 쟁점도 예산·감독·피해 등 따지는 각도가 다르다
PERSPECTIVES: dict[str, tuple[str, re.Pattern]] = {
    "budget": ("예산·재정", re.compile(r"예산|국비|집행률|불용|이월|증액|삭감|재원|기금|보조금|출연금|국고")),
    "law": ("법·제도 개선", re.compile(r"법안|법률|개정|시행령|시행규칙|제도\s?개선|규정|고시|입법|사각지대|법적 근거")),
    "field": ("현장·민생 피해", re.compile(r"피해|생계|보상|손실|어려움|민원|고통|현장의 목소리")),
    "oversight": ("관리·감독 책임", re.compile(r"감독|감사원|부실|비리|위반|적발|책임|징계|횡령|특혜|은폐|허위|방치|유착")),
    "safety": ("안전·위기 대응", re.compile(r"안전|사고|재난|대응 체계|매뉴얼|예방|방역|산불|화재|사망|인명")),
    "perf": ("성과·실효성", re.compile(r"실적|성과|실효성|목표|달성|저조|미흡|지지부진|효과가|효과는|평가")),
    "trade": ("통상·수급", re.compile(r"FTA|수입|수출|통상|협상|관세|WTO|CPTPP|개방|수급|비축")),
    "region": ("지역 현안", re.compile(r"지역구|지자체|지방자치|시군|시·군|군청|도청|지역 주민|지역 경제|지역민")),
}

ROLE_TYPES = (
    # (정규식, speaker_type)
    # 회의 진행자: 위원장·소위원장·조정위원장(및 대리). '원장'으로 끝나 기관장으로 오인되지 않게 먼저 판별
    (re.compile(r"^(소|조정|분과)?위원장(대리)?$"), "chair"),
    (re.compile(r"^위원$"), "member"),
    (re.compile(r"전문위원|입법조사관"), "staff"),
    (re.compile(r"^증인|증인$"), "witness"),
    (re.compile(r"^(참고인|진술인)|(참고인|진술인)$"), "reference"),
    (re.compile(r"(장관|차관|청장|실장|국장|본부장|원장|사장|이사장|회장|대표이사|대표|이사|처장|"
                r"정책관|단장|과장|관장|소장|총장)$"), "official"),
)


def issue_label(issue_id: str) -> str:
    return ISSUES[issue_id][0]


# 쟁점 키워드를 품은 고유명사(기관·위원회명)와 합성어: 집계 전에 지워 오탐을 막는다.
# 예) '축산' ⊂ 농림축산식품부, '산림' ⊂ 산림청, '어촌' ⊂ 농어촌
_ISSUE_MASK = re.compile("|".join(sorted(
    {re.escape(x) for name, aliases in ORGANIZATIONS.items() for x in (name, *aliases) if len(x) > 2}
    | {re.escape(x) for x in ("농림축산식품해양수산위원회", "농림축산식품해양수산", "농림축산식품",
                              "농해수위", "농어촌")},
    key=len, reverse=True)))


_ISSUE_KWS = [(iid, tuple(kws)) for iid, (_, _, kws) in ISSUES.items()]


def match_issues(text: str) -> dict[str, int]:
    """텍스트에서 이슈별 키워드 출현 횟수(기관·위원회명 속 키워드는 제외)."""
    count = _ISSUE_MASK.sub(" ", text).count
    hits: dict[str, int] = {}
    for iid, kws in _ISSUE_KWS:
        n = sum(map(count, kws))
        if n:
            hits[iid] = n
    return hits


_ORG_PATTERNS: list[tuple[str, re.Pattern]] = []
for _name, _aliases in ORGANIZATIONS.items():
    _alts = sorted({_name, *_aliases}, key=len, reverse=True)
    # 짧은 영문 약어(aT 등)는 단어 경계, 한글은 그대로
    _parts = [rf"(?<![A-Za-z]){re.escape(a)}(?![A-Za-z])" if a.isascii() else re.escape(a)
              for a in _alts]
    _ORG_PATTERNS.append((_name, re.compile("|".join(_parts))))


def match_organizations(text: str) -> dict[str, int]:
    hits: dict[str, int] = {}
    for name, pat in _ORG_PATTERNS:
        n = len(pat.findall(text))
        if n:
            hits[name] = n
    return hits


def org_from_role(role: str) -> str | None:
    for name, pat in _ORG_PATTERNS:
        if pat.search(role):
            return name
    return None


def classify_role(role: str) -> str:
    for pat, kind in ROLE_TYPES:
        if pat.search(role):
            return kind
    return "other"
