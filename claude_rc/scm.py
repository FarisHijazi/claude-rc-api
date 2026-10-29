"""Read-only GitHub SCM reads the claude.ai/code diff and file viewers use.

Captured from the 2026-10-29 web bundle: each is ``POST /v1/code/github/<op>``
with the ``/v1/code`` web headers and a JSON body ``{repo: "owner/name", ...}``
plus ``ghe_configuration_id`` only when nonzero. No session id is sent. The
browser maps 404 from compare/commit to "nothing to show"; so do these methods.
PR/commit write routes are documented in ``docs/web-api.md`` and deliberately
not implemented (approval-gated, unqualified).
"""

from __future__ import annotations

import base64
from typing import Any

from .lifecycle import repo_slug

COMPARE_REFS = "/v1/code/github/compare-refs"
GET_COMMIT = "/v1/code/github/get-commit"
GET_FILE_CONTENT = "/v1/code/github/get-file-content"


def scm_body(repo: str, ghe_configuration_id: int | None = None, **fields: Any) -> dict:
    slug = repo_slug(repo)
    if slug.count("/") != 1 or not all(slug.split("/")):
        raise ValueError("repo must identify owner/name")
    body = {k: v for k, v in fields.items() if v is not None}
    body["repo"] = slug
    if ghe_configuration_id:
        body["ghe_configuration_id"] = ghe_configuration_id
    return body


def file_content_bytes(content: dict) -> bytes:
    """Decode a :meth:`ScmMixin.get_file_content` response to raw bytes."""
    if content.get("encoding") != "base64":
        raise ValueError(f"unsupported file encoding: {content.get('encoding')!r}")
    return base64.b64decode(content.get("content") or "")


class ScmMixin:
    def _scm_post(self, path: str, body: dict, *, none_on_404: bool) -> Any:
        from .client import APIError

        try:
            return self._request("POST", path, json=body)
        except APIError as e:
            if none_on_404 and e.status == 404:
                return None
            raise

    def compare_refs(
        self, repo: str, base: str, head: str, *, ghe_configuration_id: int | None = None
    ) -> dict | None:
        """Compare two refs (branch, tag or SHA): ``{ahead_by, behind_by,
        total_commits, base_branch, head_branch, merge_base, diff_url,
        commits[{sha, short_sha, subject, author_name, author_email, date}],
        files[{filename, status, additions, deletions, changes, patch}]}``.
        ``date`` is epoch seconds as a string. ``None`` when a ref or the repo
        is not found (404)."""
        return self._scm_post(
            COMPARE_REFS, scm_body(repo, ghe_configuration_id, base=base, head=head), none_on_404=True)

    def get_commit(self, repo: str, sha: str, *, ghe_configuration_id: int | None = None) -> dict | None:
        """One commit in the compare shape (``base_branch`` = parent SHA,
        ``head_branch`` = ``sha``). ``None`` on 404; an unknown SHA currently
        returns HTTP 500, which raises."""
        return self._scm_post(GET_COMMIT, scm_body(repo, ghe_configuration_id, sha=sha), none_on_404=True)

    def get_file_content(
        self, repo: str, path: str, *, ref: str | None = None, ghe_configuration_id: int | None = None
    ) -> dict:
        """One file blob: ``{name, path, sha, size, encoding: "base64", content}``
        (decode with :func:`file_content_bytes`). ``ref`` defaults to the repo's
        default branch. Raises on 404 (missing file/repo) and 400 (directory)."""
        return self._scm_post(
            GET_FILE_CONTENT, scm_body(repo, ghe_configuration_id, path=path, ref=ref), none_on_404=False)
