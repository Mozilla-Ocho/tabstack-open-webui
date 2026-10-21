# Evidence

What was actually run, when, and what happened. Nothing here is estimated.

## Status

Phase 4 is partly done. The Tabstack account used for testing had no credits
on 2026-10-21, so no successful Research or Extract call has been made yet.
Everything below was done without spending credits. The five research
questions and three `read_url` calls the brief asks for are still to do.

## Environment

| Item | Value |
| --- | --- |
| Date | 2026-10-21 |
| Machine | Apple M3, macOS 26.5.2 (build 25F84) |
| Container runtime | OrbStack 2.2.3, Docker 29.4.0, Docker Compose v5.1.2; 8 CPUs and 11.74 GiB visible to containers |
| Open WebUI | `ghcr.io/open-webui/open-webui:v0.12.3` (`sha256:a537bbe1e6b4...`), reports version 0.12.3 |
| Ollama (container) | `ollama/ollama:0.40.7` (`sha256:2e6cc92ffa43...`), CPU only |
| Models pulled | `llama3.2:3b` (ID `a80c4f17acd5`, 2.0 GB), `qwen3:4b` (ID `359d7dd4bcda`, 2.5 GB, not used yet) |
| Tabstack SDK | `tabstack==2.8.5`, installed by Open WebUI from the tool's frontmatter |
| Python inside Open WebUI | 3.11.17 |
| Tool version | `tabstack_tool.py` 0.1.0 at the commit that adds this file |
| Browser automation | Playwright 1.58.0 with headless Chromium, driving the real UI |

The compose file is `docker-compose.yml` in this repo. The admin account was
created through the normal first-run sign-up page.

## 1. Installing the tool through the admin UI

Done once on a fresh volume, then the file was pasted again over the saved
copy after a change. Steps as performed:

1. Workspace > Tools > Create.
   (`screenshots/install-01-tools-empty.png`)
2. The editor opens with a sample tool. Selected all and pasted
   `tabstack_tool.py`. The Name, ID and Description fields filled themselves
   from the frontmatter: `Tabstack`, `tabstack`, and the description line.
   (`screenshots/install-02-pasted.png`)
3. Clicked Save & Create. A "Confirm your action" dialog warned that tools
   run arbitrary code. Clicked Confirm. (`screenshots/install-03-confirm.png`)
4. The server log shows pip installing the requirement and the module loading:
   ```
   Downloading tabstack-2.8.5-py3-none-any.whl (107 kB)
   Installing collected packages: tabstack
   Successfully installed tabstack-2.8.5
   ... load_tool_module_by_id:245 - Loaded module: tool_tabstack_...
   ```
   Every other dependency of the SDK was already present in the image
   ("Requirement already satisfied"), so nothing in Open WebUI was upgraded.
5. The tool appears in the list with a gear (Valves) and a menu.
   (`screenshots/install-04-tools-list.png`)
6. The gear opens the Valves dialog with all seven settings. Each starts on
   "Default"; a value can only be typed after switching a field from Default.
   (`screenshots/install-05-valves.png`)

Checked through the API (`GET /api/v1/tools/id/tabstack`):

- Two specs, `research(question: string)` and `read_url(url: string)`, with
  the descriptions taken from the docstrings. No `__event_emitter__` in either.
- The valve schema has the seven fields; the key field carries
  `"input": {"type": "password"}`.

Findings from the install:

- **Open WebUI reformats tool code with black when it is saved.** The first
  paste was stored 875 characters longer than the file, with lines rewrapped
  at 88 columns. The editor calls `/api/v1/utils/code/format`, which runs
  `black.format_str(code, mode=black.Mode())` (black 26.5.1 in the image).
  The file was changed to keep to black's style (ruff at 88 columns, one
  `pyright: ignore` replaced by a `cast`), and a test now checks that black
  leaves it unchanged. After pasting again, the stored source was
  byte-identical to the file (35,959 characters both).
- **Metadata is not re-read when an existing tool is edited.** After the
  second paste, the list still showed the first description. This matches the
  docs ("it does not re-derive fields when editing an existing item"). The
  screenshots above show that older description.

## 2. The tool running inside Open WebUI's runtime

`tabstack_tool.py` was copied into the running Open WebUI container and called
directly with its Python 3.11.17 and the SDK it had installed. The copy was
deleted afterwards. A deliberately fake key was used for the two calls that
reach the API; the real key was not used.

| Case | Time | Result returned to the model | Status lines |
| --- | --- | --- | --- |
| `research`, no key | 0.0 s | `Error: the Tabstack tool has no API key. An Open WebUI admin needs to add one in the tool's Valves. ...` | Research failed |
| `read_url("ftp://example.com/x")` | 0.0 s | `Error: read_url only accepts http and https addresses. 'ftp:' addresses are not supported. ...` | Could not read the page |
| `research`, fake key | 0.8 s | `Error: Tabstack rejected the API key. An Open WebUI admin needs to check the key in the tool's Valves. ...` | Starting research, Research failed |
| `read_url("https://www.python.org/")`, fake key | 0.3 s | `Error: Tabstack rejected the API key. ...` | Reading www.python.org, Could not read the page |

The fake key did not appear in any result or status event. The two API calls
confirm the live 401 path is mapped as designed; they say nothing about
successful calls.

## 3. Chat runs

### Run r0: research question, no key set, `llama3.2:3b`

- Setup: new chat, model `llama3.2:3b`, Tabstack switched on for the chat
  under the Integrations button > Tools (`screenshots/chat-01-integrations.png`,
  `screenshots/chat-02-tools-toggle.png`). Function calling left on its
  default (Native). Open WebUI's own web search was off (default config).
- Prompt: "What did the Python Software Foundation announce this week? Use the
  research tool."
- Outcome: **the model did not call the tool.** It replied "I can't help with
  that." Open WebUI then suggested a follow-up, "Can you use the research tool
  again to find the announcement?", although the tool had not been used.
  (`screenshots/run-r0-no-tool-call.png`)
- The Ollama log shows the request carrying the tools: 2,050 prompt tokens,
  8 tokens generated, 4 min 37 s for that request. No tool call appears in the
  saved chat. Because the tool was never called, the missing key was never
  reported in the chat.
- One message produced four `/api/chat` requests to Ollama in total (the
  reply plus Open WebUI's background tasks such as the title and follow-ups),
  finishing about 10 minutes after the message was sent.

This is one run of one prompt, so it shows only that this model can decline
a tool it was explicitly asked to use. It is not a rate.

## 4. Model speed in this environment

Generation inside the Ollama container was far slower than prompt processing:

| Measurement | Prompt processing | Generation |
| --- | --- | --- |
| Run r0, request with tool specs (Ollama log) | 13.46 tokens/s | 0.20 tokens/s |
| `ollama run llama3.2:3b --verbose "Say hello in five words."` in the container | 1.74 tokens/s | 0.26 tokens/s |
| Host Ollama 0.40.7 (Metal), existing `llama3.1:8b`, same prompt | 36.3 tokens/s | 14.3 tokens/s |

During these measurements the host load average was between 8.8 and 11.0 on
8 cores, with other applications running (the top process was a game at about
66% CPU). The cause of the slow container generation was not investigated
further. At this speed one chat turn with the tool takes several minutes
before the tool result is even processed.

## 5. Still to do (needs credits)

- Five research questions and three `read_url` calls, with outcomes as they
  happen, including runs where the model does not call the tool.
- Screenshots of live status updates and of a cited answer with source chips.
- A recorded Research stream to replace or sit beside the hand-built fixtures.
- Usage, if the API reports it (`metadata.metrics`); no cost will be estimated.
