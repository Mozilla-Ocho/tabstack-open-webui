# Tabstack tool for Open WebUI

A native Open WebUI tool that gives a self-hosted model two things it cannot do
on its own:

- **`research(question)`** calls Tabstack Research and returns a cited answer
  from live sources, with numbered `[n]` markers and a source list. Progress
  shows in the chat as status updates while it runs, and each source is sent
  to Open WebUI as a citation.
- **`read_url(url, focus)`** calls Tabstack Extract and returns clean Markdown
  for one page, trimmed to a length you set, with a note saying how much was
  kept. If the model passes `focus` (a few words such as "pricing"), a long
  page keeps the parts that mention them instead of only its start.

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
| `tabstack_api_key` | empty | The shared Tabstack API key, used for anyone who has not set their own. Masked in the UI. |
| `research_mode` | `fast` | `balanced` reads more sources and needs a paid plan. |
| `research_timeout_seconds` | 300 | Stops a Research call that runs longer than this. |
| `research_idle_timeout_seconds` | 120 | Stops a Research call if Tabstack sends nothing for this long. |
| `read_url_timeout_seconds` | 60 | Stops reading a page after this long. |
| `read_url_effort` | `standard` | `max` renders JavaScript-heavy pages but is slower. |
| `max_research_calls_per_reply` | 1 | Research calls allowed in one reply. Later calls are refused without contacting Tabstack. |
| `max_read_url_calls_per_reply` | 3 | The same for `read_url`. |
| `read_url_max_chars` | 20000 | Longest page text passed to the model. |
| `require_user_key` | off | Only use each user's own key (below); never the shared key. |

Each user can also set their own key in a chat under **Controls** (the
sliders icon at the top right) **> Valves > Tools > Tabstack** (a user valve,
masked in the UI). This needs the Chat Controls and Valves user permissions,
which are on by default. When set, that user's calls use and are billed to
their key instead of the shared one. Error messages about a rejected key or an
empty account say whose key it was.

Open WebUI stores valve values, including user valves, in its database as
plain text unless the admin sets `ENABLE_VALVE_ENCRYPTION=true`. Anyone who
can edit the tool can read its valves.

## How it behaves

- **No retries are added.** The SDK's own default stays in place: two retries
  on 408, 409, 429, 5xx and connection failures, before a Research stream
  opens. The tool never re-runs a call itself, because repeating a Research
  call repeats the work and the billing.
- **Repeated calls from the model are capped.** In testing, a model called
  `research` 24 times in one reply after getting good answers. The tool now
  counts calls per reply (using the message id Open WebUI passes it, or the
  request itself for API calls that send no message id) and refuses any
  beyond the valve limit, without contacting Tabstack. Successful
  results also end with an instruction to answer from them and not call
  again. The count is kept in memory per Open WebUI worker. Also set
  `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS` (see the install guide).
- **Failures come back as plain messages** starting `Error:`, such as a
  missing or rejected key, a timeout, a stream that ended with an error, a
  stream that closed early, or a page Tabstack could not read. Each is a short
  message written for the user, followed by an instruction to repeat it as it
  is, add nothing about how to fix it, and not call the tool again for the
  same question. A call refused by the per-reply limit names the question or
  page that was refused and asks the model to tell the user it was not looked
  up. No stack traces reach the chat, and the key is never echoed.
- **`read_url` only accepts `http` and `https` URLs** and says why when it
  refuses one.
- **Page titles are cleaned.** Decorative punctuation in source titles (em
  dashes, curly quotes, arrows, trade mark signs) is replaced with plain ASCII.
  Tabstack's answer text itself is passed on unchanged.
- **Duplicate sources are merged and the merge is reported.** If two cited
  URLs are the same page (http and https, `www.`, a trailing slash, a
  `#fragment` or `utm_*` parameters), they become one source, and the answer
  says which address was folded into which number. `/page` and `/page.md` are
  treated as different pages.
- **Citation numbers continue across calls in one reply.** Open WebUI numbers
  a message's sources in the order they arrive. The tool remembers which
  sources it has already sent for the reply, so a Research call after
  `read_url` (or a second Research call) numbers its sources after them, and
  a page already shown keeps its number and is not sent again.

## Known limits

- **Whether the model calls the tool is up to the model.** In the runs in
  [docs/EVIDENCE.md](docs/EVIDENCE.md), `llama3.1:8b` and `qwen3:8b` did not
  call it in any of eight runs while all of Open WebUI's built-in tools were
  on (36 extra tools offered alongside this one), even with a more insistent
  tool description. They called it in all 4 runs with only the Memory,
  Knowledge Base and Files categories left on, and in all 24 runs with
  built-in tools off. That is a small number of runs on two models, not a
  rate. The install guide explains both settings.
- **The model has to copy the `[n]` markers** into its reply for the citation
  chips to appear next to each point. Small models often drop some or all of
  them: `llama3.1:8b` left them all out of three of its four cited replies on
  2026-10-26, so its sources only showed in the panel under the reply.
- **Other tools' sources shift the numbers.** The tool only knows about the
  sources it sent itself. If another tool, or a knowledge base, adds sources
  to the message first, the `[n]` chips point at the wrong sources.
- `read_url` passes at most 20,000 characters of a page by default: the
  start, or the parts matching `focus` if the model gives one. The model only
  sees that text. The tool passes on what Tabstack returns; in testing, a
  table that came through without its date column led the model to invent
  dates, with `read_url_effort` on `standard` and on `max`.

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
- `tests/fixtures/` holds Research and Extract fixtures, both recorded from
  the live API and hand-built for cases a live call cannot produce on demand.
  See the README there.
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
admin valve or the user's own valve. If you keep a `TABSTACK_API_KEY` in a
local `.env` for scripts you run by hand, git ignores it.

## License

MIT. See [LICENSE](LICENSE).
