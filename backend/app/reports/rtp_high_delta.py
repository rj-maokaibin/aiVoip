from __future__ import annotations

from collections import Counter
from typing import Any


HIGH_DELTA_ROOT_CAUSE_BOUNDARY = (
    "HIGH_DELTA 仅证明该 RTP Stream 在抓包观察点出现异常长的相邻包间隔。"
    "当 Sequence 连续时，它不能被表述为 RTP 丢包；单凭该事件也不能区分发送端调度停顿、"
    "中间网络排队/阻塞、抓包调度或接收端处理中的具体根因。"
)


def stream_direction(stream: dict) -> str | None:
    if not stream:
        return None
    return f"{stream.get('src_ip')}:{stream.get('src_port')}->{stream.get('dst_ip')}:{stream.get('dst_port')}"


def high_delta_observation(evidence: dict, stream: dict) -> str:
    delta = evidence.get("delta_ms")
    ptime = evidence.get("expected_ptime_ms") or evidence.get("ptime_ms") or stream.get("ptime_ms")
    prev_frame = evidence.get("previous_frame_number")
    current_frame = evidence.get("current_frame_number")
    prev_seq = evidence.get("previous_sequence")
    current_seq = evidence.get("current_sequence")
    classification = evidence.get("classification")
    catch_up = evidence.get("catch_up") or {}
    direction = stream_direction(stream) or str(evidence.get("stream_id") or "当前 RTP Stream")

    parts = [f"RTP 流 {direction} 观测到相邻包间隔 {delta} ms"]
    if ptime is not None:
        parts.append(f"预期 ptime 约 {ptime} ms")
    if prev_frame is not None and current_frame is not None:
        parts.append(f"Frame {prev_frame}→{current_frame}")
    if prev_seq is not None and current_seq is not None:
        if evidence.get("sequence_continuous") is True:
            parts.append(f"Seq {prev_seq}→{current_seq} 连续，事件边界无 Sequence Loss")
        else:
            parts.append(f"Seq {prev_seq}→{current_seq}")
    if classification == "INTERARRIVAL_STALL_WITHOUT_RTP_GAP":
        parts.append("RTP Timestamp 步进与媒体 ptime 一致，异常主要体现在到达/发送节奏停顿")
    if catch_up.get("status") in {"PARTIAL", "FULL"}:
        parts.append(
            f"随后存在 {catch_up.get('status')} catch-up，约回收 {catch_up.get('recovered_delay_ms')} ms 停顿时间"
        )
    return "；".join(parts) + "。"


def high_delta_interpretation(evidence: dict) -> str:
    classification = str(evidence.get("classification") or "")
    sequence_continuous = evidence.get("sequence_continuous")
    catch_up = evidence.get("catch_up") or {}
    if sequence_continuous is True:
        base = "该事件应解释为 RTP 包到达/发送节奏出现延迟或停顿，而不是 Packet Loss（丢包）。"
    else:
        base = "该事件证明 RTP 包间隔显著增大；由于事件边界同时存在 Sequence 非连续/不确定，需与独立 Packet Loss 证据一起解释。"
    if classification == "INTERARRIVAL_STALL_WITH_MEDIA_TIMESTAMP_GAP":
        base += " RTP Timestamp 步进也异常，媒体时间轴可能同时存在跳变。"
    if catch_up.get("status") in {"PARTIAL", "FULL"}:
        base += " 停顿后可见快速到包 catch-up，说明部分延迟随后被压缩回收，但这不代表用户听感一定无影响。"
    base += " 是否来自 DUT 调度/发送、网络排队还是抓包观察点，需要结合同时间 PCM Gap、反向 RTP 和其他层证据判断。"
    return base


def high_delta_metrics(evidence: dict, stream: dict) -> dict:
    metrics = dict(evidence)
    metrics.update({
        "packet_count": stream.get("packet_count"),
        "stream_lost_packets": stream.get("lost_packets", stream.get("lost")),
        "stream_loss_rate": stream.get("loss_rate"),
        "p95_jitter_ms": stream.get("p95_rfc3550_jitter_ms", stream.get("p95_jitter_ms")),
        "stream_max_delta_ms": stream.get("max_delta_ms"),
        "stream_high_delta_count": stream.get("high_delta_count"),
        "stream_high_delta_without_sequence_loss_count": stream.get("high_delta_without_sequence_loss_count"),
        "stream_high_delta_catch_up_count": stream.get("high_delta_catch_up_count"),
        "codec": stream.get("codec"),
        "ptime_ms": stream.get("ptime_ms"),
        "ssrc": stream.get("ssrc"),
        "call_direction_role": stream.get("call_direction_role") or evidence.get("call_direction_role"),
    })
    return metrics


def aggregate_high_delta_findings(items: list[dict], head: dict) -> dict:
    events: list[dict[str, Any]] = []
    for item in items:
        metrics = item.get("metrics") or {}
        events.append({
            "time": (item.get("time_range") or {}).get("representative"),
            "delta_ms": metrics.get("delta_ms"),
            "expected_ptime_ms": metrics.get("expected_ptime_ms") or metrics.get("ptime_ms"),
            "excess_delay_ms": metrics.get("excess_delay_ms"),
            "threshold_ms": metrics.get("threshold_ms"),
            "previous_frame_number": metrics.get("previous_frame_number"),
            "current_frame_number": metrics.get("current_frame_number"),
            "previous_sequence": metrics.get("previous_sequence"),
            "current_sequence": metrics.get("current_sequence"),
            "sequence_continuous": metrics.get("sequence_continuous"),
            "classification": metrics.get("classification"),
            "catch_up": metrics.get("catch_up"),
        })
    deltas = [float(e["delta_ms"]) for e in events if e.get("delta_ms") is not None]
    excess = [float(e["excess_delay_ms"]) for e in events if e.get("excess_delay_ms") is not None]
    classifications = Counter(str(e.get("classification") or "UNKNOWN") for e in events)
    catch_up_status = Counter(str((e.get("catch_up") or {}).get("status") or "NONE") for e in events)
    all_sequence_continuous = bool(events) and all(e.get("sequence_continuous") is True for e in events)
    head["metrics"] = {
        **(head.get("metrics") or {}),
        "event_count": len(events),
        "max_delta_ms": max(deltas) if deltas else None,
        "max_excess_delay_ms": max(excess) if excess else None,
        "all_sequence_continuous": all_sequence_continuous,
        "classification_counts": dict(classifications),
        "catch_up_status_counts": dict(catch_up_status),
        "events": events,
    }
    direction = (head.get("scope") or {}).get("direction") or (head.get("scope") or {}).get("rtp_stream_id") or "当前 RTP Stream"
    sequence_text = "全部事件 Sequence 连续，未观察到对应 RTP 丢包" if all_sequence_continuous else "部分事件 Sequence 非连续或证据不足，需与丢包事件联合判断"
    max_delta = max(deltas) if deltas else None
    head["observation"] = f"RTP 流 {direction} 共观测到 {len(events)} 次包间隔异常"
    if max_delta is not None:
        head["observation"] += f"，最大间隔 {round(max_delta, 3)} ms"
    head["observation"] += f"；{sequence_text}。"
    if catch_up_status.get("FULL", 0) or catch_up_status.get("PARTIAL", 0):
        head["observation"] += f" 其中 {catch_up_status.get('FULL', 0) + catch_up_status.get('PARTIAL', 0)} 次随后可见 catch-up。"
    head["interpretation"] = (
        "该 Finding 聚合的是同一个 RTP Stream 的 HIGH_DELTA 事件。HIGH_DELTA 表示到达/发送节奏停顿；"
        + ("当前聚合事件的 Sequence 均连续，因此不应写成 Packet Loss。" if all_sequence_continuous else "若同时存在 Sequence Gap，应由独立丢包证据明确表达。")
        + " 具体根因仍需跨层证据定位。"
    )
    head["root_cause_boundary"] = HIGH_DELTA_ROOT_CAUSE_BOUNDARY
    return head
