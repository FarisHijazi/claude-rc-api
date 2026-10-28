"""Interaction: chat, every control request, permission prompts,
AskUserQuestion (single/multi-select, "Other" free text, Skip), slash
commands, live streams and the account-wide watch feed.

Wire sources: ``spec/sessions.md`` §5 (event model, control subtypes and the
cloud worker's forwarding policy, ``all/a_0258802926.js``), §7 (SSE frames),
§4.11 (watch), the HAR of 2026-10-28 (web send/initialize bodies) and the
ducktape notes on the watch's resume token. Shapes not seen on the wire are
marked *unverified* in their docstrings.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

import httpx

from .events import (
    RC,
    STREAM_META_FRAMES,
    WORKER_PROMPT_SUBTYPES,
    Event,
    cli_control_cancel_request,
    cli_elicitation_response,
    cli_hook_callback_response,
    cli_permission_decision,
    cli_question_answer,
    cli_user_dialog_response,
    cli_web_user_message,
    image_block,
    stream_meta_event,
)
from .prompts import (
    PromptsMixin, QuestionOption, Question, PendingRequest, build_answers,
    find_pending, pending_from_initialize,
)
from .sse import parse_sse
from .commands import (
    CLOUD_FLAG_SETTINGS, CLOUD_REJECTED_SUBTYPES, WEB_CLIENT_COMMANDS,
    CommandsMixin, dispatch_command, is_client_only, staged_mcp_fields,
)

RC_FLAG_SETTINGS = frozenset({"effortLevel", "ultracode"})

# Worker capabilities (system/init `capabilities`) for the interrupt variants.
INTERRUPT_CAPABILITIES = {
    "receipt": "interrupt_receipt_v1",
    "send_now": "interrupt_send_now_v1",
    "cancel_queued": "interrupt_cancel_queued_v1",
}

# The account watch (sessions.md §4.11; ducktape "Session watch").
WATCH_PATH = "/v1/code/sessions/watch"
WATCH_PLATFORM = "web_claude_ai"   # any other platform gets 404 "endpoint not enabled"
WATCH_TOKEN_MAX_AGE = 225.0        # a token ≥ 240 s old opens a watch that never delivers
WATCH_SYNC_STALL = 240.0           # sync recurs every 100-120 s; this long without one = dead
WATCH_KEEPALIVE_TOLERANCE = 135    # what the web app asks for (web-capture 2026-10-23)
WATCH_GONE_GRACE = 30.0            # a 410 this soon after minting ends the watch (CLI)
STREAM_LIVENESS = 45.0             # CLI: 45 s with no frame -> reconnect (sessions.md §7)

# --- typed views -----------------------------------------------------------
@dataclass
class SlashCommand:
    """A command or skill from initialize's ``commands`` (HAR 2026-10-28):
    ``{name, description, argumentHint, aliases?, builtin?}``."""

    name: str
    description: str = ""
    argument_hint: str = ""
    aliases: list[str] = field(default_factory=list)
    builtin: bool = False
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_wire(cls, c: Mapping[str, Any]) -> "SlashCommand":
        return cls(
            name=str(c.get("name", "")),
            description=str(c.get("description") or ""),
            argument_hint=str(c.get("argumentHint") or ""),
            aliases=list(c.get("aliases") or []),
            builtin=bool(c.get("builtin")),
            raw=dict(c),
        )


@dataclass
class WatchEvent:
    """One frame of the account watch. ``kind``: ``added`` / ``changed``
    (``session`` = the session as the list route sends it), ``removed``
    (``session`` = ``{id}``), ``sync`` (``resume_token`` = the next token), or
    ``resync`` — synthesized by :meth:`InteractionMixin.watch` after it had to
    mint a fresh token, so changes in the gap may be missing: re-list."""

    kind: str
    session: dict = field(default_factory=dict)
    resume_token: Optional[str] = None
    raw: Any = None

    @property
    def session_id(self) -> Optional[str]:
        return self.session.get("id")


# --- pure helpers ----------------------------------------------------------
class InteractionMixin(PromptsMixin, CommandsMixin):
    """Chat, control, prompt and stream methods for :class:`~claude_rc.client.RemoteControlClient`.

    The host (:class:`~claude_rc.client.RemoteControlClient`) provides, and these
    methods must use instead of any HTTP of their own:

    * ``self._request(method, path, *, json=, params=, betas=, client_platform=,
      org=, web=, headers=, accept=, retry=, timeout=, content=, data=, files=)``
      -> parsed body; ``self._send(...)`` -> checked :class:`httpx.Response`
      (``stream=True`` leaves it open). Both raise :class:`~claude_rc.client.APIError`.
    * ``self._path(*parts)`` -> ``/v1/code/sessions/...``; ``self.org_uuid``.
    * ``self.send_events``, ``self._send_control``, ``self._control``,
      ``self.request_control``, ``self.initialize``, ``self.list_events``,
      ``self.list_sessions``.

    A method defined on ``RemoteControlClient`` itself shadows a mixin method of
    the same name, so none here reuses one of its names. Control methods return
    the worker's ``response`` object and raise
    :class:`~claude_rc.client.ControlRejected` /
    :class:`~claude_rc.client.ControlTimeout`; RC/CCR notes are per
    sessions.md §5.3 ("CCR only" = a Remote Control bridge refuses it).
    """

    # -- chat ---------------------------------------------------------------
    def send_prompt(
        self,
        session_id: str,
        text: str = "",
        *,
        images: Sequence[tuple[bytes | str, str] | dict] = (),
        file_attachments: Sequence[Mapping[str, Any]] = (),
        priority: str | None = None,
        requeue_if_interrupted_within_ms: int | None = None,
        message_uuid: str | None = None,
        **extra: Any,
    ) -> dict:
        """Send a user message the way claude.ai/code does (plain-string
        ``content``, ``session_id`` in the event). Returns the events ack.

        * ``images``: ``(data, media_type)`` pairs or ready ``image`` blocks;
          they turn ``content`` into ``[text, image…]`` blocks.
        * ``file_attachments``: refs from ``upload_attachment``
          (``{file_uuid, file_name, is_image?, sha256?, file_size?}``).
        * ``priority``: ``now`` | ``next`` | ``later`` (``later`` = Ctrl+Enter
          "queue for later" while a turn runs).
        * ``requeue_if_interrupted_within_ms`` (≤ 10000): put the message back
          in the queue if the turn it starts is interrupted that soon.
        * ``message_uuid``: keep it to cancel (:meth:`cancel_queued_message`)
          or :meth:`send_queued_now`.
        * ``extra``: other user-message fields verbatim (``shouldQuery``,
          ``client_composed`` …; sessions.md §5.2).
        """
        content: str | list = text
        if images:
            blocks: list[dict] = [{"type": "text", "text": text}] if text else []
            for img in images:
                blocks.append(dict(img) if isinstance(img, Mapping) else image_block(*img))
            content = blocks
        event = cli_web_user_message(
            content,
            session_id=session_id,
            message_uuid=message_uuid,
            priority=priority,
            requeue_if_interrupted_within_ms=requeue_if_interrupted_within_ms,
            file_attachments=[dict(a) for a in file_attachments] or None,
            **extra,
        )
        return self.send_events(session_id, [event])

    def queue_message(self, session_id: str, text: str, *, message_uuid: str | None = None, **kw: Any) -> dict:
        """"Queue for later": a ``priority: later`` message that waits for the
        running turn instead of joining it."""
        return self.send_prompt(session_id, text, priority="later", message_uuid=message_uuid, **kw)

    # -- interrupt variants (sessions.md §5.3) -------------------------------
    def interrupt_turn(
        self,
        session_id: str,
        *,
        reason: str | None = None,
        cancel_queued: bool = False,
        send_now: bool = False,
        message_uuid: str | None = None,
        for_user_message_uuids: Sequence[str] | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Send an ``interrupt`` control with any of its fields.

        Without ``timeout`` it fires and returns the events ack. With one it
        waits for the receipt (workers with ``interrupt_receipt_v1``):
        ``{still_queued[], put_back?, send_now?: stopped|delivering|nothing_waiting,
        turn?: stopped|idle|spared, cancelled?}``. A Remote Control worker acks
        with an empty response.
        """
        fields: dict[str, Any] = {}
        if reason is not None:
            fields["reason"] = reason
        if cancel_queued:
            fields["cancel_queued"] = True
        if send_now:
            fields["send_now"] = True
        if message_uuid is not None:
            fields["message_uuid"] = message_uuid
        if for_user_message_uuids:
            if not 1 <= len(for_user_message_uuids) <= 8:
                raise ValueError("for_user_message_uuids takes 1-8 uuids")
            fields["for_user_message_uuids"] = list(for_user_message_uuids)
        if timeout is None:
            return self._send_control(session_id, "interrupt", **fields)[1]
        return self._control(session_id, "interrupt", timeout, **fields)[1].get("response") or {}

    def stop_turn(self, session_id: str, *, timeout: float | None = None) -> dict:
        """The stop button: ``{subtype: "interrupt"}``."""
        return self.interrupt_turn(session_id, timeout=timeout)

    def cancel_queued(self, session_id: str, *, timeout: float | None = None) -> dict:
        """Interrupt and drop queued messages (the CLI's legacy
        ``{interrupt, cancel_queued: true}``)."""
        return self.interrupt_turn(session_id, cancel_queued=True, timeout=timeout)

    def send_queued_now(self, session_id: str, message_uuid: str | None = None, *, timeout: float | None = None) -> dict:
        """"Send now": stop the turn and deliver a queued message
        (``{interrupt, send_now: true, message_uuid?}``)."""
        return self.interrupt_turn(session_id, send_now=True, message_uuid=message_uuid, timeout=timeout)

    def cancel_control(self, session_id: str, request_id: str) -> dict:
        """Withdraw one of your own in-flight control requests (no reply)."""
        return self.send_events(session_id, [cli_control_cancel_request(request_id)])

    # -- settings controls ---------------------------------------------------
    def set_max_thinking_tokens(
        self,
        session_id: str,
        max_thinking_tokens: int | None,
        *,
        thinking_display: str | None = None,
        timeout: float = 15.0,
    ) -> dict:
        """Thinking budget (``None`` resets to the session default);
        ``thinking_display`` ∈ ``summarized`` | ``omitted`` | ``highlights``."""
        fields: dict[str, Any] = {"max_thinking_tokens": max_thinking_tokens}
        if thinking_display is not None:
            fields["thinking_display"] = thinking_display
        return self.request_control(session_id, "set_max_thinking_tokens", timeout=timeout, **fields)

    def apply_flag_settings(
        self,
        session_id: str,
        settings: Mapping[str, Any],
        *,
        allowed: frozenset[str] | None = CLOUD_FLAG_SETTINGS,
        timeout: float = 15.0,
    ) -> dict:
        """Merge ``settings`` into the worker's flag layer (session-scoped).

        A cloud worker takes only :data:`CLOUD_FLAG_SETTINGS`; a Remote Control
        bridge only :data:`RC_FLAG_SETTINGS` — pass ``allowed=RC_FLAG_SETTINGS``
        for one, or ``None`` to skip the local check. Other keys raise
        ValueError before anything is sent.
        """
        if allowed is not None:
            bad = sorted(set(settings) - allowed)
            if bad:
                raise ValueError(f"apply_flag_settings keys not accepted: {bad}")
        return self.request_control(session_id, "apply_flag_settings", timeout=timeout, settings=dict(settings))

    def set_fast_mode(self, session_id: str, enabled: bool, *, timeout: float = 15.0) -> dict:
        """``/fast``: ``apply_flag_settings {fastMode}`` (CCR only)."""
        return self.apply_flag_settings(session_id, {"fastMode": enabled}, timeout=timeout)

    def set_advisor_model(self, session_id: str, model: str | None, *, timeout: float = 15.0) -> dict:
        """``/advisor``: ``apply_flag_settings {advisorModel}`` (CCR only)."""
        return self.apply_flag_settings(session_id, {"advisorModel": model}, timeout=timeout)

    def set_always_thinking(self, session_id: str, enabled: bool, *, timeout: float = 15.0) -> dict:
        """``apply_flag_settings {alwaysThinkingEnabled}`` (CCR only)."""
        return self.apply_flag_settings(session_id, {"alwaysThinkingEnabled": enabled}, timeout=timeout)

    def set_view_mode(self, session_id: str, mode: str, *, timeout: float = 15.0) -> dict:
        """``apply_flag_settings {viewMode}`` (CCR only). The accepted values are
        unverified (the web's Transcript view offers Normal / Verbose)."""
        return self.apply_flag_settings(session_id, {"viewMode": mode}, timeout=timeout)

    def set_worker_title(
        self, session_id: str, title: str, *, source: str = "remote", timeout: float = 15.0
    ) -> dict:
        """``rename_session`` control: retitle through the worker (``source`` ∈
        ``remote`` | ``host``). To retitle in every list use ``rename_session``
        (``PUT``)."""
        return self.request_control(session_id, "rename_session", timeout=timeout, title=title, source=source)

    def generate_session_title(
        self, session_id: str, description: str, *, persist: bool | None = None, timeout: float = 60.0
    ) -> Optional[str]:
        """Have the worker title a description (``{description, persist?}`` ->
        ``response.title``, the SDK's ``generateSessionTitle``). CCR only."""
        fields: dict[str, Any] = {"description": description}
        if persist is not None:
            fields["persist"] = persist
        return self.request_control(session_id, "generate_session_title", timeout=timeout, **fields).get("title")

    def end_session(self, session_id: str, reason: str | None = None, *, wait: float | None = None) -> dict:
        """Ask a cloud worker to shut down (``end_session {reason}``, handled by
        the host relay). Fires and returns the ack unless ``wait`` is given."""
        fields = {"reason": reason} if reason is not None else {}
        if wait is None:
            return self._send_control(session_id, "end_session", **fields)[1]
        return self.request_control(session_id, "end_session", timeout=wait, **fields)

    def rewind_conversation(
        self,
        session_id: str,
        target_message_uuid: str,
        *,
        last_seen_user_message_uuid: str | None = None,
        allow_assistant_target: bool | None = None,
        allow_summarized_target: bool | None = None,
        expires_at: int | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """``/rewind`` to a user message (CCR only). Returns ``{rewound,
        targetMessageUuid, prefillText, precedingAssistantUuid, resolution?}``
        (fields read from the worker's ``print.ts``; ``expires_at`` epoch ms)."""
        fields: dict[str, Any] = {"target_message_uuid": target_message_uuid}
        for key, value in (
            ("last_seen_user_message_uuid", last_seen_user_message_uuid),
            ("allow_assistant_target", allow_assistant_target),
            ("allow_summarized_target", allow_summarized_target),
            ("expires_at", expires_at),
        ):
            if value is not None:
                fields[key] = value
        return self.request_control(session_id, "rewind_conversation", timeout=timeout, **fields)

    def set_prompt_suggestions_paused(self, session_id: str, paused: bool, *, timeout: float = 15.0) -> dict:
        return self.request_control(session_id, "set_prompt_suggestions_paused", timeout=timeout, paused=paused)

    # -- MCP / plugins / skills ----------------------------------------------
    def mcp_status(self, session_id: str, *, live_reply: bool | None = None, timeout: float = 15.0) -> list[dict]:
        """Return MCP servers; liveReply requires the streaming owner's correlator."""
        fields = {"liveReply": live_reply} if live_reply is not None else {}
        request = self.request_control
        if live_reply:
            request = getattr(self, "request_control_live", None)
            if request is None:
                raise ValueError("liveReply requires request_control_live from the streaming module")
        return list(request(session_id, "mcp_status", timeout=timeout, **fields).get("mcpServers") or [])

    def mcp_toggle(self, session_id: str, server_name: str, enabled: bool, *, timeout: float = 30.0) -> dict:
        """Enable/disable an MCP server (CCR only)."""
        return self.request_control(session_id, "mcp_toggle", timeout=timeout, serverName=server_name, enabled=enabled)

    def mcp_reconnect(self, session_id: str, server_name: str, *, timeout: float = 30.0) -> dict:
        return self.request_control(session_id, "mcp_reconnect", timeout=timeout, serverName=server_name)

    def set_mcp_permission_mode_override(
        self,
        session_id: str,
        server_name: str,
        mode: str | None,
        *,
        ask_for_tools: Sequence[str] | None = None,
        timeout: float = 15.0,
    ) -> dict:
        """Pin (``default`` | ``auto``) or clear (None) a server's permission mode."""
        if mode not in (None, "default", "auto"):
            raise ValueError("MCP override mode must be default, auto or None")
        fields: dict[str, Any] = {"serverName": server_name, "mode": mode}
        if ask_for_tools is not None:
            if isinstance(ask_for_tools, (str, bytes)) or len(ask_for_tools) > 32 or any(
                not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in ask_for_tools
            ):
                raise ValueError("ask_for_tools requires at most 32 alphanumeric/underscore/hyphen tool names")
            fields["askForTools"] = [{"name": name} for name in ask_for_tools]
        return self.request_control(session_id, "set_mcp_permission_mode_override", timeout=timeout, **fields)

    def mcp_call(
        self,
        session_id: str,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_ms: int | None = None,
        input_files: Sequence[Mapping[str, Any]] | None = None,
        output_files: Sequence[Mapping[str, Any]] | None = None,
        expires_at: str | None = None,
        result_not_for_model: bool | None = None,
        capabilities: Sequence[str] = (),
        timeout: float = 60.0,
    ) -> dict:
        """Invoke an MCP tool, optionally staging synced-lane rows.

        Output rows require an RFC3339 deadline. Result exclusion is refused
        on this remote transport: CLI honors it only on worker-owned pipes.
        The returned staging status and CAS conflicts are preserved verbatim.
        """
        if result_not_for_model:
            raise ValueError("result_not_for_model is honored only on worker-owned local pipes, not this remote transport")
        fields: dict[str, Any] = {"tool": tool, "arguments": dict(arguments or {})}
        fields.update(staged_mcp_fields(
            input_files=input_files, output_files=output_files, expires_at=expires_at,
            result_not_for_model=result_not_for_model, capabilities=capabilities,
        ))
        if timeout_ms is not None:
            if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1000 <= timeout_ms <= 600000:
                raise ValueError("timeout_ms must be 1000-600000")
            fields["timeout_ms"] = timeout_ms
        return self.request_control(session_id, "mcp_call", timeout=timeout, **fields)

    def reload_skills(self, session_id: str, *, timeout: float = 30.0) -> dict:
        return self.request_control(session_id, "reload_skills", timeout=timeout)

    def reload_plugins(self, session_id: str, *, hold_on_cache_impact: bool = False, timeout: float = 60.0) -> dict:
        """Refresh plugins (``add_plugins`` is refused in cloud sessions)."""
        fields = {"hold_on_cache_impact": True} if hold_on_cache_impact else {}
        return self.request_control(session_id, "reload_plugins", timeout=timeout, **fields)

    def reload_output_styles(self, session_id: str, *, timeout: float = 30.0) -> dict:
        return self.request_control(session_id, "reload_output_styles", timeout=timeout)

    # -- read-only queries (CCR) -----------------------------------------------
    def get_session_cost(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """The ``/usage`` cost text the CLI prints in non-interactive mode."""
        return self.request_control(session_id, "get_session_cost", timeout=timeout)

    def list_models(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """The worker's selectable model catalog (shape unverified)."""
        return self.request_control(session_id, "list_models", timeout=timeout)

    def get_binary_version(self, session_id: str, *, timeout: float = 15.0) -> dict:
        return self.request_control(session_id, "get_binary_version", timeout=timeout)

    def get_settings(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """Effective merged settings plus the raw per-source ones."""
        return self.request_control(session_id, "get_settings", timeout=timeout)

    def get_hooks_listing(self, session_id: str, *, timeout: float = 15.0) -> dict:
        return self.request_control(session_id, "get_hooks_listing", timeout=timeout)

    def list_permission_rules(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """Live permission rules and workspace directories (``/permissions``)."""
        return self.request_control(session_id, "list_permission_rules", timeout=timeout)

    def get_plan(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """The current plan-mode plan (the worker resolves its own plan file)."""
        return self.request_control(session_id, "get_plan", timeout=timeout)

    def read_file(
        self,
        session_id: str,
        path: str,
        *,
        max_bytes: int | None = None,
        encoding: str | None = None,
        anchor: str | None = None,
        timeout: float = 15.0,
    ) -> dict:
        """Read a file in the session (content + ``absPath``), relative to cwd,
        gated by the worker's read rules."""
        fields: dict[str, Any] = {"path": path}
        for key, value in (("max_bytes", max_bytes), ("encoding", encoding), ("anchor", anchor)):
            if value is not None:
                fields[key] = value
        return self.request_control(session_id, "read_file", timeout=timeout, **fields)

    def list_directory(self, session_id: str, path: str = "", *, timeout: float = 15.0) -> dict:
        """Direct children (names and kinds) of one directory under the
        workspace root (CCR only)."""
        return self.request_control(session_id, "list_directory", timeout=timeout, path=path)

    def file_suggestions(self, session_id: str, query: str, *, timeout: float = 15.0) -> list[str]:
        """``@``-mention autocomplete: matching paths."""
        resp = self.request_control(session_id, "file_suggestions", timeout=timeout, query=query)
        return [s.get("path") if isinstance(s, Mapping) else s for s in resp.get("suggestions") or []]

    def seed_read_state(self, session_id: str, path: str, mtime: float, *, timeout: float = 15.0) -> dict:
        return self.request_control(session_id, "seed_read_state", timeout=timeout, path=path, mtime=mtime)

    # -- tasks -----------------------------------------------------------------
    def background_tasks(self, session_id: str, tool_use_id: str | None = None, *, timeout: float = 15.0) -> dict:
        """Background running foreground tasks (one, by ``tool_use_id``) —
        Ctrl+B. Returns ``{backgrounded?}``."""
        fields = {"tool_use_id": tool_use_id} if tool_use_id else {}
        return self.request_control(session_id, "background_tasks", timeout=timeout, **fields)

    def get_task_output(self, session_id: str, task_id: str, *, timeout: float = 15.0) -> dict:
        """Tail (≤ 8 KiB) of a background shell/Monitor task:
        ``{output, total_bytes, truncated}``."""
        return self.request_control(session_id, "get_task_output", timeout=timeout, task_id=task_id)

    def send_task_message(self, session_id: str, task_id: str, message: str, *, timeout: float = 15.0) -> dict:
        """Message a running agent task. Only ``task_id`` is confirmed in the
        CLI; the ``message`` field name is unverified."""
        return self.request_control(session_id, "send_task_message", timeout=timeout, task_id=task_id, message=message)

    def start_shell_task(
        self,
        session_id: str,
        command: str,
        *,
        args: Sequence[str] | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_ms: int | None = None,
        description: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Start a background shell task, e.g. a dev server (cloud workers with
        ``start_shell_task_v1``)."""
        fields: dict[str, Any] = {"command": command}
        for key, value in (("args", list(args) if args is not None else None), ("cwd", cwd),
                           ("env", dict(env) if env is not None else None), ("timeout_ms", timeout_ms),
                           ("description", description)):
            if value is not None:
                fields[key] = value
        return self.request_control(session_id, "start_shell_task", timeout=timeout, **fields)

    # -- feedback ----------------------------------------------------------------
    def submit_feedback(
        self, session_id: str, description: str, *, surface: str = "ccr", timeout: float = 30.0, **fields: Any
    ) -> dict:
        """``/feedback`` (``surface`` ∈ cli|ccd|ccr|ide|sdk|cowork; extra fields:
        ``title``, ``type``, ``area``, ``attach_transcript`` …)."""
        return self.request_control(
            session_id, "submit_feedback", timeout=timeout, description=description, surface=surface, **fields
        )

    def message_rated(
        self,
        session_id: str,
        message_uuid: str,
        sentiment: str,
        *,
        surface: str | None = None,
        cleared: bool = False,
        timeout: float = 15.0,
    ) -> dict:
        """Thumbs up/down on one message (``sentiment`` values unverified)."""
        fields: dict[str, Any] = {"messageUuid": message_uuid, "sentiment": sentiment, "cleared": cleared}
        if surface is not None:
            fields["surface"] = surface
        return self.request_control(session_id, "message_rated", timeout=timeout, **fields)

    # -- slash commands ----------------------------------------------------------------
    def list_commands(self, session_id: str, *, timeout: float = 15.0) -> list[SlashCommand]:
        """Commands and skills the worker offers, from ``initialize``'s
        ``commands`` (the web composer prepends :data:`WEB_CLIENT_COMMANDS`)."""
        init = self.initialize(session_id, timeout=timeout)
        return [SlashCommand.from_wire(c) for c in init.get("commands") or [] if isinstance(c, Mapping)]

    def run_command(self, session_id: str, name: str, args: str = "", *, strict: bool = False, **kw: Any) -> dict:
        """Dispatch typed controls, host-local menus or advertised worker commands.

        ``strict`` is retained for compatibility; browser-only commands never
        fall through to chat, regardless of its value. ``local_handlers`` maps
        menu names to callables taking the argument string.
        """
        return dispatch_command(self, session_id, name, args, **kw)

    # Stream implementation is shared with legacy stream_events consumers.
    from .streaming import (
        iter_history, stream_session, stream_control, mint_resume_token, watch,
        request_control_live,
    )

