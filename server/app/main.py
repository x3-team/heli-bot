import uvicorn
from fastapi import FastAPI

from app.config import get_settings


def create_app() -> FastAPI:
    app = FastAPI(title="Heli bot", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
