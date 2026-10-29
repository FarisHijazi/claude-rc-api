# 0.11.0 canonical integration (capture branches)

Merged into `main`, sequentially, each independently verified:

- `feat/capture-lifecycle` @ `36af2be`: bridge `fork_session` + `fork_result`, project templates, spaces read; groupings documented only.
- `feat/capture-scm` @ `00b1591`: `ScmMixin` compare refs / get commit / get file content; PR writes captured, not implemented. Carried the 0.11.0 bump (one minor bump for all three).
- `feat/capture-dialogs` @ `f74fbbe`: MCP form elicitation live proof; joining-client dialogs/hooks and Chrome forced-ask recorded as unsupported.

Conflicts were only in `docs/web-api.md`; both sides kept. The elicitation branch reused evidence key `E`
(already group06 environments), so it is now `EL`; `FK`, `S1` and `I11` keys added.
`claude_2026-10-29-completeness-followup.json` now lists only the integration-fire route as uncaptured.

Tests: `uv run --offline --extra test --extra web --extra cli --with playwright --with 'mcp==1.30.0' pytest -q`
with the nvm node `bin` first on PATH: 839 passed, 0 skipped, 3 existing deprecation warnings.
Network uv resolution fails here (proxy CA), hence `--offline`; the SCM smoke needs
`SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` for its GitHub cross-check.

Live reruns on merged main (all passed, exact cleanup) are in
`claude_2026-10-29-capture-integration.json` (plain path).
