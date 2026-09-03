from __future__ import annotations

import html
from typing import Any, Mapping


def render_report_v2_html(report: Mapping[str, Any]) -> str:
    """Render decision-first HTML without inventing report facts."""

    first = report.get("first_page") or {}
    validation = report.get("semantic_validation") or {}
    finding_cards = _finding_cards(report)
    appendix = _appendix(report)
    blocked = validation.get("status") != "PASS"
    status = "BLOCKED" if blocked else report.get("pipeline_status") or "COMPLETE"

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{_e(report.get('report_id'))} VOIP 初步证据分析 V2</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:1080px;margin:32px auto;padding:0 24px;line-height:1.6;color:#222}}
h1,h2,h3{{line-height:1.3}} table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
.card{{border:1px solid #ddd;border-radius:10px;padding:16px;margin:14px 0}} .boundary{{background:#f5f5f5;padding:12px;border-radius:8px}}
.blocked{{border:2px solid #900;padding:12px}} code{{white-space:pre-wrap}} .small{{font-size:.9em;color:#555}}
</style></head><body>
<h1>VOIP 初步证据分析报告 V2</h1>
<p><strong>Report：</strong>{_e(report.get('report_id'))}　<strong>Status：</strong>{_e(status)}</p>
{_validation_banner(validation)}
<h2>1. 当前结论</h2><p>{_e(first.get('conclusion'))}</p>
<h2>2. 用户问题是否复现</h2><p><strong>{_e(first.get('symptom_reproduction'))}</strong> {_e(first.get('symptom_detail'))}</p>
<h2>3. 主要异常</h2>{_top_abnormal(first)}
<h2>4. 正常 / 排除性证据</h2>{_normal(first)}
<h2>5. 证据边界</h2>{_boundaries(first)}
<h2>6. 下一步验证</h2>{_next_steps(first)}
<h2>7. Finding Cards</h2>{finding_cards}
<h2>技术附录</h2>{appendix}
</body></html>"""


def _finding_cards(report: Mapping[str, Any]) -> str:
    events_by_id = {str(item.get("event_id")): item for item in report.get("events") or [] if isinstance(item, Mapping)}
    cluster_by_id = {str(item.get("cluster_id")): item for item in report.get("correlation_clusters") or [] if isinstance(item, Mapping)}
    cards: list[str] = []

    for cluster in cluster_by_id.values():
        members = []
        for member in cluster.get("member_events") or []:
            if not isinstance(member, Mapping):
                continue
            event = events_by_id.get(str(member.get("event_ref") or "")) or {}
            members.append(
                f"<li>{_e(member.get('layer'))}: {_e(event.get('observation_type'))} @ {_e(event.get('timestamp'))}</li>"
            )
        cards.append(
            "<div class='card'>"
            f"<h3>{_e(cluster.get('cluster_id'))} · {_e(cluster.get('type'))}</h3>"
            f"<p><strong>When：</strong>{_e(cluster.get('representative_time'))}</p>"
            f"<p><strong>Key Evidence：</strong></p><ul>{''.join(members)}</ul>"
            f"<p><strong>Interpretation：</strong>{_e(cluster.get('interpretation_boundary'))}</p>"
            "<p><strong>Not Confirmed：</strong>Cross-layer correlation 不自动确认物理因果或 Root Cause。</p>"
            "</div>"
        )

    for finding in report.get("findings") or []:
        if not isinstance(finding, Mapping) or finding.get("absorbed_by_cluster"):
            continue
        cards.append(
            "<div class='card'>"
            f"<h3>{_e(finding.get('finding_id'))} · {_e(finding.get('title') or finding.get('type'))}</h3>"
            f"<p><strong>Severity：</strong>{_e(finding.get('severity'))}</p>"
            f"<p><strong>Events：</strong>{_e(finding.get('event_count'))}; continuous={_e(finding.get('continuous'))}</p>"
            f"<p><strong>Evidence refs：</strong>{_e(', '.join(str(x) for x in finding.get('evidence_refs') or []))}</p>"
            "</div>"
        )
    return "".join(cards) or "<p>无主要异常 Finding Card。</p>"


def _appendix(report: Mapping[str, Any]) -> str:
    call = report.get("call_reconstruction") or {}
    timeline = report.get("timeline") or {}
    visibility = report.get("visibility") or {}
    return (
        "<div class='small'>"
        f"<p><strong>Schema：</strong>{_e(report.get('schema'))}</p>"
        f"<p><strong>Call state：</strong>{_e(call.get('state'))}; termination={_e((call.get('termination') or {}).get('observed'))}; call_end={_e(call.get('call_end_time'))}</p>"
        f"<p><strong>Media observation：</strong>{_e((timeline.get('media_observation_window') or {}).get('start'))} → {_e((timeline.get('media_observation_window') or {}).get('end'))}</p>"
        f"<p><strong>Visibility：</strong>{_e(visibility)}</p>"
        f"<p><strong>Artifact count：</strong>{len(report.get('artifacts') or [])}; failure count={len(report.get('artifact_failures') or [])}</p>"
        "</div>"
    )


def _validation_banner(validation: Mapping[str, Any]) -> str:
    if validation.get("status") == "PASS":
        return "<p><strong>Semantic Validator：</strong>PASS</p>"
    violations = validation.get("violations") or []
    return "<div class='blocked'><strong>Semantic Validator：FAIL — 报告禁止作为 COMPLETE 发布。</strong>" + \
        "<ul>" + "".join(f"<li>{_e(item.get('rule'))}: {_e(item.get('detail'))}</li>" for item in violations if isinstance(item, Mapping)) + "</ul></div>"


def _top_abnormal(first: Mapping[str, Any]) -> str:
    items = first.get("top_abnormal") or []
    if not items:
        return "<p>无主要异常单元。</p>"
    return "<ul>" + "".join(f"<li>{_e(item.get('id'))} · {_e(item.get('type'))} @ {_e(item.get('time'))}</li>" for item in items if isinstance(item, Mapping)) + "</ul>"


def _normal(first: Mapping[str, Any]) -> str:
    items = first.get("normal_and_exclusion") or []
    if not items:
        return "<p>当前无结构化正常/排除性证据。</p>"
    return "<ul>" + "".join(f"<li>{_e(item.get('type'))}: {_e(item)}</li>" for item in items if isinstance(item, Mapping)) + "</ul>"


def _boundaries(first: Mapping[str, Any]) -> str:
    return "<div class='boundary'><ul>" + "".join(f"<li>{_e(item)}</li>" for item in first.get("evidence_boundaries") or []) + "</ul></div>"


def _next_steps(first: Mapping[str, Any]) -> str:
    steps = first.get("next_steps") or []
    return "<ol>" + "".join(f"<li>{_e(item)}</li>" for item in steps) + "</ol>" if steps else "<p>无额外建议。</p>"


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))
