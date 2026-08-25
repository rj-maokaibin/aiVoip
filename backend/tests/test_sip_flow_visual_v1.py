from __future__ import annotations

from app.reports.sip_flow_visual import render_sip_call_flow_png


def test_sip_flow_renderer_reads_call_ladder_and_frame_labels_deterministically():
    calls=[{
        "call_id":"call-1",
        "ladder":[
            {"frame_number":100,"src":"192.168.150.4:5060","dst":"192.168.3.200:5060","label":"INVITE","method":"INVITE","status_code":None},
            {"frame_number":120,"src":"192.168.3.200:5060","dst":"192.168.150.4:5060","label":"200 OK","method":None,"status_code":200},
            {"frame_number":121,"src":"192.168.150.4:5060","dst":"192.168.3.200:5060","label":"ACK","method":"ACK","status_code":None},
        ],
    }]
    first=render_sip_call_flow_png(calls,title="SIP CALL FLOW",subtitle="CALL-001")
    second=render_sip_call_flow_png(calls,title="SIP CALL FLOW",subtitle="CALL-001")
    empty=render_sip_call_flow_png([{"call_id":"call-1","ladder":[]}],title="SIP CALL FLOW",subtitle="CALL-001")

    assert first==second
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(first)>100
    assert first!=empty
