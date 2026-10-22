"""환경 설정.

인증키는 코드에 넣지 않고 환경변수(또는 서비스 폴더·저장소 루트의 .env)에서 읽는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # support-radar/
REPO_ROOT = ROOT.parent

API_URL = "https://apis.data.go.kr/B552735/kisedKstartupService01"
# 22대 국회 임기 시작일: 국회 회의록 데이터와 같은 기간의 공고부터 모은다
COLLECT_SINCE = "2024-05-30"

SEED_PATH = ROOT / "data" / "catalog.json.gz"       # 공고·사업 소개 스냅숏(자동 수집이 커밋)
SIGNALS_PATH = ROOT / "data" / "policy_signals.json"  # 국회 정책 신호(자동 수집이 커밋)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")
_load_dotenv(REPO_ROOT / ".env")

IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _default_data_dir() -> Path:
    if os.environ.get("RADAR_DATA_DIR"):
        return Path(os.environ["RADAR_DATA_DIR"])
    return Path("/tmp/support-radar") if IS_SERVERLESS else ROOT / "var"


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.environ.get("KSTARTUP_API_KEY", ""))
    api_url: str = field(default_factory=lambda: os.environ.get("KSTARTUP_API_URL", API_URL))
    # 공공데이터포털이 해외 접속을 막을 때 서울 리전 배포본을 거쳐 받는다(예: https://…/api/kstartup)
    proxy_url: str = field(default_factory=lambda: os.environ.get("KSTARTUP_PROXY", ""))
    data_dir: Path = field(default_factory=_default_data_dir)
    serverless: bool = IS_SERVERLESS
    llm_model: str = field(default_factory=lambda: os.environ.get("RADAR_LLM_MODEL", "claude-opus-5-5"))

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / "catalog.json.gz"

    @property
    def llm_enabled(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def get_settings() -> Settings:
    return Settings()
