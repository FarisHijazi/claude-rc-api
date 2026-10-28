"""Environment/account wire contracts, offline only; live evidence is separate."""

import copy
import json

import httpx
import pytest
from claude_rc.account import dispatch_account_command, register_account_commands
from claude_rc.client import RemoteControlClient
from claude_rc.environments import (
    merge_environment_config,
    redact_environment,
    redact_network_material,
)


def recorder(responses, *, cookie=True):
    seen = []

    def handler(request):
        seen.append(request)
        status, body = responses[len(seen) - 1]
        return httpx.Response(status, json=body)

    auth = {"session_key": "test-cookie"} if cookie else {"token": "test-token"}
    return RemoteControlClient(
        org_uuid="org-test", transport=httpx.MockTransport(handler), **auth
    ), seen


STORED = {
    "name": "test",
    "description": "original",
    "environment_id": "env-test",
    "config": {
        "cwd": "/home/user",
        "init_script": "true",
        "languages": [{"name": "python", "version": "3.11"}],
        "environment": {"A": "one", "B": "two"},
        "network_config": {
            "allowed_hosts": ["example.com"],
            "allow_default_hosts": True,
            "allow_mcp_servers": True,
        },
    },
}


@pytest.mark.parametrize("cookie", [True, False])
def test_create_uses_auth_lineage(cookie):
    client, seen = recorder([(200, {})], cookie=cookie)
    with client:
        client.create_cloud_environment(
            "test",
            environment={"MARKER": "nonsecret"},
            network_config={"allow_mcp_servers": False},
        )
    prefix = (
        "/v1/environment_providers/private/organizations/org-test"
        if cookie
        else "/v1/environment_providers"
    )
    assert seen[0].url.path == prefix + "/cloud/create"
    body = json.loads(seen[0].content)
    assert body["config"]["network_config"] == {
        "allowed_hosts": [],
        "allow_default_hosts": True,
        "allow_mcp_servers": False,
    }
    assert body["config"]["environment"] == {"MARKER": "nonsecret"}


@pytest.mark.parametrize("cookie", [True, False])
def test_partial_update_preserves_nested_config(cookie):
    client, seen = recorder([(200, STORED), (200, {})], cookie=cookie)
    with client:
        client.update_environment(
            "env-test",
            description="changed",
            config={
                "init_script": None,
                "environment": {"A": "new"},
                "network_config": {"allow_default_hosts": False},
            },
        )
    sent = json.loads(seen[1].content)
    expected = copy.deepcopy(STORED["config"])
    expected["init_script"] = None
    expected["environment"]["A"] = "new"
    expected["network_config"]["allow_default_hosts"] = False
    assert sent == {"name": "test", "description": "changed", "config": expected}
    prefix = (
        "/v1/environment_providers/private/organizations/org-test/environments"
        if cookie
        else "/v1/environment_providers"
    )
    assert seen[1].url.path == prefix + "/env-test"
    assert STORED["config"]["environment"]["A"] == "one"


def test_explicit_empty_objects_clear_but_omitted_fields_survive():
    assert (
        merge_environment_config(STORED["config"], {"environment": {}})["environment"]
        == {}
    )
    assert merge_environment_config(STORED["config"], {}) == STORED["config"]


def test_archive_uses_browser_post_not_invented_delete():
    client, seen = recorder([(200, {})])
    with client:
        client.archive_environment("env-test")
    assert seen[0].method == "POST"
    assert seen[0].url.path.endswith("/environments/env-test/archive")
    assert json.loads(seen[0].content) == {}


@pytest.mark.parametrize("cookie", [True, False])
def test_environment_paging_is_auth_aware(cookie):
    client, seen = recorder(
        [
            (
                200,
                {
                    "environments": [{"environment_id": "one"}],
                    "has_more": True,
                    "last_id": "one",
                },
            ),
            (200, {"environments": [{"environment_id": "two"}], "has_more": False}),
        ],
        cookie=cookie,
    )
    with client:
        assert len(client.list_environments(page_size=2)) == 2
    assert seen[1].url.params["after_id"] == "one"
    assert seen[0].url.params["limit"] == "2"
    assert ("/private/" in seen[0].url.path) == cookie


def test_private_bare_list_and_metadata_filters():
    client, _seen = recorder([(200, [{"kind": "bridge"}, {"kind": "anthropic_cloud"}])])
    with client:
        assert client.list_environments(kind="bridge") == [{"kind": "bridge"}]


def test_paging_repeated_cursor_fails_instead_of_partial_success():
    page = {
        "environments": [{"environment_id": "one"}],
        "has_more": True,
        "last_id": "one",
    }
    client, seen = recorder([(200, page), (200, page)])
    with client, pytest.raises(RuntimeError, match="no progress"):
        client.list_environments()
    assert len(seen) == 2


@pytest.mark.parametrize(
    "method,args,path,verb,body",
    [
        (
            "create_agent_proxy_profile",
            ("test-only",),
            "/agent-proxy-profiles",
            "POST",
            {"slug": "test-only", "display_name": "test"},
        ),
        (
            "update_agent_proxy_profile",
            ("p", {"display_name": "new", "updated_at": "version"}),
            "/agent-proxy-profiles/p",
            "PATCH",
            {
                "agent_proxy_profile_id": "p",
                "display_name": "new",
                "updated_at": "version",
            },
        ),
        ("delete_agent_proxy_profile", ("p",), "/agent-proxy-profiles/p", "DELETE", {}),
        (
            "bind_environment_profile",
            ("p", "e"),
            "/agent-proxy-profiles/p/bindings",
            "POST",
            {
                "agent_proxy_profile_id": "p",
                "scope": {"kind": "KIND_ENVIRONMENT", "id": "e"},
            },
        ),
        (
            "create_network_credential",
            (
                {
                    "agent_proxy_profile_id": "p",
                    "spec": {
                        "type": "basic",
                        "password_value": {"inline": "test-only"},
                    },
                },
            ),
            "/agent-proxy/credentials",
            "POST",
            {
                "credential": {
                    "agent_proxy_profile_id": "p",
                    "spec": {
                        "type": "basic",
                        "password_value": {"inline": "test-only"},
                    },
                }
            },
        ),
        (
            "rotate_network_credential",
            ("c", {"spec": {}}),
            "/agent-proxy/credentials/c/rotate",
            "POST",
            {"credential": {"spec": {}}},
        ),
        (
            "delete_network_credential",
            ("c",),
            "/agent-proxy/credentials/c",
            "DELETE",
            {},
        ),
        (
            "create_network_rule",
            ({"priority": 100},),
            "/agent-proxy/rules",
            "POST",
            {"rule": {"priority": 100}},
        ),
        ("delete_network_rule", ("r",), "/agent-proxy/rules/r", "DELETE", {}),
        (
            "create_environment_credential",
            (
                "e",
                "example.com",
                {"basic": {"username": "test", "password": "test-only"}},
            ),
            "/agent-proxy/environment-credentials",
            "POST",
            {
                "environment_id": "e",
                "host": "example.com",
                "basic": {"username": "test", "password": "test-only"},
            },
        ),
        (
            "delete_environment_credential",
            ("e", "c"),
            "/agent-proxy/environment-credentials/c",
            "DELETE",
            {},
        ),
    ],
)
def test_network_write_contracts(method, args, path, verb, body):
    client, seen = recorder([(200, {})])
    kwargs = {"display_name": "test"} if method == "create_agent_proxy_profile" else {}
    with client:
        getattr(client, method)(*args, **kwargs)
    assert seen[0].url.path == "/v1/code" + path
    assert seen[0].method == verb
    assert json.loads(seen[0].content) == body


def test_network_updates_and_unbinding():
    client, seen = recorder([(200, {})] * 3)
    with client:
        client.bind_environment_profile("p", "e", unbind=True)
        client.update_network_rule("r", {"priority": 10}, expected_version=3)
        client.update_network_credential(
            "c", display_name="new", host_constraint=["example.com"]
        )
    assert seen[0].url.path.endswith("/bindings:unbind")
    assert json.loads(seen[1].content) == {
        "rule": {"priority": 10},
        "expected_version": 3,
    }
    assert json.loads(seen[2].content) == {
        "display_name": "new",
        "host_constraint": ["example.com"],
    }


def test_network_reads_and_filters():
    client, seen = recorder([(200, {})] * 5)
    with client:
        client.list_agent_proxy_profiles(
            include_bindings=True, include_github_permissions=True
        )
        client.get_agent_proxy_profile("p")
        client.list_network_credentials()
        client.list_network_rules()
        client.list_environment_credentials("env")
    assert all(request.method == "GET" for request in seen)
    assert dict(seen[0].url.params) == {
        "include_bindings": "true",
        "include_github_permissions": "true",
    }
    assert seen[-1].url.params["environment_id"] == "env"


def test_redaction_is_recursive_and_does_not_modify_source():
    source = {
        **copy.deepcopy(STORED),
        "credential": {"spec": "sensitive"},
        "bearer": {"value": "sensitive"},
        "other": [{"inline": "sensitive"}],
        "network_secrets": ["sensitive"],
    }
    result = redact_environment(source)
    assert "sensitive" not in json.dumps(result)
    assert "one" not in json.dumps(result)
    assert result["config"]["environment"]["A"] == "<redacted>"
    assert source["credential"]["spec"] == "sensitive"
    assert redact_network_material({"password_value": {"inline": "sensitive"}}) == {
        "password_value": "<redacted>"
    }


@pytest.mark.parametrize(
    "method,path",
    [
        ("get_account_usage", "/api/organizations/org-test/usage"),
        ("get_user_settings", "/api/claude_code/organizations/org-test/user_settings"),
        ("get_account_profile", "/api/account_profile"),
        (
            "get_subscription_details",
            "/api/organizations/org-test/subscription_details",
        ),
        (
            "list_account_connectors",
            "/api/organizations/org-test/mcp/remote_servers_with_connection",
        ),
        ("list_account_skills", "/api/organizations/org-test/skills/list-skills"),
    ],
)
def test_account_discovery_is_read_only(method, path):
    client, seen = recorder([(200, {"key": "value"})])
    with client:
        assert getattr(client, method)() == {"key": "value"}
    assert (seen[0].method, seen[0].url.path) == ("GET", path)


def test_absent_user_settings_is_not_a_fake_empty_row():
    client, _seen = recorder([(404, {})])
    with client:
        assert client.get_user_settings() is None


def test_account_cli_helpers_do_not_expose_settings_content():
    import argparse

    parser = argparse.ArgumentParser()
    register_account_commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["account", "settings-shape"])
    client, _ = recorder(
        [(200, {"content": {"sensitive": "secret"}, "checksum": "digest"})]
    )
    with client:
        assert dispatch_account_command(client, args) == {
            "exists": True,
            "keys": ["checksum", "content"],
        }


def test_pagination_budget_rejects_partial_results(monkeypatch):
    from claude_rc import environments

    monkeypatch.setattr(environments, "_MAX_PAGES", 1)
    page = {
        "environments": [{"environment_id": "one"}],
        "has_more": True,
        "last_id": "one",
    }
    client, _ = recorder([(200, page)])
    with client, pytest.raises(RuntimeError, match="page budget"):
        client.list_environments()


def test_network_policy_failure_is_not_an_empty_success():
    from claude_rc.client import APIError

    client, _ = recorder(
        [(403, {"error": {"type": "permission_error", "message": "policy denied"}})]
    )
    with client, pytest.raises(APIError) as error:
        client.list_environment_credentials("env-test")
    assert error.value.status == 403


def test_last_used_environment_404_and_pool_envelope():
    client, _ = recorder([(404, {}), (200, {"pools": [{"pool_id": "ccpool_test"}]})])
    with client:
        assert client.last_used_environment() is None
        assert client.list_self_hosted_pools() == [{"pool_id": "ccpool_test"}]
