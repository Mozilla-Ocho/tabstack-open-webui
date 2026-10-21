# Installing the Tabstack tool in Open WebUI

This guide is for someone who already has Open WebUI running with at least one
local model. Every step below was carried out on Open WebUI v0.12.3; the runs
are recorded in [EVIDENCE.md](EVIDENCE.md).

## Before you start

You need:

- An Open WebUI **admin** account. Workspace tools run Python on your server,
  so only admins (or users an admin trusts with that) can add them.
- A model that supports **tool calling**, for example `llama3.1:8b` or
  `qwen3:8b` from Ollama. Leave the model's function calling on its default,
  Native.
- A **Tabstack API key**.
- The Open WebUI server must be able to reach the internet, both to install
  the Tabstack SDK when you save the tool and to call Tabstack.

If your Open WebUI runs with `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=False`
(common in multi-worker deployments), it will not install the SDK for you. Add
`tabstack==2.8.5` to your Open WebUI image first.

## 1. Add the tool

1. Open **Workspace** in the left sidebar, then the **Tools** tab.
2. Click **Create** (top right). An editor opens with a sample tool in it.
3. Open [`tabstack_tool.py`](../tabstack_tool.py), copy the whole file, click
   into the editor, select all and paste over the sample.
4. The **Tool Name**, **Tool ID** and **Tool Description** fields above the
   editor fill themselves in from the top of the file (`Tabstack`,
   `tabstack`, and a one-line description). Leave them as they are.
5. Click **Save & Create** (bottom right).
6. A **Confirm your action** dialog warns that tools can run arbitrary code.
   Read it, then click **Confirm**.

Open WebUI now installs the Tabstack SDK, which can take a little while. When
it has finished you are back on the Tools list with **Tabstack** in it.

![The Tools list with Tabstack installed](screenshots/install-04-tools-list.png)

Open WebUI runs a code formatter over tool code when you save it. The file is
already in that format, so nothing in it changes.

## 2. Set your API key

1. In **Workspace > Tools**, click the **gear icon** (Valves) on the Tabstack
   row.
2. The **Valves** dialog lists the tool's settings. Each one shows
   **Default** until you change it.
3. On **Tabstack Api Key**, click **Default**. It changes to **Custom** and
   a masked text field appears under it.
4. Paste your key into the field and click **Save**.

![The Valves dialog with the key field switched to Custom](screenshots/install-06-valve-key-set.png)

The other settings can stay on their defaults:

| Setting | Default | What it does |
| --- | --- | --- |
| Research Mode | `fast` | `balanced` reads more sources and needs a paid Tabstack plan. |
| Research Timeout Seconds | 300 | Stops a Research call that runs longer than this. |
| Research Idle Timeout Seconds | 120 | Stops a Research call if Tabstack sends nothing for this long. |
| Read Url Timeout Seconds | 60 | Stops reading a page after this long. |
| Read Url Effort | `standard` | `max` renders JavaScript-heavy pages but is slower. |
| Read Url Max Chars | 20000 | Longest page text passed to the model. Longer pages are trimmed, with a note saying how much was kept. |

About where the key is kept:

- The key field is masked on screen, and the tool never puts the key into
  the chat.
- Open WebUI stores valve values in its database as plain text unless the
  admin sets `ENABLE_VALVE_ENCRYPTION=true` (keep `WEBUI_SECRET_KEY` stable if
  you do, or stored values become unreadable).
- Anyone you give write access to the tool can read its valves.

For how Tabstack handles the questions and pages it receives, see
[Tabstack's Trust page](https://tabstack.ai/trust).

## 3. Turn the tool on for a model

**For every chat with a model (recommended):**

1. Open the user menu (your initials, bottom left) and choose
   **Admin Panel**.
2. Go to **Settings**, then **Models**, and click the model you want to use.
3. Click the **Tools** row. A picker opens; click **Tabstack**, then
   **Done**.
4. Click **Save & Update** (bottom right).

![Choosing Tabstack in the model's Tools picker](screenshots/model-02-tools-picker.png)

New chats with that model now start with the tool switched on. In the same
editor, leave **Status Updates** and **Citations** switched on under
**Capabilities**; the tool uses both.

**For a single chat:** under the message box, click the **Integrations**
button (next to **+**), open **Tools**, and switch **Tabstack** on.

![Switching the tool on for one chat](screenshots/chat-02-tools-toggle.png)

## 4. Check that it works

> Pending: this step has not yet been run with a working key. It will be
> checked against a live run before this guide is published.

1. Start a new chat with the model from step 3.
2. Ask something that needs current information, for example "What is the
   latest stable release of Node.js, and when did it come out?"
3. While Research runs, status lines appear above the reply, starting with
   **Starting research** and **Planning searches**, and ending with
   **Research finished, N sources**.
4. The reply should contain numbered markers such as `[1]`, shown as source
   chips, with the sources listed under the message.
5. To check `read_url`, paste a link and ask for a summary, for example
   "Summarise this page for me: https://docs.python.org/3/whatsnew/3.14.html".
   The status shows **Reading docs.python.org**, then **Read docs.python.org**.

If something is wrong with the setup, the tool returns a short message that
starts with `Error:`, and Open WebUI shows the tool result with a red cross.
Click **View Result** on it to read the message, for example that the key is
missing or was rejected.

## 5. If the model never calls the tool

Whether a model decides to use a tool is up to the model. The tool cannot make
it. These are the things to check, in order.

**The tool is switched on.** A wrench icon with a number appears under the
message box when tools are on for the chat. If it is missing, repeat step 3.

**Open WebUI's built-in tools are crowding it out.** By default Open WebUI
offers the model about 36 tools of its own (memory, notes, knowledge bases,
chat search, calendar, automations and more) alongside this one. In testing,
`llama3.1:8b` and `qwen3:8b` did not call the Tabstack tool in any of four
runs while those were on, and called it in all thirteen runs once they were
off (see [EVIDENCE.md](EVIDENCE.md), sections 5 and 6). That is a small
number of runs on two models, not a guarantee. To switch them off for one
model:

1. Open the model in **Admin Panel > Settings > Models**, as in step 3.
2. Click the **Capabilities** row and switch **Builtin Tools** off.
3. Click **Save & Update**.

![The model's Capabilities, with the Builtin Tools switch](screenshots/model-03-capabilities.png)

The trade-off: that model can no longer use Open WebUI's built-in tools, such
as saving memories, reading notes or searching your knowledge bases. You can
instead leave **Builtin Tools** on and switch off individual categories in the
**Builtin Tools** row below it, but the runs above only tested switching the
whole capability off.

**Open WebUI's own web search is on.** If an admin has enabled web search, the
model is also offered Open WebUI's `search_web` and `fetch_url`, which do
similar jobs and may be chosen instead.

**The model is too small or not trained for tools.** Asking directly ("Use the
research tool to find ...") can help. If it still never calls the tool, try a
model with stronger tool-calling support.

## Other problems

**The model passes on the error badly, or adds things of its own.** Error
messages ask the model to repeat them word for word and not to call the tool
again for the same question. In testing the models did relay them, but one
reply also invented a claim that the tool had not provided. The red-cross
tool result in the chat always has the exact message.

**Saving the tool fails.** If pip cannot install `tabstack==2.8.5` (no
internet access from the server, or pip installs turned off), the save fails.
Install the package into the Open WebUI image and save again.

**"Current Valves are incompatible with this code".** According to the Open
WebUI documentation, this appears when new code changes a setting so that the
value already saved no longer fits. Change that setting in the Valves dialog
first, then save the code again. This was not encountered in testing.

## Updating the tool

Open **Workspace > Tools**, click **Tabstack**, select all in the editor,
paste the new file, and click **Save**. Unlike creating the tool, saving an
update shows no confirmation dialog. Your saved valves, including the key, are
kept. If the pasted file is identical to the saved one, **Save** stays greyed
out. The description shown in the Tools list is not refreshed from the new
file when you edit an existing tool; change it by hand if you want it to
match.
