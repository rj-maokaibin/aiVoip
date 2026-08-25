from __future__ import annotations

from app.reports.evidence_visuals import Canvas, _plot_box, _header


def _message_label(msg:dict)->str:
    frame=msg.get("frame_number") or msg.get("frame")
    label=msg.get("label")
    if not label:
        method=msg.get("method") or msg.get("request_method")
        status=msg.get("status_code") or msg.get("response_code") or msg.get("status")
        phrase=msg.get("reason") or msg.get("reason_phrase")
        label=str(method or (f"{status} {phrase or ''}".strip() if status else msg.get("message") or "SIP"))
    return f"F{frame} {label}" if frame is not None else str(label)


def _endpoint(msg:dict,key:str)->str|None:
    direct=msg.get(key)
    if direct:return str(direct)
    ip=msg.get(f"{key}_ip");port=msg.get(f"{key}_port")
    if ip and port is not None:return f"{ip}:{port}"
    return str(ip) if ip else None


def render_sip_call_flow_png(calls:list[dict],*,width:int=1400,height:int=720,title:str="SIP CALL FLOW",subtitle:str|None=None)->bytes:
    """Render the production SIP `ladder[]` contract with Frame labels.

    Packet reconstruction stores call.ladder rather than a duplicate message list.
    Supporting ladder here avoids a presentation-only schema fork and preserves the
    Frame/Method/Status details required for evidence review.
    """
    canvas=Canvas(width,height);_header(canvas,title,subtitle);left,top,right,bottom=_plot_box(canvas)
    messages=[]
    for call in calls:
        rows=call.get("ladder") or call.get("messages") or call.get("sip_messages") or []
        messages.extend(rows)
    messages=messages[:24]
    endpoints=[]
    for msg in messages:
        for key in ("src","dst"):
            value=_endpoint(msg,key)
            if value and value not in endpoints:endpoints.append(value)
    endpoint_a=endpoints[0] if endpoints else "ENDPOINT A";endpoint_b=endpoints[1] if len(endpoints)>1 else "ENDPOINT B"
    x_a=left+185;x_b=right-185
    canvas.text(max(left,x_a-75),top+5,endpoint_a,scale=1,max_width=170);canvas.text(max(left,x_b-75),top+5,endpoint_b,scale=1,max_width=170)
    canvas.line(x_a,top+30,x_a,bottom-20,(80,80,80),2);canvas.line(x_b,top+30,x_b,bottom-20,(80,80,80),2)
    if not messages:
        canvas.text(left+20,top+55,"NO SIP LADDER DATA IN CALL RESULT",scale=2,color=(130,130,130),max_width=right-left-40)
    step=max(20,(bottom-top-80)//max(1,len(messages)))
    for i,msg in enumerate(messages):
        y=top+55+i*step;src=_endpoint(msg,"src");dst=_endpoint(msg,"dst")
        outgoing=src==endpoint_a if src else True
        # If B2BUA/extra endpoint appears, keep a deterministic left/right projection
        # while the text label still exposes the real src/dst endpoint.
        xa,xb=(x_a,x_b) if outgoing else (x_b,x_a)
        canvas.line(xa,y,xb,y,(45,70,95),2)
        canvas.line(xb,y,xb+(-8 if outgoing else 8),y-5,(45,70,95),2);canvas.line(xb,y,xb+(-8 if outgoing else 8),y+5,(45,70,95),2)
        endpoint_note=f"{src}>{dst} " if src or dst else ""
        canvas.text(min(xa,xb)+12,max(top,y-11),endpoint_note+_message_label(msg),scale=1,color=(45,60,80),max_width=abs(xb-xa)-24)
    canvas.text(8,top-24,"MESSAGE ORDER",scale=1,max_width=100);canvas.text((left+right)//2-45,bottom+43,"SIP ENDPOINT",scale=1)
    return canvas.png_bytes()
