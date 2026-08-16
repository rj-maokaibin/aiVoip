from __future__ import annotations
from datetime import datetime, timezone
from celery.utils.log import get_task_logger
from sqlalchemy import select
from app.core.config import settings
from app.db.models import AnalyzerRun, Case, CollectionPlan, DiagnosisRun, Evidence, Hypothesis, HypothesisEvidence, Job
from app.db.session import SessionLocal
from app.diagnosis.factory import get_diagnosis_reasoner
from app.diagnosis.policy import enforce_plan_action
from app.diagnosis.snapshot import CaseEvidenceSnapshotBuilder
from app.services.analysis import create_media_analysis_job, create_packet_analysis_job, create_pcm_analysis_job
from app.services.audit import audit
from app.services.cases import create_collect_job, transition_case
from app.services.diagnosis import create_plan, persist_decision
from app.services.jobs import transition_job
from app.contracts.enums import CaseEvent, CollectionPlanStatus, DiagnosisRunStatus, JobStatus
from app.rules.engine import RuleEngine
from app.services.rules import active_compiled_rules, merge_rule_effects
from app.knowledge.service import enrich_decision_with_history, find_similar_cases, persist_case_relations, search_knowledge_items
from app.workers.celery_app import celery_app

log=get_task_logger(__name__)
ACTIVE={DiagnosisRunStatus.PENDING.value,DiagnosisRunStatus.ANALYZING.value,DiagnosisRunStatus.WAITING_EVIDENCE.value}


def utcnow(): return datetime.now(timezone.utc)


def _refresh_feishu_card(case_id: str, reason: str) -> None:
    if not settings.feishu_live_enabled:
        return
    try:
        from app.workers.device_provision_task import sync_case_card
        sync_case_card.apply_async(args=[case_id, reason], queue='diagnosis')
    except Exception:
        log.exception('failed to enqueue feishu card refresh case=%s', case_id)


def _notify_feishu_milestone(db, *, case_id: str, feedback_type: str,
                              token: str, text: str) -> None:
    try:
        from app.integrations.feishu.feedback import notify_case_once
        notify_case_once(db, case_id=case_id, feedback_type=feedback_type,
                         token=token, text=text)
        db.commit()
    except Exception:
        db.rollback()
        log.exception('failed to enqueue feishu milestone case=%s type=%s',
                      case_id, feedback_type)


def _publish_diagnosed_artifacts(db, *, case_id: str, run_id: str) -> dict:
    """Best-effort auto-publish after a DIAGNOSED run: generate the diagnosis
    report and push the updated case card back to Feishu (if live transport is
    configured). Failures are logged and never fail the diagnosis itself.

    Returns {'report': ..., 'feishu': ...} status strings for observability.
    """
    from app.reports.diagnosis_report import generate_report
    from app.integrations.feishu.service import FeishuCaseCardService
    import asyncio

    out = {'report': 'SKIPPED', 'feishu': 'SKIPPED'}
    # 1) Generate the diagnosis report (persist HTML+JSON artifacts).
    try:
        row, _payload = generate_report(db, case_id=case_id, actor='diagnosis-worker')
        db.flush()
        audit(db, case_id=case_id, actor='diagnosis-worker', event_type='DIAGNOSIS_REPORT_GENERATED',
              target_type='diagnosis_report', target_id=row.id)
        out['report'] = f"GENERATED:{row.id}"
    except Exception as exc:
        log.exception('auto diagnosis report generation failed')
        out['report'] = f"FAILED:{type(exc).__name__}:{exc}"

    # 2) Push the case card back to Feishu with the fresh diagnosis conclusion.
    #    Only when live transport is configured; otherwise report SKIPPED.
    if getattr(settings, 'feishu_live_enabled', False):
        try:
            service = FeishuCaseCardService()
            binding = asyncio.run(service.sync_case_card(db, case_id=case_id))
            out['feishu'] = f"SYNCED:{getattr(binding, 'message_id', '')}"
        except Exception as exc:
            log.exception('auto feishu card sync failed')
            out['feishu'] = f"FAILED:{type(exc).__name__}:{exc}"
    else:
        out['feishu'] = 'SKIPPED:FEISHU_LIVE_DISABLED'
    return out


def _active_child_job(db,case_id:str,job_type:str):
    return db.scalar(select(Job).where(Job.case_id==case_id,Job.type==job_type,Job.status.in_([JobStatus.PENDING.value,JobStatus.RUNNING.value])).order_by(Job.created_at.desc()))

def _dispatch_plan(db, run:DiagnosisRun, plan:CollectionPlan, decision) -> list[str]:
    from app.services.analysis import create_field_audio_analysis_job, create_field_media_alignment_job, create_image_analysis_job
    from app.workers.attachment_tasks import align_field_media, analyze_field_audio, analyze_image
    from app.workers.collector_tasks import collect_case
    from app.workers.media_tasks import analyze_media_evidence
    from app.workers.packet_tasks import analyze_evidence
    from app.workers.pcm_tasks import analyze_pcm_evidence
    jobs=[]
    for raw_action in sorted(decision.plan,key=lambda x:x.priority):
        action=enforce_plan_action(raw_action)
        if not action.auto_execute: continue
        if action.risk_level not in {'L0','L1'}: continue
        p=action.params
        if action.action_type in {'RUN_MEDIA_ANALYSIS','RUN_PACKET_ANALYSIS','RUN_PCM_ANALYSIS','RUN_FIELD_AUDIO_ANALYSIS','RUN_IMAGE_METADATA_ANALYSIS','RUN_FIELD_MEDIA_ALIGNMENT'}:
            evidence=db.get(Evidence,p.get('evidence_id'))
            if not evidence or evidence.case_id!=run.case_id:
                continue
        if action.action_type=='RUN_MEDIA_ANALYSIS':
            child=_active_child_job(db,run.case_id,'ANALYZE_MEDIA')
            if not child:
                child=create_media_analysis_job(db,case_id=run.case_id,evidence_id=p['evidence_id'],profile_id=p.get('profile_id','ruijie_aim_diag_v1'))
                analyze_media_evidence.apply_async(args=[child.id,p['evidence_id'],p.get('profile_id','ruijie_aim_diag_v1')],queue='media')
            jobs.append(child.id)
        elif action.action_type=='RUN_PACKET_ANALYSIS':
            child=_active_child_job(db,run.case_id,'ANALYZE_PACKET')
            if not child:
                child=create_packet_analysis_job(db,case_id=run.case_id,evidence_id=p['evidence_id'])
                analyze_evidence.apply_async(args=[child.id,p['evidence_id']],queue='packet')
            jobs.append(child.id)
        elif action.action_type=='RUN_PCM_ANALYSIS':
            child=_active_child_job(db,run.case_id,'ANALYZE_PCM')
            if not child:
                child=create_pcm_analysis_job(db,case_id=run.case_id,evidence_id=p['evidence_id'],profile_id=p.get('profile_id','ruijie_aim_diag_v1'))
                analyze_pcm_evidence.apply_async(args=[child.id,p['evidence_id'],p.get('profile_id','ruijie_aim_diag_v1')],queue='pcm')
            jobs.append(child.id)
        elif action.action_type=='RUN_FIELD_AUDIO_ANALYSIS':
            child=_active_child_job(db,run.case_id,'ANALYZE_FIELD_AUDIO')
            if not child:
                child=create_field_audio_analysis_job(db,case_id=run.case_id,evidence_id=p['evidence_id'])
                analyze_field_audio.apply_async(args=[child.id,p['evidence_id']],queue='media')
            jobs.append(child.id)
        elif action.action_type=='RUN_IMAGE_METADATA_ANALYSIS':
            child=_active_child_job(db,run.case_id,'ANALYZE_IMAGE_METADATA')
            if not child:
                child=create_image_analysis_job(db,case_id=run.case_id,evidence_id=p['evidence_id'])
                analyze_image.apply_async(args=[child.id,p['evidence_id']],queue='media')
            jobs.append(child.id)
        elif action.action_type=='RUN_FIELD_MEDIA_ALIGNMENT':
            child=_active_child_job(db,run.case_id,'ALIGN_FIELD_MEDIA')
            if not child:
                media_run=db.get(AnalyzerRun,p.get('media_run_id'))
                if not media_run or media_run.case_id!=run.case_id: continue
                child=create_field_media_alignment_job(db,case_id=run.case_id,evidence_id=p['evidence_id'],media_run_id=media_run.id)
                align_field_media.apply_async(args=[child.id,p['evidence_id'],media_run.id],queue='media')
            jobs.append(child.id)
        elif action.action_type=='COLLECT_PROFILE':
            child=_active_child_job(db,run.case_id,'COLLECT_DEVICE')
            if not child:
                child=create_collect_job(db,run.case_id,p.get('profile_id','voip_basic'))
                collect_case.apply_async(args=[child.id],queue='collector')
            jobs.append(child.id)
    plan.execution_job_ids=jobs
    plan.status=CollectionPlanStatus.EXECUTING.value if jobs else CollectionPlanStatus.WAITING_USER.value
    db.flush(); return jobs


def _standard_no_progress(decision,run):
    return {
        'headline':'自动诊断暂停：连续循环未获得新增有效证据',
        'known':decision.known,
        'unknown':decision.unknown,
        'excluded':decision.excluded,
        'top_hypotheses':decision.summary.get('top_hypotheses',[]),
        'blocking_reason':'NO_PROGRESS',
        'manual_action':'请补充新的抓包点、复现阶段PCAP/PCM，或由研发选择更高风险诊断动作。',
        'cycle':run.cycle,
    }


def _execute_cycle(run_id:str):
    db=SessionLocal()
    try:
        run=db.get(DiagnosisRun,run_id)
        if not run or run.status not in ACTIVE: return {'status':'inactive'}
        case=db.get(Case,run.case_id); parent=db.get(Job,run.job_id) if run.job_id else None
        if run.cycle>=settings.diagnosis_max_cycles:
            summary={'headline':'自动诊断暂停：达到最大诊断循环次数','blocking_reason':'MAX_CYCLES','cycle':run.cycle,'manual_action':'请补充新的直接证据或由研发选择下一步诊断动作。'}
            run.status=DiagnosisRunStatus.WAITING_USER.value; run.summary_json=summary
            if parent: transition_job(db,parent,JobStatus.WAITING_USER,reason='diagnosis_max_cycles')
            transition_case(db,case,CaseEvent.USER_ACTION_REQUIRED,'diagnosis_max_cycles'); db.commit()
            from app.integrations.feishu.feedback import build_single_user_question
            _notify_feishu_milestone(
                db, case_id=case.id, feedback_type='WAITING_USER',
                token=f'{run.id}:max-cycles', text=build_single_user_question(summary=summary),
            )
            return {'status':'WAITING_USER','run_id':run.id,'summary':summary}
        run.status=DiagnosisRunStatus.ANALYZING.value; run.cycle+=1; run.started_at=run.started_at or utcnow()
        if parent:
            transition_job(db,parent,JobStatus.RUNNING,reason='diagnosis_cycle_started')
        transition_case(db,case,CaseEvent.ANALYSIS_STARTED,'diagnosis_cycle_started'); db.commit()
        _refresh_feishu_card(case.id, 'diagnosis_started')

        snapshot=CaseEvidenceSnapshotBuilder().build(db,case.id)
        similar=find_similar_cases(db,case.id,limit=settings.knowledge_similarity_limit,min_score=settings.knowledge_similarity_min_score)
        persist_case_relations(db,case.id,similar)
        snapshot['similar_cases']=similar
        snapshot['knowledge']=search_knowledge_items(db,case.summary,limit=8)
        reasoner=get_diagnosis_reasoner(); decision=reasoner.reason(snapshot)
        rules=active_compiled_rules(db)
        rule_effects,rule_matches,rule_facts=RuleEngine().evaluate(snapshot,rules) if rules else ({'hypotheses':[],'known':[],'unknown':[],'excluded':[],'plan':[]},[],{})
        decision=merge_rule_effects(decision,rule_effects,rule_matches)
        decision=enrich_decision_with_history(decision,similar)
        decision.summary={**decision.summary,'known':decision.known,'unknown':decision.unknown,'excluded':decision.excluded,'knowledge_context_count':len(snapshot.get('knowledge') or []),'similar_case_count':len(similar)}
        shadow = None
        if settings.ai_shadow_enabled:
            # Best-effort side channel only: the proposal is persisted for Eval
            # and is never merged into ``decision`` or dispatched as a plan.
            from app.diagnosis.ai_proposal import run_ai_shadow
            shadow = run_ai_shadow(
                db, case_id=case.id, diagnosis_run_id=run.id, snapshot=snapshot,
                deterministic_baseline=decision.to_dict(),
            )
            decision.summary={**decision.summary, 'ai_shadow_status':shadow.status,
                              'ai_shadow_proposal_id':shadow.id}
        # AI-F01/F02/F03/F05 are a read-only assurance sidecar. It is useful in
        # development even when no external model is configured and can never
        # modify the deterministic decision or dispatch an action.
        from app.diagnosis.ai_workbench import persist_readonly_workbench
        assurance = persist_readonly_workbench(
            db, case_id=case.id, diagnosis_run_id=run.id, snapshot=snapshot,
            baseline=decision.to_dict(), proposal_record=shadow,
        )
        decision.summary={**decision.summary, 'ai_readonly_status':assurance.status,
                          'ai_readonly_record_id':assurance.id}
        if run.last_fingerprint==snapshot['fingerprint']: run.no_progress_count+=1
        else: run.no_progress_count=0
        run.last_fingerprint=snapshot['fingerprint']; run.reasoner_name=type(reasoner).__name__; run.reasoner_version=getattr(reasoner,'version','unknown'); run.prompt_version=settings.reasoning_prompt_version; run.model_name=settings.reasoning_gateway_model if settings.diagnosis_reasoner.lower()=='hybrid' else None
        persist_decision(db,run,decision)
        plan=create_plan(db,run,decision)
        run.decision_json=decision.to_dict(); run.summary_json=decision.summary; run.updated_at=utcnow()
        audit(db,case_id=case.id,event_type='DIAGNOSIS_CYCLE',target_type='diagnosis_run',target_id=run.id,
              detail={'cycle':run.cycle,'conclusion_state':decision.conclusion_state,'hypothesis_count':len(decision.hypotheses),'plan_count':len(decision.plan),'no_progress_count':run.no_progress_count,'rule_count':len(rules),'rule_matched':sum(1 for x in rule_matches if x.matched),'similar_case_count':len(similar)})

        if run.no_progress_count>=settings.diagnosis_no_progress_limit:
            summary=_standard_no_progress(decision,run); run.summary_json=summary; run.status=DiagnosisRunStatus.WAITING_USER.value
            if parent: transition_job(db,parent,JobStatus.WAITING_USER,reason='diagnosis_no_progress')
            transition_case(db,case,CaseEvent.USER_ACTION_REQUIRED,'diagnosis_no_progress'); db.commit()
            from app.integrations.feishu.feedback import build_single_user_question
            _notify_feishu_milestone(
                db, case_id=case.id, feedback_type='WAITING_USER',
                token=f'{run.id}:no-progress:{run.cycle}',
                text=build_single_user_question(decision=decision.to_dict(), summary=summary),
            )
            return {'status':'WAITING_USER','run_id':run.id,'summary':summary}

        # A sufficiently-supported hypothesis is a conclusion: honor DIAGNOSED
        # BEFORE dispatching any auto-collection plans, otherwise a recurring plan
        # (e.g. RUN_MEDIA_ANALYSIS) keeps re-dispatching -> WAITING_EVIDENCE ->
        # fingerprint unchanged -> no-progress guard stalls the run at WAITING_USER.
        if decision.conclusion_state=='DIAGNOSED':
            run.status=DiagnosisRunStatus.DIAGNOSED.value; run.finished_at=utcnow()
            if parent: transition_job(db,parent,JobStatus.SUCCESS,reason='diagnosis_supported_hypothesis')
            transition_case(db,case,CaseEvent.DIAGNOSIS_COMPLETED,'diagnosis_supported_hypothesis')
            db.flush()
            # Auto-publish: generate report + push the conclusion card to Feishu
            # (best-effort; a publish failure must not fail the diagnosis itself).
            published = _publish_diagnosed_artifacts(db, case_id=case.id, run_id=run.id)
            db.commit()
            from app.integrations.feishu.feedback import completed_text
            _notify_feishu_milestone(
                db, case_id=case.id, feedback_type='COMPLETED', token=run.id,
                text=completed_text(case.case_no, decision.summary.get('headline')),
            )
            return {'status':'DIAGNOSED','run_id':run.id,'summary':decision.summary,
                    'published':published}

        jobs=[]
        if plan: jobs=_dispatch_plan(db,run,plan,decision)
        if jobs:
            run.status=DiagnosisRunStatus.WAITING_EVIDENCE.value
            if parent: transition_job(db,parent,JobStatus.WAITING_EVIDENCE,reason='diagnosis_auto_collection_dispatched')
            transition_case(db,case,CaseEvent.EVIDENCE_REQUIRED,'diagnosis_auto_collection_dispatched'); db.commit()
            _refresh_feishu_card(case.id, 'automatic_collection_started')
            return {'status':'WAITING_EVIDENCE','run_id':run.id,'child_jobs':jobs}

        run.status=DiagnosisRunStatus.WAITING_USER.value; run.finished_at=utcnow() if decision.conclusion_state=='WAITING_USER' else None
        if parent: transition_job(db,parent,JobStatus.WAITING_USER,reason='diagnosis_requires_user_evidence')
        transition_case(db,case,CaseEvent.USER_ACTION_REQUIRED,'diagnosis_requires_user_evidence'); db.commit()
        from app.integrations.feishu.feedback import build_single_user_question
        _notify_feishu_milestone(
            db, case_id=case.id, feedback_type='WAITING_USER',
            token=f'{run.id}:cycle:{run.cycle}',
            text=build_single_user_question(decision=decision.to_dict(), summary=decision.summary),
        )
        return {'status':'WAITING_USER','run_id':run.id,'summary':decision.summary}
    except Exception as exc:
        log.exception('diagnosis cycle failed')
        try:
            run=db.get(DiagnosisRun,run_id)
            if run:
                run.status=DiagnosisRunStatus.FAILED.value; run.finished_at=utcnow()
                if run.job_id:
                    parent=db.get(Job,run.job_id)
                    if parent:
                        parent.error_code=type(exc).__name__; parent.error_message=str(exc)
                        transition_job(db,parent,JobStatus.FAILED,reason='diagnosis_engine_error')
                case=db.get(Case,run.case_id)
                if case:
                    try: transition_case(db,case,CaseEvent.CASE_FAILED,'diagnosis_engine_error')
                    except Exception: pass
                db.commit()
                if case:
                    from app.integrations.feishu.feedback import failed_text
                    _notify_feishu_milestone(
                        db, case_id=case.id, feedback_type='FAILED', token=run.id,
                        text=failed_text(case.case_no),
                    )
        except Exception: db.rollback()
        raise
    finally: db.close()


@celery_app.task(name='diagnosis.run',bind=True,autoretry_for=(),max_retries=0)
def run_diagnosis(self,run_id:str): return _execute_cycle(run_id)


@celery_app.task(name='diagnosis.resume_case',bind=True,autoretry_for=(),max_retries=0)
def resume_case(self,case_id:str):
    db=SessionLocal()
    try:
        stmt=select(DiagnosisRun).where(DiagnosisRun.case_id==case_id,DiagnosisRun.status.in_([
            DiagnosisRunStatus.WAITING_EVIDENCE.value,DiagnosisRunStatus.WAITING_USER.value,
        ])).order_by(DiagnosisRun.created_at.desc()).with_for_update(skip_locked=True)
        run=db.scalar(stmt)
        if run:
            run.status=DiagnosisRunStatus.PENDING.value; run_id=run.id; db.commit()
        else:
            run_id=None; db.rollback()
    finally: db.close()
    return _execute_cycle(run_id) if run_id else {'status':'no_waiting_diagnosis'}


def notify_case_changed(case_id:str):
    """由Collector/Analyzer完成事件调用。只投递轻量resume，不在子Worker内执行诊断。"""
    try: resume_case.apply_async(args=[case_id],queue='diagnosis',countdown=1)
    except Exception: log.exception('failed to enqueue diagnosis resume')
