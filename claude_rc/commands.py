"""Controller admission and slash dispatch, from CLI 2.1.312 and web bundles.

No HTTP or event writer lives here. Local menus require a host renderer rather
than falling through to a model turn. Worker prompt commands must be advertised.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

CLOUD_FLAG_SETTINGS = frozenset({
    "model", "advisorModel", "effortLevel", "ultracode", "fastMode", "viewMode", "alwaysThinkingEnabled",
})
CLOUD_REJECTED_SUBTYPES = frozenset({
    "set_color", "fork_conversation", "rewind_files", "mcp_set_servers", "update_settings",
    "get_memory_dialog", "get_skills_dialog", "get_status", "export_conversation",
    "list_cloud_sessions", "get_cloud_session", "get_cloud_session_transcript",
    "get_chrome_dialog", "get_chrome_browsers", "select_chrome_browser", "get_sandbox_dialog",
    "set_cwd", "claim_session", "add_directory", "register_repo_root", "set_proactivity_level",
    "remote_control", "channel_enable", "ultrareview_launch", "claude_authenticate",
    "claude_oauth_callback", "claude_oauth_wait_for_completion", "mcp_authenticate",
    "mcp_clear_auth", "mcp_oauth_callback_url", "mcp_message", "mcp_read_resource", "poll_event",
    "start_shell_task",
})
NON_CONTROLLER_SUBTYPES = frozenset({
    "stage_file", "turn_handoff", "mcp_tasks_changed", "register_device_hooks",
    "upload_device_hook_template", "remote_tools_announce", "can_use_tool", "hook_callback",
    "elicitation", "request_user_dialog", "oauth_token_refresh", "host_auth_token_refresh",
    "remote_tool_call", "remote_plumbing_call", "remote_tools_probe", "remote_tools_reannounce",
    "remote_control_work_secret",
})
CLOUD_ALLOWED_SUBTYPES = frozenset({
    "initialize", "interrupt", "end_session", "cancel_async_message", "set_permission_mode",
    "set_model", "set_max_thinking_tokens", "mcp_toggle", "mcp_reconnect", "reload_skills",
    "reload_output_styles", "set_mcp_permission_mode_override", "set_chrome_browser_hints",
    "set_prompt_suggestions_paused", "reload_plugins", "apply_flag_settings", "rewind_conversation",
    "seed_read_state", "get_context_usage", "get_session_cost", "mcp_status", "list_models",
    "get_usage", "get_binary_version", "file_suggestions", "read_file", "list_directory",
    "get_workspace_diff", "get_plan", "stop_task", "background_tasks", "get_task_output",
    "send_task_message", "get_settings", "get_hooks_listing", "list_permission_rules",
    "submit_feedback", "submit_survey_feedback", "message_rated", "generate_session_title",
    "side_question", "mcp_call", "prefetch_attachments", "rename_session",
})


class UnsupportedControl(ValueError):
    """Rejected locally: an enqueue acknowledgement cannot make this supported."""


class LocalCommandRequired(ValueError):
    """The host must implement this menu/navigation, not post it as user text."""


def validate_control_request(subtype: str, fields: Mapping[str, Any], *, cloud: bool) -> None:
    if subtype in NON_CONTROLLER_SUBTYPES:
        raise UnsupportedControl(f"{subtype} is service/agent-originated, not a controller request")
    if not cloud:
        return
    if subtype in CLOUD_REJECTED_SUBTYPES or subtype not in CLOUD_ALLOWED_SUBTYPES:
        raise UnsupportedControl(f"{subtype} is not admitted by the cloud controller channel")
    if subtype == "reload_plugins" and fields.get("add_plugins"):
        raise UnsupportedControl("reload_plugins add_plugins is not admitted in cloud sessions")
    if subtype == "apply_flag_settings":
        settings = fields.get("settings")
        if not isinstance(settings, Mapping) or set(settings) - CLOUD_FLAG_SETTINGS:
            raise UnsupportedControl("apply_flag_settings contains keys unavailable in cloud sessions")


def staged_mcp_fields(*, input_files=None, output_files=None, expires_at=None,
                      result_not_for_model=None, capabilities: Sequence[str] = ()) -> dict:
    fields: dict[str, Any] = {}
    names: set[str] = set()
    paths: set[str] = set()
    for key, rows in (("input_files", input_files), ("output_files", output_files)):
        if rows is None:
            continue
        if isinstance(rows, (str, bytes)) or len(rows) > 64:
            raise ValueError(f"{key} must contain at most 64 file declarations")
        clean = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"{key} entries must be objects")
            name, path = row.get("name"), row.get("lane_path")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in names:
                raise ValueError("staged file names must be unique 1-64 character handles")
            if not isinstance(path, str) or not path:
                raise ValueError("lane_path must be a nonempty string")
            names.add(name)
            allowed = {"name", "lane_path"} | ({"if_match"} if key == "output_files" else set())
            if set(row) - allowed:
                raise ValueError(f"unexpected {key} declaration fields")
            if key == "output_files":
                if path in paths:
                    raise ValueError("output lane paths must be unique")
                paths.add(path)
                if "if_match" in row and (not isinstance(row["if_match"], str) or not row["if_match"]):
                    raise ValueError("if_match must be a nonempty opaque etag")
            clean.append(dict(row))
        fields[key] = clean
    if output_files is not None and expires_at is None:
        raise ValueError("output_files requires an RFC3339 expires_at deadline")
    if expires_at is not None:
        if not isinstance(expires_at, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", expires_at
        ):
            raise ValueError("expires_at must be an RFC3339 string")
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("expires_at must be an RFC3339 string") from exc
        if parsed.tzinfo is None or "T" not in expires_at:
            raise ValueError("expires_at must have a timezone")
        fields["expires_at"] = expires_at
    if result_not_for_model is not None:
        if not isinstance(result_not_for_model, bool):
            raise ValueError("result_not_for_model must be boolean")
        if result_not_for_model and "mcp_call_result_not_for_model_v1" not in capabilities:
            raise UnsupportedControl("result_not_for_model is not advertised on this remote connection")
        fields["result_not_for_model"] = result_not_for_model
    return fields


LOCAL_COMMANDS = frozenset({
    "theme", "login", "logout", "config", "sandbox", "skills", "workflows", "usage-credits",
    "design", "slides", "copy", "channels", "settings", "agents", "output-style", "focus",
    "heapdump", "import", "insights", "skill-doctor", "goal", "design-consent", "design-revoke",
    "doctor", "team-onboarding",
})
CONTROL_COMMANDS = {
    "model": "set_model", "plan": "set_permission_mode", "effort": "apply_flag_settings",
    "fast": "apply_flag_settings", "advisor": "apply_flag_settings", "permissions": "list_permission_rules",
    "btw": "side_question", "context": "get_context_usage", "usage": "get_usage",
    "cost": "get_session_cost", "diff": "get_workspace_diff", "tasks": "task_menu",
    "rewind": "rewind_conversation", "rename": "rename_session", "hooks": "get_hooks_listing",
    "mcp": "mcp_status", "reload-skills": "reload_skills", "reload-plugins": "reload_plugins",
}
UNSUPPORTED_COMMANDS = {
    "fork": "cloud fork contract not captured", "feedback": "requires explicit feedback submission",
    "add-dir": "add_directory is rejected in cloud sessions", "color": "set_color is rejected in cloud sessions",
    "clear": "session creation belongs to lifecycle, not post-text", "reset": "use lifecycle to create a session",
    "new": "use lifecycle to create a session", "__remote-workflow": "service-owned workflow entry point",
    "workflow-launch-exec": "service-owned workflow entry point",
}
WEB_CLIENT_COMMANDS = {**CONTROL_COMMANDS, **dict.fromkeys(LOCAL_COMMANDS),
                       "help": None, **UNSUPPORTED_COMMANDS}


def is_client_only(name: str) -> bool:
    return name.lstrip("/") in LOCAL_COMMANDS | {"help"}


def command_table(commands: Sequence[Any] = ()) -> list[dict]:
    table = [{"name": name, "route": "control", "target": target} for name, target in CONTROL_COMMANDS.items()]
    table += [{"name": name, "route": "local", "target": "host renderer required"} for name in LOCAL_COMMANDS]
    table += [{"name": "help", "route": "local", "target": "command_table"}]
    table += [{"name": name, "route": "unsupported", "target": reason} for name, reason in UNSUPPORTED_COMMANDS.items()]
    for cmd in commands:
        name = cmd.get("name") if isinstance(cmd, Mapping) else cmd.name
        if name and name not in WEB_CLIENT_COMMANDS:
            table.append({"name": name, "route": "worker", "target": "post-text"})
    return sorted(table, key=lambda row: row["name"])


def dispatch_command(client, session_id: str, name: str, args: str = "", *,
                     timeout: float = 15.0, local_handlers: Mapping[str, Callable] | None = None,
                     commands: Sequence[Any] | None = None, **send_options: Any) -> dict:
    name = name.removeprefix("/")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
        raise ValueError("command name must be one slash-command token")
    if name in LOCAL_COMMANDS:
        handler = (local_handlers or {}).get(name)
        if handler is None:
            raise LocalCommandRequired(f"/{name} needs a local host renderer; no worker message was sent")
        return {"via": "local", "command": name, "result": handler(args)}
    if name == "help":
        return {"via": "local", "command": name, "result": command_table(commands or ())}
    if name in UNSUPPORTED_COMMANDS:
        raise UnsupportedControl(f"/{name}: {UNSUPPORTED_COMMANDS[name]}")
    if name in CONTROL_COMMANDS:
        if send_options:
            raise ValueError("message options do not apply to typed slash controls")
        arg = args.strip()
        if name == "model":
            result = client.request_control(session_id, "set_model", timeout=timeout, model=arg) if arg else client.list_models(session_id, timeout=timeout)
        elif name == "plan":
            mode = {"": "plan", "on": "plan", "off": "default", "plan": "plan", "default": "default", "acceptEdits": "acceptEdits"}.get(arg)
            if mode is None:
                raise ValueError("/plan expects on, off, plan, default or acceptEdits")
            result = client.request_control(session_id, "set_permission_mode", timeout=timeout, mode=mode)
        elif name == "effort":
            if not arg:
                result = client.get_settings(session_id, timeout=timeout)
            else:
                if arg not in {"low", "medium", "high", "xhigh", "max", "auto", "ultracode"}:
                    raise ValueError("invalid effort level")
                settings = {"ultracode": arg == "ultracode"}
                if arg != "ultracode":
                    settings["effortLevel"] = None if arg == "auto" else arg
                result = client.apply_flag_settings(session_id, settings, timeout=timeout)
        elif name == "fast":
            if arg not in {"on", "off"}:
                raise ValueError("/fast expects on or off; implicit toggles are ambiguous")
            result = client.set_fast_mode(session_id, arg == "on", timeout=timeout)
        elif name == "advisor":
            if not arg:
                result = client.get_settings(session_id, timeout=timeout)
            else:
                result = client.set_advisor_model(session_id, None if arg == "off" else arg, timeout=timeout)
        elif name == "btw":
            if not arg:
                raise ValueError("/btw requires a question")
            result = client.side_question(session_id, args, timeout=timeout)
        elif name == "mcp":
            if not arg:
                result = client.mcp_status(session_id, timeout=timeout)
            else:
                action, _, server = arg.partition(" ")
                if not server or action not in {"reconnect", "enable", "disable"}:
                    raise ValueError("/mcp expects reconnect|enable|disable SERVER")
                if server == "all":
                    raise LocalCommandRequired("select explicit MCP server names; bulk mutation is not implicit")
                result = client.mcp_reconnect(session_id, server, timeout=timeout) if action == "reconnect" else client.mcp_toggle(session_id, server, action == "enable", timeout=timeout)
        elif name in {"reload-skills", "reload-plugins"}:
            if arg:
                raise ValueError(f"/{name} does not accept unverified CLI flags")
            result = getattr(client, name.replace("-", "_"))(session_id, timeout=timeout)
        elif name == "tasks":
            action, _, task_id = arg.partition(" ")
            if action == "output" and task_id:
                result = client.get_task_output(session_id, task_id, timeout=timeout)
            elif action == "stop" and task_id:
                result = client.request_control(session_id, "stop_task", timeout=timeout, task_id=task_id)
            else:
                raise LocalCommandRequired("/tasks needs the host task list; use /tasks output|stop TASK_ID")
        elif name == "rewind":
            if not arg:
                raise LocalCommandRequired("/rewind needs a host target picker or an explicit message UUID")
            result = client.rewind_conversation(session_id, arg, timeout=timeout)
        elif name == "rename":
            if not arg:
                raise ValueError("/rename requires a title")
            result = client.set_worker_title(session_id, arg, timeout=timeout)
        else:
            if arg:
                raise ValueError(f"/{name} does not accept arguments")
            method = {"context": "get_context_usage", "usage": "get_usage", "cost": "get_session_cost",
                      "diff": "get_workspace_diff", "permissions": "list_permission_rules", "hooks": "get_hooks_listing"}[name]
            result = getattr(client, method)(session_id, timeout=timeout)
        return {"via": "control", "command": name, "result": result}
    available = client.list_commands(session_id, timeout=timeout) if commands is None else commands
    for cmd in available:
        data = cmd if isinstance(cmd, Mapping) else {"name": cmd.name, "aliases": cmd.aliases}
        if name == data.get("name") or name in (data.get("aliases") or []):
            if data["name"] in WEB_CLIENT_COMMANDS:
                return dispatch_command(client, session_id, data["name"], args, timeout=timeout,
                                        local_handlers=local_handlers, commands=available, **send_options)
            text = f"/{data['name']} {args}".rstrip()
            return {"via": "worker", "command": data["name"], "ack": client.send_prompt(session_id, text, **send_options)}
    raise UnsupportedControl(f"/{name} was not advertised by this worker")


class CommandsMixin:
    def browser_models(self) -> dict:
        return self._request("GET", f"/api/organizations/{self.org_uuid}/model_selector/cc", web=True)

    def permission_mode_policy(self) -> dict:
        return self._request("GET", f"/api/claude_code/organizations/{self.org_uuid}/permission_mode_policy", web=True)

    def set_session_mcp_servers(self, session_id: str, servers: Mapping[str, Mapping[str, Any]]) -> dict:
        """Captured web PUT contract; replacing connector config is explicit."""
        rows = [{**dict(config), "name": name} for name, config in servers.items()]
        return self._request("PUT", self._url(session_id, "mcp-servers"), json={"mcp_servers": rows})

    def approve_session_mcp_tool(self, session_id: str, tool_name: str, scope: str, tool_use_id: str) -> dict:
        """Captured web POST. Scope is caller-supplied; no enum was inferred."""
        return self._request("POST", self._url(session_id, "mcp-approvals"),
                             json={"tool_name": tool_name, "scope": scope, "tool_use_id": tool_use_id})

    def submit_survey_feedback(self, session_id: str, description: str, appearance_id: str, *,
                               survey_response: str, survey_type: str = "session", surface: str = "ccr",
                               timeout: float = 30.0) -> dict:
        """Explicit submission can upload transcript/logs; never call as a probe."""
        if survey_response not in {"bad", "fine", "good"}:
            raise ValueError("invalid survey response")
        if survey_type not in {"session", "post_compact", "memory", "long_context"}:
            raise ValueError("invalid survey type")
        if surface not in {"cli", "ccd", "ccr", "ide", "sdk", "cowork"}:
            raise ValueError("invalid survey surface")
        return self.request_control(session_id, "submit_survey_feedback", timeout=timeout,
                                    description=description, appearance_id=appearance_id,
                                    survey_response=survey_response, survey_type=survey_type, surface=surface)

    def set_chrome_browser_hints(self, session_id: str, local_device_ids: Sequence[str], *,
                                 preferred_device_id: str | None = None, host_platform: str | None = None,
                                 timeout: float = 15.0) -> dict:
        ids = list(local_device_ids)
        if len(ids) > 32 or any(not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", i) for i in ids):
            raise ValueError("local device IDs must be 1-64 alphanumeric/hyphen characters, at most 32")
        fields: dict[str, Any] = {"localDeviceIds": ids, "preferredDeviceId": preferred_device_id}
        if preferred_device_id is not None and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", preferred_device_id):
            raise ValueError("invalid preferred device ID")
        if host_platform is not None:
            if host_platform not in {"darwin", "win32", "linux"}:
                raise ValueError("invalid host platform")
            fields["hostPlatform"] = host_platform
        return self.request_control(session_id, "set_chrome_browser_hints", timeout=timeout, **fields)


def register_command_cli(subparsers) -> None:
    parser = subparsers.add_parser("dispatch", help="dispatch a typed/local/advertised worker slash command")
    parser.add_argument("session_id")
    parser.add_argument("name")
    parser.add_argument("args", nargs="?", default="")
    parser.add_argument("--timeout", type=float, default=15.0)


def dispatch_command_cli(client, args) -> dict:
    return dispatch_command(client, args.session_id, args.name, args.args, timeout=args.timeout)
