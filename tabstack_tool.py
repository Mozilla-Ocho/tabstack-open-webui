"""
title: Tabstack
description: Cited answers from live sources and clean Markdown from URLs, by Tabstack.
version: 0.1.0
license: MIT
requirements: tabstack==2.8.5
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import tabstack
from pydantic import BaseModel, Field

# =============================================================================
# Pure section
#
# Nothing in this section knows about Open WebUI. It formats citations, maps
# Research stream events to status lines, trims Markdown and turns failures
# into plain messages. Stream events are read as plain mappings in the API's
# wire format (camelCase keys), which is what the SDK's `to_dict()` returns.
# =============================================================================

Operation = Literal["research", "read_url"]


# -- Small readers for untyped event payloads ---------------------------------


def _as_map(value: object) -> Mapping[str, object]:
    """Return value as a string-keyed mapping, or an empty one."""
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value)
        return {str(k): v for k, v in items.items()}
    return {}


def _as_str(value: object) -> str | None:
    """Return value if it is a non-empty string after stripping, else None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_count(value: object) -> int | None:
    """Return value as a non-negative int if it is a number, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(int(value), 0)


def _as_list(value: object) -> list[object]:
    """Return value as a list, or an empty one."""
    if isinstance(value, list):
        return list(cast(list[object], value))
    return []


def _plural(count: int, noun: str) -> str:
    """Return '1 source' or '3 sources'."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# -- Citations ----------------------------------------------------------------


@dataclass(frozen=True)
class CitedSource:
    """One entry of `metadata.citedPages` from a Research `complete` event."""

    url: str
    title: str | None = None
    summary: str | None = None
    claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class NumberedSource:
    """A de-duplicated source with its final citation number."""

    number: int
    url: str
    title: str | None
    summary: str | None
    claims: tuple[str, ...]
    merged_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class Merge:
    """Record that Tabstack's citation `original` at `url` became source `kept`."""

    original: int
    kept: int
    url: str


@dataclass(frozen=True)
class FormattedAnswer:
    """The text handed to the model plus the sources to emit as citations."""

    text: str
    sources: tuple[NumberedSource, ...]
    merges: tuple[Merge, ...]
    unmatched_markers: tuple[int, ...]


_TRACKING_PARAM = re.compile(r"^utm_", re.IGNORECASE)


def url_identity(url: str) -> str:
    """Return a key that is equal for URL variants of the same page."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    host = host.removeprefix("www.")
    port = parts.port
    if port is not None and port not in (80, 443):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    query_pairs = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAM.match(k)
    )
    query = urlencode(query_pairs)
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def parse_cited_pages(raw: object) -> tuple[CitedSource, ...]:
    """Read `metadata.citedPages` from the wire format, skipping entries with no URL."""
    sources: list[CitedSource] = []
    for item in _as_list(raw):
        page = _as_map(item)
        url = _as_str(page.get("url"))
        if url is None:
            continue
        claims = tuple(
            c for c in (_as_str(x) for x in _as_list(page.get("claims"))) if c
        )
        sources.append(
            CitedSource(
                url=url,
                title=_as_str(page.get("title")),
                summary=_as_str(page.get("summary")),
                claims=claims,
            )
        )
    return tuple(sources)


def dedupe_sources(
    sources: Sequence[CitedSource],
) -> tuple[tuple[NumberedSource, ...], dict[int, int], tuple[Merge, ...]]:
    """Merge URL variants of one page; return sources, old-to-new numbers and merges."""
    groups: dict[str, list[tuple[int, CitedSource]]] = {}
    order: list[str] = []
    for index, source in enumerate(sources, start=1):
        key = url_identity(source.url)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((index, source))

    numbered: list[NumberedSource] = []
    mapping: dict[int, int] = {}
    merges: list[Merge] = []
    for new_number, key in enumerate(order, start=1):
        members = groups[key]
        first_index, first = members[0]
        https_urls = [s.url for _, s in members if s.url.lower().startswith("https://")]
        display_url = https_urls[0] if https_urls else first.url
        title = next((s.title for _, s in members if s.title), None)
        summary = next((s.summary for _, s in members if s.summary), None)
        claims: list[str] = []
        for _, s in members:
            claims.extend(c for c in s.claims if c not in claims)
        merged_urls = tuple(
            dict.fromkeys(s.url for _, s in members if s.url != display_url)
        )
        numbered.append(
            NumberedSource(
                number=new_number,
                url=display_url,
                title=title,
                summary=summary,
                claims=tuple(claims),
                merged_urls=merged_urls,
            )
        )
        for index, source in members:
            mapping[index] = new_number
            if index != first_index:
                merges.append(Merge(original=index, kept=new_number, url=source.url))
    return tuple(numbered), mapping, tuple(merges)


_MARKER_RUN = re.compile(r"(?:\[\d+\])+(?!\()")
_MARKER = re.compile(r"\[(\d+)\]")


def renumber_markers(
    text: str, mapping: Mapping[int, int]
) -> tuple[str, tuple[int, ...]]:
    """Rewrite [n] markers through mapping, drop repeats, and list unknown markers."""
    unmatched: list[int] = []

    def rewrite_run(match: re.Match[str]) -> str:
        """Rewrite one run of adjacent markers such as [1][2]."""
        numbers: list[int] = []
        for raw in _MARKER.findall(match.group(0)):
            old = int(raw)
            new = mapping.get(old)
            if new is None:
                if old not in unmatched:
                    unmatched.append(old)
                new = old
            if new not in numbers:
                numbers.append(new)
        return "".join(f"[{n}]" for n in numbers)

    out: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
        elif in_fence:
            out.append(line)
        else:
            out.append(_MARKER_RUN.sub(rewrite_run, line))
    return "".join(out), tuple(unmatched)


def source_label(source: NumberedSource) -> str:
    """Return the display name for a source: its title, or its URL."""
    return source.title or source.url


def format_research_answer(
    report: str, sources: Sequence[CitedSource]
) -> FormattedAnswer:
    """Build the model-facing answer: renumbered report, source list and any notes."""
    numbered, mapping, merges = dedupe_sources(sources)
    body, unmatched = renumber_markers(report.strip(), mapping)

    lines: list[str] = [body, ""]
    if numbered:
        lines.append("Sources:")
        for source in numbered:
            if source.title:
                lines.append(f"[{source.number}] {source.title} - {source.url}")
            else:
                lines.append(f"[{source.number}] {source.url}")
    else:
        lines.append("Sources: Tabstack returned no cited sources for this answer.")

    if merges:
        pairs = "; ".join(f"{m.url} into [{m.kept}]" for m in merges)
        verb = (
            "was a variant of a page" if len(merges) == 1 else "were variants of pages"
        )
        lines.append("")
        lines.append(
            f"Note: {_plural(len(merges), 'cited address')} {verb} already listed "
            f"and merged: {pairs}."
        )
    if unmatched:
        refs = ", ".join(f"[{n}]" for n in unmatched)
        lines.append("")
        lines.append(
            f"Note: the answer refers to {refs}, which has no matching source."
        )

    lines.append("")
    lines.append(
        "When you use this answer, keep the [n] markers next to the points they "
        "support so the user can see the source for each point."
    )
    return FormattedAnswer(
        text="\n".join(lines),
        sources=numbered,
        merges=merges,
        unmatched_markers=unmatched,
    )


def citation_event(source: NumberedSource) -> dict[str, object]:
    """Return an Open WebUI `citation` event payload for one numbered source."""
    parts: list[str] = []
    if source.summary:
        parts.append(source.summary)
    if source.claims:
        parts.append(
            "Claims drawn from this page:\n"
            + "\n".join(f"- {c}" for c in source.claims)
        )
    if source.merged_urls:
        parts.append(
            "Also cited as:\n" + "\n".join(f"- {u}" for u in source.merged_urls)
        )
    document = "\n\n".join(parts) if parts else "Cited by Tabstack Research."
    label = source_label(source)
    return {
        "type": "citation",
        "data": {
            "document": [document],
            "metadata": [{"source": source.url, "name": label}],
            "source": {"name": label, "url": source.url},
        },
    }


def page_citation_event(url: str, title: str | None, excerpt: str) -> dict[str, object]:
    """Return an Open WebUI `citation` event payload for a page read with read_url."""
    label = title or url
    return {
        "type": "citation",
        "data": {
            "document": [excerpt],
            "metadata": [{"source": url, "name": label}],
            "source": {"name": label, "url": url},
        },
    }


# -- Research stream ----------------------------------------------------------


@dataclass(frozen=True)
class ResearchCompleted:
    """The stream ended with a `complete` event."""

    report: str
    sources: tuple[CitedSource, ...]
    metrics: Mapping[str, object] | None = None


@dataclass(frozen=True)
class ResearchFailed:
    """The stream ended with an `error` event."""

    message: str | None
    activity: str | None


@dataclass(frozen=True)
class ResearchIncomplete:
    """The stream closed without a `complete` or `error` event."""

    events_seen: int
    last_event: str | None


ResearchOutcome = ResearchCompleted | ResearchFailed | ResearchIncomplete


def status_for_event(event: Mapping[str, object]) -> str | None:
    """Return a status line for a Research event, or None to keep the current one."""
    name = _as_str(event.get("event"))
    data = _as_map(event.get("data"))
    if name in ("start", "planning:start"):
        return "Planning searches"
    if name == "iteration:start":
        current = _as_count(data.get("iteration"))
        total = _as_count(data.get("maxIterations"))
        if current is not None and total is not None and total > 0:
            return f"Searching, round {current} of {total}"
        return "Searching"
    if name == "searching:end":
        found = _as_count(data.get("urlsNew"))
        if found is None:
            return None
        return f"Found {_plural(found, 'new source')}"
    if name == "prefetching:start":
        count = _as_count(data.get("urlCount"))
        return (
            f"Fetching {_plural(count, 'linked page')}"
            if count
            else "Fetching linked pages"
        )
    if name == "analyzing:start":
        count = _as_count(data.get("pageCount"))
        return f"Reading {_plural(count, 'page')}" if count else "Reading pages"
    if name == "following:start":
        count = _as_count(data.get("linkCount"))
        return f"Following {_plural(count, 'link')}" if count else "Following links"
    if name == "evaluating:start":
        return "Checking what is still missing"
    if name == "outlining:start":
        return "Outlining the answer"
    if name == "writing:start":
        return (
            "Revising the answer"
            if data.get("isRevision") is True
            else "Writing the answer"
        )
    if name == "judging:start":
        return "Reviewing the draft"
    return None


def finished_status(source_count: int) -> str:
    """Return the final status line for a completed Research call."""
    return f"Research finished, {_plural(source_count, 'source')}"


def failed_status(operation: Operation) -> str:
    """Return the final status line for a failed call."""
    return "Research failed" if operation == "research" else "Could not read the page"


@dataclass
class ResearchProgress:
    """Fold Research stream events into status changes and a terminal outcome."""

    events_seen: int = 0
    last_event: str | None = None
    last_status: str | None = None
    outcome: ResearchCompleted | ResearchFailed | None = field(default=None)

    def feed(self, event: Mapping[str, object]) -> str | None:
        """Record one event; return a new status line if it changed, else None."""
        if self.outcome is not None:
            return None
        self.events_seen += 1
        name = _as_str(event.get("event"))
        self.last_event = name
        data = _as_map(event.get("data"))
        if name == "complete":
            metadata = _as_map(data.get("metadata"))
            metrics = _as_map(metadata.get("metrics")) or None
            self.outcome = ResearchCompleted(
                report=_as_str(data.get("report")) or "",
                sources=parse_cited_pages(metadata.get("citedPages")),
                metrics=metrics,
            )
            return None
        if name == "error":
            nested = _as_map(data.get("error"))
            self.outcome = ResearchFailed(
                message=_as_str(nested.get("message")) or _as_str(data.get("message")),
                activity=_as_str(data.get("activity")),
            )
            return None
        status = status_for_event(event)
        if status is None or status == self.last_status:
            return None
        self.last_status = status
        return status

    def result(self) -> ResearchOutcome:
        """Return the terminal outcome, or ResearchIncomplete if none arrived."""
        if self.outcome is not None:
            return self.outcome
        return ResearchIncomplete(
            events_seen=self.events_seen, last_event=self.last_event
        )


# -- Markdown trimming --------------------------------------------------------


@dataclass(frozen=True)
class TrimmedText:
    """Markdown cut to a length limit, with counts for the truncation note."""

    text: str
    kept_chars: int
    total_chars: int

    @property
    def trimmed(self) -> bool:
        """True when some of the original text was dropped."""
        return self.kept_chars < self.total_chars


def trim_markdown(markdown: str, max_chars: int) -> TrimmedText:
    """Cut markdown to at most max_chars, preferring a paragraph or line break."""
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    text = markdown.strip()
    total = len(text)
    if total <= max_chars:
        return TrimmedText(text=text, kept_chars=total, total_chars=total)

    window = text[:max_chars]
    cut = max_chars
    for separator in ("\n\n", "\n", " "):
        position = window.rfind(separator)
        if position >= max_chars // 2:
            cut = position
            break
    kept = text[:cut].rstrip()
    # A heading with nothing under it reads as if the section were empty.
    while "\n" in kept and kept.rsplit("\n", 1)[1].lstrip().startswith("#"):
        kept = kept.rsplit("\n", 1)[0].rstrip()
    rendered = kept
    fence_lines = sum(
        1 for line in kept.splitlines() if line.lstrip().startswith("```")
    )
    if fence_lines % 2 == 1:
        rendered = kept + "\n```"
    return TrimmedText(text=rendered, kept_chars=len(kept), total_chars=total)


def truncation_note(trimmed: TrimmedText) -> str:
    """Return a one-line note saying how much of the page was kept."""
    percent = (
        trimmed.kept_chars * 100 // trimmed.total_chars if trimmed.total_chars else 0
    )
    return (
        f"[Trimmed: kept the first {trimmed.kept_chars:,} of {trimmed.total_chars:,} "
        f"characters ({percent}%). The rest of the page is not included.]"
    )


def format_page(url: str, title: str | None, markdown: str, max_chars: int) -> str:
    """Build the model-facing text for one page read with read_url."""
    trimmed = trim_markdown(markdown, max_chars)
    lines = [
        f"Page: {title}" if title else "Page: (no title)",
        f"URL: {url}",
        "",
        trimmed.text,
    ]
    if trimmed.trimmed:
        lines.extend(["", truncation_note(trimmed)])
    return "\n".join(lines)


# -- URL checks ---------------------------------------------------------------


def clean_url(url: str) -> str:
    """Strip whitespace and the angle brackets models sometimes wrap URLs in."""
    return url.strip().removeprefix("<").removesuffix(">").strip()


def url_problem(url: str) -> str | None:
    """Return why read_url cannot accept url, or None if it is an http or https URL."""
    if not url:
        return "No URL was given."
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if not scheme:
        return f"'{url}' has no scheme. Give the full address starting with http:// or https://."
    if scheme not in ("http", "https"):
        return f"'{scheme}:' addresses are not supported."
    if not parts.hostname:
        return f"'{url}' has no host name."
    return None


# -- Errors -------------------------------------------------------------------


class FailureKind(Enum):
    """Every way a call can fail, as far as the chat is concerned."""

    MISSING_KEY = "missing_key"
    REJECTED_KEY = "rejected_key"
    NO_PERMISSION = "no_permission"
    OUT_OF_CREDITS = "out_of_credits"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    STALLED = "stalled"
    CONNECTION = "connection"
    SERVICE_ERROR = "service_error"
    BAD_REQUEST = "bad_request"
    STREAM_ERROR = "stream_error"
    STREAM_INCOMPLETE = "stream_incomplete"
    UNREADABLE_PAGE = "unreadable_page"
    INVALID_URL = "invalid_url"
    UNEXPECTED = "unexpected"


def redact(text: str, secret: str | None) -> str:
    """Replace every occurrence of secret in text."""
    if secret and len(secret) >= 4:
        return text.replace(secret, "[redacted]")
    return text


def clean_detail(
    text: str | None, secret: str | None = None, limit: int = 200
) -> str | None:
    """Collapse a server-supplied detail to one short line with the key removed."""
    if not text:
        return None
    line = " ".join(redact(text, secret).split())
    if not line:
        return None
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def error_body_detail(exc: BaseException) -> str | None:
    """Return the `error` string from a Tabstack API error body, if there is one."""
    if isinstance(exc, tabstack.APIStatusError):
        body = _as_map(exc.body)
        return _as_str(body.get("error"))
    return None


def classify_exception(
    exc: BaseException, operation: Operation, *, stream_started: bool = False
) -> FailureKind:
    """Map an exception raised by the SDK or the transport to a FailureKind."""
    if isinstance(exc, tabstack.AuthenticationError):
        return FailureKind.REJECTED_KEY
    if isinstance(exc, tabstack.PermissionDeniedError):
        return FailureKind.NO_PERMISSION
    if isinstance(exc, tabstack.RateLimitError):
        return FailureKind.RATE_LIMITED
    if isinstance(exc, tabstack.UnprocessableEntityError):
        return (
            FailureKind.UNREADABLE_PAGE
            if operation == "read_url"
            else FailureKind.BAD_REQUEST
        )
    if isinstance(exc, tabstack.BadRequestError):
        return FailureKind.BAD_REQUEST
    if isinstance(exc, tabstack.InternalServerError):
        return (
            FailureKind.UNREADABLE_PAGE
            if operation == "read_url"
            else FailureKind.SERVICE_ERROR
        )
    if isinstance(exc, tabstack.APIStatusError):
        return (
            FailureKind.OUT_OF_CREDITS
            if exc.status_code == 402
            else FailureKind.SERVICE_ERROR
        )
    if isinstance(exc, tabstack.APITimeoutError | httpx.TimeoutException):
        # The SDK timeout bounds the wait for each chunk, not the whole call.
        return FailureKind.STALLED
    if isinstance(exc, TimeoutError):
        # Raised by the tool's own total time limit.
        return FailureKind.TIMEOUT
    if isinstance(exc, tabstack.APIConnectionError | httpx.TransportError):
        # Once events have arrived, a dropped connection means the stream closed early.
        return (
            FailureKind.STREAM_INCOMPLETE if stream_started else FailureKind.CONNECTION
        )
    return FailureKind.UNEXPECTED


# Small local models tend to paraphrase an error into "I could not find that"
# and sometimes call the tool again. So each message is one sentence written
# for the user, followed by an instruction the model can follow literally.
_RELAY = " Tell the user exactly this. Do not call this tool again for this question."
_RELAY_URL = (
    " Tell the user exactly this. Only call read_url again with a full address "
    "that starts with http:// or https://."
)


def failure_message(
    kind: FailureKind,
    operation: Operation,
    *,
    detail: str | None = None,
    timeout_seconds: float | None = None,
    idle_seconds: float | None = None,
) -> str:
    """Return a plain chat message for a failure. Never includes a stack trace."""
    suffix = f" ({detail})" if detail else ""
    what = "The research" if operation == "research" else "Reading the page"
    if kind is FailureKind.MISSING_KEY:
        text = (
            "The Tabstack tool is not set up yet: it has no API key. An Open WebUI "
            "admin needs to add one in the tool's Valves."
        )
    elif kind is FailureKind.REJECTED_KEY:
        text = (
            "Tabstack rejected the tool's API key. An Open WebUI admin needs to check "
            "the key in the tool's Valves."
        )
    elif kind is FailureKind.NO_PERMISSION:
        text = f"The Tabstack API key is not allowed to make this request{suffix}."
    elif kind is FailureKind.OUT_OF_CREDITS:
        text = (
            "The Tabstack account behind this tool is out of credits, so the request "
            "was declined."
        )
    elif kind is FailureKind.RATE_LIMITED:
        text = (
            "Tabstack is limiting how often this account can make requests. Try "
            "again in a minute."
        )
    elif kind is FailureKind.TIMEOUT:
        limit = (
            f" longer than {timeout_seconds:g} seconds"
            if timeout_seconds
            else " too long"
        )
        hint = (
            " A narrower question may finish sooner, or an admin can raise the limit."
            if operation == "research"
            else ""
        )
        text = f"{what} took{limit} and was stopped.{hint}"
    elif kind is FailureKind.STALLED:
        gap = f"for {idle_seconds:g} seconds" if idle_seconds else "for too long"
        text = f"Tabstack sent nothing {gap}, so {what.lower()} was stopped."
    elif kind is FailureKind.CONNECTION:
        text = "The Tabstack tool could not reach Tabstack from the Open WebUI server."
    elif kind is FailureKind.SERVICE_ERROR:
        text = f"Tabstack returned a server error{suffix}."
    elif kind is FailureKind.BAD_REQUEST:
        text = f"Tabstack rejected the request as invalid{suffix}."
    elif kind is FailureKind.STREAM_ERROR:
        text = f"The research failed{suffix}."
    elif kind is FailureKind.STREAM_INCOMPLETE:
        text = (
            "The connection to Tabstack closed before the research finished, so "
            "there is no answer."
        )
    elif kind is FailureKind.UNREADABLE_PAGE:
        text = f"Tabstack could not read that page{suffix}."
    elif kind is FailureKind.INVALID_URL:
        text = "That address cannot be read: only http and https pages work."
        if detail:
            text = f"{text} {detail}"
        return f"Error: {text}{_RELAY_URL}"
    else:
        text = f"The Tabstack tool hit an unexpected problem{suffix}."
    return f"Error: {text}{_RELAY}"


def research_failure_message(outcome: ResearchFailed | ResearchIncomplete) -> str:
    """Return the chat message for a Research stream that did not complete."""
    if isinstance(outcome, ResearchIncomplete):
        return failure_message(FailureKind.STREAM_INCOMPLETE, "research")
    detail = clean_detail(outcome.message)
    if outcome.activity:
        detail = (
            f"during {outcome.activity}: {detail}"
            if detail
            else f"during {outcome.activity}"
        )
    return failure_message(FailureKind.STREAM_ERROR, "research", detail=detail)


def exception_message(
    exc: BaseException,
    operation: Operation,
    *,
    secret: str | None,
    stream_started: bool = False,
    timeout_seconds: float | None = None,
    idle_seconds: float | None = None,
) -> str:
    """Return the chat message for an exception, with server detail redacted."""
    kind = classify_exception(exc, operation, stream_started=stream_started)
    if kind is FailureKind.UNEXPECTED:
        # Only the class name: an arbitrary exception message could carry anything.
        detail: str | None = type(exc).__name__
    else:
        detail = clean_detail(error_body_detail(exc), secret)
    return failure_message(
        kind,
        operation,
        detail=detail,
        timeout_seconds=timeout_seconds,
        idle_seconds=idle_seconds,
    )


# =============================================================================
# Open WebUI wrapper
# =============================================================================

EventEmitter = Callable[[dict[str, object]], Awaitable[None]]
ClientFactory = Callable[[str, float], tabstack.AsyncTabstack]


def _default_client(api_key: str, timeout: float) -> tabstack.AsyncTabstack:
    """Build a Tabstack client for one call."""
    # No retries are added on top of the SDK. Its default (two retries on 408,
    # 409, 429, 5xx and connection failures, before a stream opens) is left as
    # it is, and the tool never re-runs a call itself: repeating a Research call
    # repeats the work and the billing, so a failure is reported to the user
    # instead. `timeout` is the longest wait for any one chunk of the response.
    return tabstack.AsyncTabstack(api_key=api_key, timeout=timeout)


def _event_dict(event: object) -> Mapping[str, object]:
    """Return a streamed event in wire format, whether the SDK typed it or not."""
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        return _as_map(to_dict())
    # The SDK passes event names it does not know through as plain dicts.
    return _as_map(event)


class _Status:
    """Send status and citation events; tolerate a missing or broken emitter."""

    def __init__(self, emit: EventEmitter | None) -> None:
        """Wrap the emitter Open WebUI injected, which may be None."""
        self._emit = emit

    async def send(self, event: dict[str, object]) -> None:
        """Send one event. A failure to display progress must not fail the call."""
        if self._emit is None:
            return
        with contextlib.suppress(Exception):
            await self._emit(event)

    async def update(self, description: str) -> None:
        """Show an in-progress status line."""
        await self.send(
            {"type": "status", "data": {"description": description, "done": False}}
        )

    async def done(self, description: str) -> None:
        """Show the final status line, replacing the in-progress one."""
        await self.send(
            {"type": "status", "data": {"description": description, "done": True}}
        )


class Tools:
    """Tabstack Research and Extract for Open WebUI."""

    class Valves(BaseModel):
        """Admin settings, set in Workspace > Tools > Tabstack > Valves."""

        tabstack_api_key: str = Field(
            default="",
            description="Your Tabstack API key. Never shown in chat.",
            json_schema_extra={"input": {"type": "password"}},
        )
        research_mode: Literal["fast", "balanced"] = Field(
            default="fast",
            description="'balanced' reads more sources and needs a paid plan.",
        )
        research_timeout_seconds: int = Field(
            default=300,
            ge=10,
            le=3600,
            description="Stop a Research call that runs longer than this, in seconds.",
        )
        research_idle_timeout_seconds: int = Field(
            default=120,
            ge=10,
            le=900,
            description="Stop a Research call after this many silent seconds.",
        )
        read_url_timeout_seconds: int = Field(
            default=60,
            ge=5,
            le=600,
            description="Stop reading a page after this many seconds.",
        )
        read_url_effort: Literal["min", "standard", "max"] = Field(
            default="standard",
            description="'max' renders JavaScript-heavy pages but is slower.",
        )
        read_url_max_chars: int = Field(
            default=20000,
            ge=1000,
            le=500000,
            description="Longest page text passed to the model, in characters.",
        )

    def __init__(self) -> None:
        """Set up valves; Open WebUI swaps in the saved values on each call."""
        self.valves = self.Valves()
        # Seam for tests. Underscore names are not exposed to the model.
        self._client_factory: ClientFactory = _default_client

    async def research(
        self,
        question: str,
        __event_emitter__: EventEmitter | None = None,
    ) -> str:
        """
        Answer a question that needs current information from the web. Returns a cited
        answer from live sources with numbered [n] markers and a source list. Use it for
        recent events, releases, prices, or anything that may have changed since your
        training data. It can take a minute or more.

        :param question: The full question in plain language, including any names,
            places or dates it depends on.
        """
        valves = self.valves
        status = _Status(__event_emitter__)
        key = valves.tabstack_api_key.strip()
        if not key:
            await status.done(failed_status("research"))
            return failure_message(FailureKind.MISSING_KEY, "research")
        query = question.strip()
        if not query:
            await status.done(failed_status("research"))
            return failure_message(
                FailureKind.BAD_REQUEST, "research", detail="the question was empty"
            )

        total = valves.research_timeout_seconds
        idle = valves.research_idle_timeout_seconds
        progress = ResearchProgress()
        await status.update("Starting research")
        try:
            async with (
                asyncio.timeout(total),
                self._client_factory(key, idle) as client,
            ):
                stream = await client.agent.research(
                    query=query, mode=valves.research_mode
                )
                async with stream:
                    async for event in stream:
                        line = progress.feed(_event_dict(event))
                        if line is not None:
                            await status.update(line)
                        if progress.outcome is not None:
                            break
        except asyncio.CancelledError:
            await status.done("Research stopped")
            raise
        except Exception as exc:
            await status.done(failed_status("research"))
            return exception_message(
                exc,
                "research",
                secret=key,
                stream_started=progress.events_seen > 0,
                timeout_seconds=total,
                idle_seconds=idle,
            )

        outcome = progress.result()
        if not isinstance(outcome, ResearchCompleted):
            await status.done(failed_status("research"))
            return research_failure_message(outcome)
        if not outcome.report:
            await status.done(failed_status("research"))
            return failure_message(
                FailureKind.STREAM_ERROR,
                "research",
                detail="Tabstack returned an empty answer",
            )

        answer = format_research_answer(outcome.report, outcome.sources)
        for source in answer.sources:
            await status.send(citation_event(source))
        await status.done(finished_status(len(answer.sources)))
        return answer.text

    async def read_url(
        self,
        url: str,
        __event_emitter__: EventEmitter | None = None,
    ) -> str:
        """
        Read one web page and return its text as clean Markdown. Use it when the user
        gives a link or asks about a specific page. Only http and https addresses work.

        :param url: The full address of the page, starting with http:// or https://.
        """
        valves = self.valves
        status = _Status(__event_emitter__)
        target = clean_url(url)
        problem = url_problem(target)
        if problem is not None:
            await status.done(failed_status("read_url"))
            return failure_message(FailureKind.INVALID_URL, "read_url", detail=problem)
        key = valves.tabstack_api_key.strip()
        if not key:
            await status.done(failed_status("read_url"))
            return failure_message(FailureKind.MISSING_KEY, "read_url")

        host = urlsplit(target).hostname or target
        limit = valves.read_url_timeout_seconds
        await status.update(f"Reading {host}")
        try:
            async with (
                asyncio.timeout(limit),
                self._client_factory(key, limit) as client,
            ):
                page = await client.extract.markdown(
                    url=target, metadata=True, effort=valves.read_url_effort
                )
        except asyncio.CancelledError:
            await status.done("Stopped reading the page")
            raise
        except Exception as exc:
            await status.done(failed_status("read_url"))
            return exception_message(
                exc, "read_url", secret=key, timeout_seconds=limit, idle_seconds=limit
            )

        if not page.content.strip():
            await status.done(failed_status("read_url"))
            return failure_message(
                FailureKind.UNREADABLE_PAGE,
                "read_url",
                detail="the page had no readable text",
            )
        title = _as_str(page.metadata.title) if page.metadata else None
        page_url = _as_str(page.url) or target
        max_chars = valves.read_url_max_chars
        await status.send(
            page_citation_event(
                page_url, title, trim_markdown(page.content, max_chars).text
            )
        )
        await status.done(f"Read {host}")
        return format_page(page_url, title, page.content, max_chars)
