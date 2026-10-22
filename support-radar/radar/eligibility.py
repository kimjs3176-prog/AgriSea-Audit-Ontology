"""자격 판정 + 적합도: 기업(창업자) 프로필 → 공고별 신청 가능 / 조건부 / 불가 와 근거.

공고의 정형 필드(창업기간·대상연령·지역·신청대상)는 규칙으로, 신청대상 본문은 정규식으로
조건을 뽑는다. (선택) LLM이 미리 뽑아 둔 조건(a["rules"])이 있으면 함께 쓴다.
판정 근거는 모두 사람이 읽을 수 있는 문장으로 남겨, 공고문 원문 확인을 돕는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .kstartup import status
from .topics import announcement_text, squash, topic_hits

OK, MAYBE, NO = "eligible", "conditional", "ineligible"
RANK = {OK: 0, MAYBE: 1, NO: 2}

REGIONS = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
           "충북", "충남", "전북", "전남", "경북", "경남", "제주")
REGION_ALIASES = {
    "서울": ("서울",), "부산": ("부산",), "대구": ("대구",), "인천": ("인천",), "광주": ("광주광역시", "광주"),
    "대전": ("대전",), "울산": ("울산",), "세종": ("세종",), "경기": ("경기",), "강원": ("강원",),
    "충북": ("충북", "충청북도"), "충남": ("충남", "충청남도"), "전북": ("전북", "전라북도"),
    "전남": ("전남", "전라남도"), "경북": ("경북", "경상북도"), "경남": ("경남", "경상남도"),
    "제주": ("제주",),
}
CATEGORIES = ("사업화", "기술개발(R&D)", "시설·공간·보육", "멘토링·컨설팅·교육", "융자",
              "인력", "행사·네트워크", "글로벌")
STAGES = ("예비창업자", "1년미만", "2년미만", "3년미만", "5년미만", "7년미만", "10년미만", "10년이상")


@dataclass
class Profile:
    kind: str = "company"              # pre(예비창업자) | company(창업기업)
    founded: str = ""                  # 개업일 YYYY-MM-DD (company)
    age: int | None = None             # 대표자 만 나이
    region: str = ""                   # 시·도(REGIONS)
    categories: list[str] = field(default_factory=list)   # 관심 지원 분야
    keywords: str = ""                 # 사업 아이템·업종(자유 기술)
    student: bool = False
    female: bool = False
    disabled: bool = False
    restart: bool = False              # 재창업
    include_closed: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Profile":
        p = cls()
        for k in cls.__dataclass_fields__:
            if k in d and d[k] not in (None, ""):
                setattr(p, k, d[k])
        if isinstance(p.age, str):
            p.age = int(p.age) if p.age.strip().isdigit() else None
        if isinstance(p.categories, str):
            p.categories = [c for c in p.categories.split(",") if c]
        for k in ("student", "female", "disabled", "restart", "include_closed"):
            v = getattr(p, k)
            setattr(p, k, v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes"))
        return p

    def years(self, today: date) -> float | None:
        if self.kind == "pre" or not self.founded:
            return None
        try:
            f = date.fromisoformat(self.founded[:10])
        except ValueError:
            return None
        return max(0.0, (today - f).days / 365.25)

    def stage(self, today: date) -> str | None:
        if self.kind == "pre":
            return "예비창업자"
        y = self.years(today)
        if y is None:
            return None
        for n in (1, 2, 3, 5, 7, 10):
            if y < n:
                return f"{n}년미만"
        return "10년이상"


# ── 조건 해석 ───────────────────────────────────────────
def _split(v: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,/|·]|\n", v or "") if x.strip()]


def _stage_label(s: str) -> str | None:
    t = squash(s)
    if "예비" in t:
        return "예비창업자"
    m = re.search(r"(\d+)년(미만|이내|이하)", t)
    if m:
        return f"{m.group(1)}년미만"
    if re.search(r"(\d+)년이상", t):
        return "10년이상" if int(re.search(r"(\d+)년이상", t).group(1)) >= 10 else None
    return None


def _age_ranges(text: str) -> list[tuple[int, int]]:
    """'만 20세 이상 ~ 만 39세 이하' → (20, 39). 여러 항목이면 각각."""
    out = []
    for part in _split(text):
        lo, hi = 0, 200
        for n, kind in re.findall(r"(\d+)\s*세\s*(이상|초과|이하|미만)", part):
            n = int(n)
            if kind == "이상":
                lo = n
            elif kind == "초과":
                lo = n + 1
            elif kind == "이하":
                hi = n
            else:
                hi = n - 1
        if (lo, hi) != (0, 200):
            out.append((lo, hi))
    return out


_YEARS_LIMIT = re.compile(r"(?:업력|창업|개업)[^0-9\n]{0,12}(\d+)\s*년\s*(이내|미만|이하)")
_AGE_LIMIT = re.compile(r"만\s*(\d+)\s*세\s*(이하|미만)")
_AGE_MIN = re.compile(r"만\s*(\d+)\s*세\s*(이상|초과)")
_NEED = {
    "female": (re.compile(r"여성\s*(창업|기업|대표|CEO|기업인|예비창업)"), "여성 창업자·여성 대표 기업 대상"),
    "disabled": (re.compile(r"장애인\s*(창업|기업|대표|예비창업)"), "장애인 창업자 대상"),
    "restart": (re.compile(r"재창업|재도전"), "재창업(재도전) 대상"),
    "student": (re.compile(r"(대학생|대학원생|재학생|학생\s*창업)"), "대학(원)생 대상"),
}


def amount_won(text: str) -> int | None:
    """'최대 1억 5천만원', '최대 5,000만원', '1.5억원' → 원 단위(가장 큰 '최대' 금액)."""
    best = None
    for m in re.finditer(r"(?:최대|최고|이내|한도)?\s*([\d,.]+\s*억\s*(?:[\d,]+\s*천\s*만?|[\d,]+\s*만)?|"
                         r"[\d,]+\s*천\s*만|[\d,]+\s*백\s*만|[\d,]+\s*만)\s*원", text or ""):
        ctx = text[max(0, m.start() - 12):m.end() + 6]
        if not re.search(r"최대|최고|이내|한도|지원|까지", ctx):
            continue
        s = m.group(1).replace(",", "").replace(" ", "")
        won = 0.0
        mm = re.match(r"([\d.]+)억(.*)", s)
        if mm:
            won += float(mm.group(1)) * 1e8
            s = mm.group(2)
        mm = re.match(r"([\d.]+)천만?", s)
        if mm:
            won += float(mm.group(1)) * 1e7
        else:
            mm = re.match(r"([\d.]+)백만", s)
            if mm:
                won += float(mm.group(1)) * 1e6
            else:
                mm = re.match(r"([\d.]+)만", s)
                if mm:
                    won += float(mm.group(1)) * 1e4
        if 1e6 <= won <= 5e10:
            best = max(best or 0, int(won))
    return best


def evaluate(a: dict[str, Any], p: Profile, today: date | None = None) -> dict[str, Any]:
    """판정: verdict(eligible|conditional|ineligible), checks[{item, result, detail}]."""
    today = today or date.today()
    checks: list[dict[str, str]] = []

    def add(item: str, result: str, detail: str) -> None:
        checks.append({"item": item, "result": result, "detail": detail})

    rules = a.get("rules") or {}
    st = status(a, today)
    if st == "closed":
        add("접수 기간", NO, f"마감({a.get('end') or '모집 종료'})")
    elif st == "upcoming":
        add("접수 기간", OK, f"접수 예정 {a.get('start')}")
    else:
        left = (date.fromisoformat(a["end"]) - today).days if a.get("end") else None
        add("접수 기간", OK, f"접수 중 · 마감 {a['end']} (D-{left})" if left is not None else "접수 중")

    # 지역
    region = a.get("region", "")
    if region and "전국" not in region:
        if not p.region:
            add("지원 지역", MAYBE, f"{region} 소재 기업 대상 — 소재지를 입력하면 판정")
        elif any(x in region for x in REGION_ALIASES.get(p.region, (p.region,))):
            add("지원 지역", OK, f"{region}")
        else:
            add("지원 지역", NO, f"{region} 대상(내 소재지: {p.region})")
    elif region:
        add("지원 지역", OK, "전국")

    # 창업 단계(업력)
    stages = [s for s in (_stage_label(x) for x in _split(a.get("stages", ""))) if s]
    my_stage = p.stage(today)
    if stages:
        label = ", ".join(dict.fromkeys(stages))
        if not my_stage:
            add("창업 기간", MAYBE, f"대상: {label} — 개업일을 입력하면 판정")
        elif my_stage in stages:
            add("창업 기간", OK, f"대상: {label} (나: {my_stage})")
        else:
            add("창업 기간", NO, f"대상: {label} (나: {my_stage})")
    text = f"{a.get('title', '')}\n{a.get('target_text', '')}"
    years_max, strict = rules.get("max_years"), False
    m = _YEARS_LIMIT.search(text)
    if years_max is None and m:
        years_max, strict = int(m.group(1)), m.group(2) == "미만"
    if years_max is not None and not stages:
        cond = f"업력 {years_max}년 {'미만' if strict else '이내'}"
        y = p.years(today)
        if p.kind == "pre":
            add("업력 조건", OK, f"{cond} — 예비창업자")
        elif y is None:
            add("업력 조건", MAYBE, f"{cond} — 개업일을 입력하면 판정")
        else:
            ok = y < years_max if strict else y <= years_max
            add("업력 조건", OK if ok else NO, f"{cond} (나: {y:.1f}년)")

    # 연령
    ranges = _age_ranges(a.get("ages", ""))
    everyone = all(any(lo <= x <= hi for lo, hi in ranges) for x in range(15, 90))
    if ranges and not everyone:
        desc = a.get("ages", "")
        if p.age is None:
            add("대상 연령", MAYBE, f"{desc} — 나이를 입력하면 판정")
        elif any(lo <= p.age <= hi for lo, hi in ranges):
            add("대상 연령", OK, f"{desc} (나: 만 {p.age}세)")
        else:
            add("대상 연령", NO, f"{desc} (나: 만 {p.age}세)")
    mx, mn = rules.get("max_age"), rules.get("min_age")
    if mx is None and (m := _AGE_LIMIT.search(text)):
        mx = int(m.group(1)) - (m.group(2) == "미만")
    if mn is None and (m := _AGE_MIN.search(text)):
        mn = int(m.group(1)) + (m.group(2) == "초과")
    if mx or mn:
        desc = " ".join(x for x in (f"만 {mn}세 이상" if mn else "", f"만 {mx}세 이하" if mx else "") if x)
        if p.age is None:
            add("연령 조건", MAYBE, f"{desc} — 나이를 입력하면 판정")
        else:
            ok = (mx is None or p.age <= mx) and (mn is None or p.age >= mn)
            add("연령 조건", OK if ok else NO, f"{desc} (나: 만 {p.age}세)")
    elif "청년" in a.get("title", "") and not ranges and p.age is not None and p.age > 39:
        add("청년 대상", MAYBE, "청년 대상 공고(통상 만 39세 이하) — 공고문의 연령 기준 확인")

    # 특정 대상(여성·장애인·재창업·학생)
    needs = set(rules.get("requires") or [])
    head = f"{a.get('title', '')} {a.get('program', '')} {a.get('target', '')}"
    for key, (pat, desc) in _NEED.items():
        if key in needs or pat.search(head):
            add("특정 대상", OK if getattr(p, key) else NO, desc)

    # 신청 대상 유형(예비창업자/기업)
    target = a.get("target", "")
    if target:
        t = squash(target)
        if p.kind == "pre":
            ok = any(k in t for k in ("일반인", "예비", "개인", "대학생", "누구나"))
        else:
            ok = any(k in t for k in ("기업", "창업자", "사업자", "법인", "누구나"))
        if not ok and p.kind == "pre" and p.student and "대학" in t:
            ok = True
        add("신청 대상", OK if ok else MAYBE, target if ok else f"{target} — 내 유형과 맞는지 확인")

    if a.get("exclusion_text"):
        ex = a["exclusion_text"]
        add("제외 대상", "note", "확인 필요: " + (ex if len(ex) <= 160 else ex[:160] + "…"))
    for extra in rules.get("other_conditions") or []:
        add("추가 조건", "note", extra)

    results = [c["result"] for c in checks if c["result"] != "note"]
    verdict = NO if NO in results else MAYBE if MAYBE in results else OK
    return {"verdict": verdict, "checks": checks}


def fit_score(a: dict[str, Any], p: Profile, profile_topics: dict[str, int]) -> tuple[int, list[str]]:
    """적합도(0~100)와 이유."""
    score, why = 0, []
    cat = a.get("category", "")
    if p.categories and any(c.split("(")[0] in cat for c in p.categories):
        score += 25
        why.append(f"관심 분야 '{cat}'")
    words = [w for w in re.split(r"[\s,·/]+", p.keywords or "") if len(w) >= 2]
    if words:
        title = squash(f"{a.get('title', '')} {a.get('program', '')}")
        body = squash(announcement_text(a))
        hit_t = [w for w in words if squash(w) in title]
        hit_b = [w for w in words if squash(w) in body and w not in hit_t]
        score += min(40, 20 * len(hit_t) + 8 * len(hit_b))
        if hit_t or hit_b:
            why.append("키워드 일치: " + ", ".join(hit_t + hit_b))
    common = [t for t in a.get("topics", []) if t in profile_topics]
    if common:
        from .topics import label
        score += 20
        why.append("주제 일치: " + ", ".join(label(t) for t in common))
    if not p.categories and not words:
        score += 30   # 관심사를 안 적으면 자격만으로 정렬
    return min(score, 100), why


def match(announcements: list[dict], p: Profile, today: date | None = None,
          limit: int = 200) -> dict[str, Any]:
    from .policy import tag_announcements
    today = today or date.today()
    tag_announcements(announcements)
    profile_topics = topic_hits(p.keywords)
    results = []
    for a in announcements:
        st = status(a, today)
        if st == "closed" and not p.include_closed:
            continue
        ev = evaluate(a, p, today)
        score, why = fit_score(a, p, profile_topics)
        amt = amount_won(f"{a.get('title', '')} {a.get('content', '')}")
        results.append({
            "id": a["id"], "title": a["title"], "org": a.get("org", ""), "category": a.get("category", ""),
            "region": a.get("region", ""), "start": a.get("start", ""), "end": a.get("end", ""),
            "status": st, "url": a.get("url", ""), "topics": a.get("topics", []),
            "verdict": ev["verdict"], "checks": ev["checks"], "score": score, "why": why,
            "amount": amt,
        })
    relevant = [r for r in results if r["score"] > 0]
    results.sort(key=lambda r: (RANK[r["verdict"]], -r["score"], r["end"] or "9999"))
    count = {v: sum(1 for r in relevant if r["verdict"] == v) for v in (OK, MAYBE, NO)}
    soon = [r for r in relevant if r["verdict"] != NO and r["status"] == "open" and r["end"]
            and (date.fromisoformat(r["end"]) - today).days <= 14]
    total = sum(r["amount"] or 0 for r in relevant if r["verdict"] == OK)
    return {
        "summary": {"eligible": count[OK], "conditional": count[MAYBE], "ineligible": count[NO],
                    "closing_soon": len(soon), "amount_total": total,
                    "amount_counted": sum(1 for r in relevant if r["verdict"] == OK and r["amount"]),
                    "considered": len(results)},
        "results": [r for r in results if r["score"] > 0][:limit],
    }
