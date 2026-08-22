from __future__ import annotations

import math
import struct
import zlib
from typing import Iterable

from app.contracts.evidence_report import RENDERER_VERSION


# 5x7 deterministic bitmap glyphs. The evidence renderer deliberately avoids
# host fonts so the same Analyzer input produces byte-stable PNGs in CI/Worker.
_FONT = {
    " ": ["00000"] * 7,
    "?": ["01110","10001","00010","00100","00100","00000","00100"],
    "A": ["01110","10001","10001","11111","10001","10001","10001"],
    "B": ["11110","10001","10001","11110","10001","10001","11110"],
    "C": ["01111","10000","10000","10000","10000","10000","01111"],
    "D": ["11110","10001","10001","10001","10001","10001","11110"],
    "E": ["11111","10000","10000","11110","10000","10000","11111"],
    "F": ["11111","10000","10000","11110","10000","10000","10000"],
    "G": ["01111","10000","10000","10111","10001","10001","01111"],
    "H": ["10001","10001","10001","11111","10001","10001","10001"],
    "I": ["11111","00100","00100","00100","00100","00100","11111"],
    "J": ["00111","00010","00010","00010","10010","10010","01100"],
    "K": ["10001","10010","10100","11000","10100","10010","10001"],
    "L": ["10000","10000","10000","10000","10000","10000","11111"],
    "M": ["10001","11011","10101","10101","10001","10001","10001"],
    "N": ["10001","11001","10101","10011","10001","10001","10001"],
    "O": ["01110","10001","10001","10001","10001","10001","01110"],
    "P": ["11110","10001","10001","11110","10000","10000","10000"],
    "Q": ["01110","10001","10001","10001","10101","10010","01101"],
    "R": ["11110","10001","10001","11110","10100","10010","10001"],
    "S": ["01111","10000","10000","01110","00001","00001","11110"],
    "T": ["11111","00100","00100","00100","00100","00100","00100"],
    "U": ["10001","10001","10001","10001","10001","10001","01110"],
    "V": ["10001","10001","10001","10001","10001","01010","00100"],
    "W": ["10001","10001","10001","10101","10101","11011","10001"],
    "X": ["10001","10001","01010","00100","01010","10001","10001"],
    "Y": ["10001","10001","01010","00100","00100","00100","00100"],
    "Z": ["11111","00001","00010","00100","01000","10000","11111"],
    "0": ["01110","10001","10011","10101","11001","10001","01110"],
    "1": ["00100","01100","00100","00100","00100","00100","01110"],
    "2": ["01110","10001","00001","00010","00100","01000","11111"],
    "3": ["11110","00001","00001","01110","00001","00001","11110"],
    "4": ["00010","00110","01010","10010","11111","00010","00010"],
    "5": ["11111","10000","10000","11110","00001","00001","11110"],
    "6": ["01110","10000","10000","11110","10001","10001","01110"],
    "7": ["11111","00001","00010","00100","01000","01000","01000"],
    "8": ["01110","10001","10001","01110","10001","10001","01110"],
    "9": ["01110","10001","10001","01111","00001","00001","01110"],
    ".": ["00000","00000","00000","00000","00000","00110","00110"],
    ":": ["00000","00110","00110","00000","00110","00110","00000"],
    "-": ["00000","00000","00000","11111","00000","00000","00000"],
    "_": ["00000","00000","00000","00000","00000","00000","11111"],
    "/": ["00001","00010","00100","01000","10000","00000","00000"],
    "(": ["00010","00100","01000","01000","01000","00100","00010"],
    ")": ["01000","00100","00010","00010","00010","00100","01000"],
    "+": ["00000","00100","00100","11111","00100","00100","00000"],
    "=": ["00000","11111","00000","11111","00000","00000","00000"],
    ">": ["10000","01000","00100","00010","00100","01000","10000"],
    "<": ["00001","00010","00100","01000","00100","00010","00001"],
    "|": ["00100","00100","00100","00100","00100","00100","00100"],
    "#": ["01010","11111","01010","01010","11111","01010","00000"],
    "%": ["11001","11010","00100","01000","10110","00110","00000"],
    ",": ["00000","00000","00000","00000","00110","00110","00100"],
}


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

    def text_width(self, text: str, scale: int = 1) -> int:
        return max(0, len(str(text)) * 6 * max(1, scale) - max(1, scale))

    def text(self, x: int, y: int, text: str, color=(35,35,35), scale: int = 1, max_width: int | None = None) -> None:
        scale=max(1,int(scale)); cursor=x
        for raw in str(text).upper():
            if max_width is not None and cursor + 5*scale > x + max_width:
                break
            glyph=_FONT.get(raw,_FONT["?"])
            for gy,row in enumerate(glyph):
                for gx,bit in enumerate(row):
                    if bit == "1":
                        self.rect(cursor+gx*scale,y+gy*scale,cursor+(gx+1)*scale-1,y+(gy+1)*scale-1,color,True)
            cursor += 6*scale

    def png_bytes(self) -> bytes:
        raw = bytearray(); stride = self.width * 3
        for y in range(self.height):
            raw.append(0); raw.extend(self.pixels[y*stride:(y+1)*stride])
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data)&0xffffffff)
        sig=b'\x89PNG\r\n\x1a\n'; ihdr=struct.pack('>IIBBBBB',self.width,self.height,8,2,0,0,0)
        return sig+chunk(b'IHDR',ihdr)+chunk(b'IDAT',zlib.compress(bytes(raw),9))+chunk(b'IEND',b'')


def _plot_box(canvas: Canvas) -> tuple[int,int,int,int]:
    left, top, right, bottom = 112, 92, canvas.width-42, canvas.height-92
    canvas.rect(left, top, right, bottom, (255,255,255), True)
    canvas.line(left,bottom,right,bottom,(80,80,80),2); canvas.line(left,top,left,bottom,(80,80,80),2)
    return left,top,right,bottom


def _fmt(value: float) -> str:
    if abs(value) >= 1000: return f"{value:.0f}"
    if abs(value) >= 100: return f"{value:.1f}"
    if abs(value) >= 10: return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _header(canvas: Canvas, title: str, subtitle: str | None = None) -> None:
    canvas.text(28,20,title,scale=2,max_width=canvas.width-56)
    if subtitle:
        canvas.text(30,52,subtitle,scale=1,color=(90,90,90),max_width=canvas.width-60)


def _x_ticks(canvas: Canvas,left:int,right:int,bottom:int,lo:float,hi:float,label:str) -> None:
    span=max(1e-12,hi-lo)
    for frac in (0.0,0.5,1.0):
        x=left+int((right-left)*frac); value=lo+span*frac
        canvas.line(x,bottom,x,bottom+7,(100,100,100),1); canvas.text(max(2,x-18),bottom+12,_fmt(value),scale=1)
    w=canvas.text_width(label,1); canvas.text(max(left,(left+right-w)//2),bottom+43,label,scale=1)


def _y_ticks(canvas: Canvas,left:int,top:int,bottom:int,lo:float,hi:float,label:str) -> None:
    span=max(1e-12,hi-lo)
    for frac in (0.0,0.5,1.0):
        y=bottom-int((bottom-top)*frac); value=lo+span*frac
        canvas.line(left-7,y,left,y,(100,100,100),1); canvas.text(8,max(2,y-4),_fmt(value),scale=1,max_width=92)
    canvas.text(8,top-24,label,scale=1,max_width=100)


def _right_y_ticks(canvas: Canvas,right:int,top:int,bottom:int,lo:float,hi:float,label:str) -> None:
    span=max(1e-12,hi-lo)
    for frac in (0.0,0.5,1.0):
        y=bottom-int((bottom-top)*frac); value=lo+span*frac
        canvas.line(right,y,right+7,y,(115,75,45),1)
        canvas.text(right+10,max(2,y-4),_fmt(value),scale=1,color=(115,75,45),max_width=max(20,canvas.width-right-12))
    canvas.text(max(right-8,canvas.width-112),top-24,label,scale=1,color=(115,75,45),max_width=104)


def render_waveform_png(waveform: dict, *, anomaly_start: float | None = None,
                        anomaly_end: float | None = None, width: int = 1400, height: int = 620,
                        title: str = "WAVEFORM", subtitle: str | None = None) -> bytes:
    canvas=Canvas(width,height); _header(canvas,title,subtitle)
    # Reserve deterministic right-axis room for RMS dBFS as required by SPEC §14.
    left,top,right,bottom=112,92,canvas.width-118,canvas.height-92
    canvas.rect(left,top,right,bottom,(255,255,255),True)
    canvas.line(left,bottom,right,bottom,(80,80,80),2);canvas.line(left,top,left,bottom,(80,80,80),2);canvas.line(right,top,right,bottom,(115,75,45),1)
    bins=waveform.get('bins') or []
    duration=float(waveform.get('duration_seconds') or (bins[-1].get('t') if bins else 1.0) or 1.0)
    if anomaly_start is not None:
        a=max(0.0,min(duration,float(anomaly_start))); b=max(a,min(duration,float(anomaly_end if anomaly_end is not None else anomaly_start)))
        xa=int(left+(right-left)*a/max(duration,1e-9)); xb=int(left+(right-left)*b/max(duration,1e-9))
        if xb <= xa: xb=min(right,xa+4)
        canvas.rect(xa,top,xb,bottom,(245,235,220),True); canvas.line(xa,top,xa,bottom,(180,55,40),2); canvas.line(xb,top,xb,bottom,(180,55,40),2)
        canvas.text(min(right-90,xa+4),top+8,"ANOMALY",color=(160,45,35),scale=1)
    mid=(top+bottom)//2; canvas.line(left,mid,right,mid,(185,185,185))
    max_abs=max(1.0,max((abs(float(x.get('min',0))) for x in bins),default=0.0),max((abs(float(x.get('max',0))) for x in bins),default=0.0))
    rms_points=[]
    for item in bins:
        t=float(item.get('t') or 0.0); x=int(left+(right-left)*t/max(duration,1e-9))
        ymin=int(mid-float(item.get('max',0))/max_abs*(bottom-top)*0.46); ymax=int(mid-float(item.get('min',0))/max_abs*(bottom-top)*0.46)
        canvas.line(x,ymin,x,ymax,(45,85,120))
        if item.get('rms_dbfs') is not None:
            rms=max(-120.0,min(0.0,float(item.get('rms_dbfs')))); y=int(bottom-(bottom-top)*(rms+120.0)/120.0);rms_points.append((x,y))
    for (x0,y0),(x1,y1) in zip(rms_points,rms_points[1:]):canvas.line(x0,y0,x1,y1,(155,95,45),2)
    canvas.text(left+8,top+8,"AMPLITUDE",color=(45,85,120),scale=1)
    canvas.text(left+90,top+8,"RMS DBFS",color=(155,95,45),scale=1)
    _x_ticks(canvas,left,right,bottom,0.0,duration,"TIME (S)"); _y_ticks(canvas,left,top,bottom,-max_abs,max_abs,"AMPLITUDE (PCM)")
    _right_y_ticks(canvas,right,top,bottom,-120.0,0.0,"RMS DBFS")
    return canvas.png_bytes()


def _heat_color(value: float, lo: float, hi: float) -> tuple[int,int,int]:
    if not math.isfinite(value): value=lo
    t=0.0 if hi<=lo else max(0.0,min(1.0,(value-lo)/(hi-lo)))
    return (int(245-180*t), int(245-120*t), int(245-40*t))


def _draw_relative_db_scale(canvas:Canvas,left:int,right:int,top:int,lo:float,hi:float)->None:
    bar_left=max(left,right-270);bar_right=right;bar_top=max(64,top-26);bar_bottom=bar_top+10
    width=max(1,bar_right-bar_left)
    for px in range(width+1):
        value=lo+(hi-lo)*px/width
        canvas.line(bar_left+px,bar_top,bar_left+px,bar_bottom,_heat_color(value,lo,hi),1)
    canvas.rect(bar_left,bar_top,bar_right,bar_bottom,(90,90,90),False)
    canvas.text(bar_left,bar_bottom+4,_fmt(lo),scale=1,color=(90,90,90),max_width=54)
    canvas.text(max(bar_left,bar_right-48),bar_bottom+4,_fmt(hi),scale=1,color=(90,90,90),max_width=48)
    canvas.text(max(bar_left,bar_left+80),bar_bottom+4,"REL DB",scale=1,color=(90,90,90),max_width=70)


def render_spectrogram_png(spec: dict, *, anomaly_start: float | None = None,
                           anomaly_end: float | None = None, width: int = 1400, height: int = 720,
                           title: str = "SPECTROGRAM", subtitle: str | None = None) -> bytes:
    canvas=Canvas(width,height); _header(canvas,title,subtitle); left,top,right,bottom=_plot_box(canvas)
    times=spec.get('times') or []; freqs=spec.get('frequencies') or []; matrix=spec.get('db') or []
    flat=[float(v) for row in matrix for v in row if isinstance(v,(int,float))]
    lo=min(flat) if flat else -120.0; hi=max(flat) if flat else 0.0
    if matrix and times and freqs:
        nt=len(matrix); nf=max(len(row) for row in matrix)
        for ti,row in enumerate(matrix):
            x0=left+int((right-left)*ti/max(1,nt)); x1=left+int((right-left)*(ti+1)/max(1,nt))
            for fi,v in enumerate(row):
                y1=bottom-int((bottom-top)*fi/max(1,nf)); y0=bottom-int((bottom-top)*(fi+1)/max(1,nf))
                canvas.rect(x0,y0,max(x0,x1),max(y0,y1),_heat_color(float(v),lo,hi),True)
        duration=float(times[-1]) if times else 1.0
        if anomaly_start is not None and duration>0:
            a=max(0.0,min(duration,float(anomaly_start))); b=max(a,min(duration,float(anomaly_end if anomaly_end is not None else a)))
            xa=int(left+(right-left)*a/duration); xb=int(left+(right-left)*b/duration)
            if xb <= xa: xb=min(right,xa+4)
            canvas.line(xa,top,xa,bottom,(180,35,35),2); canvas.line(xb,top,xb,bottom,(180,35,35),2); canvas.text(min(right-90,xa+4),top+8,"ANOMALY",(160,45,35),1)
        maxf=float(freqs[-1]) if freqs else 0.0
        for f in (50.0,60.0,100.0,120.0,150.0,180.0,250.0,350.0,450.0,550.0,650.0):
            if maxf and f<=maxf:
                y=bottom-int((bottom-top)*f/maxf); canvas.line(left,y,right,y,(155,155,155),1)
        _x_ticks(canvas,left,right,bottom,0.0,duration,"TIME (S)"); _y_ticks(canvas,left,top,bottom,0.0,maxf or 1.0,"FREQUENCY (HZ)")
        _draw_relative_db_scale(canvas,left,right,top,lo,hi)
    else:
        canvas.text(left+20,top+20,"NO SPECTROGRAM DATA",scale=2,color=(130,130,130))
    return canvas.png_bytes()


def render_spectrum_png(spectral: dict, *, width: int = 1400, height: int = 620,
                        title: str = "SPECTRUM", subtitle: str | None = None,
                        reference_frequencies_hz: Iterable[float] | None = None) -> bytes:
    canvas=Canvas(width,height); _header(canvas,title,subtitle); left,top,right,bottom=_plot_box(canvas)
    peaks=list(spectral.get('peaks') or [])
    markers=list(reference_frequencies_hz or (50,60,100,120,150,180,250,350,450,550,650))
    maxf=max([float(p.get('frequency_hz') or 0) for p in peaks] + [float(x) for x in markers] + [1000.0])
    use_db=any(p.get('magnitude_db') is not None or p.get('db') is not None for p in peaks)
    values=[float(p.get('magnitude_db') if p.get('magnitude_db') is not None else p.get('db')) for p in peaks if p.get('magnitude_db') is not None or p.get('db') is not None] if use_db else [float(p.get('energy_ratio') or 0) for p in peaks]
    lo=min(values) if values else (-120.0 if use_db else 0.0); hi=max(values) if values else (0.0 if use_db else 1.0)
    if hi <= lo: hi=lo+1.0
    for f in markers:
        f=float(f)
        if 0 <= f <= maxf:
            x=int(left+(right-left)*f/maxf); canvas.line(x,top,x,bottom,(215,205,190),1)
            if f in {50,60,150,250,350,450,550,650}: canvas.text(max(left,x-12),bottom-16,f"{int(f)}",scale=1,color=(125,110,95))
    for p in peaks:
        f=float(p.get('frequency_hz') or 0)
        v=float((p.get('magnitude_db') if p.get('magnitude_db') is not None else p.get('db')) if use_db else (p.get('energy_ratio') or 0))
        x=int(left+(right-left)*f/maxf); y=int(bottom-(bottom-top)*(v-lo)/(hi-lo))
        canvas.line(x,bottom,x,y,(35,70,110),5)
    for p in sorted(peaks,key=lambda x:float(x.get('energy_ratio') or 0),reverse=True)[:6]:
        f=float(p.get('frequency_hz') or 0); x=int(left+(right-left)*f/maxf); canvas.text(max(left,min(right-45,x-15)),top+10,f"{_fmt(f)}HZ",scale=1,color=(45,70,105))
    _x_ticks(canvas,left,right,bottom,0.0,maxf,"FREQUENCY (HZ)"); _y_ticks(canvas,left,top,bottom,lo,hi,"MAGNITUDE (DB)" if use_db else "ENERGY RATIO")
    return canvas.png_bytes()


def _rtp_event_label(event:dict,base_time:float)->str:
    details=event.get('details') or {}
    parts=[str(event.get('type') or 'EVENT')]
    try:parts.append(f"T+{float(event.get('start_time') or base_time)-base_time:.3f}S")
    except (TypeError,ValueError):pass
    prev_frame=details.get('previous_frame_number') or details.get('previous_frame')
    curr_frame=details.get('current_frame_number') or details.get('next_frame_number') or details.get('current_frame')
    if prev_frame is not None or curr_frame is not None:parts.append(f"F{prev_frame or '?'}>{curr_frame or '?'}")
    prev_seq=details.get('previous_sequence') or details.get('previous_sequence_ext')
    curr_seq=details.get('current_sequence') or details.get('next_sequence_ext')
    if prev_seq is not None or curr_seq is not None:parts.append(f"SEQ {prev_seq if prev_seq is not None else '?'}>{curr_seq if curr_seq is not None else '?'}")
    lost=details.get('lost_packets')
    if lost is not None:parts.append(f"LOST {lost}")
    jitter=details.get('jitter_ms') or details.get('p95_jitter_ms')
    if jitter is not None:parts.append(f"JIT {_fmt(float(jitter))}MS")
    delta=details.get('delta_ms')
    if delta is not None:parts.append(f"DELTA {_fmt(float(delta))}MS")
    if str(event.get('type') or '')=='PAYLOAD_CHANGE':
        pts=details.get('payload_types') or []
        if pts:parts.append("PT "+">".join(str(x) for x in pts[:4]))
    return " | ".join(parts)


def render_rtp_timeline_png(streams: list[dict], *, width: int = 1400, height: int = 720,
                            title: str = "RTP TIMELINE", subtitle: str | None = None) -> bytes:
    canvas=Canvas(width,height); _header(canvas,title,subtitle); left,top,right,bottom=_plot_box(canvas)
    if not streams:
        canvas.text(left+20,top+20,"NO RTP STREAM DATA",scale=2,color=(130,130,130)); return canvas.png_bytes()
    starts=[float(s.get('start_time') or 0) for s in streams]; ends=[float(s.get('end_time') or s.get('start_time') or 0) for s in streams]
    lo=min(starts); hi=max(ends); span=max(1e-9,hi-lo); lane=max(38,(bottom-top)//max(1,len(streams)+1))
    for idx,s in enumerate(streams):
        y=top+(idx+1)*lane
        x0=left+int((right-left)*(float(s.get('start_time') or lo)-lo)/span); x1=left+int((right-left)*(float(s.get('end_time') or hi)-lo)/span)
        canvas.line(x0,y,x1,y,(35,85,115),5)
        label=f"RTP{idx+1} {s.get('src_ip')}:{s.get('src_port')}>{s.get('dst_ip')}:{s.get('dst_port')}"
        canvas.text(left+6,max(top,y-20),label,scale=1,color=(45,70,95),max_width=right-left-12)
        for ev_index,ev in enumerate((s.get('events') or [])[:12]):
            t=float(ev.get('start_time') or lo); x=left+int((right-left)*(t-lo)/span)
            canvas.line(x,y-10,x,y+10,(180,45,45),3)
            label_y=max(top,min(bottom-12,y+12+(ev_index%2)*12))
            canvas.text(max(left,min(right-330,x+4)),label_y,_rtp_event_label(ev,lo),scale=1,color=(150,45,45),max_width=326)
    _x_ticks(canvas,left,right,bottom,0.0,span,"TIME FROM FIRST RTP (S)"); canvas.text(8,top-24,"RTP STREAM / EVENT",scale=1,max_width=100)
    return canvas.png_bytes()


def _sip_message_label(msg: dict) -> str:
    frame=msg.get('frame_number') or msg.get('frame')
    method=msg.get('method') or msg.get('request_method')
    status=msg.get('status_code') or msg.get('response_code') or msg.get('status')
    phrase=msg.get('reason') or msg.get('reason_phrase')
    cseq=msg.get('cseq') or msg.get('cseq_number')
    cseq_method=msg.get('cseq_method')
    core=str(method or (f"{status} {phrase or ''}".strip() if status else msg.get('label') or msg.get('message') or 'SIP'))
    if cseq is not None:core+=f" | CSEQ {cseq}{' '+str(cseq_method) if cseq_method else ''}"
    return f"F{frame} {core}" if frame is not None else core


def _sip_messages(calls:list[dict])->list[dict]:
    messages=[]
    for call in calls:
        rows=call.get('ladder') or call.get('messages') or call.get('sip_messages') or []
        messages.extend(rows)
    return messages[:24]


def _sip_endpoint(msg:dict,key:str)->str|None:
    if key=='src':return msg.get('src') or msg.get('src_ip')
    return msg.get('dst') or msg.get('dst_ip')


def render_sip_call_flow_png(calls: list[dict], *, width: int = 1400, height: int = 720,
                             title: str = "SIP CALL FLOW", subtitle: str | None = None) -> bytes:
    canvas=Canvas(width,height); _header(canvas,title,subtitle); left,top,right,bottom=_plot_box(canvas)
    messages=_sip_messages(calls)
    invite=next((m for m in messages if str(m.get('method') or '').upper()=='INVITE'),None)
    endpoints=[]
    for msg in messages:
        for key in ('src','dst'):
            value=_sip_endpoint(msg,key)
            if value and value not in endpoints:endpoints.append(value)
    if invite:
        caller_endpoint=_sip_endpoint(invite,'src');callee_endpoint=_sip_endpoint(invite,'dst')
    else:
        caller_endpoint=endpoints[0] if endpoints else None;callee_endpoint=endpoints[1] if len(endpoints)>1 else None
    endpoint_a=caller_endpoint or 'ENDPOINT A';endpoint_b=callee_endpoint or 'ENDPOINT B'
    x_a=left+210; x_b=right-210
    canvas.text(max(left,x_a-95),top+5,f"CALLER {endpoint_a}",scale=1,max_width=190)
    canvas.text(max(left,x_b-95),top+5,f"CALLEE {endpoint_b}",scale=1,max_width=190)
    first_call=calls[0] if calls else {}
    call_id=first_call.get('call_id') or first_call.get('sip_call_id')
    if call_id:canvas.text(max(left,right-390),top+22,f"CALL-ID {call_id}",scale=1,color=(90,90,90),max_width=380)
    canvas.line(x_a,top+38,x_a,bottom-20,(80,80,80),2); canvas.line(x_b,top+38,x_b,bottom-20,(80,80,80),2)
    if not messages:
        canvas.text(left+20,top+55,"NO SIP LADDER IN CALL RESULT",scale=2,color=(130,130,130),max_width=right-left-40)
    step=max(20,(bottom-top-90)//max(1,len(messages)))
    for i,msg in enumerate(messages):
        y=top+60+i*step; src=_sip_endpoint(msg,'src'); outgoing=True if not src else src==endpoint_a
        xa,xb=(x_a,x_b) if outgoing else (x_b,x_a); canvas.line(xa,y,xb,y,(45,70,95),2)
        canvas.line(xb,y,xb+(-8 if outgoing else 8),y-5,(45,70,95),2); canvas.line(xb,y,xb+(-8 if outgoing else 8),y+5,(45,70,95),2)
        canvas.text(min(xa,xb)+12,max(top,y-11),_sip_message_label(msg),scale=1,color=(45,60,80),max_width=abs(xb-xa)-24)
    canvas.text(8,top-24,"MESSAGE ORDER",scale=1,max_width=100); canvas.text((left+right)//2-45,bottom+43,"SIP ROLE / ENDPOINT",scale=1)
    return canvas.png_bytes()


def visual_metadata(kind: str, *, source: dict | None = None, window: dict | None = None,
                    title: str | None = None, x_axis: str | None = None, y_axis: str | None = None,
                    units: dict | None = None, legend: list[str] | None = None,
                    finding_ids: list[str] | None = None, call_id: str | None = None,
                    direction: str | None = None, anomaly_window: dict | None = None,
                    caption: str | None = None) -> dict:
    required_axes=kind in {"WAVEFORM","SPECTRUM","SPECTROGRAM","RTP_TIMELINE"}
    annotation_complete=bool(title and (not required_axes or (x_axis and y_axis)))
    return {
        "renderer_version": RENDERER_VERSION,
        "kind": kind,
        "source": source or {},
        "time_window": window or {},
        "anomaly_window": anomaly_window or {},
        "finding_ids": finding_ids or [],
        "call_id": call_id,
        "direction": direction,
        "annotation_complete": annotation_complete,
        "annotation_contract": {
            "title": title,
            "x_axis": x_axis,
            "y_axis": y_axis,
            "units": units or {},
            "legend": legend or [],
            "caption": caption or title,
            "anomaly_marker": "ANOMALY" if anomaly_window else None,
        },
    }
