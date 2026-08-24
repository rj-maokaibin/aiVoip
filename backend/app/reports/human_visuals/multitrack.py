from __future__ import annotations

import io
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import numpy as np

from .theme import COLORS
from .typography import human_font_properties, localized_text


def _envelope(track: dict, window_start: float, window_end: float) -> tuple[np.ndarray,np.ndarray,np.ndarray]:
    waveform=track.get("waveform") or {};bins=list(waveform.get("bins") or [])
    if not bins:return np.asarray([]),np.asarray([]),np.asarray([])
    base=float(track.get("start_time") or 0.0)
    t=np.asarray([base+float(x.get("t") or 0.0) for x in bins],dtype=float)
    lo=np.asarray([float(x.get("min") or 0.0)/32768.0 for x in bins],dtype=float)
    hi=np.asarray([float(x.get("max") or 0.0)/32768.0 for x in bins],dtype=float)
    mask=(t>=window_start)&(t<=window_end)
    return t[mask],lo[mask],hi[mask]


def render_human_multitrack_png(tracks:list[dict],*,window_start:float,window_end:float,
                                anomaly_start:float|None=None,anomaly_end:float|None=None,
                                events:list[dict]|None=None,title:str="跨层媒体同轴波形",
                                width_px:int=1800,height_px:int|None=None)->tuple[bytes,dict]:
    if window_end<=window_start:raise ValueError("MULTITRACK_WINDOW_INVALID")
    height_px=height_px or max(620,260+170*max(1,len(tracks)))
    fig,ax=plt.subplots(figsize=(width_px/160.0,height_px/160.0),constrained_layout=True)
    fig.patch.set_facecolor(COLORS["background"]);ax.set_facecolor(COLORS["panel"])
    n=max(1,len(tracks));available=[]
    for index,track in enumerate(tracks):
        row=float(n-index);label=str(track.get("label") or f"Track {index+1}")
        t,lo,hi=_envelope(track,window_start,window_end)
        ax.axhline(row,color=COLORS["grid"],linewidth=.7)
        if t.size:
            scale=.34
            ax.fill_between(t, row+lo*scale, row+hi*scale, color=COLORS["waveform_fill"],alpha=.72,linewidth=0)
            ax.plot(t,row+hi*scale,color=COLORS["waveform"],linewidth=.45)
            ax.plot(t,row+lo*scale,color=COLORS["waveform"],linewidth=.45)
            available.append(label)
        else:
            ax.text(window_start+(window_end-window_start)*.5,row,localized_text("UNAVAILABLE（无可对齐波形）","UNAVAILABLE"),ha="center",va="center",fontproperties=human_font_properties(size=8.5),color=COLORS["muted"])
    if anomaly_start is not None:
        a=max(window_start,float(anomaly_start));b=min(window_end,float(anomaly_end if anomaly_end is not None else anomaly_start))
        if b<=a:b=min(window_end,a+.02)
        ax.axvspan(a,b,color=COLORS["anomaly"],alpha=.10);ax.axvline(a,color=COLORS["anomaly"],linewidth=1.0);ax.axvline(b,color=COLORS["anomaly"],linewidth=1.0)
    event_rows=[]
    for event in events or []:
        try:when=float(event.get("time"))
        except (TypeError,ValueError):continue
        if not window_start<=when<=window_end:continue
        label=str(event.get("label") or event.get("type") or "EVENT")
        ax.axvline(when,color=COLORS["reference"],linewidth=.8,alpha=.65,linestyle="--")
        ax.text(when,n+.48,label,rotation=90,va="bottom",ha="right",fontproperties=human_font_properties(size=7.8),color=COLORS["muted"])
        event_rows.append({"time":when,"label":label})
    ax.set_xlim(window_start,window_end);ax.set_ylim(.45,n+.75)
    ax.set_yticks([float(n-i) for i in range(len(tracks))]);ax.set_yticklabels([str(x.get("label") or f"Track {i+1}") for i,x in enumerate(tracks)],fontproperties=human_font_properties(size=9.5))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x,_pos:f"{x-window_start:.3f}"))
    for label in ax.get_xticklabels():label.set_fontproperties(human_font_properties(size=8.5))
    ax.set_xlabel(localized_text("相对证据窗口时间（s）","Time from evidence-window start (s)"),fontproperties=human_font_properties(size=10))
    ax.set_title(localized_text(title,"Cross-layer aligned waveforms"),loc="left",fontproperties=human_font_properties(size=15,weight="semibold"),pad=22)
    ax.text(0,1.01,localized_text(f"绝对窗口 {window_start:.6f}～{window_end:.6f}｜各轨均按 PCM16 full-scale 显示，不做独立自动增益",f"Absolute window {window_start:.6f}-{window_end:.6f}"),transform=ax.transAxes,fontproperties=human_font_properties(size=8.5),color=COLORS["muted"])
    ax.grid(True,axis="x",alpha=.25);ax.spines["top"].set_visible(False);ax.spines["right"].set_visible(False)
    out=io.BytesIO();fig.savefig(out,format="png",dpi=160,bbox_inches="tight",facecolor=COLORS["background"]);plt.close(fig)
    return out.getvalue(),{
        "measurement_method":"ALIGNED_WAVEFORM_ENVELOPE_V1","window_start":window_start,"window_end":window_end,
        "window_duration_seconds":round(window_end-window_start,6),"track_count":len(tracks),"available_tracks":available,
        "unavailable_tracks":[str(x.get("label")) for x in tracks if str(x.get("label")) not in available],"events":event_rows,
        "amplitude_reference":"PCM16_FULL_SCALE_32768","authority":"PRESENTATION_ONLY",
    }
