"""정책 주제(레이더 축): 국회 발언과 지원사업 공고를 같은 기준으로 묶는 키워드 사전.

공고 쪽은 창업 지원 전반이라 '수출'·'AI'처럼 넓은 말은 농식품 맥락이 붙은 표현만 쓴다.
띄어쓰기 차이('스마트 팜')를 흡수하려고 공백을 지운 뒤 비교한다.
"""
from __future__ import annotations

import re

# id: (이름, 설명, 키워드)
TOPICS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "smartfarm": ("스마트팜·스마트농업", "시설 자동화, 수직농장, 정밀농업, 농업 로봇·드론",
                  ("스마트팜", "스마트농업", "수직농장", "식물공장", "정밀농업", "농업용 드론",
                   "농업 로봇", "자율주행 농기계", "스마트 농장")),
    "agtech_ai": ("농업 AI·데이터", "애그테크, 농업 빅데이터, 디지털 농업",
                  ("애그테크", "어그테크", "AgTech", "농업 AI", "농업 인공지능", "농업 데이터",
                   "디지털 농업", "농업 빅데이터")),
    "foodtech": ("푸드테크", "대체식품, 간편식, 식품 제조 혁신",
                 ("푸드테크", "대체식품", "대체육", "배양육", "식물성 단백질", "식물성 대체",
                  "간편식", "밀키트", "식품 스타트업", "식품산업 진흥")),
    "greenbio": ("그린바이오·종자", "종자, 미생물, 곤충, 천연물·바이오소재",
                 ("그린바이오", "종자산업", "종자 산업", "미생물", "곤충산업", "곤충 산업",
                  "동물용의약품", "천연물", "바이오소재", "생명소재", "디지털육종")),
    "youthfarm": ("청년농·영농창업", "청년 농업인, 후계농, 귀농·귀촌 창업",
                  ("청년농", "청년 농업인", "청년농업인", "후계농", "영농창업", "농업 창업",
                   "농식품 창업", "농산업 창업", "귀농", "귀촌")),
    "local": ("로컬·농촌 융복합", "농촌 6차산업, 로컬크리에이터, 농촌관광",
              ("로컬크리에이터", "로컬 크리에이터", "로컬 창업", "농촌 융복합", "6차산업",
               "6차 산업", "농촌관광", "농촌 창업", "농촌 공간")),
    "distribution": ("농산물 유통·판로", "온라인도매시장, 산지유통, 직거래·로컬푸드",
                     ("온라인도매시장", "온라인 도매시장", "농산물 유통", "산지유통", "로컬푸드",
                      "농산물 직거래", "유통구조 개선", "농산물 판로")),
    "kfood": ("K-푸드 수출", "농식품·수산식품 수출, 해외 판로",
              ("K-푸드", "K푸드", "케이푸드", "K-Food", "농식품 수출", "식품 수출",
               "수산식품 수출", "수산물 수출", "농산물 수출")),
    "bluefood": ("블루푸드·스마트양식", "수산·양식 혁신, 해양수산 창업",
                 ("블루푸드", "스마트양식", "스마트 양식", "수산식품", "양식산업", "해양수산 창업",
                  "수산 스타트업", "해양수산 스타트업", "어촌 창업")),
    "climate": ("기후테크·탄소중립", "저탄소 농업, 기후적응, 재생에너지",
                ("탄소중립", "저탄소", "기후테크", "기후위기", "기후 위기", "온실가스", "기후적응",
                 "영농형 태양광", "농업 에너지")),
    "pet": ("반려동물·펫산업", "펫푸드, 펫테크, 동물복지",
            ("반려동물", "펫푸드", "펫테크", "펫 산업", "펫산업", "동물복지")),
    "techtransfer": ("기술이전·사업화", "공공기술 이전, 실험실 창업, 연구소기업",
                     ("기술이전", "기술 이전", "기술사업화", "기술 사업화", "실험실 창업",
                      "연구소기업", "공공기술")),
    "forest": ("산림·임업", "산림바이오, 목재, 임산물",
               ("산림바이오", "산림 바이오", "임산물", "목재산업", "목재 산업", "임업", "산림 창업")),
}

_SPACE = re.compile(r"\s+")


def squash(text: str) -> str:
    return _SPACE.sub("", text or "").lower()


_KW: dict[str, tuple[str, ...]] = {
    tid: tuple(sorted({squash(k) for k in kws}, key=len, reverse=True))
    for tid, (_, _, kws) in TOPICS.items()
}


def topic_hits(text: str) -> dict[str, int]:
    """주제별 키워드 출현 횟수."""
    s = squash(text)
    hits = {}
    for tid, kws in _KW.items():
        n = sum(s.count(k) for k in kws)
        if n:
            hits[tid] = n
    return hits


def topic_keywords(tid: str) -> tuple[str, ...]:
    return _KW[tid]


def announcement_text(a: dict) -> str:
    return " ".join(a.get(k, "") for k in ("title", "program", "category", "target_text",
                                           "preference", "content"))


def announcement_topics(a: dict) -> list[str]:
    """공고의 주제: 제목·사업명에 나오면 바로, 본문에만 나오면 2번 이상일 때."""
    head = topic_hits(f"{a.get('title', '')} {a.get('program', '')}")
    body = topic_hits(announcement_text(a))
    return [t for t in TOPICS if head.get(t) or body.get(t, 0) >= 2]


def label(tid: str) -> str:
    return TOPICS[tid][0]
