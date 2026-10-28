# Web controller: contracts and evidence (0.6.0)

This is the canonical `claude_rc` implementation. All eight feature branches
are merged. It is **not full browser parity**: a captured request builder,
queue acknowledgement, fixture test, or terminal `success` alone is not proof
that a worker executed the requested operation.

## Evidence and status vocabulary

Each capability appears once in the matrix below. **Implemented** means a
concrete API/adapter exists; the verification column is independent:

- **Live**: accepted behavior asserted against the service on owned resources.
- **Fault-injected live**: the fault is synthetic, subsequent traffic is real.
- **Offline**: deterministic fixtures/contracts only; no live qualification.
- **Rejected/blocked**: actual service denial, missing fixture/grant, or an
  outward-facing operation deliberately not submitted.
- **Unsupported**: no captured contract or outside the controller's authority.

Evidence files are deliberately plain paths, not force-loaded includes:

| Key | Evidence in `docs/devlog/` |
|---|---|
| I | `claude_2026-10-28-integration-qualified.json` (latest integrated run), `claude_2026-10-28-integration-live.json`, `claude_2026-10-28-integration-live-rerun.json`, `claude_2026-10-28-integration-live-final.json` (earlier runs retained, including verifier failures) |
| C | `claude_2026-10-28-integration-cas.json` (independent default-workspace CAS and cleanup) |
| A | `claude_2026-10-28-group01-auth-foundation.md` (independent auth fixes and 192 unchanged shared credential files) |
| L | `claude_2026-10-28-group02-independent-verification.json` |
| S | `claude_2026-10-28-streaming-independent-verification.json` |
| Q | `claude_2026-10-28-group04-independent-verification.json` |
| W | `claude_2026-10-28-group05-independent-summary.json` and its linked task/rerun evidence |
| E | `claude_2026-10-28-group06-independent-evidence.json`, `claude_2026-10-28-group06-contracts.json` |
| F | `group07-independent-2026-10-28.json` |
| R | `claude_2026-10-28-group08-independent-evidence.json` |
| T | `tests/integration_adapters_test.py`, `tests/panel_browser_test.py` and the area-specific offline suites |

Branch evidence records the feature branch at the time it ran; claims that those
branches were not yet integrated are historical, not the current main state.

## Capability matrix

### Authentication and transport

| Capability | Implementation | Verification / boundary |
|---|---|---|
| Automatic browser authentication, no copied cookies | Implemented | Live I/A: `cookies:cdp`, recovery enabled by default |
| Re-read after an authenticated 401; same account/org | Implemented | Live I: real 401 → account 200 → original GET 200; exactly one reread |
| Initial stale-cookie 403 recovery | Implemented | Live A: account 403 `Invalid authorization` → 200; distinct from policy/Cloudflare |
| Explicit env/state/Chrome DB sources and source-pinned recovery | Implemented | Offline source variants; CDP is the live-qualified default |
| Shared CLI/Keychain/cswap/proxy access-only credentials | Implemented | Live A: invalid bearer, zero refresh exchanges; 192 credential-file hashes unchanged; cswap reader never consumes backup grants |
| Unidentified legacy bearer rotation | Fail-closed | Blocked: no reliable account lineage; a changed unidentified grant is not accepted silently |
| Isolated PKCE login; owned refresh/restart/CAS locking | Implemented | Offline; no separately enrolled owned grant. Never borrow a shared refresh token |
| Trusted-device token reread / explicit owned enrollment | Implemented | Offline; new enrollment opt-in, not performed live; Windows/macOS locks untested |
| Device/event attestation signing | Unsupported | Caller may supply attestation to file APIs; client cannot generate signatures |
| Direct HTTP, bounded retries, multipart replay, safe errors | Implemented | Live direct HTTP I; retry/replay combinations offline. CLI/panel do not print raw API error bodies |

Continuous authentication is conditional on Chrome remaining signed in and CDP
being reachable. The client cannot renew a revoked browser login without a new
login; it does not promise an immortal credential or silently enroll an account.
Explicit auth overrides still take precedence. `direct=True` / CLI `--direct`
ignore proxy/CA environment only for that HTTP client, not the whole machine.

### Session discovery and lifecycle

| Capability | Implementation | Verification / boundary |
|---|---|---|
| List/filter/page/detail; bounded local title search | Implemented | Live L; search is explicitly local, not an invented search route |
| Repository/branch/installations/auth discovery | Implemented | Live L cookie routes; exhaustive repo enumeration unproven; bearer picker rejected locally |
| Inline and deferred cloud creation; repo/revision/output branch/model/plan | Implemented | Live L: persisted config, applied mode and actual worker result |
| Pool, effort, legacy and multi-source create variants | Implemented | Offline variants; actual pool hosting not qualified |
| Title generation | Implemented | Live L |
| Rename, tags, mark-read, presence and heartbeat | Implemented | Live L; rename also I |
| Archive and usable unarchive | Implemented | Live I/L: fresh reply after unarchive, not merely active-state readback |
| Exact session deletion | Implemented | Live I/L: DELETE `{}`, subsequent GET 404; no legacy fallback |
| Share GET/create/revoke/shared-session read | Implemented | Captured contracts/offline; create requires `confirm=True`; public sharing not exercised |
| Secret scanning | Implemented | Rejected L: GET 404; explicit harmless fallback 403. Unavailable is not a clean scan |
| Support-share and debug-export | Implemented | Offline/blocked approval; no transcript/log export submitted |
| Fork, spaces/grouping/templates and SCM commit/PR actions | Unsupported | Contracts not captured; no guessed routes or action names |
| Teleport/bridge move-to-cloud | Implemented bridge-only | Offline; not a general cloud migration API |

### Chat, history and recovery

| Capability | Implementation | Verification / boundary |
|---|---|---|
| Send, processing receipts and own-turn collection | Implemented | Live I/S: exact reply and turn completion; unrelated queued result rejected offline |
| Paginated durable history (page size ≤500) | Implemented | Live I/S, including pages of two events |
| Metadata-preserving stream and legacy `stream_events` delegate | Implemented | Live I/S; unknown/meta/ephemeral vocabulary retained |
| Disconnect/resume, ordering and deduplication | Implemented | Live I/S: replay payloads equal paginated history |
| Truncated catch-up backfill | Implemented | Fault-injected live S: synthetic truncation, real GET history; natural truncation not observed |
| EOF/read failures and bounded reconnect/backoff | Implemented | Fault-injected live S; absolute stream deadline bounds reads/backoff, not synchronous callbacks/history requests |
| Worker replacement / reinitialize callback | Implemented callback | Offline; caller must explicitly integrate its handshake; natural replacement unobserved |
| Ephemeral MCP liveReply correlation | Implemented | Live S; merged `mcp_status(live_reply=True)` uses this correlator. Missed ephemeral replies cannot be replayed |
| Account watch mint/resume/rename/archive | Implemented | Live S; notifications may be `added` snapshots, not always `changed` |
| Watch 410/remint/resync | Implemented | Offline; natural 410 not observed |
| Priorities, cancellation, send-now and interrupt receipts | Implemented | Offline; genuine queue outcomes and capability-matched receipts still unqualified live |

The server's `control_only=1` suppresses ephemeral responses. `stream_control`
therefore filters the full stream locally by default. `server_filter=True` is an
explicit opt-in with that limitation. Streams own and close only their own HTTP
responses; tests do not signal browser, worker or proxy processes.

### Questions, permissions and dialogs

| Capability | Implementation | Verification / boundary |
|---|---|---|
| Pending reconciliation, complete history and initialized snapshot | Implemented | Live Q/I; stale/cancelled prompts disappear; default lookup remains read-only |
| Authoritative permission allow-once | Implemented | Offline T; harmless live Bash ran without a prompt, so genuine allow execution remains unverified |
| Deny/interrupt and permanent exact-rule reuse | Implemented | Offline T; no genuine live generator; no fabricated approval counted |
| Single/multi-select, including one-item multi | Implemented | Live Q; single/multi also through panel answer HTTP adapter I |
| Other alone and combined with selected labels | Implemented | Live Q/I; duplicate aliases rejected before posting |
| One-to-four question cards | Implemented | Live Q four-card; I three-card |
| Option previews, notes and answer-preview annotations | Implemented | Live Q/I tool-result assertions; panel fixtures T |
| Native text/number/default/unit/follow-up forms | Implemented validators/renderers | Offline T; cloud worker did not emit native kinds. Other text is not native-field proof |
| Partial/whole skip, dismiss and replied-in-chat | Implemented | Live Q distinct browser sentinels; panel/CLI encoding adapters offline T |
| User dialogs, elicitation and hook callbacks | Implemented API | Offline; dedicated generators absent; panel does not claim these renderers |

Answers preserve the original input, question text keys, tool name and tool-use
ID. A multi-choice answer remains a list; worker display joins it. Unanswered
worker encoding (empty answers plus annotations) and browser sentinel encoding
are distinct. Panel uses browser encoding; CLI defaults to worker and exposes
`--encoding browser`. Replied-in-chat marks the prompt only; it sends no chat
message. Native numeric constraints validate finite values and explicit wire
min/max/step; no missing defaults are invented.

### Controls, slash dispatch and MCP

| Capability | Implementation | Verification / boundary |
|---|---|---|
| Initialize commands/capabilities | Implemented | Live I/W |
| Authoritative pre-session model catalog and permission policy | Implemented | Live I/W; panel models load from catalog, not a hardcoded list |
| Model/mode changes and `/model`, `/plan` | Implemented | Live I/W worker verdicts and settings/initialize readback; alternative model restored before inference |
| Effort / `/effort` | Implemented | Live I/W settings readback |
| Fast/advisor/thinking/view flags | Implemented | Fast-off/advisor-off worker verdicts W; independent flag readback and other variants remain offline |
| Context/usage/btw typed dispatch | Implemented | Live I/W, exact btw response; no ordinary-text fallback |
| Cost, plan, settings, hooks, permission rules, version, suggestions/read state | Implemented | Mixed W query evidence; full method-specific live response qualification remains incomplete |
| Worker-advertised prompt commands | Implemented | Live W `/recap` actual completion; unadvertised names rejected |
| Local `/help` and panel `/theme light|dark` | Implemented local | Help live CLI process I; theme and menus tested against local fixtures, no worker turn |
| Login/config/sandbox/skills/workflows/design/slides and other host menus | Guarded, renderer absent | Unsupported in this host; `LocalCommandRequired`, never sent as chat. Account skill discovery is a separate API |
| Directory/file/diff worker controls | Implemented | Live W owned marker reads; I Changes HTTP adapter; response can legitimately be null |
| Guarded rewind | Implemented | Live W matching target; unguarded rewind failed earlier independent assertion |
| Task output/stop/end-session | Implemented | Live W marker output, stopped-task notification and worker verdict |
| Task messaging / control cancellation | Implemented | Offline; dedicated live outcomes missing |
| MCP status/toggle/reconnect/permission override | Implemented | Status ephemeral S; mutation effects offline pending dedicated fixture; askForTools uses name objects, never scalar strings |
| MCP call/staged input/output/expiry/result exclusion | Implemented/gated | Offline; authorized fixture absent. Remote result-exclusion has local-pipe restrictions |
| Session MCP servers/approvals REST | Implemented | Captured contracts/offline; connector changes not submitted |
| Feedback/rating/survey and Chrome hints | Implemented | Offline; no user feedback/logs submitted as a probe |
| Cloud-rejected and service/agent-originated controls | Explicitly unsupported | Offline fail-before-HTTP gates, including liveReply escape path; `start_shell_task`, auth/UI resources and service events cannot become controller support by serialization |
| Worker hosting, shared memory, SCM service and runner registry | Outside scope | Existing service owner must implement these, not a second controller-side service |

`command_table()` lists routing, not proof that every local renderer exists.
`run_command()` returns `via=local|control|worker`; worker results are queue
acknowledgements until the stream proves execution. `set_model` and
`set_permission_mode` retain legacy enqueue behavior without `wait=...`; CLI
and panel now wait for the worker verdict before reporting application.

### Environments, account and network configuration

| Capability | Implementation | Verification / boundary |
|---|---|---|
| Private cookie environment list/get/pagination | Implemented | Live E/I; metadata compared to browser E; environment values never logged |
| Create/update configuration preserving unspecified nested fields | Implemented | Live E/I readback; marker/setup actual tool output, not just config storage |
| Environment archive | Implemented | Live E/I excluded from lists; GET may still report active |
| Permanent environment deletion | Unsupported | No captured contract; archive is the evidenced web equivalent |
| Self-hosted pool discovery/selection | Implemented | Reads E; actual self-hosted execution unqualified |
| Last-used environment | Implemented | Live 404 absence E; 200 response shape unverified |
| Account usage/settings/profile/subscription/connectors/skills | Implemented read-only | Live E browser-shape comparisons; I usage/settings; distinct from worker settings/usage |
| Network profile detail/list, credentials/rules metadata | Implemented | Live read shapes E; metadata is not proof of secret usability |
| Profile CRUD, bindings, network credentials/rules and rotation | Implemented | Captured/offline, writes blocked without explicit authorization |
| Environment network secrets | Implemented | Live listing 403 on archived test environment; editable-fixture listing/writes unqualified |
| Bearer environment write lineage | Implemented | Offline; cookie writes are the qualified path |

### Files, workspace and media

| Capability | Implementation | Verification / boundary |
|---|---|---|
| Inline image inference | Implemented | Live F actual worker reply |
| Browser attachment multipart upload/download/reference | Implemented | Rejected F: cookie upload 401; no upload roundtrip acceptance |
| Files API upload/download/delete | Implemented | Rejected F: upload 404; no accepted roundtrip/deletion |
| Attachment prefetch | Implemented | Empty replacement acknowledged F; nonempty effect blocked by upload failure |
| Mount/seed bundles | Implemented | Offline; file upload unavailable; not cookie-qualified |
| Controller workspace GET/CAS PUT and stale-write protection | Implemented | Live C/F default workspace: byte readback and 409 without overwrite; custom-environment path behavior failed I and remains unqualified |
| TTS/Ogg mux/CLI output | Implemented | Live F valid Opus/Ogg decoding and real CLI process; no playback required |
| Cookie dictation final transcript | Implemented | Live F generated audio only; no microphone |
| Dictation pre-audio reconnect | Implemented | Fault-injected live F, two attempts and nonzero backoff |
| Dictation keepalive/natural reconnect and bearer media | Implemented subset | Offline/unqualified variants; no automatic mid-audio replay |
| Desktop dictation tickets | Unsupported | Cookie browser websocket only |

**Workspace CAS tokens are opaque.** Despite the name `content_sha256`, live C
proves it need not equal SHA256(decoded bytes). Pass the server-returned token
unchanged. Default read path is `/mnt/user-data/working/<file>`, write path is
`/working/<file>`. Do not assume custom environments expose that namespace.
A 409 propagates; do not refetch-and-overwrite automatically.

### Routines and host integration

| Capability | Implementation | Verification / boundary |
|---|---|---|
| Routine create/get/name/prompt/model/schedule updates | Implemented | Live R; I create/name readback. CLI preserves unspecified nested job fields |
| Enable/disable, fork, nonempty paging and deletion | Implemented | Live R; fork/delete also I; exact owned IDs GET 404 |
| Manual run and run history | Implemented | Live R/I submission and history only |
| Actual routine worker response | Not qualified | R/I: no `ROUTINE_OK`, limit-related output despite terminal `success`; not successful execution |
| Cron execution/mode update/alternative routine model/live tag filters | Implemented subset | Offline or unqualified live variants |
| Active routine cancellation | Implemented | Offline; useful active-run fixture absent |
| Routine API tokens/webhook creation | Implemented | Captured/offline; permission blocked, no token issued; intentionally no secret-printing CLI |
| Integration fire route / legacy `fire_routine` | Unsupported/deprecated | No captured integration-fire contract; legacy function retained with warning, not recommended |
| CLI feature helper registration and result rendering | Implemented | Live I real processes: whoami/models/dispatch/history/account; T parser/request tests; specialized unlisted CLI paths remain library-only |
| Panel auth/question continuation/model application/Changes adapters | Implemented | Live I HTTP-to-real-service checks; actual browser interaction against live workers not asserted |
| Panel native question variants and loading guards | Implemented | Isolated headless browser T: single/multi/Other/text/number/defaults/unit/skip/notes/previews/dismiss/chat encodings; fixtures, not live native-form proof |
| Panel Files inspection and lifecycle controls | Implemented | HTTP contract tests T; C library CAS proof is not a live Files-button claim |
| Panel end-to-end stream reconnect, permission rule reuse and all-click coverage | Not fully qualified | Library stream recovery is live; whole-browser live scenario remains pending |
| Panel environment/routine/network-management menus and full workspace editor | Not implemented | APIs/CLI cover portions; panel only inspects Files/Changes and exposes core session controls |
| Browser-local pinning/copy and complete navigation parity | Not implemented | Local UI work, not invented server grouping routes |
| Managed Agents public API | Separate legacy client | Outside this web-controller acceptance; no paid-key test or parity claim |

## Captured endpoint map

Cookie base is `https://claude.ai`; bearer base is `https://api.anthropic.com`
where supported. `/v1/code/*` uses `ccr-byoc-2025-07-29`,
`anthropic-version: 2023-06-01`, web platform/feature headers and selected org.
Routines add `ccr-triggers-2026-01-30`. The private Files adapter retains its
captured beta headers; public Files SDK changes do not qualify cookie access.

Abbreviations: `S=/v1/code/sessions`, `T=/v1/code/triggers`,
`E=/v1/environment_providers/private/organizations/{org}`,
`P=/v1/code/agent-proxy-profiles`, `N=/v1/code/agent-proxy`.
Matrix status above applies to every route; listing a route is not live proof.

| Surface | Captured method/path and body boundary |
|---|---|
| Sessions | GET/POST `S`; GET/PUT/DELETE `S/{id}`; DELETE body `{}` |
| Archive/restore | POST `S/{id}/archive`, `S/{id}/unarchive`, `{}` |
| Tags/read/presence | PUT session `{add_tags,remove_tags}`; POST `S/{id}/mark_read` `{event_id}`; session presence helper; POST `S/heartbeat_check` `{session_ids}` |
| Send/history/stream | POST `S/{id}/events` `{events:[{payload}]}`; GET `…/events?limit&sort_order&cursor`; GET `…/events/stream?from_sequence_num` |
| Account feed | GET `S?limit=1` mints token; GET `S/watch?resume_token&exclude_tags&include_trigger_sessions&channels_changed&keepalive_tolerance_seconds` |
| Sharing | GET/POST/DELETE `S/{id}/share`; writes `{}`; GET `/v1/code/shared-sessions/{share_id}` |
| Secret/support/debug | GET `…/share/secret-scan`; explicit POST `/api/organizations/{org}/code/shares/scan_secrets` `{text_content:[...]}`; POST/DELETE `…/support-share`; POST `…/debug-export` |
| Repo/branch pickers | GET `/api/organizations/{org}/code/repos`; GET `/api/github/organizations/{org}/github/{owner}/{repo}/branches` |
| GitHub status/title | GET `/v1/code/github/org-connection/installations-status`; GET org sync GitHub auth; POST `/v1/code/github/get-batch-branch-status`; POST org `/dust/generate_title_and_branch` |
| Models/policy | GET org `/model_selector/cc`; GET `/api/claude_code/organizations/{org}/permission_mode_policy` |
| Environment CRUD | GET `E/environments[/{id}]`; POST `E/cloud/create`; POST `E/environments/{id}` full merged object; POST `…/{id}/archive` `{}` |
| Pools/last-used | GET `/v1/code/runners/self-hosted/pools[/{id}]`; GET `/v1/code/last_used_environment` |
| Profiles | GET/POST `P`; GET/PATCH/DELETE `P/{id}`; POST `…/bindings`, `…/bindings:unbind` |
| Network credentials/rules | GET/POST `N/credentials`, `N/rules`; PATCH/DELETE `…/{id}`; POST `N/credentials/{id}/rotate` |
| Environment secrets | GET/POST `N/environment-credentials`; DELETE `…/{id}?environment_id=…` |
| Account | GET `/api/account`, `/api/account_profile`, org `/usage`, `/subscription_details`, `/mcp/remote_servers_with_connection`, `/skills/list-skills`; code-org `/user_settings` |
| Controller files | GET `S/{id}/file?path`; PUT `…/synced_file` `{path,content:base64,if_match_sha256?,...attestation}` |
| Upload/mount | POST `/api/oauth/file_upload` multipart; `/v1/files` family; POST `S/{id}/files` captured mount body |
| MCP REST | PUT `S/{id}/mcp-servers` `{mcp_servers:[...]}`; POST `…/mcp-approvals` `{tool_name,scope,tool_use_id}` |
| Routines | GET/POST `T`; GET/POST/DELETE `T/{id}`; POST `…/run` `{}` or `{text}`; POST `…/fork` `{}`; POST `…/runs/{session_id}/cancel` `{}`; POST/DELETE `…/api-token` `{}` |
| Webhook create | POST `/v1/code/webhook-triggers`; captured builder only; integration fire route not known |
| TTS | WSS `/api/ws/text_to_speech/text_stream`; text chunks then close_stream; binary Opus then SpeechComplete |
| Dictation | WSS `/api/ws/speech_to_text/voice_stream`; finite generated audio then CloseStream; final TranscriptText required |

All worker controls use the **same** event writer, never separate HTTP routes.
Admission is owned by `commands.py`; prompt responses by `prompts.py`/`events.py`;
streaming by `streaming.py`; HTTP/auth by `client._send`. Upstream MIT
`ThatCrispyToast/claude-rc-api` is the reused base. Prior-art/source-contract
provenance remains in the feature evidence, not copied into a second client.

## Reproducing verification

```bash
uv run --extra test --extra web --extra cli pytest -q
uv run --extra test --extra web --extra cli --with playwright pytest -q
uv run --extra web python scripts/claude-rc-integration-smoke --output ./integration-evidence.json
uv run --extra web python scripts/claude-rc-stream-smoke --direct --timeout 90
```

The integration smoke returns nonzero for **any** unqualified check, including
known missing permission generators and routine execution. Its output stores
request paths/statuses and exact created IDs, never credentials, environment
values, or existing transcripts. Every manifest ID is cleaned up in `finally`.
The browser test uses isolated headless Chrome, not the user's profile; install
Chrome or adjust its executable path on another platform. The fixture does not
qualify a real worker.

## Remaining acceptance plan

1. Supply a dedicated owned permission/MCP/dialog fixture and verify allow,
   deny, permanent-rule reuse, elicitation and staged files against execution.
2. Diagnose routine-run limit output with the routine's actual worker model and
   account limit state; then require a real marker, cron firing and active cancel.
   Do not classify terminal `success` as that marker.
3. Capture a cookie-authorized upload contract; only then qualify attachment
   roundtrip, nonempty prefetch, Files mount/seed/delete and staged MCP effects.
4. Capture custom-environment workspace namespace behavior. Keep the default
   workspace CAS proof separate from those failed custom-path checks.
5. Enroll an explicitly approved isolated OAuth grant and qualify refresh/restart,
   concurrent rotation and trusted-device recovery without touching shared grants.
6. Complete host-local menus, browser-only conveniences and remaining management
   screens; run the entire real-browser continuation/reconnect/lifecycle flow on
   owned resources. Rendering fixtures and live HTTP adapters are separate proof.
7. Obtain explicit approval before public shares, support/debug exports,
   feedback/log uploads, network bindings, tokens or webhook writes. Capture
   unsupported fork/grouping/SCM/integration-fire contracts before implementation.
