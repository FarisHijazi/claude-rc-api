"""Panel history pagination through the real HTTP adapter and headless renderer."""
import threading

import httpx
import pytest

from claude_rc import RemoteControlClient
from claude_rc.webui import _Handler, _RCServer


def test_long_transcript_pages_before_streaming():
    playwright = pytest.importorskip("playwright.sync_api")
    requests = []
    total = 451

    def handler(req):
        path = req.url.path
        if path.endswith("/events"):
            cursor = int(req.url.params.get("cursor", "0"))
            limit = int(req.url.params["limit"])
            assert req.url.params["sort_order"] == "asc"
            end = min(total, cursor + limit)
            requests.append(("history", cursor))
            return httpx.Response(200, json={"data": [
                {"sequence_num": i, "payload": {"type": "assistant", "message": {
                    "role": "assistant", "content": [{"type": "text", "text": f"history-{i:04d}"}]}}}
                for i in range(cursor + 1, end + 1)],
                "next_cursor": str(end) if end < total else None, "has_more": end < total})
        if path.endswith("/events/stream"):
            requests.append(("stream", int(req.url.params["from_sequence_num"])))
            return httpx.Response(200, text=": heartbeat\n\n", headers={"content-type": "text/event-stream"})
        if path == "/v1/code/sessions/cse_toy":
            return httpx.Response(200, json={"id": "cse_toy", "title": "Owned fixture"})
        if path == "/v1/code/sessions":
            return httpx.Response(200, json={"data": [{"id": "cse_toy", "title": "Owned fixture"}]})
        return httpx.Response(200, json={})

    rc = RemoteControlClient(token="toy", org_uuid="org", transport=httpx.MockTransport(handler))
    server = _RCServer(("127.0.0.1", 0), _Handler, rc, False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"], headless=True)
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}")
                page.locator(".session").click()
                page.wait_for_function("document.querySelectorAll('#log .msg').length === 451")
                assert page.locator("#log .msg").first.inner_text() == "CLAUDE\nhistory-0001"
                assert page.locator("#log .msg").last.inner_text() == "CLAUDE\nhistory-0451"
                assert page.locator("#log .msg").count() == total
                page.wait_for_function("S.es !== null")
                assert requests[:3] == [("history", 0), ("history", 200), ("history", 400)]
                assert all(cursor == total for kind, cursor in requests if kind == "stream")
                page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        rc.close()


@pytest.mark.parametrize("limit,order", [(0, "asc"), (501, "asc"), (10, "invalid")])
def test_history_page_rejects_invalid_query_before_http(limit, order):
    def unexpected(req):
        pytest.fail("invalid pagination must not reach transport")
    with RemoteControlClient(token="toy", org_uuid="org", transport=httpx.MockTransport(unexpected)) as rc:
        with pytest.raises(ValueError):
            rc.history_page("cse_toy", limit=limit, sort_order=order)


def test_history_page_retains_short_page_cursor():
    def handler(req):
        assert req.url.params["cursor"] == "12"
        return httpx.Response(200, json={"data": [{"sequence_num": 13, "payload": {"type": "result"}}],
                                       "next_cursor": "opaque-next", "has_more": True})
    with RemoteControlClient(token="toy", org_uuid="org", transport=httpx.MockTransport(handler)) as rc:
        page = rc.history_page("cse_toy", cursor="12")
        assert page["next_cursor"] == "opaque-next" and page["has_more"] is True
        assert page["events"][0].sequence_num == 13
