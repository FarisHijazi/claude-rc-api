"""Queued messages cannot borrow an earlier turn's blocking prompt."""
import httpx
import pytest

from test_streaming import client, frame, wire, SID


@pytest.mark.parametrize("receipt", ["live", "history"])
def test_collect_ignores_prior_question_before_exact_message_processing(receipt):
    old = wire(2, kind="control_request")
    old["created_at"] = "2026-10-29T00:00:01Z"
    old["payload"].update(request_id="old-question", request={"subtype": "can_use_tool", "tool_name": "AskUserQuestion"})
    own = wire(4, kind="control_request")
    own["created_at"] = "2026-10-29T00:00:03Z"
    own["payload"].update(request_id="own-question", request={"subtype": "can_use_tool", "tool_name": "AskUserQuestion"})
    posted = []

    def handler(req):
        if req.method == "POST":
            posted.append(req)
            return httpx.Response(200, json={"results": [{"event_id": "own"}]})
        if req.url.path.endswith("/stream"):
            processing = {"event_id": "own", "status": "DELIVERY_STATUS_PROCESSING", "timestamp": "2026-10-29T00:00:02Z"}
            return httpx.Response(200, content=frame(old) +
                                  (frame(processing, "delivery_update") if receipt == "live" else b"") + frame(own))
        if req.url.params.get("sort_order") == "desc":
            return httpx.Response(200, json={"data": []})
        row = {**wire(3, event_id="own", kind="user"), "processing_at": "2026-10-29T00:00:02Z"}
        return httpx.Response(200, json={"data": [row]} if receipt == "history" else {"data": []})

    with client(handler) as rc:
        events = rc.send_and_collect(SID, "queued", priority="later", reconnect=False)
    assert events[-1].request_id == "own-question"
    assert any(event.request_id == "old-question" for event in events)
    assert len(posted) == 1
