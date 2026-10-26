# Verification log

Every API and plugin behaviour the tool relies on, with where it was checked.
All checks were made on 2026-10-21.

Status key:

- CONFIRMED: read in official docs and, where noted, in source.
- SOURCE ONLY: read in source; the published docs are silent or thinner.
- OBSERVED: seen in a real recorded run, not stated in docs or source.
- UNCONFIRMED: could not be settled. Treated as unknown in the design.

## Versions checked

| Component | Version | How it was obtained |
| --- | --- | --- |
| Tabstack Python SDK (`tabstack` on PyPI) | 2.8.5 (latest, uploaded 2026-08-11) | `https://pypi.org/pypi/tabstack/json`, wheel `tabstack-2.8.5-py3-none-any.whl` unpacked and read |
| Tabstack docs | live site `https://docs.tabstack.ai`, plus local checkout of `Mozilla-Ocho/tabstack-api-docs` at `9ce7360` (2026-10-20) | live pages fetched with curl; local checkout read |
| Open WebUI | v0.12.3 (latest release, published 2026-10-16) | `git clone --depth 1 --branch v0.12.3 https://github.com/open-webui/open-webui` |
| Open WebUI docs | `open-webui/docs` at `cd7fbd8` (2026-10-21) | `git clone --depth 1 https://github.com/open-webui/docs` |

Paths below are relative to those checkouts: `sdk/` is the unpacked wheel
(`tabstack/...`), `owui/` is the Open WebUI source, `owui-docs/` is the docs repo.

---

## 1. Open WebUI native tools

### 1.1 Class and method shape - CONFIRMED (docs and source)

- A tool is one Python file with a top-level docstring (frontmatter) and a class
  named `Tools`. Optional nested `Valves` and `UserValves` classes are pydantic
  `BaseModel`s.
  - Docs: `owui-docs/docs/features/extensibility/plugin/tools/development.mdx`, "Tools Class".
  - Source: `owui/backend/open_webui/utils/plugin.py`, `load_tool_module_by_id`
    (`exec`s the file, then `module.Tools()`; raises "No Tools class found" otherwise).
- Every public callable on the instance is exposed to the model. Helpers must
  start with `_`. Docs, same section.
- `__init__` must set `self.valves = self.Valves()`. Stored valves are only
  applied if the instance already has a `valves` attribute.
  - Source: `owui/backend/open_webui/utils/tools.py`, `get_tools`:
    `if hasattr(module, 'valves') and hasattr(module, 'Valves'): module.valves = module.Valves(**valves)`.
- The method docstring text before the first `:param` becomes the tool
  description; `:param name:` lines become argument descriptions. Google-style
  `Args:` blocks are not parsed. Text after `:return:` is not sent to the model.
  Docs, "Method Docstrings"; source `get_tools` (`re.split(':(param|return)', ...)`).
- Type hints on arguments drive the JSON schema. Parameters starting with `__`
  are stripped from the schema. Docs, "Type Hints"; source `get_tools`.
- Trap found while testing, SOURCE ONLY: `convert_function_to_pydantic_model`
  (`utils/tools.py`) passes a parameter to pydantic's `create_model` as a
  `Field(...)` when the docstring has a `:param name:` line for it, and as a
  bare `(type, default)` otherwise. Pydantic 2.13.4 silently drops bare fields
  whose names start with an underscore, but raises `NameError: Fields must not
  use names with leading underscores` for a `Field`. So a docstring must never
  contain `:param __event_emitter__:` or any other injected argument. Checked
  by running `create_model` both ways under pydantic 2.13.4;
  `tests/test_tool.py` guards it.
- Return value: `str`, `dict` or `list`. A `dict` is JSON-encoded for the model.
  Docs, "Return Values".
- The `Tools` instance is cached and reused across requests and users; valves
  are re-assigned onto the shared instance on each request.
  - Source: `get_tools` (`tools_cache.get(tool_id)` then `module.valves = ...`).
  - Consequence for the design: no per-request state may be stored on `self`.

### 1.2 Async handling - CONFIRMED (docs and source)

- Docs recommend `async def` tool methods ("Use Async Functions for Future
  Compatibility", `tools/development.mdx`).
- Source: `utils/tools.py`, `get_async_tool_function_and_apply_extra_params`.
  Coroutine functions are awaited directly; sync functions are wrapped in an
  `async def` that calls them inline (on the event loop, not in a thread), so a
  blocking sync call would block the server. The tool will use the SDK's
  `AsyncTabstack` client.
- If a tool raises, the exception text goes to the model as `{'error': str(e)}`.
  - Source: `utils/middleware.py`, around `tool_result = await tool_function(**tool_function_params)`
    (`except Exception as e: tool_result = {'error': str(e)}`).
  - Consequence: the wrapper catches every exception itself and returns a plain
    message, so no exception text or trace reaches the chat.
- A string result beginning with `Error:` marks the tool call as failed in the
  UI. Source: `utils/middleware.py`, `_is_tool_result_error`.

### 1.3 Configuration: API key storage - CONFIRMED (docs and source)

- `Valves`: set from the admin panel (Workspace > Tools > the tool's gear icon).
  Tool valves can also be edited by the tool's owner and by anyone holding a
  write grant on the tool.
  - Docs: `owui-docs/docs/features/extensibility/plugin/development/valves.mdx`, opening section.
- `UserValves`: set by any user from a chat; reach the tool as
  `__user__["valves"]`, a `UserValves` instance built from the user's saved
  values. Docs, same page; source `utils/tools.py`, `get_tools`
  (`__user__['valves'] = module.UserValves(...)`). Saved in the user's
  settings under `tools.valves.<tool id>` and decrypted with the same
  `decrypt_valves` (`models/tools.py`, `get_user_valves_by_id_and_user_id`).
  The UI is Chat Controls > Valves, which renders with the same `Valves.svelte`
  component and password masking as admin valves
  (`src/lib/components/chat/Controls/Valves.svelte`). Any user with read
  access to the tool can update their own
  (`routers/tools.py`, `/id/{id}/valves/user/update`). Checked on v0.12.3,
  2026-10-26; seen live in EVIDENCE.md runs K1 to K3, where calls ran on a
  key set only in the user valve.
- A `str` field with `json_schema_extra={"input": {"type": "password"}}` is
  rendered masked. This only masks the UI.
- Valve values are stored as plaintext JSON in the database unless the admin
  sets `ENABLE_VALVE_ENCRYPTION=true` (Fernet, key derived from `WEBUI_SECRET_KEY`).
  Docs: `valves.mdx`, "Encrypting Valve Values at Rest".
- Saving new code whose valve types do not validate against stored values is
  refused. Docs: `valves.mdx`, "Changing Valves in Existing Code".

### 1.4 pip requirements - CONFIRMED (docs and source)

- Declared in frontmatter as `requirements: a==1.0, b==2.0` (comma-separated).
  On save, Open WebUI runs `pip install` on them.
  - Docs: `tools/development.mdx`, "External packages".
  - Source: `utils/plugin.py`, `install_frontmatter_requirements`
    (`requirements.split(',')`, `subprocess.check_call([sys.executable, '-m', 'pip', 'install', ...])`).
- Skipped when `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=False` or in
  offline mode. Docs recommend disabling it in multi-worker production and
  baking packages into the image instead.
- Open WebUI rewrites the import prefixes `from utils`, `from apps`, `from main`
  and `from config` in saved source. The tool must not use those prefixes.
  Docs, same section; source `utils/plugin.py`, `replace_imports`.
- CONFIRMED in a live install on 2026-10-21 (see `EVIDENCE.md`, section 1):
  saving the tool ran `pip install tabstack==2.8.5` and every other SDK
  dependency was already satisfied by the image.

### 1.4a Code is reformatted on save - CONFIRMED (live and source)

- The tool editor sends the code to `POST /api/v1/utils/code/format` before
  saving, which runs `black.format_str(code, mode=black.Mode())`
  (`owui/backend/open_webui/routers/utils.py`, `format_code`). The image has
  black 26.5.1 (`owui/backend/requirements.txt`).
- Seen live: the first paste was stored rewrapped at 88 columns. The file now
  keeps to black's style and `tests/test_secrets.py` checks it, so the stored
  copy is byte-identical to the file.
- Name, ID and description are filled from the frontmatter only when a tool
  is created; editing an existing tool does not refresh them (docs,
  `tools/development.mdx`, "Metadata auto-fill"; seen live).

### 1.5 Runtime Python - CONFIRMED (source)

- The official image is built `FROM python:3.11-slim-bookworm`
  (`owui/Dockerfile`, line 59). The pasted tool file therefore runs on Python
  3.11, not 3.12.
- Open WebUI pins `pydantic==2.13.4` and `httpx==0.28.1`
  (`owui/backend/requirements.txt`). Tabstack 2.8.5 requires `pydantic<3,>=1.9.0`
  and `httpx<1,>=0.23.0` (wheel `METADATA`). CONFIRMED compatible in a live
  install on 2026-10-21: the SDK installed without changing any package, and
  the tool ran inside the container on Python 3.11.17 (`EVIDENCE.md`,
  section 2).

### 1.6 Status updates - CONFIRMED (docs and source)

- Injected `__event_emitter__` argument; call
  `await __event_emitter__({"type": "status", "data": {"description": str, "done": bool, "hidden": bool}})`.
  - Docs: `tools/development.mdx`, "Status Events".
- Status events work in Native function-calling mode, which the docs describe
  as the only supported mode going forward. Same page, compatibility matrix.
- `__event_emitter__` can be `None` (source `socket/main.py`,
  `get_event_emitter` returns `None` without user, chat and message ids), so the
  wrapper must tolerate its absence.

### 1.7 Citations - CONFIRMED (docs and source)

- Event shape:
  `{"type": "citation", "data": {"document": [str], "metadata": [{"source": url, "name": title, ...}], "source": {"name": ..., "url": ...}}}`.
  `"source"` and `"citation"` event types are handled identically.
  - Docs: `tools/development.mdx`, "Citations".
  - Source: `owui/backend/open_webui/socket/main.py`, `elif event_type in ('source', 'citation')`
    appends `data` to the message's `sources` list.
- In Native mode a custom Python tool's result never becomes a source
  automatically. Only built-in tools in `CITATION_TOOL_NAMES`
  (`fetch_url`, `view_file`, `view_knowledge_file`, `query_knowledge_files`,
  `query_chat_files`) do.
  - Docs: `tools/development.mdx`, note "`self.citation` no longer does anything".
  - Source: `utils/middleware.py`, `CITATION_TOOL_NAMES` and its call sites.
  - Consequence: the tool must emit citation events itself, and must not name a
    method `fetch_url`.
- How inline `[n]` in the model's reply links to a source - SOURCE ONLY:
  - `owui/src/lib/utils/marked/citation-extension.ts` tokenises `[1]`, `[1][2]`,
    `[1,2]` in the rendered reply (not `[^1]` footnotes).
  - `owui/src/lib/components/chat/Messages/ContentRenderer.svelte`,
    `getSourceIds`: builds an ordered list of unique source ids across all
    citation events on the message, keyed by `metadata.source` (falling back to
    `source.id`). Label is `metadata.name`, else the URL.
  - `owui/src/lib/components/chat/Messages/Markdown/SourceToken.svelte`: `[n]`
    resolves to entry `n - 1` of that list.
  - `owui/src/lib/components/chat/Messages/Citations.svelte`: the source list
    under the message groups by the same id.
  - Consequence: emitting one citation event per de-duplicated source, in the
    same order as the numbering in the tool's text, makes the model's `[n]`
    chips point at the right source, provided the model copies the markers.
  - Numbering is per message, and a repeated id keeps its first number. The
    tool records, per reply, the ids it has sent and numbers a later call's
    sources after them, reusing the number of any page already sent (checked
    against `getSourceIds` on v0.12.3, 2026-10-26; seen live in EVIDENCE.md
    run K3, where a page read first was source 1 and Research numbered its
    sources from 2).
  - Limitation: sources added by other tools or by retrieval are not known to
    the tool. Built-in tools emit theirs after the tool-calling turn ends
    (`utils/middleware.py`, `tool_call_sources`), so they come after this
    tool's in the same turn. Reading them would mean importing
    `open_webui.models.chats` from inside the tool, which this tool avoids.

### 1.7a Per-reply identity and loop limit - CONFIRMED (source and docs)

- Tools can take `__message_id__` and `__chat_id__` (docs,
  `tools/development.mdx`, "Optional Arguments"). Source:
  `utils/middleware.py`, `extra_params` sets
  `'__message_id__': metadata.get('message_id')` once per request, so every
  tool call in the agentic loop of one reply sees the same id. Seen live: all
  repeated calls in run R4 belonged to one assistant message.
- A request to `/api/chat/completions` with no `id` (or `message_ids`) in the
  body gets `message_id` None, so tools see `__message_id__=None`
  (`main.py`, `chat_completion`: `form_data.pop('id', None)`, then the
  "Legacy/direct" branch sets `metadata['message_id']` from it). Checked
  2026-10-26 on v0.12.3.
- `__request__` is the same object for every tool call in one reply:
  `process_chat_payload` builds `extra_params` once with
  `'__request__': request` and `get_tools` binds them to each tool function
  with `functools.partial` (`utils/middleware.py`; `utils/tools.py`,
  `get_async_tool_function_and_apply_extra_params`, which also drops any
  extra param not in the function's signature). The tool uses it as the
  per-reply key when `__message_id__` is missing. Starlette's `Request` is
  hashable and can be weakly referenced (checked with `starlette` from PyPI,
  2026-10-26). Checked 2026-10-26 on v0.12.3; not yet seen live.
- `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS` caps tool-calling turns per reply,
  default 256, `-1` for unlimited, read once at start-up and not editable in
  the admin UI (`owui/backend/open_webui/env.py`; docs
  `reference/env-configuration.mdx`). Each turn counts once however many tool
  calls it contains.

- Seen live on 2026-10-21: recreating the container to add that variable
  signed the session out. `owui/backend/start.sh` writes a generated
  `WEBUI_SECRET_KEY` to `.webui_secret_key` in the working directory
  (`/app/backend`), not in the data volume, when the variable is unset.

### 1.8 Competing built-in web tools - CONFIRMED (source)

- Open WebUI adds its own `search_web` and `fetch_url` built-in tools in Native
  mode only when web search is enabled globally, the model has the
  `web_search` capability, the feature is on for the chat and the user has
  permission. Source: `utils/tools.py`, `get_builtin_tools`.
- With web search left off (the default config), the model will only see this
  tool's `research` and `read_url`.

---

## 2. Tabstack Research

### 2.1 SDK method - CONFIRMED (docs and source)

- `AsyncTabstack().agent.research(query=..., mode=..., nocache=..., fetch_timeout=..., timeout=...)`
  returns `AsyncStream[ResearchEvent]`. Sync equivalent on `Tabstack`.
  - Source: `sdk/tabstack/resources/agent.py`, `AgentResource.research` and
    `AsyncAgentResource.research`.
  - Docs: `https://docs.tabstack.ai/guides/research` (Quickstart, Parameters).
- `mode`: `"fast"` (default) or `"balanced"`. `balanced` requires a paid plan
  (docs). `query` max 10,000 characters (SDK docstring).
- `fetch_timeout` caps a single page fetch, not the whole call (docs,
  "What fetch_timeout bounds").
- There is no non-streaming mode (docs).

### 2.2 Event names and terminal states - CONFIRMED (docs and source)

- Source: `sdk/tabstack/types/research_event.py`. Discriminated union on
  `event`. Names:
  `start`, `planning:start`, `planning:end`, `iteration:start`, `iteration:end`,
  `searching:start`, `searching:end`, `writing:start`, `writing:end`,
  `complete`, `error`, and in balanced mode also `prefetching:start|end`,
  `analyzing:start|end`, `following:start|end`, `evaluating:start|end`,
  `outlining:start|end`, `judging:start|end`.
- Every event's `data` has `message: str` and `timestamp: float`.
- Terminal events: `complete` (success) and `error` (task failure). Docs: "At
  any point, `error` can arrive instead ... delivered inside the stream, not as
  an HTTP error."
- Docs say the `error` event's nested `error` object can arrive unpopulated
  even though it is typed as required; read it defensively
  (`https://docs.tabstack.ai/guides/research`, "Error handling").
- A stream that ends with neither `complete` nor `error` is possible from the
  client's point of view (connection dropped). The docs' own example raises
  "Stream ended without a complete event" in that case.
- Unknown event names: the SDK builds events with `construct_type` (loose, no
  validation) unless strict validation is enabled, so an event name added
  server-side later arrives as an untyped object rather than raising
  (`sdk/tabstack/_base_client.py`, `_process_response_data`;
  `_client.py`, `_strict_response_validation=False` default). The status
  mapper must have a fallback for unknown names.

### 2.3 Shape of the final answer and citations - CONFIRMED (docs and source)

- `complete.data.report`: Markdown string. `complete.data.metadata.cited_pages`:
  optional list; treat missing as empty.
- Each cited page: `id`, `url`, `claims` (empty in fast mode),
  `source_queries` always present; `title`, `summary`, `relevance`,
  `reliability`, `parent_url`, `depth`, `url_source`, `full_text` optional.
- SDK docstring: cited pages are "ordered by first citation appearance".
- Inline marker format in `report` - OBSERVED, not documented: a real fast-mode
  run recorded on 2026-09-15 in the sibling project
  `tabstack-cited-research-python/artifacts/sample-run/` uses `[1]`..`[7]`,
  with `[n]` matching `cited_pages[n-1]`. Neither the docs nor the SDK state
  the marker format. The formatter will treat `[n]` as the expected form and
  degrade safely (leave text untouched) if markers are missing or out of range.
- Duplicate URL variants - OBSERVED: the same run lists
  `https://docs.ollama.com/capabilities/web-search.md` as `[1]` and
  `http://docs.ollama.com/capabilities/web-search` as `[2]`, both titled
  "Web search". This is the case the de-duplication must handle.
- Usage: `metadata.metrics` (optional) carries `fetches`, `searches`,
  `tokens`, `total_duration` (ms). No credit or cost field exists in the SDK
  types. The tool will not report cost.

### 2.4 SDK default timeout and retries - CONFIRMED (docs and source)

- Client default timeout: `httpx.Timeout(timeout=60, connect=5.0)`; default
  `max_retries = 2`; backoff 0.5 s initial, 8 s max
  (`sdk/tabstack/_constants.py`).
- `research()` raises its per-request timeout to 600 s when the client is left
  on the default timeout (`resources/agent.py`:
  `if not is_given(timeout) and self._client.timeout == DEFAULT_TIMEOUT: timeout = 600`).
- What the timeout means for a stream: an httpx timeout is per operation, so
  for a streamed response it bounds the gap between chunks, not total run
  time. There is no overall server-side timeout on `/research` (live docs,
  "Latency and timeouts"). Total run time must be capped by the caller.
- Retried by the SDK: 408, 409, 429, 5xx, connection errors and timeouts,
  twice, honouring `retry-after` up to 60 s and the `x-should-retry` header
  (`_base_client.py`, `_should_retry` and the request loop; live docs
  `https://docs.tabstack.ai/production/error-reference`, "Retry behaviour").
  Not retried: 400, 401, 402, 403, 404, 422.
- Retries apply only to opening the request. Once the stream is open, the SDK
  iterates it without retry (`_streaming.py`, `AsyncStream.__stream__`), and a
  failure mid-stream surfaces as a raw `httpx` exception, not a Tabstack
  exception type.
- UNCONFIRMED: the claim that `/research` sends an SSE keepalive comment every
  30 seconds, and the wording "In Python it is the longest gap allowed between
  chunks". Both appear only in local docs commit `9ce7360`
  (`guides/production-reliability.mdx`), which is not on the live site as of
  2026-10-21. The design does not depend on keepalives; if they do not exist,
  a long silent phase could hit the per-chunk timeout, so the default
  per-chunk timeout will be generous.

### 2.5 HTTP-level errors - CONFIRMED (docs and source)

- Raised before the stream opens: `AuthenticationError` (401),
  `PermissionDeniedError` (403), `RateLimitError` (429),
  `InternalServerError` (5xx), `BadRequestError` (400),
  `UnprocessableEntityError` (422), `APITimeoutError`, `APIConnectionError`,
  and a plain `APIStatusError` for others including 402 (out of credits).
  Root class `TabstackError`. (`sdk/tabstack/_exceptions.py`,
  `_client.py` `_make_status_error`; live docs `production/error-reference`.)
- Missing key: constructing the client with `api_key=None` and no
  `TABSTACK_API_KEY` env var raises `TabstackError` at construction
  (`_client.py`). The tool will check the valve itself first and never fall
  back to the environment variable silently. Note: an empty string `""` is not
  `None`, so the SDK would accept it and fail later with 401; the tool checks
  for empty before constructing the client.

---

## 3. Tabstack Extract (Markdown)

### 3.1 SDK method and options - CONFIRMED (docs and source)

- `AsyncTabstack().extract.markdown(url=..., content=..., effort=..., metadata=..., nocache=..., geo_target=..., timeout=...)`
  returns `ExtractMarkdownResponse` with `content: str`, `url: str`,
  `metadata: Optional[Metadata]`.
  - Source: `sdk/tabstack/resources/extract.py`; `types/extract_markdown_response.py`.
  - Docs: `https://docs.tabstack.ai/guides/how-to-use-markdown-endpoint`.
- `content`: `"main"` (default, main article) or `"full"`.
- `effort`: `"min"` (1-5 s, no fallback), `"standard"` (default, 3-15 s),
  `"max"` (full browser rendering, 15-60 s).
- `metadata=True` returns metadata as a separate object and `content` as pure
  Markdown; otherwise metadata is embedded as YAML frontmatter in `content`.
  The tool will set `metadata=True` so the title is available and the
  Markdown handed to the model has no frontmatter.
- Default per-request timeout is raised to 180 s when the client is on its
  default timeout (`extract.py`: `timeout = 180`).

### 3.2 How an unreadable page is reported - CONFIRMED (docs and source)

- As HTTP errors, raised as SDK exceptions (live docs
  `production/error-reference`):
  - 422 `UnprocessableEntityError`: `url is invalid`,
    `access to internal resources is not allowed`.
  - 500 `InternalServerError`: `failed to fetch URL`,
    `failed to convert HTML to Markdown` (the SDK retries 5xx twice first).
  - 400 `BadRequestError`: missing or malformed `url`.
- UNCONFIRMED: whether a page that loads but has no extractable text returns
  200 with empty or near-empty `content`. Not stated in docs. The tool will
  treat empty or whitespace-only `content` as unreadable.

---

## 4. Tabstack wording and trust page

- Trust page: `https://tabstack.ai/trust` (HTTP 200, title "How Tabstack
  handles your data", fetched 2026-10-21). Docs will link here for data
  handling questions.
