"""Session-specific command enumeration must not imply universal CLI parity."""
import argparse

import pytest

from claude_rc.commands import (
    LocalCommandRequired, WEB_SLASH_COMMANDS, dispatch_command_cli, register_command_cli,
)
from test_interaction import FakeSession, _client, SID


def test_capabilities_separates_web_names_api_extensions_and_worker_skills():
    fake = FakeSession(responses={"initialize": {
        "claude_code_version": "test-worker", "capabilities": ["start_shell_task_v1"],
        "commands": [{"name": "safe-skill", "aliases": ["safe"]},
                     {"name": "design"}, {"name": "autocompact"}],
    }})
    with _client(fake.handler) as client:
        snapshot = client.command_capabilities(SID)
    rows = {row["name"]: row for row in snapshot["commands"]}
    assert snapshot["worker_version"] == "test-worker"
    assert snapshot["capabilities"] == ["start_shell_task_v1"]
    assert set(snapshot["web_catalog"]) == WEB_SLASH_COMMANDS
    assert rows["model"]["web_catalog"] and rows["model"]["available"]
    assert rows["cost"]["available"] and not rows["cost"]["web_catalog"]
    assert rows["safe-skill"]["worker_advertised"] and rows["safe-skill"]["route"] == "worker"
    assert not rows["safe-skill"]["web_catalog"]
    assert rows["design"]["worker_advertised"] and not rows["design"]["web_catalog"]
    assert not rows["design"]["available"] and rows["design"]["reason"]
    assert rows["autocompact"]["web_catalog"] and not rows["autocompact"]["available"]
    assert [row["request"]["subtype"] for row in fake.posted] == ["initialize"]


def test_available_local_handler_is_not_implied_by_a_web_catalog_entry():
    fake = FakeSession()
    with _client(fake.handler) as client:
        snapshot = client.command_capabilities(SID, local_handlers={"theme": lambda _: None, "feedback": lambda _: None, "sandbox": None})
    rows = {row["name"]: row for row in snapshot["commands"]}
    assert rows["theme"]["available"] and rows["theme"]["reason"] is None
    assert not rows["sandbox"]["available"] and "rejected" in rows["sandbox"]["reason"]
    assert not rows["feedback"]["available"] and rows["feedback"]["route"] == "unsupported"


@pytest.mark.parametrize("name", ["status", "autocompact", "design", "slides"])
def test_advertised_host_commands_cannot_fall_through_to_plain_chat(name):
    fake = FakeSession()
    with _client(fake.handler) as client:
        with pytest.raises(LocalCommandRequired):
            client.run_command(SID, name, commands=[{"name": name}], strict=False)
    assert fake.requests == []


def test_cli_capabilities_is_executable_without_a_command_or_prompt():
    parser = argparse.ArgumentParser()
    register_command_cli(parser.add_subparsers(required=True))
    args = parser.parse_args(["dispatch", SID, "--capabilities"])
    fake = FakeSession()
    with _client(fake.handler) as client:
        result = dispatch_command_cli(client, args)
    assert result["web_catalog"] == sorted(WEB_SLASH_COMMANDS)
    assert all(row["type"] == "control_request" for row in fake.posted)


def test_cli_capabilities_rejects_mixed_dispatch_before_network():
    parser = argparse.ArgumentParser()
    register_command_cli(parser.add_subparsers(required=True))
    args = parser.parse_args(["dispatch", SID, "model", "other", "--capabilities"])
    fake = FakeSession()
    with _client(fake.handler) as client:
        with pytest.raises(ValueError):
            dispatch_command_cli(client, args)
    assert fake.requests == []
