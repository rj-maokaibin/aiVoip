from __future__ import annotations


def build_unified_timeline(packet_result: dict, pcm_result: dict | None = None,
                           media_events: list[dict] | None = None,
                           correlations: list[dict] | None = None) -> list[dict]:
    events: list[dict] = []
    for anomaly in packet_result.get('anomalies', []):
        events.append({
            'time': anomaly.get('time'), 'source': 'PACKET', 'type': anomaly.get('type'),
            'severity': anomaly.get('severity'), 'details': anomaly.get('evidence', {}),
        })
    for call in packet_result.get('calls', []):
        if call.get('start_time') is not None:
            events.append({'time': call['start_time'], 'source': 'SIP', 'type': 'CALL_START', 'severity': 'INFO',
                           'details': {'call_id': call.get('call_id'), 'caller': call.get('caller'), 'callee': call.get('callee')}})
        if call.get('end_time') is not None:
            events.append({'time': call['end_time'], 'source': 'SIP', 'type': 'CALL_END', 'severity': 'INFO',
                           'details': {'call_id': call.get('call_id'), 'state': call.get('state')}})
    if pcm_result:
        for stream in pcm_result.get('streams', []):
            tap = stream.get('tap', {})
            for session in stream.get('sessions', []):
                start = session.get('start_time')
                for gap in session.get('gap_events', []):
                    events.append({'time': gap.get('time'), 'source': tap.get('name','PCM'), 'type': 'PCM_PACKET_GAP', 'severity': 'MEDIUM', 'details': gap})
                for d in session.get('dtmf_events', []):
                    if start is not None:
                        events.append({'time': start + d.get('start_seconds',0), 'source': tap.get('name','PCM'), 'type': 'DTMF', 'severity': 'INFO', 'details': d})
                for s in session.get('silence_events', []):
                    if start is not None:
                        events.append({'time': start + s.get('start_seconds',0), 'source': tap.get('name','PCM'), 'type': 'SILENCE', 'severity': 'MEDIUM', 'details': s})
                for c in session.get('click_pop_events', []):
                    if start is not None:
                        events.append({'time': start + c.get('time_seconds',0), 'source': tap.get('name','PCM'), 'type': 'CLICK_POP', 'severity': 'MEDIUM', 'details': c})
    for event in media_events or []:
        events.append(event)
    for corr in correlations or []:
        events.append({'time': corr.get('time'), 'source': 'CORRELATION', 'type': corr.get('type','PCM_RTP_CORRELATION'), 'severity': corr.get('severity','INFO'), 'details': corr.get('details', corr)})
    return sorted((e for e in events if e.get('time') is not None), key=lambda e: (e['time'], e['source'], e['type']))
