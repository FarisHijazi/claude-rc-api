# 2026-10-29 capture: fork, project templates, spaces, session groupings

Base `f0581dc` (0.10.0), branch `feat/capture-lifecycle`. Version not bumped here:
parallel capture branches each add surface, so the integrator bumps once.

## Capture method

One owned tab on dev Chrome (CDP 9222) loaded `https://claude.ai/code`, saved
the served JS chunks to a scratch dir and closed. Contracts below are read
from those chunks (no user input sent to the page).

## Findings (web bundle 2026-10-29)

- **Fork / branch from message.** The composer `/fork [text]` and a
  fork-send shortcut call the host `fork(sessionId, {targetMessageUuid, title})`,
  which sends control `fork_conversation {target_message_uuid?, title?}` over the
  session's Remote Control channel (60 s). Reply schema
  `{forked, sessionId?, cliSessionId?, targetMessageUuid?, prefillText?, error?, reason?}`;
  `requires_remote_control_server` -> `requires_server`,
  `background_sessions_unavailable` -> `background_unavailable`, other -> `declined`;
  a missing/odd or self id -> `invalid`. `canForkSession` is true only for
  `bridge` sessions (or desktop `local`), so the web never offers fork on cloud
  sessions ("Fork isn't available for this session").
- **Project templates.** Only `GET /v1/code/project-templates`
  (`{templates:[{key, enabled?, entry}], setup_templates}`); no write route.
- **Spaces.** `GET /v1/code/spaces?include_capabilities&limit&cursor&scope`, plus
  create/get/update/rename/delete space and member/channel operations.
- **Session groupings ("projects", `sgrp_`).** GET `/v1/code/session_groupings/{id}`,
  GET/PUT `…/settings` (full body with nulls:
  `{default_environment_id, settings:{repo_urls, instructions, model_id,
  coordinator_model_id, effort_level, coordinator_effort_level, color, icon}}`),
  POST `…/coordinator {environment_id?}`. No create route; sessions carry
  `session_grouping_id` and list filters `session_grouping_id` / `has_member_groupings`.

## Live (cookie CDP auth, cloud profile)

`scripts/claude-rc-lifecycle-smoke --output docs/devlog/claude_2026-10-29-capture-lifecycle-live.json`:

- templates 200 `{templates:[], setup_templates:[]}` (bearer: 401 `oauth_scope_insufficient`).
- spaces 400 `spaces_disabled` ("spaces are not available for this organization").
- grouping GET and settings GET: plain-text router `404 page not found` (an unknown
  session id gets a JSON 400 instead, so the route is unrouted). 600 own sessions
  listed: none has `session_grouping_id`.
- disposable Haiku session: FORKBASE turn; `fork_session` rejected locally (cloud
  gate); raw `fork_conversation` with the real first user-message UUID -> verdict
  `success`, response `{forked:false, reason:"unsupported", error:"unsupported"}`;
  AFTERFORK turn completed afterwards; no child (`parent_session_id`) or duplicate
  session; archived, deleted, GET 404. An earlier identical run
  (`session_0154EsXQVMApdAV6TS8AX3kY`, same verdict) was also deleted (GET 404);
  only its duplicate check compared mismatched id prefixes, fixed before the recorded run.

## Implemented

`lifecycle.py`: `fork_result`, `fork_session` (bridge), `list_project_templates`,
`list_spaces`, `register_lifecycle_cli`/`dispatch_lifecycle_cli` (`claude-rc fork|templates|spaces`,
wired in `cli.build_parser` through `_rc`). 14 new offline tests; full suite 818 passed, 7 skipped.

## Not supported / not qualified

- Bridge fork success: needs an owned Remote Control host; not run (would use the
  shared CLI grant).
- Spaces writes and session-grouping settings/coordinator: the server rejects or
  does not route them for this org, so they are documented only.
- Template writes: no web route exists.

## Independent verification (2026-10-29)

- Re-read the saved bundle: `fork_conversation` body, `zO` reply schema, the `VO`
  status mapping and `BO` id regex, `/v1/code/project-templates`, `/v1/code/spaces`
  query, and the four `/v1/code/session_groupings` paths all match. No guessed routes.
- Defect fixed: the web's zod schema returns `unsupported` when `sessionId`,
  `cliSessionId`, `targetMessageUuid` or `error` is not a string, or `prefillText` is
  neither a string nor null. `fork_result` returned `invalid` or passed those through.
  Four regression cases fail on `15e7416` and pass with the fix.
- Live rerun of `scripts/claude-rc-lifecycle-smoke` (session `session_0154EsXQ3gpKggWWYXAMSNWn`)
  repeated every result: cloud gate rejected locally; raw control got success
  `{forked:false, reason:"unsupported"}`; continuation turn completed; no child;
  deleted, GET 404. Fresh reads: templates 200 empty, spaces 400 `spaces_disabled`,
  grouping root/id/settings return the same plain `404 page not found` as a made-up
  `/v1/code/...` path (unrouted). Both builder sessions return GET 404.
