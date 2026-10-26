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
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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


_DECORATIVE = str.maketrans(
    {
        "\u2014": "-",  # em dash
        "\u2013": "-",  # en dash
        "\u2012": "-",  # figure dash
        "\u2015": "-",  # horizontal bar
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
        "\u00a0": " ",  # no-break space
        "\u00b7": "-",  # middle dot, common in page titles
        "\u2022": "-",  # bullet
        "\u2192": "->",
        "\u2190": "<-",
        "\u2122": "",  # trade mark sign
        "\u00ae": "",  # registered sign
    }
)


_INVISIBLE = {"\u200d", "\ufe0f", "\ufe0e"}  # joiner and emoji variation selectors


def plain_punctuation(text: str) -> str:
    """Make decorative punctuation ASCII and drop emoji and other symbols."""
    kept = (
        ch
        for ch in text.translate(_DECORATIVE)
        if ch not in _INVISIBLE and unicodedata.category(ch) != "So"
    )
    return " ".join("".join(kept).split())


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
    already_shown: bool = False


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


def _plain_title(value: object) -> str | None:
    """Return a page title with decorative punctuation made plain, or None."""
    title = _as_str(value)
    return _as_str(plain_punctuation(title)) if title else None


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
                title=_plain_title(page.get("title")),
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


def place_after(
    sources: Sequence[NumberedSource], shown: Sequence[str]
) -> tuple[tuple[NumberedSource, ...], dict[int, int]]:
    """Number sources after those already on the message; repeats keep their number.

    Open WebUI numbers a message's citation chips by the order its sources
    arrived, so a second call in one reply has to continue from there.
    """
    positions: dict[str, int] = {}
    for number, url in enumerate(shown, start=1):
        positions.setdefault(url_identity(url), number)
    next_number = len(shown) + 1
    placed: list[NumberedSource] = []
    renumbered: dict[int, int] = {}
    for source in sources:
        existing = positions.get(url_identity(source.url))
        if existing is None:
            number, next_number = next_number, next_number + 1
        else:
            number = existing
        renumbered[source.number] = number
        placed.append(
            replace(source, number=number, already_shown=existing is not None)
        )
    placed.sort(key=lambda source: source.number)
    return tuple(placed), renumbered


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


# Small models sometimes call a tool again after a good answer, and every call
# is billed, so successful results end with an explicit instruction too.
RESEARCH_DONE = (
    "This is the complete answer from Tabstack. Answer the user from it now, and "
    "keep the [n] markers next to the points they support so the user can see the "
    "source for each point. Do not call research again for this question."
)
READ_URL_DONE = (
    "This is the page text from Tabstack. Answer the user from it now. Do not call "
    "read_url again for this page."
)


def format_research_answer(
    report: str, sources: Sequence[CitedSource], shown: Sequence[str] = ()
) -> FormattedAnswer:
    """Build the model-facing answer: renumbered report, source list and any notes.

    `shown` lists the source URLs already on the message, in the order they were
    sent, so the numbers here continue from them.
    """
    deduped, local, local_merges = dedupe_sources(sources)
    numbered, renumbered = place_after(deduped, shown)
    mapping = {old: renumbered[new] for old, new in local.items()}
    merges = tuple(replace(m, kept=renumbered[m.kept]) for m in local_merges)
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
    lines.append(RESEARCH_DONE)
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


# Words too common to say what part of a page matters.
_COMMON_WORDS = (
    "about and are does for from has have how into its page tell that the their "
    "them this was what when where which who why will with"
)
_FOCUS_STOPWORDS = frozenset(_COMMON_WORDS.split())
_HEADING = re.compile(r"^#{1,6}\s")
_GAP = "[...]"
# Pages often come through with few or no headings, so long sections are cut
# into parts of about this many characters at paragraph breaks.
PART_CHARS = 2000


def focus_terms(focus: str) -> tuple[str, ...]:
    """Return the words of focus worth searching for, in order, without repeats."""
    words = re.findall(r"[a-z0-9]+", focus.lower())
    return tuple(
        dict.fromkeys(w for w in words if len(w) >= 3 and w not in _FOCUS_STOPWORDS)
    )


def split_sections(markdown: str, part_chars: int = PART_CHARS) -> list[str]:
    """Split markdown into parts: at headings, and at paragraph breaks in long text.

    Code fences are never split, so a part can be longer than part_chars.
    """
    parts: list[list[str]] = [[]]
    size = 0
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and parts[-1]:
            at_heading = _HEADING.match(line) is not None
            at_break = not line.strip() and size >= part_chars
            if at_heading or at_break:
                parts.append([])
                size = 0
        parts[-1].append(line)
        size += len(line) + 1
    return ["\n".join(lines).strip() for lines in parts if "".join(lines).strip()]


def _section_score(section: str, terms: Sequence[str]) -> int:
    """Count mentions of terms; a mention on the first line counts three times."""
    heading, _, body = section.partition("\n")
    score = 0
    for term in terms:
        pattern = re.compile(rf"\b{re.escape(term)}")
        score += 3 * len(pattern.findall(heading.lower()))
        score += len(pattern.findall(body.lower()))
    return score


@dataclass(frozen=True)
class FocusedText:
    """The parts of a long page that mention what the model asked about."""

    text: str
    kept_chars: int
    total_chars: int
    kept_parts: int
    total_parts: int
    terms: tuple[str, ...]


def focus_markdown(markdown: str, focus: str, max_chars: int) -> FocusedText | None:
    """Keep the best-matching parts that fit in max_chars, in page order.

    Returns None when the page already fits, focus has no useful words, or no
    part mentions them; the caller then keeps the start of the page.
    """
    text = markdown.strip()
    terms = focus_terms(focus)
    if len(text) <= max_chars or not terms:
        return None
    sections = split_sections(text)
    scores = [_section_score(section, terms) for section in sections]
    ranked = sorted(
        (i for i, score in enumerate(scores) if score > 0),
        key=lambda i: (-scores[i], i),
    )
    if not ranked:
        return None
    gap_cost = len(_GAP) + 4
    budget = max_chars
    chosen: dict[int, str] = {}
    for index in ranked:
        section = sections[index]
        if len(section) + gap_cost <= budget:
            chosen[index] = section
            budget -= len(section) + gap_cost
        elif not chosen:
            # The best part alone is too long: keep its start.
            chosen[index] = trim_markdown(section, max(budget - gap_cost, 1)).text
            break
    parts: list[str] = []
    previous = -1
    for index in sorted(chosen):
        if index != previous + 1:
            parts.append(_GAP)
        parts.append(chosen[index])
        previous = index
    if previous != len(sections) - 1:
        parts.append(_GAP)
    return FocusedText(
        text="\n\n".join(parts),
        kept_chars=sum(len(part) for part in chosen.values()),
        total_chars=len(text),
        kept_parts=len(chosen),
        total_parts=len(sections),
        terms=terms,
    )


def focus_note(focused: FocusedText) -> str:
    """Return a one-line note saying which part of the page was kept."""
    return (
        f"[Focused on: {', '.join(focused.terms)}. Kept {focused.kept_parts} of "
        f"{focused.total_parts} parts of the page ({focused.kept_chars:,} of "
        f"{focused.total_chars:,} characters), in page order; {_GAP} marks parts "
        "left out.]"
    )


def page_body(markdown: str, max_chars: int, focus: str = "") -> tuple[str, str | None]:
    """Return the page text to pass on and a note on what was left out, if anything."""
    focused = focus_markdown(markdown, focus, max_chars)
    if focused is not None:
        return focused.text, focus_note(focused)
    trimmed = trim_markdown(markdown, max_chars)
    if not trimmed.trimmed:
        return trimmed.text, None
    note = truncation_note(trimmed)
    if focus_terms(focus):
        note += (
            f" Nothing on the page mentions {', '.join(focus_terms(focus))}, so the "
            "start of the page is shown."
        )
    return trimmed.text, note


def format_page(
    url: str, title: str | None, markdown: str, max_chars: int, focus: str = ""
) -> str:
    """Build the model-facing text for one page read with read_url."""
    body, note = page_body(markdown, max_chars, focus)
    lines = [
        f"Page: {title}" if title else "Page: (no title)",
        f"URL: {url}",
        "",
        body,
    ]
    if note:
        lines.extend(["", note])
    lines.extend(["", READ_URL_DONE])
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


# -- Call limits --------------------------------------------------------------


@dataclass
class CallCounter:
    """Count calls per key (one key per reply) so a looping model cannot repeat them."""

    max_keys: int = 1024
    counts: dict[str, int] = field(default_factory=dict[str, int])

    def take(self, key: str, limit: int) -> bool:
        """Record a call for key and return True, or return False if limit is used."""
        used = self.counts.get(key, 0)
        if used >= limit:
            return False
        self.counts.pop(key, None)
        self.counts[key] = used + 1
        while len(self.counts) > self.max_keys:
            del self.counts[next(iter(self.counts))]
        return True


@dataclass
class ShownSources:
    """Remember, per reply, the citation URLs already sent to the message, in order."""

    max_keys: int = 1024
    urls: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def get(self, key: str) -> tuple[str, ...]:
        """Return the URLs sent so far for key."""
        return tuple(self.urls.get(key, ()))

    def add(self, key: str, urls: Sequence[str]) -> None:
        """Record URLs sent for key. Open WebUI merges a repeated URL into one chip."""
        known = self.urls.pop(key, [])
        known.extend(u for u in dict.fromkeys(urls) if u not in known)
        self.urls[key] = known
        while len(self.urls) > self.max_keys:
            del self.urls[next(iter(self.urls))]


# -- API keys -----------------------------------------------------------------

KeyOwner = Literal["shared", "user"]


@dataclass(frozen=True)
class ChosenKey:
    """The API key a call will use, and whose it is."""

    value: str
    owner: KeyOwner


def choose_key(
    shared_key: str, user_key: str, *, require_user_key: bool
) -> ChosenKey | None:
    """Pick the user's own key if set, else the shared key unless it is not allowed."""
    own = user_key.strip()
    if own:
        return ChosenKey(own, "user")
    shared = shared_key.strip()
    if shared and not require_user_key:
        return ChosenKey(shared, "shared")
    return None


# -- Errors -------------------------------------------------------------------


class FailureKind(Enum):
    """Every way a call can fail, as far as the chat is concerned."""

    MISSING_KEY = "missing_key"
    MISSING_USER_KEY = "missing_user_key"
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
    CALL_LIMIT = "call_limit"
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
# The panel and labels as Open WebUI v0.12.3 shows them.
_USER_VALVES = " > ".join(
    (
        "Controls (the sliders icon at the top right of a chat)",
        "Valves",
        "Tools",
        "Tabstack",
    )
)
# In testing, a model relaying a missing-key message also invented a command
# for the user to run, so the relay line rules out additions.
_RELAY = (
    " Tell the user exactly this and add nothing about how to fix it. Do not call "
    "this tool again for this question."
)
_RELAY_URL = (
    " Tell the user exactly this and add nothing about how to fix it. Only call "
    "read_url again with a full address that starts with http:// or https://."
)


def failure_message(
    kind: FailureKind,
    operation: Operation,
    *,
    detail: str | None = None,
    timeout_seconds: float | None = None,
    idle_seconds: float | None = None,
    call_limit: int | None = None,
    key_owner: KeyOwner = "shared",
) -> str:
    """Return a plain chat message for a failure. Never includes a stack trace."""
    suffix = f" ({detail})" if detail else ""
    what = "The research" if operation == "research" else "Reading the page"
    own = key_owner == "user"
    if kind is FailureKind.MISSING_KEY:
        text = (
            "The Tabstack tool is not set up yet: it has no API key. An Open WebUI "
            "admin needs to add one in the tool's Valves, or you can add your own "
            f"under {_USER_VALVES}."
        )
    elif kind is FailureKind.MISSING_USER_KEY:
        text = (
            "The Tabstack tool needs your own API key, and none is set. Add it under "
            f"{_USER_VALVES}."
        )
    elif kind is FailureKind.REJECTED_KEY:
        text = (
            f"Tabstack rejected your API key. Check it under {_USER_VALVES}."
            if own
            else "Tabstack rejected the tool's API key. An Open WebUI admin needs to "
            "check the key in the tool's Valves."
        )
    elif kind is FailureKind.NO_PERMISSION:
        whose = "Your Tabstack API key" if own else "The Tabstack API key"
        text = f"{whose} is not allowed to make this request{suffix}."
    elif kind is FailureKind.OUT_OF_CREDITS:
        whose = (
            "Your Tabstack account" if own else "The Tabstack account behind this tool"
        )
        text = f"{whose} is out of credits, so the request was declined."
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
    elif kind is FailureKind.CALL_LIMIT:
        # In testing, a model given this answered the first question and said
        # nothing about the one that was refused, so name it and ask for that.
        times = f" {_plural(call_limit, 'time')}" if call_limit else ""
        target = clean_detail(detail, limit=150)
        refused = f' for "{target.replace(chr(34), chr(39))}"' if target else ""
        text = (
            f"{operation} already ran{times} for this reply, so it was not run "
            f"again{refused} and nothing was charged."
        )
        return (
            f"Error: {text} Answer the user from the earlier result, and tell them "
            "plainly that this part was not looked up. Do not call this tool again "
            "for this question."
        )
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
    key_owner: KeyOwner = "shared",
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
        key_owner=key_owner,
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

    @property
    def active(self) -> bool:
        """True when Open WebUI gave the tool an emitter to send events to."""
        return self._emit is not None

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
        max_research_calls_per_reply: int = Field(
            default=1,
            ge=1,
            le=10,
            description="Research calls allowed in one reply. Each call is billed.",
        )
        max_read_url_calls_per_reply: int = Field(
            default=3,
            ge=1,
            le=20,
            description="read_url calls allowed in one reply. Each call is billed.",
        )
        read_url_max_chars: int = Field(
            default=20000,
            ge=1000,
            le=500000,
            description="Longest page text passed to the model, in characters.",
        )
        require_user_key: bool = Field(
            default=False,
            description="Only use each user's own key; never the key above.",
        )

    class UserValves(BaseModel):
        """Per-user settings, set in a chat under Controls > Valves."""

        tabstack_api_key: str = Field(
            default="",
            description="Your own Tabstack API key. Calls are billed to it.",
            json_schema_extra={"input": {"type": "password"}},
        )

    def __init__(self) -> None:
        """Set up valves; Open WebUI swaps in the saved values on each call."""
        self.valves = self.Valves()
        # Seam for tests. Underscore names are not exposed to the model.
        self._client_factory: ClientFactory = _default_client
        # Open WebUI keeps one instance per tool, so this counter outlives a
        # single call on purpose: it caps billed calls per reply even when a
        # model keeps calling the tool. Each Open WebUI worker counts separately.
        self._calls = CallCounter()
        # Citation numbers are per message, so a later call in the same reply
        # has to number its sources after the ones already sent.
        self._shown = ShownSources()

    def _choose_key(self, user: Mapping[str, object] | None) -> ChosenKey | None:
        """Pick the key for this call from the user's and the admin's valves."""
        user_valves = (user or {}).get("valves")
        if isinstance(user_valves, Mapping):
            own = _as_str(
                cast(Mapping[str, object], user_valves).get("tabstack_api_key")
            )
        else:
            own = _as_str(getattr(user_valves, "tabstack_api_key", None))
        return choose_key(
            self.valves.tabstack_api_key,
            own or "",
            require_user_key=self.valves.require_user_key,
        )

    def _missing_key(self) -> FailureKind:
        """Return the failure for a call with no usable key."""
        if self.valves.require_user_key:
            return FailureKind.MISSING_USER_KEY
        return FailureKind.MISSING_KEY

    async def research(
        self,
        question: str,
        __event_emitter__: EventEmitter | None = None,
        __message_id__: str | None = None,
        __user__: Mapping[str, object] | None = None,
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
        chosen = self._choose_key(__user__)
        if chosen is None:
            await status.done(failed_status("research"))
            return failure_message(self._missing_key(), "research")
        key = chosen.value
        query = question.strip()
        if not query:
            await status.done(failed_status("research"))
            return failure_message(
                FailureKind.BAD_REQUEST, "research", detail="the question was empty"
            )
        limit = valves.max_research_calls_per_reply
        # Open WebUI only runs tools itself for a request with a message id.
        reply = __message_id__
        if reply and not self._calls.take(f"research:{reply}", limit):
            await status.done("Research not run again")
            return failure_message(
                FailureKind.CALL_LIMIT, "research", detail=query, call_limit=limit
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
                key_owner=chosen.owner,
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

        shown = self._shown.get(reply) if reply else ()
        answer = format_research_answer(outcome.report, outcome.sources, shown)
        new = [s for s in answer.sources if not s.already_shown]
        for source in new:
            await status.send(citation_event(source))
        if reply and status.active:
            self._shown.add(reply, [s.url for s in new])
        await status.done(finished_status(len(answer.sources)))
        return answer.text

    async def read_url(
        self,
        url: str,
        focus: str = "",
        __event_emitter__: EventEmitter | None = None,
        __message_id__: str | None = None,
        __user__: Mapping[str, object] | None = None,
    ) -> str:
        """
        Read one web page and return its text as clean Markdown. Use it when the user
        gives a link or asks about a specific page. Only http and https addresses work.

        :param url: The full address of the page, starting with http:// or https://.
        :param focus: Optional. A few words on what to look for, such as "pricing" or
            "release dates". On a long page, the parts that mention them are kept.
        """
        valves = self.valves
        status = _Status(__event_emitter__)
        target = clean_url(url)
        problem = url_problem(target)
        if problem is not None:
            await status.done(failed_status("read_url"))
            return failure_message(FailureKind.INVALID_URL, "read_url", detail=problem)
        chosen = self._choose_key(__user__)
        if chosen is None:
            await status.done(failed_status("read_url"))
            return failure_message(self._missing_key(), "read_url")
        key = chosen.value

        calls = valves.max_read_url_calls_per_reply
        # Open WebUI only runs tools itself for a request with a message id.
        reply = __message_id__
        if reply and not self._calls.take(f"read_url:{reply}", calls):
            await status.done("Page not read again")
            return failure_message(
                FailureKind.CALL_LIMIT, "read_url", detail=target, call_limit=calls
            )

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
                exc,
                "read_url",
                secret=key,
                timeout_seconds=limit,
                idle_seconds=limit,
                key_owner=chosen.owner,
            )

        if not page.content.strip():
            await status.done(failed_status("read_url"))
            return failure_message(
                FailureKind.UNREADABLE_PAGE,
                "read_url",
                detail="the page had no readable text",
            )
        title = _plain_title(page.metadata.title) if page.metadata else None
        page_url = _as_str(page.url) or target
        max_chars = valves.read_url_max_chars
        body, _ = page_body(page.content, max_chars, focus)
        await status.send(page_citation_event(page_url, title, body))
        if reply and status.active:
            self._shown.add(reply, [page_url])
        await status.done(f"Read {host}")
        return format_page(page_url, title, page.content, max_chars, focus)
