"""Phase 6: tracing is optional and never exports free-form sensitive data."""

from app.config import Settings
from app.tracing import Tracer, mask_trace_value


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, database_url="postgresql+psycopg://x:x@localhost/db", **values)


def test_trace_values_are_bounded_and_safe():
    masked = mask_trace_value("jane@example.com / account 123456789012")
    assert "@" not in masked
    assert "/" in masked
    assert len(masked) <= 120


def test_tracing_is_disabled_by_default():
    tracer = Tracer(settings())
    assert not tracer.enabled
    assert tracer.start(name="test") is None


def test_missing_credentials_disable_tracing_without_failing_startup():
    tracer = Tracer(settings(tracing_enabled=True))
    assert not tracer.enabled
