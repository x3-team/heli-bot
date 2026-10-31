import json

from fastapi.testclient import TestClient

from app.main import create_app


def test_health() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_public_schema_hides_upstream_names() -> None:
    spec = json.dumps(create_app().openapi(), ensure_ascii=False).lower()
    for word in ("deepseek", "anthropic", "claude", "gemini", "openai", "gpt-5", "qwen"):
        if word in spec:
            raise AssertionError("upstream name in public schema")
