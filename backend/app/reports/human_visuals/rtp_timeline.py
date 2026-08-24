from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .theme import COLORS
from .typography import human_font_properties, localized_text

_EVENT_LABEL={
    "HIGH_DELTA":"延迟/节奏突增",
    "PACKET_LOSS":"RTP 丢包",
    "BURST_LOSS":"RTP 突发丢包",
    "PAYLOAD_CHANGE":"Payload Type 变化",
}


def _event_time(event:dict)->float|None:
    for key in ("time","start_time","representative_time"):
        if event.get(key) is not None:
            try:return float(event[key])
            except (TypeError,ValueError):pass
    return None


def _normalized_events(stream:dict,finding_metrics:dict|None)->list[dict]:
    metrics=finding_metrics or {};events=[]
    if isinstance(metrics.get("events"),list):
        for item in metrics["events"]:
            events.append({**item,"type":"HIGH_DELTA"})
    else:
        events.extend(stream.get("events") or [])
    out=[]
    for event in events:
        etype=str(event.get("type") or "").upper();when=_event_time(event)
        if when is None:continue
        row={"type":etype,"time":when,"label":_EVENT_LABEL.get(etype,etype or "RTP EVENT")}
        for key in ("delta_ms","expected_ptime_ms","sequence_continuous","previous_sequence","current_sequence","lost_packets","loss_count","classification","catch_up"):
            if key in event:row[key]=event.get(key)
        out.append(row)
    return sorted(out,key=lambda x:x["time"])


def render_human_rtp_timeline_png(stream:dict,*,finding_type:str|None=None,finding_metrics:dict|None=None,
                                  title:str="RTP 媒体时间线",width_px:int=1800,height_px:int=620)->tuple[bytes,dict]:
    start=float(stream.get("start_time") or 0.0);end=float(stream.get("end_time") or start+1.0)
    events=_normalized_events(stream,finding_metrics);candidate_times=[x["time"] for x in events]
    if candidate_times:
        start=min(start,min(candidate_times));end=max(end,max(candidate_times))
    if end<=start:end=start+1.0
    fig,ax=plt.subplots(figsize=(width_px/160.0,height_px/160.0),constrained_layout=True)
    fig.patch.set_facecolor(COLORS["background"]);ax.set_facecolor(COLORS["panel"])
    ax.hlines(0.5,start,end,color=COLORS["waveform"],linewidth=2.2)
    semantic=[]
    for index,event in enumerate(events):
        when=event["time"];etype=event["type"];label=event["label"]
        marker_color=COLORS["anomaly"] if etype in {"HIGH_DELTA","PACKET_LOSS","BURST_LOSS"} else COLORS["reference"]
        ax.vlines(when,.38,.72,color=marker_color,linewidth=1.5);ax.scatter([when],[.5],s=34,color=marker_color,zorder=4)
        lines=[label]
        if event.get("delta_ms") is not None:lines.append(f"{float(event['delta_ms']):.1f} ms")
        if etype=="HIGH_DELTA" and event.get("sequence_continuous") is True:lines.append("Seq 连续｜非丢包证据")
        ax.text(when,.76,"\n".join(lines),ha="center",va="bottom",fontproperties=human_font_properties(size=8.0),color=COLORS["text"])
        semantic.append(dict(event))
    direction=f"{stream.get('src_ip')}:{stream.get('src_port')} → {stream.get('dst_ip')}:{stream.get('dst_port')}"
    ax.set_title(localized_text(title,"RTP media timeline"),loc="left",fontproperties=human_font_properties(size=15,weight="semibold"),pad=24)
    ax.text(0,1.01,f"{direction}｜SSRC {stream.get('ssrc')}｜ptime {stream.get('ptime_ms')} ms",transform=ax.transAxes,fontproperties=human_font_properties(size=8.8),color=COLORS["muted"])
    ax.set_xlim(start,end);ax.set_ylim(.15,1.15);ax.set_yticks([])
    ticks=ax.get_xticks();ax.set_xticklabels([f"{x-start:.3f}" for x in ticks],fontproperties=human_font_properties(size=8.5))
    ax.set_xlabel(localized_text("相对时间（s）","Relative time (s)"),fontproperties=human_font_properties(size=10));ax.grid(True,axis="x",alpha=.25)
    ax.spines["top"].set_visible(False);ax.spines["right"].set_visible(False);ax.spines["left"].set_visible(False)
    note=localized_text("HIGH_DELTA 只表示包间隔/节奏异常；Sequence 连续时不得表述为 RTP 丢包。","HIGH_DELTA is not packet loss when sequence is continuous.")
    ax.text(.01,.04,note,transform=ax.transAxes,fontproperties=human_font_properties(size=8.5),color=COLORS["muted"])
    out=io.BytesIO();fig.savefig(out,format="png",dpi=160,bbox_inches="tight",facecolor=COLORS["background"]);plt.close(fig)
    return out.getvalue(),{
        "measurement_method":"CANONICAL_RTP_EVENT_PROJECTION_V1","stream_id":stream.get("stream_id"),"direction":direction,
        "finding_type":finding_type,"events":semantic,"packet_count":stream.get("packet_count"),"lost_packets":stream.get("lost_packets",stream.get("lost")),
        "loss_rate":stream.get("loss_rate"),"ptime_ms":stream.get("ptime_ms"),"max_delta_ms":stream.get("max_delta_ms"),
        "authority":"PRESENTATION_ONLY","semantic_rule":"HIGH_DELTA != PACKET_LOSS",
    }
