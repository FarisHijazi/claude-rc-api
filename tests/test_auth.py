"""Auth-foundation contracts. Offline; fixtures never load a real browser/login."""
import io
import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from claude_rc import credentials as cr, webauth
from claude_rc.client import APIError, RemoteControlClient
from claude_rc.login import begin_login, finish_login, REDIRECT_URI


def stored(path, account="account-a", token="old", refresh="grant", expiry=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": refresh,
                                               "expiresAt": expiry}, "claudeRcAccountUuid": account}))
    return cr.load_credentials(path)


def browser(monkeypatch, **cookies):
    jar = webauth.CookieJar({"sessionKey": "old", "lastActiveOrg": "org-a", **cookies}, webauth.CDP)
    monkeypatch.setattr(webauth, "load_jar", lambda: jar)
    return jar


@pytest.mark.parametrize("status", [401, 403])
def test_cookie_failure_rereads_once_and_pins_account_and_org(monkeypatch, status):
    jar = browser(monkeypatch)
    reads, requests = [], []
    def reread():
        reads.append(True)
        return webauth.CookieJar({"sessionKey": "fresh", "lastActiveOrg": "org-b"}, webauth.CDP)
    monkeypatch.setattr(webauth, "from_cdp", reread)
    def handler(req):
        requests.append(req)
        if req.url.path == "/api/account":
            return httpx.Response(200, json={"uuid": "account-a"})
        return httpx.Response(200 if "fresh" in req.headers["cookie"] else status, json={"data": []})
    with RemoteControlClient(transport=httpx.MockTransport(handler)) as rc:
        rc.list_sessions(limit=1)
        assert rc.org_uuid == "org-a" and rc._account_uuid == "account-a"
    calls = [r for r in requests if r.url.path.endswith("/sessions")]
    assert len(calls) == 2 and len(reads) == 1
    assert all(r.headers["x-organization-uuid"] == "org-a" for r in calls)


def test_changed_browser_account_is_rejected_and_rolled_back(monkeypatch):
    jar = browser(monkeypatch)
    monkeypatch.setattr(webauth, "from_cdp", lambda: webauth.CookieJar({"sessionKey": "other"}, webauth.CDP))
    actions = []
    def handler(req):
        if req.url.path == "/api/account":
            account = "account-b" if "other" in req.headers["cookie"] else "account-a"
            return httpx.Response(200, json={"uuid": account})
        actions.append(req)
        return httpx.Response(401, json={})
    with RemoteControlClient(transport=httpx.MockTransport(handler)) as rc:
        with pytest.raises(cr.CredentialsError, match="identity changed"):
            rc.list_sessions(limit=1)
        assert rc.org_uuid == "org-a" and jar.session_key == "old"
    assert len(actions) == 1


def test_repeated_auth_failure_does_not_loop(monkeypatch):
    browser(monkeypatch)
    reads, calls = [], []
    def reread():
        reads.append(True)
        return webauth.CookieJar({"sessionKey": "fresh"}, webauth.CDP)
    monkeypatch.setattr(webauth, "from_cdp", reread)
    def handler(req):
        if req.url.path == "/api/account":
            return httpx.Response(200, json={"uuid": "account-a"})
        calls.append(req)
        return httpx.Response(401, json={})
    with RemoteControlClient(transport=httpx.MockTransport(handler)) as rc:
        with pytest.raises(APIError):
            rc.list_sessions(limit=1)
    assert len(calls) == 2 and len(reads) == 1


@pytest.mark.parametrize("kind", ["cloudflare", "policy", "trusted_device"])
def test_non_auth_failures_never_reread(monkeypatch, kind):
    browser(monkeypatch)
    monkeypatch.setattr(webauth, "from_cdp", lambda: pytest.fail("not an auth failure"))
    def handler(req):
        if req.url.path == "/api/account":
            return httpx.Response(200, json={"uuid": "account-a"})
        if kind == "cloudflare":
            return httpx.Response(403, headers={"cf-mitigated": "challenge"})
        error = {"type": "permission_error"} if kind == "policy" else {"resource": "untrusted_device"}
        return httpx.Response(403, json={"error": error})
    with RemoteControlClient(transport=httpx.MockTransport(handler)) as rc:
        if kind == "cloudflare":
            with pytest.raises(webauth.CloudflareChallenge):
                rc.list_sessions(limit=1)
        else:
            with pytest.raises(APIError) as exc:
                rc.list_sessions(limit=1)
            assert exc.value.failure_kind == kind


@pytest.mark.parametrize("location", [".claude/.credentials.json", ".claude-swap-backup/a.json",
                                       ".local/share/claude-swap/a.json", ".local/state/claude-codex/a.json"])
def test_shared_paths_and_symlinks_never_refresh_or_write(tmp_path, location):
    path = Path.home() / location
    creds = stored(path)
    before = path.read_bytes()
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("shared grant spent"))) as http:
        for source in (creds, cr.load_credentials(alias)):
            with pytest.raises(cr.CredentialsError, match="read-only"):
                cr.refresh_credentials(source, client=http)
    assert path.read_bytes() == before
    assert not Path(str(path) + ".lock").exists()


def test_shared_401_does_not_refresh(monkeypatch):
    path = cr.default_credentials_path()
    stored(path, expiry=9999999999999)
    before, seen = path.read_bytes(), []
    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(401, json={})
    with RemoteControlClient(credentials_path=str(path), org_uuid="org-a", transport=httpx.MockTransport(handler)) as rc:
        with pytest.raises(APIError):
            rc.list_sessions(limit=1)
    assert seen == ["/v1/code/sessions"] and path.read_bytes() == before


def test_explicit_path_beats_browser_and_token_environment(tmp_path, monkeypatch):
    path = tmp_path / "owned.json"
    stored(path, expiry=9999999999999)
    browser(monkeypatch)
    monkeypatch.setenv("CLAUDE_RC_ACCESS_TOKEN", "env-value")
    assert cr.resolve_auth(credentials_path=path).credentials.source_path == path


def test_owned_default_precedes_shared_sources(monkeypatch):
    monkeypatch.setattr(webauth, "load_jar", lambda: None)
    path = cr.client_credentials_path()
    stored(path)
    monkeypatch.setenv("CLAUDE_RC_ACCESS_TOKEN", "shared")
    assert cr.resolve_auth().origin == "client-owned credentials file"


def test_cookie_reread_does_not_change_source(monkeypatch):
    jar = webauth.CookieJar({"sessionKey": "a"}, webauth.CDP)
    monkeypatch.setattr(webauth, "from_cdp", lambda: None)
    monkeypatch.setattr(webauth, "from_env", lambda: pytest.fail("source changed"))
    assert jar.reread() is False


def test_owned_refresh_is_serialized_and_restart_uses_rotation(tmp_path):
    path = tmp_path / "owned.json"
    first = stored(path)
    second = cr.load_credentials(path)
    calls = []
    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda c: cr.refresh_credentials(c, client=http), [first, second]))
    assert len(calls) == 1 and all(c.access_token == "new" for c in results)
    assert cr.load_credentials(path).refresh_token == "rotated"
    assert path.stat().st_mode & 0o777 == 0o600
    seen = []
    with RemoteControlClient(credentials_path=str(path), org_uuid="org-a", transport=httpx.MockTransport(
        lambda r: seen.append(r.headers["authorization"]) or httpx.Response(200, json={"data": []})
    )) as rc:
        rc.list_sessions(limit=1)
    assert seen == ["Bearer new"]


def refresh_worker(path, barrier, counter):
    creds = cr.load_credentials(path)
    barrier.wait(timeout=10)
    def handler(req):
        with counter.get_lock():
            counter.value += 1
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert cr.refresh_credentials(creds, client=http).access_token == "new"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-lock acceptance; Windows uses msvcrt")
def test_owned_refresh_serializes_across_processes(tmp_path):
    path = tmp_path / "owned.json"
    stored(path)
    context = multiprocessing.get_context("fork")
    barrier, counter = context.Barrier(2), context.Value("i", 0)
    workers = [context.Process(target=refresh_worker, args=(path, barrier, counter)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert counter.value == 1


def test_identity_change_in_owned_store_is_rejected(tmp_path):
    path = tmp_path / "owned.json"
    stored(path, expiry=9999999999999)
    with RemoteControlClient(credentials_path=str(path), org_uuid="org-a") as rc:
        stored(path, account="account-b", token="other", refresh="other")
        with pytest.raises(cr.CredentialsError, match="identity changed"):
            rc._reload_credentials()


def test_direct_option_ignores_environment_without_mutating_it(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", "/does/not/exist")
    monkeypatch.setenv("CLAUDE_AI_PROXY", "http://127.0.0.1:2")
    before = dict(os.environ)
    with RemoteControlClient(token="test", org_uuid="org-a", direct=True):
        pass
    assert dict(os.environ) == before


def test_multipart_replay_resends_complete_bytes(monkeypatch):
    monkeypatch.setattr("claude_rc.client.time.sleep", lambda n: None)
    bodies = []
    def handler(req):
        bodies.append(req.read())
        return httpx.Response(429 if len(bodies) == 1 else 200, json={})
    with RemoteControlClient(token="test", org_uuid="org-a", transport=httpx.MockTransport(handler)) as rc:
        rc._send("POST", "/upload", files={"file": ("hello.txt", b"complete-upload")})
        with pytest.raises(ValueError, match="bytes or str"):
            rc._send("POST", "/upload", files={"file": ("hello.txt", io.BytesIO(b"x"))})
    assert len(bodies) == 2 and all(b"complete-upload" in b for b in bodies)


def test_credential_and_error_reprs_do_not_leak_secrets():
    creds = cr.OAuthCredentials("secret-access", "secret-refresh", trusted_device_token="secret-device")
    auth = cr.AuthSource(credentials=creds)
    error = APIError(401, "secret-response")
    assert "secret" not in repr(creds) + repr(auth) + str(error) + repr(error)


def test_pkce_state_expiry_isolation_and_explicit_exchange(tmp_path):
    path = tmp_path / "owned.json"
    login = begin_login(path)
    query = parse_qs(urlsplit(login.authorization_url).query)
    assert query["code_challenge_method"] == ["S256"] and query["state"] == [login.state]
    assert login.verifier not in repr(login)
    bad = REDIRECT_URI + "?" + urlencode({"code": "code", "state": "wrong"})
    with pytest.raises(cr.CredentialsError, match="state"):
        finish_login(login, bad)
    callback = REDIRECT_URI + "?" + urlencode({"code": "code", "state": login.state})
    seen = []
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"access_token": "owned", "refresh_token": "owned-grant", "expires_in": 3600,
                                        "account": {"uuid": "account-a"}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        creds = finish_login(login, callback, client=http)
    assert seen[0]["code_verifier"] == login.verifier
    assert creds.source_path == path and cr.load_credentials(path).account_uuid == "account-a"
    with pytest.raises(cr.CredentialsError, match="consumed"):
        finish_login(login, callback)
    with pytest.raises(cr.CredentialsError, match="shared"):
        begin_login(cr.default_credentials_path())


def test_pkce_expired_and_wrong_destination_are_rejected(tmp_path):
    login = begin_login(tmp_path / "owned.json")
    with pytest.raises(cr.CredentialsError, match="destination"):
        finish_login(login, "https://example.com/callback?code=x")
    login.created_at -= 601
    with pytest.raises(cr.CredentialsError, match="expired"):
        finish_login(login, REDIRECT_URI)


def test_default_request_never_enrolls_a_device(tmp_path):
    path = tmp_path / "owned.json"
    stored(path, expiry=9999999999999)
    requests = []
    def handler(req):
        requests.append(req.url.path)
        return httpx.Response(403, json={"error": {"resource": "untrusted_device"}})
    with RemoteControlClient(credentials_path=str(path), org_uuid="org-a", transport=httpx.MockTransport(handler)) as rc:
        with pytest.raises(APIError):
            rc.list_sessions(limit=1)
    assert requests == ["/v1/code/sessions"]


def test_mixed_auth_is_rejected():
    with pytest.raises(cr.CredentialsError, match="not both"):
        cr.AuthSource(credentials=cr.OAuthCredentials("test"), session_key="test")


def test_failed_temp_creation_preserves_refreshed_pair(tmp_path, monkeypatch):
    path = tmp_path / "owned.json"
    creds = stored(path)
    def denied(**kwargs):
        raise PermissionError("cannot create temporary file")
    monkeypatch.setattr(cr.tempfile, "mkstemp", denied)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600}
    ))) as http:
        new = cr.refresh_credentials(creds, client=http)
    assert new.access_token == "new" and new.stale_refresh_token == "grant"
    assert cr.load_credentials(path).access_token == "old"


@pytest.mark.parametrize("refresh", [None, "different-grant"])
def test_unidentified_bearer_change_fails_closed(refresh):
    old = cr.OAuthCredentials("old", refresh_token="original-grant")
    fresh = cr.OAuthCredentials("new", refresh_token=refresh)
    auth = cr.AuthSource(credentials=old, reload=lambda: fresh)
    with RemoteControlClient(auth=auth, org_uuid="org-a") as rc:
        with pytest.raises(cr.CredentialsError, match="account continuity"):
            rc._reload_credentials()
        assert rc._creds is old


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("fresh_valid", [True, False])
def test_initial_stale_cookie_recovers_once_before_any_operation(monkeypatch, status, fresh_valid):
    browser(monkeypatch)
    reads, requests = [], []
    def reread():
        reads.append(True)
        return webauth.CookieJar({"sessionKey": "fresh"}, webauth.CDP)
    monkeypatch.setattr(webauth, "from_cdp", reread)
    def handler(req):
        requests.append(req.url.path)
        if req.url.path == "/api/account":
            if fresh_valid and "fresh" in req.headers["cookie"]:
                return httpx.Response(200, json={"uuid": "account-a"})
            return httpx.Response(status, json={"error": {"type": "permission_error", "message": "Invalid authorization"}})
        return httpx.Response(200, json={"data": []})
    with RemoteControlClient(transport=httpx.MockTransport(handler)) as rc:
        if fresh_valid:
            rc.list_sessions(limit=1)
            assert rc._account_uuid == "account-a" and rc.org_uuid == "org-a"
        else:
            with pytest.raises(APIError):
                rc.list_sessions(limit=1)
    assert len(reads) == 1
    assert requests == ["/api/account", "/api/account"] + (["/v1/code/sessions"] if fresh_valid else [])


@pytest.mark.parametrize("live", [True, False])
@pytest.mark.parametrize("expiry", [1, 9999999999999, None])
def test_cswap_reader_never_spends_grants(monkeypatch, live, expiry):
    import sys
    from types import ModuleType, SimpleNamespace

    raw = {"accessToken": "synthetic-access", "refreshToken": "never-spend"}
    if expiry is not None:
        raw["expiresAt"] = expiry
    reads = []
    switcher = SimpleNamespace(
        current_account_number=lambda: "2" if live else "1",
        _read_credentials=lambda: reads.append("live") or raw,
        _get_sequence_data=lambda: {"accounts": {"2": {"email": "user@example.com"}}},
        _read_account_credentials_ex=lambda slot, email: (reads.append("backup") or raw, None),
        consume_backup_grant=lambda *args: pytest.fail("spent shared refresh grant"),
    )
    package, module = ModuleType("claude_swap"), ModuleType("claude_swap.switcher")
    package.oauth = SimpleNamespace(extract_oauth_data=lambda data: data)
    module.ClaudeAccountSwitcher = lambda: switcher
    monkeypatch.setitem(sys.modules, "claude_swap", package)
    monkeypatch.setitem(sys.modules, "claude_swap.switcher", module)
    if expiry == 1:
        with pytest.raises(cr.CredentialsError, match="owner must renew"):
            cr.cswap_credentials("2")
    else:
        result = cr.cswap_credentials("2")
        assert result.access_token == "synthetic-access" and result.refresh_token is None
    assert reads == ["live" if live else "backup"]


def test_same_legacy_refresh_grant_proves_account_lineage():
    old = cr.OAuthCredentials("old", refresh_token="same-grant")
    fresh = cr.OAuthCredentials("new", refresh_token="same-grant")
    auth = cr.AuthSource(credentials=old, reload=lambda: fresh)
    with RemoteControlClient(auth=auth, org_uuid="org-a") as rc:
        assert rc._reload_credentials() and rc._creds is fresh


