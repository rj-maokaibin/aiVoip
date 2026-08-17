from __future__ import annotations

import math
import struct
import zlib
from typing import Iterable

from app.contracts.evidence_report import RENDERER_VERSION


# Minimal deterministic RGB PNG renderer.  It deliberately avoids optional UI
# dependencies so Worker/CI images are byte-stable for identical inputs.
class Canvas:
    def __init__(self, width: int = 1400, height: int = 720, background=(250, 250, 250)):
        self.width = max(64, int(width)); self.height = max(64, int(height))
        self.pixels = bytearray(background * (self.width * self.height))

    def pixel(self, x: int, y: int, color=(20, 20, 20)) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            i = (y * self.width + x) * 3
            self.pixels[i:i+3] = bytes(color)

    def line(self, x0: int, y0: int, x1: int, y1: int, color=(35, 35, 35), thickness: int = 1) -> None:
        dx = abs(x1-x0); sx = 1 if x0 < x1 else -1
        dy = -abs(y1-y0); sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            for ox in range(-(thickness//2), thickness//2+1):
                for oy in range(-(thickness//2), thickness//2+1):
                    self.pixel(x0+ox, y0+oy, color)
            if x0 == x1 and y0 == y1: break
            e2 = 2 * err
            if e2 >= dy: err += dy; x0 += sx
            if e2 <= dx: err += dx; y0 += sy

    def rect(self, x0: int, y0: int, x1: int, y1: int, color=(230, 230, 230), fill=True) -> None:
        xa, xb = sorted((max(0,x0), min(self.width-1,x1)))
        ya, yb = sorted((max(0,y0), min(self.height-1,y1)))
        if fill:
            for y in range(ya, yb+1):
                start=(y*self.width+xa)*3; end=(y*self.width+xb+1)*3
                self.pixels[start:end] = bytes(color) * (xb-xa+1)
        else:
            self.line(xa,ya,xb,ya,color); self.line(xb,ya,xb,yb,color)
            self.line(xb,yb,xa,yb,color); self.line(xa,yb,xa,ya,color)

    def png_bytes(self) -> bytes:
        raw = bytearray()
        stride = self.width * 3
        for y in range(self.height):
            raw.append(0)
            raw.extend(self.pixels[y*stride:(y+1)*stride])
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data)&0xffffffff)
        sig=b'\x89PNG\r\n\x1a\n'
        ihdr=struct.pack('>IIBBBBB',self.width,self.height,8,2,0,0,0)
        return sig+chunk(b'IHDR',ihdr)+chunk(b'IDAT',zlib.compress(bytes(raw),9))+chunk(b'IEND',b'')


def _plot_box(canvas: Canvas) -> tuple[int,int,int,int]:
    left, top, right, bottom = 72, 44, canvas.width-36, canvas.height-64
    canvas.rect(left, top, right, bottom, (255,255,255), True)
    canvas.line(left,bottom,right,bottom,(80,80,80),2)
    canvas.line(left,top,left,bottom,(80,80,80),2)
    return left,top,right,bottom


def render_waveform_png(waveform: dict, *, anomaly_start: float | None = None,
                        anomaly_end: float | None = None, width: int = 1400, height: int = 620) -> bytes:
    canvas=Canvas(width,height); left,top,right,bottom=_plot_box(canvas)
    bins=waveform.get('bins') or []
    duration=float(waveform.get('duration_seconds') or (bins[-1].get('t') if bins else 1.0) or 1.0)
    if anomaly_start is not None:
        a=max(0.0,min(duration,float(anomaly_start))); b=max(a,min(duration,float(anomaly_end if anomaly_end is not None else anomaly_start)))
        xa=int(left+(right-left)*a/max(duration,1e-9)); xb=int(left+(right-left)*b/max(duration,1e-9))
        canvas.rect(xa,top,xb,bottom,(245,235,220),True)
    mid=(top+bottom)//2
    canvas.line(left,mid,right,mid,(185,185,185))
    if bins:
        max_abs=max(1.0,max(abs(float(x.get('min',0))) for x in bins),max(abs(float(x.get('max',0))) for x in bins))
        for i,item in enumerate(bins):
            t=float(item.get('t') or 0.0); x=int(left+(right-left)*t/max(duration,1e-9))
            ymin=int(mid-float(item.get('max',0))/max_abs*(bottom-top)*0.46)
            ymax=int(mid-float(item.get('min',0))/max_abs*(bottom-top)*0.46)
            canvas.line(x,ymin,x,ymax,(45,85,120))
    return canvas.png_bytes()


def _heat_color(value: float, lo: float, hi: float) -> tuple[int,int,int]:
    if not math.isfinite(value): value=lo
    t=0.0 if hi<=lo else max(0.0,min(1.0,(value-lo)/(hi-lo)))
    # Deterministic neutral-to-dark heat ramp; exact palette is not diagnostic evidence.
    return (int(245-180*t), int(245-120*t), int(245-40*t))


def render_spectrogram_png(spec: dict, *, anomaly_start: float | None = None,
                           anomaly_end: float | None = None, width: int = 1400, height: int = 720) -> bytes:
    canvas=Canvas(width,height); left,top,right,bottom=_plot_box(canvas)
    times=spec.get('times') or []; freqs=spec.get('frequencies') or []; matrix=spec.get('db') or []
    flat=[float(v) for row in matrix for v in row if isinstance(v,(int,float))]
    lo=min(flat) if flat else -120.0; hi=max(flat) if flat else 0.0
    if matrix and times and freqs:
        nt=len(matrix); nf=max(len(row) for row in matrix)
        for ti,row in enumerate(matrix):
            x0=left+int((right-left)*ti/max(1,nt)); x1=left+int((right-left)*(ti+1)/max(1,nt))
            for fi,v in enumerate(row):
                # Low frequency at bottom.
                y1=bottom-int((bottom-top)*fi/max(1,nf)); y0=bottom-int((bottom-top)*(fi+1)/max(1,nf))
                canvas.rect(x0,y0,max(x0,x1),max(y0,y1),_heat_color(float(v),lo,hi),True)
        duration=float(times[-1]) if times else 1.0
        if anomaly_start is not None and duration>0:
            a=max(0.0,min(duration,float(anomaly_start))); b=max(a,min(duration,float(anomaly_end if anomaly_end is not None else a)))
            xa=int(left+(right-left)*a/duration); xb=int(left+(right-left)*b/duration)
            canvas.line(xa,top,xa,bottom,(180,35,35),2); canvas.line(xb,top,xb,bottom,(180,35,35),2)
        # Mark common mains-family reference frequencies when in range.
        maxf=float(freqs[-1]) if freqs else 0.0
        for f in (50.0,60.0,100.0,120.0,150.0,180.0):
            if maxf and f<=maxf:
                y=bottom-int((bottom-top)*f/maxf); canvas.line(left,y,right,y,(120,120,120),1)
    return canvas.png_bytes()


def render_spectrum_png(spectral: dict, *, width: int = 1400, height: int = 620) -> bytes:
    canvas=Canvas(width,height); left,top,right,bottom=_plot_box(canvas)
    peaks=list(spectral.get('peaks') or [])
    if peaks:
        maxf=max(float(p.get('frequency_hz') or 0) for p in peaks) or 1.0
        maxr=max(float(p.get('energy_ratio') or 0) for p in peaks) or 1.0
        for p in peaks:
            f=float(p.get('frequency_hz') or 0); r=float(p.get('energy_ratio') or 0)
            x=int(left+(right-left)*f/maxf); y=int(bottom-(bottom-top)*r/maxr)
            canvas.line(x,bottom,x,y,(35,70,110),5)
    return canvas.png_bytes()


def render_rtp_timeline_png(streams: list[dict], *, width: int = 1400, height: int = 720) -> bytes:
    canvas=Canvas(width,height); left,top,right,bottom=_plot_box(canvas)
    if not streams: return canvas.png_bytes()
    starts=[float(s.get('start_time') or 0) for s in streams]; ends=[float(s.get('end_time') or s.get('start_time') or 0) for s in streams]
    lo=min(starts); hi=max(ends); span=max(1e-9,hi-lo)
    lane=max(18,(bottom-top)//max(1,len(streams)+1))
    for idx,s in enumerate(streams):
        y=top+(idx+1)*lane
        x0=left+int((right-left)*(float(s.get('start_time') or lo)-lo)/span)
        x1=left+int((right-left)*(float(s.get('end_time') or hi)-lo)/span)
        canvas.line(x0,y,x1,y,(35,85,115),5)
        for ev in s.get('events',[]) or []:
            t=float(ev.get('start_time') or lo); x=left+int((right-left)*(t-lo)/span)
            canvas.line(x,y-9,x,y+9,(180,45,45),3)
    return canvas.png_bytes()


def render_sip_call_flow_png(calls: list[dict], *, width: int = 1400, height: int = 720) -> bytes:
    canvas=Canvas(width,height); left,top,right,bottom=_plot_box(canvas)
    x_a=left+180; x_b=right-180
    canvas.line(x_a,top+30,x_a,bottom-20,(80,80,80),2); canvas.line(x_b,top+30,x_b,bottom-20,(80,80,80),2)
    messages=[]
    for call in calls:
        for msg in call.get('messages',[]) or call.get('sip_messages',[]) or []:
            messages.append(msg)
    messages=messages[:24]
    step=max(18,(bottom-top-80)//max(1,len(messages)))
    for i,msg in enumerate(messages):
        y=top+60+i*step
        outgoing=str(msg.get('direction') or '').upper() not in {'IN','INBOUND','RX'}
        xa,xb=(x_a,x_b) if outgoing else (x_b,x_a)
        canvas.line(xa,y,xb,y,(45,70,95),2)
        # arrow head
        canvas.line(xb,y,xb+(-8 if outgoing else 8),y-5,(45,70,95),2)
        canvas.line(xb,y,xb+(-8 if outgoing else 8),y+5,(45,70,95),2)
    return canvas.png_bytes()


def visual_metadata(kind: str, *, source: dict | None = None, window: dict | None = None) -> dict:
    return {"renderer_version": RENDERER_VERSION, "kind": kind, "source": source or {}, "time_window": window or {}}
