"""Worker prompts and controller responses; transport remains on the client."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Sequence

from .events import (
    RC, WORKER_PROMPT_SUBTYPES, Event, cli_permission_decision,
    cli_question_answer, cli_user_dialog_response, cli_elicitation_response,
    cli_hook_callback_response,
)

# Browser renderer 74.js: explicit sentinels, not equivalent to empty answers.
BROWSER_UNANSWERED = {
    "skipped": "[No preference]",
    "dismissed": "[User dismissed — do not proceed, wait for next instruction]",
    "replied_in_chat": "[User did not answer this question — they sent a message instead. Treat that message as their response; do not re-ask.]",
}


@dataclass
class QuestionOption:
    """One choice of an AskUserQuestion question."""

    label: str
    description: str = ""
    preview: Optional[str] = None


@dataclass
class Question:
    """One AskUserQuestion question: ``question`` (its text, the answer key),
    a short ``header`` chip, ``options`` and ``multi_select``."""

    question: str
    header: str = ""
    options: list[QuestionOption] = field(default_factory=list)
    multi_select: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.raw.get("kind") or "choice"

    @property
    def description(self) -> str:
        return self.raw.get("description") or ""

    @property
    def placeholder(self) -> str | None:
        return self.raw.get("placeholder")

    @property
    def number_constraints(self) -> dict:
        """Browser numeric fields, without inventing defaults absent on wire."""
        return {k: self.raw[k] for k in ("min", "max", "step", "defaultValue", "unit") if k in self.raw}

    @property
    def labels(self) -> list[str]:
        return [o.label for o in self.options]

    @classmethod
    def from_wire(cls, q: Mapping[str, Any]) -> "Question":
        opts = [
            QuestionOption(str(o.get("label", "")), str(o.get("description") or ""), o.get("preview"))
            for o in (q.get("options") or [])
            if isinstance(o, Mapping)
        ]
        return cls(
            question=str(q.get("question", "")),
            header=str(q.get("header") or ""),
            options=opts,
            multi_select=bool(q.get("multiSelect")),
            raw=dict(q),
        )


@dataclass
class PendingRequest:
    """A worker ``control_request`` still waiting for a controller's answer.

    ``kind`` is ``question`` (AskUserQuestion), ``permission`` (any other
    ``can_use_tool``), ``dialog`` (``request_user_dialog``), ``elicitation`` or
    ``hook_callback``. ``request`` is the inner ``request`` object.
    """

    request_id: str
    subtype: str
    request: dict
    event: Optional[Event] = None

    @property
    def kind(self) -> str:
        if self.subtype == "can_use_tool":
            return "question" if self.tool_name == "AskUserQuestion" else "permission"
        return {"request_user_dialog": "dialog"}.get(self.subtype, self.subtype)

    @property
    def tool_name(self) -> Optional[str]:
        return self.request.get("tool_name")

    @property
    def tool_use_id(self) -> Optional[str]:
        return self.request.get("tool_use_id") or self.request.get("toolUseID")

    @property
    def input(self) -> dict:
        return self.request.get("input") or {}

    @property
    def permission_suggestions(self) -> list[dict]:
        """The rule updates "Always allow" applies (``updatedPermissions``)."""
        return list(self.request.get("permission_suggestions") or [])

    @property
    def sequence_num(self) -> Optional[int]:
        return self.event.sequence_num if self.event else None

    @property
    def questions(self) -> list[Question]:
        """The AskUserQuestion questions ([] for any other kind)."""
        if self.kind != "question":
            return []
        return [Question.from_wire(q) for q in self.input.get("questions") or [] if isinstance(q, Mapping)]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], event: Event | None = None) -> Optional["PendingRequest"]:
        """From a ``control_request`` payload (an event's, or an entry of
        initialize's ``pending_*_requests``); None for anything else."""
        if event is not None and event.source not in (None, "worker"):
            return None
        req = payload.get("request")
        if (payload.get("type") != RC.CONTROL_REQUEST or not isinstance(req, Mapping)
                or not payload.get("request_id") or req.get("subtype") not in WORKER_PROMPT_SUBTYPES):
            return None
        return cls(str(payload.get("request_id")), str(req.get("subtype")), dict(req), event)


def find_pending(events: Iterable[Event], initial: Iterable[PendingRequest] = ()) -> list[PendingRequest]:
    """Worker prompts in ``events`` (oldest first) not yet answered.

    A prompt is a ``control_request`` from the worker whose subtype is in
    :data:`~claude_rc.events.WORKER_PROMPT_SUBTYPES`; it is settled by any
    ``control_response`` or ``control_cancel_request`` with its ``request_id``
    (answered here, by another controller, or withdrawn by the worker).
    """
    open_: dict[str, PendingRequest] = {p.request_id: p for p in initial}
    for ev in events:
        if ev.type == RC.CONTROL_REQUEST and ev.source in (None, "worker") and ev.control_subtype in WORKER_PROMPT_SUBTYPES:
            pr = PendingRequest.from_payload(ev.payload, ev)
            if pr:
                open_[pr.request_id] = pr
        elif ev.type == RC.CONTROL_RESPONSE and ev.request_id:
            open_.pop(ev.request_id, None)
        elif ev.type == RC.CONTROL_CANCEL_REQUEST:
            open_.pop(str(ev.payload.get("request_id")), None)
    return list(open_.values())


def build_answers(
    questions: Sequence[Question],
    selections: Mapping[Any, str | Sequence[str]] | None = None,
    *,
    other: Mapping[Any, str] | None = None,
) -> dict[str, str | list[str]]:
    """Validate choices and build the ``answers`` map (question text -> label,
    or a list of labels for ``multiSelect``).

    Keys of ``selections`` / ``other`` are a question's text, its ``header`` or
    its index. ``selections`` values must be option labels (one for a
    single-select question; a str or list for multi-select). ``other`` is the
    "Other" free text: the whole answer of a single-select question, or one more
    entry after a multi-select's labels.
    """
    def resolve(key: Any) -> Question:
        return _question(questions, key)

    answers: dict[str, str | list[str]] = {}
    for key, choice in (selections or {}).items():
        q = resolve(key)
        picked = [choice] if isinstance(choice, str) else list(choice)
        unknown = [p for p in picked if p not in q.labels]
        if unknown:
            raise ValueError(f"{unknown!r} not among the options of {q.question!r}: {q.labels!r}")
        if len(set(picked)) != len(picked):
            raise ValueError("duplicate selected labels")
        if q.question in answers:
            raise ValueError("question specified more than once")
        if q.multi_select:
            answers[q.question] = picked
        elif len(picked) != 1:
            raise ValueError(f"{q.question!r} is single-select; pick exactly one label")
        else:
            answers[q.question] = picked[0]
    other_questions: set[str] = set()
    for key, text in (other or {}).items():
        q = resolve(key)
        if q.question in other_questions:
            raise ValueError("Other specified more than once for a question")
        other_questions.add(q.question)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Other must be nonempty text")
        if q.kind == "number":
            validate_number(q, text)
        if q.multi_select:
            prior = answers.get(q.question) or []
            answers[q.question] = [*prior, text] if isinstance(prior, list) else [prior, text]
        else:
            if q.question in answers:
                raise ValueError(f"{q.question!r} is single-select; give a label or Other text, not both")
            answers[q.question] = text
    return answers


def validate_number(question: Question, text: str) -> None:
    """Validate explicit wire constraints; answers remain strings on the wire."""
    try:
        value = Decimal(text)
        if not value.is_finite():
            raise ValueError("numeric answer must be finite")
        constraints = question.number_constraints
        low = Decimal(str(constraints.get("min", 0)))
        if "min" in constraints and value < low:
            raise ValueError("numeric answer is below min")
        if "max" in constraints and value > Decimal(str(constraints["max"])):
            raise ValueError("numeric answer is above max")
        if "step" in constraints:
            step = Decimal(str(constraints["step"]))
            if step <= 0 or (value - low) % step:
                raise ValueError("numeric answer does not match step")
    except InvalidOperation as exc:
        raise ValueError("numeric answer or constraints are invalid") from exc


def _question(questions: Sequence[Question], key: Any) -> Question:
    """A question by index, text or header (KeyError if none)."""
    if isinstance(key, int) and not isinstance(key, bool) and 0 <= key < len(questions):
        return questions[key]
    matches = [q for q in questions if key and key in (q.question, q.header)]
    if len(matches) > 1:
        raise ValueError(f"ambiguous question {key!r}; use its index")
    if matches:
        return matches[0]
    raise KeyError(f"no question {key!r}")


def _as_request(
    request: PendingRequest | str, subtype: str | None = None,
) -> tuple[str, Optional[PendingRequest]]:
    if isinstance(request, PendingRequest):
        if subtype is not None and request.subtype != subtype:
            raise ValueError(f"expected {subtype}, got {request.subtype}")
        return request.request_id, request
    if not request:
        raise ValueError("request_id must not be empty")
    return str(request), None


class PromptsMixin:
    # -- answering worker prompts ------------------------------------------------
    def allow_once(
        self, session_id: str, request: PendingRequest | str, *, updated_input: dict | None = None
    ) -> dict:
        """"Allow once": allow this call only. ``updated_input`` defaults to the
        request's own input (older workers expect ``updatedInput``)."""
        rid, pr = _as_request(request, "can_use_tool")
        answer = cli_permission_decision(
            rid,
            allow=True,
            updated_input=updated_input if updated_input is not None else (pr.input if pr else None),
            tool_use_id=pr.tool_use_id if pr else None,
            tool_name=pr.tool_name if pr else None,
            classification="user_temporary",
        )
        return self.send_events(session_id, [answer])

    def always_allow(
        self,
        session_id: str,
        request: PendingRequest | str,
        *,
        permissions: Sequence[Mapping[str, Any]] | None = None,
        updated_input: dict | None = None,
    ) -> dict:
        """"Always allow": allow and apply rule updates — ``permissions``, else
        the request's ``permission_suggestions`` (the web says these last for
        the session). Raises ValueError when there are none to apply."""
        rid, pr = _as_request(request, "can_use_tool")
        rules = [dict(p) for p in permissions] if permissions is not None else (pr.permission_suggestions if pr else [])
        if not rules:
            raise ValueError("always_allow needs permission rules: pass permissions= or a PendingRequest with suggestions")
        answer = cli_permission_decision(
            rid,
            allow=True,
            updated_input=updated_input if updated_input is not None else (pr.input if pr else None),
            updated_permissions=rules,
            tool_use_id=pr.tool_use_id if pr else None,
            tool_name=pr.tool_name if pr else None,
            classification="user_permanent",
        )
        return self.send_events(session_id, [answer])

    def deny_permission(
        self,
        session_id: str,
        request: PendingRequest | str,
        message: str = "The user denied this action.",
        *,
        interrupt: bool | None = None,
    ) -> dict:
        """Deny; ``message`` is what the model is told, ``interrupt=True`` also
        stops the turn."""
        rid, pr = _as_request(request, "can_use_tool")
        answer = cli_permission_decision(
            rid, allow=False, message=message, interrupt=interrupt,
            tool_use_id=pr.tool_use_id if pr else None, classification="user_reject",
        )
        return self.send_events(session_id, [answer])

    def answer_dialog(
        self, session_id: str, request: PendingRequest | str, *, completed: bool = True, result: Any = None
    ) -> dict:
        """Settle a ``request_user_dialog`` (``behavior: completed | cancelled``).
        Only answer kinds you declared in ``supportedDialogKinds``."""
        rid, _ = _as_request(request, "request_user_dialog")
        return self.send_events(session_id, [cli_user_dialog_response(rid, completed=completed, result=result)])

    def answer_elicitation(
        self, session_id: str, request: PendingRequest | str, action: str = "accept", content: dict | None = None
    ) -> dict:
        """Answer an MCP ``elicitation``: ``accept`` (with form ``content``),
        ``decline`` or ``cancel``."""
        rid, _ = _as_request(request, "elicitation")
        return self.send_events(session_id, [cli_elicitation_response(rid, action, content)])

    def answer_hook_callback(self, session_id: str, request: PendingRequest | str, output: Mapping[str, Any]) -> dict:
        """Answer a ``hook_callback`` with the hook's JSON output."""
        rid, _ = _as_request(request, "hook_callback")
        return self.send_events(session_id, [cli_hook_callback_response(rid, dict(output))])

    # -- AskUserQuestion -----------------------------------------------------------
    def answer_questions(
        self,
        session_id: str,
        request: PendingRequest,
        selections: Mapping[Any, str | Sequence[str]] | None = None,
        *,
        other: Mapping[Any, str] | None = None,
        skipped: Sequence[Any] = (),
        notes: Mapping[Any, str] | None = None,
        previews: Mapping[Any, str] | None = None,
        unanswered: Mapping[Any, str] | None = None,
        unanswered_encoding: str = "worker",
    ) -> dict:
        """Answer an AskUserQuestion card (see :func:`build_answers` for
        ``selections`` / ``other`` keys and checks). ``skipped`` questions get
        no answer and ``annotations[q].unanswered = "skipped"``; ``notes`` adds
        ``annotations[q].notes``. ``previews`` adds selected preview text.
        ``unanswered_encoding='worker'`` omits unanswered values; ``'browser'``
        inserts the renderer's distinct sentinel strings. They are NOT semantic
        equivalents: empty answers give only a generic unanswered tool result.
        """
        if unanswered_encoding not in ("worker", "browser"):
            raise ValueError("unanswered_encoding must be worker or browser")
        if request.kind != "question":
            raise ValueError(f"request {request.request_id} is a {request.kind}, not an AskUserQuestion")
        qs = request.questions
        if not 1 <= len(qs) <= 4 or len({q.question for q in qs}) != len(qs):
            raise ValueError("AskUserQuestion needs 1-4 questions with unique text")
        if "answers" in request.input or "annotations" in request.input:
            raise ValueError("request input already contains answers or annotations")
        answers = build_answers(qs, selections, other=other)
        annotations: dict[str, dict] = {}
        for key in skipped:
            annotations.setdefault(_question(qs, key).question, {})["unanswered"] = "skipped"
        for key, reason in (unanswered or {}).items():
            if reason not in ("skipped", "dismissed", "replied_in_chat"):
                raise ValueError("unanswered must be skipped, dismissed or replied_in_chat")
            annotations.setdefault(_question(qs, key).question, {})["unanswered"] = reason
        for field_name, values in (("notes", notes), ("preview", previews)):
            for key, text in (values or {}).items():
                if not isinstance(text, str):
                    raise ValueError(f"{field_name} must be text")
                annotations.setdefault(_question(qs, key).question, {})[field_name] = text
        if any(q in answers and "unanswered" in a for q, a in annotations.items()):
            raise ValueError("a question cannot be both answered and unanswered")
        if unanswered_encoding == "browser":
            for question, annotation in annotations.items():
                if "unanswered" in annotation:
                    answers[question] = BROWSER_UNANSWERED[annotation["unanswered"]]
        answer = cli_question_answer(
            request.request_id, request.input, answers, annotations=annotations or None, tool_use_id=request.tool_use_id
        )
        return self.send_events(session_id, [answer])

    def select_option(self, session_id: str, request: PendingRequest, label: str, *, question: Any = 0) -> dict:
        """Single-select: pick ``label`` for ``question`` (index, text or header)."""
        return self.answer_questions(session_id, request, {question: label})

    def select_options(
        self, session_id: str, request: PendingRequest, labels: Sequence[str], *, question: Any = 0, other: str | None = None
    ) -> dict:
        """Multi-select: pick ``labels`` (plus optional "Other" text)."""
        return self.answer_questions(
            session_id, request, {question: list(labels)}, other={question: other} if other else None
        )

    def answer_other(self, session_id: str, request: PendingRequest, text: str, *, question: Any = 0) -> dict:
        """"Other": answer ``question`` with free text."""
        return self.answer_questions(session_id, request, other={question: text})

    def skip_questions(self, session_id: str, request: PendingRequest, *, encoding: str = "worker") -> dict:
        """Skip the card. Worker encoding leaves answers empty; browser encoding
        inserts ``[No preference]`` for each question."""
        return self.answer_questions(session_id, request, skipped=list(range(len(request.questions))),
                                     unanswered_encoding=encoding)

    def dismiss_questions(self, session_id: str, request: PendingRequest, *, encoding: str = "worker") -> dict:
        """Dismiss with explicit worker annotations or browser sentinel text."""
        return self.answer_questions(session_id, request, unanswered={
            i: "dismissed" for i in range(len(request.questions))
        }, unanswered_encoding=encoding)

    def questions_replied_in_chat(self, session_id: str, request: PendingRequest, *, encoding: str = "worker") -> dict:
        """Mark unanswered; the caller must separately send the user's chat reply."""
        return self.answer_questions(session_id, request, unanswered={
            i: "replied_in_chat" for i in range(len(request.questions))
        }, unanswered_encoding=encoding)

    # -- pending prompts -------------------------------------------------------------
    def pending_requests(
        self, session_id: str, *, limit: int = 200, initialize: bool = False,
        initialized: Mapping[str, Any] | None = None,
    ) -> list[PendingRequest]:
        """Read complete history, optionally reconciled with initialized state.

        ``limit`` is a page size, not a history cutoff. Pass ``initialize=True``
        for a fresh authoritative worker snapshot, or ``initialized=`` for an
        already obtained snapshot. The default preserves read-only discovery.
        A requested handshake's errors propagate instead of returning empty.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        history = []
        cursor = None
        while True:
            page = self.list_events(session_id, limit=limit, sort_order="desc", cursor=cursor)
            if not page:
                break
            history.extend(page)
            seqs = [e.sequence_num for e in page if e.sequence_num is not None]
            if not seqs:
                raise ValueError("history page has no sequence numbers")
            next_cursor = str(min(seqs))
            if cursor is not None and int(next_cursor) >= int(cursor):
                raise ValueError("history cursor did not advance")
            cursor = next_cursor
            if len(page) < limit:
                break
        init = self.initialize(session_id) if initialize else initialized
        historical = find_pending(sorted(history, key=lambda e: e.sequence_num or 0))
        if init is None:
            return historical
        if not initialize:
            # A caller-supplied snapshot may predate the fetched history;
            # later responses/cancellations must settle its request IDs.
            return find_pending(sorted(history, key=lambda e: e.sequence_num or 0),
                                pending_from_initialize(init))
        # The snapshot is authoritative for permissions/dialogs. History also
        # covers hook/elicitation prompts absent from initialize's arrays.
        pending = {p.request_id: p for p in historical
            if p.kind not in ("permission", "question", "dialog")}
        pending.update((p.request_id, p) for p in pending_from_initialize(init))
        return list(pending.values())



def pending_from_initialize(init: Mapping[str, Any]) -> list[PendingRequest]:
    """The prompts a worker reports as outstanding in an ``initialize`` answer
    (``pending_permission_requests`` + ``pending_user_dialog_requests``, each a
    full ``control_request`` frame)."""
    out = []
    for key in ("pending_permission_requests", "pending_user_dialog_requests"):
        for payload in init.get(key) or []:
            pr = PendingRequest.from_payload(payload) if isinstance(payload, Mapping) else None
            if pr:
                out.append(pr)
    return out
