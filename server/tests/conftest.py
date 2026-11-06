import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_db, make_engine
from app.main import create_app
from app.models import Base


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-padding-32bytes!")
    monkeypatch.setenv("TRAJECTORY_HASH_PEPPER", "test-pepper")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    # Most tests sign up directly; invite-only sign-up has its own tests.
    monkeypatch.setenv("HELI_INVITE_ONLY", "0")
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def client(session):
    app = create_app()

    def override_db():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client
