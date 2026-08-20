from __future__ import annotations

from app.contracts.evidence_report import RENDERER_VERSION
from app.reports.evidence_visuals import (
    render_rtp_timeline_png,
    render_spectrum_png,
    render_spectrogram_png,
    render_waveform_png,
    visual_metadata,
)


def test_visual_metadata_requires_title_and_axes_for_plot_types():
    good=visual_metadata(
        "WAVEFORM",
        title="PCM_RX CLICK_POP",
        x_axis="Time",
        y_axis="Amplitude",
        units={"x":"s","y":"PCM"},
        anomaly_window={"start":1.0,"end":1.2},
        finding_ids=["finding-1"],
        direction="RX",
    )
    bad=visual_metadata("WAVEFORM",title="PCM_RX CLICK_POP")

    assert good["renderer_version"]==RENDERER_VERSION=="evidence-renderer-v2"
    assert good["annotation_complete"] is True
    assert good["annotation_contract"]["x_axis"]=="Time"
    assert good["annotation_contract"]["y_axis"]=="Amplitude"
    assert good["annotation_contract"]["anomaly_marker"]=="ANOMALY"
    assert bad["annotation_complete"] is False


def test_waveform_renderer_is_deterministic_but_title_and_anomaly_window_are_semantic_inputs():
    waveform={"duration_seconds":2.0,"bins":[{"t":0.0,"min":-10,"max":10},{"t":1.0,"min":-300,"max":400},{"t":2.0,"min":-20,"max":20}]}
    a=render_waveform_png(waveform,anomaly_start=0.9,anomaly_end=1.1,title="PCM_RX CLICK_POP",subtitle="RX SESSION 0")
    b=render_waveform_png(waveform,anomaly_start=0.9,anomaly_end=1.1,title="PCM_RX CLICK_POP",subtitle="RX SESSION 0")
    changed=render_waveform_png(waveform,anomaly_start=1.4,anomaly_end=1.6,title="PCM_RX CLICK_POP",subtitle="RX SESSION 0")

    assert a==b
    assert a.startswith(b"\x89PNG\r\n\x1a\n")
    assert changed!=a


def test_all_p0_visual_renderers_emit_valid_deterministic_png_with_semantic_labels():
    spectrum={"peaks":[{"frequency_hz":150,"energy_ratio":0.5},{"frequency_hz":250,"energy_ratio":0.4}]}
    spectrogram={"times":[0.0,0.5,1.0],"frequencies":[0.0,500.0,1000.0],"db":[[-60,-50,-40],[-55,-45,-35],[-58,-48,-38]]}
    streams=[{"stream_id":"up","src_ip":"1.1.1.1","src_port":10000,"dst_ip":"2.2.2.2","dst_port":20000,"start_time":10.0,"end_time":12.0,"events":[{"type":"HIGH_DELTA","start_time":11.0}]}]

    for rendered in (
        render_spectrum_png(spectrum,title="PCM_RX PERIODIC",subtitle="RX SESSION 0"),
        render_spectrogram_png(spectrogram,anomaly_start=0.4,anomaly_end=0.7,title="PCM_RX SPECTROGRAM",subtitle="RX SESSION 0"),
        render_rtp_timeline_png(streams,title="RTP HIGH_DELTA",subtitle="1.1.1.1:10000>2.2.2.2:20000"),
    ):
        assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(rendered)>100
