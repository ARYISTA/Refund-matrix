"""Phase 1: settings and logging."""

import io
import json
import logging
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config import BACKEND_DIR, REPO_ROOT, Settings
from app.logging_config import JsonFormatter, configure_logging, redact_text, redact_value

GOOD_URL = "postgresql+psycopg://u:p@localhost:5432/db"


def make_settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg, arg-type]


# ---- settings -------------------------------------------------------------------------------


def test_settings_defaults_and_required_database_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        make_settings()
    s = make_settings(database_url=GOOD_URL)
    assert s.app_env == "development"
    assert s.log_level == "INFO"
    assert s.policy_path.is_absolute() and s.policy_path.exists()


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", GOOD_URL)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    s = make_settings()
    assert (s.app_env, s.log_level) == ("test", "DEBUG")


@pytest.mark.parametrize(
    "bad_url",
    ["postgresql://u:p@h/db", "postgresql+psycopg2://u:p@h/db", "sqlite:///x.db", ""],
)
def test_settings_require_psycopg3_url(bad_url: str):
    with pytest.raises(ValidationError):
        make_settings(database_url=bad_url)


def test_settings_reject_bad_enum_values():
    with pytest.raises(ValidationError):
        make_settings(database_url=GOOD_URL, app_env="staging")
    with pytest.raises(ValidationError):
        make_settings(database_url=GOOD_URL, log_level="LOUD")


def test_database_url_is_secret():
    s = make_settings(database_url=GOOD_URL)
    assert isinstance(s.database_url, SecretStr)
    assert "u:p@" not in repr(s)
    assert "u:p@" not in str(s)
    assert "u:p@" not in s.model_dump_json()


def test_relative_policy_path_resolves_against_backend():
    s = make_settings(database_url=GOOD_URL, policy_path="app/policy/policy.yaml")
    assert s.policy_path == (BACKEND_DIR / "app/policy/policy.yaml").resolve()


def test_env_example_is_loadable_as_a_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in ("DATABASE_URL", "APP_ENV", "LOG_LEVEL", "POLICY_PATH"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text((REPO_ROOT / ".env.example").read_text())
    s = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert s.app_env == "development"
    assert s.log_level == "INFO"
    assert s.policy_path.exists()  # the example path points at the real policy.yaml
    assert s.database_url.get_secret_value().startswith("postgresql+psycopg://")


# ---- redaction --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ("contact asha.verma@example.com please", "asha.verma@example.com"),
        ("call +91 98765 43210 now", "98765"),
        ("phone 9876543210", "9876543210"),
        ("account 111122223333 was used", "111122223333"),
        ("account 1111-2222-3333", "1111-2222"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("password=hunter2 failed", "hunter2"),
        ("api_key: gsk_live_12345", "gsk_live_12345"),
    ],
)
def test_redact_text_removes_pii_and_secrets(text: str, forbidden: str):
    assert forbidden not in redact_text(text)


def test_redact_text_keeps_harmless_values():
    text = "case 1b2c order ORD-1001 amount 499999 paise policy 1.0.0"
    assert redact_text(text) == text


def test_redact_value_by_key_and_nesting():
    out = redact_value(
        "payload",
        {
            "password": "x",
            "GROQ_API_KEY": "gsk_abc",
            "bank_account_number": "111122223333",
            "note": "mail me at a@b.co",
            "ok": 5,
            "nested": [{"token": "t"}, "call 9876543210"],
            "secret_obj": SecretStr("s"),
        },
    )
    dumped = json.dumps(out)
    for forbidden in ("gsk_abc", "111122223333", "a@b.co", "9876543210", '"t"', '"x"'):
        assert forbidden not in dumped
    assert out["ok"] == 5


# ---- JSON logging ------------------------------------------------------------------------------


def _capture() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(f"test.{id(stream)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_log_line_is_json_with_standard_fields():
    logger, stream = _capture()
    logger.info("hello %s", "world", extra={"case_id": "c-1", "amount_paise": 499999})
    line = json.loads(stream.getvalue())
    assert line["message"] == "hello world"
    assert line["level"] == "INFO"
    assert line["case_id"] == "c-1" and line["amount_paise"] == 499999
    assert {"ts", "logger"} <= set(line)


def test_log_extras_and_message_are_redacted():
    logger, stream = _capture()
    logger.warning(
        "refund for asha.verma@example.com account 111122223333",
        extra={"password": "hunter2", "customer_email": "a@b.co", "bank_account_number": "111122223333"},
    )
    raw = stream.getvalue()
    for forbidden in ("asha.verma@example.com", "111122223333", "hunter2", "a@b.co"):
        assert forbidden not in raw


def test_exception_traceback_is_redacted():
    logger, stream = _capture()
    try:
        raise ValueError("failed for asha.verma@example.com")
    except ValueError:
        logger.exception("boom")
    line = json.loads(stream.getvalue())
    assert "asha.verma@example.com" not in line["exception"]
    assert "ValueError" in line["exception"]


def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging("DEBUG")
        configure_logging("WARNING")
        ours = [h for h in root.handlers if h not in before]
        assert len(ours) == 1
        assert root.level == logging.WARNING
    finally:
        root.handlers = before
        root.setLevel(logging.WARNING)
