import io
import logging
import sys

from ai_provider_gateway.logging_config import RedactingFilter, configure_logging
from ai_provider_gateway.redaction import EMAIL_REDACTED, REDACTED, redact, redact_text


def test_recursive_redaction_does_not_mutate_input() -> None:
    original = {
        "authorization": "Bearer top-secret",
        "nested": {
            "api_key": "gateway-key",
            "items": ["operator@example.com", {"password": "cleartext"}],
        },
    }

    sanitized = redact(original)

    assert sanitized == {
        "authorization": REDACTED,
        "nested": {
            "api_key": REDACTED,
            "items": [EMAIL_REDACTED, {"password": REDACTED}],
        },
    }
    assert original["authorization"] == "Bearer top-secret"
    assert original["nested"]["api_key"] == "gateway-key"


def test_structured_user_payloads_are_not_logged() -> None:
    sanitized = redact(
        {
            "prompt": "private prompt",
            "response": "private response",
            "messages": [{"content": "private message"}],
            "tool_arguments": {"path": "private path"},
            "client_secret": "oauth client secret",
            "session_id": "dashboard session",
        }
    )

    assert all(value == REDACTED for value in sanitized.values())


def test_text_redaction_covers_identity_paths_and_named_secrets() -> None:
    source = (
        r"Bearer abc.def email=operator@example.com "
        r"path=C:\Users\Alice\AppData\Local token=cleartext"
    )

    sanitized = redact_text(source)

    assert "abc.def" not in sanitized
    assert "operator@example.com" not in sanitized
    assert r"C:\Users\Alice" not in sanitized
    assert "cleartext" not in sanitized
    assert "Bearer [REDACTED]" in sanitized
    assert EMAIL_REDACTED in sanitized
    assert r"%USERPROFILE%\AppData\Local" in sanitized


def test_text_redaction_covers_authorization_and_cookie_headers() -> None:
    source = (
        "Authorization: Basic encoded-credential\n"
        "Cookie: session=private-cookie\n"
        "Set-Cookie: refresh=private-refresh"
    )

    sanitized = redact_text(source)

    assert "encoded-credential" not in sanitized
    assert "private-cookie" not in sanitized
    assert "private-refresh" not in sanitized
    assert sanitized.count(REDACTED) == 3


def test_logging_filter_redacts_message_and_arguments() -> None:
    record = logging.LogRecord(
        name="ai_provider_gateway.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="email=%s token=cleartext",
        args=("operator@example.com",),
        exc_info=None,
    )

    assert RedactingFilter().filter(record) is True
    rendered = record.getMessage()
    assert "operator@example.com" not in rendered
    assert "cleartext" not in rendered
    assert EMAIL_REDACTED in rendered
    assert REDACTED in rendered


def test_logging_filter_redacts_exception_text_and_traceback_paths() -> None:
    try:
        raise RuntimeError(
            r"Authorization: Digest private-value at C:\Users\Alice\project"
        )
    except RuntimeError:
        exception_info = sys.exc_info()

    record = logging.LogRecord(
        name="ai_provider_gateway.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="request failed",
        args=(),
        exc_info=exception_info,
    )

    assert RedactingFilter().filter(record) is True
    assert record.exc_info is None
    assert record.exc_text is not None
    assert "private-value" not in record.exc_text
    assert r"C:\Users\Alice" not in record.exc_text
    assert REDACTED in record.exc_text


def test_production_logging_configuration_protects_application_and_server_handlers() -> None:
    names = ("", "ai_provider_gateway", "uvicorn", "uvicorn.error", "uvicorn.access")
    loggers = [logging.getLogger(name) for name in names]
    saved = [(logger.handlers[:], logger.level, logger.propagate) for logger in loggers]
    try:
        for logger in loggers:
            logger.handlers = [logging.StreamHandler(io.StringIO())]
        configure_logging()
        for logger in loggers:
            assert logger.handlers
            assert all(
                any(isinstance(item, RedactingFilter) for item in handler.filters)
                for handler in logger.handlers
            )
    finally:
        for logger, (handlers, level, propagate) in zip(loggers, saved, strict=True):
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
