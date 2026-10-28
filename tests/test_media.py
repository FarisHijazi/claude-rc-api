"""Offline speech regressions; fake sockets do not establish live support."""
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from claude_rc import dictation, tts


class Socket:
    def __init__(self, events):
        self.events = iter(events)
        self.sent = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def send(self, frame):
        self.sent.append(frame)
    def recv(self, **kwargs):
        event = next(self.events, None)
        if event is None:
            raise ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)
        return json.dumps(event)


def test_dictation_url_and_encoding():
    q = parse_qs(urlsplit(dictation.dictation_url()).query)
    assert q["encoding"] == ["linear16"] and q["sample_rate"] == ["16000"]
    assert q["no_capture"] == ["true"]
    assert parse_qs(urlsplit(dictation.dictation_url(use_conversation_engine=False, redial=True)).query)["connect_reason"] == ["redial"]


def test_dictation_only_final_text_and_all_status_events(monkeypatch):
    events = [{"type": "TranscriptInterim", "data": "not final"}, {"type": "TranscriptText", "data": "test complete"},
              {"type": "TranscriptEndpoint"}, {"type": "KeepAliveAck"}]
    ws = Socket(events)
    monkeypatch.setattr(dictation, "_connect", lambda *a: ws)
    seen = []
    assert dictation.transcribe_audio(object(), [b"pcm"], on_event=seen.append) == "test complete"
    assert seen == events
    assert ws.sent == [b"pcm", json.dumps({"type": "CloseStream"})]


@pytest.mark.parametrize("events", [[], [{"type": "TranscriptInterim", "data": "unfinished"}],
                                   [{"type": "TranscriptError", "description": "private detail"}], [{"type": "error"}]])
def test_dictation_never_calls_incomplete_audio_success(monkeypatch, events):
    monkeypatch.setattr(dictation, "_connect", lambda *a: Socket(events))
    with pytest.raises(dictation.DictationError):
        dictation.transcribe_audio(object(), b"pcm")


def test_dictation_bounded_pre_audio_reconnect(monkeypatch):
    calls = []
    ws = Socket([{"type": "TranscriptText", "data": "test complete"}])
    def connect(client, url, *args):
        calls.append(url)
        if len(calls) < 3:
            raise OSError("pre-open")
        return ws
    monkeypatch.setattr(dictation, "_connect", connect)
    monkeypatch.setattr(dictation.time, "sleep", lambda _: None)
    assert dictation.transcribe_audio(object(), b"pcm") == "test complete"
    assert len(calls) == 3 and "connect_reason=redial" in calls[-1]
    assert ws.sent.count(b"pcm") == 1


def test_dictation_keepalive_and_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(dictation.time, "monotonic", lambda: clock[0])
    class Idle:
        sent = []
        def recv(self, timeout):
            clock[0] += timeout
            raise TimeoutError()
        def send(self, frame):
            self.sent.append(json.loads(frame))
    ws = Idle()
    with pytest.raises(TimeoutError):
        list(dictation.dictation_events(ws, deadline=9.0))
    assert ws.sent == [{"type": "KeepAlive"}, {"type": "KeepAlive"}]


def test_keepalive_racing_clean_close_preserves_final(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(dictation.time, "monotonic", lambda: clock[0])
    class Closing:
        def send(self, frame):
            raise ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)
        def recv(self, **kwargs):
            clock[0] = 5.0
            return json.dumps({"type": "TranscriptText", "data": "test complete"})
    assert list(dictation.dictation_events(Closing(), deadline=10)) == [{"type": "TranscriptText", "data": "test complete"}]


def test_cli_registration_dispatches_existing_client(tmp_path, capsys):
    import argparse
    from claude_rc.media_cli import register_media_commands
    from claude_rc.client import RemoteControlClient
    import httpx
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(required=True)
    seen = []
    def register(name, handler, help, session=False):
        command = sub.add_parser(name, help=help)
        command.set_defaults(func=handler)
        if session:
            command.add_argument("session_id")
        return command
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"content_sha256": "a" * 64})
    def make_client(args):
        return RemoteControlClient(token="test", org_uuid="org", transport=httpx.MockTransport(handler))
    register_media_commands(register, make_client)
    local = tmp_path / "fixture.txt"
    local.write_bytes(b"changed")
    args = parser.parse_args(["sync-file", "cse_owned", "/working/fixture.txt", str(local), "--if-match", "b" * 64])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["content_sha256"] == "a" * 64
    assert json.loads(seen[0].content)["if_match_sha256"] == "b" * 64
    assert seen[0].url.path.endswith("/synced_file")


def test_tts_normal_close_without_completion_is_failure():
    with pytest.raises(tts.TTSError, match="before SpeechComplete"):
        list(tts.speech_packets(Socket([]), "test"))


@pytest.mark.parametrize("packet", [b"", b"\x03", b"\x03\x00", b"\xff\x3f"])
def test_opus_rejects_invalid_duration(packet):
    with pytest.raises(ValueError):
        tts.opus_samples(packet)
