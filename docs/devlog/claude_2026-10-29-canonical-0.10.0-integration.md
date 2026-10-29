# 0.10.0 canonical integration (MCP + queue ownership)

- Merged verified `feat/protocol-mcp` and `feat/protocol-queue-receipts` into `main`; one
  `docs/web-api.md` conflict resolved keeping both sides.
- Hatch bump to 0.10.0 (`set_worker_mcp_servers` is new surface).
- 811 tests pass with `--with playwright --with 'mcp==1.30.0'` and the real node `bin` first
  on `PATH` (the mise shim times out under the tests' empty `HOME`).
- Fresh live reruns of both harnesses on merged main, exact cleanup.
- Index: `docs/devlog/claude_2026-10-29-canonical-integration.json` (plain path).
