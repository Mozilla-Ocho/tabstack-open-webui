# Fixtures

Two kinds of fixture live here: hand-built ones, written on 2026-10-21 before
the account had credits, and two recorded from the live API later the same
day.

## Recorded

Recorded on 2026-10-21 with `tabstack==2.8.5` (`AsyncTabstack`, default
timeout and retries). Each streamed event was saved exactly as the SDK's
`to_dict()` returned it. Nothing was edited. The files contain no API key; the
tests in `tests/test_secrets.py` check this.

| File | Call |
| --- | --- |
| `recorded_research_complete.jsonl` | `agent.research(query="What is the latest stable release of Node.js, and when did it come out?", mode="fast")`: 10 events, `start` to `complete`, 15.4 s. The `complete` event has no `metrics` field, so no usage was reported. |
| `recorded_extract_page.json` | `extract.markdown(url="https://docs.python.org/3/whatsnew/3.14.html", metadata=True)`: 148,117 characters, 1.6 s. The page text is from the Python documentation, (c) Python Software Foundation, under the PSF documentation licence. |

## Hand-built

- Shapes follow the Tabstack SDK 2.8.5 types (`tabstack/types/research_event.py`,
  `tabstack/types/extract_markdown_response.py`) in the API's wire format:
  camelCase keys, `{"event": ..., "data": {...}}` per Research event. This is
  what the SDK's `to_dict()` returns for each streamed event.
- The event order and the `[n]` marker style follow a real fast-mode run
  recorded on 2026-09-15 (see `docs/VERIFIED.md`, section 2.3).
- URLs use the reserved `example.com`, `example.net` and `example.org`
  domains. Titles, reports and page text are invented.

| File | What it represents |
| --- | --- |
| `research_complete.jsonl` | A fast-mode run ending in `complete`. Cited pages include an http/https/www/trailing-slash variant pair, and a `.md` pair that is deliberately not merged. The report has a code block containing `[9]`. |
| `research_error.jsonl` | The same run ending in an `error` event during `searching`, with a stack trace in the payload that must never reach the chat. |
| `research_no_terminal.jsonl` | The same run cut off after `writing:start`, with no `complete` or `error` event. |
| `extract_page.json` | An `extract.markdown(metadata=True)` response for a page long enough to trim, including a fenced code block. |
| `extract_empty.json` | A response whose `content` is only whitespace. |

The hand-built files cover cases a single live call cannot produce on demand:
URL variants to merge, a streamed error, and a stream that closes early.
