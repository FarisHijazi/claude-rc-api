"""Generated-data-only tools; SDK FastMCP owns the entire MCP server protocol."""
from pathlib import Path
import hashlib
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("generated-fixture")


@mcp.tool()
def echo(value: str) -> str:
    """Return the supplied generated marker without any external access."""
    return value


@mcp.tool()
def copy_bytes(input_path: str, output_path: str) -> dict:
    """Copy generated staged bytes and return their length and SHA-256."""
    data = Path(input_path).read_bytes()
    Path(output_path).write_bytes(data)
    ledger = Path(__file__).with_suffix(".calls")
    ledger.write_text(str(int(ledger.read_text()) + 1 if ledger.exists() else 1))
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "pid": os.getpid()}


if __name__ == "__main__":
    mcp.run(transport="stdio")
