"""Captured controller file contracts, offline only."""
import base64
import hashlib
import json

import httpx
import pytest

from claude_rc.client import APIError, RemoteControlClient
from claude_rc.files import inline_image


def test_inline_image_uses_content_not_attachment_upload():
    seen = []
    image = inline_image(b"png-fixture", "image/png")
    with client(lambda r: (seen.append(r) or httpx.Response(200, json={}))) as rc:
        rc.send_message_with_images("cse_owned", "color?", [image], message_uuid="owned-message")
    assert len(seen) == 1 and seen[0].url.path.endswith("/events")
    payload = json.loads(seen[0].content)["events"][0]["payload"]
    assert payload["uuid"] == "owned-message"
    assert payload["message"]["content"] == [image, {"type": "text", "text": "color?"}]
    assert base64.b64decode(image["source"]["data"]) == b"png-fixture"


@pytest.mark.parametrize("data,mime", [(b"", "image/png"), (b"data", "text/plain")])
def test_invalid_inline_image(data, mime):
    with pytest.raises(ValueError):
        inline_image(data, mime)


def client(handler):
    return RemoteControlClient(token="test", org_uuid="org", transport=httpx.MockTransport(handler))


def test_prefetch_shape_and_only_these():
    seen = []
    with client(lambda r: (seen.append(r) or httpx.Response(200, json={}))) as rc:
        rc.prefetch_attachments("cse_owned", [{"file_uuid": "file", "file_size": 0, "ignored": True}],
                                issued_at=123, only_these=True)
    event = json.loads(seen[0].content)["events"][0]["payload"]
    assert event["request_id"] == "attachment-prefetch-" + event["uuid"]
    assert event["request"] == {"subtype": "prefetch_attachments", "files": [{"file_uuid": "file", "file_size": 0}],
                                "issued_at": 123, "only_these": True}


@pytest.mark.parametrize("files", [[{"file_uuid": "f"}] * 17, [{}], [{"file_uuid": "f", "file_size": -1}],
                                  [{"file_uuid": "f", "file_size": True}]])
def test_prefetch_invalid_input_never_sends(files):
    def reject(r):
        pytest.fail("invalid prefetch touched HTTP")
    with client(reject) as rc, pytest.raises(ValueError):
        rc.prefetch_attachments("cse_owned", files)


def test_prefetch_accepts_sixteen_and_empty_replacement():
    requests = []
    with client(lambda r: (requests.append(r) or httpx.Response(200, json={}))) as rc:
        rc.prefetch_attachments("cse_owned", [{"file_uuid": str(i)} for i in range(16)])
        rc.prefetch_attachments("cse_owned", [], only_these=True)
    assert len(requests) == 2
    assert json.loads(requests[1].content)["events"][0]["payload"]["request"]["files"] == []


def test_workspace_get_put_and_stale_conflict():
    digest = hashlib.sha256(b"changed").hexdigest()
    seen = []
    def handler(r):
        seen.append(r)
        if r.method == "GET":
            return httpx.Response(200, json={"content": "old", "content_sha256": "a" * 64})
        body = json.loads(r.content)
        if body["if_match_sha256"] == "b" * 64:
            return httpx.Response(409, json={"error": {"type": "conflict"}})
        return httpx.Response(200, json={"content_sha256": digest})
    with client(handler) as rc:
        assert rc.get_session_file("cse_owned", "/tmp/a & b")["content"] == "old"
        assert rc.put_synced_file("cse_owned", "/tmp/a & b", b"changed", if_match_sha256="a" * 64,
                                  device_attestation={"kid": "test", "signature": "fake"},
                                  device_attestation_issued_at="123", attestation_header="test")["content_sha256"] == digest
        with pytest.raises(APIError) as err:
            rc.put_synced_file("cse_owned", "/tmp/a & b", b"changed", if_match_sha256="b" * 64)
    assert err.value.status == 409 and len(seen) == 3
    assert seen[0].url.params["path"] == "/tmp/a & b"
    assert seen[1].url.path == "/v1/code/sessions/cse_owned/synced_file"
    body = json.loads(seen[1].content)
    assert base64.b64decode(body["content"]) == b"changed"
    assert "session_id" not in body
    assert body["device_attestation"]["kid"] == "test"
    assert seen[1].headers["x-device-attestation"] == "test"


def test_mount_has_separate_files_api_contract():
    seen = []
    with client(lambda r: (seen.append(r) or httpx.Response(200, json={"file": {"size_bytes": 3}, "stage_dispatched": True}))) as rc:
        result = rc.mount_file("cse_owned", "file_fixture", "/tmp/fixture")
    assert result["stage_dispatched"]
    assert json.loads(seen[0].content) == {"file_id": "file_fixture", "mount_path": "/tmp/fixture"}
    assert seen[0].url.path == "/v1/code/sessions/cse_owned/files"


@pytest.mark.parametrize("kwargs", [{"if_match_sha256": "not-a-hash"}, {"device_attestation": {}},
                                    {"device_attestation_issued_at": "123"}])
def test_invalid_sync_options_fail_before_write(kwargs):
    with client(lambda r: pytest.fail("invalid write")) as rc, pytest.raises(ValueError):
        rc.put_synced_file("cse_owned", "/tmp/fixture", b"x", **kwargs)


def test_sync_requires_hash_in_server_reply():
    with client(lambda r: httpx.Response(200, json={})) as rc, pytest.raises(ValueError, match="content_sha256"):
        rc.put_synced_file("cse_owned", "/tmp/fixture", b"x")


@pytest.mark.parametrize("cookie", [True, False])
def test_attachment_upload_download_selects_auth_contract(cookie):
    data = b"exact\x00bytes\xff\r\n"
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=data)
        return httpx.Response(200, json={"file_uuid": "owned-file", "file_kind": "blob",
                                         "media_type": "application/zip"})

    kwargs = {"session_key": "test"} if cookie else {"token": "test"}
    with RemoteControlClient(**kwargs, org_uuid="org", transport=httpx.MockTransport(handler)) as rc:
        upload = rc.upload_attachment(data, "proof.zip", "application/zip")
        assert rc.download_attachment(upload["file_uuid"]) == data
    assert seen[0].url.path == ("/api/org/upload" if cookie else "/api/oauth/file_upload")
    assert dict(seen[0].url.params) == ({"store_as_is": "true"} if cookie else {})
    assert seen[1].url.path == ("/api/organizations/org/files/owned-file/contents" if cookie
                                else "/api/oauth/files/owned-file/content")
    assert b'name="file"; filename="proof.zip"' in seen[0].content
    assert b"Content-Type: application/zip" in seen[0].content
    assert data in seen[0].content
    assert upload["sha256"] == hashlib.sha256(data).hexdigest()
    assert upload["file_size"] == len(data)
    absent_header = "authorization" if cookie else "cookie"
    assert absent_header not in seen[0].headers
    assert "content-type" not in seen[1].headers or "multipart" not in seen[1].headers["content-type"]


def test_cookie_attachment_send_retains_captured_mime_reference():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"file_uuid": "owned-file", "file_kind": "blob",
                                         "media_type": "application/octet-stream"})

    with RemoteControlClient(session_key="test", org_uuid="org", transport=httpx.MockTransport(handler)) as rc:
        rc.send_message_with_files("cse_owned", "read", [b"\x00\xff"], message_uuid="owned-message")
    assert len(seen) == 2
    payload = json.loads(seen[1].content)["events"][0]["payload"]
    assert payload["uuid"] == "owned-message"
    assert payload["file_attachments"][0]["mime_type"] == "application/octet-stream"
    assert payload["file_attachments"][0]["is_image"] is False


@pytest.mark.parametrize("reply", [{}, {"file_uuid": ""}, {"file_uuid": None}, {"file_uuid": 42}])
def test_cookie_upload_rejects_missing_identifier(reply):
    with RemoteControlClient(session_key="test", org_uuid="org",
                             transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply))) as rc:
        with pytest.raises(ValueError, match="nonempty file_uuid"):
            rc.upload_attachment(b"safe")


def test_cookie_upload_lane_fails_before_http():
    with RemoteControlClient(session_key="test", org_uuid="org",
                             transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP"))) as rc:
        with pytest.raises(ValueError, match="bearer"):
            rc.upload_attachment(b"safe", lane="test")
