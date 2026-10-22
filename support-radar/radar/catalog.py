"""공고·사업 소개 스냅숏(JSON.gz)과 국회 정책 신호(JSON) 보관소.

자동 수집(GitHub Actions)이 data/catalog.json.gz 와 data/policy_signals.json 을 커밋하면
배포본은 그대로 읽어 쓴다. 인증키가 있는 배포본은 화면에서 접수 중 공고를 새로 받아
쓰기 가능한 데이터 폴더(/tmp)에 덮어 쓴다.
"""
from __future__ import annotations

import gzip
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .config import Settings
from .kstartup import KStartupClient
from .policy import tag_announcements


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Catalog:
    def __init__(self, announcements: list[dict] | None = None, programs: list[dict] | None = None,
                 meta: dict[str, Any] | None = None, signals: dict[str, Any] | None = None):
        self.announcements = announcements or []
        self.programs = programs or []
        self.meta = meta or {}
        self.signals = signals
        self.lock = threading.Lock()
        tag_announcements(self.announcements)
        self._index()

    def _index(self) -> None:
        self.by_id = {a["id"]: a for a in self.announcements}

    # ── 입출력 ─────────────────────────────────────────
    @classmethod
    def read(cls, path: Path) -> "Catalog":
        doc = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        return cls(doc.get("announcements"), doc.get("programs"), doc.get("meta"))

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        anns = [{k: v for k, v in a.items() if k != "topics"} for a in self.announcements]
        doc = {"meta": self.meta, "announcements": sorted(anns, key=lambda a: a["id"]),
               "programs": sorted(self.programs, key=lambda p: p["id"])}
        # mtime=0: 내용이 같으면 파일도 같아 불필요한 커밋이 생기지 않는다
        data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path.write_bytes(gzip.compress(data, mtime=0))

    @classmethod
    def load(cls, settings: Settings) -> "Catalog":
        """런타임 갱신본(data_dir)과 저장소 스냅숏 중 최근 것, 그리고 정책 신호."""
        candidates = []
        for p in (settings.catalog_path, config.SEED_PATH):
            if p.exists():
                try:
                    c = cls.read(p)
                    candidates.append(c)
                except (OSError, ValueError) as e:
                    print(f"[catalog] {p} 읽기 실패: {e}")
        cat = max(candidates, key=lambda c: c.meta.get("updated_at", "")) if candidates else cls()
        cat.signals = load_signals()
        return cat

    # ── 갱신 ───────────────────────────────────────────
    def merge(self, announcements: list[dict], programs: list[dict] | None = None) -> int:
        with self.lock:
            before = len(self.by_id)
            for a in announcements:
                old = self.by_id.get(a["id"])
                if old and old.get("rules") and old.get("content") == a.get("content"):
                    a["rules"] = old["rules"]   # LLM이 뽑아 둔 조건은 본문이 같으면 유지
                self.by_id[a["id"]] = a
            self.announcements = list(self.by_id.values())
            tag_announcements(self.announcements)
            if programs is not None:
                merged = {p["id"]: p for p in self.programs}
                merged.update({p["id"]: p for p in programs})
                self.programs = list(merged.values())
            self.meta["updated_at"] = _now()
            self.meta["announcements"] = len(self.announcements)
            self.meta["programs"] = len(self.programs)
            return len(self.by_id) - before

    def refresh(self, settings: Settings, since: str = "", open_only: bool = True,
                with_programs: bool = False, max_pages: int = 400) -> dict[str, Any]:
        client = KStartupClient(settings.api_key, settings.api_url, settings.proxy_url)
        anns = client.announcements(since=since, open_only=open_only, max_pages=max_pages)
        progs = client.programs(since_year=int((since or config.COLLECT_SINCE)[:4])) if with_programs else None
        added = self.merge(anns, progs)
        self.meta["source"] = "K-Startup(창업진흥원) 조회서비스 · 공공데이터포털"
        self.meta["last_fetch"] = {"at": _now(), "fetched": len(anns), "added": added,
                                   "open_only": open_only, "since": since}
        return self.meta["last_fetch"]

    def stats(self) -> dict[str, Any]:
        from .kstartup import status
        st = [status(a) for a in self.announcements]
        return {"announcements": len(self.announcements), "open": st.count("open"),
                "upcoming": st.count("upcoming"), "programs": len(self.programs),
                "with_rules": sum(1 for a in self.announcements if a.get("rules")),
                "updated_at": self.meta.get("updated_at"), "last_fetch": self.meta.get("last_fetch"),
                "signals": (self.signals or {}).get("source"),
                "signals_generated_at": (self.signals or {}).get("generated_at")}


def load_signals(path: Path | None = None) -> dict[str, Any] | None:
    path = path or config.SIGNALS_PATH
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
