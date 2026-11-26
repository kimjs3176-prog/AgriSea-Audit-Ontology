"""수집 후 사전 계산: 회의 요약, 쟁점·기관·관계망 집계, 지식그래프(원 데이터 + 추론, N-Quads).

배포 환경(서버리스)은 요청마다 무거운 계산을 할 수 없어, 자동 수집 단계에서 미리 만들어
초기 데이터(seed)와 data/kg.nq.gz 에 함께 싣는다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from . import analysis, followup, governance, members, programs, readable
from .ontology import build_kg, save_kg
from .store import Store

# 사전 계산 형식이 바뀌면 올린다(초기 데이터를 다시 만들게 함).
# 2: 발언자 역할 재분류, 회기별 쟁점·개요 집계 / 3: 띄어쓰기 교정 / 4: 요약에서 의안 목록 제외 / 5: 위원별 분석
# 6: 정부 약속 이행 추적 / 7: 운영 관문 정책(차단 대상어) 반영
# 13: 온톨로지 검증(배타 클래스·정의역 수정, 인물 식별 통합, 추적 불가 판정)
# 14: 직무대리·직무대행·후보자 직함을 본래 직위로 분류(정부 답변·약속 누락 수정)
# 15: 소관 기관 5곳 추가(해양수산연수원·해양교통안전공단 등, 기관장 소속 식별)
# 16: 질의 대상 기관 식별(사람 단위 소속, 연달은 질의, 직함 호칭)
# 17: 회의 진행 질의·진술인 질의 분류(스키마 0.3)
# 18: 같은 질의 차례로 추정 질의 대상 기관(ag:likelyAddressedTo)
# 19: 회의 진행 표현 보강('1분도', '마무리 하시지요')
# 20: 회의록 끝 부록(출석 위원·참석자·증인 명단)을 마지막 발언에서 떼어 냄
# 21: '없어요'를 물음표가 있을 때만 질의로 판정
# 22: '검토해 보겠습니다'·'보고드리도록 하겠습니다' 등을 약속으로, '하겠습니다마는'은 제외
# 23: 대시보드·위원 분석·브리핑의 약속을 이행 추적 대상과 같은 기준으로
# 24: 일괄 상정 안건 묶음(첫 안건 외 N건), 앞부분이 같은 안건 줄 구분
# 25: 발언 본문에 남은 안건 제목 조각(줄바꿈된 제목 끝·아래 항목·목차 점선) 제거
# 26: 첫 발언에 끼어 있던 회의록 표지를 첫머리로(안건 목록·발언 안건 자리 복구)
# 27: 저장된 발언 본문의 의안 목록 조각(번호 붙은 의안 제목·의안번호 조각) 제거
# 28: 지식그래프에 일괄 상정 안건 묶음(ag:jointlyTakenUpWith, 스키마 0.4)
# 29: 약속은 답변의 하위 개념이 아님(질의 전 업무보고 속 약속은 답변 아님, 스키마 0.5)
PRECOMPUTE_VERSION = 32  # 32: 붙어 버린 기관 관계자 답변 분리, 정부 관계자 말 뒤 질의·위원 의견 뒤 질의 분류


def current_policy() -> dict:
    """자동 수집 단계의 정책: 운영 관문 원장(AGRISEA_GOVERNANCE_LEDGER, governance-log 브랜치에서 받은 파일)의 최신 정책."""
    return governance.policy_from_file(os.environ.get("AGRISEA_GOVERNANCE_LEDGER"))

CACHED_VIEWS: dict[str, Callable[[Store], object]] = {
    # 이행 추적을 먼저: 대시보드·위원 분석·브리핑이 같은 기준(추적 대상 약속)으로 약속을 보여 준다
    "followups": lambda s: followup.track_all(s, current_policy()),  # 정부 약속 이행 추적(정책 반영)
    "issues": analysis.issue_overview,
    "orgs": analysis.org_overview,
    "graph": analysis.issue_network,
    "speakers": lambda s: s.speakers(),
    "sessions": lambda s: s.sessions(),
    "issue_by_session": analysis.issue_by_session,
    "overview": analysis.overview,
    "members_all": members.member_profiles,  # 전체 회기 위원별 분석(가장 무거움: 사전 계산)
    analysis.briefing_cache_key(): analysis.briefing,  # 조건 없는 전체 국감 브리핑(가장 무거운 조합)
    "program_signals": programs.supply_signals,   # 쟁점별 국회 관심 대비 지원사업 공고 공급
    "readable": readable.glossary,                # '읽기 쉽게' 보기: 끊긴 낱말 잇기·기관 약칭·법률 이름
}


def fix_spacing(store: Store, progress: Callable[[str], None] = print) -> int:
    """아직 교정하지 않은 발언의 띄어쓰기를 고친다(규칙 버전이 오르면 전체를 다시 고친다).

    교정 모델은 전체 발언으로 학습하고, 교정은 발언마다 한 번만 한다(매번 다시 고치면 조금씩 흔들림).
    """
    from .parser import clean_utterance_text
    from .spacing import SPACING_VERSION, SpacingModel
    pending = store.conn.execute("SELECT id, text FROM utterances WHERE spacing < ?",
                                 (SPACING_VERSION,)).fetchall()
    if not pending:
        return 0
    texts = [clean_utterance_text(t) for (t,) in store.conn.execute("SELECT text FROM utterances")]
    model = SpacingModel.train(texts)
    updates = [(model.correct(clean_utterance_text(r["text"])), SPACING_VERSION, r["id"])
               for r in pending]
    with store.tx() as conn:
        conn.executemany("UPDATE utterances SET text=?, spacing=? WHERE id=?", updates)
        conn.execute("DELETE FROM kv")
        conn.execute("DELETE FROM summaries WHERE kind='rule'")
    changed = sum(1 for (new, _, _), r in zip(updates, pending) if new != r["text"])
    progress(f"띄어쓰기 교정: 대상 {len(pending)}건 중 {changed}건 수정")
    return changed


def strip_appendix(store: Store) -> int:
    """저장된 회의록에서 끝 부록(출석 위원·참석자·증인 명단 등)을 떼어 낸다(PDF 재수집 없이).

    부록은 마지막 위원장 발언에 붙어 있거나, 부록 속 '◯출석 전문위원' 때문에 가짜 발언자 행으로 갈라져 있다.
    부록이 시작되는 발언은 본문만 남기고, 그 뒤 행은 모두 부록이라 지운다. 부록 자리 뒤에 발언 문장이
    있는 회의는 건드리지 않는다. 바뀐(지운 것 포함) 행 수를 돌려준다.
    """
    from .parser import APPENDIX_START, SPOKEN_ENDING, split_appendix
    rows = store.conn.execute("SELECT id, meeting_id, idx, text FROM utterances "
                              "ORDER BY meeting_id, idx").fetchall()
    by_meeting: dict[str, list] = {}
    for r in rows:
        by_meeting.setdefault(r["meeting_id"], []).append(r)
    updates, deletes = [], []
    for utts in by_meeting.values():
        at = next((i for i, r in enumerate(utts) if APPENDIX_START.search(r["text"])), None)
        if at is None:
            continue
        head, appendix = split_appendix(utts[at]["text"])
        rest = utts[at + 1:]
        if not appendix or any(SPOKEN_ENDING.search(r["text"]) for r in rest):
            continue
        if head:
            updates.append((head, utts[at]["id"]))
        else:
            deletes.append((utts[at]["id"],))
        deletes.extend((r["id"],) for r in rest)
    if updates or deletes:
        with store.tx() as conn:
            conn.executemany("UPDATE utterances SET text=? WHERE id=?", updates)
            conn.executemany("DELETE FROM utterances WHERE id=?", deletes)
            conn.execute("DELETE FROM kv")
            conn.execute("DELETE FROM summaries WHERE kind='rule'")
    return len(updates) + len(deletes)


def strip_bill_fragments(store: Store) -> int:
    """발언 본문에 남은 의안 목록 조각(번호 붙은 의안 제목·의안번호 조각)을 지운다(회의록 원문 없이).
    앞뒤가 공백인 덩어리만 지우므로 문장 속 인용은 그대로 둔다. 바뀐 발언 수를 돌려준다."""
    from .parser import BILL_NOISE, strip_bill_noise
    updates = []
    for r in store.conn.execute("SELECT id, text FROM utterances").fetchall():
        if BILL_NOISE.search(r["text"]):
            new = strip_bill_noise(r["text"])
            if new != r["text"]:
                updates.append((new, r["id"]))
    if updates:
        with store.tx() as conn:
            conn.executemany("UPDATE utterances SET text=? WHERE id=?", updates)
            conn.execute("DELETE FROM kv")
            conn.execute("DELETE FROM summaries WHERE kind='rule'")
    return len(updates)


def reclassify(store: Store) -> int:
    """저장된 발언의 발언자 유형·소속·질의/약속 표시를 현재 규칙으로 다시 계산(PDF 재수집 없이)."""
    import json

    from .lexicon import (DATA_REQUEST_PATTERNS, QUESTION_PATTERNS, classify_role, is_commitment,
                          match_issues, match_organizations, org_from_role)
    from .parser import clean_utterance_text, fix_role_qualifier
    rows = store.conn.execute("SELECT id, speaker_role, speaker_name, speaker_type, org, text, "
                              "is_question, is_commitment, is_data_request, issues_json, orgs_json "
                              "FROM utterances").fetchall()
    computed = []
    for r in rows:
        role, name, text = fix_role_qualifier(r["speaker_role"], r["speaker_name"],
                                              clean_utterance_text(r["text"]))
        issues = json.dumps(match_issues(text), ensure_ascii=False)
        orgs = json.dumps(match_organizations(text), ensure_ascii=False)
        stype = classify_role(role)
        org = org_from_role(role) if stype in ("official", "witness", "reference", "other") else None
        q = int(stype == "member" and bool(QUESTION_PATTERNS.search(text)))
        d = int(stype in ("member", "chair") and bool(DATA_REQUEST_PATTERNS.search(text)))
        c = int(stype in ("official", "witness") and is_commitment(text))
        computed.append((r, [role, name, stype, org, q, c, d, text, issues, orgs]))
    # 직함에 기관이 없는 정부 관계자·증인(예: 증인으로 나온 장관)은 같은 이름이 다른 회의에서
    # 한 기관으로만 확인될 때 그 기관 소속으로 본다(기관이 둘 이상이면 동명이인일 수 있어 비워 둔다)
    known: dict[str, set[str]] = {}
    for _, (_, name, stype, org, *_rest) in computed:
        if stype in ("official", "witness") and org:
            known.setdefault(name, set()).add(org)
    updates = []
    for r, new in computed:
        if new[2] in ("official", "witness") and not new[3] and len(known.get(new[1], ())) == 1:
            new[3] = next(iter(known[new[1]]))
        new = tuple(new)
        if new != (r["speaker_role"], r["speaker_name"], r["speaker_type"], r["org"], r["is_question"],
                   r["is_commitment"], r["is_data_request"], r["text"], r["issues_json"], r["orgs_json"]):
            updates.append((*new, r["id"]))
    if updates:
        with store.tx() as conn:
            conn.executemany("UPDATE utterances SET speaker_role=?, speaker_name=?, speaker_type=?, "
                             "org=?, is_question=?, is_commitment=?, is_data_request=?, text=?, "
                             "issues_json=?, orgs_json=? WHERE id=?", updates)
            conn.execute("DELETE FROM kv")
            conn.execute("DELETE FROM summaries WHERE kind='rule'")
    return len(updates)


def precompute(store: Store, kg_path: Path | None = None,
               progress: Callable[[str], None] = print) -> dict:
    trimmed = strip_appendix(store)
    trimmed += strip_bill_fragments(store)
    spaced = fix_spacing(store, progress)
    fixed = reclassify(store)
    n = 0
    for m in store.meetings(status="parsed"):
        if store.summary(m["id"], "rule") is None:
            store.save_summary(m["id"], "rule", analysis.summarize_meeting(store, m["id"]))
            n += 1
    for key, fn in CACHED_VIEWS.items():
        store.kv_set(key, fn(store))
    out = {"appendix": trimmed, "spacing": spaced, "reclassified": fixed, "summaries": n,
           "views": list(CACHED_VIEWS)}
    if kg_path:
        from . import kgview, validate
        kg, counts = build_kg(store, text_limit=500)
        save_kg(kg, kg_path)
        summary = kgview.summary(kg)
        store.kv_set("ontology", summary)
        report = validate.validate(kg)
        store.kv_set("ontology_validation", report)
        out["validation"] = report["counts"]
        out["triples"] = summary["triples"]
        out["inferred"] = summary["inferred"]
    progress(f"사전 계산: 회의록 끝 부록·의안 목록 조각 정리 {trimmed}건, 발언자 재분류 {fixed}건, 회의 요약 {n}건, 집계 {len(CACHED_VIEWS)}종"
             + (f", 지식그래프 트리플 {out['triples']}개(추론 {out['inferred']}개)" if kg_path else ""))
    return out
