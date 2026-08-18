from __future__ import annotations

from app.reports.finding_composer import derive_first_observable_layer


def _periodic_abnormal(node: dict | None) -> tuple[bool,bool]:
    if node is None:
        return False, False
    level=str(node.get("level") or "LOW").upper()
    return True, level in {"MEDIUM","HIGH"}


def apply_first_observable_boundaries(payload:dict) -> dict:
    """Enrich cross-layer Findings without creating new root-cause authority."""
    for finding in payload.get("findings",[]) or []:
        if finding.get("type") not in {"LOCAL_CAPTURE_PERIODIC_INTERFERENCE","PERIODIC_INTERFERENCE_PATH_COMPARISON"}:
            continue
        metrics=finding.get("metrics") or {}
        down_available,down_abnormal=_periodic_abnormal(metrics.get("downstream_rtp"))
        pcm_available,pcm_abnormal=_periodic_abnormal(metrics.get("pcm_rx"))
        up_available,up_abnormal=_periodic_abnormal(metrics.get("upstream_rtp"))
        result=derive_first_observable_layer([
            {"layer":"RTP_DOWNSTREAM","available":down_available,"abnormal":down_abnormal},
            {"layer":"PCM_RX","available":pcm_available,"abnormal":pcm_abnormal},
            {"layer":"RTP_UPSTREAM","available":up_available,"abnormal":up_abnormal},
        ])
        correlation=dict(finding.get("correlation") or {})
        correlation["first_observable_boundary"]=result
        correlation["role_boundary"]=(
            "RTP Downstream / Upstream 角色来自当前 Media Correlation 的方向映射；"
            "本字段仅描述当前采集链路中的首次可观测位置，不声明物理信号起源。"
        )
        finding["correlation"]=correlation
        if result.get("status")=="OBSERVED_BOUNDARY":
            finding["interpretation"]=(finding.get("interpretation") or "")+" "+result.get("statement","")
        elif result.get("status")=="UNKNOWN":
            finding["interpretation"]=(finding.get("interpretation") or "")+" 当前上游/对照层证据不完整，首次异常层保持 UNKNOWN（未知）。"
    return payload
