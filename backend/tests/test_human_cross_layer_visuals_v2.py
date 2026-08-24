from __future__ import annotations

from app.reports.human_visuals.cross_layer import render_human_cross_layer_png
from app.reports.human_visuals.multitrack import render_human_multitrack_png
from app.reports.human_visuals.rtp_timeline import render_human_rtp_timeline_png

PNG=b"\x89PNG\r\n\x1a\n"


def _waveform():
    return {"duration_seconds":2.0,"bins":[{"t":0.0,"min":-1000,"max":1000},{"t":.5,"min":-8000,"max":9000},{"t":1.0,"min":-4000,"max":5000},{"t":1.5,"min":-12000,"max":11000},{"t":1.99,"min":-500,"max":500}]}


def test_multitrack_uses_shared_absolute_window_without_new_diagnosis():
    tracks=[
        {"label":"PCM RX","start_time":100.0,"waveform":_waveform()},
        {"label":"RTP Uplink","start_time":100.0,"waveform":_waveform()},
        {"label":"RTP Downlink","start_time":100.0,"waveform":{}},
    ]
    png,meta=render_human_multitrack_png(tracks,window_start=100.5,window_end=101.5,anomaly_start=100.8,anomaly_end=101.0,events=[{"time":100.9,"label":"DTMF 6"}])
    assert png.startswith(PNG)
    assert meta["measurement_method"]=="ALIGNED_WAVEFORM_ENVELOPE_V1"
    assert meta["window_duration_seconds"]==1.0
    assert "PCM RX" in meta["available_tracks"] and "RTP Uplink" in meta["available_tracks"]
    assert "RTP Downlink" in meta["unavailable_tracks"]
    assert meta["authority"]=="PRESENTATION_ONLY"


def test_cross_layer_projects_canonical_correlations_and_boundary_without_inference():
    layers=[{"name":"PCM RX","available":True},{"name":"RTP Uplink","available":True},{"name":"RTP Downlink","available":True},{"name":"PCM TX","available":False}]
    correlations=[{"from":"PCM RX","to":"RTP Uplink","absolute_correlation":.91,"lag_ms":18.0,"quality":"HIGH"}]
    boundary="异常首次可观测于 PCM RX；这是证据边界，不等于异常物理起源或最终根因。"
    png,meta=render_human_cross_layer_png(layers,correlations,canonical_boundary_statement=boundary)
    assert png.startswith(PNG)
    assert meta["correlations"][0]["absolute_correlation"]==.91
    assert meta["first_observable_boundary"]==boundary
    assert meta["boundary_inference_performed"] is False
    assert meta["authority"]=="PRESENTATION_ONLY"


def test_human_rtp_high_delta_keeps_delay_not_packet_loss_semantics():
    stream={"stream_id":"s1","start_time":10.0,"end_time":12.0,"src_ip":"1.1.1.1","src_port":10000,"dst_ip":"2.2.2.2","dst_port":20000,"ssrc":1,"ptime_ms":20,"packet_count":100,"lost_packets":0,"loss_rate":0.0,"max_delta_ms":146.0}
    metrics={"events":[{"time":11.0,"delta_ms":146.0,"expected_ptime_ms":20.0,"sequence_continuous":True,"previous_sequence":100,"current_sequence":101}]}
    png,meta=render_human_rtp_timeline_png(stream,finding_type="HIGH_DELTA",finding_metrics=metrics)
    assert png.startswith(PNG)
    assert meta["semantic_rule"]=="HIGH_DELTA != PACKET_LOSS"
    assert meta["lost_packets"]==0
    assert meta["events"][0]["sequence_continuous"] is True
    assert meta["authority"]=="PRESENTATION_ONLY"
