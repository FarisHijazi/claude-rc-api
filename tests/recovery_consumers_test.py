"""Consumer handshake wiring, fault-injected replacement rather than live replacement."""
import json

import httpx

from test_streaming import SID, client, frame, wire


def test_collect_reinitializes_replaced_worker_without_resending_prompt(monkeypatch):
    posted = []
    initialized = []
    def handler(req):
        if req.method == 'POST':
            posted.append(json.loads(req.content)['events'][0]['payload'])
            return httpx.Response(200, json={'results': [{'event_id': 'own', 'sequence_num': '1'}]})
        if req.url.path.endswith('/stream'):
            return httpx.Response(200, content=(
                frame({'worker_id': 'first'}, 'session_update') +
                frame({'worker_id': 'replacement'}, 'session_update') +
                frame({'event_id': 'own', 'status': 'DELIVERY_STATUS_PROCESSING'}, 'delivery_update') +
                frame(wire(2, kind='result'))))
        return httpx.Response(200, json={'data': []})
    with client(handler) as rc:
        monkeypatch.setattr(rc, 'initialize', lambda sid: initialized.append(sid))
        assert rc.send_and_collect(SID, 'hello', reconnect=False)[-1].is_turn_end
    assert initialized == [SID]
    assert len(posted) == 1 and posted[0]['type'] == 'user'
