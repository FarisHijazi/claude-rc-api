"""Control admission, typed slash dispatch and captured MCP contracts (offline)."""
import argparse
import json

import httpx
import pytest

from claude_rc.client import RemoteControlClient, ControlRejected, ControlTimeout
from claude_rc.commands import (
    CLOUD_REJECTED_SUBTYPES, NON_CONTROLLER_SUBTYPES, LOCAL_COMMANDS,
    UnsupportedControl, LocalCommandRequired, command_table, staged_mcp_fields,
    register_command_cli, dispatch_command_cli,
)
from test_interaction import FakeSession, _client, SID


@pytest.mark.parametrize("name,arg,subtype,fields", [
    ("model", "claude-haiku-5-5", "set_model", {"model": "claude-haiku-5-5"}),
    ("model", "", "list_models", {}),
    ("plan", "", "set_permission_mode", {"mode": "plan"}),
    ("plan", "off", "set_permission_mode", {"mode": "default"}),
    ("plan", "acceptEdits", "set_permission_mode", {"mode": "acceptEdits"}),
    ("effort", "low", "apply_flag_settings", {"settings": {"effortLevel": "low", "ultracode": False}}),
    ("effort", "auto", "apply_flag_settings", {"settings": {"effortLevel": None, "ultracode": False}}),
    ("effort", "ultracode", "apply_flag_settings", {"settings": {"ultracode": True}}),
    ("effort", "", "get_settings", {}),
    ("fast", "off", "apply_flag_settings", {"settings": {"fastMode": False}}),
    ("advisor", "off", "apply_flag_settings", {"settings": {"advisorModel": None}}),
    ("context", "", "get_context_usage", {"detail": "summary"}),
    ("usage", "", "get_usage", {}),
    ("cost", "", "get_session_cost", {}),
    ("btw", "what next?", "side_question", {"question": "what next?"}),
    ("permissions", "", "list_permission_rules", {}),
    ("diff", "", "get_workspace_diff", {}),
    ("hooks", "", "get_hooks_listing", {}),
    ("mcp", "", "mcp_status", {}),
    ("mcp", "reconnect test", "mcp_reconnect", {"serverName": "test"}),
    ("mcp", "disable test", "mcp_toggle", {"serverName": "test", "enabled": False}),
    ("reload-skills", "", "reload_skills", {}),
    ("reload-plugins", "", "reload_plugins", {}),
    ("tasks", "output task-1", "get_task_output", {"task_id": "task-1"}),
    ("tasks", "stop task-1", "stop_task", {"task_id": "task-1"}),
    ("rename", "New title", "rename_session", {"title": "New title", "source": "remote"}),
    ("rewind", "uuid-1", "rewind_conversation", {"target_message_uuid": "uuid-1"}),
])
def test_dispatch_uses_typed_controls_not_chat(name, arg, subtype, fields):
    fake = FakeSession()
    with _client(fake.handler) as client:
        result = client.run_command(SID, name, arg)
    assert result["via"] == "control"
    assert fake.last_request() == {"subtype": subtype, **fields}
    assert all(p["type"] == "control_request" for p in fake.posted)


@pytest.mark.parametrize("name", sorted(LOCAL_COMMANDS))
def test_local_commands_never_infer_or_touch_network(name):
    fake = FakeSession()
    state = []
    with _client(fake.handler) as client:
        with pytest.raises(LocalCommandRequired):
            client.run_command(SID, name, strict=False)
        result = client.run_command(SID, name, "value", local_handlers={name: state.append})
    assert result["via"] == "local" and state == ["value"]
    assert fake.requests == []


def test_help_is_local_and_table_covers_every_registered_command():
    from claude_rc.commands import WEB_CLIENT_COMMANDS
    fake = FakeSession()
    with _client(fake.handler) as client:
        result = client.run_command(SID, "help")
    assert {row["name"] for row in result["result"]} == set(WEB_CLIENT_COMMANDS)
    assert not fake.requests


@pytest.mark.parametrize("name,arg", [
    ("fast", ""), ("fast", "yes"), ("effort", "huge"), ("plan", "bypassPermissions"),
    ("btw", ""), ("context", "extra"), ("tasks", ""), ("rewind", ""), ("rename", ""),
    ("model on", ""), ("//model", ""), ("feedback", "send my logs"), ("fork", ""),
])
def test_invalid_or_unsupported_dispatch_cannot_enqueue(name, arg):
    fake = FakeSession()
    with _client(fake.handler) as client:
        with pytest.raises(ValueError):
            client.run_command(SID, name, arg)
    assert not fake.requests


def test_advertised_worker_command_alias_and_exact_arguments():
    fake = FakeSession(responses={"initialize": {"commands": [{"name": "safe-skill", "aliases": ["safe"]}]}})
    with _client(fake.handler) as client:
        result = client.run_command(SID, "safe", "a  b", priority="later")
        with pytest.raises(UnsupportedControl):
            client.run_command(SID, "unknown")
    assert result["via"] == "worker"
    user = next(p for p in fake.posted if p["type"] == "user")
    assert user["message"]["content"] == "/safe-skill a  b" and user["priority"] == "later"


@pytest.mark.parametrize("subtype", sorted(CLOUD_REJECTED_SUBTYPES | NON_CONTROLLER_SUBTYPES | {"unknown_control"}))
def test_cloud_rejected_and_wrong_direction_controls_fail_before_http(subtype):
    fake = FakeSession()
    with RemoteControlClient(token="test-token", org_uuid="org", control_profile="cloud",
                             transport=httpx.MockTransport(fake.handler)) as client:
        with pytest.raises(UnsupportedControl):
            client.request_control(SID, subtype)
    assert not fake.requests


def test_cloud_flag_settings_and_plugin_additions_are_gated():
    fake = FakeSession()
    with RemoteControlClient(token="test-token", org_uuid="org", control_profile="cloud",
                             transport=httpx.MockTransport(fake.handler)) as client:
        for subtype, fields in [("apply_flag_settings", {"settings": {"theme": "dark"}}),
                                ("apply_flag_settings", {"settings": []}),
                                ("reload_plugins", {"add_plugins": ["new-plugin"]})]:
            with pytest.raises(UnsupportedControl):
                client.send_control(SID, subtype, **fields)
    assert not fake.requests


def test_cookie_auth_defaults_cloud_but_bridge_override_is_available():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with RemoteControlClient(session_key="test", org_uuid="org", transport=transport) as client:
        assert client.control_profile == "cloud"
        with pytest.raises(UnsupportedControl):
            client.start_shell_task(SID, "echo")
    with RemoteControlClient(session_key="test", org_uuid="org", transport=transport, control_profile="bridge") as client:
        assert client.control_profile == "bridge"


def test_model_and_mode_wait_for_worker_rejection_not_enqueue_ack():
    fake = FakeSession(errors={"set_model": "policy", "set_permission_mode": "not allowed"})
    with _client(fake.handler) as client:
        for method, value in [(client.set_model, "other"), (client.set_permission_mode, "plan")]:
            with pytest.raises(ControlRejected):
                method(SID, value, wait=0.2)
        with pytest.raises(ControlRejected):
            client.run_command(SID, "model", "other")


def test_model_ack_remains_backwards_compatible():
    fake = FakeSession()
    with _client(fake.handler) as client:
        assert client.set_model(SID, "test")["results"]
        assert client.set_permission_mode(SID, "plan")["results"]
        assert client.set_model(SID, "test", wait=0.2) == {}


def test_mcp_ask_for_tools_objects_not_strings():
    fake = FakeSession()
    with _client(fake.handler) as client:
        client.set_mcp_permission_mode_override(SID, "server", "default", ask_for_tools=["read", "list"])
    assert fake.last_request()["askForTools"] == [{"name": "read"}, {"name": "list"}]


def test_staged_mcp_fields_and_conflicts_are_preserved():
    reply = {"staging": {"status": "error", "error_code": "output_conflict"}, "toolResult": "result"}
    fake = FakeSession(responses={"mcp_call": reply})
    inputs = [{"name": "src", "lane_path": "/working/test.txt"}]
    outputs = [{"name": "dst", "lane_path": "/working/out.txt", "if_match": "opaque-etag"}]
    with _client(fake.handler) as client:
        result = client.mcp_call(SID, "mcp__test__copy", {"from": "{{in:src}}", "to": "{{out:dst}}"},
                                 input_files=inputs, output_files=outputs, expires_at="2030-01-01T00:00:00Z",
                                 timeout_ms=1000)
    request = fake.last_request()
    assert result == reply
    assert request["input_files"] == inputs and request["output_files"] == outputs
    assert request["expires_at"] == "2030-01-01T00:00:00Z" and request["timeout_ms"] == 1000


@pytest.mark.parametrize("fields", [
    {"output_files": []}, {"expires_at": "tomorrow"}, {"expires_at": "2030-01-01T00:00:00"},
    {"input_files": [{"name": "bad handle", "lane_path": "/x"}]},
    {"input_files": [{"name": "ok", "lane_path": ""}]},
    {"input_files": [{"name": "a", "lane_path": "/x"}] * 2},
    {"input_files": [{"name": "a", "lane_path": "/x", "extra": 1}]},
    {"output_files": [{"name": "a", "lane_path": "/x", "if_match": ""}], "expires_at": "2030-01-01T00:00:00Z"},
    {"result_not_for_model": True},
])
def test_invalid_staging_fails_locally(fields):
    with pytest.raises(ValueError):
        staged_mcp_fields(**fields)


def test_result_exclusion_requires_explicit_advertised_capability():
    assert staged_mcp_fields(result_not_for_model=True,
                             capabilities=["mcp_call_result_not_for_model_v1"]) == {"result_not_for_model": True}


def test_live_mcp_does_not_silently_poll_history(monkeypatch):
    fake = FakeSession()
    with _client(fake.handler) as client:
        with pytest.raises(ValueError, match="request_control_live"):
            client.mcp_status(SID, live_reply=True)
        seen = []
        monkeypatch.setattr(client, "request_control_live", lambda *a, **kw: seen.append(kw) or {"mcpServers": [{"name": "test"}]}, raising=False)
        assert client.mcp_status(SID, live_reply=True) == [{"name": "test"}]
    assert seen[0]["liveReply"] is True and not fake.requests


def test_captured_browser_discovery_and_mcp_rest_contracts():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={})
    with _client(handle) as client:
        client.browser_models()
        client.permission_mode_policy()
        client.set_session_mcp_servers(SID, {"test": {"type": "stdio", "command": "echo"}})
        client.approve_session_mcp_tool(SID, "mcp__test__read", "once", "tu-test")
    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", "/api/organizations/org-1/model_selector/cc"),
        ("GET", "/api/claude_code/organizations/org-1/permission_mode_policy"),
        ("PUT", f"/v1/code/sessions/{SID}/mcp-servers"),
        ("POST", f"/v1/code/sessions/{SID}/mcp-approvals"),
    ]
    assert json.loads(requests[2].content) == {"mcp_servers": [{"name": "test", "type": "stdio", "command": "echo"}]}
    assert json.loads(requests[3].content) == {"tool_name": "mcp__test__read", "scope": "once", "tool_use_id": "tu-test"}


def test_cli_registration_and_dispatch_helper():
    parser = argparse.ArgumentParser()
    register_command_cli(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["dispatch", SID, "help"])
    fake = FakeSession()
    with _client(fake.handler) as client:
        result = dispatch_command_cli(client, args)
    assert result["via"] == "local" and not fake.requests


def test_alias_cannot_bypass_local_command_guard():
    fake = FakeSession(responses={"initialize": {"commands": [{"name": "config", "aliases": ["prefs"]}]}})
    with _client(fake.handler) as client:
        with pytest.raises(LocalCommandRequired):
            client.run_command(SID, "prefs", "key=value")
    assert all(p["type"] != "user" for p in fake.posted)


@pytest.mark.parametrize("names", [["bad name"], ["tool"] * 33, [""], "read", b"read"])
def test_mcp_ask_tools_validates_captured_limits(names):
    fake = FakeSession()
    with _client(fake.handler) as client:
        with pytest.raises(ValueError):
            client.set_mcp_permission_mode_override(SID, "test", "default", ask_for_tools=names)
    assert not fake.requests


def test_remote_result_exclusion_cannot_claim_local_pipe_semantics():
    fake = FakeSession()
    with _client(fake.handler) as client:
        with pytest.raises(ValueError, match="local pipes"):
            client.mcp_call(SID, "mcp__test__read", result_not_for_model=True,
                            capabilities=["mcp_call_result_not_for_model_v1"])
    assert not fake.requests


def test_browser_hints_and_survey_exact_captured_shapes():
    fake = FakeSession()
    with _client(fake.handler) as client:
        client.set_chrome_browser_hints(SID, ["device-a"], preferred_device_id="device-a", host_platform="linux")
        assert fake.last_request() == {"subtype": "set_chrome_browser_hints", "localDeviceIds": ["device-a"],
                                       "preferredDeviceId": "device-a", "hostPlatform": "linux"}
        client.submit_survey_feedback(SID, "test only", "appearance-test", survey_response="fine")
    assert fake.last_request() == {"subtype": "submit_survey_feedback", "description": "test only",
                                   "appearance_id": "appearance-test", "survey_response": "fine",
                                   "survey_type": "session", "surface": "ccr"}
