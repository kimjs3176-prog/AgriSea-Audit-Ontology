"""Vercel 서버리스 진입점(이 파일의 `app`을 사용한다).

앱 생성이 실패해도 함수 자체는 뜨도록, 실패 원인을 보여 주는 최소 앱으로 대체한다.
"""
import sys
import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _build() -> FastAPI:
    try:
        from radar.web.app import create_app

        return create_app()
    except Exception:  # noqa: BLE001 - 배포 진단용
        error = traceback.format_exc()
        print(error, file=sys.stderr)
        fallback = FastAPI()

        @fallback.api_route("/{path:path}", methods=["GET", "POST"])
        def startup_failed(path: str):
            return JSONResponse({"error": "앱 초기화 실패", "traceback": error}, status_code=500)

        return fallback


app = _build()
