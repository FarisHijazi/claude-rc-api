"""Offline streaming recovery contracts, using only the existing HTTP/event writer."""
import json
from contextlib import contextmanager

import httpx
import pytest

from claude_rc import RemoteControlClient
from claude_rc.client import APIError, ControlRejected, ControlTimeout
from claude_rc import streaming
from claude_rc.sse import parse_sse

SID = 'cse_test'


def wire(seq, *, event_id=None, kind='assistant'):
    return {'sequence_num': str(seq), 'event_id': event_id or f'e-{seq}',
            'source': 'worker', 'payload': {'type': kind, 'message': {'role': 'assistant', 'content': f'text-{seq}'}}}


def frame(data, name='client_event', ident=None):
    return (f'event: {name}\n' + (f'id: {ident}\n' if ident is not None else '') +
            f'data: {json.dumps(data)}\n\n').encode()


@contextmanager
def client(handler):
    with RemoteControlClient(token='test-token', org_uuid='test-org', transport=httpx.MockTransport(handler)) as rc:
        yield rc


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    delays = []
    monkeypatch.setattr(streaming.time, 'sleep', delays.append)
    return delays


def test_history_pages_sorts_dedupes_and_keeps_envelopes():
    calls = []
    def handler(req):
        calls.append(req.url.params['cursor'])
        if len(calls) == 1:
            return httpx.Response(200, json={'data': [wire(2), wire(1)], 'next_cursor': 'opaque'})
        if len(calls) == 2:
            return httpx.Response(200, json={'events': [wire(2), wire(3)], 'next_cursor': '3'})
        return httpx.Response(200, json={'data': []})
    with client(handler) as rc:
        events = list(rc.iter_history(SID, page_size=2))
    assert [event.sequence_num for event in events] == [1, 2, 3]
    assert events[0].raw == wire(1)
    assert calls == ['0', 'opaque', '3']


def test_history_full_opaque_page_falls_back_to_sequence_cursor():
    cursors = []
    def handler(req):
        cursors.append(req.url.params['cursor'])
        pages = [{'data': [wire(1)], 'next_cursor': 'opaque'}, {'data': [wire(2)]}, {'data': []}]
        return httpx.Response(200, json=pages[len(cursors) - 1])
    with client(handler) as rc:
        assert [e.sequence_num for e in rc.iter_history(SID, page_size=1)] == [1, 2]
    assert cursors == ['0', 'opaque', '2']


def test_history_refuses_cursor_cycle():
    with client(lambda req: httpx.Response(200, json={'data': [], 'next_cursor': '0'})) as rc:
        with pytest.raises(RuntimeError, match='non-advancing'):
            list(rc.iter_history(SID))


def test_truncated_backfills_before_replay_preserving_meta():
    calls = []
    def handler(req):
        calls.append(req)
        if req.url.path.endswith('/events'):
            return httpx.Response(200, json={'data': [wire(3), wire(2)]})
        return httpx.Response(200, content=frame({'reason': 'limit'}, 'catch_up_truncated') + frame(wire(2)) + frame(wire(4)))
    with client(handler) as rc:
        events = list(rc.stream_session(SID, from_sequence_num=1, reconnect=False))
    assert [e.sequence_num for e in events] == [2, 3, None, 4]
    assert events[2].raw == {'event': 'catch_up_truncated', 'data': {'reason': 'limit'}}
    assert calls[1].url.params['cursor'] == '1'


def test_control_backfill_filters_transcript_but_advances_resume():
    calls = []
    def handler(req):
        calls.append(req)
        if req.url.path.endswith('/events'):
            return httpx.Response(200, json={'data': [wire(1), wire(2, kind='control_response')]})
        if len([r for r in calls if r.url.path.endswith('/stream')]) == 1:
            return httpx.Response(200, content=frame({}, 'catch_up_truncated'))
        raise httpx.ConnectError('stop')
    with client(handler) as rc:
        stream = rc.stream_control(SID, max_reconnects=1)
        assert next(stream).type == 'control_response'
        assert next(stream).type == 'catch_up_truncated'
        with pytest.raises(httpx.ConnectError):
            list(stream)
    assert calls[-1].url.params['from_sequence_num'] == '2'


@pytest.mark.parametrize('failure', ['eof', 'read', 'metadata', 'ephemeral', 'duplicate'])
def test_reconnect_budget_not_reset_by_attach_or_nonprogress(failure, no_sleep):
    calls = []
    class Failing(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadError('broken')
            yield b''
    def handler(req):
        calls.append(req)
        if failure == 'read':
            return httpx.Response(200, stream=Failing())
        content = {'eof': b'', 'metadata': frame({}, 'session_update'),
                   'ephemeral': frame(wire(999), 'ephemeral_event'),
                   'duplicate': frame(wire(1))}[failure]
        return httpx.Response(200, content=content)
    with client(handler) as rc:
        with pytest.raises((streaming.StreamExhausted, httpx.ReadError)):
            list(rc.stream_session(SID, from_sequence_num=1, max_reconnects=3, base_delay=.1))
    assert len(calls) == 4
    assert no_sleep == [.1, .2, .4]
    assert all(req.url.params['from_sequence_num'] == '1' for req in calls)


def test_new_durable_progress_resets_retry_budget(no_sleep):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, content=frame(wire(2)) if len(calls) == 2 else b'')
    with client(handler) as rc:
        with pytest.raises(streaming.StreamExhausted):
            list(rc.stream_session(SID, max_reconnects=2))
    assert len(calls) == 4
    assert no_sleep == [1, 1, 2]
    assert calls[2].headers['Last-Event-ID'] == '2'


def test_ephemeral_ids_never_poison_resume_and_event_ids_dedupe():
    calls = []
    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(200, content=frame(wire(1)) + frame(wire(900), 'ephemeral_event', 900) + frame(wire(2, event_id='e-1')))
        return httpx.Response(200, content=frame(wire(2)) + frame(wire(3)))
    with client(handler) as rc:
        stream = rc.stream_session(SID)
        events = [next(stream) for _ in range(3)]
        stream.close()
    assert [e.sequence_num for e in events] == [1, None, 3]
    assert calls[1].url.params['from_sequence_num'] == '2'
    assert calls[1].headers['Last-Event-ID'] == '2'


def test_unknown_frames_and_malformed_data_are_not_discarded():
    content = frame([1, 2], 'future_signal') + b'event: future_text\ndata: not-json\n\n'
    with client(lambda req: httpx.Response(200, content=content)) as rc:
        events = list(rc.stream_events(SID, reconnect=False))
    assert [event.type for event in events] == ['future_signal', 'future_text']
    assert events[0].raw['data'] == [1, 2]
    assert events[1].raw['data'] == 'not-json'


def test_reinitialize_callback_on_connection_recovery_and_worker_change():
    updates = [{'connection_status': 'CONNECTION_STATUS_CONNECTED', 'worker_id': 'one'},
               {'connection_status': 'CONNECTION_STATUS_DISCONNECTED'},
               {'connection_status': 'CONNECTION_STATUS_CONNECTED', 'worker_id': 'two'}]
    reinit = []
    with client(lambda req: httpx.Response(200, content=b''.join(frame(u, 'session_update') for u in updates))) as rc:
        assert len(list(rc.stream_session(SID, reconnect=False, on_reinitialize=reinit.append))) == 3
    assert reinit == [updates[2]]


@pytest.mark.parametrize('verdict', ['success', 'error', 'unrelated'])
def test_live_control_attaches_before_write_and_correlates_ephemeral(verdict):
    order, posted, responses = [], [], []
    class Live(httpx.SyncByteStream):
        def __iter__(self):
            assert posted
            response = {'type': 'control_response', 'response': {'request_id': posted[0]['request_id'] if verdict != 'unrelated' else 'other',
                        'subtype': 'error' if verdict == 'error' else 'success', 'error': 'rejected', 'response': {'mcpServers': []}}}
            yield frame({'payload': response}, 'ephemeral_event')
        def close(self):
            order.append('closed')
    def handler(req):
        if req.method == 'POST':
            order.append('post')
            posted.append(json.loads(req.content)['events'][0]['payload'])
            return httpx.Response(200, json={'results': []})
        order.append('attach')
        response = httpx.Response(200, stream=Live())
        responses.append(response)
        return response
    with client(handler) as rc:
        if verdict == 'success':
            assert rc.request_control(SID, 'mcp_status', liveReply=True) == {'mcpServers': []}
        else:
            with pytest.raises(ControlRejected if verdict == 'error' else ControlTimeout):
                rc.request_control_live(SID, 'mcp_status', liveReply=True)
    assert order == ['attach', 'post', 'closed']
    assert responses[0].is_closed
    assert posted[0]['request']['liveReply'] is True


def test_deadline_checked_even_when_only_heartbeats_arrive(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(streaming.time, 'monotonic', lambda: clock[0])
    class Heartbeats(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(5):
                clock[0] += 1
                yield b': keepalive\n\n'
    with client(lambda req: httpx.Response(200, stream=Heartbeats())) as rc:
        with pytest.raises(httpx.ReadTimeout, match='deadline'):
            list(rc.stream_session(SID, deadline=2, reconnect=False))
    assert clock[0] == 2


@pytest.mark.parametrize('stop', ['result', 'control_request', 'session.status_terminated'])
def test_send_collect_stops_and_closes_only_owned_stream(stop):
    order, closed = [], []
    payload = {'type': stop, 'subtype': 'terminated' if stop == 'system' else None}
    if stop == 'control_request':
        payload.update(request_id='permission', request={'subtype': 'can_use_tool', 'tool_name': 'Bash'})
    class Turn(httpx.SyncByteStream):
        def __iter__(self):
            assert order == ['history', 'attach', 'post']
            yield frame({'event_id': 'own', 'status': 'DELIVERY_STATUS_PROCESSING'}, 'delivery_update')
            yield frame({'sequence_num': '2', 'source': 'worker', 'payload': payload})
            raise AssertionError('read beyond stop')
        def close(self):
            closed.append(True)
    def handler(req):
        if req.method == 'POST':
            order.append('post')
            return httpx.Response(200, json={'results': [{'event_id': 'own'}]})
        if req.url.path.endswith('/stream'):
            order.append('attach')
            return httpx.Response(200, stream=Turn())
        order.append('history')
        return httpx.Response(200, json={'data': [wire(1)]})
    with client(handler) as rc:
        assert rc.send_and_collect(SID, 'hello')[-1].type == stop
    assert closed == [True]


def test_send_collect_failure_does_not_send_or_return_incomplete_success():
    posted = []
    def handler(req):
        if req.method == 'POST':
            posted.append(req)
        return httpx.Response(200, content=b'') if req.url.path.endswith('/stream') else httpx.Response(200, json={'data': []})
    with client(handler) as rc:
        with pytest.raises(streaming.StreamExhausted):
            rc.send_and_collect(SID, 'hello')
    assert len(posted) == 1
    with client(lambda req: httpx.Response(403, json={})) as rc:
        with pytest.raises(APIError):
            rc.send_and_collect(SID, 'never sent')


def test_watch_eof_budget_and_unknown_notifications(no_sleep):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, content=frame({}, 'added') + frame('new schema', 'future'))
    with client(handler) as rc:
        events = []
        with pytest.raises(streaming.StreamExhausted):
            for event in rc.watch(resume_token='test-resume', max_reconnects=2):
                events.append(event)
    assert len(calls) == 3 and no_sleep == [1, 2]
    assert events[1].raw == 'new schema'


def test_watch_410_remints_and_emits_resync(no_sleep):
    calls = []
    def handler(req):
        calls.append(req)
        if req.url.path.endswith('/sessions'):
            return httpx.Response(200, json={'resume_token': 'fresh'})
        if len(calls) == 1:
            return httpx.Response(200, content=frame({}, 'sync', 'newer'))
        if len(calls) == 2:
            return httpx.Response(410, json={})
        return httpx.Response(200, content=frame({'id': SID}, 'changed'))
    with client(handler) as rc:
        stream = rc.watch(resume_token='old', max_reconnects=2, channels_changed=True)
        assert next(stream).kind == 'sync'
        assert next(stream).kind == 'resync'
        assert next(stream).kind == 'changed'
        stream.close()
    assert calls[-1].url.params['resume_token'] == 'fresh'
    assert calls[-1].url.params['channels_changed'] == 'true'


def test_initial_watch_410_signals_resync_even_before_first_successful_attach():
    calls = []
    def handler(req):
        calls.append(req)
        if req.url.path.endswith('/sessions'):
            return httpx.Response(200, json={'resume_token': 'fresh'})
        if len(calls) == 1:
            return httpx.Response(410, json={})
        return httpx.Response(200, content=frame({}, 'sync', 'next-checkpoint'))
    with client(handler) as rc:
        watch = rc.watch(resume_token='expired', max_reconnects=1)
        assert next(watch).kind == 'resync'
        assert next(watch).kind == 'sync'
        watch.close()


def test_stream_does_not_retry_transport_owner_identity_rejection():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(403, json={'error': {'type': 'identity_changed'}})
    with client(handler) as rc:
        with pytest.raises(APIError):
            list(rc.stream_session(SID))
    assert len(calls) == 1


def test_control_stream_filters_locally_to_keep_ephemeral_replies():
    calls = []
    response = {'payload': {'type': 'control_response', 'response': {'request_id': 'test', 'subtype': 'success'}}}
    def handler(req):
        calls.append(req)
        content = frame(wire(1)) + frame(wire(2, kind='control_request'))
        if req.url.params.get('control_only') != '1':
            content += frame(response, 'ephemeral_event')
        return httpx.Response(200, content=content)
    with client(handler) as rc:
        events = list(rc.stream_control(SID, reconnect=False))
    assert [e.type for e in events] == ['control_request', 'control_response']
    assert events[-1].sequence_num is None
    assert 'control_only' not in calls[0].url.params


@pytest.mark.parametrize('page_size,sequence', [(0, 0), (501, 0), (1, -1)])
def test_history_validates_live_page_limit_before_http(page_size, sequence):
    def no_request(req):
        raise AssertionError('validation must happen before HTTP')
    with client(no_request) as rc:
        with pytest.raises(ValueError):
            list(rc.iter_history(SID, page_size=page_size, after_sequence_num=sequence))


def test_collect_reconnects_without_resending_prompt():
    posts, attaches = [], []
    def handler(req):
        if req.method == 'POST':
            posts.append(req)
            return httpx.Response(200, json={'results': [{'event_id': 'own'}]})
        if req.url.path.endswith('/stream'):
            attaches.append(req)
            content = frame(wire(1))
            if len(attaches) > 1:
                content += frame({'event_id': 'own', 'status': 'DELIVERY_STATUS_PROCESSING'}, 'delivery_update')
                content += frame(wire(2, kind='result'))
            return httpx.Response(200, content=content)
        return httpx.Response(200, json={'data': []})
    with client(handler) as rc:
        events = rc.send_and_collect(SID, 'hello', max_reconnects=1)
    assert [e.sequence_num for e in events if e.sequence_num is not None] == [1, 2]
    assert len(posts) == 1
    assert attaches[1].url.params['from_sequence_num'] == '1'


def test_collect_uses_existing_priority_writer():
    posted = []
    def handler(req):
        if req.method == 'POST':
            posted.append(json.loads(req.content)['events'][0]['payload'])
            return httpx.Response(200, json={'results': [{'event_id': 'own'}]})
        if req.url.path.endswith('/stream'):
            return httpx.Response(200, content=(
                frame({'event_id': 'own', 'status': 'DELIVERY_STATUS_PROCESSING'}, 'delivery_update') +
                frame(wire(1, kind='result'))))
        return httpx.Response(200, json={'data': []})
    with client(handler) as rc:
        rc.send_and_collect(SID, 'hello', priority='next', message_uuid='owned-id')
    assert posted[0]['priority'] == 'next' and posted[0]['uuid'] == 'owned-id'


@pytest.mark.parametrize('command,expected', [
    (['history', SID, '--page-size', '2'], 'assistant'),
    (['stream', SID, '--no-reconnect'], 'future_signal'),
    (['stream', SID, '--no-reconnect', '--control-only'], 'future_signal'),
    (['watch-account', '--resume-token', 'test-checkpoint', '--no-reconnect'], 'changed'),
])
def test_cli_registration_dispatches_through_injected_existing_client(command, expected, capsys):
    import argparse
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(required=True)
    requests = []
    def handler(req):
        requests.append(req)
        if req.url.path.endswith('/events'):
            return httpx.Response(200, json={'data': [wire(1)]})
        if req.url.path.endswith('/watch'):
            return httpx.Response(200, content=frame({'id': SID}, 'changed'))
        return httpx.Response(200, content=frame({'reason': 'test'}, 'future_signal'))
    streaming.register_stream_commands(subparsers, lambda args: client(handler))
    args = parser.parse_args(command)
    assert args.func(args) == 0
    output = json.loads(capsys.readouterr().out)
    actual = output.get('kind') or output.get('event') or output.get('payload', {}).get('type')
    assert actual == expected
    assert len(requests) == 1


def test_collect_does_not_accept_another_turn_result_before_own_processing():
    posted = []
    def handler(req):
        if req.method == 'POST':
            posted.append(json.loads(req.content)['events'][0]['payload'])
            return httpx.Response(200, json={'results': [{'event_id': 'own', 'sequence_num': '1'}]})
        if req.url.path.endswith('/stream'):
            return httpx.Response(200, content=(
                frame(wire(2, kind='result')) +
                frame({'event_id': 'own', 'status': 'DELIVERY_STATUS_RECEIVED'}, 'delivery_update') +
                frame(wire(3, kind='result')) +
                frame({'event_id': 'other', 'status': 'DELIVERY_STATUS_PROCESSING'}, 'delivery_update') +
                frame(wire(4, kind='result')) +
                frame({'event_id': 'own', 'status': 'DELIVERY_STATUS_PROCESSING'}, 'delivery_update') +
                frame(wire(5, kind='result'))))
        return httpx.Response(200, json={'data': []})
    with client(handler) as rc:
        events = rc.send_and_collect(SID, 'hello', reconnect=False)
    assert events[-1].sequence_num == 5
    assert len(posted) == 1


def test_collect_recovers_missed_processing_receipt_from_live_history():
    calls = []
    def handler(req):
        calls.append(req)
        if req.method == 'POST':
            return httpx.Response(200, json={'results': [{'event_id': 'own', 'sequence_num': '1'}]})
        if req.url.path.endswith('/stream'):
            result = {**wire(2, kind='result'), 'created_at': '2026-10-28T00:00:01Z'}
            return httpx.Response(200, content=frame(result))
        row = wire(1, event_id='own', kind='user')
        row['processing_at'] = '2026-10-28T00:00:00Z'
        return httpx.Response(200, json={'data': [] if len(calls) == 1 else [row]})
    with client(handler) as rc:
        assert rc.send_and_collect(SID, 'hello', reconnect=False)[-1].is_turn_end
    assert len([r for r in calls if r.url.path.endswith('/events') and r.method == 'GET']) == 2


def test_collect_rejects_historical_processing_newer_than_buffered_result():
    def handler(req):
        if req.method == 'POST':
            return httpx.Response(200, json={'results': [{'event_id': 'own'}]})
        if req.url.path.endswith('/stream'):
            old = {**wire(2, kind='result'), 'created_at': '2026-10-28T00:00:01Z'}
            current = {**wire(3, kind='result'), 'created_at': '2026-10-28T00:00:03Z'}
            return httpx.Response(200, content=frame(old) + frame(current))
        if req.url.params.get('sort_order') == 'desc':
            return httpx.Response(200, json={'data': []})
        row = {**wire(1, event_id='own', kind='user'), 'processing_at': '2026-10-28T00:00:02Z'}
        return httpx.Response(200, json={'data': [row]})
    with client(handler) as rc:
        assert rc.send_and_collect(SID, 'hello', reconnect=False)[-1].sequence_num == 3


def test_collect_keeps_earliest_processing_receipt_when_processed_arrives():
    def handler(req):
        if req.method == 'POST':
            return httpx.Response(200, json={'results': [{'event_id': 'own'}]})
        if req.url.path.endswith('/stream'):
            processing = {'event_id': 'own', 'status': 'DELIVERY_STATUS_PROCESSING', 'timestamp': '2026-10-28T00:00:01Z'}
            processed = {'event_id': 'own', 'status': 'DELIVERY_STATUS_PROCESSED', 'timestamp': '2026-10-28T00:00:03Z'}
            result = {**wire(2, kind='result'), 'created_at': '2026-10-28T00:00:02Z'}
            return httpx.Response(200, content=frame(processing, 'delivery_update') +
                                  frame(processed, 'delivery_update') + frame(result))
        return httpx.Response(200, json={'data': []})
    with client(handler) as rc:
        assert rc.send_and_collect(SID, 'hello', reconnect=False)[-1].is_turn_end


@pytest.mark.parametrize('callback_kind', ['ready', 'reinitialize'])
def test_callback_http_failure_propagates_without_stream_reconnect(callback_kind, no_sleep):
    calls, responses = [], []
    failure = httpx.ReadError('callback outcome unknown')
    def callback(*args):
        raise failure
    def handler(req):
        calls.append(req)
        response = httpx.Response(200, content=frame({'reinitialize': True}, 'session_update'))
        responses.append(response)
        return response
    option = 'on_ready' if callback_kind == 'ready' else 'on_reinitialize'
    with client(handler) as rc:
        with pytest.raises(httpx.ReadError) as caught:
            list(rc.stream_session(SID, max_reconnects=2, **{option: callback}))
    assert caught.value is failure
    assert len(calls) == 1 and no_sleep == []
    assert responses[0].is_closed


def test_reconnect_backoff_respects_absolute_deadline(monkeypatch):
    clock, delays, calls = [0.0], [], []
    monkeypatch.setattr(streaming.time, 'monotonic', lambda: clock[0])
    def sleep(delay):
        delays.append(delay)
        clock[0] += delay
    monkeypatch.setattr(streaming.time, 'sleep', sleep)
    def handler(req):
        calls.append(req)
        return httpx.Response(200, content=b'')
    with client(handler) as rc:
        with pytest.raises(httpx.ReadTimeout, match='deadline'):
            list(rc.stream_session(SID, deadline=.25, base_delay=10))
    assert delays == [.25]
    assert len(calls) == 1


def test_sse_multiline_comments_and_invalid_id_retry():
    events = list(parse_sse([': live', '', 'event: test', 'id: good', 'id: bad\x00id',
                             'retry: -1', 'data: one', 'data: two', ''], include_heartbeats=True))
    assert events[0].is_heartbeat
    assert events[1].data == 'one\ntwo'
    assert events[1].id == 'good' and events[1].retry is None
