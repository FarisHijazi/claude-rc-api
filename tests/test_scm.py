"""ScmMixin: captured read-only GitHub compare/commit/file wire shapes, offline."""
import json

import httpx
import pytest

from claude_rc import client as client_mod
from claude_rc.client import CCR_BETA, APIError, RemoteControlClient
from claude_rc.scm import file_content_bytes, scm_body


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def _client(status=200, payload=None, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(req)
        return httpx.Response(status, json=payload if payload is not None else {})
    return RemoteControlClient(session_key="test-cookie", org_uuid="org-1", transport=httpx.MockTransport(handler))


def test_scm_body_shapes_repo_and_ghe():
    assert scm_body("https://github.com/o/r.git", base="a", head="b") == {"base": "a", "head": "b", "repo": "o/r"}
    assert scm_body("o/r", 0, path="x", ref=None) == {"path": "x", "repo": "o/r"}
    assert scm_body("o/r", 7, sha="s") == {"sha": "s", "repo": "o/r", "ghe_configuration_id": 7}
    with pytest.raises(ValueError):
        scm_body("just-a-name", sha="s")


@pytest.mark.parametrize("call,path,body", [
    (lambda c: c.compare_refs("o/r", "main", "feat"), "/v1/code/github/compare-refs",
     {"repo": "o/r", "base": "main", "head": "feat"}),
    (lambda c: c.get_commit("o/r", "abc", ghe_configuration_id=3), "/v1/code/github/get-commit",
     {"repo": "o/r", "sha": "abc", "ghe_configuration_id": 3}),
    (lambda c: c.get_file_content("o/r", "README", ref="main"), "/v1/code/github/get-file-content",
     {"repo": "o/r", "path": "README", "ref": "main"}),
    (lambda c: c.get_file_content("o/r", "README"), "/v1/code/github/get-file-content",
     {"repo": "o/r", "path": "README"}),
])
def test_requests_match_captured_contract(call, path, body):
    seen = []
    out = call(_client(payload={"ok": 1}, seen=seen))
    assert out == {"ok": 1}
    (req,) = seen
    assert req.method == "POST" and req.url.path == path
    assert json.loads(req.content) == body
    assert CCR_BETA in req.headers["anthropic-beta"]
    assert req.headers["x-organization-uuid"] == "org-1"


def test_compare_and_commit_map_404_to_none():
    assert _client(404).compare_refs("o/r", "a", "b") is None
    assert _client(404).get_commit("o/r", "s") is None


def test_file_content_404_and_server_errors_raise():
    with pytest.raises(APIError) as e:
        _client(404).get_file_content("o/r", "missing")
    assert e.value.status == 404
    with pytest.raises(APIError):
        _client(400).compare_refs("o/r", "a", "b")


def test_file_content_bytes():
    assert file_content_bytes({"encoding": "base64", "content": "SGVsbG8gV29ybGQhCg==\n"}) == b"Hello World!\n"
    with pytest.raises(ValueError):
        file_content_bytes({"encoding": "utf-8", "content": "x"})
