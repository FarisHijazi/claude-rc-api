"""Generated-data-only MCP elicitation; SDK FastMCP owns the protocol."""
from pathlib import Path
import json
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel

mcp = FastMCP("generated-elicitation")


class Reply(BaseModel):
    name: str
    count: int


@mcp.tool()
async def ask(marker: str, ctx: Context) -> dict:
    """Ask the session's host a generated form question and report its answer."""
    result = await ctx.elicit(message=f"Generated elicitation {marker}", schema=Reply)
    data = result.data.model_dump() if result.action == "accept" else None
    row = {"marker": marker, "action": result.action, "content": data}
    with Path(__file__).with_suffix(".elicit").open("a") as ledger:
        ledger.write(json.dumps(row) + "\n")
    return row


if __name__ == "__main__":
    mcp.run(transport="stdio")
