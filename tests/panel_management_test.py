"""Panel adapters exercise canonical feature owners using disposable fixtures."""
import base64
import json

import httpx
import pytest

from test_cli import ORG, SID, Server, webui


def test_binary_cas_preserves_every_byte(webui):
    path = f'/v1/code/sessions/{SID}/synced_file'
    server = Server({('PUT', path): {'content_sha256': 'b' * 64}})
    payload = bytes(range(256))
    response = httpx.post(webui(server) + f'/api/sessions/{SID}/sync_file', trust_env=False,
                         json={'path': '/working/owned.bin', 'content': base64.b64encode(payload).decode(),
                               'encoding': 'base64', 'if_match_sha256': 'a' * 64})
    assert response.status_code == 200
    sent = json.loads(server.requests[-1].content)
    assert base64.b64decode(sent['content']) == payload
    assert sent['if_match_sha256'] == 'a' * 64


@pytest.mark.parametrize('encoding,content', [('base64', 'not valid!'), ('hex', 'ff')])
def test_bad_binary_content_never_writes(webui, encoding, content):
    server = Server()
    response = httpx.post(webui(server) + f'/api/sessions/{SID}/sync_file', trust_env=False,
                         json={'path': '/working/owned.bin', 'content': content,
                               'encoding': encoding, 'if_match_sha256': 'token'})
    assert response.status_code == 400
    assert not server.requests


def test_environment_metadata_preserves_private_config(webui):
    get_path = f'/v1/environment_providers/private/organizations/{ORG}/environments/env_owned'
    server = Server({('GET', get_path): {'name': 'old', 'config': {'environment': {'SECRET': 'toy-private'}, 'cwd': '/working'}},
                     ('POST', '/v1/environment_providers/env_owned'): {'name': 'new', 'config': {'environment': {'SECRET': 'toy-private'}}}})
    base = webui(server)
    response = httpx.post(base + '/api/environments/env_owned', trust_env=False, json={'name': 'new', 'description': 'updated'})
    assert response.status_code == 200
    assert 'toy-private' not in response.text
    assert json.loads(server.requests[-1].content)['config']['environment'] == {'SECRET': 'toy-private'}
    before = len(server.requests)
    assert httpx.post(base + '/api/environments/env_owned', trust_env=False, json={'config': {}}).status_code == 400
    assert len(server.requests) == before


def test_routine_create_is_disabled_and_updates_are_partial(webui):
    server = Server({('POST', '/v1/code/triggers'): {'trigger': {'id': 'trg_owned'}},
                     ('POST', '/v1/code/triggers/trg_owned'): {'trigger': {'id': 'trg_owned'}}})
    base = webui(server)
    response = httpx.post(base + '/api/routines', trust_env=False,
                         json={'name': 'owned', 'environment_id': 'env_owned', 'prompt': 'hello',
                               'run_once_at': '2030-01-01T00:00:00Z', 'model': 'toy-model'})
    assert response.status_code == 200
    assert json.loads(server.requests[-1].content)['enabled'] is False
    response = httpx.post(base + '/api/routines/trg_owned', trust_env=False, json={'enabled': False})
    assert response.status_code == 200
    assert json.loads(server.requests[-1].content) == {'enabled': False}
    before = len(server.requests)
    assert httpx.post(base + '/api/routines/trg_owned', trust_env=False, json={'enabled': 'false'}).status_code == 400
    assert len(server.requests) == before


@pytest.mark.parametrize('action,method,suffix,body', [
    ('run', 'POST', '/run', {}), ('fork', 'POST', '/fork', {}),
    ('delete', 'DELETE', '', {}), ('cancel', 'POST', '/runs/cse_owned/cancel', {'session_id': 'cse_owned'}),
])
def test_routine_actions_delegate(webui, action, method, suffix, body):
    path = '/v1/code/triggers/trg_owned' + suffix
    server = Server({(method, path): {}})
    response = httpx.post(webui(server) + '/api/routines/trg_owned/' + action, trust_env=False, json=body)
    assert response.status_code == 200
    assert json.loads(server.requests[-1].content) == {}


def test_network_profiles_are_redacted_and_read_only(webui):
    server = Server({('GET', '/v1/code/agent-proxy-profiles'): {'profiles': [{'id': 'profile_owned', 'token': 'toy-private'}]}})
    base = webui(server)
    response = httpx.get(base + '/api/network-profiles', trust_env=False)
    assert response.status_code == 200
    assert 'toy-private' not in response.text
    assert httpx.post(base + '/api/network-profiles', trust_env=False, json={}).status_code == 404
