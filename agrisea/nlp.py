"""외부 형태소분석기 없이 동작하는 경량 한국어 처리: 키워드 추출, 추출 요약."""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from typing import Iterable

from .lexicon import ISSUES, match_issues

JOSA = sorted([
    "으로부터", "에서부터", "이라는", "라는", "에게서", "께서는", "에서는", "으로는", "이라고",
    "라고", "에서", "에게", "께서", "으로", "부터", "까지", "처럼", "보다", "이나", "하고",
    "과는", "와는", "에는", "은", "는", "이", "가", "을", "를", "에", "의", "로", "과", "와",
    "도", "만", "나", "요",
], key=len, reverse=True)
ENDINGS = ("하겠습니다", "했습니다", "합니까", "입니까", "습니까", "합니다", "입니다", "습니다", "됩니다", "있습니다", "없습니다",
           "하는", "했던", "하고", "해서", "하면", "했고", "되는", "해야", "님", "해", "된", "한", "할")
STOPWORDS = set("""
그리고 그래서 그런데 그러나 하지만 그러면 이것 저것 그것 여기 거기 저기 지금 오늘 우리 저희
말씀 생각 부분 문제 관련 경우 정도 이런 저런 그런 이렇게 그렇게 어떻게 있는 없는 하는
장관 장관님 청장 청장님 위원 위원님 위원장 위원장님 의원 질의 답변 존경하는 감사 감사합니다
네 예 아니 그게 이제 좀 많이 아주 너무 계속 다시 사실 정말 진짜 한번 이거 그거 저거
것은 것이 것을 것으로 때문 대해 대한 통해 위해 따라 같은 다른 모든 각각 해당 어떤 무슨
있습니다 없습니다 합니다 됩니다 하겠습니다 드리겠습니다 말씀드리겠습니다 그렇습니다 맞습니다
않습니까 아닙니까 없습니까 있습니까 됩니까 무엇 어디 언제 왜 주십시오 바랍니다 국정감사 실시 다음 이상 올해 내년 이후 수준 대상 여쭙겠습니다 공감합니다
대해서 관련해서 관련된 페이지 저희들 저희가 하고 하는데 해서 되는 있고 있는데 있다 같습니다 겁니다 거고요 그다음 그래서요
하겠 위원님들 일부개정법률안 내용 의견 필요 사항 부분들 그렇지요 그렇죠 이게 그건 이건 뭐 여러 가지 거기에 여기에 위원님께서 차관님 차관 보고 설명
""".split())
SENT_SPLIT = re.compile(r"(?<=[.?!])\s+")
WORD = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9]+|\d+(?:\.\d+)?%?")

_DOMAIN_TERMS = sorted({k for _, _, kws in ISSUES.values() for k in kws}, key=len, reverse=True)
_DOMAIN_SET = frozenset(_DOMAIN_TERMS)
_SPACED_TERMS = [t for t in _DOMAIN_TERMS if " " in t]


# 같은 낱말이 수없이 반복되므로(브리핑 한 번에 백만 회 이상) 결과를 기억해 둔다
@lru_cache(maxsize=1 << 18)
def strip_josa(tok: str) -> str:
    for e in ENDINGS:
        if tok.endswith(e) and len(tok) - len(e) >= 2:
            return tok[: -len(e)]
    for j in JOSA:
        if tok.endswith(j) and len(tok) - len(j) >= 2:
            return tok[: -len(j)]
    return tok


def tokenize(text: str) -> list[str]:
    toks: list[str] = []
    # 띄어쓰기가 들어간 도메인 복합어는 통째로 보존
    for term in _SPACED_TERMS:
        if term in text:
            toks.extend([term] * text.count(term))
    toks.extend(t for t in map(_word_token, WORD.findall(text)) if t)
    return toks


@lru_cache(maxsize=1 << 18)
def _word_token(w: str) -> str:
    """낱말 하나를 토큰으로(숫자·불용어·한 글자는 빈 문자열)."""
    if w[0].isdigit():
        return ""
    w = strip_josa(w)
    return "" if len(w) < 2 or w in STOPWORDS else w


def split_sentences(text: str) -> list[str]:
    sents = [s.strip() for s in SENT_SPLIT.split(text) if s and s.strip()]
    return [s for s in sents if len(s) >= 8]


def keywords(texts: Iterable[str], top_k: int = 15,
             background: Counter | None = None, n_docs: int = 1,
             exclude: Iterable[str] = ()) -> list[tuple[str, float]]:
    """TF(·IDF) 기반 키워드. background(문서빈도)가 주어지면 IDF 가중, exclude(발언자명 등) 제외."""
    tf = Counter()
    for t in texts:
        tf.update(tokenize(t))
    skip = set(exclude)
    scored = []
    for w, c in tf.items():
        if w in skip:
            continue
        idf = math.log((n_docs + 1) / (background.get(w, 0) + 1)) + 1 if background else 1.0
        bonus = 1.5 if w in _DOMAIN_SET else 1.0
        scored.append((w, c * idf * bonus))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


# 인사·의례 문장(요약 대상에서 뒤로 미룸)
COURTESY = re.compile(r"(감사드립니다|감사합니다|수고하셨습니다|수고 많으셨습니다|반갑습니다|인사(를|말씀)|"
                      r"바쁘신 (일정|가운데)|성원이 되었으므로|개의하겠습니다|산회를 선포|"
                      r"보고(를)? 드리겠습니다|말씀드리겠습니다|의사일정 제\d+항)")


# 요약에 넣지 않을 문장: 의안 회부 목록('…(의안번호2220471) 8월10일 회부됨 …') 같은 PDF 잔여물
BILL_LIST = re.compile(r"의안번호|대표발의|회부[됨함]|\(\d{4}\.\s?\d{1,2}\.\s?\d{1,2}\.")
MAX_SENT = 350      # 이보다 긴 '문장'은 문장부호 없이 이어진 목록·표
MIN_HANGUL = 12     # '3번 사업과 같은 내용입니다', '예, 맞습니다' 같은 짧은 대답 제외


def is_summary_noise(s: str) -> bool:
    if len(s) > MAX_SENT or len(BILL_LIST.findall(s)) >= 2:
        return True
    return len(re.findall(r"[가-힣]", s)) < MIN_HANGUL


def extractive_summary(text: str, n: int = 3, focus: Iterable[str] = ()) -> list[str]:
    """중요 문장 n개를 원문 순서대로 반환(키워드 밀도 + 도메인 용어 + 수치 가중)."""
    sents = [s for s in split_sentences(text) if not is_summary_noise(s)]
    if len(sents) <= n:
        return sents
    freq = Counter(tokenize(text))
    focus = set(focus)
    scores = []
    for i, s in enumerate(sents):
        toks = tokenize(s)
        if not toks:
            scores.append((0.0, i))
            continue
        base = sum(freq[t] for t in toks) / (len(toks) ** 0.6)
        domain = sum(1 for t in toks if t in _DOMAIN_SET or t in focus)
        numeric = 0.5 if re.search(r"\d", s) else 0.0
        issue = 0.5 * len(match_issues(s))
        score = base + domain + numeric + issue
        if COURTESY.search(s):
            score *= 0.2
        scores.append((score, i))
    top = sorted(scores, reverse=True)[:n]
    return [sents[i] for _, i in sorted(top, key=lambda x: x[1])]


def truncate(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
