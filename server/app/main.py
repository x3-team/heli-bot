import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.credentials import register as register_credentials
from app.agent.google_analytics import register as register_ga4
from app.agent.bitrix import register as register_bitrix
from app.agent.moysklad import register as register_moysklad
from app.agent.http_tool import register as register_http
from app.agent.mail import register as register_mail
from app.agent.yandex_direct import register as register_direct
from app.knowledge import register as register_knowledge
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
    register_credentials()
    register_moysklad()
    register_bitrix()
    register_http()
    register_mail()
    register_direct()
    register_ga4()
    register_knowledge()
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
