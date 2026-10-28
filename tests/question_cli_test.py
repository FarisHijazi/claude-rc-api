"""CLI adapters for the authoritative question builder, offline toy cards."""
import json

import pytest

from claude_rc import cli
from claude_rc.prompts import BROWSER_UNANSWERED
from test_cli import QUESTION, SID, Server, run


@pytest.mark.parametrize('flag,reason', [('--skip','skipped'), ('--dismiss','dismissed'),
                                       ('--replied-in-chat','replied_in_chat')])
def test_cli_browser_unanswered_encoding(monkeypatch, flag, reason):
    server=Server(log=[QUESTION])
    assert run(monkeypatch,server,'answer',SID,'question',flag,'--encoding','browser')==0
    assert BROWSER_UNANSWERED[reason] in json.dumps(server.posted_events(),ensure_ascii=False)


def test_cli_preserves_annotations_and_partial_skip(monkeypatch):
    server=Server(log=[QUESTION])
    assert run(monkeypatch,server,'answer',SID,'question','--select','0:Blue',
        '--skip-question','1','--encoding','browser','--note','0:toy note','--preview','0:toy preview')==0
    answer=json.dumps(server.posted_events(),ensure_ascii=False)
    assert all(value in answer for value in ['Blue','toy note','toy preview',BROWSER_UNANSWERED['skipped']])


@pytest.mark.parametrize('argv', [['account','usage','--direct'],['dispatch',SID,'help','--direct']])
def test_extension_credentials_work_after_subcommand(argv):
    assert cli.build_parser().parse_args(argv).direct is True
