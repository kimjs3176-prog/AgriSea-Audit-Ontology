"""환경 설정.

인증키는 코드에 넣지 않고 환경변수(또는 프로젝트 루트의 .env)에서 읽는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

API_URL = "https://open.assembly.go.kr/portal/openapi/ncwgseseafwbuheph"
COMMITTEE_NAME = "농림축산식품해양수산위원회"
# 대수별 위원회 명칭 변화(19대 이전: 농림수산식품위원회 등)를 함께 매칭
COMMITTEE_ALIASES = (
    "농림축산식품해양수산위원회",
    "농해수위",
    "농림해양수산위원회",
    "농림수산식품위원회",
)


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

# Vercel 등 서버리스 환경: 배포 번들은 읽기 전용이고 /tmp만 쓸 수 있다.
IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
# GitHub Actions 「회의록 데이터 갱신」이 수집해 커밋하는 초기 데이터(있으면 기동 시 풀어서 사용)
SEED_DB = ROOT / "data" / "seed.sqlite3.gz"
SEED_META = ROOT / "data" / "seed.meta.json"
# 22대 국회 임기 시작일: 초기 데이터가 없을 때 자동 수집의 시작점
TERM_START = "2024-05-30"


def _default_data_dir() -> Path:
    if os.environ.get("AGRISEA_DATA_DIR"):
        return Path(os.environ["AGRISEA_DATA_DIR"])
    return Path("/tmp/agrisea") if IS_SERVERLESS else ROOT / "var"


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.environ.get("ASSEMBLY_API_KEY", ""))
    # 선택: 약속 이행 추적에서 지원사업 공고(K-Startup, 공공데이터포털)를 근거로 찾을 때
    kstartup_key: str = field(default_factory=lambda: os.environ.get("KSTARTUP_API_KEY", ""))
    # 공공데이터포털이 해외(GitHub Actions) 접속을 막을 때 서울 리전 배포본의 /api/kstartup 을 거쳐 받는다
    kstartup_proxy: str = field(default_factory=lambda: os.environ.get("KSTARTUP_PROXY", ""))
    api_url: str = field(default_factory=lambda: os.environ.get("ASSEMBLY_API_URL", API_URL))
    data_dir: Path = field(default_factory=_default_data_dir)
    serverless: bool = IS_SERVERLESS
    llm_model: str = field(
        default_factory=lambda: os.environ.get("AGRISEA_LLM_MODEL", "claude-opus-5")
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agrisea.sqlite3"

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdf"

    @property
    def graph_path(self) -> Path:
        return self.data_dir / "agrisea_kg.ttl"

    @property
    def llm_enabled(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pdf_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
