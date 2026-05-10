"""Standalone HTTP server for Hermes Orchestration Core."""

from __future__ import annotations

import os

from fastapi import FastAPI

from orchestration.api import router as orchestration_router


def create_app() -> FastAPI:
    app = FastAPI(title="Hermes Orchestration Core", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "hermes-orchestration-core"}

    app.include_router(orchestration_router)
    return app


def main() -> None:
    import uvicorn

    host = os.getenv("HERMES_ORCHESTRATION_HOST", "127.0.0.1")
    port = int(os.getenv("HERMES_ORCHESTRATION_PORT", "8650"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
