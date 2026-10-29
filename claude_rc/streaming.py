"""Lossless session streams and resumable account watch, over the client's transport."""
from __future__ import annotations

import json
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator

import httpx

from .events import Event, cli_control_request, stream_meta_event
from .sse import parse_sse


class StreamExhausted(httpx.TransportError):
    """The stream exhausted its reconnect budget without durable progress."""


@dataclass
class RetryBudget:
    maximum: int = 20
    base_delay: float = 1.0
    max_delay: float = 30.0
    attempts: int = 0

    def __post_init__(self) -> None:
        if self.maximum < 0 or self.base_delay <= 0 or self.max_delay <= 0:
            raise ValueError("retry count must be nonnegative and backoff delays positive")

    def progress(self) -> None:
        self.attempts = 0

    def retry(self, error: httpx.HTTPError | None = None, *, deadline: float | None = None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise httpx.ReadTimeout("stream deadline exceeded")
        if self.attempts >= self.maximum:
            if error is not None:
                raise error
            raise StreamExhausted("stream closed without progress; reconnect budget exhausted")
        delay = min(self.base_delay * 2 ** min(self.attempts, 30), self.max_delay)
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - time.monotonic()))
        time.sleep(delay)
        self.attempts += 1


def history_page(self, session_id: str, *, limit: int = 200,
                 sort_order: str = "asc", cursor: str | None = None) -> dict:
    """One durable history page, retaining the service's pagination metadata."""
    if not 1 <= limit <= 500 or sort_order not in ("asc", "desc"):
        raise ValueError("history limit must be 1..500 and order asc or desc")
    params = {"limit": limit, "sort_order": sort_order}
    if cursor is not None:
        params["cursor"] = cursor
    body = self._request("GET", self._path(session_id, "events"), params=params)
    rows = body if isinstance(body, list) else body.get("data") or body.get("events") or []
    events = [Event.from_wire(row) for row in rows]
    metadata = {} if isinstance(body, list) else {k: v for k, v in body.items() if k not in ("data", "events")}
    # Older responses omit metadata. A full page needs one more fetch to prove EOF.
    if "next_cursor" not in metadata and "has_more" not in metadata and len(rows) == limit:
        metadata["next_cursor"] = str(events[-1].sequence_num) if events else None
    if metadata.get("has_more") and metadata.get("next_cursor") is None:
        if not events or events[-1].sequence_num is None:
            raise ValueError("history has_more without a usable continuation cursor")
        metadata["next_cursor"] = str(events[-1].sequence_num)
    return {**metadata, "events": events}


def iter_history(self, session_id: str, *, after_sequence_num: int = 0,
                 page_size: int = 500) -> Iterator[Event]:
    """Page ascending durable history, preserving the complete wire envelope.

    Follow the server's cursor, including short pages with a next_cursor. Refuse
    non-advancing cursors rather than looping forever or silently dropping data.
    """
    if not 1 <= page_size <= 500 or after_sequence_num < 0:
        raise ValueError("page_size must be 1..500 and sequence nonnegative")
    cursor = str(after_sequence_num)
    cursors: set[str] = set()
    last_seq = after_sequence_num
    seen_ids: set[str] = set()
    while True:
        if cursor in cursors:
            raise RuntimeError("history returned a non-advancing cursor")
        cursors.add(cursor)
        page_start = last_seq
        body = self._request("GET", self._path(session_id, "events"), params={
            "sort_order": "asc", "cursor": cursor, "limit": page_size,
        })
        if not isinstance(body, (dict, list)):
            raise ValueError("history response must be a JSON object or list")
        rows = body if isinstance(body, list) else body.get("data") or body.get("events") or []
        events = sorted((Event.from_wire(row) for row in rows),
                        key=lambda event: event.sequence_num if event.sequence_num is not None else -1)
        for event in events:
            if event.sequence_num is None or event.sequence_num <= last_seq:
                continue
            last_seq = event.sequence_num
            if event.event_id and event.event_id in seen_ids:
                continue
            if event.event_id:
                seen_ids.add(event.event_id)
            yield event
        next_cursor = body.get("next_cursor") if isinstance(body, dict) else None
        if next_cursor is not None:
            cursor = str(next_cursor)
        elif isinstance(body, dict) and body.get("has_more"):
            cursor = str(last_seq)
        elif len(rows) >= page_size and last_seq > page_start:
            cursor = str(last_seq)
        else:
            return


def stream_session(
    self, session_id: str, *, from_sequence_num: int = 0,
    control_only: bool = False, reconnect: bool = True, max_reconnects: int = 20,
    liveness: float = 45.0, base_delay: float = 1.0, max_delay: float = 30.0,
    on_ready: Callable[[], Any] | None = None,
    on_reinitialize: Callable[[dict], Any] | None = None,
    deadline: float | None = None,
) -> Iterator[Event]:
    """Follow durable + ephemeral events, backfilling a truncated replay.

    Only durable sequence/event IDs advance the resume cursor. Ephemeral frames
    are deliberately delivered every time, not deduplicated or used as progress.
    Unknown envelope-less frames retain their SSE name and data. EOF and read
    failures share a bounded backoff budget, reset only by a new durable event.
    on_ready runs once after HTTP attach (before reading), useful for send-first
    races. on_reinitialize receives replacement-worker session_update payloads;
    the control owner supplies any required handshake through the existing writer.
    """
    if from_sequence_num < 0 or liveness <= 0:
        raise ValueError("sequence must be nonnegative and liveness positive")
    budget = RetryBudget(max_reconnects, base_delay, max_delay)
    last_seq = from_sequence_num
    seen_ids: set[str] = set()
    worker_id = None
    worker_connection = None
    ready = on_ready

    def accept(event: Event) -> bool:
        nonlocal last_seq
        if event.sequence_num is not None:
            if event.sequence_num <= last_seq:
                return False
            last_seq = event.sequence_num
            if event.event_id and event.event_id in seen_ids:
                return False
            if event.event_id:
                seen_ids.add(event.event_id)
            budget.progress()
        return True

    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise httpx.ReadTimeout("stream deadline exceeded")
        read_timeout = min(liveness, max(0.001, deadline - time.monotonic())) if deadline is not None else liveness
        params: dict[str, Any] = {"from_sequence_num": last_seq}
        if control_only:
            params["control_only"] = "1"
        headers = {"Accept": "text/event-stream"}
        if last_seq:
            headers["Last-Event-ID"] = str(last_seq)
        error = None
        in_callback = False
        try:
            with closing(self._send(
                "GET", self._path(session_id, "events", "stream"), params=params,
                headers=headers, accept=(200,), retry=False, stream=True,
                timeout=httpx.Timeout(min(30.0, read_timeout), read=read_timeout),
            )) as response:
                if ready is not None:
                    callback, ready = ready, None
                    in_callback = True
                    callback()
                    in_callback = False
                for frame in parse_sse(response.iter_lines(), include_heartbeats=True):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise httpx.ReadTimeout("stream deadline exceeded")
                    if frame.is_heartbeat:
                        continue
                    try:
                        data = json.loads(frame.data) if frame.data else {}
                    except json.JSONDecodeError:
                        data = frame.data
                    if frame.event == "catch_up_truncated":
                        for event in iter_history(self, session_id, after_sequence_num=last_seq):
                            if accept(event) and (not control_only or event.type in ("control_request", "control_response", "control_cancel_request")):
                                yield event
                    if frame.event == "session_update" and isinstance(data, dict):
                        replacement = data.get("worker_id") or data.get("worker_generation")
                        connection = str(data.get("connection_status", "")).removeprefix("CONNECTION_STATUS_").lower()
                        changed = worker_id is not None and replacement is not None and replacement != worker_id
                        reconnected = worker_connection == "disconnected" and connection == "connected"
                        if on_reinitialize and (changed or reconnected or data.get("reinitialize")):
                            in_callback = True
                            on_reinitialize(data)
                            in_callback = False
                        if connection:
                            worker_connection = connection
                        if replacement is not None:
                            worker_id = replacement
                    if isinstance(data, dict) and ("payload" in data or frame.event in (None, "message", "client_event", "ephemeral_event")):
                        event = Event.from_wire(data)
                        if frame.event == "ephemeral_event":
                            event.sequence_num = None
                        elif event.sequence_num is None and frame.event == "client_event" and frame.id:
                            try:
                                event.sequence_num = int(frame.id)
                            except ValueError:
                                pass
                        if accept(event):
                            yield event
                    else:
                        yield stream_meta_event(frame.event or "message", data)
        except httpx.HTTPError as exc:
            # Callback writes may have reached the server. Never hide an unknown
            # send/handshake outcome behind a successful read-only reconnect.
            if in_callback:
                raise
            error = exc
        if not reconnect:
            if error is not None:
                raise error
            return
        budget.retry(error, deadline=deadline)


def register_stream_commands(subparsers, client_factory: Callable, *, parents=()) -> None:
    """Group08 integration hook: add history, stream and watch-account JSONL commands.

    client_factory(args) must return the CLI's existing authenticated client
    context manager. Registration is explicit; this module never imports cli.py.
    """
    def run(args):
        with client_factory(args) as client:
            return dispatch_stream_command(client, args)

    for name, help_text in (
        ("history", "page complete durable history as JSONL"),
        ("stream", "stream all session frames, including metadata"),
        ("watch-account", "resume account changes as JSONL"),
    ):
        parser = subparsers.add_parser(name, help=help_text, parents=list(parents))
        parser.set_defaults(func=run, stream_command=name)
        if name == "watch-account":
            parser.add_argument("--resume-token")
            parser.add_argument("--channels-changed", action="store_true", default=None)
        else:
            parser.add_argument("session_id")
            parser.add_argument("--from-seq", type=int, default=0)
        if name == "history":
            parser.add_argument("--page-size", type=int, default=500)
        else:
            parser.add_argument("--no-reconnect", action="store_true")
            parser.add_argument("--max-reconnects", type=int, default=20)
        if name == "stream":
            parser.add_argument("--control-only", action="store_true", help="filter locally, preserving ephemeral replies")
            parser.add_argument("--server-filter", action="store_true", help="server control_only=1; excludes ephemeral replies")


def dispatch_stream_command(client, args, *, emit: Callable[[dict], Any] | None = None) -> int:
    """Execute a registered command; emit receives JSON-ready envelopes, not text."""
    if emit is None:
        emit = lambda row: print(json.dumps(row), flush=True)
    if args.stream_command == "history":
        for event in iter_history(client, args.session_id, after_sequence_num=args.from_seq, page_size=args.page_size):
            emit(event.raw)
        return 0
    common = {"reconnect": not args.no_reconnect, "max_reconnects": args.max_reconnects}
    if args.stream_command == "watch-account":
        with closing(watch(client, resume_token=args.resume_token, channels_changed=args.channels_changed, **common)) as events:
            for event in events:
                emit({"kind": event.kind, "session": event.session, "resume_token": event.resume_token, "raw": event.raw})
    elif args.stream_command == "stream":
        source = (stream_control(client, args.session_id, server_filter=args.server_filter, from_sequence_num=args.from_seq, **common)
                  if args.control_only or args.server_filter else
                  stream_session(client, args.session_id, from_sequence_num=args.from_seq, **common))
        with closing(source) as events:
            for event in events:
                emit(event.raw)
    else:
        raise ValueError(f"unknown stream command: {args.stream_command}")
    return 0

def stream_control(self, session_id: str, *, server_filter: bool = False,
                   **kwargs: Any) -> Iterator[Event]:
    """Control verdicts and metadata, including live ephemeral replies.

    The live service's control_only=1 excludes ephemeral mcp_status replies.
    Default to an unfiltered HTTP attach and filter locally. Explicit
    server_filter=True reduces traffic but cannot promise ephemeral delivery.
    """
    with closing(stream_session(self, session_id, control_only=server_filter, **kwargs)) as stream:
        for event in stream:
            if server_filter or event.sequence_num is None or event.type in (
                "control_request", "control_response", "control_cancel_request",
            ):
                yield event


def request_control_live(self, session_id: str, subtype: str, *, timeout: float = 15.0,
                         **fields: Any) -> dict:
    """Attach before sending and correlate live (including ephemeral) verdicts."""
    from .client import ControlRejected, ControlTimeout

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    from .commands import validate_control_request
    validate_control_request(subtype, fields, cloud=self.control_profile == "cloud")
    request_id = str(uuid.uuid4())
    deadline = time.monotonic() + timeout
    def send() -> None:
        self.send_events(session_id, [cli_control_request(subtype, request_id=request_id, **fields)])

    try:
        with closing(stream_control(self, session_id, reconnect=False, liveness=timeout, deadline=deadline, on_ready=send)) as stream:
            for event in stream:
                if event.type == "control_response" and event.request_id == request_id:
                    verdict = event.payload.get("response") or {}
                    if verdict.get("subtype") == "error":
                        raise ControlRejected(str(verdict.get("error") or "control rejected"))
                    return verdict.get("response") or {}
                if time.monotonic() >= deadline:
                    break
    except httpx.ReadTimeout:
        pass
    raise ControlTimeout(request_id, f"no live {subtype} response within {timeout}s")


def send_and_collect(self, session_id: str, text: str, *, print_stream: bool = False,
                     timeout: float = 300.0, reconnect: bool = True,
                     max_reconnects: int = 20, **prompt_options: Any) -> list[Event]:
    """Attach before sending; collect until a result, terminal or blocking prompt.

    Uses the existing send_prompt writer, including its priority/attachment
    validation. Explicitly closes the HTTP response when a stop condition fires.
    A failed history read aborts before writing, not a replay of an old result.
    A result qualifies only after this message's processing receipt (or an
    earlier processing timestamp recovered from history), never its queue ack.
    Explicit result user-message UUIDs must also name this message: the next
    message can start processing before the previous result reaches the stream.
    Returned frames can include other turns while this message was queued.
    Stream deadlines bound reads/backoff, not synchronous history/POST callbacks.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    history = self.list_events(session_id, limit=1, sort_order="desc")
    start = max((event.sequence_num or 0 for event in history), default=0)
    collected: list[Event] = []
    message_id = prompt_options.get("message_uuid")
    if message_id is None:
        message_id = prompt_options["message_uuid"] = str(uuid.uuid4())
    event_id = None
    started = False
    started_at = None

    def send() -> None:
        nonlocal event_id
        ack = self.send_prompt(session_id, text, **prompt_options)
        results = ack.get("results") if isinstance(ack, dict) else None
        if results and isinstance(results[0], dict):
            event_id = results[0].get("event_id")

    def own(event: Event) -> bool:
        return bool((event_id and event.event_id == event_id) or
                    (event.type == "user" and event.payload.get("uuid") == message_id))

    def processing_time(event: Event):
        return (event.raw.get("processing_at") or event.raw.get("processed_at")) if own(event) else None

    def no_later_than(first, second) -> bool:
        try:
            return datetime.fromisoformat(first.replace("Z", "+00:00")) <= datetime.fromisoformat(second.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return False

    with closing(stream_session(
        self, session_id, from_sequence_num=start, reconnect=reconnect,
        max_reconnects=max_reconnects, deadline=time.monotonic() + timeout,
        on_ready=send, on_reinitialize=lambda _: self.initialize(session_id),
    )) as stream:
        for event in stream:
            collected.append(event)
            if own(event):
                event_id = event.event_id or event_id
            if processing_time(event) and not started:
                started, started_at = True, processing_time(event)
            if event.type == "delivery_update":
                receipt = event.raw.get("data") or {}
                if isinstance(receipt, dict) and event_id and receipt.get("event_id") == event_id:
                    if not started and receipt.get("status") in ("DELIVERY_STATUS_PROCESSING", "DELIVERY_STATUS_PROCESSED"):
                        started, started_at = True, receipt.get("timestamp")
            if event.type == "result":
                owners = event.payload.get("user_message_uuids")
                owner = event.payload.get("user_message_uuid")
                if "user_message_uuid" in event.payload or "user_message_uuids" in event.payload:
                    if message_id != owner and not (isinstance(owners, list) and message_id in owners):
                        continue
            if event.type == "result" or event.is_blocking_control:
                if not started:
                    # An earlier turn can finish or ask a question while ours is
                    # queued. A recovered receipt must predate this event.
                    for row in iter_history(self, session_id, after_sequence_num=start):
                        stamp = processing_time(row)
                        if stamp and no_later_than(stamp, event.raw.get("created_at")):
                            started, started_at = True, stamp
                            break
                if not started or (started_at and event.raw.get("created_at") and
                                   not no_later_than(started_at, event.raw["created_at"])):
                    continue
            if print_stream and started and event.role == "assistant" and event.text():
                print(event.text())
            if event.is_turn_end or event.is_terminal or event.is_blocking_control:
                return collected
    raise StreamExhausted("stream closed before a turn-end, terminal or blocking event")


def mint_resume_token(self) -> str:
    body = self.list_sessions(limit=1)
    token = body.get("resume_token") if isinstance(body, dict) else None
    if not token:
        raise RuntimeError("session list returned no resume_token")
    return str(token)


def watch(self, *, resume_token: str | None = None, include_trigger_sessions: bool = True,
          exclude_tags: str = "-", channels_changed: bool | None = None,
          keepalive_tolerance_seconds: int | None = 135, reconnect: bool = True,
          max_reconnects: int = 20, base_delay: float = 1.0,
          max_delay: float = 30.0) -> Iterator[Any]:
    """Follow the browser's watch feed; resume sync tokens or remint and resync.

    A freshly minted token rejected with 410 ends the feed, matching the browser
    guard. Other 410s remint within the same bounded reconnect budget. Sync token
    changes and changed/removed notifications count as progress, initial snapshots
    do not (otherwise a server returning only added/EOF loops forever).
    """
    from .client import APIError
    from .interaction import (WatchEvent, WATCH_PATH, WATCH_PLATFORM, WATCH_TOKEN_MAX_AGE,
                              WATCH_GONE_GRACE, WATCH_SYNC_STALL)

    budget = RetryBudget(max_reconnects, base_delay, max_delay)
    token, token_at, minted_at = resume_token, time.monotonic(), None
    connected_once = False
    needs_resync = False
    while True:
        stale = token is not None and time.monotonic() - token_at >= WATCH_TOKEN_MAX_AGE
        if token is None or stale:
            token, token_at = mint_resume_token(self), time.monotonic()
            minted_at = token_at
            if connected_once or needs_resync or stale:
                yield WatchEvent("resync", resume_token=token)
            needs_resync = False
        params: dict[str, Any] = {"exclude_tags": exclude_tags, "resume_token": token}
        if include_trigger_sessions:
            params["include_trigger_sessions"] = "true"
        if channels_changed is not None:
            params["channels_changed"] = str(channels_changed).lower()
        if keepalive_tolerance_seconds is not None:
            params["keepalive_tolerance_seconds"] = keepalive_tolerance_seconds
        error = None
        try:
            with closing(self._send(
                "GET", WATCH_PATH, params=params, client_platform=WATCH_PLATFORM,
                headers={"Accept": "text/event-stream", "Last-Event-ID": token},
                accept=(200,), retry=False, stream=True,
                timeout=httpx.Timeout(30.0, read=float(keepalive_tolerance_seconds or 120) + 30),
            )) as response:
                connected_once = True
                last_sync = time.monotonic()
                for frame in parse_sse(response.iter_lines(), include_heartbeats=True):
                    if frame.event == "sync":
                        last_sync = time.monotonic()
                        if frame.id:
                            if token != frame.id:
                                budget.progress()
                            token, token_at = frame.id, last_sync
                        yield WatchEvent("sync", resume_token=frame.id, raw=frame.data)
                    elif frame.event:
                        try:
                            data = json.loads(frame.data) if frame.data else {}
                        except json.JSONDecodeError:
                            data = frame.data
                        if frame.event in ("changed", "removed"):
                            budget.progress()
                        yield WatchEvent(frame.event, session=data if isinstance(data, dict) else {}, raw=data)
                    if time.monotonic() - last_sync >= WATCH_SYNC_STALL:
                        token = None
                        break
        except APIError as exc:
            if exc.status != 410:
                raise
            if not reconnect:
                raise
            if minted_at is not None and time.monotonic() - minted_at < WATCH_GONE_GRACE:
                return
            token = None
            needs_resync = True
        except httpx.HTTPError as exc:
            error = exc
        if not reconnect:
            if error is not None:
                raise error
            return
        budget.retry(error)
