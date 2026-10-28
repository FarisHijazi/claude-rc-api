import os

import pytest


@pytest.fixture(autouse=True)
def _no_real_logins(monkeypatch, tmp_path_factory):
    """A machine's real claude.ai logins must never reach a test: no auth env,
    and an empty $HOME (no ~/.claude, no rotated-cookie state)."""
    for name in list(os.environ):
        if name.startswith(("CLAUDE_AI_", "CLAUDE_RC_")) or name in (
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_TRUSTED_DEVICE_TOKEN",
            "TS_AUTHKEY",
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
