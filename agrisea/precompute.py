"""수집 후 사전 계산: 회의 요약, 쟁점·기관·관계망 집계, 지식그래프(N-Triples).

배포 환경(서버리스)은 요청마다 무거운 계산을 할 수 없어, 자동 수집 단계에서 미리 만들어
초기 데이터(seed)와 data/kg.nt.gz 에 함께 싣는다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from . import analysis, followup, governance, members
from .lexicon import ISSUES
from .ontology import build_graph, to_ntriples_gz
from .store import Store

# 사전 계산 형식이 바뀌면 올린다(초기 데이터를 다시 만들게 함).
# 2: 발언자 역할 재분류, 회기별 쟁점·개요 집계 / 3: 띄어쓰기 교정 / 4: 요약에서 의안 목록 제외 / 5: 위원별 분석
# 6: 정부 약속 이행 추적 / 7: 운영 관문 정책(차단 대상어) 반영 / 8: 주요 기관 브리핑
# 9: 브리핑 바로가기 버튼 전체(기관·쟁점·위원)
PRECOMPUTE_VERSION = 9


def current_policy() -> dict:
    """자동 수집 단계의 정책: 운영 관문 원장(AGRISEA_GOVERNANCE_LEDGER, governance-log 브랜치에서 받은 파일)의 최신 정책."""
    return governance.policy_from_file(os.environ.get("AGRISEA_GOVERNANCE_LEDGER"))

CACHED_VIEWS: dict[str, Callable[[Store], object]] = {
    "issues": analysis.issue_overview,
    "orgs": analysis.org_overview,
    "graph": analysis.issue_network,
    "speakers": lambda s: s.speakers(),
    "sessions": lambda s: s.sessions(),
    "issue_by_session": analysis.issue_by_session,
    "overview": analysis.overview,
    "members_all": members.member_profiles,  # 전체 회기 위원별 분석(가장 무거움: 사전 계산)
    "followups": lambda s: followup.track_all(s, current_policy()),  # 정부 약속 이행 추적(정책 반영)
}

# 국감 브리핑 화면의 바로가기 버튼(index.html briefStarter)과 같은 대상을 전체 회기로 미리 만든다:
# 언급 많은 기관 6곳, 하위 쟁점 앞 8개, 질의 많은 위원 6명
BRIEFING_TOP_ORGS = 6
BRIEFING_TOP_ISSUES = 8
BRIEFING_TOP_MEMBERS = 6


def briefing_key(kind: str, value: str) -> str:
    """kind: org / issue / member 중 하나만 고른 전체 회기 브리핑."""
    return f"briefing:{kind}:{value}"


def top_orgs(orgs: list[dict], n: int = BRIEFING_TOP_ORGS) -> list[str]:
    """org_overview 결과에서 언급 많은 기관 n곳(화면 버튼과 같은 순서)."""
    return [o["name"] for o in sorted((o for o in orgs if o["mentions"]),
                                      key=lambda o: -o["mentions"])[:n]]


def top_issues(n: int = BRIEFING_TOP_ISSUES) -> list[str]:
    """쟁점 분류 순서대로 하위 쟁점 n개(화면 버튼과 같은 순서)."""
    return [iid for iid, (_, parent, _) in ISSUES.items() if parent][:n]


def top_members(speakers: list[dict], n: int = BRIEFING_TOP_MEMBERS) -> list[str]:
    """질의 많은 위원 n명(화면 버튼과 같은 순서)."""
    return [s["speaker_name"] for s in sorted((s for s in speakers if s["speaker_type"] == "member"),
                                              key=lambda s: -(s["n_questions"] or 0))[:n]]


def quick_briefings(store: Store) -> list[tuple[str, str]]:
    """브리핑 화면 바로가기 버튼 대상 (kind, value). 집계(orgs·speakers)를 먼저 저장한 뒤 부른다."""
    return ([("org", o) for o in top_orgs(store.kv_get("orgs"))]
            + [("issue", i) for i in top_issues()]
            + [("member", m) for m in top_members(store.kv_get("speakers"))])


def briefing_for(store: Store, kind: str, value: str) -> dict:
    return analysis.briefing(store, **{kind: value})


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


def reclassify(store: Store) -> int:
    """저장된 발언의 발언자 유형·소속·질의/약속 표시를 현재 규칙으로 다시 계산(PDF 재수집 없이)."""
    import json

    from .lexicon import (DATA_REQUEST_PATTERNS, QUESTION_PATTERNS, classify_role, is_commitment,
                          match_issues, match_organizations, org_from_role)
    from .parser import clean_utterance_text, fix_role_qualifier
    rows = store.conn.execute("SELECT id, speaker_role, speaker_name, speaker_type, org, text, "
                              "is_question, is_commitment, is_data_request, issues_json, orgs_json "
                              "FROM utterances").fetchall()
    updates = []
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
        new = (role, name, stype, org, q, c, d, text, issues, orgs)
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
    spaced = fix_spacing(store, progress)
    fixed = reclassify(store)
    n = 0
    for m in store.meetings(status="parsed"):
        if store.summary(m["id"], "rule") is None:
            store.save_summary(m["id"], "rule", analysis.summarize_meeting(store, m["id"]))
            n += 1
    for key, fn in CACHED_VIEWS.items():
        store.kv_set(key, fn(store))
    briefed = quick_briefings(store)
    for kind, value in briefed:
        store.kv_set(briefing_key(kind, value), briefing_for(store, kind, value))
    out = {"spacing": spaced, "reclassified": fixed, "summaries": n, "views": list(CACHED_VIEWS),
           "briefings": [f"{k}:{v}" for k, v in briefed]}
    if kg_path:
        g = build_graph(store, text_limit=500)
        to_ntriples_gz(g, kg_path)
        out["triples"] = len(g)
    progress(f"사전 계산: 발언자 재분류 {fixed}건, 회의 요약 {n}건, 집계 {len(CACHED_VIEWS)}종, "
             f"바로가기 브리핑 {len(briefed)}건"
             + (f", 지식그래프 트리플 {out['triples']}개" if kg_path else ""))
    return out
