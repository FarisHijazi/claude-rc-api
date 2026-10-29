"""Generated-data-only MCP elicitation; SDK FastMCP owns the protocol."""
from datetime import timedelta
from pathlib import Path
import json

import anyio
from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from pydantic import BaseModel

mcp = FastMCP("generated-elicitation")


class Reply(BaseModel):
    name: str
    count: int


def record(row: dict) -> dict:
    with Path(__file__).with_suffix(".elicit").open("a") as ledger:
        ledger.write(json.dumps(row) + "\n")
    return row


@mcp.tool()
async def ask(marker: str, ctx: Context) -> dict:
    """Ask the session's host a generated form question and report its answer."""
    try:
        result = await ctx.elicit(message=f"Generated elicitation {marker}", schema=Reply)
    except anyio.get_cancelled_exc_class():
        record({"marker": marker, "action": "tool_cancelled", "content": None})
        raise
    data = result.data.model_dump() if result.action == "accept" else None
    return record({"marker": marker, "action": result.action, "content": data})


@mcp.tool()
async def ask_url(marker: str, ctx: Context) -> dict:
    """Ask the host to visit a generated URL (URL-mode elicitation) and report its answer."""
    result = await ctx.elicit_url(message=f"Generated URL elicitation {marker}",
                                  url=f"https://example.com/claude-rc/{marker}", elicitation_id=marker)
    return record({"marker": marker, "mode": "url", "action": result.action})


@mcp.tool()
async def ask_timeout(marker: str, seconds: float, ctx: Context) -> dict:
    """Ask a form question, give up after ``seconds`` and cancel it per MCP."""
    session = ctx.session
    request_id = session._request_id
    params = types.ElicitRequestFormParams(message=f"Generated elicitation {marker}",
                                           requestedSchema=Reply.model_json_schema())
    try:
        result = await session.send_request(types.ServerRequest(types.ElicitRequest(params=params)),
                                            types.ElicitResult, request_read_timeout_seconds=timedelta(seconds=seconds))
    except McpError as error:
        if error.error.code != 408:
            raise
        await session.send_notification(types.ServerNotification(types.CancelledNotification(
            params=types.CancelledNotificationParams(requestId=request_id, reason="timeout"))))
        return record({"marker": marker, "action": "timeout", "code": error.error.code})
    return record({"marker": marker, "action": result.action})


if __name__ == "__main__":
    mcp.run(transport="stdio")
