"""Application logging configured around mandatory redaction."""

from __future__ import annotations

import logging
import traceback

from .redaction import redact, redact_text


class RedactingFilter(logging.Filter):
    """Sanitize messages and structured arguments before handlers emit them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        else:
            record.msg = redact(record.msg)
        if record.args:
            record.args = redact(record.args)
        if record.exc_info:
            record.exc_text = redact_text(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


def _protect_handlers(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        if not any(isinstance(item, RedactingFilter) for item in handler.filters):
            handler.addFilter(RedactingFilter())


def configure_logging(level: str = "INFO") -> None:
    """Install redaction on application, root and Uvicorn output handlers."""

    logger = logging.getLogger("ai_provider_gateway")
    logger.setLevel(level.upper())
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    _protect_handlers(logger)

    root = logging.getLogger()
    if not root.handlers:
        root_handler = logging.StreamHandler()
        root_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        root.addHandler(root_handler)
    _protect_handlers(root)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _protect_handlers(logging.getLogger(name))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ai_provider_gateway.{name}")
