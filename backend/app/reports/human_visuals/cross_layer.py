from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .theme import COLORS
from .typography import human_font_properties, localized_text


def render_human_cross_layer_png(layers:list[dict],correlations:list[dict],*,canonical_boundary_statement:str|None=None,
                                 title:str="跨层媒体证据路径",width_px:int=1800,height_px:int=700)->tuple[bytes,dict]:
    fig,ax=plt.subplots(figsize=(width_px/160.0,height_px/160.0),constrained_layout=True)
    fig.patch.set_facecolor(COLORS["background"]);ax.set_facecolor(COLORS["panel"]);ax.axis("off")
    count=max(1,len(layers));xs=[.08+i*(.84/max(1,count-1)) for i in range(count)] if count>1 else [.5]
    centers={}
    for x,layer in zip(xs,layers):
        name=str(layer.get("name") or layer.get("label") or "UNKNOWN");available=bool(layer.get("available",True));status=str(layer.get("status") or ("AVAILABLE" if available else "UNAVAILABLE"))
        face=COLORS["background"] if available else "#EEF1F5";edge=COLORS["waveform"] if available else COLORS["muted"]
        box=plt.Rectangle((x-.075,.49),.15,.20,transform=ax.transAxes,facecolor=face,edgecolor=edge,linewidth=1.5)
        ax.add_patch(box);centers[name]=(x,.59)
        ax.text(x,.63,name,ha="center",va="center",fontproperties=human_font_properties(size=10,weight="semibold"),color=COLORS["text"])
        ax.text(x,.565,status,ha="center",va="center",fontproperties=human_font_properties(size=8.2),color=COLORS["muted"])
        if layer.get("rms_dbfs") is not None:
            ax.text(x,.52,f"RMS {float(layer['rms_dbfs']):.2f} dBFS",ha="center",va="center",fontproperties=human_font_properties(size=7.8),color=COLORS["muted"])
    normalized=[]
    for corr in correlations:
        src=str(corr.get("from") or corr.get("source") or "");dst=str(corr.get("to") or corr.get("target") or "")
        if src not in centers or dst not in centers:continue
        x0,y0=centers[src];x1,y1=centers[dst]
        ax.annotate("",xy=(x1-.078 if x1>x0 else x1+.078,y1),xytext=(x0+.078 if x1>x0 else x0-.078,y0),xycoords=ax.transAxes,textcoords=ax.transAxes,arrowprops=dict(arrowstyle="->",linewidth=1.3,color=COLORS["reference"]))
        abs_corr=corr.get("absolute_correlation");lag=corr.get("lag_ms");quality=str(corr.get("quality") or "UNKNOWN")
        label=[]
        if abs_corr is not None:label.append(f"corr {float(abs_corr):.3f}")
        if lag is not None:label.append(f"lag {float(lag):.1f} ms")
        label.append(quality)
        ax.text((x0+x1)/2,.635,"\n".join(label),ha="center",va="bottom",fontproperties=human_font_properties(size=7.5),color=COLORS["muted"])
        normalized.append({"label":f"{src} ↔ {dst}","from":src,"to":dst,"absolute_correlation":abs_corr,"lag_ms":lag,"quality":quality})
    ax.text(.02,.92,localized_text(title,"Cross-layer media evidence path"),transform=ax.transAxes,fontproperties=human_font_properties(size=15,weight="semibold"),color=COLORS["text"],va="top")
    ax.text(.02,.84,localized_text("仅投影 Analyzer 已有的可用性、相关性和 lag；不根据图形自行确认物理根因。","Projects existing Analyzer availability/correlation/lag only."),transform=ax.transAxes,fontproperties=human_font_properties(size=8.8),color=COLORS["muted"],va="top")
    if canonical_boundary_statement:
        ax.text(.02,.22,localized_text("证据边界：","Evidence boundary: ")+str(canonical_boundary_statement),transform=ax.transAxes,fontproperties=human_font_properties(size=9.2),color=COLORS["anomaly"],va="top",wrap=True)
    out=io.BytesIO();fig.savefig(out,format="png",dpi=160,bbox_inches="tight",facecolor=COLORS["background"]);plt.close(fig)
    return out.getvalue(),{
        "measurement_method":"CANONICAL_CROSS_LAYER_PROJECTION_V1","layers":[dict(x) for x in layers],"correlations":normalized,
        "first_observable_boundary":canonical_boundary_statement,"authority":"PRESENTATION_ONLY","boundary_inference_performed":False,
    }
