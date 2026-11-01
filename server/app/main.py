import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import router
from app.browser import install_reaper
from app.cloud_browser import register as register_cloud_browser
from app.config import get_settings
from app.files import register as register_files
from app.masking import install_redact_filter


def create_app() -> FastAPI:
    install_redact_filter()
    app = FastAPI(title="Heli bot", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.mail_sink = []

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    install_reaper()
    register_files()
    register_cloud_browser(background=True)
    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
