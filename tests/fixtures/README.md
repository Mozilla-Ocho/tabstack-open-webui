# Fixtures

All fixtures here are hand-built, not recorded. They were written on
2026-10-21 without calling the API (the account had no credits at the time).

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

Replace these with recorded fixtures once real runs are possible, and say so here.
