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
AGENDA_LINE = re.compile(r"^\s*(\d{1,3})\s*\.\s*(.{4,120}?)\s*(?:[·.…]{3,}\s*\d+)?\s*$")
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
    # 일괄 상정: 발언 속 안건 줄이 잇달아 나온 묶음. {묶음 끝 안건 자리(발언의 agenda_idx): 묶음 안건 자리들}
    agenda_groups: dict[int, list[int]] = field(default_factory=dict)
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


# 회의록 끝 부록: 산회 뒤에 출석 위원·출석 전문위원·정부측 참석자 명단, 증인·참고인 명단, 소위원회 구성 명단,
# 【보고사항】(의안 회부 목록)이 붙는다. 발언이 아니라서 떼어 낸다. 그대로 두면 마지막 위원장 발언에 붙어
# 쟁점·기관 언급을 부풀리고(명단의 기관명·의안명), 부록 속 '◯출석 전문위원'이 가짜 발언자 행이 된다.
APPENDIX_START = re.compile(r"출\s*석\s*위\s*원\s*\(\s*\d+\s*인\s*\)")
# 정회로 끝난 회의는 '(계속 개의되지 않았음)'까지 본문. PDF 원문에서는 그 사이에 '(11시40분 회의중지)' 같은
# 시각 표기가 끼어 있다(발언별 TIME_NOTE 제거는 나중에 하므로 여기서 건너뛴다).
MEETING_CLOSE = re.compile(
    r"(?:산\s*회|정\s*회)\s*[를을]?\s*선\s*포\s*(?:합\s*니\s*다|하\s*겠\s*습\s*니\s*다)\s*\.?"
    r"(?:(?:\s*\(\s*\d{1,2}\s*시[^()]{0,16}\))?\s*\(\s*계\s*속\s*개\s*의\s*되\s*지\s*않\s*았\s*음\s*\))?"
    r"|산\s*회\s*하\s*겠\s*습\s*니\s*다\s*\.?")
# 부록(명단·표)에는 없는 말끝. 부록 자리 뒤에 이런 문장이 있으면 발언이 이어지는 것으로 보고 자르지 않는다.
SPOKEN_ENDING = re.compile(r"습\s*니\s*다|니\s*까\s*\?|십\s*시\s*오")


def split_appendix(text: str) -> tuple[str, str]:
    """(본문, 부록)으로 나눈다. 부록은 '출석위원(N인)'부터인데, 그 앞에 산회·정회 선포가 있고 그 사이에 발언
    문장이 없으면(증인 명단·소위원회 구성 명단 같은 표만 있으면) 선포 바로 뒤부터 부록으로 본다.
    부록 자리 뒤에 발언 문장이 있으면 회의가 끝난 것이 아니므로 나누지 않는다."""
    m = APPENDIX_START.search(text)
    if not m or SPOKEN_ENDING.search(text, m.start()):
        return text, ""
    cut = m.start()
    closes = list(MEETING_CLOSE.finditer(text, 0, m.start()))
    if closes and not SPOKEN_ENDING.search(text, closes[-1].end(), m.start()):
        cut = closes[-1].end()
    return text[:cut].rstrip(), text[cut:].strip()


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


# 목차 줄 끝의 점선과 쪽 번호: '출석 요구의 건 ········ 1 1'
_LEADER_TAIL = re.compile(r"(?:\s*[·.…‧∙・]){3,}[\s\d]*$")


def _agenda_entries(preamble: str) -> list[tuple[str, str]]:
    """회의록 첫머리(의사일정·상정된 안건)의 (번호, 안건) 목록. 발언의 agenda_idx 는 이 목록의 자리다.
    의사일정과 상정된 안건에 같은 안건이 띄어쓰기·점선·쪽 번호만 달리 두 번 나오므로 한 번만 넣는다."""
    entries: list[tuple[str, str]] = []
    keys: set[str] = set()
    for line in preamble.split("\n"):
        m = AGENDA_LINE.match(line)
        if m:
            title = _LEADER_TAIL.sub("", re.sub(r"\s+", " ", m.group(2))).strip(" ·.")
            if title and _norm(title) not in keys:
                keys.add(_norm(title))
                entries.append((m.group(1), title))
    return entries


def _extract_agendas(preamble: str) -> list[str]:
    return [t for _, t in _agenda_entries(preamble)]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


_AGENDA_NO = re.compile(r"^\s*(?:\d{1,3}|[가-힣])\s*\.\s*")
_BILL_NO = re.compile(r"의안번호\s*(\d{5,})")


# 안건 줄 사이에 끼어도 묶음을 끊지 않는 줄(줄바꿈된 제목 끝·쪽 머리글·시각)과 끊는 발언 문장
_SPOKEN = re.compile(r"[가-힣][다요까죠]\s*[.?!]|습니다|합니다")   # 줄 첫머리 항목 표시('다.')는 문장 끝이 아님
# 안건 줄 뒤에 붙는 제목 조각: 줄바꿈된 제목 끝('2202041)', '대표발의)(의안번호 2206845)', '…(의안'),
# 아래 항목('가. 농림축산식품부 소관', '- 마산항 재해 취약지구 정비사업'), 목차 점선
_BILL_REF = re.compile(r"(?:의안|청원)\s*번\s*호|대표\s*발의\s*\)|정부\s*제출\s*\)")
_TITLE_TAIL = re.compile(r"^[^.?!]{0,90}?(?:\d{5,}\s*\)|\(\s*(?:의안|청원)(?:\s*번\s*호?)?)(?:\s*[·.…]{3,})?[\s\d]*$")
_SUB_ITEM = re.compile(r"^\s*(?:[가-하]\s*\.|-)\s*\S[^.?!]{0,50}$")
_LEADER_ONLY = re.compile(r"^\s*(?:[·.…]\s*){3,}\d*\s*$")
_BILL_WORDS = re.compile(r"의원|대표\s*발의|(?:의안|청원)?\s*번\s*호|정부\s*제출|[()\s·.…\d]")


def _bill_fragment(line: str) -> bool:
    """의안번호 조각만 있는 줄('2202801)', '의원 대표발의)(의안번호 2202635)', '번호2205814)'): 발언일 수 없다."""
    t = _LEADER_TAIL.sub("", line).strip()
    return len(t) <= 60 and bool(re.search(r"\d{5,}\s*\)", t)) and len(_BILL_WORDS.sub("", t)) <= 4


def agenda_labels(agendas: list[str], groups: dict) -> list[str]:
    """발언에 붙일 안건 이름. 한꺼번에 상정한 묶음의 끝 안건 자리에는 '묶음에서 목록 순서가 가장 앞선 안건 외 N건'을 붙인다."""
    out = list(agendas)
    for k, members in (groups or {}).items():
        i = int(k)
        first = min(members) if members else -1
        if 0 <= i < len(out) and len(members) > 1 and 0 <= first < len(agendas):
            out[i] = f"{agendas[first]} 외 {len(members) - 1}건"
    return out


def align_agendas(minutes: list[str], api: list[str]) -> list[str]:
    """회의 안건 목록 = 회의록 첫머리 순서(발언의 agenda_idx 기준). 표기는 같은 안건의 API 문구가 있으면 그것을
    쓴다(PDF 추출 문구는 띄어쓰기가 깨지기 쉬움). 짝은 의안번호, 없으면 띄어쓰기·번호를 뺀 제목으로 찾는다."""
    key = lambda s: re.sub(r"[\s·ㆍ.,]", "", _AGENDA_NO.sub("", s))  # noqa: E731
    pool = list(api)
    out = []
    for i, a in enumerate(minutes):
        b, k = _BILL_NO.search(a), key(a)
        hit = next((x for x in pool if b and (bx := _BILL_NO.search(x)) and bx.group(1) == b.group(1)), None)
        if hit is None and k:
            hit = next((x for x in pool if key(x)[:40] == k[:40]), None)
        if hit is None and len(k) >= 12:  # 줄바꿈으로 '(의안번호'에서 끊긴 회의록 제목 등: 한쪽이 다른 쪽의 앞부분
            hit = next((x for x in pool if len(key(x)) >= 12 and (key(x).startswith(k) or k.startswith(key(x)))),
                       None)
        if hit is not None:
            pool.remove(hit)
        out.append(f"{i + 1}. " + (_AGENDA_NO.sub("", hit).strip() if hit is not None else a))
    return out


# 표지 첫 줄('제426회국회'). 첫 쪽이 두 단이면 PDF에서 본문 첫 단이 먼저 나오고 표지(회의록 제목·일시·
# 의사일정·상정된 안건)가 그 뒤, 첫 발언 속에 끼어 나온다.
COVER_START = re.compile(r"^\s*제\s*\d+\s*회\s*국\s*회\s*$", re.M)
_HEADER_LINE = re.compile(r"^[^\S\n]*(?:" + RUNNING_HEADER.pattern + r")[^\S\n]*$", re.M)


def lift_cover(text: str) -> str:
    """첫 발언 뒤에 끼어 나온 표지를 회의록 첫머리로 옮긴다. 표지는 다음 쪽 머리말 줄이나 다음 발언자 표시
    앞까지다(그 뒤는 끊겼던 본문이 이어진다)."""
    first = SPEAKER_MARK.search(text)
    cover = COVER_START.search(text, first.end()) if first else None
    if not cover or "회의록" not in re.sub(r"\s", "", text[cover.start():cover.start() + 300]):
        return text
    ends = [m.start() for m in (_HEADER_LINE.search(text, cover.end()), SPEAKER_MARK.search(text, cover.end())) if m]
    end = min(ends) if ends else len(text)
    return text[cover.start():end] + "\n" + text[:cover.start()] + text[end:]


def parse_minutes(raw_text: str) -> MinutesDoc:
    text, _appendix = split_appendix(lift_cover(clean_text(raw_text)))
    parts = SPEAKER_MARK.split(text)
    preamble, chunks = parts[0], parts[1:]
    entries = _agenda_entries(preamble)
    doc = MinutesDoc(preamble=preamble.strip(), agendas=[t for _, t in entries])
    agenda_keys = [_norm(a)[:25] for a in doc.agendas]

    def agenda_of(no: str, title: str, after: int | None) -> int | None:
        """발언 속 안건 줄 → 안건 자리. 앞부분이 같은 안건(같은 법률의 개정안 여러 건)은 번호로, 번호도 같으면
        직전 안건 다음 자리로 가린다."""
        key = _norm(title)[:25]
        cands = [i for i, k in enumerate(agenda_keys) if k == key]
        if len(cands) > 1:
            cands = [i for i in cands if entries[i][0] == no] or [i for i in cands if after is None or i > after] \
                or cands
        return cands[0] if cands else None

    current_agenda: int | None = 0 if doc.agendas else None
    for chunk in chunks:
        parsed = parse_speaker(chunk)
        if not parsed:
            # 발언자 판별 실패 → 직전 발언에 이어붙임
            if doc.utterances:
                doc.utterances[-1].text += " " + chunk.strip()
            continue
        role, name, body = parsed

        # 발언 중간에 등장하는 단독 안건 제목 줄 → 이후 발언의 안건 전환.
        # 사이에 발언 문장 없이 잇달아 나온 안건 줄은 한꺼번에 상정한 묶음이다(마지막 줄이 이후 발언의 안건).
        # 안건 줄과 그 뒤에 붙은 제목 조각(줄바꿈된 제목 끝·아래 항목·목차 점선)은 발언 본문이 아니므로 뺀다.
        kept_lines: list[str] = []
        next_agenda = None
        run: list[int] = []
        spoken = False
        heading = False   # 바로 앞 줄이 안건 줄(또는 그 조각)이었나
        lines = body.split("\n")
        for li, line in enumerate(lines):
            m = AGENDA_LINE.match(line)
            if m and agenda_keys:
                ai = agenda_of(m.group(1), m.group(2), current_agenda if next_agenda is None else next_agenda)
                if ai is not None:
                    if spoken:
                        run, spoken = [], False
                    if ai not in run:
                        run.append(ai)
                    next_agenda = ai
                    heading = True
                    continue
            nxt = lines[li + 1] if li + 1 < len(lines) else ""
            if m and not _SPOKEN.search(line) and (_BILL_REF.search(line) or _bill_fragment(nxt)):
                heading = True   # 목록에 없는 의안 줄(번호가 붙은 의안 제목): 안건 전환은 아니지만 본문도 아님
                continue
            if _bill_fragment(line):
                heading = True
                continue
            if heading and not _SPOKEN.search(line) and (_TITLE_TAIL.match(line) or _SUB_ITEM.match(line)
                                                         or _LEADER_ONLY.match(line)):
                continue
            heading = False
            if _SPOKEN.search(line):
                spoken = True
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
            if len(run) > 1 and len(run) > len(doc.agenda_groups.get(next_agenda, ())):
                doc.agenda_groups[next_agenda] = run

    annotate(doc.utterances)
    return doc


def annotate(utts: list[Utterance]) -> None:
    for u in utts:
        if u.speaker_type in ("member", "chair"):
            u.is_question = bool(QUESTION_PATTERNS.search(u.text)) and u.speaker_type == "member"
            u.is_data_request = bool(DATA_REQUEST_PATTERNS.search(u.text))
        if u.speaker_type in ("official", "witness"):
            u.is_commitment = is_commitment(u.text)
