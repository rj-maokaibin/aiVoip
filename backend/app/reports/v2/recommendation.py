from __future__ import annotations

from typing import Any, Iterable, Mapping


def generate_recommendations(
    *,
    findings: Iterable[Mapping[str, Any]] = (),
    clusters: Iterable[Mapping[str, Any]] = (),
    visibility: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Generate deterministic, evidence-bound next actions.

    Rules are intentionally conservative: they reference entities that actually
    exist in the canonical report and never invent a severity or Root Cause.
    """

    finding_list = [dict(item) for item in findings]
    cluster_list = [dict(item) for item in clusters]
    recommendations: list[dict[str, Any]] = []

    for cluster in cluster_list:
        cluster_id = str(cluster.get("cluster_id") or "")
        if not cluster_id:
            continue
        cluster_type = str(cluster.get("type") or "").upper()
        if cluster_type == "CROSS_LAYER_MEDIA_TIMING_SPIKE":
            recommendations.append(
                {
                    "priority": "P0",
                    "action": "复现时同步采集 PCM RX/TX、RTP、CPU/softirq 与媒体驱动计数器。",
                    "why": "同一 Call 的多个媒体层在同一时间窗出现 timing spike，但当前相关性不能确认物理根因。",
                    "collect": ["PCM_RX", "PCM_TX", "RTP", "CPU", "SOFTIRQ", "RFF_CNT", "TFE_CNT"],
                    "decision_rule": "若同一 timing spike 与 CPU/softirq stall 或驱动计数器增长同窗出现，则提升对应候选链路优先级；否则继续排查 packet emission/buffering。",
                    "pass_criteria": "至少一次复现中完成同一时钟域绑定，并能判断 spike 前后 RTP sequence 与驱动计数器是否连续。",
                    "source": "RULE",
                    "cluster_refs": [cluster_id],
                }
            )

    for finding in finding_list:
        finding_id = str(finding.get("finding_id") or "")
        if not finding_id:
            continue
        finding_type = str(finding.get("type") or "").upper()
        finding_class = str(finding.get("class") or finding.get("kind") or "ABNORMAL").upper()
        if finding.get("absorbed_by_cluster") or finding_class != "ABNORMAL":
            continue
        if finding_type == "RTP_SEQUENCE_LOSS":
            recommendations.append(
                {
                    "priority": "P0",
                    "action": "按 RTP stream/SSRC 核对 sequence gap、方向与网络路径，并同步抓取链路侧丢包计数。",
                    "why": "当前 Finding 已有 RTP sequence-loss 证据，需要区分发送端缺包、网络丢包和抓包点不可见。",
                    "collect": ["RTP_SEQUENCE", "INTERFACE_COUNTERS", "PEER_PCAP"],
                    "decision_rule": "若发送侧可见完整 sequence 而接收侧缺失，则优先网络路径；若发送侧自身缺失，则优先发送/媒体栈。",
                    "pass_criteria": "同一 SSRC 在至少两个观察点完成 sequence 对账。",
                    "source": "RULE",
                    "finding_refs": [finding_id],
                }
            )

    visibility = visibility or {}
    end_to_end = visibility.get("end_to_end_media")
    if end_to_end in {"PARTIAL", "UNKNOWN", "UNAVAILABLE"}:
        recommendations.append(
            {
                "priority": "P1",
                "action": "补齐缺失媒体方向或对端抓包后再判断 End-to-End 媒体完整性。",
                "why": f"当前 end-to-end media visibility={end_to_end}，不足以做完整双向媒体结论。",
                "collect": ["MISSING_MEDIA_LEG_PCAP"],
                "decision_rule": "只有 caller/callee 需要的媒体方向均可见时，才允许升级为 COMPLETE。",
                "pass_criteria": "visibility.end_to_end_media=COMPLETE，或明确记录无法获取的边界。",
                "source": "RULE",
            }
        )

    return _deduplicate(recommendations)


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
    for item in items:
        key = (
            str(item.get("action") or ""),
            tuple(str(x) for x in item.get("finding_refs") or []),
            tuple(str(x) for x in item.get("cluster_refs") or []),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
