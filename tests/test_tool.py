"""The Open WebUI wrapper, driven by a fake SDK client. No test makes a network call."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from typing import Any, TypeVar, cast, get_type_hints

import httpx
import pytest
import tabstack
from conftest import load_events, load_json
from pydantic import Field, TypeAdapter, ValidationError, create_model
from tabstack.types import ExtractMarkdownResponse, ResearchEvent

import tabstack_tool
from tabstack_tool import Tools

KEY = "fake-key-for-tests-1234"
REQUEST = httpx.Request("POST", "https://api.tabstack.ai/v1/research")
EVENT_ADAPTER: TypeAdapter[ResearchEvent] = TypeAdapter(ResearchEvent)


def sdk_events(name: str) -> list[object]:
    """Load a fixture as SDK-typed events; fails if it does not match the SDK schema."""
    return [EVENT_ADAPTER.validate_python(e) for e in load_events(name)]


# -- Fake SDK -----------------------------------------------------------------


class FakeStream:
    """Stands in for tabstack.AsyncStream."""

    def __init__(
        self,
        events: Iterable[object],
        *,
        fail_with: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self.events = list(events)
        self.fail_with = fail_with
        self.delay = delay
        self.closed = False
        self.yielded = 0

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True

    async def _iterate(self) -> AsyncIterator[object]:
        for event in self.events:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.yielded += 1
            yield event
        if self.fail_with is not None:
            raise self.fail_with

    def __aiter__(self) -> AsyncIterator[object]:
        return self._iterate()


class FakeAgent:
    def __init__(self, stream: FakeStream | None, error: BaseException | None) -> None:
        self.stream = stream
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def research(self, **kwargs: object) -> FakeStream:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.stream is not None
        return self.stream


class FakeExtract:
    def __init__(
        self, page: dict[str, object] | None, error: BaseException | None
    ) -> None:
        self.page = page
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def markdown(self, **kwargs: object) -> ExtractMarkdownResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return ExtractMarkdownResponse.model_validate(self.page)


class FakeClient:
    def __init__(
        self,
        *,
        stream: FakeStream | None = None,
        research_error: BaseException | None = None,
        page: dict[str, object] | None = None,
        extract_error: BaseException | None = None,
    ) -> None:
        self.agent = FakeAgent(stream, research_error)
        self.extract = FakeExtract(page, extract_error)
        self.created_with: list[tuple[str, float]] = []
        self.exited = False

    def factory(self, api_key: str, timeout: float) -> tabstack.AsyncTabstack:
        self.created_with.append((api_key, timeout))
        return cast(tabstack.AsyncTabstack, self)

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.exited = True


class Recorder:
    """Collects what the tool sends to the chat."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def __call__(self, event: dict[str, object]) -> None:
        self.events.append(event)

    def statuses(self) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []
        for e in self.events:
            if e["type"] == "status":
                data = cast(dict[str, Any], e["data"])
                out.append((data["description"], data["done"]))
        return out

    def citations(self) -> list[dict[str, Any]]:
        return [
            cast(dict[str, Any], e["data"])
            for e in self.events
            if e["type"] == "citation"
        ]


def make_tool(client: FakeClient, **valves: object) -> Tools:
    tool = Tools()
    tool.valves = Tools.Valves.model_validate({"tabstack_api_key": KEY, **valves})
    tool._client_factory = client.factory  # pyright: ignore[reportPrivateUsage]
    return tool


T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def everything_sent(recorder: Recorder, result: str) -> str:
    return json.dumps(recorder.events) + result


# -- What Open WebUI sees -----------------------------------------------------


def public_callables(tool: object) -> list[str]:
    """Mirror open_webui.utils.tools.get_functions_from_tool (v0.12.3)."""
    return sorted(
        name
        for name in dir(tool)
        if callable(getattr(tool, name))
        and not name.startswith("_")
        and not inspect.isclass(getattr(tool, name))
    )


def model_schema(func: Callable[..., object]) -> dict[str, Any]:
    """Mirror convert_function_to_pydantic_model (v0.12.3) well enough to check it."""
    hints = get_type_hints(func)
    doc = func.__doc__ or ""
    fields: dict[str, Any] = {}
    for name, param in inspect.signature(func).parameters.items():
        default = param.default if param.default is not param.empty else ...
        if f":param {name}:" in doc:
            fields[name] = (hints.get(name, Any), Field(default, description="..."))
        else:
            # Open WebUI passes a bare (type, default) here, and pydantic drops
            # names with leading underscores. A `Field` for such a name raises.
            fields[name] = (hints.get(name, Any), default)
    return create_model(func.__name__, **fields).model_json_schema()


def test_only_research_and_read_url_are_exposed() -> None:
    assert public_callables(Tools()) == ["read_url", "research"]


def test_model_sees_only_the_plain_arguments() -> None:
    # Arguments with leading underscores are injected by Open WebUI.
    tool = Tools()
    assert list(model_schema(tool.research)["properties"]) == ["question"]
    read_url = model_schema(tool.read_url)
    assert list(read_url["properties"]) == ["url", "focus"]
    assert read_url["required"] == ["url"]


def test_docstrings_use_param_lines_open_webui_reads() -> None:
    assert ":param question:" in (Tools.research.__doc__ or "")
    assert ":param url:" in (Tools.read_url.__doc__ or "")


def test_docstrings_never_document_injected_arguments() -> None:
    # A `:param __event_emitter__:` line makes Open WebUI build a pydantic Field
    # for a dunder name, which raises NameError when the tool is loaded.
    for method in (Tools.research, Tools.read_url):
        assert ":param __" not in (method.__doc__ or "")


def test_valves_defaults_and_key_input() -> None:
    valves = Tools().valves
    assert valves.tabstack_api_key == ""
    assert valves.research_mode == "fast"
    assert valves.research_timeout_seconds == 300
    assert valves.research_idle_timeout_seconds == 120
    assert valves.read_url_timeout_seconds == 60
    assert valves.read_url_max_chars == 20000
    extra = Tools.Valves.model_fields["tabstack_api_key"].json_schema_extra
    assert extra == {"input": {"type": "password"}}


def test_valves_reject_out_of_range_values() -> None:
    with pytest.raises(ValidationError):
        Tools.Valves.model_validate({"research_timeout_seconds": 1})
    with pytest.raises(ValidationError):
        Tools.Valves.model_validate({"read_url_max_chars": 10})


def test_default_client_factory_uses_the_key_and_timeout() -> None:
    client = tabstack_tool._default_client(KEY, 42)  # pyright: ignore[reportPrivateUsage]
    assert client.api_key == KEY
    assert client.timeout == 42
    assert client.max_retries == 2  # the SDK default, unchanged


# -- research -----------------------------------------------------------------


def test_research_complete() -> None:
    stream = FakeStream(sdk_events("research_complete.jsonl"))
    client = FakeClient(stream=stream)
    recorder = Recorder()
    result = run(
        make_tool(client).research("What changed?", __event_emitter__=recorder)
    )

    assert client.created_with == [(KEY, 120)]
    assert client.agent.calls == [{"query": "What changed?", "mode": "fast"}]
    assert recorder.statuses() == [
        ("Starting research", False),
        ("Planning searches", False),
        ("Searching, round 1 of 1", False),
        ("Found 18 new sources", False),
        ("Writing the answer", False),
        ("Research finished, 4 sources", True),
    ]
    assert [c["source"]["url"] for c in recorder.citations()] == [
        "https://www.example.com/docs/tools",
        "https://news.example.net/2026/10/release",
        "https://docs.example.org/web-search.md",
        "https://docs.example.org/web-search",
    ]
    # Citations go out before the final status, so sources are in place when it clears.
    assert recorder.events[-1]["type"] == "status"
    assert result.startswith(
        "The example tool added a tools overview page in October 2026 [1]."
    )
    assert "[4] Web search - https://docs.example.org/web-search" in result
    assert stream.closed
    assert client.exited


def test_research_uses_valves() -> None:
    client = FakeClient(stream=FakeStream(sdk_events("research_complete.jsonl")))
    tool = make_tool(client, research_mode="balanced", research_idle_timeout_seconds=30)
    run(tool.research("  q  "))
    assert client.agent.calls == [{"query": "q", "mode": "balanced"}]
    assert client.created_with == [(KEY, 30)]


def test_research_stream_error() -> None:
    client = FakeClient(stream=FakeStream(sdk_events("research_error.jsonl")))
    recorder = Recorder()
    result = run(make_tool(client).research("q", __event_emitter__=recorder))
    assert result.startswith(
        "Error: The research failed (during searching: All search providers failed)."
    )
    assert recorder.statuses()[-1] == ("Research failed", True)
    assert recorder.citations() == []
    assert "search.js" not in everything_sent(recorder, result)


def test_research_stream_closes_without_terminal_event() -> None:
    client = FakeClient(stream=FakeStream(sdk_events("research_no_terminal.jsonl")))
    recorder = Recorder()
    result = run(make_tool(client).research("q", __event_emitter__=recorder))
    assert result.startswith(
        "Error: The connection to Tabstack closed before the research"
    )
    assert recorder.statuses()[-1] == ("Research failed", True)


def test_research_connection_dropped_mid_stream() -> None:
    drop = httpx.RemoteProtocolError("peer closed connection", request=REQUEST)
    events = sdk_events("research_no_terminal.jsonl")[:3]
    client = FakeClient(stream=FakeStream(events, fail_with=drop))
    result = run(make_tool(client).research("q"))
    assert result.startswith(
        "Error: The connection to Tabstack closed before the research"
    )


def test_research_rejected_key_is_not_echoed() -> None:
    error = tabstack.AuthenticationError(
        "Error code: 401",
        response=httpx.Response(401, request=REQUEST),
        body={"error": f"invalid key {KEY}"},
    )
    client = FakeClient(research_error=error)
    recorder = Recorder()
    result = run(make_tool(client).research("q", __event_emitter__=recorder))
    assert result.startswith("Error: Tabstack rejected the tool's API key.")
    assert KEY not in everything_sent(recorder, result)
    assert recorder.statuses() == [
        ("Starting research", False),
        ("Research failed", True),
    ]


def test_research_is_called_once_even_when_it_fails() -> None:
    error = tabstack.InternalServerError(
        "Error code: 500", response=httpx.Response(500, request=REQUEST), body=None
    )
    client = FakeClient(research_error=error)
    result = run(make_tool(client).research("q"))
    assert result.startswith("Error: Tabstack returned a server error.")
    assert len(client.agent.calls) == 1


def test_research_total_time_limit() -> None:
    stream = FakeStream(sdk_events("research_complete.jsonl"), delay=0.05)
    client = FakeClient(stream=stream)
    tool = make_tool(client)
    tool.valves = Tools.Valves.model_construct(
        tabstack_api_key=KEY,
        research_mode="fast",
        research_timeout_seconds=0.12,  # below the valve minimum; test only
        research_idle_timeout_seconds=120,
    )
    recorder = Recorder()
    result = run(tool.research("q", __event_emitter__=recorder))
    assert result.startswith("Error: The research took longer than 0.12 seconds")
    assert stream.yielded < len(stream.events)
    assert stream.closed
    assert recorder.statuses()[-1] == ("Research failed", True)


def test_research_idle_timeout_from_the_sdk() -> None:
    stall = httpx.ReadTimeout("no data", request=REQUEST)
    client = FakeClient(
        stream=FakeStream(sdk_events("research_complete.jsonl")[:2], fail_with=stall)
    )
    result = run(make_tool(client).research("q"))
    assert result.startswith(
        "Error: Tabstack sent nothing for 120 seconds, so the research was"
    )


def test_research_unknown_event_names_are_tolerated() -> None:
    events = sdk_events("research_complete.jsonl")
    events.insert(2, {"event": "something:new", "data": {"message": "added later"}})
    client = FakeClient(stream=FakeStream(events))
    result = run(make_tool(client).research("q"))
    assert not result.startswith("Error:")


def test_research_empty_report() -> None:
    complete: dict[str, Any] = {
        "event": "complete",
        "data": {
            "message": "done",
            "timestamp": 1.0,
            "report": "",
            "metadata": load_events("research_complete.jsonl")[-1]["data"]["metadata"],  # type: ignore[index]
        },
    }
    client = FakeClient(stream=FakeStream([EVENT_ADAPTER.validate_python(complete)]))
    result = run(make_tool(client).research("q"))
    assert result.startswith(
        "Error: The research failed (Tabstack returned an empty answer)."
    )


def test_research_missing_key_makes_no_call() -> None:
    client = FakeClient(stream=FakeStream([]))
    tool = make_tool(client, tabstack_api_key="   ")
    recorder = Recorder()
    result = run(tool.research("q", __event_emitter__=recorder))
    assert result.startswith(
        "Error: The Tabstack tool is not set up yet: it has no API key."
    )
    assert client.created_with == []
    assert recorder.statuses() == [("Research failed", True)]


# -- per-user keys ------------------------------------------------------------

USER_KEY = "fake-user-key-for-tests-5678"


def as_user(key: str) -> dict[str, object]:
    """Build __user__ the way Open WebUI does, with a UserValves instance."""
    return {"id": "u1", "valves": Tools.UserValves(tabstack_api_key=key)}


def test_user_key_is_used_instead_of_the_shared_key() -> None:
    client = FakeClient(stream=FakeStream(sdk_events("research_complete.jsonl")))
    run(make_tool(client).research("q", __user__=as_user(USER_KEY)))
    assert [key for key, _ in client.created_with] == [USER_KEY]


def test_blank_user_key_falls_back_to_the_shared_key() -> None:
    client = FakeClient(page=load_json("extract_page.json"))
    tool = make_tool(client)
    run(tool.read_url("https://a.example", __user__=as_user("  ")))
    run(tool.read_url("https://a.example", __user__={"id": "u1"}))
    assert [key for key, _ in client.created_with] == [KEY, KEY]


def test_user_valves_as_a_plain_dict_are_read_too() -> None:
    client = FakeClient(page=load_json("extract_page.json"))
    user = {"valves": {"tabstack_api_key": USER_KEY}}
    run(make_tool(client).read_url("https://a.example", __user__=user))
    assert [key for key, _ in client.created_with] == [USER_KEY]


def test_required_user_key_refuses_the_shared_key() -> None:
    client = FakeClient(stream=FakeStream([]))
    tool = make_tool(client, require_user_key=True)
    result = run(tool.research("q", __user__={"id": "u1"}))
    assert result.startswith("Error: The Tabstack tool needs your own API key")
    assert "Controls (the sliders icon" in result
    assert "> Valves > Tools > Tabstack." in result
    assert client.created_with == []


def test_required_user_key_uses_the_user_key() -> None:
    client = FakeClient(page=load_json("extract_page.json"))
    tool = make_tool(client, require_user_key=True)
    result = run(tool.read_url("https://a.example", __user__=as_user(USER_KEY)))
    assert result.startswith("Page:")
    assert [key for key, _ in client.created_with] == [USER_KEY]


def test_rejected_user_key_points_at_the_user_valves_and_is_not_echoed() -> None:
    error = tabstack.AuthenticationError(
        "Error code: 401",
        response=httpx.Response(401, request=REQUEST),
        body={"error": f"invalid key {USER_KEY}"},
    )
    client = FakeClient(research_error=error)
    recorder = Recorder()
    result = run(
        make_tool(client).research(
            "q", __event_emitter__=recorder, __user__=as_user(USER_KEY)
        )
    )
    assert result.startswith("Error: Tabstack rejected your API key.")
    assert USER_KEY not in everything_sent(recorder, result)


def test_user_valves_key_input_is_masked() -> None:
    assert Tools.UserValves().tabstack_api_key == ""
    extra = Tools.UserValves.model_fields["tabstack_api_key"].json_schema_extra
    assert extra == {"input": {"type": "password"}}
    assert Tools().valves.require_user_key is False


def test_research_empty_question_makes_no_call() -> None:
    client = FakeClient(stream=FakeStream([]))
    result = run(make_tool(client).research("   "))
    assert result.startswith(
        "Error: Tabstack rejected the request as invalid (the question was"
    )
    assert client.created_with == []


def test_research_works_without_an_emitter() -> None:
    client = FakeClient(stream=FakeStream(sdk_events("research_complete.jsonl")))
    result = run(make_tool(client).research("q", __event_emitter__=None))
    assert "Sources:" in result


def test_research_survives_a_broken_emitter() -> None:
    async def broken(_: dict[str, object]) -> None:
        raise RuntimeError("socket gone")

    client = FakeClient(stream=FakeStream(sdk_events("research_complete.jsonl")))
    result = run(make_tool(client).research("q", __event_emitter__=broken))
    assert "Sources:" in result


def test_research_cancellation_is_not_swallowed() -> None:
    stream = FakeStream(sdk_events("research_complete.jsonl"), delay=1.0)
    client = FakeClient(stream=stream)
    recorder = Recorder()

    async def scenario() -> None:
        task = asyncio.create_task(
            make_tool(client).research("q", __event_emitter__=recorder)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        run(scenario())
    assert recorder.statuses()[-1] == ("Research stopped", True)


# -- read_url -----------------------------------------------------------------


def test_read_url_success() -> None:
    client = FakeClient(page=load_json("extract_page.json"))
    recorder = Recorder()
    tool = make_tool(client, read_url_max_chars=1000, read_url_effort="max")
    result = run(
        tool.read_url(
            " <https://news.example.net/2026/10/release> ", __event_emitter__=recorder
        )
    )

    assert client.extract.calls == [
        {
            "url": "https://news.example.net/2026/10/release",
            "metadata": True,
            "effort": "max",
        }
    ]
    assert client.created_with == [(KEY, 60)]
    assert result.startswith(
        "Page: Release notes\nURL: https://news.example.net/2026/10/release"
    )
    assert "[Trimmed: kept the first " in result
    assert recorder.statuses() == [
        ("Reading news.example.net", False),
        ("Read news.example.net", True),
    ]
    citation = recorder.citations()[0]
    assert citation["source"] == {
        "name": "Release notes",
        "url": "https://news.example.net/2026/10/release",
    }
    assert len(citation["document"][0]) <= 1000 + len("\n```")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.com/file", "'ftp:' addresses are not supported."),
        ("file:///etc/passwd", "'file:' addresses are not supported."),
        ("example.com/page", "has no scheme"),
        ("", "No URL was given."),
    ],
)
def test_read_url_refuses_non_http_urls(url: str, reason: str) -> None:
    client = FakeClient(page=load_json("extract_page.json"))
    recorder = Recorder()
    result = run(make_tool(client).read_url(url, __event_emitter__=recorder))
    assert result.startswith(
        "Error: That address cannot be read: only http and https pages work."
    )
    assert reason in result
    assert client.created_with == []
    assert recorder.statuses() == [("Could not read the page", True)]


def test_read_url_empty_page() -> None:
    client = FakeClient(page=load_json("extract_empty.json"))
    result = run(make_tool(client).read_url("https://blank.example.com/"))
    assert result.startswith(
        "Error: Tabstack could not read that page (the page had no readable text)."
    )


def test_read_url_unreadable_page() -> None:
    error = tabstack.InternalServerError(
        "Error code: 500",
        response=httpx.Response(500, request=REQUEST),
        body={"error": "failed to fetch URL"},
    )
    client = FakeClient(extract_error=error)
    recorder = Recorder()
    result = run(
        make_tool(client).read_url("https://example.com/", __event_emitter__=recorder)
    )
    assert result.startswith(
        "Error: Tabstack could not read that page (failed to fetch URL)."
    )
    assert recorder.statuses()[-1] == ("Could not read the page", True)
    assert len(client.extract.calls) == 1


def test_read_url_internal_address_refused_by_tabstack() -> None:
    error = tabstack.UnprocessableEntityError(
        "Error code: 422",
        response=httpx.Response(422, request=REQUEST),
        body={"error": "access to internal resources is not allowed"},
    )
    client = FakeClient(extract_error=error)
    result = run(make_tool(client).read_url("http://192.168.1.1/"))
    assert "(access to internal resources is not allowed)" in result


def test_read_url_missing_key() -> None:
    client = FakeClient(page=load_json("extract_page.json"))
    result = run(make_tool(client, tabstack_api_key="").read_url("https://example.com"))
    assert result.startswith(
        "Error: The Tabstack tool is not set up yet: it has no API key."
    )
    assert client.created_with == []


def test_read_url_timeout() -> None:
    client = FakeClient(extract_error=tabstack.APITimeoutError(request=REQUEST))
    result = run(
        make_tool(client, read_url_timeout_seconds=15).read_url("https://example.com")
    )
    assert result.startswith(
        "Error: Tabstack sent nothing for 15 seconds, so reading the page was stopped."
    )


# -- the key ------------------------------------------------------------------


def test_key_never_reaches_the_chat_in_any_scenario() -> None:
    scenarios: list[tuple[FakeClient, str]] = [
        (
            FakeClient(stream=FakeStream(sdk_events("research_complete.jsonl"))),
            "research",
        ),
        (FakeClient(stream=FakeStream(sdk_events("research_error.jsonl"))), "research"),
        (
            FakeClient(stream=FakeStream(sdk_events("research_no_terminal.jsonl"))),
            "research",
        ),
        (FakeClient(research_error=RuntimeError(f"boom {KEY}")), "research"),
        (FakeClient(page=load_json("extract_page.json")), "read_url"),
        (FakeClient(extract_error=ValueError(KEY)), "read_url"),
    ]
    for client, operation in scenarios:
        recorder = Recorder()
        tool = make_tool(client)
        if operation == "research":
            result = run(tool.research("q", __event_emitter__=recorder))
        else:
            result = run(
                tool.read_url("https://example.com", __event_emitter__=recorder)
            )
        assert KEY not in everything_sent(recorder, result)
        assert client.created_with[0][0] == KEY


def test_recorded_fixtures_match_the_sdk_types() -> None:
    events = sdk_events("recorded_research_complete.jsonl")
    assert len(events) == 10
    ExtractMarkdownResponse.model_validate(load_json("recorded_extract_page.json"))


def test_read_url_with_recorded_page() -> None:
    client = FakeClient(page=load_json("recorded_extract_page.json"))
    result = run(
        make_tool(client).read_url("https://docs.python.org/3/whatsnew/3.14.html")
    )
    assert result.startswith("Page: What")
    assert "[Trimmed: kept the first " in result


# -- per-reply call limits ----------------------------------------------------


def test_second_research_call_in_one_reply_is_refused() -> None:
    client = FakeClient(stream=FakeStream(sdk_events("research_complete.jsonl")))
    tool = make_tool(client)
    first = run(tool.research("q", __message_id__="m1"))
    assert "Sources:" in first
    recorder = Recorder()
    second = run(tool.research("q", __event_emitter__=recorder, __message_id__="m1"))
    assert second.startswith(
        "Error: research already ran 1 time for this reply, so it was not run again "
        'for "q" and nothing was charged.'
    )
    assert len(client.agent.calls) == 1
    assert recorder.statuses() == [("Research not run again", True)]


def test_research_limit_is_per_reply_and_configurable() -> None:
    events = sdk_events("research_complete.jsonl")
    client = FakeClient(stream=FakeStream(events))
    tool = make_tool(client, max_research_calls_per_reply=2)
    for _ in range(2):
        client.agent.stream = FakeStream(events)
        assert not run(tool.research("q", __message_id__="m1")).startswith("Error:")
    assert run(tool.research("q", __message_id__="m1")).startswith("Error:")
    client.agent.stream = FakeStream(events)
    assert not run(tool.research("q", __message_id__="m2")).startswith("Error:")
    assert len(client.agent.calls) == 3


def test_no_message_id_or_request_means_no_limit() -> None:
    events = sdk_events("research_complete.jsonl")
    client = FakeClient(stream=FakeStream(events))
    tool = make_tool(client)
    for _ in range(3):
        client.agent.stream = FakeStream(events)
        assert not run(tool.research("q")).startswith("Error:")
    assert len(client.agent.calls) == 3


class FakeRequest:
    """Stands in for the Starlette request Open WebUI binds to every call."""


def test_request_object_limits_a_reply_without_message_id() -> None:
    # API clients that send no `id` get __message_id__=None; the request object
    # is still the same for every tool call in the reply.
    events = sdk_events("research_complete.jsonl")
    client = FakeClient(stream=FakeStream(events))
    tool = make_tool(client)
    request = FakeRequest()
    assert "Sources:" in run(tool.research("q", __request__=request))
    client.agent.stream = FakeStream(events)
    second = run(tool.research("q", __request__=request))
    assert second.startswith("Error: research already ran 1 time for this reply")
    client.agent.stream = FakeStream(events)
    assert "Sources:" in run(tool.research("q", __request__=FakeRequest()))
    assert len(client.agent.calls) == 2


def test_message_id_takes_precedence_over_request() -> None:
    client = FakeClient(page=load_json("extract_page.json"))
    tool = make_tool(client, max_read_url_calls_per_reply=1)
    request = FakeRequest()
    url = "https://a.example"
    assert run(tool.read_url(url, __message_id__="m1", __request__=request)).startswith(
        "Page:"
    )
    assert run(tool.read_url(url, __message_id__="m2", __request__=request)).startswith(
        "Page:"
    )
    assert run(tool.read_url(url, __request__=request)).startswith("Page:")
    assert run(tool.read_url(url, __request__=request)).startswith("Error:")
    assert len(client.extract.calls) == 3


def test_untrackable_request_means_no_limit() -> None:
    # A plain object() cannot be weakly referenced, so there is no key to count.
    client = FakeClient(page=load_json("extract_page.json"))
    tool = make_tool(client, max_read_url_calls_per_reply=1)
    request = object()
    for _ in range(2):
        assert run(tool.read_url("https://a.example", __request__=request)).startswith(
            "Page:"
        )


def test_read_url_limit_per_reply() -> None:
    client = FakeClient(page=load_json("extract_page.json"))
    tool = make_tool(client, max_read_url_calls_per_reply=2)
    for _ in range(2):
        assert run(tool.read_url("https://a.example", __message_id__="m1")).startswith(
            "Page:"
        )
    third = run(tool.read_url("https://a.example", __message_id__="m1"))
    assert third.startswith("Error: read_url already ran 2 times for this reply")
    assert len(client.extract.calls) == 2


def test_refused_url_does_not_use_up_the_limit() -> None:
    client = FakeClient(page=load_json("extract_page.json"))
    tool = make_tool(client, max_read_url_calls_per_reply=1)
    assert run(tool.read_url("ftp://a.example", __message_id__="m1")).startswith(
        "Error:"
    )
    assert run(tool.read_url("https://a.example", __message_id__="m1")).startswith(
        "Page:"
    )


def test_valve_defaults_for_call_limits() -> None:
    valves = Tools().valves
    assert valves.max_research_calls_per_reply == 1
    assert valves.max_read_url_calls_per_reply == 3


# -- citation numbers across calls in one reply -------------------------------


def test_research_after_read_url_numbers_after_the_page() -> None:
    # The page read here is the release notes, which Research also cites.
    client = FakeClient(
        page=load_json("extract_page.json"),
        stream=FakeStream(sdk_events("research_complete.jsonl")),
    )
    tool = make_tool(client)
    recorder = Recorder()
    run(
        tool.read_url(
            "https://a.example", __event_emitter__=recorder, __message_id__="m1"
        )
    )
    result = run(tool.research("q", __event_emitter__=recorder, __message_id__="m1"))
    assert result.startswith(
        "The example tool added a tools overview page in October 2026 [2]. "
        "The release notes describe a new settings screen [1]"
    )
    assert "[1] Release notes - https://news.example.net/2026/10/release" in result
    assert "[2] Tools overview - https://www.example.com/docs/tools" in result
    urls = [c["metadata"][0]["source"] for c in recorder.citations()]
    assert urls == [
        "https://news.example.net/2026/10/release",
        "https://www.example.com/docs/tools",
        "https://docs.example.org/web-search.md",
        "https://docs.example.org/web-search",
    ]


def test_second_research_call_reuses_numbers_and_skips_shown_citations() -> None:
    events = sdk_events("research_complete.jsonl")
    client = FakeClient(stream=FakeStream(events))
    tool = make_tool(client, max_research_calls_per_reply=2)
    recorder = Recorder()
    run(tool.research("q", __event_emitter__=recorder, __message_id__="m1"))
    client.agent.stream = FakeStream(events)
    second = run(tool.research("q", __event_emitter__=recorder, __message_id__="m1"))
    assert second.startswith(
        "The example tool added a tools overview page in October 2026 [1]."
    )
    assert len(recorder.citations()) == 4


def test_without_an_emitter_nothing_counts_as_shown() -> None:
    client = FakeClient(
        page=load_json("extract_page.json"),
        stream=FakeStream(sdk_events("research_complete.jsonl")),
    )
    tool = make_tool(client)
    run(tool.read_url("https://a.example", __message_id__="m1"))
    result = run(tool.research("q", __message_id__="m1"))
    assert "[1] Tools overview - https://www.example.com/docs/tools" in result


def test_read_url_focus_reaches_the_page_text_and_citation() -> None:
    client = FakeClient(page=load_json("recorded_extract_page.json"))
    recorder = Recorder()
    result = run(
        make_tool(client).read_url(
            "https://docs.python.org/3/whatsnew/3.14.html",
            focus="zstandard",
            __event_emitter__=recorder,
        )
    )
    assert "[Focused on: zstandard." in result
    assert "compression.zstd" in result
    (citation,) = recorder.citations()
    assert "[...]" in citation["document"][0]
