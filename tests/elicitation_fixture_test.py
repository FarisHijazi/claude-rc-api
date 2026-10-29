"""The elicitation fixture's SDK child returns exactly what its host answered."""
import asyncio
import json
from pathlib import Path
import shutil
import sys

import pytest

from claude_rc.events import cli_elicitation_response

ANSWERS = [("accept", {"name": "Ada", "count": 3}), ("decline", None), ("cancel", None)]


def test_real_sdk_child_relays_each_action_and_content(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters, types
    from mcp.client.stdio import stdio_client

    script = tmp_path / "fixture.py"
    shutil.copyfile(Path(__file__).parent / "fixtures/mcp_elicitation.py", script)
    seen, answers = [], iter(ANSWERS)

    async def host(context, params):
        action, content = next(answers)
        seen.append((params.message, sorted(params.requestedSchema["properties"])))
        wire = cli_elicitation_response("r", action, content)["response"]["response"]
        return types.ElicitResult(**wire)

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[str(script)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, elicitation_callback=host) as session:
                await session.initialize()
                out = []
                for action, _ in ANSWERS:
                    reply = await session.call_tool("ask", {"marker": action})
                    assert not reply.isError
                    out.append(json.loads(reply.content[0].text))
                return out

    results = asyncio.run(exercise())
    assert results == [{"marker": a, "action": a, "content": c} for a, c in ANSWERS]
    assert seen == [(f"Generated elicitation {a}", ["count", "name"]) for a, _ in ANSWERS]
    ledger = [json.loads(line) for line in script.with_suffix(".elicit").read_text().splitlines()]
    assert ledger == results


def test_smoke_child_result_parses_string_and_block_content():
    import runpy
    smoke = runpy.run_path(str(Path(__file__).parents[1] / "scripts/claude-rc-elicitation-smoke"))
    row = {"marker": "m", "action": "decline", "content": None}
    assert smoke["child_result"]({"content": json.dumps(row)}) == row
    assert smoke["child_result"]({"content": [{"type": "text", "text": json.dumps(row)}]}) == row


def _run_child(tmp_path, host, calls):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    script = tmp_path / "fixture.py"
    shutil.copyfile(Path(__file__).parent / "fixtures/mcp_elicitation.py", script)

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[str(script)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, elicitation_callback=host) as session:
                await session.initialize()
                out = []
                for tool, arguments in calls:
                    reply = await session.call_tool(tool, arguments)
                    assert not reply.isError
                    out.append(json.loads(reply.content[0].text))
                return out

    results = asyncio.run(exercise())
    ledger = [json.loads(line) for line in script.with_suffix(".elicit").read_text().splitlines()]
    assert ledger == results
    return results


def test_url_mode_child_receives_bare_web_actions(tmp_path):
    pytest.importorskip("mcp")
    from mcp import types
    seen, actions = [], iter(["accept", "decline", "cancel"])

    async def host(context, params):
        seen.append((params.mode, params.url, params.elicitationId, params.message))
        wire = cli_elicitation_response("r", next(actions))["response"]["response"]
        return types.ElicitResult(**wire)

    results = _run_child(tmp_path, host, [("ask_url", {"marker": a}) for a in ("A", "D", "C")])
    assert results == [{"marker": m, "mode": "url", "action": a}
                       for m, a in (("A", "accept"), ("D", "decline"), ("C", "cancel"))]
    assert seen == [("url", f"https://example.com/claude-rc/{m}", m, f"Generated URL elicitation {m}")
                    for m in ("A", "D", "C")]


def test_timeout_child_gives_up_and_cancels_unanswered_request(tmp_path):
    import anyio
    started = []

    async def host(context, params):
        started.append(params.message)
        await anyio.sleep(3)

    results = _run_child(tmp_path, host, [("ask_timeout", {"marker": "T", "seconds": 0.5})])
    assert started == ["Generated elicitation T"]
    assert results == [{"marker": "T", "action": "timeout", "code": 408}]


def test_fixture_install_prewarms_then_offline_startup():
    import runpy
    smoke = runpy.run_path(str(Path(__file__).parents[1] / "scripts/claude-rc-mcp-smoke"))
    prompt = smoke["fixture_install_prompt"](b"print('hi')", "/working/fixture.py")
    assert "uv run --with mcp==1.30.0 python -c 'import mcp.server.fastmcp'" in prompt
    assert "MCP_WARM_SECONDS=" in prompt and "sha256sum /working/fixture.py" in prompt
    config = smoke["fixture_server"]("/working/fixture.py")
    assert config == {"type": "stdio", "command": "uv",
                      "args": ["run", "--offline", "--with", "mcp==1.30.0", "python", "/working/fixture.py"]}
    assert smoke["warm_seconds"]("sha file\nMCP_WARM_SECONDS=41\n") == 41
    assert smoke["warm_seconds"]([{"type": "text", "text": "MCP_WARM_SECONDS=0"}]) == 0
    with pytest.raises(AssertionError, match="did not finish"):
        smoke["warm_seconds"]("download failed")
