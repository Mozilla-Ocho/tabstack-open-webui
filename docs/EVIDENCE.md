# Evidence

What was actually run, when, and what happened. Nothing here is estimated.

## Status

Sections 1 to 6 were run before the Tabstack account had credits (empty key
or a deliberately fake key). Section 7 covers the first runs with a working
key, which were **stopped part way** after the model started calling Research
repeatedly. Section 8 covers the re-run after the tool was changed to cap
calls per reply. Between them, all five research questions and three
`read_url` calls have an outcome with a working key.

## Environment

| Item | Value |
| --- | --- |
| Date | 2026-10-21 |
| Machine | Apple M3, macOS 26.5.2 (build 25F84) |
| Container runtime | OrbStack 2.2.3, Docker 29.4.0, Docker Compose v5.1.2; 8 CPUs and 11.74 GiB visible to containers |
| Open WebUI | `ghcr.io/open-webui/open-webui:v0.12.3` (`sha256:a537bbe1e6b4...`), reports version 0.12.3 |
| Ollama, `host` profile | Ollama 0.40.7 installed on the Mac (Metal), reached from the container at `host.docker.internal:11434` |
| Ollama, `bundled` profile | `ollama/ollama:0.40.7` (`sha256:2e6cc92ffa43...`), CPU only |
| Models, `host` profile | `llama3.1:8b` (ID `46e0c10c039e`, 4.9 GB), `qwen3:8b` (ID `500a1f067a9f`, 5.2 GB); both already on the machine |
| Models, `bundled` profile | `llama3.2:3b` (ID `a80c4f17acd5`, 2.0 GB); `qwen3:4b` pulled but not used |
| Tabstack SDK | `tabstack==2.8.5`, installed by Open WebUI from the tool's frontmatter |
| Python inside Open WebUI | 3.11.17 |
| Tool version | `tabstack_tool.py` 0.1.0 at the commit that adds this file |
| Browser automation | Playwright 1.58.0 with headless Chromium, driving the real UI |

The compose file is `docker-compose.yml` in this repo. Each profile has its own
data volume, and in each the admin account was created through the normal
first-run sign-up page. The `bundled` profile was tried first (sections 1 to
4); the `host` profile was set up afterwards because of the speed problem in
section 4, and the tool-calling runs in section 5 use it.

## 1. Installing the tool through the admin UI

Done on a fresh volume for each profile. The screenshots are from the `host`
profile install. Steps as performed:

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
- On the `host` install the stored source was byte-identical to the file
  (35,959 characters).

Findings from the install:

- **Open WebUI reformats tool code with black when it is saved.** On the
  first `bundled` install the stored copy was 875 characters longer than the
  file, with lines rewrapped at 88 columns. The editor calls
  `/api/v1/utils/code/format`, which runs
  `black.format_str(code, mode=black.Mode())` (black 26.5.1 in the image).
  The file was changed to keep to black's style, and a test now checks that
  black leaves it unchanged.
- **Metadata is not re-read when an existing tool is edited.** After pasting a
  changed file over the saved tool, the list kept the first description. This
  matches the docs ("it does not re-derive fields when editing an existing
  item").

## 2. Enabling the tool for a model

Two ways were used, both through the UI:

- **For one chat:** the Integrations button under the message box > Tools >
  switch Tabstack on (`screenshots/chat-01-integrations.png`,
  `screenshots/chat-02-tools-toggle.png`).
- **For a model, permanently:** user menu > Admin Panel > Settings > Models >
  click the model. In the editor, the Tools row opens a picker; ticking
  Tabstack and clicking Done, then Save & Update, stored
  `toolIds: ["tabstack"]` on the model (`screenshots/model-01-editor.png`,
  `screenshots/model-02-tools-picker.png`). New chats with that model then
  start with the tool on.

The same editor has a Capabilities row with a single **Builtin Tools** switch,
on by default (`screenshots/model-03-capabilities.png`). Section 5 shows why
it matters. Typing `/admin/settings` into the address bar redirected to the
chat page in this setup; going through the user menu worked.

## 3. The tool running inside Open WebUI's runtime

`tabstack_tool.py` was copied into the running Open WebUI container
(`bundled` profile) and called directly with its Python 3.11.17 and the SDK it
had installed. The copy was deleted afterwards. A deliberately fake key was
used for the two calls that reach the API.

| Case | Time | Result returned to the model | Status lines |
| --- | --- | --- | --- |
| `research`, no key | 0.0 s | `Error: the Tabstack tool has no API key. An Open WebUI admin needs to add one in the tool's Valves. ...` | Research failed |
| `read_url("ftp://example.com/x")` | 0.0 s | `Error: read_url only accepts http and https addresses. 'ftp:' addresses are not supported. ...` | Could not read the page |
| `research`, fake key | 0.8 s | `Error: Tabstack rejected the API key. An Open WebUI admin needs to check the key in the tool's Valves. ...` | Starting research, Research failed |
| `read_url("https://www.python.org/")`, fake key | 0.3 s | `Error: Tabstack rejected the API key. ...` | Reading www.python.org, Could not read the page |

The fake key did not appear in any result or status event. The two API calls
confirm the live 401 path is mapped as designed; they say nothing about
successful calls.

## 4. Model speed in the `bundled` profile

Generation inside the Ollama container was far slower than prompt processing:

| Measurement | Prompt processing | Generation |
| --- | --- | --- |
| Run r0 (below), request with tool specs, from the Ollama log | 13.46 tokens/s | 0.20 tokens/s |
| `ollama run llama3.2:3b --verbose "Say hello in five words."` in the container | 1.74 tokens/s | 0.26 tokens/s |
| Host Ollama 0.40.7 (Metal), `llama3.1:8b`, same prompt | 36.3 tokens/s | 14.3 tokens/s |

During these measurements the host load average was between 8.8 and 11.0 on
8 cores, with other applications running (the top process was a game at about
66% CPU). The cause of the slow container generation was not investigated
further.

**Run r0** (`bundled`, `llama3.2:3b`, tool switched on for the chat, built-in
tools on, no key): prompt "What did the Python Software Foundation announce
this week? Use the research tool." The model replied "I can't help with that."
without calling the tool. The request carrying the tools was 2,050 prompt
tokens, 8 tokens generated, 4 min 37 s. Open WebUI then suggested the
follow-up "Can you use the research tool again to find the announcement?",
although the tool had not been used. (`screenshots/run-r0-no-tool-call.png`)

## 5. Tool-calling runs, `host` profile, no key

All runs used a new chat, function calling on its default (Native), Open
WebUI's own web search off (default config) and an empty API key valve, so
every tool call returned the missing-key message. These runs test whether the
model calls the tool and what it does with an error. They do not test
answers.

To see exactly what the model was offered, a small logging proxy was placed
between Open WebUI and Ollama for runs p1b to q4. It recorded the tool names
in each `/api/chat` request and nothing else. Open WebUI's Ollama URL was
pointed at it through the admin API and set back afterwards.

**What the model is offered.** With built-in tools on (the default), every
chat request listed **38 tools**: `read_url`, `research`, and 36 of Open
WebUI's own (`get_current_timestamp`, `calculate_timestamp`, `ask_user`, seven
knowledge tools, `search_chats`, `view_chat`, eight memory tools, four note
tools, `create_skill`, two task tools, five automation tools and four
calendar tools). With the model's Builtin Tools capability switched off, the
list was exactly `read_url` and `research`.

| Run | Model | Built-in tools | Prompt | Tool calls | What the model said | Time to done |
| --- | --- | --- | --- | --- | --- | --- |
| p1 | llama3.1:8b | on (tool on for chat) | What did the Python Software Foundation announce this week? | none | No ability to browse; listed places to look | not recorded |
| p1b | llama3.1:8b | on (tool on for chat) | same | none | "I don't have any information about the Python Software Foundation announcing something recently." | 28.4 s |
| p2 | llama3.1:8b | on (tool on for model) | same | none | No ability to browse; suggested places to look | 57.8 s |
| p3 | llama3.1:8b | off | same | `research({"question": "Python Software Foundation announcement this week"})` | "I'm unable to find the latest announcement ... I recommend checking the official Python Software Foundation website" | 28.1 s |
| p4 | llama3.1:8b | off | Summarise this page for me: https://docs.python.org/3/whatsnew/3.14.html | `read_url({"url": "https://docs.python.org/3/whatsnew/3.14.html"})` | "To summarize the page, I would need to access the webpage directly", then steps to read it in a browser | not recorded |
| p5 | llama3.1:8b | off | What is the latest stable release of Node.js, and when did it come out? | `research` with the prompt as the question | "I'm unable to retrieve the latest stable release ... I recommend checking the official Node.js website" | 8.6 s |
| q1 | qwen3:8b | on (tool on for model) | What did the Python Software Foundation announce this week? | none | No access to real-time news; pointed to python.org/psf and offered to help search | 80.3 s |
| q2 | qwen3:8b | off | same | `research` with the prompt as the question | Said there was "an error related to the Tabstack tool's API key configuration, which isn't directly relevant to your question", then pointed to the PSF website | 139.2 s |
| q3 | qwen3:8b | off | Summarise this page for me: https://docs.python.org/3/whatsnew/3.14.html | `read_url` with that URL, **twice** (called again after the first error) | Said the tool had a configuration problem and to "contact the Open WebUI admin to add an API key for the Tabstack tool in its Valves settings" | not recorded |
| q4 | qwen3:8b | off | What is the latest stable release of Node.js, and when did it come out? | `research` with the prompt as the question | "It seems there's a technical issue with the tools ... My knowledge cutoff is October 2023" | 295.9 s |

"Time to done" is wall-clock time from sending the message until Open WebUI
marked the reply done, measured by the test script; it includes Open WebUI's
background title request. Rows marked "not recorded" were not captured by the
script.

Screenshots: `screenshots/run-p1b-builtins-on-no-call.png`,
`screenshots/run-p3-builtins-off-called.png`,
`screenshots/run-p4-read-url-called.png`. In the UI a failed call shows as a
red cross with "View Result from research", because Open WebUI marks a tool
result that starts with `Error:` as failed.

What these runs show, and only for these prompts and models:

- With built-in tools on, neither model called the tool in any of the four
  runs (p1, p1b, p2, q1), whether the tool was switched on for the chat or for
  the model.
- With built-in tools off, both models called the right function with
  sensible arguments in all six runs (p3, p4, p5, q2, q3, q4).
- The missing-key message was relayed accurately once (q3). In q2 the model
  mentioned the key but called it irrelevant. In the other four the models
  said they were unable to get the information, without saying why.
- In q3 the model called `read_url` a second time after the error. The tool
  adds no retries, but a model can repeat a call by itself; with a working key
  that would repeat the work and the billing.

### 5a. Re-run with reworded error messages (v2)

After the runs above, every failure message was rewritten as one sentence for
the user, followed by "Tell the user exactly this. Do not call this tool again
for this question." (for an invalid URL the second part instead allows a call
with a full http or https address). The missing-key message became:

```
Error: The Tabstack tool is not set up yet: it has no API key. An Open WebUI
admin needs to add one in the tool's Valves. Tell the user exactly this. Do
not call this tool again for this question.
```

The updated file was pasted over the saved tool, and the six built-ins-off
prompts were run again with the same models and settings, still with no key.
No proxy was used for these runs.

| Run | Model | Prompt | Tool calls | What the model said | Time to done |
| --- | --- | --- | --- | --- | --- |
| p3v2 | llama3.1:8b | What did the Python Software Foundation announce this week? | `research` once, with the prompt as the question | "The Python Software Foundation did not announce anything this week. Unfortunately, the "research" tool is not set up with an API key, so I couldn't retrieve any information. An Open WebUI admin needs to add an API key in the tool's Valves for future queries." | 17.2 s |
| p4v2 | llama3.1:8b | Summarise this page for me: https://docs.python.org/3/whatsnew/3.14.html | `read_url` once, with that URL | "The Tabstack tool is not set up yet. An Open WebUI admin needs to add an API key in the tool's Valves." | 8.7 s |
| p5v2 | llama3.1:8b | What is the latest stable release of Node.js, and when did it come out? | `research` once (`"latest stable release of Node.js and release date"`) | Same as p4v2 | 6.6 s |
| q2v2 | qwen3:8b | What did the Python Software Foundation announce this week? | `research` once | The message word for word, then "I cannot proceed with further research or function calls for this question until this issue is resolved." | 127.4 s |
| q3v2 | qwen3:8b | Summarise this page for me: https://docs.python.org/3/whatsnew/3.14.html | `read_url` once | The message word for word | 254.3 s |
| q4v2 | qwen3:8b | What is the latest stable release of Node.js, and when did it come out? | `research` once | The message word for word | 203.1 s |

Screenshots: `screenshots/run-p4v2-error-relayed.png`,
`screenshots/run-p3v2-relayed-with-invented-claim.png`.

- All six replies told the user the tool had no API key and that an admin
  needs to add one, against one accurate relay in the first round.
- Every run made exactly one tool call; in the first round q3 had called
  `read_url` twice.
- **p3v2 invented a fact.** It opened with "The Python Software Foundation did
  not announce anything this week", which the model had no basis for: the
  tool returned only the missing-key error. The rest of the reply relayed the
  error correctly. Better wording did not stop the model adding its own
  claim.

## 6. Setting the key, updating the tool, and a rejected key

All on the `host` profile, through the UI, with a deliberately fake key
(`fake-key-...`, not a Tabstack key).

- **Setting the key.** In Workspace > Tools, the Tabstack row has two buttons
  labelled Valves in the accessibility tree; the gear icon opens the dialog.
  Clicking **Default** next to Tabstack Api Key switched it to **Custom** and
  showed a masked input with a show/hide eye; after typing the key and
  clicking Save, `GET /api/v1/tools/id/tabstack/valves` returned it.
  (`screenshots/install-06-valve-key-set.png`, where the key shows only as
  dots.)
- **Updating keeps the key.** A copy of the file with one extra comment line
  was pasted over the saved tool and saved, then the real file was pasted back
  and saved. The key was still stored after each save. Saving an update showed
  no confirmation dialog (unlike creating the tool). Pasting a file identical
  to the stored one leaves Save disabled.
- **Run p6** (`llama3.1:8b`, built-ins off, fake key): "What is the latest
  stable release of Node.js, and when did it come out?" The model called
  `research` once. The tool reached the API and got a 401; status lines were
  "Starting research", then "Research failed". The model replied that the tool
  had rejected the API key and an Open WebUI admin needs to check the key in
  the tool's Valves, then suggested the official Node.js website. The fake key
  did not appear anywhere in the saved chat.
- The key was then switched back to Default (empty), and the API confirmed no
  key was stored.

## 7. Live runs with a working key (stopped)

### 7.1 Recorded directly through the SDK

One Research call and one Extract call were made with the SDK, outside Open
WebUI, to record fixtures (`tests/fixtures/recorded_*`):

- `agent.research(query="What is the latest stable release of Node.js, and
  when did it come out?", mode="fast")`: 10 events from `start` to
  `complete` in 15.4 s; 4 cited pages, markers `[1]` to `[4]` all matched.
  The `complete` event had **no `metrics` field**, so the API reported no
  usage for this call.
- `extract.markdown(url="https://docs.python.org/3/whatsnew/3.14.html",
  metadata=True)`: 148,117 characters in 1.6 s.

### 7.2 Chat runs

Setup: `host` profile, `llama3.1:8b` with Tabstack enabled on the model and
**Builtin Tools switched off** (the configuration from section 5), function
calling Native, the real key set through the Valves dialog by a script that
read it from the environment. Default valves (`fast` mode, 300 s limit).

| Run | Prompt | Research calls | What happened |
| --- | --- | --- | --- |
| R1 | What is the latest stable release of Node.js, and when did it come out? | 1 | Statuses: Starting research, Planning searches, Searching, round 1 of 1, Found 4 new sources, Writing the answer, Research finished, 3 sources. The reply repeated the tool's answer word for word with its `[n]` markers, which rendered as source chips, and a 3-source panel. 56.3 s to done. (`screenshots/live-R1-status-searching.png`, `screenshots/live-R1-cited-answer.png`) |
| R2 | What did the Python Software Foundation announce this week? | 4 | All four calls succeeded. Each answer named the same two announcements (the wiki relaunch on 20 October 2026 and the Q3 2026 Fellows on 19 October 2026). The model called `research` again after each, then replied: "I apologize for the limited knowledge I have on this topic. I am unable to verify any information regarding a recent announcement by the Python Software Foundation." 10 sources ended up on the one message. 63.1 s to done. (`screenshots/live-R2-four-calls-no-answer.png`) |
| R3 | What changed in Open WebUI version 0.12.3? | 1 | One successful call with 5 sources. The model rewrote the answer as bullet points, put `[1]` at the start of every bullet and dropped some of the original markers, so the chips no longer match the sources Tabstack cited for each point. 133.1 s to done. (`screenshots/live-R3-markers-rewritten.png`) |
| R4 | Which UK bank holidays are left in 2026 in England? | 24 | Every call succeeded; the first said two bank holidays remained (Christmas Day on 25 December and the Boxing Day substitute on 28 December 2026). The model kept calling `research` with the same question. The run was stopped by hand (below) with no reply text. (`screenshots/live-R4-repeated-calls.png`) |
| R5 | Who won the most recent Formula 1 Grand Prix? | 2 | Both calls succeeded with the same answer; stopped by hand before the model replied. |

The three `read_url` prompts were not run.

### 7.3 How the runs were stopped

When R4 passed about 19 calls, the key was cleared from the valve through the
admin API (so any further call would fail without reaching Tabstack), the
test scripts were killed, and the two generation tasks still running on the
server (R4 and R5) were stopped with `POST /api/tasks/stop/{id}`. Every one of
the 24 R4 calls and both R5 calls had already returned a successful answer,
so all were made with the key.

Total calls made with the working key on 2026-10-21: **33 Research calls**
(1 recording, then 1 + 4 + 1 + 24 + 2 in chat) and **1 Extract call**. No
cost is estimated here; the API reported no usage for the recorded call.

### 7.4 Why the loop could run so long

Open WebUI caps tool-calling turns per reply with
`CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS`, default **256**. It is read once at
start-up and cannot be changed in the admin UI (`owui/backend/open_webui/env.py`;
docs `reference/env-configuration.mdx`). Nothing else in Open WebUI or in the
tool stopped R4. The tool adds no retries, but it also did nothing to stop the
model calling it again after a successful answer.

## 8. Re-run with per-reply call caps

Changes made after section 7, all in the repo:

- The tool counts calls per reply, keyed on the `__message_id__` Open WebUI
  passes it, and refuses calls beyond `max_research_calls_per_reply`
  (default 1) or `max_read_url_calls_per_reply` (default 3) without
  contacting Tabstack.
- Successful results end with "This is the complete answer from Tabstack.
  Answer the user from it now ... Do not call research again for this
  question." (and a matching line for `read_url`).
- `docker-compose.yml` sets `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS=3`. The
  container was recreated with it (`printenv` inside showed `3`). Recreating
  it signed the browser session out, because the image keeps its generated
  secret key outside the data volume.
- Source titles lose decorative punctuation.

Same setup as section 7.2 otherwise (`llama3.1:8b`, Builtin Tools off, real
key set through the Valves dialog from the environment). A watchdog script
polled the chats and would have cleared the key if any reply got more
successful calls than the caps allow; it did not trigger. The key was cleared
from the valve straight after the runs.

| Run | Prompt | Calls | Statuses | What the model did | Time to done |
| --- | --- | --- | --- | --- | --- |
| L2 | What did the Python Software Foundation announce this week? | `research` x1 | Starting research ... Research finished, 4 sources | Answered from the result (the wiki relaunch on 20 October 2026 and the Q3 2026 Fellows on 19 October 2026). Kept markers `[1]` and `[4]`, dropped `[2]` and `[3]`, and repeated only sources 1 and 4 at the end. | 32.0 s |
| L4 | Which UK bank holidays are left in 2026 in England? | `research` x1 | ... Research finished, 6 sources | Answered from the result as two bullet points (Christmas Day, 25 December, and the Boxing Day substitute day, 28 December 2026) with all markers kept; they rendered as source chips. (`screenshots/live-L4-status.png`, `screenshots/live-L4-cited-answer.png`) | 21.4 s |
| L5 | Who won the most recent Formula 1 Grand Prix? | `research` x1 | ... Research finished, 7 sources | Answered from the result (Max Verstappen, Singapore Grand Prix, 11 October 2026). Cut the first sentence's markers from `[1]`..`[7]` to `[1]`. | 23.5 s |
| U1 | Summarise this page for me: https://docs.python.org/3/whatsnew/3.14.html | `read_url` x1 | Reading docs.python.org, Read docs.python.org | The page was 148,117 characters; the tool passed the first 19,777 with the truncation note. The model wrote its summary as answers to four questions nobody asked ("Based on the provided text, I'll answer your questions."), using points from the passed text. (`screenshots/live-U1-read-url.png`) | 34.7 s |
| U2 | What does this page say about tools? https://docs.openwebui.com/features/extensibility/plugin/tools/ | `read_url` x1 | Reading docs.openwebui.com, Read docs.openwebui.com | The tool passed the first 19,860 of 84,240 characters. The model answered as if asked how to enable Native mode, which is what the last section of the passed text covers, rather than summarising what the page says about tools. | 54.5 s |
| U3 | Can you read https://www.gov.uk/bank-holidays and list the next three dates? | `read_url` x1 | Reading www.gov.uk, Read www.gov.uk | **Wrong dates.** In the Markdown Tabstack returned, the bank holiday tables run their cells together ("DateDay of the weekBank holidayFridayChristmas DayMondayBoxing Day (substitute day)") with the date column missing; only "25 December" appears, in a heading. The model listed "25 December 2023", "26 December 2023" and "6 January 2024"; the last two dates do not appear in the text it was given, and 2023 and 2024 appear only as headings for past years. (`screenshots/live-U3-read-url-table.png`) | 61.6 s |

Every run made exactly one billed call, against 4, 24 and 2 calls for the same
three research questions in section 7. The caps were not reached in these
runs, so the refusal path has been exercised by the tests only, not live.

Calls made with the working key in this section: 3 Research, 3 Extract.
Running totals for 2026-10-21: **36 Research calls and 4 Extract calls**. The
API reported no usage for any of them (the tool does not receive `metrics`
in fast mode).

What these runs show, for this model and these prompts only:

- With the caps and the end-of-answer instruction, the model answered from
  the first result each time instead of calling again.
- Citation markers survive inconsistently: one reply kept them all, two kept
  some, and in section 7 one reply moved them.
- A long page is trimmed to the first 20,000 characters, and the model can
  end up answering from whatever part of the page that leaves it with (U2).
- When the extracted Markdown loses information, here a table's date column,
  the model may fill the gap with invented dates (U3). The tool passes on
  what Tabstack returns and cannot detect this.

## 9. Not done

- The per-reply refusal was not seen in section 8; it was seen in
  section 11 (run K2).
- Only `llama3.1:8b` was run with a working key; `qwen3:8b` was run without
  one (section 5).
- The `bundled` profile was not used for any run after section 4.
- The per-reply cap for API requests that send no message id is covered by
  tests only. The chat runs in section 11 always have a message id.

## 10. `read_url_effort=max` on the bank holidays page

Run on 2026-10-26 through the SDK directly (`tabstack==2.8.5`, outside Open
WebUI), to see whether `max` effort keeps the date column that was missing in
run U3:
`extract.markdown(url="https://www.gov.uk/bank-holidays", metadata=True, effort="max")`.

- 12,267 characters in 12.3 s.
- **The date column is still missing.** Each table still comes through as
  run-together text with no table markup (no `|` anywhere in the page), for
  example "Upcoming bank holidays in England and Wales 2026
  DateDay of the weekBank holidayFridayChristmas DayMondayBoxing Day
  (substitute day)". No date in the form "25 December 2026" appears anywhere;
  the only dates are in the "next bank holiday" headings. In the page HTML
  each date is a `<th scope="row">` wrapping a `<time>` element.
- So the loss is in how Tabstack converts this page, not in how much effort
  it spends rendering it. The tool cannot recover the dates.

Calls made with the working key on 2026-10-26: 1 Extract.

## 11. Per-user keys, refusal, focus, numbering and built-in tools

Run on 2026-10-26 on the `host` profile (Open WebUI v0.12.3, same container
as sections 5 to 8, `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS=3`), with
`tabstack_tool.py` at the commit that adds this section, pushed through
`POST /api/v1/tools/id/tabstack/update` (stored content identical to the
file). Chats were driven through the real UI with Playwright and headless
Chromium, signed in as a second admin account created for these runs (the
first account's password had not been kept). Tool calls, results, sources and
statuses were read back with `GET /api/v1/chats/{id}`. Valves at their
defaults unless stated (`fast` mode, 1 Research and 3 `read_url` calls per
reply).

### 11.1 Keys, refusal, focus and numbering (`llama3.1:8b`, Builtin Tools off)

| Run | Setup | Prompt | Calls | What happened | Time |
| --- | --- | --- | --- | --- | --- |
| K1 | `require_user_key` on, no key anywhere | Which UK bank holidays are left in 2026 in England? | `research` x1 | The tool returned "Error: The Tabstack tool needs your own API key, and none is set. Add it under Chat Controls > Valves > Tabstack. ..." without contacting Tabstack; status "Research failed". The model paraphrased it and then invented a JSON "command" with an `apikey` field for the user to run. (`screenshots/live-K1-no-user-key.png`) | 24.9 s |
| K2 | Admin key empty, `require_user_key` on, key set only in this user's valve | Use the research tool twice, as two separate calls: first research which UK bank holidays are left in 2026 in England, then research who won the most recent Formula 1 Grand Prix. | `research` x2 in one turn | The first call ran on the user's key (the only key set) and returned 5 sources. The second returned "Error: research already ran 1 time for this reply, so it was not run again and nothing was charged. ..." with status "Research not run again". The reply answered the bank holiday question, did not mention the refused Formula 1 question, and kept no `[n]` markers. (`screenshots/live-K2-second-call-refused.png`) | 46.6 s |
| K3 | As K2 | First read https://docs.python.org/3/whatsnew/3.14.html with focus on Zstandard and tell me what it says about Zstandard support. Then use research to find out when Python 3.15 is due to be released. | `read_url` x1 with `focus: "Zstandard"`, `research` x1 | `read_url` passed 6,998 characters with "[Focused on: zstandard. Kept 3 of 69 parts of the page (6,650 of 148,117 characters), in page order; ...]"; the text mentions `zstd` 8 times. The page became source 1 and Research numbered its 7 sources `[2]` to `[8]`, matching the order of the 8 sources on the message. The reply described `compression.zstd` and gave 9 October 2026 for Python 3.15.0, with no `[n]` markers. (`screenshots/live-K3-read-then-research.png`) | 62.7 s |

### 11.2 Whether the model calls the tool with built-in tools on

Prompts from section 7. Each pair of runs used both models with the model's
Builtin Tools capability switched as shown.

| Run | Builtin Tools | `research` docstring | `llama3.1:8b` | `qwen3:8b` |
| --- | --- | --- | --- | --- |
| B (PSF prompt) | on, all categories | as committed | no call: "I don't have information on what the Python Software Foundation announced this week." 27.9 s (`screenshots/live-B1-builtins-on-no-call.png`) | no call: no real-time access, pointed to the PSF blog. 90.4 s |
| V1 (PSF prompt) | on, all categories | reworded to "Call this for any question about news, announcements, releases, prices, or events ... Do not answer from memory or say you have no real-time access; call this tool instead." (not committed) | no call. 331.8 s | no call. 263.7 s |
| P1 (PSF prompt) | on, only `memory`, `knowledge`, `files` | as committed | `research` x1, 3 sources, reply without markers. 373.0 s | `research` x1, 4 sources, all markers kept and shown as chips. 211.5 s (`screenshots/live-P1-slim-builtins-called.png`) |
| P2 (Formula 1 prompt) | on, only `memory`, `knowledge`, `files` | as committed | `research` x1, 4 sources, markers kept. 256.7 s (`screenshots/live-P2-slim-builtins-called.png`) | `research` x1, 4 sources, markers kept. 421.3 s |

The categories were set through the model's `meta.builtinTools` (the 17
categories in v0.12.3: `automations`, `calendar`, `channels`, `chats`,
`code_interpreter`, `files`, `image_generation`, `knowledge`, `memory`,
`notes`, `notifications`, `skills`, `subagents`, `tasks`, `time`,
`user_input`, `web_search`). `files` only adds tools when the chat has files.
Times in this section are longer than in section 8 because Ollama was
switching between the two models.

What these runs show, for these two models and prompts only: with every
built-in category on, neither model called the tool in 4 runs, and rewording
the docstring did not change that. With only memory, knowledge and files on,
both models called it in all 4 runs. Which of the 14 other categories matters
was not narrowed down.

### 11.3 UI paths checked by hand

Checked in the UI with Playwright later the same day, without saving
anything (the model's settings read back unchanged through the API):

- The user valve is under **Controls** (the sliders icon at the top right of
  a chat) > **Valves**, with a first list set to **Tools** and a second list
  offering **Tabstack**. The panel is titled "Controls", not "Chat
  Controls", so the tool's message was changed to name it that way. The key
  field is masked, with an eye icon to reveal it.
  (`screenshots/user-valve-01-controls.png`)
- **Admin Panel > Users > Groups** has a **Default permissions** entry.
- In the model editor, a **Builtin Tools** row appears below **Default
  Features** only while the **Builtin Tools** capability is on. Clicking it
  shows the category switches, including **Memory**, **Knowledge Base** and
  **Files**. (`screenshots/model-04-builtin-categories.png`, taken with the
  capability switched on in the page but not saved)

### 11.4 Clean-up

After the runs, the key was cleared from the user valve (the admin valve was
already empty), `require_user_key` was switched back off, and both models were
set back to Builtin Tools off with no per-category settings.

Calls made with the working key in this section: 6 Research (K2, K3, P1 x2,
P2 x2; the refused second call in K2 made none) and 1 Extract (K3). Totals for
2026-10-26, with section 10: **6 Research calls and 2 Extract calls**.
