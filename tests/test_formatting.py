"""Citation formatting, Markdown trimming and URL checks."""

from __future__ import annotations

import pytest
from conftest import load_events, load_json

from tabstack_tool import (
    CitedSource,
    ResearchCompleted,
    ResearchProgress,
    citation_event,
    clean_url,
    dedupe_sources,
    format_page,
    format_research_answer,
    page_citation_event,
    renumber_markers,
    trim_markdown,
    truncation_note,
    url_identity,
    url_problem,
)


def completed_fixture() -> ResearchCompleted:
    progress = ResearchProgress()
    for event in load_events("research_complete.jsonl"):
        progress.feed(event)
    outcome = progress.result()
    assert isinstance(outcome, ResearchCompleted)
    return outcome


# -- url_identity -------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "https://example.com/docs/tools",
        "http://example.com/docs/tools",
        "https://www.example.com/docs/tools",
        "https://EXAMPLE.com/docs/tools/",
        "https://example.com/docs/tools#section",
        "https://example.com:443/docs/tools",
        "https://example.com/docs/tools?utm_source=x&utm_medium=y",
    ],
)
def test_url_variants_share_identity(variant: str) -> None:
    assert url_identity(variant) == url_identity("https://example.com/docs/tools")


@pytest.mark.parametrize(
    "different",
    [
        "https://example.com/docs/tools.md",
        "https://example.com/Docs/tools",
        "https://example.com/docs/tools?page=2",
        "https://docs.example.com/docs/tools",
        "https://example.com:8080/docs/tools",
    ],
)
def test_different_pages_keep_separate_identity(different: str) -> None:
    assert url_identity(different) != url_identity("https://example.com/docs/tools")


def test_query_order_does_not_matter() -> None:
    assert url_identity("https://example.com/a?b=2&a=1") == url_identity(
        "https://example.com/a?a=1&b=2"
    )


# -- dedupe_sources -----------------------------------------------------------


def test_dedupe_merges_variants_and_records_it() -> None:
    sources = [
        CitedSource(url="http://example.com/a/", title=None),
        CitedSource(url="https://example.org/b", title="B"),
        CitedSource(url="https://www.example.com/a", title="A", claims=("c1",)),
    ]
    numbered, mapping, merges = dedupe_sources(sources)
    assert [s.number for s in numbered] == [1, 2]
    assert numbered[0].url == "https://www.example.com/a"  # https preferred for display
    assert numbered[0].title == "A"  # first non-empty title across the group
    assert numbered[0].claims == ("c1",)
    assert numbered[0].merged_urls == ("http://example.com/a/",)
    assert mapping == {1: 1, 2: 2, 3: 1}
    assert len(merges) == 1
    assert (merges[0].original, merges[0].kept, merges[0].url) == (
        3,
        1,
        "https://www.example.com/a",
    )


def test_dedupe_without_duplicates_is_identity() -> None:
    sources = [CitedSource(url="https://example.com/1"), CitedSource(url="https://example.com/2")]
    numbered, mapping, merges = dedupe_sources(sources)
    assert mapping == {1: 1, 2: 2}
    assert merges == ()
    assert all(s.merged_urls == () for s in numbered)


# -- renumber_markers ---------------------------------------------------------


def test_renumber_rewrites_and_collapses_runs() -> None:
    text, unmatched = renumber_markers("A [1][3]. B [2][3][1].", {1: 1, 2: 2, 3: 1})
    assert text == "A [1]. B [2][1]."
    assert unmatched == ()


def test_renumber_reports_unknown_markers_and_leaves_them() -> None:
    text, unmatched = renumber_markers("A [1] and [7].", {1: 1})
    assert text == "A [1] and [7]."
    assert unmatched == (7,)


def test_renumber_skips_links_and_code_fences() -> None:
    source = "See [1](https://example.com) and [2].\n```\nx = a[2]\n```\n"
    text, _ = renumber_markers(source, {1: 9, 2: 1})
    assert text == "See [1](https://example.com) and [1].\n```\nx = a[2]\n```\n"


# -- format_research_answer ---------------------------------------------------


def test_complete_fixture_answer() -> None:
    outcome = completed_fixture()
    answer = format_research_answer(outcome.report, outcome.sources)

    assert [s.url for s in answer.sources] == [
        "https://www.example.com/docs/tools",
        "https://news.example.net/2026/10/release",
        "https://docs.example.org/web-search.md",
        "https://docs.example.org/web-search",
    ]
    # [3] was the http variant of [1]; Tabstack's [4] and [5] become [3] and [4].
    assert "tools overview page in October 2026 [1]." in answer.text
    assert "the screen is optional [1]." in answer.text
    assert "own page [3][4]." in answer.text
    assert "together [1][2]." in answer.text
    assert "mentions [9] must not" in answer.text
    assert answer.unmatched_markers == ()
    assert "[1] Tools overview - https://www.example.com/docs/tools" in answer.text
    assert "[4] Web search - https://docs.example.org/web-search" in answer.text
    assert (
        "Note: 1 cited address was a variant of a page already listed and merged: "
        "http://example.com/docs/tools/ into [1]." in answer.text
    )
    assert "keep the [n] markers" in answer.text


def test_answer_with_no_sources_says_so() -> None:
    answer = format_research_answer("Plain text.", [])
    assert "Tabstack returned no cited sources" in answer.text
    assert answer.sources == ()


def test_answer_notes_unmatched_marker() -> None:
    answer = format_research_answer("Claim [2].", [CitedSource(url="https://example.com")])
    assert "Note: the answer refers to [2], which has no matching source." in answer.text


# -- citation events ----------------------------------------------------------


def test_citation_event_shape_matches_open_webui() -> None:
    outcome = completed_fixture()
    answer = format_research_answer(outcome.report, outcome.sources)
    event = citation_event(answer.sources[0])
    assert event["type"] == "citation"
    data = event["data"]
    assert isinstance(data, dict)
    assert data["metadata"] == [
        {"source": "https://www.example.com/docs/tools", "name": "Tools overview"}
    ]
    assert data["source"] == {"name": "Tools overview", "url": "https://www.example.com/docs/tools"}
    assert data["document"] == [
        "Also cited as:\n- http://example.com/docs/tools/",
    ]


def test_citation_event_falls_back_to_url_label() -> None:
    numbered, _, _ = dedupe_sources([CitedSource(url="https://example.com/x")])
    data = citation_event(numbered[0])["data"]
    assert isinstance(data, dict)
    assert data["source"] == {"name": "https://example.com/x", "url": "https://example.com/x"}
    assert data["document"] == ["Cited by Tabstack Research."]


def test_page_citation_event() -> None:
    data = page_citation_event("https://example.com", None, "text")["data"]
    assert isinstance(data, dict)
    assert data["metadata"] == [{"source": "https://example.com", "name": "https://example.com"}]


# -- trimming -----------------------------------------------------------------


def test_short_markdown_is_untouched() -> None:
    trimmed = trim_markdown("  # Title\n\nBody.  ", 100)
    assert trimmed.text == "# Title\n\nBody."
    assert not trimmed.trimmed


def test_trim_prefers_paragraph_break() -> None:
    text = "First paragraph here.\n\nSecond paragraph that is longer than the limit allows."
    trimmed = trim_markdown(text, 40)
    assert trimmed.text == "First paragraph here."
    assert trimmed.kept_chars == 21
    assert trimmed.total_chars == len(text)
    assert trimmed.trimmed


def test_trim_hard_cuts_when_no_break_is_close() -> None:
    trimmed = trim_markdown("x" * 50, 10)
    assert trimmed.text == "x" * 10


def test_trim_closes_an_open_code_fence() -> None:
    text = "Intro.\n\n```python\n" + "line\n" * 40 + "```\n"
    trimmed = trim_markdown(text, 60)
    assert trimmed.text.endswith("\n```")
    assert trimmed.text.count("```") == 2


def test_trim_drops_a_dangling_heading() -> None:
    text = "Intro paragraph that is long enough.\n\n## Next section\n\n" + "body " * 40
    trimmed = trim_markdown(text, 60)
    assert trimmed.text == "Intro paragraph that is long enough."
    assert trimmed.kept_chars == len(trimmed.text)


def test_trim_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        trim_markdown("x", 0)


def test_truncation_note_states_amount_kept() -> None:
    trimmed = trim_markdown("a " * 50_000, 20_000)
    note = truncation_note(trimmed)
    assert note.startswith(
        f"[Trimmed: kept the first {trimmed.kept_chars:,} of 99,999 characters ("
    )
    assert "The rest of the page is not included." in note


def test_format_page_from_fixture() -> None:
    page = load_json("extract_page.json")
    content = page["content"]
    assert isinstance(content, str)
    text = format_page("https://news.example.net/2026/10/release", "Release notes", content, 500)
    assert text.startswith("Page: Release notes\nURL: https://news.example.net/2026/10/release\n\n")
    assert "[Trimmed: kept the first " in text
    untrimmed = format_page("https://example.com", None, content, 100_000)
    assert untrimmed.startswith("Page: (no title)")
    assert "[Trimmed" not in untrimmed


# -- URL checks ---------------------------------------------------------------


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com/a?b=1"])
def test_http_urls_are_accepted(url: str) -> None:
    assert url_problem(url) is None


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("", "No URL"),
        ("example.com/page", "has no scheme"),
        ("ftp://example.com/file", "'ftp:' addresses are not supported"),
        ("file:///etc/passwd", "'file:' addresses are not supported"),
        ("javascript:alert(1)", "'javascript:' addresses are not supported"),
        ("https://", "has no host name"),
    ],
)
def test_other_urls_are_refused_with_a_reason(url: str, fragment: str) -> None:
    problem = url_problem(url)
    assert problem is not None
    assert fragment in problem


def test_clean_url_strips_wrapping() -> None:
    assert clean_url("  <https://example.com>  ") == "https://example.com"
