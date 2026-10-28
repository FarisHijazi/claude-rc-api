"""Cross-group adapters against toy resources, never real account credentials."""
import copy
import json

import httpx
import pytest

from claude_rc import RemoteControlClient, UnsupportedControl, cli
from claude_rc.prompts import BROWSER_UNANSWERED
from test_cli import ORG as cli_test_org, QUESTION, SID, Server, run, webui
from test_interaction import FakeSession, _client


def test_live_control_escape_hatch_still_enforces_admission():
    fake = FakeSession()
    with _client(fake.handler) as rc:
        rc.control_profile = 'cloud'
        with pytest.raises(UnsupportedControl):
            rc.request_control_live(SID, 'stage_file', liveReply=True)
        with pytest.raises(UnsupportedControl):
            rc.request_control(SID, 'mcp_authenticate', liveReply=True)
    assert fake.requests == []


@pytest.mark.parametrize('argv', [
    ['history', SID], ['stream', SID, '--control-only'], ['watch-account', '--no-reconnect'],
    ['transcribe', 'sample.pcm'], ['session-file', SID, '/working/owned.txt'],
    ['sync-file', SID, '/working/owned.txt', 'sample.txt', '--if-match', 'a'*64],
    ['prefetch', SID, 'references.json'], ['mount-file', SID, 'file_owned', '/owned.txt'],
    ['account', 'usage'], ['dispatch', SID, 'help'], ['models'], ['policy'],
    ['repos'], ['branches', 'org/project'], ['envs', 'archive', 'env_owned'],
])
def test_extension_parsers_are_wired(argv):
    args = cli.build_parser().parse_args(['--direct', *argv])
    assert callable(args.func)
    assert args.direct is True


def test_cli_dispatch_returns_local_result_not_sent_receipt(monkeypatch, capsys):
    assert run(monkeypatch, Server(), 'dispatch', SID, 'help') == 0
    out = json.loads(capsys.readouterr().out)
    assert out['via'] == 'local' and out['command'] == 'help'


def test_cli_environment_update_preserves_nested_configuration(monkeypatch):
    path = '/v1/environment_providers/env_owned'
    server = Server({('GET', f'/v1/environment_providers/private/organizations/{cli_test_org}/environments/env_owned'): {'name':'original','config':{'environment':{'MARKER':'toy'}, 'cwd':'/working'}},
                     ('POST', path): {}})
    assert run(monkeypatch, server, 'envs','update','env_owned','--option','description=changed') == 0
    body=json.loads(server.requests[-1].content)
    assert body['config'] == {'environment':{'MARKER':'toy'},'cwd':'/working'}
    assert body['description'] == 'changed'


@pytest.mark.parametrize('flag,reason', [('skip','skipped'), ('dismissed','dismissed'), ('replied_in_chat','replied_in_chat')])
def test_panel_unanswered_uses_explicit_browser_encoding(webui, flag, reason):
    server=Server(log=[QUESTION])
    response=httpx.post(webui(server)+f'/api/sessions/{SID}/answer', trust_env=False,
                        json={'request_id':'req-q',flag:True})
    assert response.status_code==200
    payload=json.dumps(server.posted_events(), ensure_ascii=False)
    assert BROWSER_UNANSWERED[reason] in payload


def test_panel_answer_preserves_previews_partial_skips_and_other(webui):
    server=Server(log=[QUESTION])
    response=httpx.post(webui(server)+f'/api/sessions/{SID}/answer', trust_env=False,
        json={'request_id':'req-q','other':{'0':'Violet'}, 'skipped':[1],
              'notes':{'0':'toy note'}, 'previews':{'0':'toy preview'}})
    assert response.status_code==200
    payload=json.dumps(server.posted_events(), ensure_ascii=False)
    assert all(v in payload for v in ['Violet','toy note','toy preview',BROWSER_UNANSWERED['skipped']])


def test_panel_numeric_constraint_rejects_before_write(webui):
    question=copy.deepcopy(QUESTION)
    question['request']['input']['questions']=[{'question':'Count?', 'kind':'number', 'min':1,'max':5,'step':1}]
    server=Server(log=[question])
    response=httpx.post(webui(server)+f'/api/sessions/{SID}/answer',trust_env=False,
        json={'request_id':'req-q','other':{'0':'6'}})
    assert response.status_code==400
    assert not server.posted_events()


@pytest.mark.parametrize('action,method,path', [
    ('rename','PUT',f'/v1/code/sessions/{SID}'),
    ('unarchive','POST',f'/v1/code/sessions/{SID}/unarchive'),
    ('delete','DELETE',f'/v1/code/sessions/{SID}'),
])
def test_panel_lifecycle_is_the_canonical_adapter(webui,action,method,path):
    server=Server({(method,path):{'title':'owned'}})
    response=httpx.post(webui(server)+f'/api/sessions/{SID}/{action}',trust_env=False,json={'title':'owned'})
    assert response.status_code==200
    assert server.requests[-1].url.path==path
    assert server.requests[-1].method==method


def test_panel_cas_requires_hash_and_preserves_conflicts(webui):
    path=f'/v1/code/sessions/{SID}/synced_file'
    server=Server({('PUT',path):{'content_sha256':'b'*64}})
    base=webui(server)
    body={'path':'/working/toy.txt','content':'toy'}
    assert httpx.post(base+f'/api/sessions/{SID}/sync_file',trust_env=False,json=body).status_code==400
    assert not server.requests
    response=httpx.post(base+f'/api/sessions/{SID}/sync_file',trust_env=False,
                        json={**body,'if_match_sha256':'a'*64})
    assert response.status_code==200
    assert json.loads(server.requests[-1].content)['if_match_sha256']=='a'*64


def test_error_bodies_never_reach_cli_or_panel(monkeypatch,capsys,webui):
    def fail(req):
        return httpx.Response(400,json={'error':{'type':'invalid_request_error','message':'toy-private-response'}})
    rc=RemoteControlClient(token='toy',org_uuid='org',transport=httpx.MockTransport(fail))
    monkeypatch.setattr(cli,'_rc',lambda args:rc)
    assert cli.main(['get',SID])==1
    assert 'toy-private-response' not in capsys.readouterr().out
    server=Server()
    server.handler=fail
    response=httpx.get(webui(server)+f'/api/sessions/{SID}',trust_env=False)
    assert response.status_code==400
    assert 'toy-private-response' not in response.text
