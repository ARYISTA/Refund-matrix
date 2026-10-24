"""Phase 8: API authentication and authorization tests without LLM calls."""

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.security import hash_password


def test_health_does_not_require_auth():
    settings = Settings(
        _env_file=None, database_url="postgresql+psycopg://x:x@localhost/db", jwt_secret="x" * 32
    )
    app = create_app(settings)
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_password_hash_round_trip():
    from app.security import verify_password

    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong", hashed)
