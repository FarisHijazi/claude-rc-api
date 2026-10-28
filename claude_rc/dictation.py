"""Captured browser dictation protocol, without microphone access.

The cookie websocket uses /api/ws/speech_to_text/voice_stream. Binary audio
is followed by CloseStream; text frames include TranscriptInterim,
TranscriptText, TranscriptEndpoint, TranscriptError and KeepAliveAck.
Only connection establishment may redial: replaying audio after a partial
transcript could duplicate words. Ticket-based desktop auth is not implemented.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlencode

from .tts import _connect

DICTATION_PATH = "/api/ws/speech_to_text/voice_stream"


class DictationError(RuntimeError):
    """Dictation failed or ended without a final transcript."""


def dictation_url(*, language: str = "en", use_conversation_engine: bool = True,
                  redial: bool = False, org_uuid: str | None = None) -> str:
    query = {
        "encoding": "linear16" if use_conversation_engine else "opus",
        "sample_rate": "16000", "channels": "1", "endpointing_ms": "300",
        "utterance_end_ms": "1000", "language": language,
        "use_conversation_engine": str(use_conversation_engine).lower(),
        "forward_interims": "typed", "client_platform": "web_claude_ai",
        "no_capture": "true", "keepalive_ack": "true",
    }
    if org_uuid:
        query["organization_uuid"] = org_uuid
    if redial:
        query["connect_reason"] = "redial"
    return f"wss://claude.ai{DICTATION_PATH}?{urlencode(query)}"


def transcribe_audio(
    client: Any, audio: bytes | Iterable[bytes], *, language: str = "en",
    use_conversation_engine: bool = True, timeout: float = 30.0,
    connect: Callable[..., Any] | None = None, max_connect_retries: int = 2,
    on_event: Callable[[dict], None] | None = None,
) -> str:
    """Transcribe finite, already-generated audio; never opens a microphone.

    Default input is raw signed little-endian PCM, 16 kHz mono (no WAV header).
    With conversation engine disabled, supply the browser's Opus encoding.
    Interim/endpoint/unknown events go to on_event; only final text is returned.
    A clean close without final text is a failure, not a successful empty result.
    Reconnect is bounded and pre-audio only; caller must decide how to recover
    a mid-stream failure. Timeout bounds connection/retry/read time, not a
    caller-provided audio iterator or callback that blocks indefinitely.
    """
    from websockets.exceptions import ConnectionClosed

    if timeout <= 0 or not 0 <= max_connect_retries <= 5:
        raise ValueError("timeout must be positive and max_connect_retries between 0 and 5")
    deadline = time.monotonic() + timeout
    ws = None
    for attempt in range(max_connect_retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("dictation connection deadline exceeded")
        try:
            ws = _connect(client, dictation_url(language=language,
                          use_conversation_engine=use_conversation_engine, redial=attempt > 0,
                          org_uuid=getattr(client, "org_uuid", None)),
                          connect, min(10.0, remaining))
            break
        except (OSError, TimeoutError, ConnectionClosed):
            if attempt == max_connect_retries:
                raise
            delay = min(0.25 * 2 ** attempt, max(0, deadline - time.monotonic()))
            time.sleep(delay)
    finals = []
    with ws:
        chunks = [audio] if isinstance(audio, bytes) else audio
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("audio chunks must be bytes")
            if time.monotonic() >= deadline:
                raise TimeoutError("dictation audio deadline exceeded")
            if chunk:
                ws.send(chunk)
        ws.send(json.dumps({"type": "CloseStream"}))
        for event in dictation_events(ws, deadline=deadline):
            if on_event is not None:
                on_event(event)
            if event.get("type") == "TranscriptText" and isinstance(event.get("data"), str):
                finals.append(event["data"])
    if not finals:
        raise DictationError("dictation closed without final transcript")
    return " ".join(finals)


def dictation_events(ws: Any, *, deadline: float) -> Iterator[dict]:
    """Read through socket close, with 4-second application keepalives."""
    from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

    next_keepalive = time.monotonic() + 4.0
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError("dictation transcript deadline exceeded")
        try:
            if now >= next_keepalive:
                ws.send(json.dumps({"type": "KeepAlive"}))
                next_keepalive = now + 4.0
            frame = ws.recv(timeout=min(deadline - now, max(0.001, next_keepalive - now)))
        except TimeoutError:
            continue
        except ConnectionClosedOK:
            return
        except ConnectionClosed as exc:
            raise DictationError("dictation socket disconnected; audio was not replayed") from exc
        if not isinstance(frame, str):
            raise DictationError("unexpected binary dictation reply")
        try:
            event = json.loads(frame)
        except ValueError as exc:
            raise DictationError("invalid dictation JSON") from exc
        if not isinstance(event, dict):
            raise DictationError("dictation event must be an object")
        if event.get("type") in ("TranscriptError", "error"):
            raise DictationError("dictation server reported an error")
        yield event
