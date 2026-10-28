# Group 07: files and media (2026-10-28)

## Changes and ownership

Extended the existing `FilesMixin` and TTS implementation; no second HTTP,
auth, credential refresh, event writer, runner, or persistence owner.
The assigned worktree started clean at `ab77994`; no earlier edits were discarded.
No shared `client.py`, `interaction.py`, `cli.py`, static UI or exports were edited.
Prior-art inventory in `docs/web-api.md` and the captured browser bundles were
checked first; the existing client already owned uploads/TTS, so it was extended.
No available upstream client covered the captured new file/dictation contracts.

- `files.py`: inline image block/send helpers; max-16 attachment prefetch;
  Files API mount; controller file GET; base64 synced-file PUT with explicit
  caller-provided attestation and optional CAS. A 409 propagates without retry.
- `dictation.py`: captured cookie websocket URL, PCM16/Opus query selection,
  interim/final/endpoint events, CloseStream, four-second KeepAlive, bounded
  pre-audio retry/backoff, deadlines, sanitized failures. Never opens a microphone.
  Delivered audio is never automatically replayed after a disconnect.
- `tts.py`: clean close without SpeechComplete is now an error; malformed
  Opus duration headers raise ValueError instead of IndexError or invalid muxing.
- `media_cli.py`: registration helpers for group08, using its existing client
  factory rather than importing or modifying its CLI.

Captured contracts: core browser bundle 29 contains session file GET,
POST `/files` with `{file_id,mount_path}`, and PUT `/synced_file` with
`{path,content,if_match_sha256?,device_attestation?,device_attestation_issued_at?}`.
The session ID is in the URL, NOT the PUT body. Browser bundle 29 in the
other bundle set contains prefetch event construction; bundles 113/114 contain
speech-to-text transport and cookie organization query. These downloaded sources
remain local scratch data, not executable dependencies or committed third-party code.

## Integration signatures

Available on `RemoteControlClient` through its existing `FilesMixin`:

- `send_message_with_images(session_id, text, images, *, message_uuid=None)`;
  build blocks with `claude_rc.files.inline_image(data, mime)`.
- `prefetch_attachments(session_id, files, *, issued_at=None, only_these=False)`;
  issued_at is milliseconds, files are file_uuid/file_size mappings, max 16.
  Returns ingestion acknowledgement, NOT worker completion. An empty replacement
  is allowed; an empty nonreplacement is a no-op.
- `mount_file(session_id, file_id, mount_path, *, attestation_header=None)`.
- `get_session_file(session_id, path, *, attestation_header=None)` returns JSON
  containing base64 `content`, `content_sha256`, and `content_type`.
- `put_synced_file(session_id, path, content: bytes, *, if_match_sha256=None,
  device_attestation=None, device_attestation_issued_at=None,
  attestation_header=None)` returns `content_sha256`.

Standalone media API: `claude_rc.dictation.transcribe_audio(client, audio, *,
language='en', use_conversation_engine=True, timeout=30,
connect=None, max_connect_retries=2, on_event=None) -> str`.
Default audio is raw signed little-endian PCM16, 16 kHz mono, without a WAV header.
The optional event callback receives interim/final/endpoint/unknown events;
only final text is returned. No implicit ticket enrollment or credential exchange.

CLI owner: call `register_media_commands(cmd, _rc)` from `claude_rc.media_cli`
inside `build_parser`, where `cmd` is its existing registration closure.
Registers `transcribe`, `session-file`, `sync-file` (requires --if-match),
`mount-file`, and `prefetch`. It does not register or shadow existing
`speak`/`upload`. These new registrations are tested but not yet wired into the
shared CLI; group08 owns that integration. New exception handling should include
`DictationError` and `TTSError`, without printing raw credential-bearing objects.

## Verification

Full offline command: `uv run --extra test --extra web --extra cli pytest -q`.
Final result: **295 passed, 1 skipped**, versus the supplied 265/1 baseline.
New test modules follow this repository's `test_<area>.py` convention.
They test wire bodies, max-16 validation, multipart separation, inline images,
CAS/409 behavior, attestation passthrough, CLI handler dispatch, dictation final
versus interim, retry bounds, keepalives/deadlines and clean-close races.

Live evidence: [group07-live-2026-10-28.json](group07-live-2026-10-28.json).
Authentication was `cookies:cdp` from localhost:9222, direct networking, with no
copied cookies, shared OAuth refresh, microphone capture, or audio playback.
The JSON contains only generated test resources, result flags and status codes.

| Capability | Terminal status and evidence |
| --- | --- |
| Read-only list/environments | Live passed; four environments, no environment variable values logged. |
| OAuth chat attachment text/image upload | Server rejected cookie auth: 401 authentication_error for both files. No upload UUID returned; download/hash/prefetch/inference acceptance blocked. Existing bearer contract retained, not relabeled cookie-supported. |
| Inline images | Live passed: generated 16×16 red PNG sent through event writer; owned Haiku session answered Red and completed its turn. |
| Files API upload/download/delete | Cookie host returned 404 at upload. No ID created; download/delete acceptance blocked, not passed. |
| Session Files API mount | POST contract captured and tested offline; live blocked by absent accepted Files API upload. |
| Seed bundles | Existing lifecycle config and Files API upload retained; live seed provisioning blocked by Files API 404. No invented upload path. |
| Attachment prefetch | Empty only_these replacement acknowledged live. Real uploaded-ID prefetch/worker file effects remain unverified because upload returned 401. |
| Controller file GET | Live passed: worker wrote generated nonrepository file; GET bytes matched and returned a CAS hash. |
| Synced file PUT/readback | Live passed using the server-returned hash; changed bytes and hash matched GET readback. |
| Stale CAS | Live passed: reuse of original returned hash produced real 409 and did not overwrite changed content. |
| Device attestation | Caller-supplied fields/header tested offline. Signing/enrollment is auth-owner responsibility; no signature fabricated or needed for successful cookie CAS run. |
| Staged MCP input/output files | group05 owns the MCP staging contract. FilesMixin provides byte upload/download and captured mount methods; dedicated MCP live staging not run. |
| TTS websocket/Ogg/CLI | Live passed for 'test complete': 5961 bytes, 1200 ms; ffprobe identified Opus 48 kHz mono; ffmpeg decoded to PCM. Real `python -m claude_rc.cli speak ... -o ...` exited 0 and wrote Ogg. Bearer TTS remains unverified. |
| Dictation interim/final/endpoint | Live passed using generated TTS→PCM fixture: TranscriptInterim, TranscriptText='Test complete.', TranscriptEndpoint; no microphone opened. |
| Dictation keepalive | Wire/deadline/race tests offline. Live successful calls used real event reader; KeepAliveAck was not naturally observed and is not claimed live. |
| Dictation reconnect | Fault-injected first dial OSError, then real successful dial; two attempts, measured 0.250 s backoff, correct final text. This is fault-injected live recovery, not a naturally occurring service disconnect. Mid-audio replay/resume deliberately unsupported. |
| Desktop dictation tickets | Observed browser source has claude.dictation.v1 + ticket subprotocol. No client-owned ticket grant; not implemented or claimed cookie parity. |

Live path correction: GET accepts `/mnt/user-data/outputs`,
`/mnt/user-data/working`, `/mnt/project-files`; PUT requires `/working/`.
Testing `/tmp` returned explicit 400 messages. The same working file is read via
`/mnt/user-data/working/<file>` and written via `/working/<file>`.
Use the GET response hash verbatim; a locally computed attachment digest failed
CAS, whereas the server-returned hash succeeded. No automatic hash substitution.

The first create attempt timed out (ReadTimeout); read-only reconciliation found
no corresponding recent session. Subsequent creates used the actual default
environment and returned IDs. Every returned owned session ID (five total,
including admission checks and inline-image inference) was archived and deleted;
the evidence lists the exact IDs. A subsequent GET of each exact ID returned
404, verifying cleanup. Existing user sessions remained read-only.

## Remaining work, explicitly not passes

- Group08 CLI wiring/export/UI integration: call the helper above and expose
  read/write namespaces and CAS conflicts accurately (~15 min plus its UI tests).
- Cookie attachment upload: capture the authenticated browser's actual composer
  upload/download flow and compare with OAuth-only 401 route before implementing
  an alternative (~30 min). Do not borrow or refresh a shared OAuth grant.
- Files API/mount/seed bundle acceptance: repeat only with a separately owned
  authorized grant or captured cookie route; no grant was enrolled here
  (~20 min after access exists). Delete exact returned test file IDs afterward.
- Auth owner may supply actual attestation signing; test that path only with its
  owned enrollment (~20 min after contract integration).
- Group05 must qualify staged MCP file effects against its dedicated test server
  (~20 min after its MCP contract is integrated).
- Desktop ticket dictation is a distinct auth mode, not required for the working
  cookie route; implement only after its ticket minting contract/access exists
  (~30 min). Mid-audio transparent replay stays unsupported to avoid duplicate text.
