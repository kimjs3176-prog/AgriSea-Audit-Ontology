"""회의록 본문(PDF/텍스트) → 발언 단위 구조화.

국회 회의록은 발언자를 '◯'(또는 '○')로 표기한다.
    ◯위원장 홍길동  성원이 되었으므로 ...
    ◯김가람 위원  장관님, ...
    ◯농림축산식품부장관 정마루  말씀드리겠습니다.
PDF 추출 시 띄어쓰기가 사라지는 경우도 있어 여러 패턴을 순차 적용한다.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from .lexicon import (DATA_REQUEST_PATTERNS, QUESTION_PATTERNS, classify_role, is_commitment,
                      org_from_role)

SPEAKER_MARK = re.compile(r"[◯○]")
TIME_NOTE = re.compile(r"\((?:\d{1,2}\s*시\s*\d{1,2}\s*분\s*)?[^()]{0,12}"
                       r"(?:개의|산회|회의중지|계속개의|정회|속개)\)")
PAGE_NOISE = [
    re.compile(r"^\s*-?\s*\d{1,4}\s*-?\s*$"),
    re.compile(r"^\s*제\s*\d+\s*회.{0,40}제\s*\d+\s*호\s*$"),
    re.compile(r"^\s*국\s*회\s*사\s*무\s*처\s*$"),
]
AGENDA_LINE = re.compile(r"^\s*(\d{1,2})\s*\.\s*(.{4,120}?)\s*(?:[·.…]{3,}\s*\d+)?\s*$")
OFFICIAL_SUFFIX = (r"(?:장관|차관|청장|처장|실장|국장|본부장|원장|사장|이사장|회장|대표이사|대표|"
                   r"정책관|단장|과장|총장|전문위원|수석전문위원|입법조사관|증인|참고인|진술인)")

SPEAKER_PATTERNS = [
    # 위원장 / 위원장대리
    re.compile(r"^(위원장(?:대리)?)\s+([가-힣]{2,4})(?=\s|$)"),
    re.compile(r"^(위원장(?:대리)?)([가-힣]{3})"),
    # 소위원장 등
    re.compile(r"^(소위원장)\s*([가-힣]{2,4})(?=\s|$)"),
    # ○○○ 위원
    re.compile(r"^([가-힣]{2,4})\s+(위원)(?=\s|$)"),
    re.compile(r"^([가-힣]{3})(위원)(?![장회])"),
    # 증인/참고인 이름 (앞)
    re.compile(r"^(증인|참고인|진술인)\s*([가-힣]{2,4})(?=\s|$)"),
    # 기관+직위 이름
    re.compile(r"^(\S{1,30}?" + OFFICIAL_SUFFIX + r")\s+([가-힣]{2,4})(?=\s|$)"),
    re.compile(r"^([^\s]{1,30}?" + OFFICIAL_SUFFIX + r")([가-힣]{3})"),
]


@dataclass
class Utterance:
    idx: int
    speaker_name: str
    speaker_role: str
    speaker_type: str
    org: str | None
    text: str
    agenda_idx: int | None = None
    is_question: bool = False
    is_commitment: bool = False
    is_data_request: bool = False


@dataclass
class MinutesDoc:
    agendas: list[str] = field(default_factory=list)
    utterances: list[Utterance] = field(default_factory=list)
    preamble: str = ""


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


# 본문 사이에 끼어든 쪽 머리말: '16 제439회-농림축산식품해양수산제2차(2026년9월17일)' 등
RUNNING_HEADER = re.compile(
    r"(?:\b\d{1,3}\s*)?제\s*\d+\s*회\s*-\s*[가-힣·\s]{2,40}?제\s*\d+\s*(?:차|호)\s*"
    r"\(\s*\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일\s*\)(?:\s*\d{1,3}\b)?")


# PDF 쪽 경계에 남는 글리프 잔여물: 'ਘ\x13\x11ੌ \x01 \x01 \x16'(구르무키 문자 + 제어문자 + 쪽 번호 코드)
PDF_GLYPH_NOISE = re.compile(r"[\u0a00-\u0a7f][\x00-\x1f]*[\u0a00-\u0a7f]?(?:\s*[\x00-\x08\x0b\x0c\x0e-\x1f])*")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_utterance_text(text: str) -> str:
    text = CONTROL_CHARS.sub(" ", PDF_GLYPH_NOISE.sub(" ", text))
    return re.sub(r"\s+", " ", RUNNING_HEADER.sub(" ", text)).strip()


def clean_text(raw: str) -> str:
    lines = []
    for line in raw.replace("\r", "\n").split("\n"):
        if any(p.match(line) for p in PAGE_NOISE):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines)


# 직위 뒤에 붙는 대행·후보 표시: '해양경찰청장 직무대행 장인식', '해양수산부장관 후보자 황종우'.
# 띄어쓰기가 사라지면 '직무대'가 이름으로, '후보자'가 이름으로 잡히므로 바로잡는다.
ROLE_QUALIFIER = re.compile(r"^((?:전담)?직무대[행리]|권한대행|후보자)\s*([가-힣]{2,4})(?=\s|$)")


def fix_role_qualifier(role: str, name: str, text: str) -> tuple[str, str, str]:
    """(직위, 이름, 본문)에서 이름 자리에 끼어든 '직무대행'·'후보자'를 직위로 옮기고 진짜 이름을 찾는다."""
    for joined in (f"{name}{text}", f"{name} {text}"):
        m = ROLE_QUALIFIER.match(joined)
        if m:
            return f"{role} {m.group(1)}", m.group(2), joined[m.end():].strip()
    return role, name, text


def parse_speaker(chunk: str) -> tuple[str, str, str] | None:
    """'위원장 홍길동 성원이...' → (role, name, rest)."""
    head = chunk.lstrip()
    for pat in SPEAKER_PATTERNS:
        m = pat.match(head)
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        if b == "위원":  # (이름, '위원') 순서
            role, name = "위원", a
        else:
            role, name = a, b
        return fix_role_qualifier(role, name, head[m.end():].strip())
    return None


def _extract_agendas(preamble: str) -> list[str]:
    agendas: list[str] = []
    for line in preamble.split("\n"):
        m = AGENDA_LINE.match(line)
        if m:
            title = re.sub(r"\s+", " ", m.group(2)).strip(" ·.")
            if title and title not in agendas:
                agendas.append(title)
    return agendas


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def parse_minutes(raw_text: str) -> MinutesDoc:
    text = clean_text(raw_text)
    parts = SPEAKER_MARK.split(text)
    preamble, chunks = parts[0], parts[1:]
    doc = MinutesDoc(preamble=preamble.strip(), agendas=_extract_agendas(preamble))
    agenda_keys = [_norm(a)[:25] for a in doc.agendas]

    current_agenda: int | None = 0 if doc.agendas else None
    for chunk in chunks:
        parsed = parse_speaker(chunk)
        if not parsed:
            # 발언자 판별 실패 → 직전 발언에 이어붙임
            if doc.utterances:
                doc.utterances[-1].text += " " + chunk.strip()
            continue
        role, name, body = parsed

        # 발언 중간에 등장하는 단독 안건 제목 줄 → 이후 발언의 안건 전환
        kept_lines = []
        next_agenda = None
        for line in body.split("\n"):
            m = AGENDA_LINE.match(line)
            if m and agenda_keys:
                key = _norm(m.group(2))[:25]
                if key in agenda_keys:
                    next_agenda = agenda_keys.index(key)
                    continue
            kept_lines.append(line)
        body = clean_utterance_text(TIME_NOTE.sub(" ", " ".join(kept_lines)))

        stype = classify_role(role)
        utt = Utterance(
            idx=len(doc.utterances),
            speaker_name=name,
            speaker_role=role,
            speaker_type=stype,
            org=org_from_role(role) if stype in ("official", "witness", "reference", "other") else None,
            text=body,
            agenda_idx=current_agenda,
        )
        if body:
            doc.utterances.append(utt)
        if next_agenda is not None:
            current_agenda = next_agenda

    annotate(doc.utterances)
    return doc


def annotate(utts: list[Utterance]) -> None:
    for u in utts:
        if u.speaker_type in ("member", "chair"):
            u.is_question = bool(QUESTION_PATTERNS.search(u.text)) and u.speaker_type == "member"
            u.is_data_request = bool(DATA_REQUEST_PATTERNS.search(u.text))
        if u.speaker_type in ("official", "witness"):
            u.is_commitment = is_commitment(u.text)
