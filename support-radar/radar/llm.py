"""(선택) Claude로 공고문 신청 자격을 구조화한다. ANTHROPIC_API_KEY가 있을 때만 쓰인다.

정규식이 못 잡는 조건(업력·연령 상한, 특정 대상, 기타 요건)을 뽑아 a["rules"]에 넣으면
eligibility.evaluate()가 규칙 판정과 함께 쓴다. 본문이 같으면 다시 부르지 않는다.
"""
from __future__ import annotations

import json
from typing import Any

from .config import Settings

RULES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "max_years": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "업력 상한(년, '이내' 기준). 없으면 null"},
        "max_age": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "대표자 만 나이 상한. 없으면 null"},
        "min_age": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "대표자 만 나이 하한. 없으면 null"},
        "requires": {
            "type": "array",
            "items": {"type": "string", "enum": ["female", "disabled", "restart", "student"]},
            "description": "반드시 해당해야 하는 신청자 유형(여성·장애인·재창업·대학생)",
        },
        "other_conditions": {
            "type": "array", "items": {"type": "string"},
            "description": "그 밖의 필수 요건(업종·매출·인증·소재지 등) 최대 5개, 각 40자 이내",
        },
    },
    "required": ["max_years", "max_age", "min_age", "requires", "other_conditions"],
    "additionalProperties": False,
}

SYSTEM = (
    "당신은 정부 창업지원사업 공고문에서 신청 자격 요건을 정확히 뽑는 심사 담당자입니다. "
    "공고문에 명시된 요건만 적고, 추정하지 마세요. 우대사항은 필수 요건이 아닙니다."
)


def extract_rules(announcements: list[dict], settings: Settings, limit: int = 150) -> int:
    """조건이 없는 공고부터 limit건 처리. 처리 건수를 돌려준다."""
    import anthropic

    client = anthropic.Anthropic()
    done = 0
    for a in announcements:
        if done >= limit:
            break
        if a.get("rules") is not None:
            continue
        prompt = (f"공고명: {a['title']}\n신청대상: {a.get('target', '')}\n창업기간: {a.get('stages', '')}\n"
                  f"대상연령: {a.get('ages', '')}\n지역: {a.get('region', '')}\n"
                  f"<신청대상 상세>\n{a.get('target_text', '')}\n</신청대상 상세>\n"
                  f"<제외대상>\n{a.get('exclusion_text', '')}\n</제외대상>\n"
                  f"<공고 본문>\n{a.get('content', '')}\n</공고 본문>\n\n위 공고의 신청 자격 요건을 정리하세요.")
        response = client.messages.create(
            model=settings.llm_model, max_tokens=16000, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": RULES_SCHEMA}},
            # 안전 분류기에 의한 거절 시 서버측 대체 모델로 자동 재시도
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            continue
        text = next(b.text for b in response.content if b.type == "text")
        a["rules"] = json.loads(text)
        done += 1
    return done
