from __future__ import annotations

import math
from typing import Any

from .evidence_visuals_core import *  # noqa: F401,F403
from .evidence_visuals_core import Canvas, _heat_color, _plot_box
from app.contracts.evidence_report import RENDERER_VERSION


SEMANTIC_RENDERER_VERSION = "evidence-semantic-renderer-v2"

# Tiny deterministic 5x7 bitmap font. We intentionally use ASCII technical
# labels inside evidence images so rendering is stable on Worker/CI hosts and
# does not depend on system font packages.
_FONT = {
    "A":["01110","10001","10001","11111","10001","10001","10001"],"B":["11110","10001","10001","11110","10001","10001","11110"],
    "C":["01111","10000","10000","10000","10000","10000","01111"],"D":["11110","10001","10001","10001","10001","10001","11110"],
    "E":["11111","10000","10000","11110","10000","10000","11111"],"F":["11111","10000","10000","11110","10000","10000","10000"],
    "G":["01111","10000","10000","10111","10001","10001","01111"],"H":["10001","10001","10001","11111","10001","10001","10001"],
    "I":["11111","00100","00100","00100","00100","00100","11111"],"J":["00111","00010","00010","00010","10010","10010","01100"],
    "K":["10001","10010","10100","11000","10100","10010","10001"],"L":["10000","10000","10000","10000","10000","10000","11111"],
    "M":["10001","11011","10101","10101","10001","10001","10001"],"N":["10001","11001","10101","10011","10001","10001","10001"],
    "O":["01110","10001","10001","10001","10001","10001","01110"],"P":["11110","10001","10001","11110","10000","10000","10000"],
    "Q":["01110","10001","10001","10001","10101","10010","01101"],"R":["11110","10001","10001","11110","10100","10010","10001"],
    "S":["01111","10000","10000","01110","00001","00001","11110"],"T":["11111","00100","00100","00100","00100","00100","00100"],
    "U":["10001","10001","10001","10001","10001","10001","01110"],"V":["10001","10001","10001","10001","10001","01010","00100"],
    "W":["10001","10001","10001","10101","10101","11011","10001"],"X":["10001","10001","01010","00100","01010","10001","10001"],
    "Y":["10001","10001","01010","00100","00100","00100","00100"],"Z":["11111","00001","00010","00100","01000","10000","11111"],
    "0":["01110","10001","10011","10101","11001","10001","01110"],"1":["00100","01100","00100","00100","00100","00100","01110"],
    "2":["01110","10001","00001","00010","00100","01000","11111"],"3":["11110","00001","00001","01110","00001","00001","11110"],
    "4":["00010","00110","01010","10010","11111","00010","00010"],"5":["11111","10000","10000","11110","00001","00001","11110"],
    "6":["01110","10000","10000","11110","10001","10001","01110"],"7":["11111","00001","00010","00100","01000","01000","01000"],
    "8":["01110","10001","10001","01110","10001","10001","01110"],"9":["01110","10001","10001","01111","00001","00001","01110"],
    "-":["00000","00000","00000","11111","00000","00000","00000"],"_":["00000","00000","00000","00000","00000","00000","11111"],
    ".":["00000","00000","00000","00000","00000","00110","00110"],":":["00000","00110","00110","00000","00110","00110","00000"],
    "/":["00001","00010","00100","01000","10000","00000","00000"],">":["10000","01000","00100","00010","00100","01000","10000"],
    "<":["00001","00010","00100","01000","00100","00010","00001"],"+":["00000","00100","00100","11111","00100","00100","00000"],
    "=":["00000","11111","00000","11111","00000","00000","00000"],"%":["11001","11010","00100","01000","10110","00110","00000"],
    "#":["01010","11111","01010","01010","11111","01010","00000"],"(":["00010","00100","01000","01000","01000","00100","00010"],
    ")":["01000","00100","00010","00010","00010","00100","01000"],"[":["01110","01000","01000","01000","01000","01000","01110"],
    "]":["01110","00010","00010","00010","00010","00010","01110"],"?":["01110","10001","00001","00010","00100","00000","00100"],
    " ":["00000"]*7,
}


def _safe_text(value: Any, limit: int = 80) -> str:
    raw = str(value or "").upper()
    text = "".join(ch if ch in _FONT else "?" for ch in raw)
    return text[:limit]


def _text(canvas: Canvas, x: int, y: int, text: Any, *, scale: int = 2, color=(35,35,35), max_chars: int = 100) -> None:
    cursor = x
    for ch in _safe_text(text, max_chars):
        glyph = _FONT.get(ch, _FONT["?"])
        for gy,row in enumerate(glyph):
            for gx,bit in enumerate(row):
                if bit == "1":
                    canvas.rect(cursor+gx*scale, y+gy*scale, cursor+(gx+1)*scale-1, y+(gy+1)*scale-1, color, True)
        cursor += 6*scale


def _title(canvas: Canvas, context: dict | None, fallback: str) -> None:
    context = context or {}
    title = context.get("title") or fallback
    _text(canvas, 72, 10, title, scale=2, color=(25,25,25), max_chars=78)
    subtitle = context.get("subtitle")
    if subtitle:
        _text(canvas, 72, 28, subtitle, scale=1, color=(80,80,80), max_chars=130)


def _axis_ticks(canvas: Canvas, left: int, top: int, right: int, bottom: int, *, duration: float | None = None, max_frequency: float | None = None) -> None:
    if duration is not None:
        for frac in (0.0,0.25,0.5,0.75,1.0):
            x=int(left+(right-left)*frac); canvas.line(x,bottom,x,bottom+6,(90,90,90),1)
            _text(canvas,max(left,x-20),bottom+10,f"{duration*frac:.2F}s",scale=1,color=(80,80,80),max_chars=10)
    if max_frequency is not None:
        for frac in (0.0,0.25,0.5,0.75,1.0):
            y=int(bottom-(bottom-top)*frac); canvas.line(left-6,y,left,y,(90,90,90),1)
            _text(canvas,4,max(top,y-4),f"{max_frequency*frac:.0F}",scale=1,color=(80,80,80),max_chars=8)


def render_waveform_png(waveform: dict, *, anomaly_start: float | None = None,
                        anomaly_end: float | None = None, width: int = 1400, height: int = 620,
                        context: dict | None = None) -> bytes:
    canvas=Canvas(width,height); _title(canvas,context,"WAVEFORM | TIME VS AMPLITUDE"); left,top,right,bottom=_plot_box(canvas)
    bins=waveform.get("bins") or []
    duration=float(waveform.get("duration_seconds") or (bins[-1].get("t") if bins else 1.0) or 1.0)
    if anomaly_start is not None:
        a=max(0.0,min(duration,float(anomaly_start))); b=max(a,min(duration,float(anomaly_end if anomaly_end is not None else anomaly_start)))
        xa=int(left+(right-left)*a/max(duration,1e-9)); xb=int(left+(right-left)*b/max(duration,1e-9))
        canvas.rect(xa,top,xb,bottom,(245,235,220),True); _text(canvas,max(left,xa),top+8,"ANOMALY",scale=1,color=(160,45,45))
    mid=(top+bottom)//2; canvas.line(left,mid,right,mid,(185,185,185))
    if bins:
        max_abs=max(1.0,max(abs(float(x.get("min",0))) for x in bins),max(abs(float(x.get("max",0))) for x in bins))
        for item in bins:
            t=float(item.get("t") or 0.0); x=int(left+(right-left)*t/max(duration,1e-9))
            ymin=int(mid-float(item.get("max",0))/max_abs*(bottom-top)*0.46); ymax=int(mid-float(item.get("min",0))/max_abs*(bottom-top)*0.46)
            canvas.line(x,ymin,x,ymax,(45,85,120))
    _axis_ticks(canvas,left,top,right,bottom,duration=duration)
    _text(canvas,left,bottom+30,"TIME (S)",scale=1,color=(60,60,60)); _text(canvas,left+8,top+8,"AMPLITUDE",scale=1,color=(60,60,60))
    return canvas.png_bytes()


def render_spectrum_png(spectral: dict, *, width: int = 1400, height: int = 620,
                        context: dict | None = None) -> bytes:
    canvas=Canvas(width,height); _title(canvas,context,"SPECTRUM | FREQUENCY HZ"); left,top,right,bottom=_plot_box(canvas)
    peaks=list(spectral.get("peaks") or [])
    maxf=max([float(p.get("frequency_hz") or 0) for p in peaks]+[1000.0]); maxr=max([float(p.get("energy_ratio") or 0) for p in peaks]+[1e-6])
    for p in peaks:
        f=float(p.get("frequency_hz") or 0); r=float(p.get("energy_ratio") or 0)
        x=int(left+(right-left)*f/maxf); y=int(bottom-(bottom-top)*r/maxr)
        canvas.line(x,bottom,x,y,(35,70,110),5)
        if r >= maxr*0.15:
            _text(canvas,max(left,min(right-70,x-18)),max(top,y-18),f"{f:.0F}HZ",scale=1,color=(40,55,75),max_chars=10)
    for f in (50,60,150,250,350,450,550,650,750,850,950):
        if f>maxf: continue
        x=int(left+(right-left)*f/maxf); canvas.line(x,top,x,bottom,(205,205,205),1)
        if f in (50,60,150,250,350,450,550): _text(canvas,max(left,x-12),bottom-14,f"{f}",scale=1,color=(95,95,95),max_chars=5)
    _axis_ticks(canvas,left,top,right,bottom,max_frequency=None)
    _text(canvas,left,bottom+28,"FREQUENCY (HZ)",scale=1,color=(60,60,60)); _text(canvas,left+8,top+8,"REL ENERGY",scale=1,color=(60,60,60))
    return canvas.png_bytes()


def render_spectrogram_png(spec: dict, *, anomaly_start: float | None = None,
                           anomaly_end: float | None = None, width: int = 1400, height: int = 720,
                           context: dict | None = None) -> bytes:
    canvas=Canvas(width,height); _title(canvas,context,"SPECTROGRAM | TIME / FREQUENCY"); left,top,right,bottom=_plot_box(canvas)
    times=spec.get("times") or []; freqs=spec.get("frequencies") or []; matrix=spec.get("db") or []
    flat=[float(v) for row in matrix for v in row if isinstance(v,(int,float))]; lo=min(flat) if flat else -120.0; hi=max(flat) if flat else 0.0
    if matrix and times and freqs:
        nt=len(matrix); nf=max(len(row) for row in matrix)
        for ti,row in enumerate(matrix):
            x0=left+int((right-left)*ti/max(1,nt)); x1=left+int((right-left)*(ti+1)/max(1,nt))
            for fi,v in enumerate(row):
                y1=bottom-int((bottom-top)*fi/max(1,nf)); y0=bottom-int((bottom-top)*(fi+1)/max(1,nf))
                canvas.rect(x0,y0,max(x0,x1),max(y0,y1),_heat_color(float(v),lo,hi),True)
        duration=float(times[-1]) if times else 1.0; maxf=float(freqs[-1]) if freqs else 0.0
        if anomaly_start is not None and duration>0:
            a=max(0.0,min(duration,float(anomaly_start))); b=max(a,min(duration,float(anomaly_end if anomaly_end is not None else a)))
            xa=int(left+(right-left)*a/duration); xb=int(left+(right-left)*b/duration)
            canvas.line(xa,top,xa,bottom,(180,35,35),3); canvas.line(xb,top,xb,bottom,(180,35,35),3); _text(canvas,xa+4,top+8,"ANOMALY",scale=1,color=(160,35,35))
        for f in (50.0,60.0,150.0,250.0,350.0,450.0,550.0):
            if maxf and f<=maxf:
                y=bottom-int((bottom-top)*f/maxf); canvas.line(left,y,right,y,(120,120,120),1); _text(canvas,left+5,max(top,y-8),f"{f:.0F}HZ",scale=1,color=(80,80,80),max_chars=8)
        _axis_ticks(canvas,left,top,right,bottom,duration=duration,max_frequency=maxf)
    _text(canvas,left,bottom+28,"TIME (S)",scale=1,color=(60,60,60)); _text(canvas,left+8,top+8,"FREQUENCY (HZ) / REL DB",scale=1,color=(60,60,60))
    return canvas.png_bytes()


def render_rtp_timeline_png(streams: list[dict], *, width: int = 1400, height: int = 720,
                            context: dict | None = None) -> bytes:
    canvas=Canvas(width,height); _title(canvas,context,"RTP TIMELINE | DELTA / LOSS EVENTS"); left,top,right,bottom=_plot_box(canvas)
    if not streams: return canvas.png_bytes()
    starts=[float(s.get("start_time") or 0) for s in streams]; ends=[float(s.get("end_time") or s.get("start_time") or 0) for s in streams]
    lo=min(starts); hi=max(ends); span=max(1e-9,hi-lo); lane=max(26,(bottom-top)//max(1,len(streams)+1))
    for idx,s in enumerate(streams):
        y=top+(idx+1)*lane; x0=left+int((right-left)*(float(s.get("start_time") or lo)-lo)/span); x1=left+int((right-left)*(float(s.get("end_time") or hi)-lo)/span)
        canvas.line(x0,y,x1,y,(35,85,115),5)
        label=f"S{idx+1} {str(s.get('src_ip') or '')[-8:]}:{s.get('src_port')} > {str(s.get('dst_ip') or '')[-8:]}:{s.get('dst_port')}"
        _text(canvas,left+5,max(top,y-22),label,scale=1,color=(55,55,55),max_chars=70)
        for ev in s.get("events",[]) or []:
            t=float(ev.get("start_time") or lo); x=left+int((right-left)*(t-lo)/span); canvas.line(x,y-10,x,y+10,(180,45,45),3)
            details=ev.get("details") or {}; etype=str(ev.get("type") or "EVENT")
            if etype=="HIGH_DELTA": note=f"DELTA {details.get('delta_ms')}MS F{details.get('previous_frame_number')}>{details.get('current_frame_number')}"
            elif etype in {"PACKET_LOSS","BURST_LOSS"}: note=f"LOSS {details.get('lost_packets')} F{details.get('previous_frame_number')}>{details.get('next_frame_number')}"
            else: note=etype
            _text(canvas,max(left,min(right-190,x+4)),y+11,note,scale=1,color=(140,35,35),max_chars=38)
    _axis_ticks(canvas,left,top,right,bottom,duration=span); _text(canvas,left,bottom+30,"RELATIVE CAPTURE TIME (S)",scale=1,color=(60,60,60))
    return canvas.png_bytes()


def _sip_label(msg: dict) -> str:
    method=msg.get("method") or msg.get("request_method") or ""
    status=msg.get("status_code") or msg.get("response_code")
    frame=msg.get("frame_number") or msg.get("frame")
    if status: base=str(status)
    else: base=str(method or "SIP")
    return f"{base} F{frame}" if frame is not None else base


def render_sip_call_flow_png(calls: list[dict], *, width: int = 1400, height: int = 720,
                             context: dict | None = None) -> bytes:
    canvas=Canvas(width,height); _title(canvas,context,"SIP CALL FLOW | FRAME TRACE"); left,top,right,bottom=_plot_box(canvas)
    x_a=left+220; x_b=right-220; _text(canvas,x_a-70,top+8,"ENDPOINT A",scale=1,color=(55,55,55)); _text(canvas,x_b-70,top+8,"ENDPOINT B",scale=1,color=(55,55,55))
    canvas.line(x_a,top+30,x_a,bottom-20,(80,80,80),2); canvas.line(x_b,top+30,x_b,bottom-20,(80,80,80),2)
    messages=[]
    for call in calls:
        for msg in call.get("messages",[]) or call.get("sip_messages",[]) or []: messages.append(msg)
    messages=messages[:24]; step=max(22,(bottom-top-90)//max(1,len(messages)))
    for i,msg in enumerate(messages):
        y=top+55+i*step; outgoing=str(msg.get("direction") or "").upper() not in {"IN","INBOUND","RX"}; xa,xb=(x_a,x_b) if outgoing else (x_b,x_a)
        canvas.line(xa,y,xb,y,(45,70,95),2); canvas.line(xb,y,xb+(-8 if outgoing else 8),y-5,(45,70,95),2); canvas.line(xb,y,xb+(-8 if outgoing else 8),y+5,(45,70,95),2)
        _text(canvas,(xa+xb)//2-70,y-15,_sip_label(msg),scale=1,color=(45,55,70),max_chars=24)
    return canvas.png_bytes()


def visual_metadata(kind: str, *, source: dict | None = None, window: dict | None = None,
                    annotations: dict | None = None) -> dict:
    return {
        "renderer_version": RENDERER_VERSION,
        "semantic_renderer_version": SEMANTIC_RENDERER_VERSION,
        "kind": kind,
        "source": source or {},
        "time_window": window or {},
        "semantic_annotations": annotations or {},
    }
