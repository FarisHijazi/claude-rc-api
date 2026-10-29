"""On-demand recovery and strict enrollment boundaries; no real credentials."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import threading
import runpy
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from claude_rc import credentials as cr, webauth
from claude_rc.client import APIError, RemoteControlClient
from claude_rc.login import REDIRECT_URI, begin_login, finish_login


@pytest.mark.parametrize('changed', [False, True])
def test_on_demand_recovery_verifies_account_even_when_cookie_unchanged(monkeypatch, changed):
    jar = webauth.CookieJar({'sessionKey': 'old', 'lastActiveOrg': 'org-a'}, webauth.CDP)
    monkeypatch.setattr(webauth, 'load_jar', lambda: jar)
    reads, requests = [], []
    def reread():
        reads.append(True)
        return webauth.CookieJar({'sessionKey': 'fresh' if changed else 'old',
                                  'lastActiveOrg': 'org-a'}, webauth.CDP)
    monkeypatch.setattr(webauth, 'from_cdp', reread)
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'uuid': 'account-a'})
    with RemoteControlClient(transport=httpx.MockTransport(handler), auto_refresh=False) as client:
        assert client.recover_browser_auth() is changed
        assert client._account_uuid == 'account-a'
        assert client.org_uuid == 'org-a'
    assert len(reads) == 1
    assert [request.url.path for request in requests] == ['/api/account']


@pytest.mark.parametrize('failure', ['account_changed', 'still_unauthorized', 'network'])
def test_on_demand_recovery_rolls_back_and_never_replays_actions(monkeypatch, failure):
    jar = webauth.CookieJar({'sessionKey': 'old', 'lastActiveOrg': 'org-a'}, webauth.CDP)
    monkeypatch.setattr(webauth, 'load_jar', lambda: jar)
    reads, actions = [], []
    def reread():
        reads.append(True)
        return webauth.CookieJar({'sessionKey': 'fresh', 'lastActiveOrg': 'org-b'}, webauth.CDP)
    monkeypatch.setattr(webauth, 'from_cdp', reread)
    def handler(request):
        actions.append(request.url.path)
        if failure == 'network':
            raise httpx.ConnectError('unavailable', request=request)
        if failure == 'still_unauthorized':
            return httpx.Response(401, json={})
        return httpx.Response(200, json={'uuid': 'account-b'})
    with RemoteControlClient(transport=httpx.MockTransport(handler)) as client:
        client._account_uuid = 'account-a'
        with pytest.raises((cr.CredentialsError, APIError, httpx.ConnectError)):
            client.recover_browser_auth()
        assert jar.cookies == {'sessionKey': 'old', 'lastActiveOrg': 'org-a'}
        assert client._account_uuid == 'account-a' and client.org_uuid == 'org-a'
    assert len(reads) == 1 and actions == ['/api/account']


def test_on_demand_recovery_does_not_switch_bearer_to_browser(monkeypatch):
    monkeypatch.setattr(webauth, 'from_cdp', lambda: pytest.fail('unexpected discovery'))
    with RemoteControlClient(token='synthetic', org_uuid='org-a') as client:
        with pytest.raises(cr.CredentialsError, match='cookie source'):
            client.recover_browser_auth()


@pytest.mark.parametrize('extra', ['&state=', '&code=', '&error=access_denied', '#fragment'])
def test_pkce_rejects_ambiguous_or_error_callbacks_before_exchange(tmp_path, extra):
    login = begin_login(tmp_path / 'owned.json')
    callback = REDIRECT_URI + '?' + urlencode({'code': 'synthetic', 'state': login.state}) + extra
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('unexpected exchange'))) as http:
        with pytest.raises(cr.CredentialsError):
            finish_login(login, callback, client=http)
    assert not login.consumed and not login.path.exists()


@pytest.mark.parametrize('payload', [[], {'account': []}, {'account': {'uuid': 'account-a'},
    'access_token': 'synthetic', 'refresh_token': 'synthetic', 'expires_in': 'invalid'}])
def test_pkce_rejects_malformed_success_without_persisting(tmp_path, payload):
    login = begin_login(tmp_path / 'owned.json')
    callback = REDIRECT_URI + '?' + urlencode({'code': 'synthetic', 'state': login.state})
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as http:
        with pytest.raises(cr.CredentialsError, match='grant'):
            finish_login(login, callback, client=http)
    assert login.consumed and not login.path.exists()


@pytest.mark.parametrize('success', [False, True])
def test_explicit_browser_helper_owns_only_its_tab_and_redacts_failures(monkeypatch, tmp_path, capsys, success):
    script = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/claude-rc-auth'))
    calls = []
    class Page:
        url = ''
        def goto(self, url, **kwargs):
            calls.append('navigate_owned_tab')
            state = parse_qs(urlsplit(url).query)['state'][0]
            self.url = REDIRECT_URI + '?' + urlencode({'code': 'secret-code', 'state': state})
        def wait_for_url(self, predicate, **kwargs):
            assert predicate(self.url)
            if not success:
                raise TimeoutError('secret-code and secret-state must not be logged')
        def close(self):
            calls.append('close_owned_tab')
    page = Page()
    class Context:
        def new_page(self):
            calls.append('new_tab')
            return page
    class Chromium:
        def connect_over_cdp(self, *args, **kwargs):
            return SimpleNamespace(contexts=[Context()])
    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=Chromium())
        def __exit__(self, *args):
            pass
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=Playwright))
    def finish(login, url, **kwargs):
        calls.append('finish_login')
        assert login.state in url and kwargs['direct']
    script['enroll'].__globals__['finish_login'] = finish
    if success:
        script['enroll'](tmp_path / 'owned.json', cdp_url='http://localhost:9222', timeout=1, direct=True)
    else:
        with pytest.raises(cr.CredentialsError, match='TimeoutError') as error:
            script['enroll'](tmp_path / 'owned.json', cdp_url='http://localhost:9222', timeout=1, direct=True)
        assert 'secret-' not in str(error.value)
    assert calls == ['new_tab', 'navigate_owned_tab', *(['finish_login'] if success else []), 'close_owned_tab']
    output = capsys.readouterr()
    assert 'secret-' not in output.out + output.err


def test_same_pkce_attempt_cannot_exchange_twice_after_concurrent_failure(monkeypatch, tmp_path):
    from claude_rc import login as module
    login = begin_login(tmp_path / 'owned.json')
    callback = REDIRECT_URI + '?' + urlencode({'code': 'synthetic', 'state': login.state})
    barrier, exchanges = threading.Barrier(2), []
    original_lock = cr.credential_lock
    @contextmanager
    def simultaneous_lock(path):
        barrier.wait(timeout=5)
        with original_lock(path):
            yield
    monkeypatch.setattr(module, 'credential_lock', simultaneous_lock)
    def handler(request):
        exchanges.append(True)
        return httpx.Response(400, json={'error': 'invalid_grant'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        def finish(_):
            with pytest.raises(cr.CredentialsError):
                finish_login(login, callback, client=http)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(finish, range(2)))
    assert len(exchanges) == 1 and login.consumed and not login.path.exists()


def test_unicode_pkce_state_rejection_is_sanitized(tmp_path):
    login = begin_login(tmp_path / 'owned.json')
    callback = REDIRECT_URI + '?' + urlencode({'code': 'synthetic', 'state': 'مرحبا'})
    with pytest.raises(cr.CredentialsError, match='state or code'):
        finish_login(login, callback)
    assert not login.consumed
