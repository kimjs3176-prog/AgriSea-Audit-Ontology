"""운영 관문(Gate): AI를 전제로 업무 시작점·처리경로·역할을 다시 설계하고, 모든 관문의 운영 증거를 남긴다.

적용 업무: 정부 약속 이행 판정(followup.py). 판정 하나가 '건(case)'이다.

  G1 접수       업무 시작점은 AI. 자동 수집 → 약속 인식 → 이행 판정을 AI가 먼저 수행하고 실행마다 run 이벤트를 남긴다.
  G2 위험평가   판정마다 위험 점수(판정 종류·조치 유형·근거 강도·근거 수)와 요인을 계산한다.
  G3 경로배정   저위험 → 자동 처리(그대로 공개), 고위험 → 숙련자 이관, 저위험 중 일부 → 표본 검증(자동 경로의 정확도 측정).
  G4 숙련자 검토 승인(AI 판정 확정) / 거부(올바른 판정·사유 코드·문제 대상어). 검토자·관리자 역할만 할 수 있다.
  G5 예외처리   롤백(직전 결정 취소, 사유 필수). 데이터가 바뀌어 AI 판정이 달라지면 이전 결정은 '재검토 필요'가 된다.
  G6 평가 데이터 결정마다 (AI 판정, 최종 판정, 일치, 경로, 위험 요인, 정책 버전) 한 줄 → 지표·내려받기.
  G7 제도 반영  쌓인 예외(거부 사유·문제 대상어·표본 불일치)에서 규칙 개정안을 만들고, 관리자가 채택하면
                새 정책 버전이 되어 다음 판정부터 적용된다(차단 대상어, 위험 기준, 표본 비율).

증거: 모든 사람·시스템 행위는 추가 전용 원장(ledger)의 이벤트다. 각 이벤트는 앞 이벤트의 해시를 품어
(hash chain) 중간 삭제·수정이 드러난다. 원장은 로컬 파일 또는 GitHub 브랜치(governance-log)에 영구 보관한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

# ── 정책(제도) ─────────────────────────────────────────────
DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "risk_threshold": 50,   # 이 점수 이상 → 숙련자 이관
    "sample_rate": 0.1,     # 저위험 중 표본 검증으로 돌릴 비율
    "keyword_block": [],    # 대상어로 쓰지 않을 말(오인으로 확인된 말)
    "note": "초기 정책",
}

GATES = [
    ("intake", "G1 접수", "AI가 수집·약속 인식·이행 판정을 먼저 수행"),
    ("risk", "G2 위험평가", "판정 종류·조치 유형·근거 강도로 위험 점수"),
    ("route", "G3 경로배정", "저위험 자동 처리 · 고위험 숙련자 이관 · 표본 검증"),
    ("review", "G4 숙련자 검토", "승인 또는 거부(정정 판정·사유)"),
    ("exception", "G5 예외처리", "롤백 · AI 판정 변경 시 재검토"),
    ("evaluation", "G6 평가 데이터", "결정마다 일치 여부를 평가 데이터로"),
    ("institution", "G7 제도 반영", "예외 → 규칙 개정 제안 → 승인 → 정책 버전"),
]

PATHS = {"auto": "자동 처리", "sampled": "표본 검증", "escalated": "숙련자 이관"}
STATES = {
    "auto_published": "자동 공개",
    "pending_sample": "표본 검증 대기",
    "pending_review": "숙련자 검토 대기",
    "approved": "승인(AI 판정 확정)",
    "corrected": "정정(숙련자 판정)",
    "stale": "재검토 필요(AI 판정 변경)",
}
REASONS = {
    "keyword": "대상어 오인(엉뚱한 근거와 연결)",
    "overclaim": "판정 과대(이행·진행이 아님)",
    "underclaim": "판정 과소(근거가 있는데 못 찾음)",
    "not_promise": "약속이 아님(인식 오류)",
    "org": "기관 불일치",
    "other": "기타",
}
EVENT_KINDS = {
    "run": "intake", "decision": "review", "rollback": "exception",
    "policy": "institution", "proposal_dismissed": "institution",
}
MIN_SUPPORT = 2  # 같은 대상어가 이만큼 거부 근거로 지목되면 차단 제안


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def event_hash(ev: dict) -> str:
    body = {k: ev.get(k) for k in ("seq", "ts", "kind", "actor", "role", "case", "payload", "prev")}
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()


def verify_chain(events: list[dict]) -> dict:
    """해시 연결 검증: 순번이 이어지고, 각 이벤트의 prev가 앞 이벤트 해시이며, 해시가 내용과 맞는지."""
    prev = ""
    for i, ev in enumerate(events):
        if ev.get("seq") != i or ev.get("prev") != prev or ev.get("hash") != event_hash(ev):
            return {"ok": False, "events": len(events), "broken_at": i}
        prev = ev["hash"]
    return {"ok": True, "events": len(events), "head": prev[:12] if prev else ""}


# ── 원장 저장소 ────────────────────────────────────────────
class MemorySink:
    durable = False
    kind = "memory"

    def __init__(self, events: list[dict] | None = None):
        self._events = list(events or [])

    def load(self) -> list[dict]:
        return list(self._events)

    def commit(self, build: Callable[[list[dict]], dict]) -> list[dict]:
        ev = build(self._events)
        self._events.append(ev)
        return list(self._events)


class FileSink(MemorySink):
    """로컬 JSONL 파일(개발·상시 서버용). 서버리스(/tmp)에서는 재시작 때 사라지므로 영구 보관이 아니다."""
    kind = "file"

    def __init__(self, path: Path, durable: bool = True):
        self.path = Path(path)
        self.durable = durable

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def commit(self, build: Callable[[list[dict]], dict]) -> list[dict]:
        events = self.load()
        ev = build(events)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(_canon(ev) + "\n")
        return events + [ev]


class GitHubSink(MemorySink):
    """GitHub 저장소의 전용 브랜치에 JSONL로 커밋(영구 보관 + 커밋 이력 자체가 증거).

    여러 서버리스 인스턴스가 동시에 쓰면 sha 충돌(409)이 나므로 최신본을 다시 읽어 이어 붙인다.
    """
    kind = "github"
    durable = True
    API = "https://api.github.com"

    def __init__(self, repo: str, token: str, branch: str = "governance-log",
                 path: str = "governance/events.jsonl", http: requests.Session | None = None):
        self.repo, self.token, self.branch, self.path = repo, token, branch, path
        self.http = http or requests.Session()

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _get_file(self) -> tuple[list[dict], str | None]:
        r = self.http.get(f"{self.API}/repos/{self.repo}/contents/{self.path}",
                          params={"ref": self.branch}, headers=self._h(), timeout=(5, 20))
        if r.status_code == 404:
            return [], None
        r.raise_for_status()
        meta = r.json()
        content = meta.get("content") or ""
        if not content and meta.get("size"):  # 1MB가 넘으면 내용이 비어 오므로 blob으로
            b = self.http.get(f"{self.API}/repos/{self.repo}/git/blobs/{meta['sha']}", headers=self._h(), timeout=(5, 30))
            b.raise_for_status()
            content = b.json()["content"]
        text = base64.b64decode(content).decode("utf-8") if content else ""
        return [json.loads(x) for x in text.splitlines() if x.strip()], meta["sha"]

    def _ensure_branch(self) -> None:
        r = self.http.get(f"{self.API}/repos/{self.repo}/branches/{self.branch}", headers=self._h(), timeout=(5, 20))
        if r.status_code != 404:
            r.raise_for_status()
            return
        repo = self.http.get(f"{self.API}/repos/{self.repo}", headers=self._h(), timeout=(5, 20))
        repo.raise_for_status()
        base = repo.json()["default_branch"]
        ref = self.http.get(f"{self.API}/repos/{self.repo}/git/ref/heads/{base}", headers=self._h(), timeout=(5, 20))
        ref.raise_for_status()
        made = self.http.post(f"{self.API}/repos/{self.repo}/git/refs", headers=self._h(), timeout=(5, 20),
                              json={"ref": f"refs/heads/{self.branch}", "sha": ref.json()["object"]["sha"]})
        if made.status_code not in (201, 422):  # 422: 그사이 다른 인스턴스가 만듦
            made.raise_for_status()

    def load(self) -> list[dict]:
        return self._get_file()[0]

    def commit(self, build: Callable[[list[dict]], dict]) -> list[dict]:
        for attempt in range(5):
            events, sha = self._get_file()
            if sha is None and attempt == 0:
                self._ensure_branch()
            ev = build(events)
            text = "".join(_canon(e) + "\n" for e in events + [ev])
            body = {"message": f"governance: {ev['kind']} #{ev['seq']} ({ev['actor']})",
                    "content": base64.b64encode(text.encode("utf-8")).decode("ascii"), "branch": self.branch}
            if sha:
                body["sha"] = sha
            r = self.http.put(f"{self.API}/repos/{self.repo}/contents/{self.path}", headers=self._h(),
                              json=body, timeout=(5, 30))
            if r.status_code in (409, 422) and attempt < 4:  # 다른 인스턴스가 먼저 씀 → 다시 읽어 이어 붙임
                time.sleep(0.3 * (attempt + 1))
                continue
            r.raise_for_status()
            return events + [ev]
        raise RuntimeError("원장 기록 충돌이 계속됩니다")


class LedgerConflict(Exception):
    """조건부 기록의 전제가 최신 원장에서 깨짐(다른 인스턴스가 먼저 결정·채택함)."""


class Ledger:
    """추가 전용·해시 연결 원장. 읽기는 짧게 캐시하고(다른 인스턴스의 기록 반영), 쓰기는 저장소에 바로 커밋.

    payload 대신 함수(최신 이벤트 목록 → payload)를 주면 조건부 기록: 저장소가 충돌로 다시 시도할 때마다
    가장 최신 원장에 대해 다시 검사·계산하고, 전제가 깨지면 LedgerConflict로 기록하지 않는다.
    """

    def __init__(self, sink: MemorySink, max_age: float = 20.0):
        self.sink = sink
        self.max_age = max_age
        self._events: list[dict] = []
        self._loaded = 0.0
        self._lock = threading.Lock()
        self.error = ""

    def events(self) -> list[dict]:
        with self._lock:
            if not self._loaded or time.time() - self._loaded > self.max_age:
                try:
                    self._events, self.error = self.sink.load(), ""
                except Exception as e:  # 저장소를 못 읽어도 화면은 뜨게(마지막으로 읽은 원장 사용)
                    self.error = f"{type(e).__name__}: {e}"
                self._loaded = time.time()
            return list(self._events)

    def append(self, kind: str, actor: str, role: str, payload: dict | Callable[[list[dict]], dict],
               case: str | None = None) -> dict:
        def build(events: list[dict]) -> dict:
            body = payload(events) if callable(payload) else payload
            ev = {"seq": len(events), "ts": now_iso(), "kind": kind, "actor": actor, "role": role,
                  "case": case, "payload": body, "prev": events[-1]["hash"] if events else ""}
            ev["hash"] = event_hash(ev)
            return ev
        with self._lock:
            try:
                self._events = self.sink.commit(build)
            except LedgerConflict:
                self._loaded = 0.0  # 다음 읽기는 최신 원장으로
                raise
            self._loaded = time.time()
            return self._events[-1]


def ledger_from_env(data_dir: Path, serverless: bool, http: requests.Session | None = None) -> Ledger:
    repo, token = os.environ.get("AGRISEA_GOV_GITHUB_REPO", ""), os.environ.get("AGRISEA_GOV_GITHUB_TOKEN", "")
    if repo and token:
        return Ledger(GitHubSink(repo, token, os.environ.get("AGRISEA_GOV_BRANCH", "governance-log"), http=http))
    path = os.environ.get("AGRISEA_GOVERNANCE_LEDGER") or str(Path(data_dir) / "governance.jsonl")
    return Ledger(FileSink(Path(path), durable=not serverless))


# ── 역할(권한) ─────────────────────────────────────────────
def parse_tokens(spec: str) -> dict[str, str]:
    """'홍길동:토큰,김가람:토큰' → {토큰: 이름}"""
    out = {}
    for part in (spec or "").split(","):
        name, _, tok = part.strip().partition(":")
        if name and tok:
            out[tok.strip()] = name.strip()
    return out


class Roles:
    """검토자(숙련자)·관리자(제도 승인권자) 인증. 토큰이 하나도 없고 상시 서버(로컬)면 개발용 개방 모드."""

    def __init__(self, reviewers: str = "", admins: str = "", open_mode: bool = False):
        self.reviewers, self.admins = parse_tokens(reviewers), parse_tokens(admins)
        self.open_mode = open_mode and not (self.reviewers or self.admins)

    @classmethod
    def from_env(cls, serverless: bool) -> "Roles":
        return cls(os.environ.get("AGRISEA_REVIEWERS", ""), os.environ.get("AGRISEA_ADMINS", ""),
                   open_mode=not serverless or os.environ.get("AGRISEA_GOV_OPEN") == "1")

    def who(self, token: str | None) -> dict:
        if self.open_mode:
            return {"name": "로컬 운영자", "role": "admin", "open": True}
        token = token or ""
        for table, role in ((self.admins, "admin"), (self.reviewers, "reviewer")):
            for tok, name in table.items():
                if token and hmac.compare_digest(tok, token):
                    return {"name": name, "role": role, "open": False}
        return {"name": "", "role": "viewer", "open": False}

    @property
    def configured(self) -> bool:
        return bool(self.reviewers or self.admins)


# ── 정책 ──────────────────────────────────────────────────
def active_policy(events: list[dict]) -> dict:
    pol = next((e["payload"]["policy"] for e in reversed(events) if e["kind"] == "policy"), None)
    return {**DEFAULT_POLICY, **(pol or {})}


def policy_history(events: list[dict]) -> list[dict]:
    out = [{"version": 1, "ts": None, "actor": "시스템", "policy": DEFAULT_POLICY, "change": "초기 정책", "proposal": None}]
    for e in events:
        if e["kind"] == "policy":
            out.append({"version": e["payload"]["policy"]["version"], "ts": e["ts"], "actor": e["actor"],
                        "policy": e["payload"]["policy"], "change": e["payload"].get("change", ""),
                        "proposal": e["payload"].get("proposal"), "seq": e["seq"]})
    return out


def policy_from_file(path: str | None) -> dict:
    """사전 계산(자동 수집) 단계에서 쓸 정책: 원장 파일이 있으면 그 최신 정책."""
    if not path or not Path(path).exists():
        return dict(DEFAULT_POLICY)
    return active_policy(FileSink(Path(path)).load())


# ── G2 위험평가 · G3 경로배정 ─────────────────────────────────
STATUS_RISK = {"done": 45, "reraised": 45, "progress": 25, "none": 5, "early": 0}


def assess(item: dict, policy: dict) -> dict:
    """위험 점수와 요인. 이행·미이행을 단정하는 판정, 법·예산 약속, 약한 근거일수록 높다."""
    factors = [{"code": "status", "label": f"판정 '{item.get('status_label', item['status'])}'",
                "points": STATUS_RISK.get(item["status"], 10)}]
    if item.get("action") == "law":
        factors.append({"code": "action", "label": "법·제도 개정 약속", "points": 15})
    elif item.get("action") == "budget":
        factors.append({"code": "action", "label": "예산·지원 약속", "points": 10})
    ev = [e for e in item.get("evidence", []) if e.get("kind") in ("done", "progress", "reraised")]
    if item["status"] in ("done", "progress", "reraised"):
        if len(ev) == 1:
            factors.append({"code": "single", "label": "근거 1건뿐", "points": 10})
        minutes = [e for e in ev if e.get("source") == "minutes"]
        if minutes and max(e.get("score", 0) for e in minutes) < 3:
            factors.append({"code": "weak", "label": "대상어 일치가 적은 근거", "points": 15})
        if ev and all(e.get("source") == "agenda" for e in ev):
            factors.append({"code": "agenda_only", "label": "의원 발의 안건만 근거(정부 조치 아님)", "points": 10})
    score = min(100, sum(f["points"] for f in factors))
    return {"score": score, "factors": factors, "tier": "high" if score >= policy["risk_threshold"] else "low"}


def _sample(case_id: str, policy: dict) -> bool:
    """표본 추출은 재현 가능해야 하므로(증거) 건 번호·정책 버전의 해시로 정한다."""
    h = int(hashlib.sha256(f"{case_id}:{policy['version']}".encode()).hexdigest()[:8], 16)
    return (h % 10000) < policy["sample_rate"] * 10000


def route(case_id: str, risk: dict, policy: dict) -> str:
    if risk["tier"] == "high":
        return "escalated"
    return "sampled" if _sample(case_id, policy) else "auto"


def fingerprint(item: dict) -> str:
    """AI 판정의 지문: 판정과 근거가 같으면 같은 지문(데이터가 바뀌면 결정이 '재검토 필요'로)."""
    ev = sorted(f"{e.get('source')}|{e.get('kind')}|{e.get('date')}|{e.get('meeting_id') or e.get('title')}"
                for e in item.get("evidence", []))
    return hashlib.sha256(_canon([item["status"], ev]).encode()).hexdigest()[:16]


def case_id(item: dict) -> str:
    return f"followup:{item['id']}"


# ── G4·G5 결정 접기 ───────────────────────────────────────────
def decision_stacks(events: list[dict]) -> dict[str, list[dict]]:
    """건별 유효 결정 스택(롤백되면 빠진다)."""
    stacks: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e["kind"] == "decision":
            stacks[e["case"]].append(e)
        elif e["kind"] == "rollback":
            st = stacks.get(e["case"], [])
            target = e["payload"].get("target")
            if st and st[-1]["seq"] == target:
                st.pop()
    return stacks


def case_view(item: dict, policy: dict, stack: list[dict]) -> dict:
    """건 하나의 관문 상태: 위험·경로·결정·최종 판정."""
    cid = case_id(item)
    risk = assess(item, policy)
    path = route(cid, risk, policy)
    fp = fingerprint(item)
    active = stack[-1] if stack else None
    if active and active["payload"].get("fingerprint") != fp:
        state, final = "stale", item["status"]
    elif active:
        state = "approved" if active["payload"]["action"] == "approve" else "corrected"
        final = active["payload"].get("final_status") or item["status"]
    else:
        state = {"auto": "auto_published", "sampled": "pending_sample", "escalated": "pending_review"}[path]
        final = item["status"]
    return {"case": cid, "fingerprint": fp, "risk": risk, "path": path, "path_label": PATHS[path],
            "state": state, "state_label": STATES[state], "ai_status": item["status"], "final_status": final,
            "decision": active, "history_len": len(stack)}


# ── G6 평가 데이터 ───────────────────────────────────────────
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((c - m) / d, 3), round((c + m) / d, 3))


def dataset(items: list[dict], events: list[dict], policy: dict) -> list[dict]:
    """평가 데이터: 유효 결정 한 건이 한 줄(AI 판정 vs 숙련자 최종 판정)."""
    by_case = {case_id(x): x for x in items}
    rows = []
    for cid, stack in decision_stacks(events).items():
        if not stack:
            continue
        d = stack[-1]
        x = by_case.get(cid)
        p = d["payload"]
        rows.append({
            "case": cid, "decision_seq": d["seq"], "decided_at": d["ts"], "reviewer": d["actor"],
            "path": p.get("path"), "risk_score": p.get("risk_score"), "risk_factors": p.get("risk_factors"),
            "policy_version": p.get("policy_version"), "ai_status": p.get("ai_status"),
            "final_status": p.get("final_status"), "agree": p.get("action") == "approve",
            "reason": p.get("reason"), "flagged_keywords": p.get("keywords", []),
            "stale": bool(x) and p.get("fingerprint") != fingerprint(x),
            "org": x and x.get("org"), "action": x and x.get("action"), "promise_date": x and x.get("date"),
        })
    return sorted(rows, key=lambda r: r["decision_seq"])


def metrics(items: list[dict], events: list[dict], policy: dict) -> dict:
    stacks = decision_stacks(events)
    views = [case_view(x, policy, stacks.get(case_id(x), [])) for x in items]
    rows = [r for r in dataset(items, events, policy) if not r["stale"]]
    paths = Counter(v["path"] for v in views)
    states = Counter(v["state"] for v in views)

    def agree(rs):
        k, n = sum(r["agree"] for r in rs), len(rs)
        return {"n": n, "agree": k, "rate": round(k / n, 3) if n else None, "ci95": wilson(k, n)}

    confusion: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        confusion[r["ai_status"]][r["final_status"]] += 1
    by_version: dict[int, list] = defaultdict(list)
    for r in rows:
        by_version[r["policy_version"]].append(r)
    total = len(views)
    return {
        "total": total, "paths": dict(paths), "states": dict(states),
        "auto_rate": round((paths["auto"] + paths["sampled"]) / total, 3) if total else None,
        "escalation_rate": round(paths["escalated"] / total, 3) if total else None,
        "reviewed": len(rows),
        "pending": states["pending_review"] + states["pending_sample"] + states["stale"],
        "agreement": agree(rows),
        "agreement_sampled": agree([r for r in rows if r["path"] == "sampled"]),
        "agreement_escalated": agree([r for r in rows if r["path"] == "escalated"]),
        "confusion": {a: dict(c) for a, c in confusion.items()},
        "reasons": dict(Counter(r["reason"] for r in rows if r["reason"])),
        "by_policy_version": {v: agree(rs) for v, rs in sorted(by_version.items())},
        "rollbacks": sum(1 for e in events if e["kind"] == "rollback"),
        "policies_adopted": sum(1 for e in events if e["kind"] == "policy"),
        "runs": sum(1 for e in events if e["kind"] == "run"),
    }


# ── G7 제도 반영: 예외 → 규칙 개정 제안 ─────────────────────────
def _pid(kind: str, value: Any) -> str:
    return hashlib.sha256(_canon([kind, value]).encode()).hexdigest()[:10]


def proposals(items: list[dict], events: list[dict], policy: dict) -> list[dict]:
    """쌓인 예외에서 규칙 개정안을 만든다(채택·기각된 제안은 다시 내지 않는다)."""
    closed = closed_proposals(events)
    rows = [r for r in dataset(items, events, policy) if not r["stale"]]
    out = []
    support: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if not r["agree"] and r["reason"] == "keyword":
            for k in r["flagged_keywords"]:
                support[k].append(r["decision_seq"])
    for kw, seqs in sorted(support.items(), key=lambda x: -len(x[1])):
        if len(seqs) >= MIN_SUPPORT and kw not in policy["keyword_block"]:
            out.append({"id": _pid("keyword_block", kw), "type": "keyword_block", "value": kw,
                        "title": f"대상어 「{kw}」 차단",
                        "rationale": f"숙련자 거부 {len(seqs)}건에서 엉뚱한 근거를 끌어온 말로 지목됨",
                        "support": seqs, "change": {"keyword_block": policy["keyword_block"] + [kw]}})
    sampled = [r for r in rows if r["path"] == "sampled"]
    escal = [r for r in rows if r["path"] == "escalated"]
    if len(sampled) >= 10:
        rate = sum(r["agree"] for r in sampled) / len(sampled)
        if rate < 0.85 and policy["risk_threshold"] > 20:
            val = policy["risk_threshold"] - 10
            out.append({"id": _pid("risk_threshold", val), "type": "risk_threshold", "value": val,
                        "title": f"위험 기준 {policy['risk_threshold']} → {val}(이관 확대)",
                        "rationale": f"표본 검증 일치율 {rate:.0%}(n={len(sampled)})로 자동 경로 정확도가 기준(85%)에 못 미침",
                        "support": [r["decision_seq"] for r in sampled if not r["agree"]],
                        "change": {"risk_threshold": val}})
        if rate < 0.9 and policy["sample_rate"] < 0.5:
            val = round(min(0.5, policy["sample_rate"] * 2), 2)
            out.append({"id": _pid("sample_rate", val), "type": "sample_rate", "value": val,
                        "title": f"표본 비율 {policy['sample_rate']:.0%} → {val:.0%}",
                        "rationale": f"표본 검증 일치율 {rate:.0%}(n={len(sampled)}) — 자동 경로 감시를 강화",
                        "support": [r["decision_seq"] for r in sampled if not r["agree"]],
                        "change": {"sample_rate": val}})
    if len(escal) >= 20:
        rate = sum(r["agree"] for r in escal) / len(escal)
        if rate >= 0.95 and policy["risk_threshold"] < 90:
            val = policy["risk_threshold"] + 10
            out.append({"id": _pid("risk_threshold", val), "type": "risk_threshold", "value": val,
                        "title": f"위험 기준 {policy['risk_threshold']} → {val}(자동화 확대)",
                        "rationale": f"숙련자 이관 건 일치율 {rate:.0%}(n={len(escal)}) — 검토 부담을 줄여도 됨",
                        "support": [r["decision_seq"] for r in escal[-20:]], "change": {"risk_threshold": val}})
    return [p for p in out if p["id"] not in closed]


def adopt(policy: dict, proposal: dict) -> dict:
    """제안을 주어진(최신) 정책 위에 얹은 새 버전. 그사이 다른 개정이 채택됐어도 그 위에 다시 계산한다."""
    t, v = proposal["type"], proposal["value"]
    if t == "keyword_block":
        if v in policy["keyword_block"]:
            raise LedgerConflict(f"「{v}」는 이미 차단돼 있습니다")
        change = {"keyword_block": policy["keyword_block"] + [v]}
    else:
        change = {t: v}
    return {**policy, **change, "version": policy["version"] + 1, "note": proposal["title"]}


def closed_proposals(events: list[dict]) -> set:
    return {e["payload"].get("proposal") for e in events if e["kind"] in ("policy", "proposal_dismissed")}


# ── 증거 보고서 ───────────────────────────────────────────────
def evidence_report(items: list[dict], events: list[dict], status: dict) -> str:
    policy = active_policy(events)
    m = metrics(items, events, policy)
    chain = verify_chain(events)
    runs = [e for e in events if e["kind"] == "run"]
    pct = lambda v: "-" if v is None else f"{v:.1%}"
    ag = m["agreement"]
    lines = [
        "# 운영 관문 증거 보고서", "",
        f"- 작성 시각: {now_iso()}",
        f"- 원장: 이벤트 {chain['events']}건 · 해시 연결 {'정상' if chain['ok'] else '깨짐(' + str(chain.get('broken_at')) + '번)'}"
        f" · 보관 {status['storage']}({'영구' if status['durable'] else '임시'})",
        f"- 현재 정책: v{policy['version']} (위험 기준 {policy['risk_threshold']}, 표본 비율 {policy['sample_rate']:.0%},"
        f" 차단 대상어 {len(policy['keyword_block'])}개)", "",
        "## G1 접수(업무 시작점 = AI)",
        f"- 자동 실행 기록 {len(runs)}건" + (f", 최근 {runs[-1]['ts']} (데이터 {runs[-1]['payload'].get('data_hash', '')[:12]}, 정책 v{runs[-1]['payload'].get('policy_version')})" if runs else ""),
        f"- 판정 대상 {m['total']}건", "",
        "## G2 위험평가 · G3 경로배정",
        f"- 자동 처리 {m['paths'].get('auto', 0)} · 표본 검증 {m['paths'].get('sampled', 0)} · 숙련자 이관 {m['paths'].get('escalated', 0)}",
        f"- 자동화율 {pct(m['auto_rate'])} · 이관율 {pct(m['escalation_rate'])}", "",
        "## G4 숙련자 검토 · G5 예외처리",
        f"- 검토 완료 {m['reviewed']}건 · 대기 {m['pending']}건 · 롤백 {m['rollbacks']}건",
        f"- 상태: " + ", ".join(f"{STATES[k]} {v}" for k, v in m["states"].items()), "",
        "## G6 평가 데이터",
        f"- AI–숙련자 일치율 {pct(ag['rate'])} (n={ag['n']}, 95% 구간 {ag['ci95']})",
        f"- 표본 검증(자동 경로 정확도) {pct(m['agreement_sampled']['rate'])} (n={m['agreement_sampled']['n']})",
        f"- 숙련자 이관 건 일치율 {pct(m['agreement_escalated']['rate'])} (n={m['agreement_escalated']['n']})",
        "- 거부 사유: " + (", ".join(f"{REASONS.get(k, k)} {v}" for k, v in m["reasons"].items()) or "없음"), "",
        "## G7 제도 반영",
        f"- 채택된 정책 개정 {m['policies_adopted']}건",
    ]
    for h in policy_history(events)[1:]:
        lines.append(f"  - v{h['version']} {h['ts']} {h['actor']}: {h['change']}")
    lines += ["", "## 원장(최근 20건)", "", "| # | 시각 | 관문 | 행위자 | 건 | 내용 | 해시 |", "|---|---|---|---|---|---|---|"]
    for e in events[-20:]:
        lines.append(f"| {e['seq']} | {e['ts']} | {EVENT_KINDS.get(e['kind'], e['kind'])} | {e['actor']}({e['role']}) |"
                     f" {e.get('case') or ''} | {e['kind']} {e['payload'].get('action', '')} | `{e['hash'][:10]}` |")
    return "\n".join(lines) + "\n"
