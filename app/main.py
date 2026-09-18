"""app 入口：本地起 FastAPI / 或 python -m app.main 直接起。"""
from __future__ import annotations

import uvicorn

from .api import app


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    uvicorn.run("app.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    serve()
