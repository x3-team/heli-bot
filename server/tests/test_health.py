import json

from fastapi.testclient import TestClient

from app.main import create_app


def test_health(monkeypatch, tmp_path) -> None:
    from app import trajectories

    for name in ("APP_REVISION", "GIT_SHA", "GIT_COMMIT", "SOURCE_COMMIT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(trajectories, "REVISION_FILE", tmp_path / "missing")
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "revision": None}


def test_health_names_the_deployed_revision(monkeypatch, tmp_path) -> None:
    from app import trajectories

    for name in ("APP_REVISION", "GIT_SHA", "GIT_COMMIT", "SOURCE_COMMIT"):
        monkeypatch.delenv(name, raising=False)
    file = tmp_path / "REVISION"
    file.write_text("89d22637ffa07fad63a5d8b451e67240ffd3488a main 2026-11-07T13:39:22Z\n", encoding="utf-8")
    monkeypatch.setattr(trajectories, "REVISION_FILE", file)
    response = TestClient(create_app()).get("/health")
    assert response.json() == {"status": "ok", "revision": "89d22637ffa07fad63a5d8b451e67240ffd3488a"}


def test_public_schema_hides_upstream_names() -> None:
    spec = json.dumps(create_app().openapi(), ensure_ascii=False).lower()
    for word in ("deepseek", "anthropic", "claude", "gemini", "openai", "gpt-5", "qwen"):
        if word in spec:
            raise AssertionError("upstream name in public schema")
