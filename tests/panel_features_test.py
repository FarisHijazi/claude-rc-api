"""Isolated browser acceptance for selection, local actions and CAS editing.

These fixtures do not qualify worker behavior; scripts/claude-rc-panel-smoke does.
"""
import base64
import threading
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from claude_rc import RemoteControlClient
from claude_rc.webui import _Handler, _RCServer
from test_cli import SID, Server, webui


@pytest.fixture
def panel_page():
    playwright = pytest.importorskip('playwright.sync_api')
    rc = RemoteControlClient(token='toy', org_uuid='org', transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    server = _RCServer(('127.0.0.1', 0), _Handler, rc, False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/usr/bin/google-chrome', args=['--no-sandbox'], headless=True)
            try:
                page = browser.new_page()
                yield page, f'http://127.0.0.1:{server.server_port}'
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        rc.close()


def test_create_selection_policy_pool_revision_and_outcome_branch(panel_page):
    page, base = panel_page
    created = []
    def route(request):
        url = urlparse(request.request.url)
        path = url.path
        if path == '/api/sessions' and request.request.method == 'POST':
            created.append(request.request.post_data_json)
            return request.fulfill(json={'id': 'cse_toy'})
        data = {
            '/api/sessions': {'sessions': []},
            '/api/whoami': {'logged_in': True},
            '/api/models': {'model_selector_config': [{'models': [{'id': 'toy-model', 'name': 'Toy model'}]}]},
            '/api/permission-policy': {'auto_permissions': {'allowed': True}, 'bypass_permissions': {'allowed': False}},
            '/api/environments': {'environments': [{'environment_id': 'env_toy', 'name': 'Toy environment'}]},
            '/api/pools': {'pools': [{'pool_id': 'ccpool_toy', 'name': 'Toy pool'}]},
            '/api/repos': {'repos': [{'repo': {'full_name': 'acme/toy'}}], 'is_complete': True},
            '/api/branches': {'branches': [{'name': 'feature/toy'}]},
            '/api/sessions/cse_toy': {'id': 'cse_toy'},
            '/api/sessions/cse_toy/events': {'events': [], 'next_cursor': None},
        }.get(path)
        if path.endswith('/stream'):
            return request.fulfill(body=': connected\n\n', content_type='text/event-stream')
        request.fulfill(json=data or {})
    page.route('**/api/**', route)
    page.goto(base)
    page.locator('#new-session').click()
    page.wait_for_function("!document.getElementById('submit-create').disabled")
    form = page.locator('#create-form')
    assert form.locator('[name=model] option').all_text_contents() == ['Toy model']
    assert form.locator('[name=permission_mode] option').all_text_contents() == ['default', 'plan', 'acceptEdits', 'auto']
    assert 'bypassPermissions' not in page.locator('#sel-perm').inner_text()
    form.locator('[name=env]').select_option('ccpool_toy')
    form.locator('[name=repo]').select_option('acme/toy')
    page.wait_for_function("document.querySelector('#source-revisions option') !== null")
    form.locator('[name=revision]').fill('feature/toy')
    form.locator('[name=branch]').fill('claude/owned-outcome')
    form.locator('[name=permission_mode]').select_option('auto')
    form.locator('[name=effort]').select_option('low')
    form.locator('[name=prompt]').fill('Toy prompt')
    form.locator('[type=submit]').click()
    page.wait_for_function('S.selectedId === "cse_toy"')
    assert created == [{'title': '', 'env': 'ccpool_toy', 'repo': 'acme/toy', 'revision': 'feature/toy',
                        'branch': 'claude/owned-outcome', 'model': 'toy-model', 'permission_mode': 'auto',
                        'effort': 'low', 'prompt': 'Toy prompt'}]


def test_file_editor_keeps_cas_token_and_retains_edits_on_conflict(panel_page):
    page, base = panel_page
    writes = []
    def route(request):
        path = urlparse(request.request.url).path
        if path.endswith('/sync_file'):
            writes.append(request.request.post_data_json)
            return request.fulfill(status=409, json={'error': 'CAS conflict'}) if len(writes) == 2 else request.fulfill(json={'content_sha256': 'b' * 64})
        if path.endswith('/file'):
            return request.fulfill(json={'content': base64.b64encode('hello العربية'.encode()).decode(), 'content_sha256': 'a' * 64})
        request.fulfill(json={'sessions': [], 'model_selector_config': [], 'logged_in': True})
    page.route('**/api/**', route)
    page.goto(base)
    page.evaluate("S.selectedId='cse_toy'; document.getElementById('pane-body').style.display='flex'")
    page.on('dialog', lambda dialog: dialog.accept('/mnt/user-data/working/toy.txt') if dialog.type == 'prompt' else dialog.accept())
    page.locator('#btn-files').click()
    page.locator('#file-content').wait_for(state='visible')
    assert page.locator('#file-content').input_value() == 'hello العربية'
    assert page.locator('#file-write-path').input_value() == '/working/toy.txt'
    page.locator('#file-content').fill('updated')
    page.locator('#save-file').click()
    page.wait_for_function("document.getElementById('file-save-status').textContent === 'Saved with CAS.'")
    page.locator('#file-content').fill('unsaved conflict')
    page.locator('#save-file').click()
    page.wait_for_function("document.getElementById('file-save-status').textContent.includes('edits retained')")
    assert page.locator('#file-content').input_value() == 'unsaved conflict'
    assert [row['if_match_sha256'] for row in writes] == ['a' * 64, 'b' * 64]
    assert all(row['path'] == '/working/toy.txt' for row in writes)


def test_create_adapter_preserves_pool_revision_branch_and_mode(webui):
    server = Server({('POST', '/v1/code/sessions'): {'id': SID}})
    response = httpx.post(webui(server) + '/api/sessions', trust_env=False, json={
        'env': 'ccpool_owned', 'repo': 'acme/toy', 'revision': 'branch/source', 'branch': 'claude/outcome',
        'model': 'toy-model', 'effort': 'low', 'permission_mode': 'plan', 'prompt': 'Toy prompt'})
    assert response.status_code == 200
    import json
    body = json.loads(server.requests[-1].content)
    assert body['self_hosted_runner_pool_id'] == 'ccpool_owned'
    assert body['config']['sources'][0]['revision'] == 'branch/source'
    assert 'claude/outcome' in json.dumps(body['config']['outcomes'])
    assert body['config']['effort_level'] == 'low'
    assert any(event['payload'].get('request', {}).get('mode') == 'plan' for event in body['events'])


@pytest.mark.parametrize('name,method,result', [
    ('config', 'get_settings', {'applied': {'model': 'toy-model'}}),
    ('settings', 'get_settings', {'applied': {'model': 'toy-model'}}),
    ('skills', 'list_account_skills', {'skills': []}),
    ('usage-credits', 'get_account_usage', {'five_hour': {'utilization': 3}}),
])
def test_panel_local_menu_has_handler_and_never_posts_chat(webui, monkeypatch, name, method, result):
    calls = []
    def local(self, *args):
        calls.append(args)
        return result
    monkeypatch.setattr(RemoteControlClient, method, local)
    server = Server()
    response = httpx.post(webui(server) + f'/api/sessions/{SID}/command', trust_env=False, json={'name': name})
    assert response.status_code == 200
    assert response.json() == {'via': 'local', 'command': name, 'result': result}
    assert calls == ([(SID,)] if method == 'get_settings' else [()])
    assert not server.posted_events()


def test_success_subtype_with_error_flag_is_not_a_successful_panel_result(panel_page):
    from claude_rc.events import Event
    from claude_rc.webui import event_to_dict
    event = event_to_dict(Event(payload={'type': 'result', 'subtype': 'success', 'is_error': True}))
    assert event['is_error'] is True
    page, base = panel_page
    page.route('**/api/**', lambda route: route.fulfill(json={'sessions': []}))
    page.goto(base)
    rendered = page.evaluate('(event) => eventNode(event).textContent', event)
    assert rendered == 'turn failed'
