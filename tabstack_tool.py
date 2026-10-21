"""
title: Tabstack
description: Tabstack Research for cited answers from live sources, Extract for clean Markdown.
version: 0.1.0
license: MIT
requirements: tabstack==2.8.5
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import tabstack

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
        return {str(k): v for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
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
        return list(value)  # pyright: ignore[reportUnknownArgumentType]
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
    """Record that Tabstack's citation `original` (at `url`) was folded into final source `kept`."""

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
        claims = tuple(c for c in (_as_str(x) for x in _as_list(page.get("claims"))) if c)
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
    """Merge URL variants of the same page and return sources, old-to-new numbers and merges."""
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
        merged_urls = tuple(dict.fromkeys(s.url for _, s in members if s.url != display_url))
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


def renumber_markers(text: str, mapping: Mapping[int, int]) -> tuple[str, tuple[int, ...]]:
    """Rewrite [n] markers through mapping, drop repeats in a run, and list unknown markers."""
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


def format_research_answer(report: str, sources: Sequence[CitedSource]) -> FormattedAnswer:
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
        verb = "was a variant of a page" if len(merges) == 1 else "were variants of pages"
        lines.append("")
        lines.append(
            f"Note: {_plural(len(merges), 'cited address')} {verb} already listed "
            f"and merged: {pairs}."
        )
    if unmatched:
        refs = ", ".join(f"[{n}]" for n in unmatched)
        lines.append("")
        lines.append(f"Note: the answer refers to {refs}, which has no matching source.")

    lines.append("")
    lines.append(
        "When you use this answer, keep the [n] markers next to the points they support "
        "so the user can see the source for each point."
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
        parts.append("Claims drawn from this page:\n" + "\n".join(f"- {c}" for c in source.claims))
    if source.merged_urls:
        parts.append("Also cited as:\n" + "\n".join(f"- {u}" for u in source.merged_urls))
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
    """Return a status line for a non-terminal Research event, or None to keep the current one."""
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
        return f"Fetching {_plural(count, 'linked page')}" if count else "Fetching linked pages"
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
        return "Revising the answer" if data.get("isRevision") is True else "Writing the answer"
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
        return ResearchIncomplete(events_seen=self.events_seen, last_event=self.last_event)


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
    fence_lines = sum(1 for line in kept.splitlines() if line.lstrip().startswith("```"))
    if fence_lines % 2 == 1:
        rendered = kept + "\n```"
    return TrimmedText(text=rendered, kept_chars=len(kept), total_chars=total)


def truncation_note(trimmed: TrimmedText) -> str:
    """Return a one-line note saying how much of the page was kept."""
    percent = trimmed.kept_chars * 100 // trimmed.total_chars if trimmed.total_chars else 0
    return (
        f"[Trimmed: kept the first {trimmed.kept_chars:,} of {trimmed.total_chars:,} "
        f"characters ({percent}%). The rest of the page is not included.]"
    )


def format_page(url: str, title: str | None, markdown: str, max_chars: int) -> str:
    """Build the model-facing text for one page read with read_url."""
    trimmed = trim_markdown(markdown, max_chars)
    lines = [f"Page: {title}" if title else "Page: (no title)", f"URL: {url}", "", trimmed.text]
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


def clean_detail(text: str | None, secret: str | None = None, limit: int = 200) -> str | None:
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
        return FailureKind.UNREADABLE_PAGE if operation == "read_url" else FailureKind.BAD_REQUEST
    if isinstance(exc, tabstack.BadRequestError):
        return FailureKind.BAD_REQUEST
    if isinstance(exc, tabstack.InternalServerError):
        return FailureKind.UNREADABLE_PAGE if operation == "read_url" else FailureKind.SERVICE_ERROR
    if isinstance(exc, tabstack.APIStatusError):
        return FailureKind.OUT_OF_CREDITS if exc.status_code == 402 else FailureKind.SERVICE_ERROR
    if isinstance(exc, tabstack.APITimeoutError | TimeoutError | httpx.TimeoutException):
        return FailureKind.TIMEOUT
    if isinstance(exc, tabstack.APIConnectionError | httpx.TransportError):
        # Once events have arrived, a dropped connection means the stream closed early.
        return FailureKind.STREAM_INCOMPLETE if stream_started else FailureKind.CONNECTION
    return FailureKind.UNEXPECTED


_RELAY = " Tell the user this instead of answering from memory."


def failure_message(
    kind: FailureKind,
    operation: Operation,
    *,
    detail: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """Return a plain chat message for a failure. Never includes a stack trace."""
    suffix = f" ({detail})" if detail else ""
    what = "the research" if operation == "research" else "the page"
    if kind is FailureKind.MISSING_KEY:
        text = (
            "Error: the Tabstack tool has no API key. An Open WebUI admin needs to "
            "add one in the tool's Valves."
        )
    elif kind is FailureKind.REJECTED_KEY:
        text = (
            "Error: Tabstack rejected the API key. An Open WebUI admin needs to check "
            "the key in the tool's Valves."
        )
    elif kind is FailureKind.NO_PERMISSION:
        text = f"Error: the Tabstack API key is not allowed to make this request{suffix}."
    elif kind is FailureKind.OUT_OF_CREDITS:
        text = "Error: Tabstack declined the request because the account is out of credits."
    elif kind is FailureKind.RATE_LIMITED:
        text = "Error: Tabstack is rate limiting this account. Try again in a minute."
    elif kind is FailureKind.TIMEOUT:
        limit = f" within {timeout_seconds:g} seconds" if timeout_seconds else " in time"
        hint = (
            " Try a narrower question, or ask an admin to raise the time limit."
            if operation == "research"
            else ""
        )
        text = f"Error: {what} did not finish{limit} and was stopped.{hint}"
    elif kind is FailureKind.CONNECTION:
        text = "Error: could not reach Tabstack. Check the Open WebUI server's internet connection."
    elif kind is FailureKind.SERVICE_ERROR:
        text = f"Error: Tabstack returned a server error{suffix}."
    elif kind is FailureKind.BAD_REQUEST:
        text = f"Error: Tabstack rejected the request as invalid{suffix}."
    elif kind is FailureKind.STREAM_ERROR:
        text = f"Error: the research failed{suffix}."
    elif kind is FailureKind.STREAM_INCOMPLETE:
        text = (
            "Error: the connection to Tabstack closed before the research finished, "
            "so there is no answer to show."
        )
    elif kind is FailureKind.UNREADABLE_PAGE:
        text = f"Error: Tabstack could not read that page{suffix}."
    elif kind is FailureKind.INVALID_URL:
        text = f"Error: read_url only accepts http and https addresses. {detail or ''}".rstrip()
    else:
        text = f"Error: the Tabstack tool hit an unexpected problem{suffix}."
    return text + _RELAY


def research_failure_message(outcome: ResearchFailed | ResearchIncomplete) -> str:
    """Return the chat message for a Research stream that did not complete."""
    if isinstance(outcome, ResearchIncomplete):
        return failure_message(FailureKind.STREAM_INCOMPLETE, "research")
    detail = clean_detail(outcome.message)
    if outcome.activity:
        detail = f"during {outcome.activity}: {detail}" if detail else f"during {outcome.activity}"
    return failure_message(FailureKind.STREAM_ERROR, "research", detail=detail)


def exception_message(
    exc: BaseException,
    operation: Operation,
    *,
    secret: str | None,
    stream_started: bool = False,
    timeout_seconds: float | None = None,
) -> str:
    """Return the chat message for an exception, with any server detail cleaned and redacted."""
    kind = classify_exception(exc, operation, stream_started=stream_started)
    if kind is FailureKind.UNEXPECTED:
        # Only the class name: an arbitrary exception message could carry anything.
        detail: str | None = type(exc).__name__
    else:
        detail = clean_detail(error_body_detail(exc), secret)
    return failure_message(kind, operation, detail=detail, timeout_seconds=timeout_seconds)


# =============================================================================
# Open WebUI wrapper
# =============================================================================
