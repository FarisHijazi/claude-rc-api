# claude-rc-api (FarisHijazi fork)

Python client for Claude Code Remote Control and Claude Code on the web
(claude.ai/code). Fork of `ThatCrispyToast/claude-rc-api` (MIT); this is the
canonical home of the `claude_rc` package. cc-proxy consumes it as a pinned git
dependency (its `pyproject.toml`), so there is no second copy to keep in sync.

## Remotes

- `origin` = `git@github.com:FarisHijazi/claude-rc-api.git` (push here; `main`).
- `upstream` = `ThatCrispyToast/claude-rc-api`, **read-only**: fetch and merge
  `upstream/main` into `main`; never push there, never open upstream PRs/issues.
- Version: ours is `0.9.0` (upstream already used 0.4.0). Bump the minor for a
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
| `lifecycle.py` | create/unarchive/delete/share/tag, filtered/local-title discovery, cookie repo/branch pickers, guarded support/debug export, secret scan, teleport events |
| `environments.py` | environments (cookie private writes + bearer lineage), recursive config merging, archive, self-hosted pool reads, network profiles/rules/credentials |
| `account.py` | read-only web usage/settings/profile/subscription/connectors/skills; distinct from session worker settings and usage |
| `streaming.py` | paginated durable history, metadata/ephemeral session streams, bounded reconnect/backfill, live control correlation, account watch; opt-in CLI registration helpers |
| `prompts.py` | pending prompts, permission decisions, AskUserQuestion answers/annotations, explicit worker versus browser unanswered encodings, dialog/elicitation/hook responses; re-exported by `interaction.py` for compatibility |
| `commands.py` | cloud admission/direction gates, typed slash dispatch, browser model/policy discovery, captured MCP REST and staging contracts; CLI registration/dispatch helpers for integration |
| `triggers.py` | scheduled tasks (routines); CLI job updates must preserve unspecified nested configuration |
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
Current cloud qualification supersedes that historical missing-generator state:
`docs/devlog/claude_2026-10-29-core-permissions-summary.json` indexes bounded
`scripts/claude-rc-permission-smoke` and `scripts/claude-rc-question-smoke` proofs.
Cloud default includes a broad cliArg Bash allow; tighten only an owned fixture
via the existing permission-response owner before testing genuine prompts.
Native text/number kinds require the remote-worker startup environment flag
`CLAUDE_CODE_QUESTION_EXTENDED=1` in a disposable environment. The tested cloud
initialize hook registration returns `hooks_applied:false`; never count it as a
working callback. These evidence paths are deliberately plain, not includes.

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
  captured CLI 2.1.312 admission policy before enqueueing, with the live-qualified
  durable-session `mcp_set_servers` exception documented below. Raw `send_events` is
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
- `command_capabilities(session_id)` and `claude-rc dispatch SESSION --capabilities`
  separate the captured 22-name Code web menu, session-advertised worker commands
  and API-only routes. Availability means dispatchable, not live-qualified.
  `/status` and `/autocompact` require explicit host handlers; advertised harness
  names never bypass that gate. Design/slides are not in the captured Code menu.
- Final command/queue acceptance is indexed by
  `docs/devlog/claude_2026-10-29-final-commands-summary.json` (plain path, not an
  include). `scripts/claude-rc-command-smoke --qualify-controls --capture-web-catalog`
  needs `--output PATH` and optional playwright. It owns and closes one dev CDP
  tab, creates a disposable environment/session, and verifies cleanup. Legacy
  cancel/interrupt effects are qualified; send-now/v1 receipts/task messaging are
  not. Cancelled events can later have transport processing timestamps without
  model execution. Collection gates blocking prompts on its own message receipt,
  just like results, so an earlier queued turn's question cannot end collection.
- Queue/task follow-up: `docs/devlog/claude_2026-10-29-protocol-queue-summary.json`
  and `scripts/claude-rc-queue-smoke` qualify next-before-later execution at a real
  question boundary, exact shell-task output/stop, and a typed cloud rejection of
  `send_task_message`. Send-now/v1 receipts remain unadvertised; in-flight control
  cancellation has no accepted long-lived fixture. Result `user_message_uuid` /
  `user_message_uuids`, when present, must match the collected message even after
  its processing receipt; a receipt must not override explicit result ownership.
  Legacy results without these fields retain receipt/time gates.
  Independent verification is indexed by
  `docs/devlog/claude_2026-10-29-protocol-queue-independent.json`: 791 tests pass,
  28 correlation cases fail against the preserved base and pass with the fix.
  One fresh live fixture repeats priority/file/task/rejection/cleanup effects.
  Retained live traces do not prove the receipt-first race; its proof is offline.
  These evidence paths are deliberately plain, not includes.
- Group05 live evidence, per-capability limitations and integration contracts
  are in `docs/devlog/claude_2026-10-28-group05-live.json` (deliberately a plain
  path, not an include). Those historical MCP gaps are superseded by the scoped
  qualification below; the correlator/parser remain owned by streaming, not a new poller here.
- Independent controls verification and its remaining acceptance gaps are indexed
  in `docs/devlog/claude_2026-10-28-group05-independent-summary.json` (plain path;
  context/usage/MCP evidence stores shapes and counts, not configuration payloads).
  `ask_for_tools` must be a sequence of names, never a scalar string: otherwise
  it silently expands into one-character tool declarations.
- MCP cloud qualification: `scripts/claude-rc-mcp-smoke --output PATH` uses SDK
  FastMCP 1.30.0 in one disposable cloud child. `set_worker_mcp_servers(...,
  confirm=True)` replaces only that session's dynamic set via the existing
  durable control writer; replacement can remove inherited worker servers.
  It works on Code cloud workers despite the separate native cloud-host
  admission switch rejecting `mcp_set_servers`; do not conflate those paths.
  Status, disable/re-enable/reconnect replies, actual model-tool permission-rule
  reuse, staged binary bytes, stale-CAS protection and expiry's unchanged call
  ledger are qualified. Synced-file staging does not require Files API mounts.
  Empty replacement disconnects the child; exact PID absence, session GET 404
  and environment list exclusion are checked. Worker override/askForTools
  acknowledgements do not prove enforcement: bypass transition requires a
  missing startup flag, and forced-ask is Chrome-safe-tool-specific. Stdio REST
  PUT returns 400; remote config/approval effects remain unqualified. Evidence:
  `docs/devlog/claude_2026-10-29-protocol-mcp-summary.json` and its linked runs,
  deliberately plain paths. Recovery independently passed 781 tests, including
  an actual local SDK child, six cleanup-path cases and isolated headless browser
  tests. Read the child call ledger with worker `read_file`, not the synced-file
  snapshot: child-written files may not yet exist in that snapshot. Cleanup still
  attempts environment archive after a session-cleanup failure and fails closed.
  A separate verifier reran all 781 tests and the complete cloud fixture:
  `docs/devlog/claude_2026-10-29-protocol-mcp-independent-live.json` and
  `docs/devlog/claude_2026-10-29-protocol-mcp-independent-summary.json` (plain paths).
  Fresh-client reads confirm all eight recorded sessions return 404 and their
  environments are excluded. Exact session routing and unchanged rejection gates
  passed offline; this is not a live cross-session isolation proof. The admission
  allowlist is not an authorization boundary: generic control/raw-event callers
  retain responsibility for their chosen session and explicit consent.

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
- `recover_browser_auth()` explicitly rereads the original cookie source, validates
  the pinned account and rolls back on failure; it never replays an operation.
  `scripts/claude-rc-auth recover|enroll` provides on-demand recovery or an owned
  PKCE tab (optional Playwright), never automatic consent or shared refresh.
  Live OAuth inspection still reached fresh login without a code/grant; owned
  rotation remains offline-only. Evidence: `docs/devlog/claude_2026-10-29-final-auth-live.json`
  (deliberately plain path, not an include). Browser auth-page cookie changes are
  not a promise of natural renewal; all 190 compared shared credential files
  were unchanged. PKCE single-use is checked again inside the enrollment lock.
- Independent auth qualification: `docs/devlog/claude_2026-10-29-final-auth-independent.json`
  (deliberately plain path, not an include) reruns 749 offline/headless tests,
  real invalid-memory-cookie recovery and two completed turns on the same session,
  fresh-client paged readback and exact deletion (GET 404). No auth defect was
  confirmed. Owned server rotation remains blocked without an independently
  enrolled grant; simulated refresh tests do not establish natural expiry.
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
TTS/dictation and inline images worked live. Cookie Code attachment uploads now
use `/api/{org}/upload?store_as_is=true` (multipart `file`), and download original
bytes at `/api/organizations/{org}/files/{uuid}/contents`. Actual Code composer
ZIP and client binary uploads were consumed by workers with matching Bash hashes:
`docs/devlog/claude_2026-10-29-core-upload-browser.json` and
`docs/devlog/claude_2026-10-29-core-upload-live.json` (plain paths, not includes).
`scripts/claude-rc-upload-smoke --evidence <path>` repeats the client acceptance.
No standalone org attachment deletion contract is captured: both test sessions
were deleted (GET 404), but blobs remain readable. Do not claim session deletion
cleans up uploads. Bearer OAuth upload and Files API routes remain distinct;
their prior cookie 401/404 results do not apply to the qualified org route.
Independent reruns and remaining gaps are recorded in
`docs/devlog/group07-independent-2026-10-28.json`. Dictation rejects empty or
whitespace-only final transcripts rather than reporting a successful empty result.

## Integrated verification

All eight feature branches are merged. `docs/web-api.md` is the current
per-capability matrix and endpoint map; branch devlogs retain historical states.
`docs/devlog/claude_2026-10-28-integration-qualified.json` records the fresh
integrated cookie-auth/worker/CLI/panel-HTTP run, including incomplete checks.
These are deliberately plain paths, not `@` includes.

- `commands`, `account`, `streaming`, and `media_cli` registration helpers are
  wired by `cli.build_parser`; credential flags use its one client factory.
- `InteractionMixin(PromptsMixin, CommandsMixin)` preserves prompt exports and
  delegates streams to their owner. Live control correlation must enforce the
  same admission gate as durable controls before opening HTTP.
- `content_sha256` is an opaque server CAS token, not necessarily the digest of
  decoded content. Default namespaces and a fresh custom environment explicitly
  using `/mnt/user-data/working` are qualified; arbitrary custom cwd mappings are
  not. Never derive the token locally or retry a conflict as an overwrite.
- Routine enqueue/terminal success is not execution: require the actual expected
  assistant marker. Core permission allow/deny/exact session-rule reuse, native
  text/number/follow-up and cookie Code attachments are live-qualified. Rendered
  permission/native-form acceptance, owned OAuth enrollment/refresh, standalone
  upload deletion and full live-browser parity remain incomplete.
- `tests/integration_adapters_test.py` covers merged boundaries;
  `tests/panel_browser_test.py` optionally runs isolated headless Chrome via
  `uv run --extra test --extra web --extra cli --with playwright pytest -q`.
  Tests only collect `tests/`, never the preserved feature worktrees.
- `history_page` retains cursors; the panel pages ascending to EOF before attaching
  its stream. Never restore the newest-200-only transcript. Single-choice Other
  and radio inputs are mutually exclusive; skipped numeric fields bypass validation.
- `scripts/claude-rc-panel-smoke` drives isolated headless Chrome against the real
  service, records exact owned resources and verifies deletion. Its latest evidence
  is `docs/devlog/claude_2026-10-28-panel-live-cas.json`; long real history is in
  `docs/devlog/claude_2026-10-28-panel-long-live-rerun.json`. These are plain paths.
- Panel and collection consumers use the existing initialize control on a replacement
  handshake. Generic read-only streams do not acquire a new write path.
- 0.8.0 management screens delegate to existing environment/trigger/file owners.
  Environment panel edits accept metadata only; never round-trip redacted variables.
  Routines are created disabled; network inspection is read-only. Binary files use
  validated base64 through the same CAS writer. Text decoding preserves BOM and
  refuses carriage-return normalization. `scripts/claude-rc-management-smoke` and
  `docs/devlog/claude_2026-10-29-management-live.json` qualify this subset live.
  `docs/devlog/claude_2026-10-29-auth-source-live.json` qualifies isolated env/state
  source recovery, not natural expiry or Chrome DB extraction. The current full
  gap plan is `docs/devlog/claude_2026-10-29-completeness-followup.json`; all paths
  here are deliberately plain, not force-loaded includes.
- Routine diagnosis: `docs/devlog/claude_2026-10-28-routine-diagnosis.json` proves
  Haiku was applied but the routine returned a weekly-limit message with
  `subtype: success, is_error: true`. Respect the error flag; do not equate turn
  completion with successful execution. All remaining review gaps and next acceptance
  steps are in `docs/devlog/claude_2026-10-28-completeness-followup.json` (plain path).

Canonical core integration: `docs/devlog/claude_2026-10-29-core-integrated-live.json` records
732 offline/headless tests and fresh upload/permission/native-question effects
and exact cleanup. These are plain paths, not includes. Blob deletion and full
extended panel parity remain unsupported or incomplete; the gap plan is updated.

0.9.0 final integration: `docs/devlog/claude_2026-10-29-final-integration.json`
indexes 761 offline/headless tests and fresh auth/commands live harnesses with
same-session continuation, fresh-client history, two-model inference, exact
queue cancellation/legacy interrupt effects and exact resource cleanup. These
paths are deliberately plain, not includes. `docs/web-api.md` separates requested
web API core, extended web API gaps and optional local CLI/panel presentation;
unqualified web operations must never be relabeled as presentation-only.
Hatch owns the dynamic version in `claude_rc/_version.py`; use
`uv run --with hatch hatch version minor` for a new surface. No new transport,
shared grant refresh, persistent service or deployment was added.
