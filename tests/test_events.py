"""Research stream events: status lines and terminal outcomes."""

from __future__ import annotations

import pytest
from conftest import load_events

from tabstack_tool import (
    ResearchCompleted,
    ResearchFailed,
    ResearchIncomplete,
    ResearchProgress,
    finished_status,
    format_research_answer,
    research_failure_message,
    status_for_event,
)


def run(name: str) -> tuple[ResearchProgress, list[str]]:
    progress = ResearchProgress()
    statuses = [s for e in load_events(name) if (s := progress.feed(e)) is not None]
    return progress, statuses


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"event": "start", "data": {}}, "Planning searches"),
        ({"event": "planning:start", "data": {}}, "Planning searches"),
        (
            {"event": "iteration:start", "data": {"iteration": 2.0, "maxIterations": 3.0}},
            "Searching, round 2 of 3",
        ),
        ({"event": "iteration:start", "data": {}}, "Searching"),
        ({"event": "searching:end", "data": {"urlsNew": 1.0}}, "Found 1 new source"),
        ({"event": "searching:end", "data": {"urlsNew": 18.0}}, "Found 18 new sources"),
        ({"event": "prefetching:start", "data": {"urlCount": 2.0}}, "Fetching 2 linked pages"),
        ({"event": "analyzing:start", "data": {"pageCount": 20.0}}, "Reading 20 pages"),
        ({"event": "following:start", "data": {"linkCount": 1.0}}, "Following 1 link"),
        ({"event": "evaluating:start", "data": {}}, "Checking what is still missing"),
        ({"event": "outlining:start", "data": {}}, "Outlining the answer"),
        ({"event": "writing:start", "data": {"isRevision": False}}, "Writing the answer"),
        ({"event": "writing:start", "data": {"isRevision": True}}, "Revising the answer"),
        ({"event": "judging:start", "data": {}}, "Reviewing the draft"),
    ],
)
def test_status_for_event(event: dict[str, object], expected: str) -> None:
    assert status_for_event(event) == expected


@pytest.mark.parametrize(
    "event",
    [
        {"event": "planning:end", "data": {}},
        {"event": "iteration:end", "data": {}},
        {"event": "searching:end", "data": {}},
        {"event": "something:new", "data": {"message": "added later"}},
        {"data": {}},
        {},
    ],
)
def test_events_without_a_status_keep_the_current_one(event: dict[str, object]) -> None:
    assert status_for_event(event) is None


def test_complete_fixture() -> None:
    progress, statuses = run("research_complete.jsonl")
    assert statuses == [
        "Planning searches",
        "Searching, round 1 of 1",
        "Found 18 new sources",
        "Writing the answer",
    ]
    outcome = progress.result()
    assert isinstance(outcome, ResearchCompleted)
    assert len(outcome.sources) == 5
    assert outcome.report.startswith("The example tool added")


def test_error_fixture() -> None:
    progress, statuses = run("research_error.jsonl")
    assert statuses == ["Planning searches", "Searching, round 1 of 1"]
    outcome = progress.result()
    assert outcome == ResearchFailed(message="All search providers failed", activity="searching")
    assert isinstance(outcome, ResearchFailed)
    message = research_failure_message(outcome)
    assert message.startswith(
        "Error: the research failed (during searching: All search providers failed)."
    )
    assert "search.js" not in message
    assert "SearchProviderError" not in message


def test_no_terminal_fixture() -> None:
    progress, statuses = run("research_no_terminal.jsonl")
    assert statuses[-1] == "Writing the answer"
    outcome = progress.result()
    assert outcome == ResearchIncomplete(events_seen=8, last_event="writing:start")
    assert isinstance(outcome, ResearchIncomplete)
    assert research_failure_message(outcome).startswith(
        "Error: the connection to Tabstack closed before the research finished"
    )


def test_three_fixtures_give_three_different_results() -> None:
    texts: list[str] = []
    for name in ("research_complete.jsonl", "research_error.jsonl", "research_no_terminal.jsonl"):
        outcome = run(name)[0].result()
        if isinstance(outcome, ResearchCompleted):
            texts.append(format_research_answer(outcome.report, outcome.sources).text)
        else:
            texts.append(research_failure_message(outcome))
    assert len(set(texts)) == 3
    assert not texts[0].startswith("Error:")
    assert texts[1].startswith("Error:")
    assert texts[2].startswith("Error:")


def test_error_event_with_unpopulated_error_field() -> None:
    progress = ResearchProgress()
    progress.feed({"event": "error", "data": {"message": "Research failed", "error": None}})
    outcome = progress.result()
    assert outcome == ResearchFailed(message="Research failed", activity=None)
    assert isinstance(outcome, ResearchFailed)
    assert research_failure_message(outcome).startswith(
        "Error: the research failed (Research failed)."
    )


def test_error_event_with_nothing_at_all() -> None:
    progress = ResearchProgress()
    progress.feed({"event": "error", "data": {}})
    outcome = progress.result()
    assert isinstance(outcome, ResearchFailed)
    assert research_failure_message(outcome).startswith("Error: the research failed.")


def test_events_after_a_terminal_event_are_ignored() -> None:
    progress = ResearchProgress()
    progress.feed({"event": "error", "data": {"error": {"message": "boom"}}})
    assert progress.feed({"event": "writing:start", "data": {}}) is None
    progress.feed({"event": "complete", "data": {"report": "late", "metadata": {}}})
    assert isinstance(progress.result(), ResearchFailed)
    assert progress.events_seen == 1


def test_repeated_status_is_not_reemitted() -> None:
    progress = ResearchProgress()
    assert progress.feed({"event": "start", "data": {}}) == "Planning searches"
    assert progress.feed({"event": "planning:start", "data": {}}) is None


def test_complete_without_cited_pages() -> None:
    progress = ResearchProgress()
    progress.feed({"event": "complete", "data": {"report": "Answer.", "metadata": {}}})
    outcome = progress.result()
    assert outcome == ResearchCompleted(report="Answer.", sources=(), metrics=None)


def test_finished_status() -> None:
    assert finished_status(1) == "Research finished, 1 source"
    assert finished_status(4) == "Research finished, 4 sources"
