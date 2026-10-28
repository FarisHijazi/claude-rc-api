"""Recovered routine contracts and panel behavior, using only owned fake resources."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from claude_rc import RemoteControlClient, trigger_item, trigger_prompt
from claude_rc.events import stream_meta_event
from claude_rc.webui import event_to_dict
from test_cli import ORG, PERMISSION, QUESTION, SID, Server, run, webui


def test_live_trigger_envelopes_are_unwrapped():
    trigger = {"id": "trig_owned", "job_config": {"ccr": {"events": [
        {"data": {"message": {"content": "marker"}}}
    ]}}}
    assert trigger_item({"trigger": trigger, "outcome": "created"}) == trigger
    assert trigger_prompt({"trigger": trigger}) == "marker"
    assert trigger_item(trigger) is trigger


@pytest.mark.parametrize("action,suffix,method", [
    ("fork_trigger", "/fork", "POST"),
    ("delete_trigger", "", "DELETE"),
    ("create_trigger_api_token", "/api-token", "POST"),
    ("revoke_trigger_api_token", "/api-token", "DELETE"),
])
def test_empty_body_routine_operations(action, suffix, method):
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={})
    with RemoteControlClient(token="fake", org_uuid=ORG, transport=httpx.MockTransport(handler)) as rc:
        getattr(rc, action)("trig_owned")
    assert seen[0].method == method
    assert seen[0].url.path == "/v1/code/triggers/trig_owned" + suffix
    assert json.loads(seen[0].content) == {}
    assert "ccr-triggers-2026-01-30" in seen[0].headers["anthropic-beta"]


def test_trigger_filters_survive_cursor_paging():
    seen = []
    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(200, json={"data": [{"id": "a"}], "next_cursor": "next"})
        return httpx.Response(200, json={"data": [{"id": "b"}], "has_more": False})
    with RemoteControlClient(token="fake", org_uuid=ORG, transport=httpx.MockTransport(handler)) as rc:
        assert [t["id"] for t in rc.list_triggers(limit=1, tags=["owned"], exclude_tags=["skip"])] == ["a", "b"]
    assert seen[1].url.params["cursor"] == "next"
    assert seen[1].url.params.get_list("tags") == ["owned"]
    assert seen[1].url.params.get_list("exclude_tags") == ["skip"]


def test_trigger_repeated_cursor_is_bounded():
    def handler(req):
        return httpx.Response(200, json={"data": [], "next_cursor": "same"})
    with RemoteControlClient(token="fake", org_uuid=ORG, transport=httpx.MockTransport(handler)) as rc:
        with pytest.raises(RuntimeError, match="repeated"):
            rc.list_triggers()


@pytest.mark.parametrize("action,suffix", [("delete", ""), ("fork", "/fork"), ("cancel-run", "/runs/cse_owned/cancel")])
def test_cli_owned_routine_operations(monkeypatch, action, suffix):
    method = "DELETE" if action == "delete" else "POST"
    srv = Server({(method, "/v1/code/triggers/trig_owned" + suffix): {}})
    args = ["triggers", action, "trig_owned"]
    if action == "cancel-run":
        args += ["--run-session", "cse_owned"]
    assert run(monkeypatch, srv, *args) == 0
    assert srv.requests[-1].url.path.endswith("trig_owned" + suffix)
    assert json.loads(srv.requests[-1].content) == {}


def test_cli_disabled_create_and_live_envelope(monkeypatch, capsys):
    srv = Server({("POST", "/v1/code/triggers"): {"trigger": {"id": "trig_owned"}, "outcome": "created"}})
    assert run(monkeypatch, srv, "triggers", "create", "owned", "--env", "env_owned",
               "--at", "2030-01-01T00:00:00Z", "--disabled") == 0
    assert json.loads(srv.requests[0].content)["enabled"] is False
    assert "trig_owned" in capsys.readouterr().out


def test_cli_manual_run_optional_text(monkeypatch):
    srv = Server({("POST", "/v1/code/triggers/trig_owned/run"): {}})
    assert run(monkeypatch, srv, "triggers", "run", "trig_owned", "--prompt", "marker") == 0
    assert json.loads(srv.requests[-1].content) == {"text": "marker"}


@pytest.mark.parametrize("decision,classification", [("allow", "user_temporary"), ("always", "user_permanent"), ("deny", "user_reject")])
def test_panel_permission_uses_authoritative_decision(webui, decision, classification):
    permission = copy.deepcopy(PERMISSION)
    permission["request"]["permission_suggestions"] = [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "printf owned"}], "behavior": "allow", "destination": "session"}]
    srv = Server(log=[permission])
    response = httpx.post(f"{webui(srv)}/api/sessions/{SID}/permission", trust_env=False,
                          json={"request_id": "req-p", "decision": decision})
    assert response.status_code == 200, response.text
    answer = json.dumps(srv.posted_events())
    assert classification in answer
    if decision == "always":
        assert "updatedPermissions" in answer


def test_panel_rejects_stale_permission(webui):
    srv = Server(log=[])
    response = httpx.post(f"{webui(srv)}/api/sessions/{SID}/permission", trust_env=False,
                          json={"request_id": "not-pending", "decision": "always"})
    assert response.status_code == 404
    assert srv.posted_events() == []


def test_panel_question_notes(webui):
    srv = Server(log=[QUESTION])
    response = httpx.post(f"{webui(srv)}/api/sessions/{SID}/answer", trust_env=False,
                          json={"request_id": "req-q", "selections": {"0": "Blue"}, "notes": {"0": "owned note"}})
    assert response.status_code == 200
    assert "owned note" in json.dumps(srv.posted_events())


def test_panel_auth_uses_its_client_not_shared_home(webui):
    result = httpx.get(webui(Server()) + "/api/whoami", trust_env=False)
    assert result.status_code == 200
    assert result.json()["logged_in"] is True
    assert "token_len" not in result.json()


def test_panel_serializes_full_stream_metadata():
    event = stream_meta_event("delivery_update", {"event_id": "owned", "status": "delivered"})
    out = event_to_dict(event)
    assert out["raw"]["data"]["status"] == "delivered"
    assert out["payload"]["event_id"] == "owned"


def test_cli_help_runs_as_real_process():
    result = subprocess.run([sys.executable, "-m", "claude_rc.cli", "triggers", "--help"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "cancel-run" in result.stdout and "--disabled" in result.stdout


def test_panel_javascript_parses(tmp_path):
    page = Path(__file__).parents[1] / "claude_rc/static/index.html"
    script = page.read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    target = tmp_path / "panel.js"
    target.write_text(script)
    result = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
