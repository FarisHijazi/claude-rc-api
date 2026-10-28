"""Environments: cloud environment providers, private org environments,
self-hosted runner pools, last-used environment.

An *environment* is where a session's worker runs: ``anthropic_cloud`` (a
claude.ai sandbox), ``bridge`` (a Remote Control machine — your own computer
running ``claude remote-control``), ``byoc`` / ``snap`` / ``ccs``. Start a
session on one by passing its ``environment_id`` to session create; a
self-hosted pool id (``ccpool_…``) goes in ``self_hosted_runner_pool_id``
instead (see :func:`environment_target`).

Environment objects can carry the environment's variables (``config.environment``).
Nothing here logs or prints them; :func:`redact_environment` gives a copy safe
to show.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import quote

#: Known ``kind`` values (CLI 2.1.312 enum).
ENVIRONMENT_KINDS = ("anthropic_cloud", "bridge", "byoc", "snap", "ccs")

#: Languages the CLI's default cloud environment installs.
DEFAULT_LANGUAGES = (
    {"name": "python", "version": "3.11"},
    {"name": "node", "version": "20"},
)

_PROVIDERS = "/v1/environment_providers"
_POOLS = "/v1/code/runners/self-hosted/pools"
_MAX_PAGES = 50


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def environment_items(body: Any) -> list[dict]:
    """The environment list out of a list response: ``{environments: […]}``
    (``/v1/environment_providers``), a bare list, or ``{data: […]}``. The private
    browser route uses ``{environments, has_more, first_id, last_id}``."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("environments", "data", "items"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def environments_of_kind(environments: Iterable[dict], *kinds: str) -> list[dict]:
    """Only the environments whose ``kind`` is one of ``kinds``.
    ``environments_of_kind(envs, "bridge")`` lists Remote Control machines."""
    return [e for e in environments if e.get("kind") in kinds]


def bridge_machines(environments: Iterable[dict]) -> list[dict]:
    """Remote Control machines: ``kind: "bridge"`` entries."""
    return environments_of_kind(environments, "bridge")


def default_environment(
    environments: Sequence[dict], preferred_id: str | None = None
) -> dict | None:
    """Pick an environment the way the CLI's teleport does: ``preferred_id``
    if present, else the first ``anthropic_cloud``, else the first non-bridge,
    else the first. ``None`` for an empty list (the CLI then auto-creates one,
    :meth:`EnvironmentsMixin.create_cloud_environment`)."""
    if preferred_id:
        for e in environments:
            if e.get("environment_id") == preferred_id:
                return e
    for pick in (
        lambda e: e.get("kind") == "anthropic_cloud",
        lambda e: e.get("kind") != "bridge",
        lambda e: True,
    ):
        for e in environments:
            if pick(e):
                return e
    return None


def environment_target(environment_id: str) -> dict:
    """The session-create field for an environment id: a ``ccpool_…`` id is a
    self-hosted pool (``self_hosted_runner_pool_id``), anything else is an
    ``environment_id`` (the CLI's rule)."""
    if environment_id.startswith("ccpool_"):
        return {"self_hosted_runner_pool_id": environment_id}
    return {"environment_id": environment_id}


def redact_environment(environment: dict) -> dict:
    """A deep copy with every environment-variable value replaced by
    ``"<redacted>"`` (keys kept), safe to print or log."""
    out = copy.deepcopy(environment)
    config = out.get("config")
    if isinstance(config, dict) and "environment" in config:
        env = config["environment"]
        config["environment"] = (
            {k: "<redacted>" for k in env} if isinstance(env, dict) else "<redacted>"
        )
    return redact_network_material(out)


def redact_network_material(value: Any) -> Any:
    """Copy network responses, hiding secret payloads while retaining metadata."""
    if isinstance(value, list):
        return [redact_network_material(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if key.lower()
            in {
                "credential",
                "secret",
                "value",
                "token",
                "password",
                "headers",
                "network_secrets",
                "access_token",
                "refresh_token",
                "api_key",
                "inline",
                "secret_value",
                "password_value",
                "secret_access_key_value",
                "session_token_value",
                "spec",
                "authorization",
            }
            else redact_network_material(item)
            for key, item in value.items()
        }
    return copy.deepcopy(value)


def merge_environment_config(
    current: Mapping[str, Any], patch: Mapping[str, Any]
) -> dict:
    """Merge object keys recursively; arrays/scalars replace, null explicitly clears.

    An empty object clears that object (use environment={} to remove all variables).
    """
    out = copy.deepcopy(dict(current))
    for key, value in patch.items():
        if isinstance(value, Mapping) and value and isinstance(out.get(key), Mapping):
            out[key] = merge_environment_config(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def cloud_environment_body(
    name: str = "Default",
    *,
    description: str | None = None,
    cwd: str = "/home/user",
    init_script: str | None = None,
    environment: dict[str, str] | None = None,
    languages: Iterable[dict] | None = None,
    allowed_hosts: Iterable[str] = (),
    allow_default_hosts: bool = True,
    allow_mcp_servers: bool | None = None,
    network_config: Mapping[str, Any] | None = None,
) -> dict:
    """The ``POST /v1/environment_providers/cloud/create`` body. All defaults
    reproduce the CLI's auto-created environment exactly (spec surface §2.2).

    ``allow_mcp_servers`` appears in the web app's environment object (HAR #28)
    but not in the CLI's create body; it is only sent when given (unverified on
    create). ``allowed_hosts=["*"]`` is what claude.ai shows as a fully open
    network."""
    network: dict[str, Any] = {
        "allowed_hosts": list(allowed_hosts),
        "allow_default_hosts": allow_default_hosts,
    }
    if allow_mcp_servers is not None:
        network["allow_mcp_servers"] = allow_mcp_servers
    if network_config is not None:
        network = merge_environment_config(network, network_config)
    return {
        "name": name,
        "kind": "anthropic_cloud",
        "description": f"{name} - trusted network access"
        if description is None
        else description,
        "config": {
            "environment_type": "anthropic",
            "cwd": cwd,
            "init_script": init_script,
            "environment": dict(environment or {}),
            "languages": [
                dict(lang)
                for lang in (DEFAULT_LANGUAGES if languages is None else languages)
            ],
            "network_config": network,
        },
    }


# ---------------------------------------------------------------------------
# mixin
# ---------------------------------------------------------------------------
class EnvironmentsMixin:
    """Environment and runner-pool methods for :class:`~claude_rc.client.RemoteControlClient`.

    The host (:class:`~claude_rc.client.RemoteControlClient`) provides, and these
    methods must use instead of any HTTP of their own:

    * ``self._request(method, path, *, json=, params=, betas=, client_platform=,
      org=, web=, headers=, accept=, retry=, timeout=, content=, data=, files=)``
      -> parsed body; ``self._send(...)`` -> checked :class:`httpx.Response`
      (``stream=True`` leaves it open). Both raise :class:`~claude_rc.client.APIError`.
    * ``self._path(*parts)`` -> ``/v1/code/sessions/...``; ``self.org_uuid``.
    * ``self.send_events``, ``self._send_control``, ``self.request_control``,
      ``self.wait_control_response``, ``self.list_events``, ``self.stream_events``.

    A method defined on ``RemoteControlClient`` itself shadows a mixin method of
    the same name.
    """

    # -- environment providers (CLI route) ----------------------------------
    def iter_environments(self, *, page_size: int = 100) -> Iterator[dict]:
        """Yield every environment from ``GET /v1/environment_providers``,
        following ``after_id`` = ``last_id`` while ``has_more`` is true (the
        CLI's paging; it stops on an empty page or a missing ``last_id``)."""
        path = (
            self._private_environments_path()
            if self._cookie_environments
            else _PROVIDERS
        )
        yield from self._iter_environment_pages(path, page_size)

    @property
    def _cookie_environments(self) -> bool:
        return bool(getattr(getattr(self, "_auth", None), "is_cookie", False))

    def _iter_environment_pages(self, path: str, limit: int) -> Iterator[dict]:
        if limit < 1:
            raise ValueError("page size must be positive")
        params: dict[str, Any] = {"limit": limit}
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            body = self._request("GET", path, params=params)
            items = environment_items(body)
            yield from items
            if not isinstance(body, dict) or body.get("has_more") is not True:
                return
            last_id = body.get("last_id")
            if not items or not isinstance(last_id, str) or not last_id:
                return
            if last_id in seen:
                raise RuntimeError("environment pagination made no progress")
            seen.add(last_id)
            params = {"limit": limit, "after_id": last_id}
        raise RuntimeError("environment pagination exceeded page budget")

    def list_environments(
        self, *, kind: str | Sequence[str] | None = None, page_size: int = 100
    ) -> list[dict]:
        """All environments (every page), each ``{environment_id, name, kind, …}``.
        ``kind`` filters, e.g. ``kind="bridge"`` for Remote Control machines or
        ``kind=("anthropic_cloud", "byoc")``. Under cookie auth this is the web
        app's private org route: the public one answers 403 "Authentication
        method not allowed" to a cookie (live, 2026-10-28)."""
        envs = list(self.iter_environments(page_size=page_size))
        if kind is None:
            return envs
        return environments_of_kind(envs, *((kind,) if isinstance(kind, str) else kind))

    def create_cloud_environment(self, name: str = "Default", **options: Any) -> dict:
        """Create a cloud environment. Cookies use the browser's private org
        ``/cloud/create``; bearer auth retains the CLI's public route.
        Options are :func:`cloud_environment_body`'s. Not idempotent: never
        retried on 5xx. The live service adds default Node to requested Python.
        """
        path = (
            self._private_environments_path().removesuffix("/environments")
            + "/cloud/create"
            if self._cookie_environments
            else f"{_PROVIDERS}/cloud/create"
        )
        return self._request("POST", path, json=cloud_environment_body(name, **options))

    def update_environment(
        self,
        environment_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> Any:
        """Read/merge/write the complete name, description and configuration.

        Cookies use the private org ``/environments/{id}`` POST (live verified);
        bearer auth retains ``/v1/environment_providers/{id}``. Nested config
        objects merge recursively, lists replace, and an explicit empty object
        clears its field. There is no evidenced compare-and-swap on this route:
        callers must serialize edits to the same environment.
        """
        current = self.get_environment(environment_id)
        body = {
            "name": current.get("name", "") if name is None else name,
            "description": current.get("description") or ""
            if description is None
            else description,
            "config": merge_environment_config(
                current.get("config") or {}, config or {}
            ),
        }
        path = (
            self._private_environments_path(environment_id)
            if self._cookie_environments
            else f"{_PROVIDERS}/{quote(environment_id, safe='')}"
        )
        return self._request("POST", path, json=body)

    def archive_environment(self, environment_id: str) -> Any:
        """Archive via the browser's private route; no permanent delete is evidenced."""
        return self._request(
            "POST", self._private_environments_path(environment_id, "archive"), json={}
        )

    # -- private org route (web app) ----------------------------------------
    def _private_environments_path(self, *parts: str) -> str:
        org = self.org_uuid
        if not org:
            raise ValueError("organization uuid unknown; pass org_uuid= to the client")
        return "/".join(
            (
                f"{_PROVIDERS}/private/organizations/{quote(org, safe='')}/environments",
                *(quote(p, safe="") for p in parts),
            )
        ).rstrip("/")

    def list_org_environments(self, *, limit: int = 1000) -> list[dict]:
        """``GET /v1/environment_providers/private/organizations/{org}/environments?limit=1000``
        — the web app's environment list (same data as :meth:`list_environments`).
        The browser requests limit=1000; follows the same observed after_id /
        last_id envelope as the public route when has_more is true."""
        return list(
            self._iter_environment_pages(self._private_environments_path(), limit)
        )

    def get_environment(self, environment_id: str) -> dict:
        """``GET /v1/environment_providers/private/organizations/{org}/environments/{id}``
        (web app, HAR #28) — the full object: ``{kind, environment_id, name,
        description, created_at, state, owner_account_uuid, config:{environment_type,
        sub_type, cwd, init_script, environment, languages, network_config:{allowed_hosts,
        allow_default_hosts, allow_mcp_servers}}, bridge_info, is_ccr_default}``.
        ``config.environment`` holds variable values: pass the result through
        :func:`redact_environment` before showing it."""
        return self._request("GET", self._private_environments_path(environment_id))

    def last_used_environment(self) -> dict | None:
        """``GET /v1/code/last_used_environment`` — the environment the web app
        preselects, or ``None`` when the server has none (the captured call
        returned a plain-text 404, HAR #2). The 200 shape is unverified."""
        resp = self._send("GET", "/v1/code/last_used_environment", accept=(200, 404))
        if resp.status_code == 404:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        return body or None

    # -- self-hosted runner pools -------------------------------------------
    def list_self_hosted_pools(self) -> list[dict]:
        """``GET /v1/code/runners/self-hosted/pools`` — ``[{pool_id: "ccpool_…",
        name, created_at, alive_runner_count}]``. Start a session on a pool with
        :func:`environment_target` (``self_hosted_runner_pool_id``)."""
        body = self._request("GET", _POOLS)
        if isinstance(body, dict) and isinstance(body.get("pools"), list):
            return body["pools"]
        return body if isinstance(body, list) else []

    def get_self_hosted_pool(self, pool_id: str) -> dict:
        """``GET /v1/code/runners/self-hosted/pools/{pool_id}`` — one pool
        (``{pool}`` unwrapped). The CLI's admin tool sends this with neither the
        org header nor a beta, so neither is sent here."""
        body = self._request("GET", f"{_POOLS}/{pool_id}", web=False, org=False)
        return (
            body["pool"]
            if isinstance(body, dict) and isinstance(body.get("pool"), dict)
            else body
        )

    # Contracts captured in browser bundles ca9a464a4, c9928c88a, c58f0efa3.
    def list_agent_proxy_profiles(
        self,
        *,
        include_bindings: bool = False,
        include_github_permissions: bool = False,
    ) -> dict:
        params = {}
        if include_bindings:
            params["include_bindings"] = "true"
        if include_github_permissions:
            params["include_github_permissions"] = "true"
        return self._request("GET", "/v1/code/agent-proxy-profiles", params=params)

    def _profile_path(self, profile_id: str) -> str:
        return f"/v1/code/agent-proxy-profiles/{quote(profile_id, safe='')}"

    def get_agent_proxy_profile(self, profile_id: str) -> dict:
        return self._request("GET", self._profile_path(profile_id))

    def create_agent_proxy_profile(
        self, slug: str, *, display_name: str, setup_entrypoint: str | None = None
    ) -> dict:
        body = {"slug": slug, "display_name": display_name}
        if setup_entrypoint is not None:
            body["setup_entrypoint"] = setup_entrypoint
        return self._request("POST", "/v1/code/agent-proxy-profiles", json=body)

    def update_agent_proxy_profile(
        self, profile_id: str, patch: Mapping[str, Any]
    ) -> dict:
        """Browser PATCH fields, including updated_at for optimistic concurrency.

        The browser sends agent_proxy_profile_id in the body as well as the URL.
        Profile changes are distinct from credential material; never put secrets here.
        """
        return self._request(
            "PATCH",
            self._profile_path(profile_id),
            json={**patch, "agent_proxy_profile_id": profile_id},
        )

    def delete_agent_proxy_profile(self, profile_id: str) -> Any:
        return self._request("DELETE", self._profile_path(profile_id), json={})

    def bind_environment_profile(
        self, profile_id: str, environment_id: str, *, unbind: bool = False
    ) -> Any:
        suffix = "bindings:unbind" if unbind else "bindings"
        return self._request(
            "POST",
            f"{self._profile_path(profile_id)}/{suffix}",
            json={
                "agent_proxy_profile_id": profile_id,
                "scope": {"kind": "KIND_ENVIRONMENT", "id": environment_id},
            },
        )

    def list_network_credentials(self) -> dict:
        """Credential metadata; redact_network_material before printing."""
        return self._request("GET", "/v1/code/agent-proxy/credentials")

    def create_network_credential(
        self, credential: Mapping[str, Any], *, create_paired_allow_rule: bool = False
    ) -> dict:
        """Explicit secret write: browser credential object (profile, host, spec).

        No automatic binding, credential discovery, or OAuth consent flow is started.
        """
        body = {"credential": dict(credential)}
        if create_paired_allow_rule:
            body["create_paired_allow_rule"] = True
        return self._request("POST", "/v1/code/agent-proxy/credentials", json=body)

    def update_network_credential(
        self,
        credential_id: str,
        *,
        display_name: str | None = None,
        host_constraint: Sequence[str] | None = None,
    ) -> dict:
        body = {}
        if display_name is not None:
            body["display_name"] = display_name
        if host_constraint is not None:
            body["host_constraint"] = list(host_constraint)
        return self._request(
            "PATCH",
            f"/v1/code/agent-proxy/credentials/{quote(credential_id, safe='')}",
            json=body,
        )

    def rotate_network_credential(
        self, credential_id: str, credential: Mapping[str, Any]
    ) -> dict:
        return self._request(
            "POST",
            f"/v1/code/agent-proxy/credentials/{quote(credential_id, safe='')}/rotate",
            json={"credential": dict(credential)},
        )

    def delete_network_credential(self, credential_id: str) -> Any:
        return self._request(
            "DELETE",
            f"/v1/code/agent-proxy/credentials/{quote(credential_id, safe='')}",
            json={},
        )

    def list_network_rules(self) -> dict:
        return self._request("GET", "/v1/code/agent-proxy/rules")

    def create_network_rule(self, rule: Mapping[str, Any]) -> dict:
        return self._request(
            "POST", "/v1/code/agent-proxy/rules", json={"rule": dict(rule)}
        )

    def update_network_rule(
        self, rule_id: str, rule: Mapping[str, Any], *, expected_version: int
    ) -> dict:
        return self._request(
            "PATCH",
            f"/v1/code/agent-proxy/rules/{quote(rule_id, safe='')}",
            json={"rule": dict(rule), "expected_version": expected_version},
        )

    def delete_network_rule(self, rule_id: str) -> Any:
        return self._request(
            "DELETE", f"/v1/code/agent-proxy/rules/{quote(rule_id, safe='')}", json={}
        )

    def list_environment_credentials(self, environment_id: str) -> dict:
        return self._request(
            "GET",
            "/v1/code/agent-proxy/environment-credentials",
            params={"environment_id": environment_id},
        )

    def create_environment_credential(
        self,
        environment_id: str,
        host: str,
        secret: Mapping[str, Any],
        *,
        display_name: str | None = None,
    ) -> dict:
        """Browser network-secret body; secret is the captured bearer/basic spec.

        Never log the request or response. This is separate from config.environment.
        """
        body = {**secret, "environment_id": environment_id, "host": host}
        if display_name is not None:
            body["display_name"] = display_name
        return self._request(
            "POST", "/v1/code/agent-proxy/environment-credentials", json=body
        )

    def delete_environment_credential(
        self, environment_id: str, credential_id: str
    ) -> Any:
        return self._request(
            "DELETE",
            f"/v1/code/agent-proxy/environment-credentials/{quote(credential_id, safe='')}",
            params={"environment_id": environment_id},
            json={},
        )
