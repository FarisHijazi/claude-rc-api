# MCP independent verification

Reviewed `1da331dc8804c8e8bf791e8f9f1f541c94f1a946` in the existing
`worktrees/protocol-mcp` checkout. No reset, stash, worktree recreation or
duplicate worker was used.

- Full suite: 781 passed, 3 existing legacy-routine warnings.
- Live cloud harness: `claude_2026-10-29-protocol-mcp-independent-live.json`.
  It proved actual SDK child replies, toggle/reconnect, permission-rule reuse,
  byte-exact staging, CAS conflict, expiry rejection and exact child exit.
- Scope and cleanup: `claude_2026-10-29-protocol-mcp-independent-summary.json`.
  It records exact-session routing and fail-before-HTTP gates offline, plus a
  fresh-client recheck of eight deleted sessions and archived environments.
- Result: no confirmed defect; only evidence and documentation changed.

Limitations: live cross-session isolation, override enforcement, Chrome
forced-ask, remote REST variants, natural expiry and large-file/platform
variants remain unqualified. These paths are plain references, not includes.
