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
