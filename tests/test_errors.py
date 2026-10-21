"""Failure classification and chat messages."""

from __future__ import annotations

import httpx
import pytest
import tabstack

from tabstack_tool import (
    FailureKind,
    Operation,
    classify_exception,
    clean_detail,
    exception_message,
    failure_message,
    redact,
)

REQUEST = httpx.Request("POST", "https://api.tabstack.ai/v1/research")
FAKE_KEY = "fake-key-for-tests-0000"


def status_error(
    cls: type[tabstack.APIStatusError], code: int, body: object = None
) -> tabstack.APIStatusError:
    return cls(
        f"Error code: {code}", response=httpx.Response(code, request=REQUEST), body=body
    )


@pytest.mark.parametrize(
    ("exc", "operation", "kind"),
    [
        (
            status_error(tabstack.AuthenticationError, 401),
            "research",
            FailureKind.REJECTED_KEY,
        ),
        (
            status_error(tabstack.PermissionDeniedError, 403),
            "research",
            FailureKind.NO_PERMISSION,
        ),
        (
            status_error(tabstack.APIStatusError, 402),
            "research",
            FailureKind.OUT_OF_CREDITS,
        ),
        (
            status_error(tabstack.RateLimitError, 429),
            "read_url",
            FailureKind.RATE_LIMITED,
        ),
        (
            status_error(tabstack.BadRequestError, 400),
            "research",
            FailureKind.BAD_REQUEST,
        ),
        (
            status_error(tabstack.UnprocessableEntityError, 422),
            "read_url",
            FailureKind.UNREADABLE_PAGE,
        ),
        (
            status_error(tabstack.UnprocessableEntityError, 422),
            "research",
            FailureKind.BAD_REQUEST,
        ),
        (
            status_error(tabstack.InternalServerError, 500),
            "read_url",
            FailureKind.UNREADABLE_PAGE,
        ),
        (
            status_error(tabstack.InternalServerError, 503),
            "research",
            FailureKind.SERVICE_ERROR,
        ),
        (
            status_error(tabstack.APIStatusError, 418),
            "research",
            FailureKind.SERVICE_ERROR,
        ),
        (tabstack.APITimeoutError(request=REQUEST), "research", FailureKind.STALLED),
        (TimeoutError(), "research", FailureKind.TIMEOUT),
        (httpx.ReadTimeout("slow", request=REQUEST), "research", FailureKind.STALLED),
        (
            tabstack.APIConnectionError(request=REQUEST),
            "read_url",
            FailureKind.CONNECTION,
        ),
        (
            httpx.ConnectError("refused", request=REQUEST),
            "research",
            FailureKind.CONNECTION,
        ),
        (RuntimeError("anything"), "research", FailureKind.UNEXPECTED),
    ],
)
def test_classify_exception(
    exc: BaseException, operation: Operation, kind: FailureKind
) -> None:
    assert classify_exception(exc, operation) is kind


def test_dropped_connection_after_events_is_an_incomplete_stream() -> None:
    exc = httpx.RemoteProtocolError("peer closed connection", request=REQUEST)
    assert (
        classify_exception(exc, "research", stream_started=True)
        is FailureKind.STREAM_INCOMPLETE
    )
    assert (
        classify_exception(exc, "research", stream_started=False)
        is FailureKind.CONNECTION
    )


def test_unreadable_page_message_uses_server_detail() -> None:
    exc = status_error(
        tabstack.InternalServerError, 500, {"error": "failed to fetch URL"}
    )
    message = exception_message(exc, "read_url", secret=FAKE_KEY)
    assert message.startswith(
        "Error: Tabstack could not read that page (failed to fetch URL)."
    )


def test_internal_resource_message() -> None:
    exc = status_error(
        tabstack.UnprocessableEntityError,
        422,
        {"error": "access to internal resources is not allowed"},
    )
    message = exception_message(exc, "read_url", secret=FAKE_KEY)
    assert "(access to internal resources is not allowed)" in message


def test_server_detail_is_redacted() -> None:
    exc = status_error(
        tabstack.BadRequestError, 400, {"error": f"bad token {FAKE_KEY}"}
    )
    message = exception_message(exc, "research", secret=FAKE_KEY)
    assert FAKE_KEY not in message
    assert "[redacted]" in message


def test_unexpected_error_shows_only_the_class_name() -> None:
    exc = RuntimeError(f"Traceback (most recent call last): key={FAKE_KEY}")
    message = exception_message(exc, "research", secret=FAKE_KEY)
    assert message.startswith(
        "Error: the Tabstack tool hit an unexpected problem (RuntimeError)."
    )
    assert "Traceback" not in message
    assert FAKE_KEY not in message


def test_timeout_message_names_the_limit() -> None:
    message = exception_message(
        TimeoutError(), "research", secret=None, timeout_seconds=300
    )
    assert message.startswith(
        "Error: the research did not finish within 300 seconds and was stopped."
    )
    page = exception_message(
        TimeoutError(), "read_url", secret=None, timeout_seconds=60
    )
    assert page.startswith(
        "Error: the page did not finish within 60 seconds and was stopped."
    )


@pytest.mark.parametrize("kind", list(FailureKind))
@pytest.mark.parametrize("operation", ["research", "read_url"])
def test_every_message_is_plain(kind: FailureKind, operation: Operation) -> None:
    message = failure_message(
        kind, operation, detail="detail", timeout_seconds=10, idle_seconds=5
    )
    assert message.startswith("Error: ")
    assert message.endswith("Tell the user this instead of answering from memory.")
    assert message.isascii()
    assert "Traceback" not in message
    assert "\n" not in message


def test_stalled_message_names_the_gap() -> None:
    exc = httpx.ReadTimeout("slow", request=REQUEST)
    message = exception_message(
        exc, "research", secret=None, timeout_seconds=300, idle_seconds=120
    )
    assert message.startswith(
        "Error: Tabstack sent nothing for 120 seconds, so the research was stopped."
    )


def test_missing_key_message() -> None:
    message = failure_message(FailureKind.MISSING_KEY, "research")
    assert "no API key" in message
    assert "Valves" in message


def test_messages_are_distinct() -> None:
    messages = {failure_message(kind, "research") for kind in FailureKind}
    assert len(messages) == len(FailureKind)


def test_redact() -> None:
    assert redact(f"a {FAKE_KEY} b {FAKE_KEY}", FAKE_KEY) == "a [redacted] b [redacted]"
    assert redact("abc", "") == "abc"
    assert redact("abc", None) == "abc"


def test_clean_detail() -> None:
    assert clean_detail("  two\n  lines  ") == "two lines"
    assert clean_detail("") is None
    assert clean_detail("   ") is None
    long = clean_detail("x" * 500)
    assert long is not None
    assert len(long) == 200
    assert long.endswith("...")
