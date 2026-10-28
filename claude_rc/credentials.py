"""OAuth credential handling for the Claude Remote Control / Sessions API.

The Claude Code CLI logs in through claude.ai (OAuth) and stores the resulting
tokens in ``~/.claude/.credentials.json`` (on macOS, in the login Keychain item
``Claude Code-credentials`` instead) and account metadata (including the
organization UUID that the API requires as the ``x-organization-uuid`` header)
in ``~/.claude.json``.

This module reads those stores so a third-party program can authenticate exactly
the way the claude.ai/code web app does, and refreshes the access token against
``POST https://platform.claude.com/v1/oauth/token`` when it has expired.

All of the constants below were reverse-engineered from the CLI binary:

* client id          -> ``9d1c250a-e61b-44d9-88ed-5944d1962f5e``
* token endpoint     -> ``https://platform.claude.com/v1/oauth/token``
  (refresh is served by platform.claude.com, *not* api.anthropic.com)
* oauth beta header  -> ``oauth-2025-04-20``
"""

from __future__ import annotations

import ast
import contextlib
import tempfile
import threading
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from ._version import __version__

_log = logging.getLogger(__name__)

# --- reverse-engineered constants ------------------------------------------
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Token refresh is served by platform.claude.com, NOT api.anthropic.com.
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_BETA = "oauth-2025-04-20"
# The CLI's own refresh User-Agent, for parity.
OAUTH_USER_AGENT = "anthropic-sdk-typescript/0.94.0 userOAuthProvider"

# Lookups go through the functions below, which follow ``$HOME`` at call time;
# assigning a different path to one of these constants overrides it.
DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
DEFAULT_CONFIG_PATH = Path.home() / ".claude.json"
_IMPORT_CREDENTIALS_PATH, _IMPORT_CONFIG_PATH = DEFAULT_CREDENTIALS_PATH, DEFAULT_CONFIG_PATH
# On macOS the CLI keeps the same JSON document (``claudeAiOauth`` block and
# all) as a login-Keychain generic password under this service name. It reads
# the Keychain first; the credentials file is only its fallback (a Keychain
# write that failed).
KEYCHAIN_SERVICE = "Claude Code-credentials"
# The account it files the item under: $USER, unless that has other characters.
_KEYCHAIN_ACCOUNT = re.compile(r"[a-zA-Z0-9._-]+")
_KEYCHAIN_FALLBACK_ACCOUNT = "claude-code-user"
_KEYCHAIN_NOT_FOUND = 44  # `security` exit status for errSecItemNotFound
# The longest command the CLI pipes into `security -i`; past it, it uses argv.
_KEYCHAIN_STDIN_MAX = 4032

# Trusted Devices: the enrolled device's token sits at the document's top
# level next to the OAuth block, with a stamp when a refused call (not a
# sign-in) enrolled it; a refused token that a refusal enrolled less than a day
# ago isn't replaced by enrolling again (the CLI's guard against a loop).
TRUSTED_DEVICES_PATH = "/api/auth/trusted_devices"
_ENROLLED_AFTER_REFUSAL = "trustedDeviceEnrolledAfterRefusalAtMs"
_REFUSAL_ENROLL_COOLDOWN_MS = 24 * 3600 * 1000

# The claude.ai origin: serves the same ``/v1/code/*`` routes to a browser's
# ``sessionKey`` cookie (plus ``/api/organizations/...`` web routes).
CLAUDE_AI_BASE_URL = "https://claude.ai"

# Environment overrides (handy in CI / other machines).
ENV_CLAUDE_AI_ACCESS_TOKEN = "CLAUDE_AI_ACCESS_TOKEN"
ENV_ACCESS_TOKEN = "CLAUDE_RC_ACCESS_TOKEN"
ENV_CLAUDE_AI_ORG_UUID = "CLAUDE_AI_ORG_UUID"
ENV_ORG_UUID = "CLAUDE_RC_ORG_UUID"
# The CLI's own env override for a bare access token.
ENV_CLI_OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
# A claude.ai browser ``sessionKey`` cookie; selects cookie auth on claude.ai
# (the other cookie sources live in claude_rc.webauth).
ENV_SESSION_KEY = "CLAUDE_AI_SESSION_KEY"
# A claude-swap (cswap) slot whose stored login to borrow (access token only).
ENV_CSWAP_SLOT = "CLAUDE_RC_CSWAP_SLOT"
# The CLI's trusted-device override (orgs that enforce Trusted Devices).
ENV_TRUSTED_DEVICE_TOKEN = "CLAUDE_TRUSTED_DEVICE_TOKEN"


def default_credentials_path() -> Path:
    """``~/.claude/.credentials.json`` under the current ``$HOME``, unless
    :data:`DEFAULT_CREDENTIALS_PATH` was reassigned."""
    if DEFAULT_CREDENTIALS_PATH != _IMPORT_CREDENTIALS_PATH:
        return DEFAULT_CREDENTIALS_PATH
    return Path.home() / ".claude" / ".credentials.json"


def client_credentials_path() -> Path:
    """An isolated store; never an alias for the CLI or cswap's rotating grant."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "claude-rc/credentials.json"


def default_config_path() -> Path:
    """``~/.claude.json`` under the current ``$HOME``, unless
    :data:`DEFAULT_CONFIG_PATH` was reassigned."""
    if DEFAULT_CONFIG_PATH != _IMPORT_CONFIG_PATH:
        return DEFAULT_CONFIG_PATH
    return Path.home() / ".claude.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


class CredentialsError(RuntimeError):
    """Raised when credentials cannot be located, read, or refreshed."""


@dataclass
class OAuthCredentials:
    """The ``claudeAiOauth`` block of ``~/.claude/.credentials.json``."""

    access_token: str = field(repr=False)
    refresh_token: Optional[str] = field(default=None, repr=False)
    expires_at_ms: Optional[int] = None
    scopes: list[str] = field(default_factory=list)
    subscription_type: Optional[str] = None
    client_id: Optional[str] = None
    trusted_device_token: Optional[str] = field(default=None, repr=False)
    source_path: Optional[Path] = None
    # Keychain service the tokens came from (macOS); mutually exclusive with
    # ``source_path``. Either one marks the credentials as re-readable.
    source_keychain: Optional[str] = None
    account_uuid: Optional[str] = None
    # Set while a refresh of these tokens isn't saved (it failed, or persist
    # was off): the spent refresh token the store still holds. The store is
    # this login, just behind, so it isn't reloaded over these tokens, and
    # the next refresh may still save over it.
    stale_refresh_token: Optional[str] = field(default=None, repr=False)

    def is_expired(self, skew_seconds: int = 60) -> bool:
        """True if the token is expired (or within ``skew_seconds`` of it)."""
        if not self.expires_at_ms:
            return False
        return _now_ms() >= self.expires_at_ms - skew_seconds * 1000

    # -- (de)serialization -------------------------------------------------
    @classmethod
    def from_block(
        cls,
        block: dict,
        source_path: Optional[Path] = None,
        trusted_device_token: Optional[str] = None,
        source_keychain: Optional[str] = None,
    ) -> "OAuthCredentials":
        token = block.get("accessToken")
        if not token:
            raise CredentialsError("credentials file has no claudeAiOauth.accessToken")
        return cls(
            access_token=token,
            refresh_token=block.get("refreshToken"),
            expires_at_ms=block.get("expiresAt"),
            scopes=list(block.get("scopes") or []),
            subscription_type=block.get("subscriptionType"),
            client_id=block.get("clientId"),
            trusted_device_token=trusted_device_token,
            source_path=source_path,
            source_keychain=source_keychain,
        )

    def to_block(self) -> dict:
        block = {
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at_ms,
            "scopes": self.scopes,
            "subscriptionType": self.subscription_type,
        }
        if self.client_id:
            block["clientId"] = self.client_id
        return block


def load_credentials(path: os.PathLike | str | None = None) -> OAuthCredentials:
    """Load OAuth credentials.

    An explicit ``path`` bypasses environment overrides and Keychain lookup.
    Otherwise priority is:
      1. ``CLAUDE_RC_ACCESS_TOKEN`` env var (no refresh possible).
      2. macOS only, when no explicit ``path`` is given: the login-Keychain
         item :data:`KEYCHAIN_SERVICE`, which the CLI also reads first.
      3. ``~/.claude/.credentials.json`` (``claudeAiOauth`` block).
    """
    env_token = os.environ.get(ENV_ACCESS_TOKEN) or os.environ.get(ENV_CLI_OAUTH_TOKEN)
    if env_token and path is None:
        return OAuthCredentials(
            access_token=env_token, trusted_device_token=os.environ.get(ENV_TRUSTED_DEVICE_TOKEN)
        )

    keychain_miss = None
    if path is None and sys.platform == "darwin":
        try:
            raw = _keychain_read(KEYCHAIN_SERVICE)
        except CredentialsError as exc:  # locked, timed out: fall back to the file, as the CLI does
            raw, keychain_miss = None, str(exc)
        if raw is not None:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:  # the CLI reads an unparsable item as none too
                keychain_miss = f"Keychain item {KEYCHAIN_SERVICE!r} is not valid JSON"
            else:
                where = f"macOS Keychain item {KEYCHAIN_SERVICE!r}"
                return _from_document(data, where, source_keychain=KEYCHAIN_SERVICE)
        keychain_miss = keychain_miss or f"no {KEYCHAIN_SERVICE!r} Keychain item"

    p = Path(path) if path else default_credentials_path()
    if not p.exists():
        where = f"credentials file not found at {p}"
        if keychain_miss:
            where = f"{keychain_miss}, and {where}"
        raise CredentialsError(
            f"{where}. Run `claude` and `/login` first, or set {ENV_ACCESS_TOKEN}."
        )
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - io
        raise CredentialsError(f"could not read {p}: {exc}") from exc
    return _from_document(data, str(p), source_path=p)


def _from_document(
    data: dict,
    where: str,
    *,
    source_path: Optional[Path] = None,
    source_keychain: Optional[str] = None,
) -> OAuthCredentials:
    """Build credentials from the CLI's credentials JSON document."""
    block = data.get("claudeAiOauth")
    if not block:
        raise CredentialsError(
            f"{where} has no `claudeAiOauth` block — are you logged in via an API key "
            f"instead of claude.ai? Remote Control requires a claude.ai OAuth login."
        )
    # A top-level `trustedDeviceToken` sibling is used when the org requires
    # Trusted Devices; harmless to carry when absent.
    creds = OAuthCredentials.from_block(
        block,
        source_path=source_path,
        trusted_device_token=os.environ.get(ENV_TRUSTED_DEVICE_TOKEN) or data.get("trustedDeviceToken"),
        source_keychain=source_keychain,
    )
    creds.account_uuid = data.get("claudeRcAccountUuid")
    return creds


def _keychain_account() -> str:
    """The account the CLI files its item under: ``$USER`` (else the passwd
    name), or ``claude-code-user`` when that has characters it won't use."""
    try:
        import pwd  # only reached on macOS; Windows has no pwd

        name = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # no USER and no passwd entry
        name = ""
    return name if _KEYCHAIN_ACCOUNT.fullmatch(name) else _KEYCHAIN_FALLBACK_ACCOUNT


def _keychain_read(service: str) -> Optional[str]:
    """The item's secret, or ``None`` when there is no such item. Raises
    :class:`CredentialsError` when the Keychain can't be read (locked, timed
    out), so a caller never mistakes that for an empty store."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", _keychain_account(), "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialsError(f"could not read Keychain item {service!r}: {exc}") from exc
    if out.returncode == _KEYCHAIN_NOT_FOUND:
        return None
    if out.returncode != 0:
        raise CredentialsError(
            f"could not read Keychain item {service!r}: security exited {out.returncode}"
            f" {out.stderr.strip()[:200]}"
        )
    return out.stdout.strip() or None


def _keychain_write(service: str, secret: str) -> None:
    """Create or update (``-U``) the item the way the CLI does: hex-encoded
    (``-X``) over ``security -i``'s stdin, out of reach of ``ps``. A command
    too long for ``security -i`` goes on argv instead, as the CLI's does (a
    credentials document holding MCP logins easily passes the limit)."""
    account, hexed = _keychain_account(), secret.encode().hex()
    cmd = f'add-generic-password -U -a "{account}" -s "{service}" -X "{hexed}"\n'
    if len(cmd) <= _KEYCHAIN_STDIN_MAX:
        argv, io = ["security", "-i"], {"input": cmd}
    else:
        argv = ["security", "add-generic-password", "-U", "-a", account, "-s", service, "-X", hexed]
        io = {"stdin": subprocess.DEVNULL}
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=15, **io)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialsError(f"could not update Keychain item {service!r}: {exc}") from exc
    if out.returncode != 0:
        raise CredentialsError(
            f"could not update Keychain item {service!r}: {out.stderr.strip()[:200]}"
        )


def load_org_uuid(config_path: os.PathLike | str | None = None) -> Optional[str]:
    """The organization UUID from the environment or ``~/.claude.json``.

    Priority: ``CLAUDE_AI_ORG_UUID``, ``CLAUDE_RC_ORG_UUID``,
    ``oauthAccount.organizationUuid`` in ``~/.claude.json``, a top-level
    ``organizationUuid`` in ``.credentials.json``. ``None`` when none has it —
    :class:`~claude_rc.client.RemoteControlClient` then asks
    ``/api/oauth/profile`` (see :func:`org_uuid_from_profile`).
    """
    env_org = os.environ.get(ENV_CLAUDE_AI_ORG_UUID) or os.environ.get(ENV_ORG_UUID)
    if env_org:
        return env_org

    p = Path(config_path) if config_path else default_config_path()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            account = data.get("oauthAccount") or {}
            if account.get("organizationUuid"):
                return account["organizationUuid"]
        except (OSError, json.JSONDecodeError):
            pass

    # Fallback: a top-level `organizationUuid` sibling in .credentials.json.
    cp = default_credentials_path()
    if cp.exists():
        try:
            data = json.loads(cp.read_text())
            return data.get("organizationUuid")
        except (OSError, json.JSONDecodeError):
            return None
    return None


def shared_credentials(creds: OAuthCredentials) -> bool:
    """Known external credential stores are always read-only, even via symlinks."""
    if creds.source_keychain:
        return True
    if creds.source_path is None:
        return False
    path = creds.source_path.expanduser().resolve()
    home = Path.home()
    roots = [home / ".claude", home / ".claude-swap", home / ".config/claude-swap",
             home / ".config/cc-proxy", home / ".config/claude-code-proxy",
             home / ".local/state/claude-codex",
             home / ".local/share/claude-swap", home / ".claude-swap-backup"]
    for variable, suffix in (("XDG_DATA_HOME", "claude-swap"),
                             ("XDG_STATE_HOME", "claude-codex")):
        if os.environ.get(variable):
            roots.append(Path(os.environ[variable]) / suffix)
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if config:
        roots.append(Path(config))
    return path == default_credentials_path().resolve() or any(
        path == r.expanduser().resolve() or r.expanduser().resolve() in path.parents for r in roots
    )


_refresh_lock = threading.RLock()


@contextlib.contextmanager
def credential_lock(path: Path | None):
    """Serialize the complete read/exchange/CAS write across threads and processes.

    OS advisory locks are released on process exit. Windows uses msvcrt; POSIX
    uses flock. The sidecar is deliberately retained, never unlinked while held.
    """
    with _refresh_lock:
        if path is None:
            yield
            return
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b") as lock:
            if os.name == "nt":
                import msvcrt
                if not lock.read(1):
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def refresh_credentials(creds: OAuthCredentials, *, persist: bool = True,
                        client: httpx.Client | None = None) -> OAuthCredentials:
    """Refresh only an isolated client-owned grant, under its serialization lock.

    An identified concurrent rotation is adopted from disk, never exchanged twice.
    Known shared stores are rejected before a lock file or HTTP request is made.
    """
    if shared_credentials(creds):
        raise CredentialsError("shared CLI/Keychain/cswap/proxy credentials are read-only")
    with credential_lock(creds.source_path):
        if creds.source_path and persist:
            data = _read_document(creds) or {}
            block = data.get("claudeAiOauth") or {}
            stored_refresh = block.get("refreshToken") if isinstance(block, dict) else None
            if stored_refresh and stored_refresh not in (creds.refresh_token, creds.stale_refresh_token):
                # Only an identified client-owned lineage may adopt a concurrent rotation.
                if data.get("claudeRcAccountUuid") and data.get("claudeRcAccountUuid") == creds.account_uuid:
                    return _from_document(data, "client-owned credentials", source_path=creds.source_path)
        return _refresh_credentials_unlocked(creds, persist=persist, client=client)


def _refresh_credentials_unlocked(
    creds: OAuthCredentials,
    *,
    persist: bool = True,
    client: httpx.Client | None = None,
) -> OAuthCredentials:
    """Refresh an expired access token via the OAuth token endpoint.

    Mirrors the CLI: ``POST`` to :data:`OAUTH_TOKEN_URL` with a JSON body of
    ``{grant_type, refresh_token, client_id}`` and the ``oauth-2025-04-20`` beta
    header. The public wrapper rejects all shared CLI/Keychain stores first.
    With ``persist``, tokens are atomically saved only while the isolated file
    still holds the grant being refreshed. The refresh token is single-use: a
    sign-in or sign-out since, another process's refresh, or a corrupt file raises
    :class:`CredentialsError` first. If saving fails after the refresh, the
    new tokens are still returned, with a logged warning and
    :attr:`~OAuthCredentials.stale_refresh_token` set: they are the only
    valid ones left.
    """
    if not creds.refresh_token:
        raise CredentialsError(
            "access token is expired and no refresh_token is available "
            "(token came from an env var). Re-run `claude` to refresh it."
        )
    # A store left behind by an unsaved refresh is still this login, as is
    # the "" the CLI clears its copy of that spent token to.
    behind = (creds.stale_refresh_token, "") if creds.stale_refresh_token else ()
    if persist:
        _stored_login(creds, (creds.refresh_token, *behind))

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        resp = client.post(
            OAUTH_TOKEN_URL,
            headers={
                "Content-Type": "application/json",
                "anthropic-beta": OAUTH_BETA,
                "User-Agent": OAUTH_USER_AGENT,
            },
            json={
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh_token,
                "client_id": creds.client_id or CLAUDE_CODE_CLIENT_ID,
            },
        )
    finally:
        if owns_client:
            client.close()

    if resp.status_code != 200:
        raise CredentialsError(
            f"token refresh failed: HTTP {resp.status_code}"
        )
    body = resp.json()
    new = OAuthCredentials(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", creds.refresh_token),
        expires_at_ms=_now_ms() + int(body.get("expires_in", 0)) * 1000
        if body.get("expires_in")
        else creds.expires_at_ms,
        scopes=(body.get("scope") or " ".join(creds.scopes)).split()
        if body.get("scope")
        else creds.scopes,
        subscription_type=creds.subscription_type,
        client_id=creds.client_id,
        trusted_device_token=creds.trusted_device_token,
        source_path=creds.source_path,
        source_keychain=creds.source_keychain,
        account_uuid=creds.account_uuid,
        stale_refresh_token=creds.stale_refresh_token or creds.refresh_token,
    )

    if persist:
        try:
            # Like the CLI's compare-and-swap, which also takes a "" it cleared.
            _persist(new, held=(creds.refresh_token, "", *behind))
            new.stale_refresh_token = None
        except CredentialsError as exc:  # never raise here: `new` is the only valid pair
            _log.warning("refreshed tokens kept in memory only: %s", exc)
    return new


def _stored_login(creds: OAuthCredentials, held: tuple) -> Optional[dict]:
    """The document ``creds`` came from, provided its login's refresh token is
    one of ``held`` (``None`` when there's no store). Raises
    :class:`CredentialsError` when the store can't be read or has moved on."""
    data = _read_document(creds)
    if data is None:
        return None
    block = data.get("claudeAiOauth")
    if (not isinstance(block, dict) or block.get("refreshToken") not in held
            or (creds.account_uuid and data.get("claudeRcAccountUuid") != creds.account_uuid)):
        where = f"Keychain item {creds.source_keychain!r}" if creds.source_keychain else creds.source_path
        raise CredentialsError(
            f"{where} no longer holds the login being refreshed (refreshed, signed in"
            " or out elsewhere, or unreadable), so it was left alone"
        )
    return data


def _persist(creds: OAuthCredentials, *, held: tuple) -> None:
    """Write refreshed tokens back to the store they came from, the way the
    CLI's refresh saves: a compare-and-swap that writes only while the stored
    login's refresh token is one of ``held`` (the one just spent, or what the
    store was left with). Only the token fields change; the block's other
    fields (``subscriptionType``, ``rateLimitTier`` …) and the document's
    other keys (``mcpOAuth`` …) stay as stored."""
    data = _stored_login(creds, held)
    if data is None:
        return
    block = data["claudeAiOauth"]
    if block.get("refreshToken") != creds.refresh_token:
        # It described the refresh token we just spent — drop it rather
        # than leave a stale expiry next to the new token.
        block.pop("refreshTokenExpiresAt", None)
    block.update(
        accessToken=creds.access_token,
        refreshToken=creds.refresh_token,
        expiresAt=creds.expires_at_ms,
        scopes=creds.scopes,
    )
    _write_document(creds, data)


def _read_document(creds: OAuthCredentials) -> Optional[dict]:
    """The document ``creds`` came from (the credentials file or the Keychain
    item): ``None`` when they came from no store, and ``{}`` when it is gone,
    unreadable or not a JSON object, as the CLI's store reads it. A Keychain
    that can't be read (locked, timed out) raises :class:`CredentialsError`
    instead of passing for empty."""
    if creds.source_keychain:
        raw = _keychain_read(creds.source_keychain)
    elif creds.source_path:
        try:
            raw = creds.source_path.read_text()
        except OSError:
            raw = None
    else:
        return None
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_document(creds: OAuthCredentials, data: dict) -> None:
    """Replace the document ``creds`` came from (the file atomically)."""
    if creds.source_keychain:
        _keychain_write(creds.source_keychain, json.dumps(data))
        return
    path = creds.source_path
    tmp = None
    try:
        fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        tmp = Path(name)
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()  # it holds tokens
        raise CredentialsError(f"could not write {path}: {exc}") from exc


def enroll_trusted_device(
    creds: OAuthCredentials,
    *,
    base_url: str = "https://api.anthropic.com",
    persist: bool = True,
    client: httpx.Client | None = None,
    display_name: str | None = None,
    after_refusal: bool = False,
) -> str | None:
    """Enroll this device for Trusted Devices, returning the device token.

    Some organizations require Trusted Devices for Remote Control: calls that
    touch an elevated session (e.g. reading its event stream) are rejected
    with HTTP 403 ``untrusted_device`` unless the caller presents an
    ``X-Trusted-Device-Token`` header. The official CLI enrolls on demand when
    it hits that rejection ("lazy enrollment"); this mirrors that behavior.
    Each enrollment registers a device on the account and emails its owner.

    On success the token is stored on ``creds.trusted_device_token`` and
    (optionally) written back to the store the credentials came from as the
    top-level ``trustedDeviceToken`` key, exactly where the CLI keeps it, as
    long as that store still holds this login (same access token) and can be
    read; otherwise that is logged and the token serves from memory.
    ``after_refusal`` (a refused call prompted this) also stamps the time,
    which the client's guard against re-enrolling reads. Returns ``None``
    when no token came back (the server declined, e.g. the org does not use
    Trusted Devices, or the request failed), so callers can fall through to
    the original error.
    """
    import platform
    import socket

    if not creds.access_token:
        raise CredentialsError("cannot enroll a trusted device without an access token")

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        name = display_name or f"claude-rc-api on {socket.gethostname()} · {platform.system().lower()}"
        resp = client.post(
            base_url.rstrip("/") + TRUSTED_DEVICES_PATH,
            headers={
                "Authorization": f"Bearer {creds.access_token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "User-Agent": f"claude-rc-api-python/{__version__}",
            },
            json={"display_name": name},
            timeout=10.0,
        )
    except httpx.HTTPError:
        return None
    finally:
        if owns_client:
            client.close()

    if resp.status_code not in (200, 201):
        return None
    try:
        token = resp.json().get("device_token")
    except (ValueError, AttributeError):
        token = None
    if not token or not isinstance(token, str):
        return None

    creds.trusted_device_token = token  # first: an unsaved token still serves this process
    if persist:
        try:  # never raise past here: the device exists now, and its owner has been emailed
            data = _read_document(creds)
            if data is not None:
                if not _holds_login(data, creds):
                    raise CredentialsError("the store no longer holds the login that enrolled")
                data["trustedDeviceToken"] = token
                if after_refusal:
                    data[_ENROLLED_AFTER_REFUSAL] = _now_ms()
                else:
                    data.pop(_ENROLLED_AFTER_REFUSAL, None)
                _write_document(creds, data)
        except CredentialsError as exc:
            _log.warning("trusted-device token kept in memory only: %s", exc)
    return token


def _holds_login(data: dict, creds: OAuthCredentials) -> bool:
    """Whether a stored document's login is the one ``creds`` hold (the CLI
    compares access tokens the same way before it writes for a login)."""
    block = data.get("claudeAiOauth")
    return isinstance(block, dict) and block.get("accessToken") == creds.access_token


def _stored_device_token(creds: OAuthCredentials) -> tuple[Optional[str], bool]:
    """The store's ``trustedDeviceToken`` and whether ``creds`` may enroll to
    replace it: only for the login the store holds (not after a sign-out, or
    for one a sign-in replaced), and, as the CLI guards against an enrollment
    loop, not while the token is one a refusal enrolled less than a day ago
    or its stamp is unusable."""
    data = _read_document(creds) or {}
    token = data.get("trustedDeviceToken")
    if not _holds_login(data, creds):
        return token, False
    if not token or _ENROLLED_AFTER_REFUSAL not in data:
        return token, True
    stamp = data[_ENROLLED_AFTER_REFUSAL]
    if not isinstance(stamp, int) or isinstance(stamp, bool) or not 0 < stamp <= _now_ms():
        return token, False
    return token, _now_ms() - stamp >= _REFUSAL_ENROLL_COOLDOWN_MS


def org_uuid_from_profile(body: Any) -> Optional[str]:
    """``organization.uuid`` out of a ``GET /api/oauth/profile`` reply (the
    CLI's last-resort org lookup; needs the ``user:profile`` scope)."""
    org = body.get("organization") if isinstance(body, dict) else None
    uuid = org.get("uuid") if isinstance(org, dict) else None
    return uuid if isinstance(uuid, str) and uuid else None


# --- cswap (claude-swap) slots ---------------------------------------------
# cc-proxy's mitmproxy addon owns the slot logic (`slot_token` in
# scripts/account_inject.py). That module wires mitmproxy addons at import, so
# instead of importing it whole we compile just `slot_token` (and the constants
# it reads) out of its source: one implementation, no side effects.
_SLOT_TOKEN_NAMES = {"slot_token", "MARGIN_MS", "SETUP_TOKEN_REREAD_MS"}


def _account_inject_path() -> Optional[Path]:
    """cc-proxy's scripts/account_inject.py: inside the installed ``cc_proxy``
    wheel (``cc_proxy/scripts``), else next to an editable cc-proxy checkout
    (``python/cc_proxy`` -> ``scripts/``)."""
    try:
        spec = importlib.util.find_spec("cc_proxy")
    except (ImportError, ValueError):
        spec = None
    for root in (spec.submodule_search_locations or []) if spec else []:
        for candidate in (Path(root) / "scripts", Path(root).resolve().parents[1] / "scripts"):
            if (candidate / "account_inject.py").exists():
                return candidate / "account_inject.py"
    return None


def load_slot_token() -> Callable[[str], tuple[str, float]]:
    """cc-proxy's ``slot_token(slot) -> (access_token, expires_at_ms)``.

    Reads the live default login as-is and refreshes any other slot only
    through cswap's ``consume_backup_grant`` (cswap owns every refresh grant;
    nothing here writes credentials). Needs ``claude_swap`` importable when
    called."""
    path = _account_inject_path()
    if path is None:
        raise CredentialsError("cswap slots need cc-proxy's scripts/account_inject.py (repo checkout or cc-proxy wheel)")
    tree = ast.parse(path.read_text(), str(path))
    keep = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _SLOT_TOKEN_NAMES:
            keep.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in _SLOT_TOKEN_NAMES for t in node.targets
        ):
            keep.append(node)
    namespace: dict[str, Any] = {"time": time}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), namespace)
    if "slot_token" not in namespace:
        raise CredentialsError(f"{path} defines no slot_token")
    return namespace["slot_token"]


def cswap_current_slot() -> Optional[str]:
    """cswap's live account (the login Claude Code itself uses — Keychain on
    macOS), or ``None`` when cswap is not installed or has no account."""
    try:
        from claude_swap.switcher import ClaudeAccountSwitcher
    except ImportError:
        return None
    slot = ClaudeAccountSwitcher().current_account_number()
    return None if slot is None else str(slot)

def cswap_credentials(slot: str, slot_token: Callable[[str], tuple[str, float]] | None = None) -> OAuthCredentials:
    """An access-token-only :class:`OAuthCredentials` for a cswap slot (no
    refresh token: the client re-asks cswap when it expires)."""
    try:
        token, expires_at = (slot_token or load_slot_token())(str(slot))
    except ImportError as exc:
        raise CredentialsError(f"cswap slot {slot}: claude_swap is not installed ({exc})") from exc
    except ValueError as exc:
        raise CredentialsError(str(exc)) from exc
    return OAuthCredentials(access_token=token, expires_at_ms=int(expires_at) if expires_at else None)


# --- one resolver for the client --------------------------------------------
@dataclass
class AuthSource:
    """What a client authenticates with, and how to re-read it.

    Exactly one of ``credentials`` (Bearer OAuth) or ``jar`` (claude.ai cookies,
    :class:`~claude_rc.webauth.CookieJar`) is set; a bare ``session_key`` is
    turned into a one-cookie jar. ``reload`` re-reads a bearer source
    (credentials file, cswap slot) for a rotated or expired token; a jar
    re-reads itself (:meth:`~claude_rc.webauth.CookieJar.reread`). ``origin``
    names where it came from, for messages (never the secret itself).
    """

    credentials: Optional[OAuthCredentials] = None
    session_key: Optional[str] = field(default=None, repr=False)
    origin: str = ""
    reload: Optional[Callable[[], OAuthCredentials]] = None
    jar: Any = None

    def __post_init__(self) -> None:
        from .webauth import EXPLICIT, CookieJar

        if self.credentials is not None and (self.jar is not None or self.session_key is not None):
            raise CredentialsError("choose cookie or bearer authentication, not both")
        if self.jar is None and self.session_key is not None:
            self.jar = CookieJar({"sessionKey": self.session_key}, EXPLICIT)
        if self.jar is not None:
            self.session_key = self.jar.session_key

    @property
    def is_cookie(self) -> bool:
        return self.jar is not None


def resolve_auth(
    token: str | None = None,
    *,
    credentials: OAuthCredentials | None = None,
    session_key: str | None = None,
    cswap_slot: str | int | None = None,
    credentials_path: os.PathLike | str | None = None,
    slot_token: Callable[[str], tuple[str, float]] | None = None,
) -> AuthSource:
    """Pick the client's identity. First match wins:

    1. explicit arguments: ``credentials``, ``token``, ``session_key``, ``cswap_slot``;
    2. claude.ai cookies — the primary mode whenever any source yields a
       ``sessionKey`` (:func:`claude_rc.webauth.load_jar`: env
       ``CLAUDE_AI_SESSION_KEY`` / ``CLAUDE_AI_COOKIES``, the rotated-jar state
       file, local Chrome, Chrome over CDP); the client moves to claude.ai;
    3. env ``CLAUDE_AI_ACCESS_TOKEN``, then ``CLAUDE_RC_ACCESS_TOKEN``, then the
       CLI's ``CLAUDE_CODE_OAUTH_TOKEN`` (bare tokens: no refresh);
    4. env ``CLAUDE_RC_CSWAP_SLOT``, else cswap's live account (Claude Code's own login,
       Keychain on macOS) — access token only, cswap keeps owning the refresh;
    5. the CLI's credential store (file / macOS Keychain), re-read only, never
       refreshed by the client; or explicit ``credentials_path`` pointing at a
       client-owned OAuth login (refreshed and saved).

    ``slot_token`` replaces cc-proxy's slot reader (tests).
    """
    from .webauth import load_jar

    if credentials is not None:
        if not (credentials.source_path or credentials.source_keychain):
            return AuthSource(credentials=credentials, origin="explicit credentials")
        # Re-read rotated tokens from where they came from: the path given, a
        # non-default file, else the CLI's own lookup (Keychain first on macOS).
        source = credentials.source_path if credentials.source_path != default_credentials_path() else None

        def reload_explicit() -> OAuthCredentials:
            return load_credentials(credentials_path or source)

        return AuthSource(credentials=credentials, origin="explicit credentials", reload=reload_explicit)
    if token:
        return AuthSource(credentials=OAuthCredentials(access_token=token), origin="explicit token")
    if session_key:
        return AuthSource(session_key=session_key, origin="explicit session key")
    if credentials_path is not None:
        def reload_owned() -> OAuthCredentials:
            return load_credentials(credentials_path)
        return AuthSource(credentials=reload_owned(), origin="credentials file", reload=reload_owned)
    slot = cswap_slot if cswap_slot not in (None, "") else None
    if slot is None:
        jar = load_jar()
        if jar is not None:
            return AuthSource(jar=jar, origin=f"cookies:{jar.origin}")
        owned = client_credentials_path()
        if owned.is_file():
            def reload_owned_default() -> OAuthCredentials:
                return load_credentials(owned)
            return AuthSource(credentials=reload_owned_default(), origin="client-owned credentials file", reload=reload_owned_default)
        for env in (ENV_CLAUDE_AI_ACCESS_TOKEN, ENV_ACCESS_TOKEN, ENV_CLI_OAUTH_TOKEN):
            if os.environ.get(env):
                return AuthSource(credentials=OAuthCredentials(access_token=os.environ[env]), origin=f"env:{env}")
        slot = os.environ.get(ENV_CSWAP_SLOT) or cswap_current_slot()
    if slot is not None:
        def reload_slot(slot: str = str(slot)) -> OAuthCredentials:
            return cswap_credentials(slot, slot_token)

        return AuthSource(credentials=reload_slot(), origin=f"cswap slot {slot}", reload=reload_slot)

    def reload_file() -> OAuthCredentials:
        return load_credentials(credentials_path)

    return AuthSource(credentials=reload_file(), origin="credentials file", reload=reload_file)
