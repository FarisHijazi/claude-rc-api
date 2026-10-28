# claude-rc-api

Unofficial Python client for the Claude Code Remote Control web service,
reverse-engineered from the Claude Code CLI and exercised against the live API.

It lets your own programs do what `claude.ai/code` and the Claude mobile app do:
list, observe, and steer Claude Code sessions running on a machine. It talks to
the same `/v1/code/sessions` endpoints and claude.ai OAuth token the web app
uses.

> ⚠️ **Unofficial and unsupported.** These are private endpoints
> (`api.anthropic.com`), reconstructed from the CLI binary. They can change or
> break at any time. Use your own account, at your own risk. Full protocol
> writeup in [`API_REFERENCE.md`](./API_REFERENCE.md).

## This fork (0.6.0): Claude Code on the web

`FarisHijazi/claude-rc-api` adds the rest of claude.ai/code on top of
upstream 0.4.0: create/unarchive/delete/share/tag cloud sessions, environments
(list/get/create/edit), scheduled tasks, every control request (model,
permission mode, effort, interrupt), permission and AskUserQuestion answers,
slash commands, the account watch feed, attachments, claude.ai TTS, and
claude.ai `sessionKey` cookie auth read from a signed-in Chrome
(automatically probes `http://127.0.0.1:9222`; `CLAUDE_AI_CDP_URL` overrides,
empty disables). Chrome owns these cookies and keeps them fresh; the client
re-reads on auth failure. Shared CLI OAuth credentials are never refreshed;
use a separate client-owned OAuth file for refreshable bearer auth. Shapes and live status:
[`docs/web-api.md`](./docs/web-api.md).

```bash
uv add "claude-rc-api[web,cli] @ git+https://github.com/FarisHijazi/claude-rc-api"
CLAUDE_AI_CDP_URL=http://127.0.0.1:9222 claude-rc whoami
claude-rc envs list
claude-rc create --model claude-haiku-5-5 "reply with PONG"
claude-rc pending <session_id>
claude-rc answer <session_id> question --select "Blue"   # or allow | always | deny
```

---

## How it works

Remote Control relays, it doesn't run cloud compute. Your local
`claude remote-control` process runs Claude and holds an outbound connection to
Anthropic; the web app and mobile app act as a controller that posts messages
and reads the session's event stream. This library plays that controller role.

```
your script ──► POST /v1/code/sessions/{id}/events    (send a message)
            ◄── GET  /v1/code/sessions/{id}/events/stream  (SSE: read the reply)
   (claude.ai OAuth Bearer + x-organization-uuid, from ~/.claude)
```

It also ships a `ManagedAgentsClient` for the sibling public Sessions API
(`/v1/sessions`, `x-api-key`) that runs agents in Anthropic's cloud.

## Install

As a library / CLI, straight from the repo:

```bash
uv add "claude-rc-api @ git+https://github.com/ThatCrispyToast/claude-rc-api"   # into a uv project
pip install "git+https://github.com/ThatCrispyToast/claude-rc-api"              # into any venv
uvx --from "git+https://github.com/ThatCrispyToast/claude-rc-api[cli]" claude-rc list   # run the CLI, zero install
```

For development in a checkout, use [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra cli                 # runtime + pretty CLI
uv sync --extra cli --extra test    # + pytest
```

## Prerequisites

1. Sign into claude.ai in Chrome (cookie mode), or use an existing Claude Code
   subscription login (bearer mode). API-key logins cannot use Remote Control.
2. Have a session to drive: run `claude remote-control` (or
   `claude --remote-control`) in some project. That registers a session you can
   then control from here.

## Authentication

Credentials resolve through `credentials.resolve_auth`: explicit arguments first,
then env/CDP/state/Chrome cookies, an isolated client-owned file at
`$XDG_CONFIG_HOME/claude-rc/credentials.json` (default `~/.config/claude-rc/`),
then bearer env overrides and read-only cswap/CLI/Keychain sources.
`credentials_path=` explicitly selects a file even when a browser is available.
Cookie clients pin `/api/account` before use, re-read their original source once
on an auth failure, and verify the same account before replaying the operation;
the selected organization stays fixed. Known policy denials and Cloudflare
challenges are not treated as expired cookies. Unidentified legacy bearer/slot
changes fail closed unless an unchanged nonempty refresh grant proves lineage;
use the default cookie path for continuously re-readable browser authentication.

Use `RemoteControlClient(direct=True)` to ignore proxy/CA environment variables
for that client's HTTP transports without changing the process environment.
Explicit PKCE enrollment helpers live in `claude_rc.login`; see
`docs/devlog/claude_2026-10-28-group01-auth-foundation.md` for their signatures
and the distinction between offline coverage and live verification.

## CLI

```bash
claude-rc whoami                 # login / org / token status
claude-rc list                   # your Remote Control sessions
claude-rc get   <cse_id>         # session details (JSON)
claude-rc events <cse_id>        # recent history
claude-rc watch <cse_id>         # stream live (read-only)
claude-rc info  <cse_id>         # summary, context, plan usage, git changes
claude-rc ask   <cse_id> "what are you stuck on?"  # side question
claude-rc send  <cse_id> "run the tests"   # send a message, print the reply
claude-rc repl  <cse_id>         # interactive chat
claude-rc rename <cse_id> "new title"
claude-rc web                    # browser control panel (all sessions)
```

`ask` is claude.ai's "btw": the worker answers from its conversation without
interrupting a running turn or adding to the transcript (it costs a model call).

## Web UI

A dependency-free browser control panel for your Remote Control sessions: list
them (with each one's effort, context use, and status summary), watch the live
event stream, send messages, ask side questions, allow or deny tool
permissions, and steer (interrupt / model / permission mode / effort / archive).

```bash
claude-rc web                    # serves http://127.0.0.1:8765 and opens it
claude-rc web --port 9000 --no-open
```

It's a standard-library `http.server` (no new dependencies) that proxies the
`RemoteControlClient`. It speaks to the private API with *your* OAuth token and
does no auth of its own, so it binds to `127.0.0.1` by default. Pass
`--host 0.0.0.0` only if you accept that anyone who can reach it can steer your
sessions. You can also run it programmatically:

```python
from claude_rc import serve_webui
serve_webui(host="127.0.0.1", port=8765, open_browser=True)
```

## Library

```python
from claude_rc import RemoteControlClient

rc = RemoteControlClient()                 # signed-in Chrome first; no copied cookie

# discover sessions
for s in rc.sessions():
    meta = s.get("external_metadata") or {}
    print(s["id"], s["worker_status"], s["title"], meta.get("effort_level"))

sid = rc.sessions()[0]["id"]

# ask and wait for the answer (sends, streams from the pre-send cursor, stops on `result`)
for ev in rc.send_and_collect(sid, "summarize the current diff", print_stream=True):
    pass

# observe read-only
for ev in rc.stream_events(sid):
    if ev.role == "assistant":
        print(ev.text())
    if ev.type == "result":
        break

# steer
rc.interrupt(sid)
rc.set_model(sid, "claude-opus-5-5")
rc.set_permission_mode(sid, "acceptEdits")
rc.set_effort(sid, "high", wait=6.0)        # low|medium|high|xhigh|max, or None = auto
rc.rename_session(sid, "release prep")

# answer the worker's prompts
for ev in rc.stream_events(sid):
    if ev.is_blocking_control and ev.control_subtype == "can_use_tool":
        req = ev.payload["request"]
        rc.respond_permission(sid, ev.request_id, allow=True,
                              tool_input=req["input"], tool_use_id=req.get("tool_use_id"))
        break

# ask the worker — each waits for its verdict (ControlRejected / ControlTimeout)
rc.side_question(sid, "what are you stuck on?")   # "btw": no interruption, not in the transcript
rc.get_context_usage(sid)                   # {totalTokens, maxTokens, percentage, categories…}
rc.get_usage(sid)                           # plan limits: rate_limits.limits[{kind, percent, resets_at}]
rc.get_workspace_diff(sid)                  # {stats: {filesCount, linesAdded, linesRemoved}, …} | None
rc.stop_task(sid, "b5rkopq8x")              # task_id from a system/task_started event

# withdraw a message before the worker starts on it
import uuid
mid = str(uuid.uuid4())
rc.send_message(sid, "actually, run the linter too", message_uuid=mid)
rc.cancel_queued_message(sid, mid)          # True while it was still queued

# any other control subtype (see API_REFERENCE.md §3.2)
rc.request_control(sid, "mcp_status")
```

The session object already carries the cheap answers, no round trip needed:
`external_metadata` has `effort_level`, `context_usage` (`used_tokens` /
`max_tokens`), `post_turn_summary` (a one-line `status_detail`, plus
`needs_action` when it waits on you) and the worker's `container_cc_version`.

Cloud (Managed Agents) mode:

```python
from claude_rc import ManagedAgentsClient
ma = ManagedAgentsClient(api_key="sk-ant-...")
agent = ma.create_agent(name="A", model="claude-opus-5-5",
                        tools=[{"type": "agent_toolset_20260401"}])
env = ma.create_environment(name="e", config={"type": "cloud",
                            "networking": {"type": "unrestricted"}})
sess = ma.create_session(agent=agent["id"], environment_id=env["id"])
ma.send_message(sess["id"], "hello")
for ev in ma.stream_events(sess["id"]):
    if ev.type == "agent.message": print(ev.text())
    if ev.type == "session.status_idle": break
```

See [`examples/`](./examples).

## Events

`Event` normalizes both wire formats. For Remote Control the important payload
types are `user`, `assistant`, `result` (turn complete), `control_request` /
`control_response` (steering, prompts, and their answers), and `system` (`init`,
`status`, task events). Helpers:

```python
ev.type          # payload type
ev.role          # "user" | "assistant" | None
ev.text()        # concatenated text blocks (handles str or block-list content)
ev.tool_uses()   # tool_use content blocks
ev.sequence_num  # RC ordering/resume cursor (int)
ev.source        # "client" (a controller) | "worker" (Claude Code)
ev.is_turn_end   # a `result` (RC) or `session.status_idle` (managed agents)
ev.is_blocking_control  # a worker prompt waiting on you (never a controller's own request)
ev.request_id    # the id a control_request carries / a control_response answers
ev.worker_state  # system/init|status → {model, effort, permission_mode, claude_code_version}
ev.attestation   # "UNSPECIFIED" | "VERIFIED" | "ABSENT" … (Trusted Devices)
```

## Safety

- `list`/`get`/`events`/`watch` and the `RemoteControlClient` read methods are
  read-only. `send`/`repl`/`send_message`/`interrupt` and the other steering
  calls inject into a live session, so only use them on sessions you own. The
  worker queries (`ask`, `info`, `side_question`, `get_*`) leave a
  `control_request` / `control_response` pair in the event log, as claude.ai's
  own attach does; a side question also costs a model call.
- Trusted-device enrollment is **opt-in**: `allow_device_enrollment=True` only
  permits it for a client-owned credential source. Default requests never enroll
  or email the owner. Existing device tokens can still be re-read and retried.
  The upstream refusal cooldowns remain; this library does not sign events, so
  `served_call_unsigned` cannot be repaired by enrolling again. See
  `API_REFERENCE.md` §3.4.
- Shared CLI/Keychain/cswap/proxy credentials are never refreshed, including an
  explicit path or symlink into a known shared store. Client-owned refreshes
  serialize the complete exchange and compare-and-swap write using a sidecar
  OS lock; token files are atomically replaced with mode 0600. A new client-owned
  enrollment is always explicit and cannot overwrite an existing file.
- Credential reprs and exception messages do not include secret values or raw
  response bodies. `APIError.body` is available for deliberate inspection and
  may contain sensitive data; do not log it.

## Used by

[`claude-remote-bridge`](https://github.com/ThatCrispyToast/g2-claude-remote)
(the `server/` half of the Claude Remote glasses app) depends on this package
straight from git and wraps `RemoteControlClient` in a JSON + SSE HTTP bridge. It
imports `client`, `credentials`, and `events` (prompt answers, side questions,
the session metadata) and resolves this repo's default branch on each fresh `uv`
install — so keep those three backwards-compatible.

## Project layout

```
claude_rc/
  credentials.py   # OAuth load + refresh (file or macOS Keychain), org uuid
  client.py        # RemoteControlClient (mode A) + ManagedAgentsClient (mode B)
  events.py        # Event model + builders for both wire formats
  sse.py           # dependency-free SSE parser
  cli.py           # `claude-rc` command line
  webui.py         # `claude-rc web` — stdlib http.server control panel
  static/          # the web UI single-page app
API_REFERENCE.md   # full reverse-engineered protocol reference
examples/          # runnable examples
tests/             # offline unit tests (pytest)
```

## Tests

```bash
uv run --extra test pytest -q
```

All tests run offline (no network, no credentials required).

## Routine management (0.6.0)

```bash
claude-rc triggers create test-only --env <environment-id> --at 2030-01-01T00:00:00Z --disabled --prompt "Reply ROUTINE_OK"
claude-rc triggers get <trigger-id>
claude-rc triggers run <trigger-id> --prompt "Reply ROUTINE_OK"
claude-rc triggers runs <trigger-id>
claude-rc triggers fork <trigger-id>
claude-rc triggers cancel-run <trigger-id> --run-session <owned-session-id>
claude-rc triggers delete <trigger-id>
```

List supports `--limit`, `--cursor`, `--tag` and `--exclude-tag`. Responses
from create/get/fork wrap the routine in `trigger`; `trigger_item()` handles
that envelope. `fire_routine()` is deprecated: its legacy route has not been
verified. Use authenticated `run_trigger()` instead. Token management methods
return secrets and are deliberately not exposed as commands that print JSON.

The panel now uses its own client's auth status (including cookie mode),
metadata-preserving streams, server-revalidated permission decisions including
Always allow, question notes/previews, and a sticky first-load state. These
changes do not claim full parity with browser-only commands or newer question
schemas. The current evidence and remaining integration work are in
`docs/web-api.md`.
