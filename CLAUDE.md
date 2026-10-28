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
| `lifecycle.py` | create/unarchive/delete/share/tag/mark-read, teleport events |
| `environments.py` | environments (public + private org routes), create/update, self-hosted pools |
| `triggers.py` | scheduled tasks (routines); CLI job updates must preserve unspecified nested configuration |
| `interaction.py` | pending prompts, permission + AskUserQuestion answers, slash commands, generic controls, `stream_session` (keeps meta frames), account `watch` feed |
| `files.py` | attachments / Files API |
| `tts.py` | claude.ai text-to-speech websocket + Ogg muxer |
| `events.py`, `sse.py` | wire event builders/parsers, SSE parser |
| `cli.py` | `claude-rc` subcommands (one `cmd_*` per command, registered in `main`) |
| `webui.py`, `static/` | `claude-rc web` control panel |

Tests: `tests/test_<area>.py` per module (`test_client.py` is upstream's; keep
its new tests passing when merging upstream). Offline only, via
`httpx.MockTransport`. `tests/conftest.py` strips auth env and gives each test
an empty `$HOME`. Docs: `API_REFERENCE.md` (upstream's protocol write-up),
`docs/web-api.md` (claude.ai/code shapes and what is live-verified).

## Test

```bash
uv run --extra test --extra web --extra cli pytest -q
```

On dev, uv needs the proxy CA plus the system bundle:
`cat /etc/ssl/certs/ca-certificates.crt "$SSL_CERT_FILE" > /tmp/ca.pem` and
`SSL_CERT_FILE=/tmp/ca.pem uv …`.

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
- Record what ran live in `docs/web-api.md` ("LIVE CHECK"). Devlogs go in
  `docs/devlog/claude_<date>-<desc>.md`.
