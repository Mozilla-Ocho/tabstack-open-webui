# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A native Open WebUI tool: one file, `tabstack_tool.py`, that an admin pastes into the Open WebUI admin panel. It exposes `research(question)` (Tabstack Research, streamed, cited answer) and `read_url(url)` (Tabstack Extract, trimmed Markdown). There is no service or package to deploy; the file itself is the product. The frontmatter docstring at the top (`requirements: tabstack==2.8.5`) is how Open WebUI installs the SDK, so keep it in sync with `pyproject.toml`.

## Commands

Python 3.12 with uv; Open WebUI runs tools on 3.11, so ruff/pyright target 3.11 and tests must pass on both.

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright                      # strict mode, covers tests too
uv run pytest
uv run pytest tests/test_tool.py::test_name   # single test
# Python 3.11 run (matches Open WebUI's runtime):
uv run --python 3.11 --isolated --with pytest==9.1.1 --with tabstack==2.8.5 \
  --with pydantic==2.13.4 pytest -p no:cacheprovider
```

Local Open WebUI (http://localhost:3000): `docker compose --profile host up -d` (uses host Ollama) or `--profile bundled` (Ollama in a container, CPU-only on macOS).

## Constraints that aren't obvious

- **Black style is enforced by a test.** Open WebUI runs black on tool code when saved; `tests/test_secrets.py` asserts `black.format_str(source)` leaves `tabstack_tool.py` unchanged. Line length 88.
- **Repo text must be plain ASCII** (enforced by `test_repo_text_is_plain_ascii`): no em dashes, curly quotes or arrows in code, docs or this file. Format tests with `ruff format`, not black.
- **Every public method on `Tools` is exposed to the model as a tool.** A test asserts the public callables are exactly `read_url` and `research`. Helpers must be underscore-prefixed or live outside the class. Method docstrings and `:param:` lines are the tool description the model sees.
- **Tests make no network calls.** An autouse fixture in `tests/conftest.py` patches socket connect. Tests swap in a fake client via `tool._client_factory` (the test seam set in `Tools.__init__`).
- **No retries beyond the SDK default, ever.** Re-running Research repeats billing. Failures are returned to the model as a one-sentence `Error:` string plus a relay instruction (`_RELAY`), never raised, never containing stack traces or the API key (`redact`).
- **Per-reply call cap.** `CallCounter` on the `Tools` instance, keyed by `__message_id__` (falling back to the `__request__` object, which is the same for every call in one reply), refuses calls beyond `max_*_calls_per_reply` valves without contacting Tabstack. It is in-memory per worker by design.
- The tool never reads environment variables. The key comes from the user's `UserValves.tabstack_api_key` if set, else the admin `tabstack_api_key` valve unless `require_user_key` is on (`choose_key`). Failure messages take `key_owner` so they say whose key failed.
- **Citation numbers are per message in Open WebUI**, so `ShownSources` remembers, per reply key, the URLs already sent; `format_research_answer(..., shown)` numbers new sources after them and skips re-sending ones already shown.

## Architecture

`tabstack_tool.py` has two sections separated by banner comments:

1. **Pure section**: no Open WebUI dependency. Research stream events are handled as plain mappings in wire format (camelCase, `{"event": ..., "data": {...}}`, what the SDK's `to_dict()` returns). Key pieces: `ResearchProgress` (feeds events -> status lines -> `ResearchCompleted | ResearchFailed | ResearchIncomplete`), citation handling (`parse_cited_pages` -> `dedupe_sources` via `url_identity` -> `renumber_markers` -> `format_research_answer`), `page_body` for Extract (`focus_markdown` keeps the parts matching the model's optional `focus`, else `trim_markdown` keeps the start), and `classify_exception`/`failure_message` mapping SDK/httpx errors to `FailureKind` user messages.
2. **Open WebUI wrapper**: `_Status` (wraps `__event_emitter__`, swallows emitter failures), `Valves`, and the `Tools` class with `research`/`read_url`, which also emit status and citation events.

Tests are grouped by concern: `test_formatting` (citations, trimming), `test_events` (stream -> status), `test_errors` (failure messages), `test_tool` (end-to-end through `Tools` with fake client), `test_secrets` (no keys in fixtures, black check).

## Docs and fixtures

- `docs/VERIFIED.md` records every Open WebUI / Tabstack API behaviour the code depends on, with status (CONFIRMED / SOURCE ONLY / OBSERVED / UNCONFIRMED) and where it was checked. Update it when relying on a new behaviour.
- `docs/EVIDENCE.md` logs real runs against Open WebUI (screenshots in `docs/screenshots/`). Only record what was actually run.
- `tests/fixtures/README.md` explains recorded (`recorded_*`, unedited live output) vs hand-built fixtures (example.com domains, covering merge/error/early-close cases).
- Prose in README and docs is plain and factual: no estimates, no claims beyond what was observed.
