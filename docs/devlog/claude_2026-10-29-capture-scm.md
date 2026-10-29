# 2026-10-29 SCM read capture (0.11.0)

- Loaded the current claude.ai/code in one owned dev-Chrome CDP tab (closed) and
  read the bundle: compare-refs / get-commit / get-file-content are
  `POST /v1/code/github/<op>` with `{repo:"owner/name", ...}` plus
  `ghe_configuration_id` only when nonzero. No session id in the body.
- Added `claude_rc/scm.py` (`ScmMixin`, one base on `RemoteControlClient`),
  `tests/test_scm.py`, and the read-only `scripts/claude-rc-scm-smoke`.
- Live: all three reads on `octocat/Hello-World` match GitHub's public REST API and
  a locally computed git blob SHA; missing ref -> `None`, missing file -> 404.
- Two disposable public-repo sessions: the browser only emitted branch-status and
  branch-picker reads. The server suffixes every outcome branch, so compare-refs
  needs a pushed branch, which a read-only repo cannot have. Both deleted (GET 404).
- PR create / generate-content / subscribe / unsubscribe / mute contracts captured,
  approval-gated, not implemented, no request sent. No commit-write web route.
- Evidence: `docs/devlog/claude_2026-10-29-capture-scm.json`.
