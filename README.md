# Tabstack tool for Open WebUI

A native Open WebUI tool that gives a self-hosted model two things it cannot do
on its own:

- **`research(question)`** calls Tabstack Research and returns a cited answer
  from live sources, with numbered `[n]` markers and a source list. Progress
  shows in the chat as status updates while it runs, and each source is sent
  to Open WebUI as a citation.
- **`read_url(url)`** calls Tabstack Extract and returns clean Markdown for one
  page, trimmed to a length you set, with a note saying how much was kept.

It is one Python file, `tabstack_tool.py`, pasted into the Open WebUI admin
panel. There is no extra service to run.

Install steps are in [docs/INSTALL.md](docs/INSTALL.md).

## Requirements

- Open WebUI. Tested on v0.12.3 (see [docs/EVIDENCE.md](docs/EVIDENCE.md)).
- A model that supports tool calling, with Open WebUI's function calling left
  on Native (the default).
- A Tabstack API key.

Open WebUI installs the SDK (`tabstack==2.8.5`) from the file's frontmatter
when the tool is saved. If your deployment sets
`ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=False`, install that package into
the Open WebUI image yourself.

## Settings (Valves)

| Valve | Default | What it does |
| --- | --- | --- |
| `tabstack_api_key` | empty | Your Tabstack API key. Masked in the UI. |
| `research_mode` | `fast` | `balanced` reads more sources and needs a paid plan. |
| `research_timeout_seconds` | 300 | Stops a Research call that runs longer than this. |
| `research_idle_timeout_seconds` | 120 | Stops a Research call if Tabstack sends nothing for this long. |
| `read_url_timeout_seconds` | 60 | Stops reading a page after this long. |
| `read_url_effort` | `standard` | `max` renders JavaScript-heavy pages but is slower. |
| `read_url_max_chars` | 20000 | Longest page text passed to the model. |

Open WebUI stores valve values in its database as plain text unless the admin
sets `ENABLE_VALVE_ENCRYPTION=true`. Anyone who can edit the tool can read its
valves.

## How it behaves

- **No retries are added.** The SDK's own default stays in place: two retries
  on 408, 409, 429, 5xx and connection failures, before a Research stream
  opens. The tool never re-runs a call itself, because repeating a Research
  call repeats the work and the billing. A model may still decide to call the
  tool again after an error; see the evidence.
- **Failures come back as plain messages** starting `Error:`, such as a
  missing or rejected key, a timeout, a stream that ended with an error, a
  stream that closed early, or a page Tabstack could not read. No stack traces
  reach the chat, and the key is never echoed.
- **`read_url` only accepts `http` and `https` URLs** and says why when it
  refuses one.
- **Duplicate sources are merged and the merge is reported.** If two cited
  URLs are the same page (http and https, `www.`, a trailing slash, a
  `#fragment` or `utm_*` parameters), they become one source, and the answer
  says which address was folded into which number. `/page` and `/page.md` are
  treated as different pages.

## Known limits

- **Whether the model calls the tool is up to the model.** In the runs in
  [docs/EVIDENCE.md](docs/EVIDENCE.md), `llama3.1:8b` and `qwen3:8b` never
  called it while Open WebUI's built-in tools were on (36 extra tools offered
  alongside this one), and called it in every run once the model's Builtin
  Tools capability was switched off. That is ten runs on two models, not a
  rate.
- **Citation numbers are per message.** Open WebUI numbers sources in the
  order they arrive on a message. If the model calls `research` twice in one
  reply, or another tool adds sources first, the `[n]` chips for the second
  call point at the wrong sources.
- The model has to copy the `[n]` markers into its reply for the citation
  chips to appear.

## Data handling

For how Tabstack handles the questions and URLs it receives, see
[Tabstack's Trust page](https://tabstack.ai/trust).

## Development

Python 3.12 with [uv](https://docs.astral.sh/uv/). Open WebUI runs tools on
Python 3.11, so ruff and pyright target 3.11 and the tests run on both.

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest
uv run --python 3.11 --isolated --with pytest==9.1.1 --with tabstack==2.8.5 \
  --with pydantic==2.13.4 pytest -p no:cacheprovider
```

The tests make no network calls; a fixture in `tests/conftest.py` blocks them.
Open WebUI runs black on tool code when it is saved, so the file is kept in
black's style and a test checks that black leaves it unchanged.

- `tabstack_tool.py` has two sections: pure functions with no Open WebUI
  dependency (citations, status lines, trimming, error messages), and the
  `Tools` class that Open WebUI loads.
- `tests/fixtures/` holds hand-built Research and Extract fixtures. See the
  README there.
- `docs/VERIFIED.md` records every API and plugin behaviour the code relies
  on, and where it was checked.

### Running Open WebUI locally

`docker-compose.yml` has two profiles. Open WebUI is served on
http://localhost:3000.

```sh
# Open WebUI in a container, using Ollama already installed on this machine.
docker compose --profile host up -d

# Ollama and Open WebUI both in containers. On macOS, Ollama runs on the
# CPU only here and is much slower.
docker compose --profile bundled up -d
```

The tool never reads environment variables; its key comes only from the
valve. If you keep a `TABSTACK_API_KEY` in a local `.env` for scripts you run
by hand, git ignores it.
