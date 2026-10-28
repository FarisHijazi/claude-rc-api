# Group01 auth and HTTP foundation

## Changes

Started an absent `feat/01-auth-and-http-foundation` worktree from `main` at
`ab77994`. No pre-existing changes existed in this worktree; other feature
worktrees were not edited. No CLI, web UI, static assets or top-level exports
were changed.

- Kept `_send` as the HTTP owner. Recoverable cookie sources are pinned through
  `GET /api/account` before the first operation. On a 401/eligible 403, re-read
  only the original source, verify the same account, then replay the original
  operation once with its original organization. Reject and restore the prior
  in-memory jar if identity validation fails. Serialize request dispatch and
  identity recovery per client; streaming bodies are read outside that lock.
- Distinguish Cloudflare, policy and trusted-device denials. Retain upstream
  retry bounds and refusal cooldowns. Enrollment now requires explicit
  `allow_device_enrollment=True` and is disallowed for known shared stores.
- Refuse refresh of known CLI/Keychain/cswap/proxy paths, including resolved
  symlinks and overridden config roots, in both the client and public refresh
  function. An explicit `credentials_path` wins over browser/env discovery.
- Discover an isolated default client-owned file after cookie sources and before
  shared bearer sources. Serialize its complete read/exchange/CAS persistence
  with a thread lock and OS sidecar lock. Adopt an identified concurrent
  rotation instead of spending the stale grant twice. New files use unique
  mode-0600 temporary files, fsync and atomic replacement. Failed persistence
  retains the only valid rotated pair in memory, including mkstemp failures.
- `direct=True` applies `trust_env=False` to this client's HTTP and refresh
  transports and disables claude.ai proxy mounts, without editing global env.
  Multipart payloads must be bytes/str; file objects are rejected before send.
  Authenticated absolute URLs must remain on the configured origin.
- Secret fields are excluded from repr; APIError string/repr excludes raw bodies.
  `.body` remains available for deliberate, potentially sensitive inspection.
- Added explicit PKCE helpers, not an implicit login flow. They never open a
  browser, start a service, enroll a trusted device or borrow a shared grant.

## Reuse and contracts for integration

Extended the existing upstream refresh/CAS, retry and trusted-device owners;
did not create another transport or poller. Inspected claude-swap's refresh
ownership and its XDG/legacy storage roots. Installed Claude Code 2.1.312 exposes
these authorization literals: `https://claude.com/cai/oauth/authorize`,
`https://platform.claude.com/oauth/code/callback`, the existing token endpoint
and client ID, and `user:profile`, `user:inference`,
`user:sessions:claude_code`. A community PKCE source fetch returned 404; no code
was copied. The implemented enrollment exchange remains **not live-verified**;
endpoint literals alone do not prove the complete grant contract.

Group08 can import directly; this group does not register CLI commands:

```python
from claude_rc.login import PKCELogin, begin_login, finish_login
from claude_rc.credentials import client_credentials_path

begin_login(path: str | Path) -> PKCELogin
finish_login(login: PKCELogin, callback_url: str, *,
             client: httpx.Client | None = None,
             direct: bool = False) -> OAuthCredentials
client_credentials_path() -> Path
```

`PKCELogin.authorization_url`, `.verifier`, `.state` and callback URLs must never
be logged. The caller explicitly opens authorization_url and supplies the
callback through its existing interaction layer. State, destination, 10-minute
expiry and single-use checks precede exchange; existing/shared destinations are
refused. The new file stores `claudeRcAccountUuid`, which pins owned-file reloads.
`RemoteControlClient(..., direct=False, allow_device_enrollment=False)` adds only
optional keyword arguments. `_send`'s existing signature is preserved.
`APIError.failure_kind` distinguishes `authentication`, `policy`,
`trusted_device`, `authentication_or_policy` and other `http` errors. For signed
cross-origin upload URLs, callers must not use authenticated `_send`.

## Offline verification

`uv run --extra test --extra web --extra cli pytest -q tests`:
**293 passed, 1 skipped**. `git diff --check` passed.

New `tests/test_auth.py` covers real client dispatch against MockTransport,
both 401/403 recovery paths, exactly one replay, account-change rejection and
rollback, organization preservation, policy/Cloudflare distinction, original
source rereads, shared-store/symlink refusal, explicit/default-owned precedence,
thread/process refresh serialization, atomic persistence and restart loading,
direct-network isolation, multipart replay, secret reprs, default no-enrollment,
PKCE state/destination/expiry/single-use/isolation and failure to create a
post-refresh temporary file. Existing tests were updated where the prior
behavior was intentionally unsafe: shared Keychain refresh, cross-origin auth
and implicit device enrollment.

## LIVE CHECK: read-only acceptance on dev

Ran the feature worktree's installed package with `RemoteControlClient(direct=True)`;
no token, cookie, account or organization arguments. No browser input or real
session writes were performed. Successful acceptance was repeated after the
source-pinning changes.

- Auth source: `cookies:cdp` from automatic localhost:9222 discovery.
- Initial account pin, `list_sessions(limit=1)` and `list_environments()` succeeded.
- Replaced both sessionKey variants with invalid strings **only in this client
  instance's memory**. The real original sessions GET returned **401**.
- Re-read CDP once; identity-validation `GET /api/account` returned **200**;
  exactly one replay of the original sessions GET returned **200**.
- Account UUID and organization remained unchanged. No cookie values, UUIDs,
  session metadata or environment values were printed or saved as evidence.
- A separate in-memory invalid bearer marked as originating from the shared
  credential file returned a real **401**. The refresh transport recorded
  **zero** exchanges.
- SHA-256 comparisons before/after showed all **180** existing shared credential
  files examined (CLI, account-router snapshots and cswap stores) unchanged.
  Credential contents and individual hashes were not printed.
- The isolated default client-owned OAuth file was absent. No enrollment was
  attempted to manufacture a grant.

## Explicit remaining verification and limitations

1. **Owned PKCE enrollment/refresh/restart: unverified live.** No separately
   enrolled client-owned identity exists. Next acceptance must explicitly enroll
   one through the integration layer, force only its local expiry, refresh it,
   restart and GET, then verify concurrent rotation. Never substitute a CLI or
   cswap refresh token. Offline tests prove synchronization, not server support.
2. **Trusted-device enrollment and signed event attestation: unverified live.**
   No client-owned trusted-device enrollment exists. The upstream refusal and
   cooldown behavior remains tested offline; this client still cannot sign
   events. `served_call_unsigned` must not trigger repeated enrollment.
3. **Legacy bearer continuity is deliberately fail-closed.** Owned login files
   carry account metadata and reject account changes. A changed legacy bearer
   is adopted only with matching account metadata or an unchanged nonempty
   refresh grant proving lineage. Access-only cswap-slot changes and unidentified
   rotating shared logins are rejected before sending; they cannot silently
   switch accounts. To support uninterrupted legacy bearer recovery too, capture
   profile identity before first use and validate every reread against it. The
   default CDP-cookie path already enforces and live-verifies its account pin.
4. **Platform coverage:** thread and separate-process refresh serialization were
   tested on Linux. The Windows msvcrt and macOS filesystem/Keychain variants
   were not executed here. Shared Keychain refresh is intentionally forbidden.
5. **Lifetime:** cookie recovery is verified, but no finite test proves Chrome
   will retain a login forever. Browser logout/expired interactive login still
   requires re-authentication; no hidden refresh endpoint or background daemon
   was invented.

## Independent verification and fixes (same day)

Verified the feature worktree package explicitly, not the main checkout. Found
and fixed two defects in the builder's result:

- `load_slot_token()` still executed the proxy's slot accessor, which calls
  `consume_backup_grant` for an expired non-live slot. Replaced that dependency
  with cswap's existing read-only storage API (`_read_credentials`,
  `_read_account_credentials_ex`, `oauth.extract_oauth_data`), inspecting the
  installed claude-swap implementation. Never call its grant consumer. Expired
  slots fail closed. Covered live/backup selection and expired/fresh/no-expiry
  access tokens offline. Updated the old core test that asserted AST internals;
  the optional cc-proxy-dependent skip is no longer necessary.
- Initial `/api/account` pinning prevented stale-cookie recovery before the first
  operation. Permit one original-source reread and account GET retry before
  sending the requested operation. Live invalid cookies at this endpoint return
  **403 `permission_error` with message `Invalid authorization`**, not 401.
  Recognize this exact observed authentication error separately from policy.
  Other policy/Cloudflare denials still do not reread. Offline tests cover both
  401/403 and a still-invalid reread without loops.

Commands executed (absolute worktree path supplied via `uv --directory`):

- `uv --directory <feature-worktree> run --extra test --extra web --extra cli pytest -q`
  before fixes: **293 passed, 1 skipped**; after fixes: **304 passed**.
- `<feature-worktree>/.venv/bin/python <session-scratchpad>/group01_independent_live.py`
  (exit 0): automatic `cookies:cdp` discovery listed a session and environments.
  Already-pinned sessions GET: **401 → account GET 200 → original GET 200**,
  one CDP reread, same account and org. Initial stale-cookie pin:
  **account GET 403 → account GET 200 → sessions GET 200**, one CDP reread.
  These are client-memory fault injections with actual service responses, not
  mocked server responses. No real-user session mutations or browser input.
- Invalid in-memory shared bearer: real **401**, **zero** refresh exchanges.
  Its reload hook was disabled just for this check so no real shared access
  token could replace the synthetic invalid token. The refresh endpoint was
  instrumented with a recording MockTransport; it received no requests.
  Hashes of **192** existing CLI/router snapshot/cswap credential files remained
  unchanged. Only aggregate counts and booleans were printed, not file contents
  or individual hashes.
- `git diff --check`: passed.

The initial bootstrap live check first failed at the real 403, exposing the
misclassification; acceptance was rerun successfully after the correction.
No isolated default OAuth grant exists: PKCE enrollment, owned refresh/restart,
trusted-device enrollment, signed attestation and other-platform locking remain
unverified live, as listed above. Cookie continuity is not a promise of endless
browser login. Group08 integration must update `docs/web-api.md`'s old AST-slot
reader/offline-suite notes; that ledger is outside this group's editing scope.
