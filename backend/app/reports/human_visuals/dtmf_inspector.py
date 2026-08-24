from __future__ import annotations

import io
import math
import wave
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from app.analyzers.pcm.signal import DTMF_COLS, DTMF_GRID
from .theme import COLORS
from .typography import human_cjk_font_available, human_font_properties, localized_text


_DIGIT_TO_TONES = {
    digit: (float(row), float(col))
    for row, digits in DTMF_GRID.items()
    for col, digit in zip(DTMF_COLS.keys(), digits)
}


def _read_pcm16(wav_bytes: bytes) -> tuple[np.ndarray, int, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels=int(wf.getnchannels());sample_width=int(wf.getsampwidth());sample_rate=int(wf.getframerate())
        raw=wf.readframes(wf.getnframes())
    if sample_width!=2:raise ValueError(f"DTMF_INSPECTOR_UNSUPPORTED_SAMPLE_WIDTH:{sample_width}")
    samples=np.frombuffer(raw,dtype="<i2").astype(np.float64)
    if channels>1:samples=samples.reshape(-1,channels).mean(axis=1)
    return samples,sample_rate,channels


def _parabolic_peak(freqs:np.ndarray,levels:np.ndarray,index:int)->tuple[float,float]:
    if index<=0 or index>=len(levels)-1:return float(freqs[index]),float(levels[index])
    y0,y1,y2=(float(levels[index-1]),float(levels[index]),float(levels[index+1]))
    denom=y0-2.0*y1+y2
    delta=0.0 if abs(denom)<1e-12 else 0.5*(y0-y2)/denom
    delta=max(-1.0,min(1.0,delta));step=float(freqs[1]-freqs[0]) if len(freqs)>1 else 0.0
    peak_hz=float(freqs[index])+delta*step
    peak_db=y1-0.25*(y0-y2)*delta
    return peak_hz,peak_db


def _peak_near(freqs:np.ndarray,levels:np.ndarray,expected_hz:float,search_hz:float=45.0)->tuple[float,float]:
    mask=(freqs>=expected_hz-search_hz)&(freqs<=expected_hz+search_hz)
    indexes=np.flatnonzero(mask)
    if not indexes.size:return expected_hz,-120.0
    index=int(indexes[int(np.argmax(levels[indexes]))])
    return _parabolic_peak(freqs,levels,index)


def measure_dtmf_event(wav_bytes:bytes,event:dict,*,min_hz:float=500.0,max_hz:float=1800.0)->dict:
    digit=str(event.get("digit") or "")
    if digit not in _DIGIT_TO_TONES:return {"status":"UNAVAILABLE","reason":"DTMF_DIGIT_UNKNOWN","digit":digit}
    samples,sample_rate,channels=_read_pcm16(wav_bytes)
    duration=samples.size/max(1,sample_rate)
    start=max(0.0,min(duration,float(event.get("start_seconds") or 0.0)))
    end=max(start,min(duration,float(event.get("end_seconds") or start)))
    i0=max(0,int(round(start*sample_rate)));i1=min(samples.size,int(round(end*sample_rate)))
    x=samples[i0:i1].astype(np.float64,copy=False)
    if x.size<32:return {"status":"UNAVAILABLE","reason":"DTMF_EVENT_TOO_SHORT","digit":digit,"sample_rate":sample_rate}
    x=x-float(np.mean(x));window=np.hanning(x.size);coherent=float(np.sum(window))
    nfft=max(4096,1<<int(math.ceil(math.log2(max(256,x.size*8)))))
    nfft=min(65536,nfft)
    spectrum=np.fft.rfft(x*window,n=nfft)
    amplitude=2.0*np.abs(spectrum)/max(coherent,1e-12)
    levels=20.0*np.log10(np.maximum(amplitude/32768.0,1e-12));levels=np.maximum(levels,-140.0)
    freqs=np.fft.rfftfreq(nfft,d=1.0/sample_rate)
    low_expected,high_expected=_DIGIT_TO_TONES[digit]
    low_hz,low_dbfs=_peak_near(freqs,levels,low_expected)
    high_hz,high_dbfs=_peak_near(freqs,levels,high_expected)
    resolution=float(sample_rate/nfft);guard=max(30.0,resolution*4.0)
    valid=(freqs>=min_hz)&(freqs<=min(max_hz,sample_rate/2.0))
    valid&=~((freqs>=low_hz-guard)&(freqs<=low_hz+guard))
    valid&=~((freqs>=high_hz-guard)&(freqs<=high_hz+guard))
    spur_indexes=np.flatnonzero(valid)
    if spur_indexes.size:
        spur_index=int(spur_indexes[int(np.argmax(levels[spur_indexes]))]);spur_hz,spur_dbfs=_parabolic_peak(freqs,levels,spur_index)
        spur_margin=min(low_dbfs,high_dbfs)-spur_dbfs
    else:
        spur_hz=spur_dbfs=spur_margin=None
    measurement={
        "status":"MEASURED","threshold_status":"UNVERIFIED_THRESHOLD","digit":digit,
        "start_seconds":round(start,6),"end_seconds":round(end,6),
        "duration_ms":round((end-start)*1000.0,3),"detector_confidence":event.get("confidence"),
        "row_expected_hz":low_expected,"row_measured_hz":round(low_hz,3),"row_error_hz":round(low_hz-low_expected,3),
        "row_error_percent":round((low_hz-low_expected)/low_expected*100.0,5),"row_level_dbfs":round(low_dbfs,3),
        "col_expected_hz":high_expected,"col_measured_hz":round(high_hz,3),"col_error_hz":round(high_hz-high_expected,3),
        "col_error_percent":round((high_hz-high_expected)/high_expected*100.0,5),"col_level_dbfs":round(high_dbfs,3),
        "twist_db":round(low_dbfs-high_dbfs,3),
        "strongest_spur_hz":round(spur_hz,3) if spur_hz is not None else None,
        "strongest_spur_dbfs":round(spur_dbfs,3) if spur_dbfs is not None else None,
        "spur_margin_db":round(spur_margin,3) if spur_margin is not None else None,
        "spur_margin_definition":"weaker DTMF primary tone level minus strongest non-primary spectral peak",
        "sample_rate":sample_rate,"channels":channels,"fft_size":nfft,"window":"HANN",
        "frequency_resolution_hz":round(resolution,6),"level_unit":"dBFS",
        "measurement_method":"DTMF_EVENT_RFFT_HANN_FINE_PEAK_V1",
        "authority":"PRESENTATION_MEASUREMENT_ONLY",
        "boundary":"Frequency error and spur margin are measured facts only unless a versioned AnalyzerProfile/Golden threshold explicitly governs PASS/FAIL.",
    }
    measurement["_plot_frequency_hz"]=freqs.tolist();measurement["_plot_level_dbfs"]=levels.tolist()
    return measurement


def _fmt(value:Any,unit:str="")->str:
    if value is None:return "UNAVAILABLE"
    if isinstance(value,float):text=f"{value:.3f}".rstrip("0").rstrip(".")
    else:text=str(value)
    return f"{text}{(' '+unit) if unit else ''}"


def render_human_dtmf_inspector_png(wav_bytes:bytes,event:dict,*,sip_target:str|None=None,pcm_sequence:str|None=None,
                                    title:str|None=None,width_px:int=1800,height_px:int=900)->tuple[bytes,dict]:
    measurement=measure_dtmf_event(wav_bytes,event)
    if measurement.get("status")!="MEASURED":raise ValueError(str(measurement.get("reason") or "DTMF_MEASUREMENT_UNAVAILABLE"))
    freqs=np.asarray(measurement.pop("_plot_frequency_hz"),dtype=float);levels=np.asarray(measurement.pop("_plot_level_dbfs"),dtype=float)
    digit=measurement["digit"];mask=(freqs>=500.0)&(freqs<=min(1800.0,measurement["sample_rate"]/2.0))
    fig=plt.figure(figsize=(width_px/160.0,height_px/160.0),constrained_layout=True);fig.patch.set_facecolor(COLORS["background"])
    gs=fig.add_gridspec(1,2,width_ratios=[1.55,1.15]);ax=fig.add_subplot(gs[0,0]);info=fig.add_subplot(gs[0,1])
    ax.set_facecolor(COLORS["panel"]);info.set_facecolor(COLORS["panel"])
    ax.plot(freqs[mask],levels[mask],color=COLORS["spectrum"],linewidth=1.0)
    floor=max(-120.0,float(np.nanpercentile(levels[mask],2))-5.0);ceiling=min(0.0,float(np.nanmax(levels[mask]))+6.0)
    ax.set_ylim(floor,ceiling);ax.set_xlim(500,1800);ax.grid(True,alpha=.3)
    for key,label in (("row_expected_hz","Low expected"),("col_expected_hz","High expected")):
        ax.axvline(measurement[key],color=COLORS["reference"],linewidth=1.0,alpha=.65,linestyle="--")
    for hz_key,db_key in (("row_measured_hz","row_level_dbfs"),("col_measured_hz","col_level_dbfs")):
        ax.scatter([measurement[hz_key]],[measurement[db_key]],s=38,color=COLORS["spectrum"],zorder=4)
        ax.annotate(f"{measurement[hz_key]:.1f} Hz",(measurement[hz_key],measurement[db_key]),xytext=(5,8),textcoords="offset points",fontsize=9)
    if measurement.get("strongest_spur_hz") is not None:
        ax.scatter([measurement["strongest_spur_hz"]],[measurement["strongest_spur_dbfs"]],s=28,color=COLORS["anomaly"],zorder=4)
        ax.annotate(localized_text(f"最强杂散 {measurement['strongest_spur_hz']:.1f} Hz",f"Spur {measurement['strongest_spur_hz']:.1f} Hz"),(measurement["strongest_spur_hz"],measurement["strongest_spur_dbfs"]),xytext=(5,-16),textcoords="offset points",fontproperties=human_font_properties(size=8.5))
    ax.set_xlabel(localized_text("频率（Hz）","Frequency (Hz)"),fontproperties=human_font_properties(size=10));ax.set_ylabel(localized_text("频谱电平（dBFS）","Spectrum level (dBFS)"),fontproperties=human_font_properties(size=10))
    ax.set_title(localized_text(title or f"DTMF {digit} · 频谱分析",title or f"DTMF {digit} · Spectrum"),loc="left",fontproperties=human_font_properties(size=15,weight="semibold"),pad=18)
    info.axis("off")
    match="UNAVAILABLE"
    if sip_target and pcm_sequence:match="MATCH" if str(sip_target)==str(pcm_sequence) else "MISMATCH"
    measurement["pcm_sequence"]=pcm_sequence;measurement["sip_target"]=sip_target;measurement["sequence_match"]=match
    threshold_display=localized_text("已测量 / 阈值未冻结","MEASURED / UNVERIFIED_THRESHOLD")
    rows=[
        ("按键 / Digit",digit),("时间窗",f"{measurement['start_seconds']:.3f}–{measurement['end_seconds']:.3f} s"),("持续时间",_fmt(measurement["duration_ms"],"ms")),
        ("低频：期望 / 实测",f"{measurement['row_expected_hz']:.0f} / {measurement['row_measured_hz']:.3f} Hz"),("低频频偏",f"{measurement['row_error_percent']:+.4f}%"),("低频电平",_fmt(measurement["row_level_dbfs"],"dBFS")),
        ("高频：期望 / 实测",f"{measurement['col_expected_hz']:.0f} / {measurement['col_measured_hz']:.3f} Hz"),("高频频偏",f"{measurement['col_error_percent']:+.4f}%"),("高频电平",_fmt(measurement["col_level_dbfs"],"dBFS")),
        ("Twist",_fmt(measurement["twist_db"],"dB")),("最强杂散",f"{_fmt(measurement.get('strongest_spur_hz'),'Hz')} / {_fmt(measurement.get('strongest_spur_dbfs'),'dBFS')}"),("Spur Margin",_fmt(measurement.get("spur_margin_db"),"dB")),
        ("PCM 序列",pcm_sequence or "UNAVAILABLE"),("SIP 目标",sip_target or "UNAVAILABLE"),("序列对比",match),("阈值状态",threshold_display),
    ]
    y=.965
    for label,value in rows:
        info.text(.02,y,f"{label}",fontproperties=human_font_properties(size=8.8,weight="semibold"),va="top",color=COLORS["muted"])
        info.text(.48,y,str(value),fontproperties=human_font_properties(size=9.1),va="top",color=COLORS["text"]);y-=.050
    info.axhline(.135,xmin=.02,xmax=.98,color=COLORS["grid"],linewidth=.8,alpha=.55)
    info.text(.02,.105,localized_text("说明：实测频偏、杂散和 Spur Margin 仅作为测量事实；未绑定版本化阈值时不判 PASS/FAIL。","Measured frequency error/spur metrics do not imply PASS/FAIL without versioned thresholds."),fontproperties=human_font_properties(size=8.0),va="top",wrap=True,color=COLORS["muted"])
    out=io.BytesIO();fig.savefig(out,format="png",dpi=160,bbox_inches="tight",facecolor=COLORS["background"]);plt.close(fig)
    return out.getvalue(),measurement
