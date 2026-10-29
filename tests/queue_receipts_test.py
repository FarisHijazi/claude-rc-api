"""Queue results must not override explicit worker message correlation with receipts."""
import json

import httpx
import pytest

from claude_rc import RemoteControlClient


@pytest.mark.parametrize("own_fields", [
    {"user_message_uuid": "own-message"},
    {"user_message_uuids": ["another-folded-message", "own-message"]},
    {"user_message_uuid": "another-folded-message", "user_message_uuids": ["own-message"]},
])
def test_explicit_result_uuid_rejects_another_turn_after_own_processing(own_fields):
    def frame(data, kind="client_event"):
        return f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()

    def result(sequence, fields):
        return {"event_id": f"result-{sequence}", "sequence_num": str(sequence),
                "source": "worker", "created_at": "2026-10-29T02:00:02Z",
                "payload": {"type": "result", "subtype": "success", **fields}}

    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"results": [{"event_id": "own-event"}]})
        if request.url.path.endswith("/stream"):
            receipt = {"event_id": "own-event", "status": "DELIVERY_STATUS_PROCESSING",
                       "timestamp": "2026-10-29T02:00:01Z"}
            return httpx.Response(200, content=(
                frame(receipt, "delivery_update") +
                frame(result(2, {"user_message_uuid": "previous-message",
                                 "user_message_uuids": ["previous-message"]})) +
                frame(result(3, own_fields))))
        return httpx.Response(200, json={"data": []})

    with RemoteControlClient(token="test", org_uuid="test", transport=httpx.MockTransport(handler)) as client:
        events = client.send_and_collect("cse_test", "own task", message_uuid="own-message", reconnect=False)
    assert events[-1].event_id == "result-3"
    assert len(posts) == 1


def test_collect_with_explicit_none_generates_a_correlatable_message_uuid():
    posted = []
    def handler(request):
        if request.method == "POST":
            posted.append(json.loads(request.content)["events"][0]["payload"])
            return httpx.Response(200, json={"results": [{"event_id": "own"}]})
        if request.url.path.endswith("/stream"):
            class Reply(httpx.SyncByteStream):
                def __iter__(self):
                    receipt = {"event_id": "own", "status": "DELIVERY_STATUS_PROCESSING"}
                    result = {"sequence_num": "2", "payload": {
                        "type": "result", "user_message_uuid": posted[0]["uuid"]}}
                    yield f"event: delivery_update\ndata: {json.dumps(receipt)}\n\n".encode()
                    yield f"event: client_event\ndata: {json.dumps(result)}\n\n".encode()
            return httpx.Response(200, stream=Reply())
        return httpx.Response(200, json={"data": []})

    with RemoteControlClient(token="test", org_uuid="test", transport=httpx.MockTransport(handler)) as client:
        events = client.send_and_collect("cse_test", "fixture", message_uuid=None, reconnect=False)
    assert posted[0]["uuid"] and events[-1].is_turn_end


def test_live_control_rejects_task_messaging_with_exact_request_id():
    def handler(request):
        if request.method == "POST":
            request_id = json.loads(request.content)["events"][0]["payload"]["request_id"]
            seen.append(request_id)
            return httpx.Response(200, json={"results": [{"event_id": "control-event"}]})
        if request.url.path.endswith("/stream"):
            class Reply(httpx.SyncByteStream):
                def __iter__(self):
                    body = {"payload": {"type": "control_response", "response": {
                        "subtype": "error", "request_id": seen[0],
                        "error": "Unsupported control request subtype: send_task_message"}}}
                    yield f"event: client_event\ndata: {json.dumps(body)}\n\n".encode()
            return httpx.Response(200, stream=Reply())
        return httpx.Response(200, json={"data": []})

    from claude_rc.client import ControlRejected
    seen = []
    with RemoteControlClient(token="test", org_uuid="test", control_profile="cloud",
                             transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ControlRejected, match="Unsupported control request subtype: send_task_message"):
            client.request_control_live("cse_test", "send_task_message", task_id="owned-task", message="fixture")
    assert len(seen) == 1
