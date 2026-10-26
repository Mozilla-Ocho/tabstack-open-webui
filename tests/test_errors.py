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
        "Error: The Tabstack tool hit an unexpected problem (RuntimeError)."
    )
    assert "Traceback" not in message
    assert FAKE_KEY not in message


def test_timeout_message_names_the_limit() -> None:
    message = exception_message(
        TimeoutError(), "research", secret=None, timeout_seconds=300
    )
    assert message.startswith(
        "Error: The research took longer than 300 seconds and was stopped."
    )
    page = exception_message(
        TimeoutError(), "read_url", secret=None, timeout_seconds=60
    )
    assert page.startswith(
        "Error: Reading the page took longer than 60 seconds and was stopped."
    )


@pytest.mark.parametrize("kind", list(FailureKind))
@pytest.mark.parametrize("operation", ["research", "read_url"])
def test_every_message_is_plain(kind: FailureKind, operation: Operation) -> None:
    message = failure_message(
        kind, operation, detail="detail", timeout_seconds=10, idle_seconds=5
    )
    assert message.startswith("Error: ")
    if kind is not FailureKind.CALL_LIMIT:
        # The call-limit message is for the model, not something to relay.
        assert (
            "Tell the user exactly this and add nothing about how to fix it." in message
        )
    if kind is not FailureKind.INVALID_URL:
        assert message.endswith("Do not call this tool again for this question.")
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
    assert message.startswith("Error: The Tabstack tool is not set up yet")
    assert "Valves" in message
    assert "Controls (the sliders icon" in message
    assert "> Valves > Tools > Tabstack." in message


def test_messages_name_whose_key_failed() -> None:
    for kind in (
        FailureKind.REJECTED_KEY,
        FailureKind.NO_PERMISSION,
        FailureKind.OUT_OF_CREDITS,
    ):
        shared = failure_message(kind, "research")
        own = failure_message(kind, "research", key_owner="user")
        assert "your" not in shared.split(".")[0].lower()
        assert "Your" in own or "your" in own
        assert own != shared


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


def test_invalid_url_message_allows_a_corrected_call() -> None:
    message = failure_message(FailureKind.INVALID_URL, "read_url", detail="x")
    assert "Do not call this tool again" not in message
    assert message.endswith(
        "Only call read_url again with a full address that starts with http:// "
        "or https://."
    )


def test_call_limit_message() -> None:
    message = failure_message(FailureKind.CALL_LIMIT, "research", call_limit=1)
    assert message == (
        "Error: research already ran 1 time for this reply, so it was not run again "
        "and nothing was charged. Answer the user from the earlier result, and tell "
        "them plainly that this part was not looked up. Do not call this tool again "
        "for this question."
    )
    assert "ran for this reply" in failure_message(FailureKind.CALL_LIMIT, "read_url")


def test_call_limit_message_names_what_was_refused() -> None:
    message = failure_message(
        FailureKind.CALL_LIMIT,
        "research",
        detail='Who won the  "most recent"\nGrand Prix?',
        call_limit=1,
    )
    assert message.startswith(
        "Error: research already ran 1 time for this reply, so it was not run again "
        "for \"Who won the 'most recent' Grand Prix?\" and nothing was charged."
    )
    long = failure_message(FailureKind.CALL_LIMIT, "read_url", detail="x" * 400)
    assert '"' + "x" * 147 + '..."' in long


def test_relayed_messages_rule_out_additions() -> None:
    for kind in (FailureKind.MISSING_KEY, FailureKind.INVALID_URL):
        message = failure_message(kind, "read_url")
        assert "Tell the user exactly this and add nothing about how to fix it." in (
            message
        )
