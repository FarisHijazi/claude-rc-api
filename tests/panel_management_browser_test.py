"""Rendered management and byte editing fixtures, not live qualification."""
import base64
import threading

import httpx
import pytest

from claude_rc import RemoteControlClient
from claude_rc.webui import _Handler, _RCServer


def test_rendered_management_and_binary_drafts():
    playwright = pytest.importorskip('playwright.sync_api')
    rc = RemoteControlClient(token='toy', org_uuid='toy-org',
                             transport=httpx.MockTransport(lambda request: httpx.Response(404, json={})))
    server = _RCServer(('127.0.0.1', 0), _Handler, rc, False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    seen = []
    pending_environment = []
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/usr/bin/google-chrome', args=['--no-sandbox'], headless=True)
            page = browser.new_page()
            try:
                def route(request):
                    path = request.request.url.split('/api/', 1)[1].split('?', 1)[0]
                    body = request.request.post_data_json if request.request.method == 'POST' else None
                    if body is not None:
                        seen.append((path, body))
                    responses = {
                        'sessions': {'sessions': []}, 'whoami': {'logged_in': True},
                        'models': {'model_selector_config': [{'models': [{'id': 'toy-model'}]}]},
                        'permission-policy': {},
                        'environments': {'environments': [{'environment_id': 'env_owned', 'name': 'Owned', 'description': 'old'}]},
                        'environments/env_owned': {},
                        'routines': {'routines': [{'id': 'trg_owned', 'name': 'Routine', 'enabled': False, 'run_once_at': '2030-01-01T00:00:00Z'}]},
                        'routines/trg_owned': {},
                        'network-profiles': {'profiles': []},
                        'sessions/cse_owned/file': {'content': base64.b64encode(bytes(range(256))).decode(), 'content_sha256': 'a' * 64},
                        'sessions/cse_owned/sync_file': {'content_sha256': 'b' * 64},
                    }
                    if path == 'environments' and not pending_environment:
                        pending_environment.append(request)
                        return
                    request.fulfill(json=responses.get(path, {}))
                page.route('**/api/**', route)
                page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='domcontentloaded')
                page.locator('#manage-environments').click()
                assert page.locator('#manage-status').inner_text() == 'Loading…'
                assert 'No environments' not in page.locator('#manage-content').inner_text()
                page.wait_for_timeout(50)
                pending_environment[0].fulfill(json={'environments': [{'environment_id': 'env_owned', 'name': 'Owned', 'description': 'old'}]})
                page.wait_for_function("document.getElementById('manage-status').textContent === 'Loaded.'")
                card = page.locator('[data-resource-id=env_owned]')
                card.locator('summary').click()
                card.locator('[name=description]').fill('new')
                card.get_by_role('button', name='Save', exact=True).click()
                page.wait_for_function("document.getElementById('manage-status').textContent.startsWith('Saved.')")
                assert seen[-1] == ('environments/env_owned', {'name': 'Owned', 'description': 'new'})
                page.locator('#manage-close').click()
                page.locator('#manage-routines').click()
                page.wait_for_selector('[data-resource-id=trg_owned]')
                card = page.locator('[data-resource-id=trg_owned]')
                card.locator('summary').click()
                assert not card.locator('[name=enabled]').is_checked()
                card.get_by_role('button', name='Save', exact=True).click()
                page.wait_for_function("document.getElementById('manage-status').textContent.startsWith('Saved.')")
                assert seen[-1][1]['enabled'] is False
                page.get_by_role('button', name='New disabled routine', exact=True).click()
                page.wait_for_selector('#manage-content [name=prompt]')
                form = page.locator('#manage-content form')
                form.locator('[name=name]').fill('New routine')
                form.locator('[name=prompt]').fill('hello')
                form.locator('[name=run_once_at]').fill('2030-01-01T00:00:00Z')
                form.get_by_role('button', name='Create disabled', exact=True).click()
                page.wait_for_selector('[data-resource-id=trg_owned]')
                assert seen[-1] == ('routines', {'name': 'New routine', 'prompt': 'hello', 'run_once_at': '2030-01-01T00:00:00Z', 'environment_id': 'env_owned', 'model': 'toy-model'})
                page.locator('#manage-close').click()
                page.locator('#manage-network').click()
                page.wait_for_function("document.getElementById('manage-status').textContent.startsWith('Secrets redacted')")
                assert 'read-only' in page.locator('#manage-title').inner_text()
                page.locator('#manage-close').click()

                page.evaluate("S.selectedId='cse_owned';document.getElementById('pane-body').style.display='flex'")
                page.on('dialog', lambda dialog: dialog.accept('/mnt/user-data/working/owned.bin') if dialog.type == 'prompt' else dialog.accept())
                page.locator('#btn-files').click()
                page.wait_for_selector('#file-editor', state='visible')
                assert page.locator('#file-encoding').input_value() == 'base64'
                original = page.locator('#file-content').input_value()
                assert base64.b64decode(original) == bytes(range(256))
                page.locator('#file-encoding').select_option('utf-8')
                assert page.locator('#file-encoding').input_value() == 'base64'
                assert page.locator('#file-content').input_value() == original
                page.locator('#file-content').fill('AP9CSU5BUlk=')
                page.locator('#save-file').click()
                page.wait_for_function("document.getElementById('file-save-status').textContent === 'Saved with CAS.'")
                assert seen[-1] == ('sessions/cse_owned/sync_file', {'path': '/working/owned.bin', 'content': 'AP9CSU5BUlk=', 'encoding': 'base64', 'if_match_sha256': 'a' * 64})
                assert page.evaluate("() => decodeEditorText(new Uint8Array([239,187,191,65])).charCodeAt(0)") == 0xfeff
                assert page.evaluate("() => {try {decodeEditorText(new Uint8Array([65,13,10]));return false;} catch {return true;}}") is True
                page.locator('#file-content').fill('77u/QcOp')
                page.locator('#file-encoding').select_option('utf-8')
                assert page.locator('#file-content').input_value() == '﻿Aé'
                page.locator('#file-encoding').select_option('base64')
                assert page.locator('#file-content').input_value() == '77u/QcOp'
                page.locator('#file-content').fill('Y2hhbmdlZA==')
                page.route('**/api/sessions/cse_owned/sync_file', lambda route: route.fulfill(status=409, json={'error': 'conflict'}))
                page.locator('#save-file').click()
                page.wait_for_function("document.getElementById('file-save-status').textContent.startsWith('Save failed; edits retained.')")
                assert page.locator('#file-content').input_value() == 'Y2hhbmdlZA=='
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        rc.close()
