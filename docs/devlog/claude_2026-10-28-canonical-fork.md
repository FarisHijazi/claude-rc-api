# 2026-10-28: the fork becomes the canonical claude_rc

## What changed

- `main` fast-forwarded to `upstream/main` (fac6d28, upstream 0.4.0: macOS
  Keychain, Trusted Devices lazy enrollment, CAS refresh save).
- cc-proxy's vendored `python/claude_rc` (cc-proxy 90f0914..c49e4ed, branch
  `claude-rc-live`) ported as one commit on a branch from 0c4f384 (the vendoring
  base), then merged into `main`. Tests renamed `tests/claude_rc_<x>_test.py`
  -> `tests/test_<x>.py`; `scripts/claude-rc-smoke` came along.
- Version 0.5.0 (upstream took 0.4.0, ours collided).
- From claude-web-api: `update_environment` (the edit dialog's
  `POST /v1/environment_providers/{id}` route). Its `env/` tailnet wiring
  (`ccweb-init.sh`, `push_env.py`) holds machine and tailnet specifics, so it
  stays out of this public repo.

## Merge decisions

- Kept our `_send` narrow waist; upstream's `_request(method, url)` returning a
  Response is gone. Upstream's trusted-device recovery moved into `_send`: a 403
  whose body refuses the device token renews once and resends. Skipped under
  cookie auth (no bearer login to enroll).
- `resolve_auth` re-reads explicit credentials from their own store (upstream's
  `_creds_path` rule), and the CLI lookup (Keychain first on macOS) for the
  default file.
- `default_credentials_path()` follows `$HOME` (ours) but a reassigned
  `DEFAULT_CREDENTIALS_PATH` wins (upstream's tests patch the constant).
- cc-proxy's `account_inject.py` (cswap slots) is found via the installed or
  editable `cc_proxy` package; the old "repo root" lookup assumed the vendored
  layout.
- Upstream's trusted-device tests now drive `_send` through a fake
  `build_request`/`send`; conftest gives each test an empty `$HOME` so a real
  `~/.claude` never leaks in.

## Verified

- `uv run --extra test --extra web --extra cli pytest -q`: 265 passed, 1
  skipped (the cswap slot test, which needs cc-proxy installed).
- Live, read-only, cookie auth from dev Chrome over CDP:
  `claude-rc whoami` (auth `cookies:cdp`, org present) and
  `claude-rc list --limit 3` (3 sessions), `claude-rc envs list`.

## Safe default auth follow-up

- CDP now probes `http://127.0.0.1:9222` with no env var needed (empty
  `CLAUDE_AI_CDP_URL` disables). Cookie source precedence is env, live CDP,
  state, local Chrome DB, so an old state jar cannot shadow the live browser.
- `client._refresh` refuses the shared CLI file and macOS Keychain, including
  explicitly supplied credentials loaded from either. Client-owned OAuth
  files (explicit `credentials_path`) retain upstream's refresh and CAS save.
- Offline tests stub `_cdp_cookies` in addition to isolating HOME. Regression
  test verifies an expired shared credential makes no HTTP call or file write.
- Live `whoami` + `list --limit 1` also passed with NO CDP env var configured
  (auth `cookies:cdp`, org present).
