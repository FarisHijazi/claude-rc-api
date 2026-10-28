# claude-rc-api (FarisHijazi fork)

Python client for Claude Code Remote Control and Claude Code on the web
(claude.ai/code). Fork of `ThatCrispyToast/claude-rc-api` (MIT); this is the
canonical home of the `claude_rc` package. cc-proxy consumes it as a pinned git
dependency (its `pyproject.toml`), so there is no second copy to keep in sync.

## Remotes

- `origin` = `git@github.com:FarisHijazi/claude-rc-api.git` (push here; `main`).
- `upstream` = `ThatCrispyToast/claude-rc-api`, **read-only**: fetch and merge
  `upstream/main` into `main`; never push there, never open upstream PRs/issues.
- Version: ours is `0.6.0`+ (upstream already used 0.4.0). Bump the minor for a
  new surface.

## Layout

`RemoteControlClient` (`claude_rc/client.py`) is the narrow waist: `_send` is
the only place HTTP happens (headers, 401 renewal, cookie re-read, trusted-device
renewal, retries, `APIError`); `_request` is `_send` + parse. Each feature area
is a mixin the client inherits, one module each — add new methods to the
module that owns the area, or a new mixin module plus one line in the
`RemoteControlClient` bases and `__init__.py` exports.

| Module | Owns |
|---|---|
| `client.py` | transport, headers (`build_headers`), auth hooks, session list/get/rename/archive, send/stream events, control requests (model, mode, effort, interrupt), `ManagedAgentsClient` |
| `credentials.py` | OAuth credentials (file / macOS Keychain), refresh with CAS save, trusted devices, `resolve_auth` (the auth chain), cswap slots |
| `webauth.py` | claude.ai cookie jar: env, state file, Chrome cookie DB, Chrome CDP; Cloudflare detection |
| `login.py` | explicit isolated PKCE helpers; never called by auth discovery; live grant exchange still unverified |
| `lifecycle.py` | create/unarchive/delete/share/tag/mark-read, teleport events |
| `lifecycle.py` | create/unarchive/delete/share/tag, filtered/local-title discovery, cookie repo/branch pickers, guarded support/debug export, secret scan, teleport events |
| `environments.py` | environments (public + private org routes), create/update, self-hosted pools |
| `environments.py` | environments (cookie private writes + bearer lineage), recursive config merging, archive, self-hosted pool reads, network profiles/rules/credentials |
| `account.py` | read-only web usage/settings/profile/subscription/connectors/skills; distinct from session worker settings and usage |
| `triggers.py` | scheduled tasks (routines) |
| `interaction.py` | pending prompts, permission + AskUserQuestion answers, slash commands, generic controls; stream methods delegate to `streaming.py` |
| `streaming.py` | paginated durable history, metadata/ephemeral session streams, bounded reconnect/backfill, live control correlation, account watch; opt-in CLI registration helpers |
| `interaction.py` | slash commands, generic controls, `stream_session` (keeps meta frames), account `watch` feed |
| `prompts.py` | pending prompts, permission decisions, AskUserQuestion answers/annotations, explicit worker versus browser unanswered encodings, dialog/elicitation/hook responses; re-exported by `interaction.py` for compatibility |
| `commands.py` | cloud admission/direction gates, typed slash dispatch, browser model/policy discovery, captured MCP REST and staging contracts; CLI registration/dispatch helpers for integration |
| `interaction.py` | compatibility delegates for slash dispatch, typed session controls, pending prompts, permission + AskUserQuestion answers, `stream_session` (keeps meta frames), account `watch` feed |
| `triggers.py` | scheduled tasks (routines); CLI job updates must preserve unspecified nested configuration |
| `interaction.py` | pending prompts, permission + AskUserQuestion answers, slash commands, generic controls, `stream_session` (keeps meta frames), account `watch` feed |
| `files.py` | attachments / Files API |
| `interaction.py` | pending prompts, permission + AskUserQuestion answers, slash commands, generic controls, `stream_session` (keeps meta frames), account `watch` feed |
| `files.py` | chat attachments, inline images, Files API staging, controller file reads and CAS sync writes |
| `tts.py` | claude.ai text-to-speech websocket + Ogg muxer |
| `dictation.py` | generated-audio transcription over the captured cookie websocket; no microphone; pre-audio redial only |
| `media_cli.py` | optional file/media registrations consumed by the CLI owner; uses its client factory |
| `events.py`, `sse.py` | wire event builders/parsers, SSE parser |
| `cli.py` | `claude-rc` subcommands (one `cmd_*` per command, registered in `main`) |
| `webui.py`, `static/` | `claude-rc web` control panel |

Tests: `tests/test_<area>.py` per module (`test_client.py` is upstream's; keep
its new tests passing when merging upstream). Offline only, via
`httpx.MockTransport`. `tests/conftest.py` strips auth env and gives each test
an empty `$HOME`. Docs: `API_REFERENCE.md` (upstream's protocol write-up),
`docs/web-api.md` (claude.ai/code shapes and what is live-verified).
Prompt integration signatures and sanitized live evidence are in
`docs/devlog/claude_2026-10-28-group04-prompts.json` (deliberately a plain path,
not an `@` include). `pending_requests(initialize=True)` reconciles complete
paged history with the worker snapshot; its default remains read-only.
Empty worker unanswered annotations are not browser dismissal sentinels; pass
`unanswered_encoding="browser"` or the convenience helpers' `encoding="browser"`
when that distinct browser behavior is intended. Answer validation rejects
multiple Other entries for the same question, even through different key aliases,
before posting (multi-select permits selected labels plus one Other only).
Independent refutation, fix reproduction and live worker-result assertions are in
`docs/devlog/claude_2026-10-28-group04-independent-verification.json` (plain path).

Lifecycle integration contracts and per-capability live evidence are in
`docs/devlog/claude_2026-10-28-group02-lifecycle.json` (deliberately a plain
path, not an `@` include). Delete uses the code-session route with `{}` only;
share creation, support sharing and debug export require `confirm=True`.
Independent lifecycle verification is recorded in
`docs/devlog/claude_2026-10-28-group02-independent-verification.json` (also a
plain path). Unarchive state alone is not worker readiness: send a fresh
message and require a newer worker result when testing usability.

## Test

```bash
uv run --extra test --extra web --extra cli pytest -q
```

On dev, uv needs the proxy CA plus the system bundle:
`cat /etc/ssl/certs/ca-certificates.crt "$SSL_CERT_FILE" > /tmp/ca.pem` and
`SSL_CERT_FILE=/tmp/ca.pem uv …`.

## Control contracts

- `control_profile` defaults to `cloud` for cookies and `bridge` for bearer auth;
  set it explicitly when the session worker differs. `_send_control` checks the
  captured CLI 2.1.312 admission policy before enqueueing. Raw `send_events` is
  still the protocol escape hatch, not a promise that a worker accepts a control.
- `set_model(..., wait=seconds)` and `set_permission_mode(..., wait=seconds)`
  return worker verdicts; without `wait` they retain the legacy enqueue ack.
- `run_command` now dispatches typed controls and advertised worker commands.
  UI-only names always require `local_handlers`, even with `strict=False`;
  handlers take the argument string. Never fall through to ordinary user text.
- Integration helpers are `commands.register_command_cli(subparsers)` and
  `commands.dispatch_command_cli(client, args) -> dict`. The latter returns
  `via=local|control|worker`; the CLI/panel owner must render its result rather
  than print a misleading "sent" receipt. `command_table(commands)` supplies
  routing/availability, not a claim that a host renderer already exists.
- Group05 live evidence, per-capability limitations and integration contracts
  are in `docs/devlog/claude_2026-10-28-group05-live.json` (deliberately a plain
  path, not an include). Ephemeral MCP replies remain unverified live; the
  correlator/parser are owned by the streaming module, not a new poller here.
- Independent controls verification and its remaining acceptance gaps are indexed
  in `docs/devlog/claude_2026-10-28-group05-independent-summary.json` (plain path;
  context/usage/MCP evidence stores shapes and counts, not configuration payloads).
  `ask_for_tools` must be a sequence of names, never a scalar string: otherwise
  it silently expands into one-character tool declarations.

## Auth rules

- Claude OAuth refresh tokens ROTATE and are single-use. Never refresh a
  credential another consumer also holds (the CLI's `~/.claude`, cswap slots,
  the claude-accounts proxy): it logs that consumer out. cswap slots are read
  access-token-only; cswap owns their refresh.
- Default on dev: the claude.ai `sessionKey` cookie read live from the
  signed-in Chrome over CDP (automatically probes `http://127.0.0.1:9222`;
  override `CLAUDE_AI_CDP_URL`, empty disables). Chrome
  owns those cookies: read, never write; re-read once on 401/403.
- The chain is `credentials.resolve_auth`; change auth order only there.
  Cookie source order: explicit env, live CDP, state file, Chrome DB. The
  shared CLI file / Keychain may be re-read, but `client._refresh` refuses to
  refresh either. A client-owned OAuth login must use its own file via
  `credentials_path`, never the CLI's default path.
- Cookie recovery pins `/api/account` and preserves the selected org. Re-read
  only the original cookie source; policy/Cloudflare denials are not expiry.
  Client-owned refresh exchanges use an OS sidecar lock around read/exchange/CAS.
  Shared stores, including resolved symlinks, remain read-only. The cswap accessor
  uses only cswap's storage reads, never the proxy reader (which can refresh a
  backup grant). Expired slots fail closed until their owner renews them.
  Initial account pinning can reread stale cookies once; the observed account
  403 `permission_error` / `Invalid authorization` is an auth denial, not policy.
- `direct=True` ignores proxy/CA env only for this client's HTTP transports;
  never rewrite global network settings. Multipart retries require bytes/str.
  New device enrollment requires `allow_device_enrollment=True` and an owned
  store; default requests never enroll or email anyone.
- Contracts, live 401 recovery evidence and explicit remaining verification:
  `docs/devlog/claude_2026-10-28-group01-auth-foundation.md` (deliberately a plain
  path, not an `@` include).
- Never print, log, commit or put in fixtures any token or cookie value
  (`repr`s show names only).

## Live-test rules

- Read-only checks first: `claude-rc whoami`, `claude-rc list --limit 3`,
  `claude-rc envs list`.
- Writes: few throwaway sessions, tiny prompts, `--model claude-haiku-5-5`;
  archive (and delete when testing delete) each one afterwards.
  `scripts/claude-rc-smoke` runs the full create → answer → archive → delete
  loop.
- Never touch sessions you did not create, except read-only listing.
- Streaming acceptance: `uv run --extra web python scripts/claude-rc-stream-smoke --direct`.
  History pages accept 1–500 events. `stream_control` filters locally by default:
  the live server's `control_only=1` omits ephemeral `mcp_status liveReply` responses.
  Group03 contracts/evidence: `docs/devlog/claude_2026-10-28-streaming-recovery.json`
  (deliberately a plain path, not an `@` include). Independent rerun/fix evidence:
  `docs/devlog/claude_2026-10-28-streaming-independent-verification.json`.
  Collection requires its own processing receipt before accepting a result;
  callback HTTP failures propagate (never reconnect/reissue a possibly applied
  write). Stream/backoff deadlines do not override synchronous callback/history
  transport timeouts.
- Record what ran live in `docs/web-api.md` ("LIVE CHECK"). Devlogs go in
  `docs/devlog/claude_<date>-<desc>.md`.
- Group06 evidence/contracts: `docs/devlog/claude_2026-10-28-group06-contracts.json`
  and `docs/devlog/claude_2026-10-28-group06-live-evidence.json` (plain paths,
  not includes). Environment archive removes it from lists but GET may still
  return `state: active`; permanent environment deletion is unsupported.
  Network profile/secret writes are captured-contract/offline-only, not live verified.
  Independent rerun: `docs/devlog/claude_2026-10-28-group06-independent-evidence.json`
  correlates Bash tool results to tool-use IDs and requires turn completion; POST
  acknowledgement or assistant text alone is not environment execution proof.

Files/media integration contracts and redacted live evidence are in
`docs/devlog/claude_2026-10-28-group07-files-media.md` and its linked JSON.
These are deliberately plain paths, not `@` includes. Controller GET uses
`/mnt/user-data/working/<file>`; synced PUT uses `/working/<file>`. CAS must
use the GET response's `content_sha256`, not the attachment digest. Cookie
TTS/dictation and inline images worked live; OAuth attachment upload returned
401 and Files API returned 404, so neither is advertised as cookie-supported.
Independent reruns and remaining gaps are recorded in
`docs/devlog/group07-independent-2026-10-28.json`. Dictation rejects empty or
whitespace-only final transcripts rather than reporting a successful empty result.
