"""Regressions found during independent group08 verification."""
import json

import pytest

from test_cli import Server, run


@pytest.mark.parametrize("options,expected", [
    (["--prompt", "new prompt"], "prompt"),
    (["--model", "claude-haiku-5-5"], "model"),
    (["--env", "env_new"], "environment"),
    (["--repo", "https://github.com/acme/project"], "repo"),
])
def test_update_routine_job_flags_preserve_other_configuration(monkeypatch, options, expected):
    job = {"ccr": {
        "environment_id": "env_owned",
        "session_context": {"model": "old-model", "allowed_tools": ["Read"]},
        "events": [{"data": {"message": {"content": "old prompt", "role": "user"}}}],
    }}
    srv = Server({
        ("GET", "/v1/code/triggers/trig_owned"): {"trigger": {"job_config": job}},
        ("POST", "/v1/code/triggers/trig_owned"): {},
    })
    assert run(monkeypatch, srv, "triggers", "update", "trig_owned", *options) == 0
    updated = json.loads(srv.requests[-1].content)["job_config"]["ccr"]
    assert updated["session_context"]["allowed_tools"] == ["Read"]
    assert updated["environment_id"] == ("env_new" if expected == "environment" else "env_owned")
    assert updated["session_context"]["model"] == ("claude-haiku-5-5" if expected == "model" else "old-model")
    assert updated["events"][0]["data"]["message"]["content"] == ("new prompt" if expected == "prompt" else "old prompt")
    if expected == "repo":
        assert updated["session_context"]["sources"] == [{"git_repository": {"url": "https://github.com/acme/project"}}]


def test_update_job_override_does_not_fetch_or_erase_explicit_fields(monkeypatch):
    srv = Server({("POST", "/v1/code/triggers/trig_owned"): {}})
    field = 'job_config={"ccr":{"environment_id":"env_owned","session_context":{"allowed_tools":["Read"]}}}'
    assert run(monkeypatch, srv, "triggers", "update", "trig_owned", "--field", field,
               "--prompt", "new prompt") == 0
    assert len(srv.requests) == 1
    ccr = json.loads(srv.requests[-1].content)["job_config"]["ccr"]
    assert ccr["session_context"]["allowed_tools"] == ["Read"]
    assert ccr["events"][0]["data"]["message"]["content"] == "new prompt"
