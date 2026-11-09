from io import StringIO

import pytest
from loguru import logger

from nanobot.utils.log_config import add_console_log_sink, configure_console_logging


def test_console_log_level_from_environment_suppresses_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NANOBOT_LOG_LEVEL", "WARNING")
    output = StringIO()
    sink = configure_console_logging(output)
    try:
        logger.info("hidden info")
        logger.warning("visible warning")
        logger.error("visible error")
        logger.critical("visible critical")
    finally:
        logger.remove(sink)

    rendered = output.getvalue()
    assert "hidden info" not in rendered
    assert "visible warning" in rendered
    assert "visible error" in rendered
    assert "visible critical" in rendered


def test_console_log_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NANOBOT_LOG_LEVEL", raising=False)
    output = StringIO()
    sink = configure_console_logging(output)
    try:
        logger.info("visible info")
    finally:
        logger.remove(sink)

    assert "visible info" in output.getvalue()


def test_console_log_sink_includes_context_and_escapes_message_newlines() -> None:
    output = StringIO()
    sink = add_console_log_sink(output, level="DEBUG")
    try:
        with logger.contextualize(
            request_id="request-1",
            turn_id="turn-1",
            session_key="channel:chat",
        ):
            logger.bind(channel="test").info("first line\nsecond line")
    finally:
        logger.remove(sink)

    rendered = output.getvalue()
    assert "request=request-1 turn=turn-1 session=channel:chat" in rendered
    assert "first line\\nsecond line" in rendered
    assert "first line\nsecond line" not in rendered


def test_console_log_sink_keeps_exception_traceback_multiline() -> None:
    output = StringIO()
    sink = add_console_log_sink(output)
    try:
        try:
            raise RuntimeError("broken")
        except RuntimeError:
            logger.opt(exception=True).error("operation failed")
    finally:
        logger.remove(sink)

    rendered = output.getvalue()
    assert "operation failed" in rendered
    assert "RuntimeError: broken" in rendered
