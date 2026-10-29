"""Worker MCP replacement uses the existing control owner, never account REST."""
import pytest

from claude_rc.client import ControlRejected
from test_interaction import FakeSession, SID, _client


@pytest.mark.parametrize("servers", [{}, {"fixture": {"type": "stdio", "command": "python3", "args": ["fixture.py"]}}])
def test_worker_mcp_replacement_is_admitted_on_cloud_durable_channel(servers):
    reply = {"added": list(servers), "removed": [], "errors": {}}
    fake = FakeSession(responses={"mcp_set_servers": reply})
    with _client(fake.handler) as client:
        client.control_profile = "cloud"
        assert client.set_worker_mcp_servers(SID, servers, confirm=True) == reply
    assert fake.last_request() == {"subtype": "mcp_set_servers", "servers": servers}
    assert all(p["type"] == "control_request" for p in fake.posted)
    assert not any(r.url.path.endswith("/mcp-servers") for r in fake.requests)


@pytest.mark.parametrize("confirmation", [False, None, 1, "yes"])
def test_worker_mcp_replacement_requires_explicit_confirmation(confirmation):
    fake = FakeSession()
    with _client(fake.handler) as client:
        with pytest.raises(ValueError, match="confirm=True"):
            client.set_worker_mcp_servers(SID, {}, confirm=confirmation)
    assert fake.requests == []


@pytest.mark.parametrize("servers", [None, [], "fixture", {"": {}}, {1: {}}, {"fixture": []}])
def test_worker_mcp_invalid_mapping_never_writes(servers):
    fake = FakeSession()
    with _client(fake.handler) as client:
        with pytest.raises(ValueError, match="servers must map"):
            client.set_worker_mcp_servers(SID, servers, confirm=True)
    assert fake.requests == []


def test_worker_mcp_connection_errors_are_not_hidden_by_success_verdict():
    reply = {"added": ["fixture"], "errors": {"fixture": "Connection closed"}, "removed": []}
    fake = FakeSession(responses={"mcp_set_servers": reply})
    with _client(fake.handler) as client:
        assert client.set_worker_mcp_servers(SID, {"fixture": {}}, confirm=True) == reply


def test_sdk_fixture_real_stdio_child_returns_exact_generated_bytes(tmp_path):
    import asyncio
    import hashlib
    import json
    from pathlib import Path
    import shutil
    import sys

    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    script = tmp_path / "fixture.py"
    shutil.copyfile(Path(__file__).parent / "fixtures/mcp_generated.py", script)
    data = bytes(range(256)) + b"\x00generated\xff"
    source, output = tmp_path / "source.bin", tmp_path / "output.bin"
    source.write_bytes(data)

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[str(script)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert {t.name for t in (await session.list_tools()).tools} == {"echo", "copy_bytes"}
                echo = await session.call_tool("echo", {"value": "GENERATED_OK"})
                assert not echo.isError and echo.content[0].text == "GENERATED_OK"
                reply = await session.call_tool("copy_bytes", {"input_path": str(source), "output_path": str(output)})
                assert not reply.isError
                result = json.loads(reply.content[0].text)
                assert result["sha256"] == hashlib.sha256(data).hexdigest()
                assert result["size"] == len(data)
                return result["pid"]

    pid = asyncio.run(exercise())
    assert output.read_bytes() == data
    assert script.with_suffix(".calls").read_text() == "1"
    if sys.platform == "linux":
        assert not Path(f"/proc/{pid}").exists()


@pytest.mark.parametrize("failure", [None, "archive_session", "delete_session", "get_session", "archive_environment", "list_environments"])
def test_smoke_cleanup_attempts_both_owned_resources_and_fails_closed(failure):
    from pathlib import Path
    import runpy
    from types import SimpleNamespace
    from claude_rc import APIError

    cleanup = runpy.run_path(str(Path(__file__).parents[1] / "scripts/claude-rc-mcp-smoke"))["cleanup_resources"]
    calls, saved = [], []

    def operation(name):
        def call(*args):
            calls.append((name, args))
            if failure == name:
                raise RuntimeError("injected failure")
            if name == "get_session":
                raise APIError(404, "missing")
            if name == "list_environments":
                return []
        return call

    names = ("archive_session", "delete_session", "get_session", "archive_environment", "list_environments")
    client = SimpleNamespace(**{name: operation(name) for name in names})
    proof = {"complete": True, "cleanup": []}
    if failure in (None, "archive_session"):
        cleanup(client, "owned-session", "owned-env", proof, lambda: saved.append(True))
        assert proof["complete"] is True
        assert {"session_id": "owned-session", "get_status": 404} in proof["cleanup"]
    else:
        with pytest.raises(RuntimeError, match="owned resource cleanup failed"):
            cleanup(client, "owned-session", "owned-env", proof, lambda: saved.append(True))
        assert proof["complete"] is False
    assert ("delete_session", ("owned-session",)) in calls
    assert ("archive_environment", ("owned-env",)) in calls
    assert saved == [True]


def test_worker_mcp_unsupported_worker_propagates_rejection():
    fake = FakeSession(errors={"mcp_set_servers": "callback not registered"})
    with _client(fake.handler) as client:
        with pytest.raises(ControlRejected, match="callback not registered"):
            client.set_worker_mcp_servers(SID, {}, confirm=True)
