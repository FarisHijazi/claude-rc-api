"""Prompt contracts through the existing client write path; no live credentials."""
import json
from copy import deepcopy

import httpx
import pytest

from claude_rc.client import RemoteControlClient
from claude_rc.events import Event, cli_permission_decision, cli_permission_response, cli_question_response, cli_question_answer
from claude_rc.prompts import PendingRequest, Question, build_answers, find_pending


def question_request():
    return PendingRequest.from_payload({
        "type": "control_request", "request_id": "question-1",
        "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "tool_use_id": "tool-1",
                    "input": {"questions": [
                        {"question": "Colour?", "header": "Colour", "multiSelect": False,
                         "options": [{"label": "Red"}, {"label": "Blue", "preview": "blue preview"}]},
                        {"question": "Toppings?", "header": "Toppings", "multiSelect": True,
                         "options": [{"label": "Ham"}, {"label": "Egg"}]},
                        {"question": "Name?", "kind": "text", "options": [], "placeholder": "Your name"},
                        {"question": "Count?", "kind": "number", "options": [],
                         "min": 1, "max": 5, "step": 0.5, "defaultValue": 2, "unit": "items"},
                    ]}}
    })


@pytest.fixture
def client():
    sent = []
    def handler(req):
        sent.extend(w["payload"] for w in json.loads(req.content)["events"])
        return httpx.Response(200, json={})
    with RemoteControlClient(token="test-token", org_uuid="test-org", transport=httpx.MockTransport(handler)) as rc:
        yield rc, sent


def test_legacy_builders_delegate_to_authoritative_wire():
    assert cli_permission_response("r", allow=True, tool_input={"x": 1}, tool_use_id="t") == cli_permission_decision(
        "r", allow=True, updated_input={"x": 1}, tool_use_id="t")
    data = question_request().input
    assert cli_question_response("r", {"Colour?": "Red"}, tool_input=data) == cli_question_answer("r", data, {"Colour?": "Red"})


def test_four_question_card_preserves_fields_and_annotations(client):
    rc, sent = client
    request = question_request()
    original = deepcopy(request.input)
    rc.answer_questions("session", request, {0: "Blue", 1: ["Ham", "Egg"]},
                        other={2: "Ada", 3: "2.5"}, notes={0: "note"}, previews={0: "blue preview"})
    response = sent[-1]["response"]
    assert response["request_id"] == request.request_id
    decision = response["response"]
    assert (decision["tool_name"], decision["toolUseID"]) == ("AskUserQuestion", "tool-1")
    updated = decision["updatedInput"]
    assert updated["questions"] == original["questions"]
    assert updated["answers"] == {"Colour?": "Blue", "Toppings?": ["Ham", "Egg"], "Name?": "Ada", "Count?": "2.5"}
    assert updated["annotations"] == {"Colour?": {"notes": "note", "preview": "blue preview"}}
    assert request.input == original
    assert request.questions[0].options[1].preview == "blue preview"
    assert request.questions[2].placeholder == "Your name"
    assert request.questions[3].number_constraints["unit"] == "items"


@pytest.mark.parametrize("text", ["0", "6", "1.2", "NaN", "Infinity", "abc"])
def test_numeric_constraints_reject_invalid_answers(text):
    with pytest.raises(ValueError):
        build_answers(question_request().questions, other={3: text})


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_question_card_sizes(count):
    questions = question_request().questions[:count]
    assert len(questions) == count
    assert build_answers(questions, {0: "Red"}) == {"Colour?": "Red"}


def test_single_item_multi_and_other():
    questions = question_request().questions
    assert build_answers(questions, {1: "Ham"}) == {"Toppings?": ["Ham"]}
    assert build_answers(questions, {1: ["Ham"]}, other={1: "Basil"}) == {"Toppings?": ["Ham", "Basil"]}
    assert build_answers(questions, other={1: "Basil"}) == {"Toppings?": ["Basil"]}
    with pytest.raises(ValueError):
        build_answers(questions, {1: ["Ham", "Ham"]})
    with pytest.raises(ValueError):
        build_answers(questions, {0: "Red", "Colour": "Blue"})


@pytest.mark.parametrize("reason", ["skipped", "dismissed", "replied_in_chat"])
def test_unanswered_variants_are_annotations_not_browser_sentinels(client, reason):
    rc, sent = client
    rc.answer_questions("session", question_request(), {0: "Red"}, unanswered={1: reason})
    updated = sent[-1]["response"]["response"]["updatedInput"]
    assert updated["answers"] == {"Colour?": "Red"}
    assert updated["annotations"] == {"Toppings?": {"unanswered": reason}}
    with pytest.raises(ValueError):
        rc.answer_questions("session", question_request(), {0: "Red"}, unanswered={0: reason})


@pytest.mark.parametrize("method,reason", [("skip_questions", "skipped"), ("dismiss_questions", "dismissed"),
                                         ("questions_replied_in_chat", "replied_in_chat")])
def test_whole_card_unanswered(client, method, reason):
    rc, sent = client
    getattr(rc, method)("session", question_request())
    updated = sent[-1]["response"]["response"]["updatedInput"]
    assert updated["answers"] == {}
    assert all(a == {"unanswered": reason} for a in updated["annotations"].values())
    assert len(updated["annotations"]) == 4


def event(seq, payload, source="worker"):
    return Event.from_wire({"sequence_num": seq, "source": source, "payload": payload})


def test_history_responses_cancellation_and_origin_gates():
    payload = {"type": "control_request", "request_id": "r", "request": {"subtype": "elicitation"}}
    assert len(find_pending([event(1, payload)])) == 1
    assert find_pending([event(1, payload, "client"), event(2, payload, "service"), event(3, payload, "agent")]) == []
    assert PendingRequest.from_payload(payload, event(1, payload, "service")) is None
    for response in ({"type": "control_cancel_request", "request_id": "r"},
                     {"type": "control_response", "response": {"request_id": "r", "subtype": "error"}}):
        assert find_pending([event(1, payload), event(2, response, "client")]) == []
    assert PendingRequest.from_payload({**payload, "request_id": None}) is None


def test_pending_reads_all_pages_and_authoritative_initialize(client, monkeypatch):
    rc, _ = client
    payload = {"type": "control_request", "request_id": "old", "request": {"subtype": "elicitation"}}
    pages = [[event(4, {"type": "assistant"}), event(3, {"type": "assistant"})],
             [event(2, {"type": "assistant"}), event(1, payload)], []]
    cursors = []
    def listing(sid, **kwargs):
        cursors.append(kwargs["cursor"])
        return pages.pop(0)
    monkeypatch.setattr(rc, "list_events", listing)
    question = question_request()
    monkeypatch.setattr(rc, "initialize", lambda sid: {"pending_permission_requests": [
        {"type": "control_request", "request_id": question.request_id, "request": question.request}]})
    assert [p.request_id for p in rc.pending_requests("session", limit=2, initialize=True)] == ["old", "question-1"]
    assert cursors == [None, "3", "1"]


def test_pending_does_not_invent_empty_on_initialize_failure(client, monkeypatch):
    rc, _ = client
    monkeypatch.setattr(rc, "list_events", lambda *a, **k: [])
    def fail(sid):
        raise TimeoutError("worker unavailable")
    monkeypatch.setattr(rc, "initialize", fail)
    with pytest.raises(TimeoutError):
        rc.pending_requests("session", initialize=True)


@pytest.mark.parametrize("reason,expected", [
    ("skipped", "[No preference]"),
    ("dismissed", "[User dismissed — do not proceed, wait for next instruction]"),
    ("replied_in_chat", "[User did not answer this question — they sent a message instead. Treat that message as their response; do not re-ask.]"),
])
def test_browser_unanswered_values_are_distinct(client, reason, expected):
    rc, sent = client
    rc.answer_questions("session", question_request(), {0: "Blue"}, unanswered={1: reason},
                        unanswered_encoding="browser")
    updated = sent[-1]["response"]["response"]["updatedInput"]
    assert updated["answers"] == {"Colour?": "Blue", "Toppings?": expected}
    assert updated["annotations"]["Toppings?"] == {"unanswered": reason}


@pytest.mark.parametrize("field,value", [("answers", {}), ("annotations", {})])
def test_already_answered_input_rejected_without_post(client, field, value):
    rc, sent = client
    request = question_request()
    request.request["input"][field] = value
    with pytest.raises(ValueError):
        rc.answer_questions("session", request, {0: "Blue"})
    assert sent == []


@pytest.mark.parametrize("method", ["answer_dialog", "answer_elicitation", "allow_once", "deny_permission"])
def test_wrong_prompt_kind_rejected(client, method):
    rc, sent = client
    request = PendingRequest("r", "hook_callback", {})
    with pytest.raises(ValueError):
        getattr(rc, method)("session", request)
    assert sent == []


def test_permission_decisions_exact_rules_and_original_input(client):
    rc, sent = client
    rule = {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "printf test"}],
            "behavior": "allow", "destination": "session"}
    request = PendingRequest("permit", "can_use_tool", {"tool_name": "Bash", "toolUseID": "tool",
                             "input": {"command": "printf test"}, "permission_suggestions": [rule]})
    rc.allow_once("session", request)
    once = sent[-1]["response"]["response"]
    assert once["updatedInput"] == request.input
    assert once["toolUseID"] == "tool"
    assert once["decisionClassification"] == "user_temporary"
    rc.always_allow("session", request)
    always = sent[-1]["response"]["response"]
    assert always["updatedPermissions"] == [rule]
    assert always["updatedInput"] == request.input
    assert always["decisionClassification"] == "user_permanent"
    rc.deny_permission("session", request, "denied", interrupt=True)
    assert sent[-1]["response"]["response"] == {
        "behavior": "deny", "message": "denied", "interrupt": True,
        "toolUseID": "tool", "decisionClassification": "user_reject"}


def test_snapshot_response_and_cancellation_ids_reconciled(client, monkeypatch):
    rc, _ = client
    question = question_request()
    snapshot = {"pending_permission_requests": [{"type": "control_request",
                "request_id": question.request_id, "request": question.request}]}
    response = {"type": "control_cancel_request", "request_id": question.request_id}
    monkeypatch.setattr(rc, "list_events", lambda *a, **k: [event(10, response)])
    assert rc.pending_requests("session", initialized=snapshot) == []


@pytest.mark.parametrize("source", ["agent", "service", "client"])
def test_prohibited_request_sources_not_blocking(source):
    payload = {"type": "control_request", "request_id": "r", "request": {"subtype": "can_use_tool"}}
    assert not event(1, payload, source).is_blocking_control
