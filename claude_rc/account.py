"""Read-only claude.ai account discovery, not session worker settings or usage.

Routes are captured from the web browser bundles (55/56/59/61.js). No method
changes the user's preferences, billing, connectors, skills or login.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote


class AccountMixin:
    def _account_org_path(self, suffix: str, *, code: bool = False) -> str:
        org = self.org_uuid
        if not org:
            raise ValueError("organization uuid unknown; pass org_uuid= to the client")
        prefix = "/api/claude_code/organizations" if code else "/api/organizations"
        return f"{prefix}/{quote(org, safe='')}/{suffix}"

    def get_account_usage(self) -> dict:
        """Web subscription quota; distinct from worker get_usage(session_id)."""
        return self._request("GET", self._account_org_path("usage"))

    def get_user_settings(self) -> dict | None:
        """Web sync row, or None on the browser's supported 404 (no row).

        Contains user-specific settings: do not print its content by default.
        """
        response = self._send(
            "GET", self._account_org_path("user_settings", code=True), accept=(200, 404)
        )
        return None if response.status_code == 404 else response.json()

    def get_account_profile(self) -> dict:
        return self._request("GET", "/api/account_profile", org=False)

    def get_subscription_details(self, *, cached: bool = False) -> dict:
        return self._request(
            "GET",
            self._account_org_path("subscription_details"),
            params={"cached": "true"} if cached else {},
        )

    def list_account_connectors(self) -> list[dict]:
        """Existing remote MCP connections, without starting authorization."""
        return self._request(
            "GET", self._account_org_path("mcp/remote_servers_with_connection")
        )

    def list_account_skills(self) -> dict:
        """Raw web skill envelope, not worker initialize.commands."""
        return self._request("GET", self._account_org_path("skills/list-skills"))


def register_account_commands(subparsers: Any, *, parents=()) -> None:
    """Group08 integration: add parsers; dispatch_account_command handles them."""
    parser = subparsers.add_parser("account", help="read-only web account discovery", parents=list(parents))
    parser.add_argument(
        "account_action",
        choices=(
            "usage",
            "profile",
            "subscription",
            "connectors",
            "skills",
            "settings-shape",
        ),
    )


def dispatch_account_command(client: AccountMixin, args: Any) -> Any:
    """Return JSON-serializable output; settings content is never CLI output."""
    actions = {
        "usage": client.get_account_usage,
        "profile": client.get_account_profile,
        "subscription": client.get_subscription_details,
        "connectors": client.list_account_connectors,
        "skills": client.list_account_skills,
    }
    if args.account_action == "settings-shape":
        row = client.get_user_settings()
        return {
            "exists": row is not None,
            "keys": sorted(row) if row is not None else [],
        }
    return actions[args.account_action]()
