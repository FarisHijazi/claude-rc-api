"""Explicit, isolated PKCE enrollment helpers; never invoked by auth discovery.

Endpoint/client/scope literals are from Claude Code 2.1.312. The grant exchange
is not live-verified: see the group01 devlog. Browser opening and callback
collection belong to the caller, so importing this never starts a listener.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .credentials import (
    CLAUDE_CODE_CLIENT_ID, OAUTH_BETA, OAUTH_TOKEN_URL, CredentialsError,
    OAuthCredentials, _now_ms, _write_document, credential_lock, shared_credentials,
)

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = ("user:profile", "user:inference", "user:sessions:claude_code")


@dataclass
class PKCELogin:
    authorization_url: str = field(repr=False)
    verifier: str = field(repr=False)
    state: str = field(repr=False)
    path: Path
    created_at: float = field(default_factory=time.monotonic)
    consumed: bool = False


def begin_login(path: str | Path) -> PKCELogin:
    """Build a login URL for a NEW client-owned file; do not open it implicitly."""
    path = Path(path).expanduser().resolve()
    if shared_credentials(OAuthCredentials("", source_path=path)):
        raise CredentialsError("login destination is a shared credential store")
    if path.exists():
        raise CredentialsError("login destination already exists; use a new client-owned path")
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode({"code": "true", "client_id": CLAUDE_CODE_CLIENT_ID,
                       "response_type": "code", "redirect_uri": REDIRECT_URI,
                       "scope": " ".join(SCOPES), "code_challenge": challenge,
                       "code_challenge_method": "S256", "state": state})
    return PKCELogin(AUTHORIZE_URL + "?" + query, verifier, state, path)


def finish_login(login: PKCELogin, callback_url: str, *, client: httpx.Client | None = None,
                 direct: bool = False) -> OAuthCredentials:
    """Exchange a state-checked callback and atomically create the isolated file.

    No CLI/Keychain/cswap reads, no device enrollment. The caller must explicitly
    authorize the URL from begin_login first. Never log the callback URL.
    """
    if login.consumed or time.monotonic() - login.created_at > 600:
        raise CredentialsError("PKCE login expired or was already consumed")
    parsed = urlsplit(callback_url)
    expected = urlsplit(REDIRECT_URI)
    if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path):
        raise CredentialsError("unexpected OAuth callback destination")
    query = parse_qs(parsed.query)
    states, codes = query.get("state", []), query.get("code", [])
    if len(states) != 1 or not secrets.compare_digest(states[0], login.state) or len(codes) != 1:
        raise CredentialsError("invalid OAuth callback state or code")
    creds = OAuthCredentials("", source_path=login.path, client_id=CLAUDE_CODE_CLIENT_ID)
    if shared_credentials(creds):
        raise CredentialsError("login destination is a shared credential store")
    with credential_lock(login.path):
        if login.path.exists():
            raise CredentialsError("login destination already exists")
        login.consumed = True
        own = client is None
        http = client or httpx.Client(timeout=30, trust_env=not direct)
        try:
            resp = http.post(OAUTH_TOKEN_URL, headers={"anthropic-beta": OAUTH_BETA}, json={
                "grant_type": "authorization_code", "code": codes[0], "state": login.state,
                "client_id": CLAUDE_CODE_CLIENT_ID, "redirect_uri": REDIRECT_URI,
                "code_verifier": login.verifier,
            })
        finally:
            if own:
                http.close()
        if resp.status_code != 200:
            raise CredentialsError(f"OAuth enrollment failed: HTTP {resp.status_code}")
        body = resp.json()
        account = (body.get("account") or {}).get("uuid")
        if not account or not body.get("access_token") or not body.get("refresh_token"):
            raise CredentialsError("OAuth enrollment did not return an identified refreshable grant")
        creds.access_token = body["access_token"]
        creds.refresh_token = body["refresh_token"]
        creds.expires_at_ms = _now_ms() + int(body["expires_in"]) * 1000
        creds.scopes = body.get("scope", " ".join(SCOPES)).split()
        creds.account_uuid = account
        _write_document(creds, {"claudeAiOauth": creds.to_block(), "claudeRcAccountUuid": account})
        return creds
