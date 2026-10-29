# Elicitation, dialog, hook and Chrome forced-ask qualification

- Added `scripts/claude-rc-elicitation-smoke` and `tests/fixtures/mcp_elicitation.py`
  (FastMCP 1.30.0 child calling `ctx.elicit`), plus `tests/elicitation_fixture_test.py`.
- Live: cloud worker 2.1.312 relayed the child's elicitation as a `control_request`;
  `answer_elicitation` accept/decline/cancel each reached the child (exact tool
  result + ledger). Cleanup: session 404, environment excluded.
- Unsupported with evidence: `request_user_dialog` (first-attached client's
  `supportedDialogKinds`), `hook_callback` (`hooks_applied:false` for joining
  clients), Chrome forced-ask (no `claude-in-chrome` server on the cloud worker).
- `prompts.py` docstrings now state these boundaries; no behavior change.
- Details: `claude_2026-10-29-elicitation-summary.json`, `claude_2026-10-29-elicitation-live.json`.
- Independent verify: 813 tests pass; CLI contract strings rechecked in the 2.1.312
  binary; live run 1 failed at fixture install (`mcp_set_servers`, exact cleanup),
  run 2 passed accept/decline/cancel. Smoke assertion now records the reply.
  Evidence: `claude_2026-10-29-elicitation-independent.json`.
