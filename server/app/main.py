import uvicorn
from fastapi import FastAPI

from app.api import router
from app.config import get_settings
from app.masking import install_redact_filter


def create_app() -> FastAPI:
    install_redact_filter()
    app = FastAPI(title="Heli bot", version="0.1.0")
    app.state.mail_sink = []

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
