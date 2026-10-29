"""Offline safety/receipt tests for the opt-in proof scripts, not live parity."""
import base64
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from claude_rc import APIError, RemoteControlClient
from claude_rc.events import Event
from claude_rc.prompts import PendingRequest

ROOT = Path(__file__).resolve().parents[1]
SMOKE = runpy.run_path(str(ROOT / "scripts" / "claude-rc-permission-smoke"))


def test_native_captured_input_preserved_by_existing_question_owner():
    evidence = json.loads((ROOT / "docs/devlog/claude_2026-10-29-core-questions-qualified.json").read_text())
    schema = next(row["schema"] for row in evidence["checks"] if row["name"] == "actual_question")
    request = PendingRequest.from_payload({"type": "control_request", "request_id": "owned-question",
        "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "tool_use_id": "owned-tool", "input": schema}})
    sent = []
    def handler(req):
        sent.extend(row["payload"] for row in json.loads(req.content)["events"])
        return httpx.Response(200, json={})
    with RemoteControlClient(token="test-token", org_uuid="test-org", transport=httpx.MockTransport(handler)) as client:
        client.answer_questions("owned-session", request, other={0: "Ada", 1: "3"})
    decision = sent[0]["response"]["response"]
    assert decision["updatedInput"] == {**schema, "answers": {"Name?": "Ada", "Count?": "3"}}
    assert decision["toolUseID"] == "owned-tool"
    assert request.input == schema


def test_native_follow_up_uses_existing_allow_once_without_schema_rewrite():
    schema = {"questions": [{"question": "Keep?", "header": "Confirm", "kind": "choice", "options": [
        {"label": "Keep", "description": "Keep it"}, {"label": "Change", "description": "Change it"}]}]}
    request = PendingRequest.from_payload({"type": "control_request", "request_id": "owned-question",
        "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": schema}})
    sent = []
    def handler(req):
        sent.extend(row["payload"] for row in json.loads(req.content)["events"])
        return httpx.Response(200, json={})
    updated = {**schema, "answers": {"Keep?": "Keep"}, "followUp": True, "response": "Ask for confirmation"}
    with RemoteControlClient(token="test-token", org_uuid="test-org", transport=httpx.MockTransport(handler)) as client:
        client.allow_once("owned-session", request, updated_input=updated)
    assert sent[0]["response"]["response"]["updatedInput"] == updated
    assert request.input == schema


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_missing_file_requires_actual_404(status):
    def read(*args):
        raise APIError(status, {"type": "error", "error": {"type": "test", "message": "test"}})
    client = SimpleNamespace(get_session_file=read)
    if status == 404:
        assert SMOKE["file_bytes"](client, "owned", "owned.txt") is None
    else:
        with pytest.raises(APIError):
            SMOKE["file_bytes"](client, "owned", "owned.txt")


def test_file_effect_reads_actual_decoded_bytes():
    client = SimpleNamespace(get_session_file=lambda *args: {"content": base64.b64encode(b"XX").decode()})
    assert SMOKE["file_bytes"](client, "owned", "owned.txt") == b"XX"


@pytest.mark.parametrize("stamp,error", [(None, False), ("2026-10-29T00:00:02Z", False),
                                         ("2026-10-29T00:00:00Z", True)])
def test_collector_rejects_unprocessed_late_or_error_results(stamp, error):
    sent = []
    result = Event.from_wire({"sequence_num": 2, "source": "worker", "created_at": "2026-10-29T00:00:01Z",
                             "payload": {"type": "result", "subtype": "success", "is_error": error}})
    def listing(sid, **kwargs):
        return [] if kwargs.get("limit") == 1 else [result]
    def send(sid, prompt, **kwargs):
        sent.append(kwargs["message_uuid"])
    def history(sid, **kwargs):
        return [Event.from_wire({"processing_at": stamp,
                "payload": {"type": "user", "uuid": sent[0]}})]
    client = SimpleNamespace(list_events=listing, send_prompt=send, iter_history=history)
    with pytest.raises((AssertionError, RuntimeError)):
        SMOKE["collect"](client, "owned", "test", lambda request: None)


def test_collector_requires_own_message_not_unrelated_receipt():
    result = Event.from_wire({"sequence_num": 2, "source": "worker", "created_at": "2026-10-29T00:00:01Z",
                             "payload": {"type": "result", "subtype": "success"}})
    client = SimpleNamespace(list_events=lambda sid, **kw: [] if kw.get("limit") == 1 else [result],
        send_prompt=lambda *args, **kw: None,
        iter_history=lambda *args, **kw: [Event.from_wire({"processing_at": "2026-10-29T00:00:00Z",
            "payload": {"type": "user", "uuid": "unrelated"}})])
    with pytest.raises(AssertionError, match="own-message"):
        SMOKE["collect"](client, "owned", "test", lambda request: None)


def test_tool_result_correlation_ignores_other_tools_and_ids():
    events = [Event.from_wire({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "bash", "name": "Bash"},
        {"type": "tool_use", "id": "read", "name": "Read"}]}}),
        Event.from_wire({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "bash", "content": "X"},
            {"type": "tool_result", "tool_use_id": "read", "content": "Y"},
            {"type": "tool_result", "tool_use_id": "unknown", "content": "Z"}]}})]
    assert [b["content"] for b in SMOKE["tool_results"](events, "Bash")] == ["X"]


def test_saved_cloud_proof_has_scoped_reuse_and_verified_cleanup():
    evidence = json.loads((ROOT / "docs/devlog/claude_2026-10-29-core-permissions-qualified.json").read_text())
    rows = {row["name"]: row for row in evidence["checks"]}
    assert evidence["profile"] == "cloud" and evidence["complete"]
    assert rows["allow_once"]["after"] == "X"
    assert rows["repeat_once"]["prompts"] == 1 and rows["repeat_once"]["after"] == "X"
    assert rows["deny"]["after"] is None and rows["out_of_scope"]["after"] is None
    assert rows["reuse"]["prompts"] == 0 and rows["reuse"]["after"] == "XX"
    updates = rows["exact_rule"]["permissions"]
    assert len(updates) == 1 and updates[0]["destination"] == "session"
    assert len(updates[0]["rules"]) == 1 and "*" not in updates[0]["rules"][0]["ruleContent"]
    assert evidence["cleanup"] == [{"session_id": evidence["session_id"], "get_404": True}]


def test_native_success_does_not_claim_hook_success():
    evidence = json.loads((ROOT / "docs/devlog/claude_2026-10-29-core-questions-qualified.json").read_text())
    assert evidence["native_qualified"] and evidence["follow_up_qualified"]
    assert not evidence["hook_qualified"]
    hook = next(row for row in evidence["checks"] if row["name"] == "hook_negotiation")
    assert hook["hooks_applied"] is False
    assert all(row.get("get_404") or row.get("excluded_from_list") for row in evidence["cleanup"])
