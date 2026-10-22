import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# 저장소 루트에서 함께 실행해도 루트 서비스의 api 패키지를 가리지 않도록 뒤에 추가
sys.path.append(str(Path(__file__).resolve().parent.parent))


def day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


@pytest.fixture()
def anns():
    """테스트용 가상 공고(필드 구성만 K-Startup 형식을 따름)."""
    return [
        {"id": "A1", "title": "[테스트] 스마트팜 청년 창업 사업화 지원", "program": "", "category": "사업화",
         "target": "일반기업,1인 창조기업", "target_text": "업력 3년 이내 창업기업", "exclusion_text": "국세 체납 기업",
         "preference": "", "region": "전국", "stages": "1년미만,2년미만,3년미만", "ages": "만 20세 이상 ~ 만 39세 이하",
         "start": day(-10), "end": day(5), "org": "테스트진흥원", "supervisor": "", "dept": "", "contact": "",
         "url": "https://example.test/a1", "guide_url": "", "apply_url": "", "recruiting": True, "integrated": False,
         "content": "스마트팜 분야 창업기업에 최대 1억 5천만원 사업화 자금을 지원합니다."},
        {"id": "A2", "title": "[테스트] 여성 창업 경진대회", "program": "", "category": "행사·네트워크",
         "target": "일반인", "target_text": "", "exclusion_text": "", "preference": "", "region": "서울",
         "stages": "예비창업자", "ages": "", "start": day(-3), "end": day(30), "org": "테스트재단",
         "supervisor": "", "dept": "", "contact": "", "url": "", "guide_url": "", "apply_url": "",
         "recruiting": True, "integrated": False, "content": "상금 최대 3,000만원"},
        {"id": "A3", "title": "[테스트] 푸드테크 R&D 지원(마감)", "program": "", "category": "기술개발(R&D)",
         "target": "일반기업", "target_text": "", "exclusion_text": "", "preference": "", "region": "전국",
         "stages": "", "ages": "", "start": day(-60), "end": day(-30), "org": "테스트원", "supervisor": "",
         "dept": "", "contact": "", "url": "", "guide_url": "", "apply_url": "", "recruiting": False,
         "integrated": False, "content": "푸드테크 대체식품 기술개발"},
        {"id": "A4", "title": "[테스트] 스마트팜 실증 지원 사업", "program": "", "category": "사업화",
         "target": "일반기업", "target_text": "", "exclusion_text": "", "preference": "", "region": "전국",
         "stages": "", "ages": "", "start": day(-1), "end": day(20), "org": "테스트원", "supervisor": "",
         "dept": "", "contact": "", "url": "", "guide_url": "", "apply_url": "", "recruiting": True,
         "integrated": False, "content": "스마트팜 실증"},
    ]
