from __future__ import annotations
import html, json, hashlib
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.ids import new_id
from app.contracts.enums import ReportStatus
from app.db.models import Artifact, Case, CollectionPlan, DiagnosisReport, DiagnosisRun, Hypothesis, HypothesisEvidence
from app.knowledge.service import find_similar_cases


def _now(): return datetime.now(timezone.utc)

def _esc(x): return html.escape(str(x or ''))

def build_report_payload(db:Session,case_id:str)->dict:
    case=db.get(Case,case_id)
    if not case: raise ValueError('CASE_NOT_FOUND')
    run=db.scalar(select(DiagnosisRun).where(DiagnosisRun.case_id==case_id).order_by(DiagnosisRun.created_at.desc()))
    hypotheses=list(db.scalars(select(Hypothesis).where(Hypothesis.case_id==case_id).order_by(Hypothesis.confidence.desc())))
    hdata=[]
    for h in hypotheses:
        refs=list(db.scalars(select(HypothesisEvidence).where(HypothesisEvidence.hypothesis_id==h.id).order_by(HypothesisEvidence.created_at.asc())))
        hdata.append({'id':h.id,'code':h.code,'title':h.title,'fault_domain':h.fault_domain,'status':h.status,'confidence':round(h.confidence/10000.0,4),'rationale':h.rationale,'confirmable':bool(h.confirmable),'confirm_rule':h.confirm_rule,'evidence':[{'level':r.evidence_level,'direction':r.direction,'ref_type':r.ref_type,'ref_id':r.ref_id,'rationale':r.rationale,'details':r.details_json or {}} for r in refs]})
    plans=list(db.scalars(select(CollectionPlan).where(CollectionPlan.case_id==case_id).order_by(CollectionPlan.created_at.asc())))
    summary=(run.summary_json if run else {}) or {}
    return {
        'schema_version':'diagnosis-report-v1','generated_at':_now().isoformat(),
        'case':{'id':case.id,'case_no':case.case_no,'summary':case.summary,'status':case.status},
        'diagnosis_run':None if not run else {'id':run.id,'status':run.status,'cycle':run.cycle,'reasoner_name':run.reasoner_name,'reasoner_version':run.reasoner_version,'workflow_version':run.workflow_version,'prompt_version':run.prompt_version,'model_name':run.model_name},
        'headline':summary.get('headline','尚未形成最终诊断结论'),
        'known':summary.get('known') or (run.decision_json or {}).get('known',[]) if run else [],
        'unknown':summary.get('unknown') or (run.decision_json or {}).get('unknown',[]) if run else [],
        'excluded':summary.get('excluded') or (run.decision_json or {}).get('excluded',[]) if run else [],
        'hypotheses':hdata,
        'collection_plans':[{'cycle':p.cycle,'status':p.status,'goal':p.goal,'actions':p.actions_json,'execution_job_ids':p.execution_job_ids or []} for p in plans],
        'similar_cases':find_similar_cases(db,case_id,limit=5),
        'traceability':{'rule_engine':summary.get('rule_engine'),'reasoner':summary.get('llm_status','deterministic'),'note':'CONFIRMED必须满足直接证据、无关键反证及人工确认；历史Case和AI推断不能单独确认根因。'},
    }


def render_report_html(payload:dict)->str:
    hs=payload.get('hypotheses',[])
    rows=''.join(f"<tr><td>{_esc(h['title'])}</td><td>{_esc(h['fault_domain'])}</td><td>{_esc(h['status'])}</td><td>{h['confidence']:.1%}</td><td>{_esc(h.get('rationale'))}</td></tr>" for h in hs)
    def lis(items): return ''.join(f'<li>{_esc(x)}</li>' for x in (items or [])) or '<li>无</li>'
    similar=''.join(f"<li><b>{_esc(x['case_no'])}</b> 相似度 {x['score']:.1%}：{_esc(x['summary'])}</li>" for x in payload.get('similar_cases',[])) or '<li>未发现达到阈值的历史相似Case</li>'
    run=payload.get('diagnosis_run') or {}
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{_esc(payload['case']['case_no'])} VOIP诊断报告</title>
<style>body{{font-family:Arial,"Microsoft YaHei",sans-serif;max-width:1180px;margin:32px auto;color:#1f2937;line-height:1.6}}h1,h2{{color:#111827}}.hero{{padding:20px;border:1px solid #d1d5db;border-radius:12px;background:#f9fafb}}.badge{{display:inline-block;padding:2px 8px;border-radius:10px;background:#e5e7eb;margin-right:6px}}table{{width:100%;border-collapse:collapse}}th,td{{border:1px solid #d1d5db;padding:8px;vertical-align:top}}th{{background:#f3f4f6}}.warn{{background:#fff7ed;padding:12px;border-left:4px solid #f59e0b}}</style></head><body>
<h1>VOIP AI 故障诊断报告</h1><div class="hero"><div><span class="badge">{_esc(payload['case']['case_no'])}</span><span class="badge">{_esc(payload['case']['status'])}</span></div><h2>{_esc(payload['headline'])}</h2><p>{_esc(payload['case']['summary'])}</p></div>
<h2>已确认事实</h2><ul>{lis(payload.get('known'))}</ul><h2>仍未知 / 待补证</h2><ul>{lis(payload.get('unknown'))}</ul><h2>已排除方向</h2><ul>{lis(payload.get('excluded'))}</ul>
<h2>根因假设</h2><table><thead><tr><th>假设</th><th>域</th><th>状态</th><th>置信度</th><th>依据</th></tr></thead><tbody>{rows}</tbody></table>
<h2>历史相似Case</h2><ul>{similar}</ul>
<h2>诊断追溯</h2><p>Reasoner: {_esc(run.get('reasoner_name'))} {_esc(run.get('reasoner_version'))}；Workflow: {_esc(run.get('workflow_version'))}；Model: {_esc(run.get('model_name') or '未使用/未记录')}</p>
<div class="warn"><b>证据纪律：</b>{_esc((payload.get('traceability') or {}).get('note'))}</div></body></html>'''


def generate_report(db:Session,case_id:str,*,actor:str|None=None,storage=None):
    if storage is None:
        from app.integrations.storage import ObjectStorage
        storage=ObjectStorage()
    payload=build_report_payload(db,case_id)
    html_text=render_report_html(payload)
    report_id=new_id(); prefix=f'cases/{case_id}/reports/{report_id}'
    json_bytes=json.dumps(payload,ensure_ascii=False,indent=2).encode(); html_bytes=html_text.encode()
    json_key=prefix+'/diagnosis-report.json'; html_key=prefix+'/diagnosis-report.html'
    storage.put_bytes(json_key,json_bytes,'application/json'); storage.put_bytes(html_key,html_bytes,'text/html; charset=utf-8')
    run_id=(payload.get('diagnosis_run') or {}).get('id')
    row=DiagnosisReport(id=report_id,case_id=case_id,diagnosis_run_id=run_id,version='1.0',status=ReportStatus.GENERATED.value,html_object_key=html_key,json_object_key=json_key,snapshot_json={'headline':payload['headline'],'hypothesis_count':len(payload['hypotheses'])},created_by=actor)
    db.add(row); db.flush()
    db.add(Artifact(case_id=case_id,analyzer_run_id=None,evidence_id=None,type='DIAGNOSIS_REPORT_HTML',filename='diagnosis-report.html',object_key=html_key,content_type='text/html; charset=utf-8',size_bytes=len(html_bytes),sha256=hashlib.sha256(html_bytes).hexdigest(),metadata_json={'report_id':report_id,'version':'1.0'}))
    db.add(Artifact(case_id=case_id,analyzer_run_id=None,evidence_id=None,type='DIAGNOSIS_REPORT_JSON',filename='diagnosis-report.json',object_key=json_key,content_type='application/json',size_bytes=len(json_bytes),sha256=hashlib.sha256(json_bytes).hexdigest(),metadata_json={'report_id':report_id,'version':'1.0'}))
    db.flush(); return row,payload
