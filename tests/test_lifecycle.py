"""LifecycleMixin: create / list / tag / unarchive / delete / share / heartbeat /
teleport / github helpers — exact wire shapes, offline through httpx.MockTransport."""
import json

import httpx
import pytest

from claude_rc import client as client_mod
from claude_rc.client import CCR_BETA, APIError, RemoteControlClient
from claude_rc.lifecycle import (
    branch_for,
    environment_target,
    repo_slug,
    session_web_url,
    to_cse_id,
    to_session_id,
)

AUTH_ENV = (
    "CLAUDE_AI_ACCESS_TOKEN",
    "CLAUDE_RC_ACCESS_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_AI_SESSION_KEY",
    "CLAUDE_RC_CSWAP_SLOT",
    "CLAUDE_AI_ORG_UUID",
    "CLAUDE_RC_ORG_UUID",
    "CLAUDE_TRUSTED_DEVICE_TOKEN",
)


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    for name in AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


SESSION = {"id": "cse_01ABC", "title": "t", "status": "active"}


def _recorder(responses):
    """A MockTransport handler that records requests and replays ``responses``
    (a list of (status, json) consumed in order; the last one repeats)."""
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        status, body = responses[min(len(seen) - 1, len(responses) - 1)]
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    return seen, handler


def _client(handler) -> RemoteControlClient:
    return RemoteControlClient(token="tok", org_uuid="org-1", transport=httpx.MockTransport(handler))


def _body(req) -> dict:
    return json.loads(req.content)


# --- pure helpers --------------------------------------------------------------
def test_id_helpers_swap_prefix_only():
    assert to_cse_id("session_01X") == "cse_01X"
    assert to_cse_id("cse_01X") == "cse_01X"
    assert to_session_id("cse_01X") == "session_01X"
    assert to_session_id("session_01X") == "session_01X"
    assert to_session_id("other") == "other"
    assert session_web_url("cse_01X") == "https://claude.ai/code/session_01X"


def test_repo_slug_forms():
    for form in ("o/r", "https://github.com/o/r", "https://github.com/o/r.git", "git@github.com:o/r.git", "github.com/o/r/"):
        assert repo_slug(form) == "o/r"


def test_branch_for_and_environment_target():
    assert branch_for("Fix the Login bug!") == "claude/fix-the-login-bug"
    assert branch_for(None) == "claude/task"
    assert environment_target("env_1") == {"environment_id": "env_1"}
    assert environment_target("ccpool_1") == {"self_hosted_runner_pool_id": "ccpool_1"}


# --- create --------------------------------------------------------------------
def test_create_session_web_body_matches_har():
    seen, handler = _recorder([(200, {"deduplicated": False, "session": SESSION})])
    with _client(handler) as c:
        s = c.create_session(
            "env_01S", title="__warming__", repo="acme/widget", branch="claude/lucid-brown",
            model="claude-opus-5-5", effort_level="medium", append_system_prompt="\n\nx", tags=["cowork-warm-start"])
    assert s == SESSION
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/v1/code/sessions"
    assert req.url.host == "api.anthropic.com"
    assert _body(req) == {
        "title": "__warming__",
        "environment_id": "env_01S",
        "config": {
            "sources": [{"type": "git_repository", "url": "https://github.com/acme/widget"}],
            "outcomes": [{"type": "git_repository", "git_info": {
                "type": "github", "repo": "acme/widget", "branches": ["claude/lucid-brown"]}}],
            "model": "claude-opus-5-5",
            "effort_level": "medium",
            "append_system_prompt": "\n\nx",
        },
        "events": [],
        "tags": ["cowork-warm-start"],
    }
    h = req.headers
    assert h["authorization"] == "Bearer tok"
    assert h["anthropic-version"] == "2023-06-01"
    assert h["anthropic-beta"] == CCR_BETA
    assert h["anthropic-client-platform"] == "web_claude_ai"
    assert h["anthropic-client-feature"] == "ccr"
    assert h["x-organization-uuid"] == "org-1"


def test_create_session_inline_events_in_cli_order():
    seen, handler = _recorder([(201, {"session": SESSION})])
    with _client(handler) as c:
        c.create_session(
            "ccpool_9", "hello there", repo="o/r", revision="main", permission_mode="auto",
            max_thinking_tokens=2048, cwd="/w", allowed_tools=["Bash"], reuse_outcome_branches=True,
            seed_bundle_file_id="file_1", session_grouping_id="g1",
            environment_variables={"A": "1", "CLAUDE_CODE_OAUTH_TOKEN": "x"}, body_extra={"agent_id": "ag"})
    b = _body(seen[0])
    assert "environment_id" not in b and b["self_hosted_runner_pool_id"] == "ccpool_9"
    assert b["config"]["sources"] == [{"type": "git_repository", "url": "https://github.com/o/r", "revision": "main"}]
    assert b["config"]["outcomes"][0]["git_info"]["branches"] == ["claude/hello-there"]
    assert b["config"]["cwd"] == "/w" and b["config"]["allowed_tools"] == ["Bash"]
    assert b["config"]["reuse_outcome_branches"] is True and b["config"]["seed_bundle_file_id"] == "file_1"
    assert b["session_grouping_id"] == "g1" and b["agent_id"] == "ag"
    assert b["environment_variables"] == {"A": "1"}
    payloads = [e["payload"] for e in b["events"]]
    assert [p["type"] for p in payloads] == ["control_request", "control_request", "user"]
    assert payloads[0]["request"] == {"subtype": "set_max_thinking_tokens", "max_thinking_tokens": 2048}
    assert payloads[0]["request_id"].startswith("set-thinking-")
    assert payloads[1]["request"] == {"subtype": "set_permission_mode", "mode": "auto"}
    assert payloads[1]["request_id"].startswith("set-mode-")
    assert payloads[2]["message"] == {"role": "user", "content": "hello there"}
    assert payloads[2]["session_id"] == "" and payloads[2]["parent_tool_use_id"] is None
    assert all(p.get("uuid") for p in payloads)


def test_create_session_defer_mirrors_web_two_step():
    seen, handler = _recorder([(200, {"session": SESSION}), (200, {"results": [{"sequence_num": "3"}]})])
    with _client(handler) as c:
        s = c.create_session("env_1", "hi this is a test", permission_mode="auto", defer_events=True)
    assert s["id"] == "cse_01ABC"
    create, send = seen
    assert _body(create)["events"] == []
    assert _body(create)["config"] == {"sources": [], "outcomes": []}
    assert send.method == "POST" and send.url.path == "/v1/code/sessions/cse_01ABC/events"
    events = [e["payload"] for e in _body(send)["events"]]
    assert events[0]["request"] == {"subtype": "set_permission_mode", "mode": "auto"}
    assert events[1]["type"] == "user" and events[1]["session_id"] == "session_01ABC"
    assert events[1]["message"]["content"] == "hi this is a test"


def test_create_session_legacy_envelope():
    seen, handler = _recorder([(200, {"id": "session_01L", "title": "x"})])
    with _client(handler) as c:
        s = c.create_session("env_1", "go", title="x", model="m", legacy=True, environment_variables={"K": "V"})
    assert s["id"] == "session_01L"
    (req,) = seen
    assert req.url.path == "/v1/sessions"
    b = _body(req)
    assert b["title"] == "x" and b["environment_id"] == "env_1"
    assert b["session_context"]["model"] == "m" and b["session_context"]["environment_variables"] == {"K": "V"}
    assert b["events"][0]["type"] == "event" and b["events"][0]["data"]["type"] == "user"
    assert "config" not in b


def test_generate_title_and_branch_is_oauth_route():
    seen, handler = _recorder([(200, {"title": "Test session", "branch_name": "claude/test-session"})])
    with _client(handler) as c:
        out = c.generate_title_and_branch("hi this is a test", model="claude-opus-5-5")
    assert out["branch_name"] == "claude/test-session"
    (req,) = seen
    assert req.url.path == "/api/organizations/org-1/dust/generate_title_and_branch"
    assert _body(req) == {"first_session_message": "hi this is a test", "title_style": "default", "model": "claude-opus-5-5"}
    assert req.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "anthropic-client-feature" not in req.headers


# --- list ----------------------------------------------------------------------
def test_query_sessions_repeats_filters():
    seen, handler = _recorder([(200, {"data": [], "next_cursor": None})])
    with _client(handler) as c:
        c.query_sessions(limit=30, statuses=["active", "paused"], tags=["cowork-remote"], exclude_tags="-",
                         trigger_id="trig_1", include_trigger_sessions=True)
    q = seen[0].url.params
    assert seen[0].url.path == "/v1/code/sessions"
    assert q.get_list("statuses") == ["active", "paused"]
    assert q["tags"] == "cowork-remote" and q["exclude_tags"] == "-"
    assert q["trigger_id"] == "trig_1" and q["include_trigger_sessions"] == "true" and q["limit"] == "30"


def test_iter_sessions_follows_cursor():
    seen, handler = _recorder([
        (200, {"data": [{"id": "a"}], "next_cursor": "c1"}),
        (200, {"data": [{"id": "b"}]}),
    ])
    with _client(handler) as c:
        ids = [s["id"] for s in c.iter_sessions(statuses=["active"])]
    assert ids == ["a", "b"]
    assert "cursor" not in seen[0].url.params and seen[1].url.params["cursor"] == "c1"


# --- update / archive / delete -------------------------------------------------
def test_tag_session_put_body():
    seen, handler = _recorder([(200, {"session": SESSION})])
    with _client(handler) as c:
        assert c.tag_session("session_01ABC", add=["color:red"], remove=["x"]) == SESSION
    assert seen[0].method == "PUT" and seen[0].url.path == "/v1/code/sessions/session_01ABC"
    assert _body(seen[0]) == {"add_tags": ["color:red"], "remove_tags": ["x"]}


def test_unarchive_posts_empty_object():
    seen, handler = _recorder([(200, {"session": SESSION})])
    with _client(handler) as c:
        assert c.unarchive_session("cse_01ABC") == SESSION
    assert seen[0].url.path == "/v1/code/sessions/cse_01ABC/unarchive" and _body(seen[0]) == {}


def test_unarchive_403_exposes_resource():
    _, handler = _recorder([(403, {"type": "error", "error": {"type": "permission_error", "resource": "untrusted_device"}})])
    with _client(handler) as c, pytest.raises(APIError) as ei:
        c.unarchive_session("cse_1")
    assert ei.value.resource == "untrusted_device"


def test_delete_session_uses_empty_body_without_destructive_fallback():
    seen, handler = _recorder([(204, None)])
    with _client(handler) as c:
        assert c.delete_session("cse_01ABC") is True
    assert [(r.method, r.url.path) for r in seen] == [("DELETE", "/v1/code/sessions/cse_01ABC")]
    assert _body(seen[0]) == {}


@pytest.mark.parametrize("status", [403, 404, 405])
def test_delete_session_errors_never_guess_a_legacy_route(status):
    seen, handler = _recorder([(status, {"error": {}})])
    with _client(handler) as c, pytest.raises(APIError):
        c.delete_session("cse_1", legacy_fallback=True)
    assert len(seen) == 1


def test_share_get_create_revoke_and_public_read():
    share = {"effective_visibility": "private", "sharing_settings": None}
    seen, handler = _recorder([(200, share), (200, {"share_id": "share_1"}), (204, None), (200, SESSION)])
    with _client(handler) as c:
        assert c.get_share("session_1") == share
        assert c.set_share("session_1", confirm=True)["share_id"] == "share_1"
        c.revoke_share("session_1")
        assert c.get_shared_session("share_1") == SESSION
    assert [r.method for r in seen] == ["GET", "POST", "DELETE", "GET"]
    assert _body(seen[1]) == _body(seen[2]) == {}
    assert seen[3].url.path == "/v1/code/shared-sessions/share_1"


@pytest.mark.parametrize("kwargs", [{}, {"method": "PUT", "confirm": True},
    {"confirm": True, "visibility_type": "sign_in_only"}])
def test_share_writes_fail_closed_before_http(kwargs):
    seen, handler = _recorder([(200, {})])
    with _client(handler) as c, pytest.raises(ValueError):
        c.set_share("cse_1", **kwargs)
    assert not seen


def test_heartbeat_check_sends_cse_ids():
    resp = {"checks_enabled": True, "results": [{"session_id": "cse_1", "outcome": "fresh"}]}
    seen, handler = _recorder([(200, resp)])
    with _client(handler) as c:
        assert c.heartbeat_check(["session_1", "cse_2"]) == resp
    assert seen[0].url.path == "/v1/code/sessions/heartbeat_check"
    assert _body(seen[0]) == {"session_ids": ["cse_1", "cse_2"]}


def test_mark_read_event_id_body():
    seen, handler = _recorder([(200, {"unread": False})])
    with _client(handler) as c:
        assert c.mark_read("session_1", event_id="ev-1") == {"unread": False}
    assert _body(seen[0]) == {"event_id": "ev-1"}


# --- teleport ------------------------------------------------------------------
def test_teleport_events_pages_and_drops_refused_from_first_event():
    seen, handler = _recorder([
        (400, {"error": {"type": "invalid_request_error"}}),
        (200, {"data": [{"payload": {"n": 1}}], "next_cursor": "k"}),
        (200, {"data": [{"payload": {"n": 2}}], "next_cursor": None}),
    ])
    with _client(handler) as c:
        rows = list(c.iter_teleport_events("cse_1", subagents=True))
    assert [r["payload"]["n"] for r in rows] == [1, 2]
    assert seen[0].url.path == "/v1/code/sessions/cse_1/teleport-events"
    assert seen[0].url.params["from_first_event"] == "true" and seen[0].url.params["limit"] == "1000"
    assert "from_first_event" not in seen[1].url.params and seen[1].url.params["subagents"] == "true"
    assert seen[2].url.params["cursor"] == "k"
    assert seen[0].headers["x-organization-uuid"] == "org-1"


# --- move / github -------------------------------------------------------------
def test_move_to_cloud_body():
    seen, handler = _recorder([(200, {"session": {"environment_kind": "anthropic_cloud", "environment_id": "env_2"}})])
    with _client(handler) as c:
        s = c.move_to_cloud("cse_1", "env_2", outcome_branch="claude/x", permission_mode="acceptEdits")
    assert s["environment_id"] == "env_2"
    assert seen[0].url.path == "/v1/code/sessions/cse_1/move-to-cloud"
    assert _body(seen[0]) == {"environment_id": "env_2", "outcome_branch": "claude/x", "permission_mode": "acceptEdits"}


def test_batch_branch_status_matches_har():
    status = {"repo": "acme/widget", "branch": "claude/x", "branch_exists": False}
    seen, handler = _recorder([(200, {"branch_statuses": [status]})])
    with _client(handler) as c:
        out = c.batch_branch_status([("https://github.com/acme/widget", "claude/x")],
                                    session_ids=["cse_01ABC"], discover_session_prs=True, caller="epitaxy-repopr")
    assert out == [status]
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/v1/code/github/get-batch-branch-status"
    assert req.url.params["caller"] == "epitaxy-repopr"
    assert _body(req) == {"repo_branches": [{"repo": "acme/widget", "branch": "claude/x"}],
                          "discover_session_prs": True, "session_ids": ["session_01ABC"]}


def test_get_code_repo_bearer_route():
    seen, handler = _recorder([(200, {"repo": {"default_branch": "main"}, "status": {"app_installed": True}})])
    with _client(handler) as c:
        assert c.get_code_repo("o/r")["status"]["app_installed"] is True
    req = seen[0]
    assert req.url.path == "/api/oauth/organizations/org-1/code/repos/o/r"
    assert req.headers["x-organization-uuid"] == "org-1"
    assert "anthropic-beta" not in req.headers


def _cookie_client(handler):
    return RemoteControlClient(session_key="test-cookie", org_uuid="org-1", transport=httpx.MockTransport(handler))


def test_cookie_repo_picker_preserves_response_and_observed_query_fields():
    response = {"repos": [{"repo": {"name": "widget"}}], "is_complete": False,
                "next_cursor": "opaque", "sso_required_orgs": []}
    seen, handler = _recorder([(200, response)])
    with _cookie_client(handler) as c:
        assert c.list_code_repos(page=2, per_page=5, skip_status=False) == response
    assert seen[0].url.path == "/api/organizations/org-1/code/repos"
    assert dict(seen[0].url.params) == {"page": "2", "per_page": "5", "skip_status": "false", "include_link_optional": "true"}
    assert "authorization" not in seen[0].headers


def test_cookie_branch_and_github_status_routes():
    seen, handler = _recorder([(200, {"branches": []})])
    with _cookie_client(handler) as c:
        c.list_repo_branches("git@github.com:acme/widget.git")
        c.github_installations_status(include_viewer_install_probe=False)
        c.github_auth_status()
    assert [r.url.path for r in seen] == [
        "/api/github/organizations/org-1/github/acme/widget/branches",
        "/v1/code/github/org-connection/installations-status",
        "/api/organizations/org-1/sync/github/auth"]
    assert seen[1].url.params["include_viewer_install_probe"] == "false"


def test_repository_auth_routes_fail_closed():
    seen, handler = _recorder([(200, {})])
    with _client(handler) as c:
        for call in (c.list_code_repos, c.github_auth_status, lambda: c.list_repo_branches("acme/widget")):
            with pytest.raises(ValueError, match="cookie"):
                call()
    with _cookie_client(handler) as c, pytest.raises(ValueError, match="bearer-only"):
        c.get_code_repo("acme/widget")
    assert not seen


@pytest.mark.parametrize("repo", ["", "acme", "acme/widget/extra", "/widget"])
def test_invalid_branch_repository_never_reaches_http(repo):
    seen, handler = _recorder([(200, {})])
    with _cookie_client(handler) as c, pytest.raises(ValueError):
        c.list_repo_branches(repo)
    assert not seen


def test_title_search_is_local_and_pages_filtered_sessions():
    seen, handler = _recorder([
        (200, {"data": [{"title": "Release FIX"}, {"title": None}], "next_cursor": "next"}),
        (200, {"data": [{"title": "fix tests"}], "next_cursor": None}),
    ])
    with _client(handler) as c:
        assert [r["title"] for r in c.search_sessions("FiX", statuses=["active"], tags=["owned"])] == ["Release FIX", "fix tests"]
    assert seen[1].url.params["cursor"] == "next"
    assert all("search" not in r.url.params for r in seen)
    assert seen[0].url.params["tags"] == "owned"


def test_session_pagination_repeated_cursor_is_bounded():
    seen, handler = _recorder([(200, {"data": [], "next_cursor": "same"})])
    with _client(handler) as c, pytest.raises(RuntimeError, match="repeated cursor"):
        list(c.iter_sessions())
    assert len(seen) == 2


def test_session_pagination_rejects_nonpositive_max_pages():
    seen, handler = _recorder([(200, {})])
    with _client(handler) as c, pytest.raises(ValueError):
        list(c.iter_sessions(max_pages=0))
    assert not seen


def test_captured_optional_repo_and_branch_query_fields():
    seen, handler = _recorder([(200, {})])
    with _cookie_client(handler) as c:
        c.list_code_repos(sort="updated", affiliation="owner", ghe_configuration_id=7, include_link_optional=False)
        c.list_repo_branches("acme/widget", query="feature/a", ghe_configuration_id=7)
    assert seen[0].url.params["sort"] == "updated"
    assert seen[0].url.params["affiliation"] == "owner"
    assert "include_link_optional" not in seen[0].url.params
    assert dict(seen[1].url.params) == {"query": "feature/a", "ghe_configuration_id": "7"}


def test_secret_scan_preserves_incomplete_state():
    response = {"secret_types": [], "complete": False}
    seen, handler = _recorder([(200, response)])
    with _cookie_client(handler) as c:
        assert c.scan_share_secrets("cse_1") == response
    assert seen[0].url.path == "/v1/code/sessions/cse_1/share/secret-scan"
    assert seen[0].method == "GET"
    assert not seen[0].content


def test_secret_scan_fallback_requires_explicit_caller_text():
    seen, handler = _recorder([(404, {}), (200, {"secrets": []})])
    with _cookie_client(handler) as c:
        assert c.scan_share_secrets("cse_1", fallback_text="test marker") == {"secrets": []}
    assert seen[1].url.path == "/api/organizations/org-1/code/shares/scan_secrets"
    assert _body(seen[1]) == {"text_content": ["test marker"]}


@pytest.mark.parametrize("status,text", [(404, None), (403, "test"), (400, "test")])
def test_secret_scan_does_not_hide_unavailable_or_auth_failures(status, text):
    seen, handler = _recorder([(status, {})])
    with _cookie_client(handler) as c, pytest.raises(APIError):
        c.scan_share_secrets("cse_1", fallback_text=text)
    assert len(seen) == 1


@pytest.mark.parametrize("method", ["create_support_share", "debug_export"])
def test_support_and_debug_require_explicit_confirmation(method):
    seen, handler = _recorder([(200, {})])
    with _client(handler) as c, pytest.raises(ValueError, match="confirm=True"):
        getattr(c, method)("cse_1")
    assert not seen


def test_captured_support_share_and_debug_export_bodies():
    seen, handler = _recorder([(200, {})])
    with _client(handler) as c:
        c.create_support_share("cse_1", confirm=True)
        c.revoke_support_share("cse_1")
        c.debug_export("cse_1", caption="owned test", confirm=True)
        c.debug_export("cse_1", confirm=True)
    assert [r.method for r in seen] == ["POST", "DELETE", "POST", "POST"]
    assert [r.url.path.rsplit("/", 1)[1] for r in seen] == ["support-share", "support-share", "debug-export", "debug-export"]
    assert [_body(r) for r in seen] == [{}, {}, {"caption": "owned test"}, {}]


# -- fork / templates / spaces (claude.ai/code bundle 2026-10-29) --------------
from claude_rc.commands import UnsupportedControl  # noqa: E402
from claude_rc.lifecycle import dispatch_lifecycle_cli, fork_result  # noqa: E402


@pytest.mark.parametrize("response, expected", [
    ({"forked": True, "sessionId": "cse_NEW", "cliSessionId": "c1", "prefillText": None,
      "targetMessageUuid": "m1"},
     {"status": "forked", "session_id": "session_NEW", "cli_session_id": "c1", "target_message_uuid": "m1"}),
    ({"forked": False, "reason": "requires_remote_control_server"},
     {"status": "requires_server", "reason": "requires_remote_control_server"}),
    ({"forked": False, "reason": "background_sessions_unavailable"},
     {"status": "background_unavailable", "reason": "background_sessions_unavailable"}),
    ({"forked": False, "reason": "other"}, {"status": "declined", "reason": "other"}),
    ({"forked": True}, {"status": "invalid"}),
    ({"forked": True, "sessionId": "bad id"}, {"status": "invalid"}),
    ({"forked": True, "sessionId": "cse_SRC"}, {"status": "invalid"}),
    ({}, {"status": "unsupported"}),
    (None, {"status": "unsupported"}),
    ({"forked": True, "sessionId": 7}, {"status": "unsupported"}),
    ({"forked": True, "sessionId": None}, {"status": "unsupported"}),
    ({"forked": False, "error": 1}, {"status": "unsupported"}),
    ({"forked": True, "sessionId": "cse_NEW", "prefillText": 3}, {"status": "unsupported"}),
])
def test_fork_result_matches_web_normalization(response, expected):
    assert fork_result(response, "session_SRC") == expected


def test_fork_session_sends_fork_conversation_on_bridge(monkeypatch):
    seen, handler = _recorder([(200, {})])
    c = RemoteControlClient(token="tok", org_uuid="org-1", control_profile="bridge",
                            transport=httpx.MockTransport(handler))
    calls = []

    def fake(session_id, subtype, *, timeout, **fields):
        calls.append((session_id, subtype, timeout, fields))
        return {"forked": True, "sessionId": "cse_CHILD"}

    monkeypatch.setattr(c, "request_control", fake)
    out = c.fork_session("session_SRC", target_message_uuid="m1", title="Fork", timeout=5)
    assert calls == [("session_SRC", "fork_conversation", 5, {"target_message_uuid": "m1", "title": "Fork"})]
    assert out == {"status": "forked", "session_id": "session_CHILD"}
    c.fork_session("session_SRC")
    assert calls[-1][3] == {}


def test_fork_session_cloud_rejected_before_http():
    seen, handler = _recorder([(200, {})])
    c = RemoteControlClient(token="tok", org_uuid="org-1", control_profile="cloud",
                            transport=httpx.MockTransport(handler))
    with pytest.raises(UnsupportedControl):
        c.fork_session("session_SRC")
    assert seen == []


def test_list_project_templates_route():
    seen, handler = _recorder([(200, {"templates": [], "setup_templates": []})])
    assert _client(handler).list_project_templates() == {"templates": [], "setup_templates": []}
    assert seen[0].method == "GET" and seen[0].url.path == "/v1/code/project-templates"


def test_list_spaces_params_and_disabled_error():
    seen, handler = _recorder([(200, {"data": []}), (400, {"error": {
        "message": "spaces are not available for this organization", "reason": "spaces_disabled",
        "type": "invalid_request_error"}})])
    c = _client(handler)
    assert c.list_spaces(limit=1, cursor="k", scope="mine", include_capabilities=True) == {"data": []}
    assert seen[0].url.path == "/v1/code/spaces"
    assert dict(seen[0].url.params) == {"include_capabilities": "true", "limit": "1", "cursor": "k", "scope": "mine"}
    with pytest.raises(APIError) as err:
        c.list_spaces()
    assert err.value.status == 400 and "spaces_disabled" in err.value.body
    assert "include_capabilities" not in seen[1].url.params


def test_lifecycle_cli_registration(monkeypatch, capsys):
    from claude_rc import cli

    args = cli.build_parser().parse_args(["fork", "session_SRC", "--message", "m1", "--profile", "bridge"])
    assert args.lifecycle_command == "fork" and args.target_message_uuid == "m1"

    class Fake:
        control_profile = "cloud"

        def fork_session(self, sid, **kw):
            assert self.control_profile == "bridge" and kw["target_message_uuid"] == "m1"
            return {"status": "requires_server"}

        def list_project_templates(self):
            return {"templates": []}

    assert dispatch_lifecycle_cli(Fake(), args) == 1
    assert '"requires_server"' in capsys.readouterr().out
    targs = cli.build_parser().parse_args(["templates"])
    assert dispatch_lifecycle_cli(Fake(), targs) == 0
    assert cli.build_parser().parse_args(["spaces", "--capabilities"]).capabilities is True
