from app.reports.diagnosis_report import render_report_html


def test_report_separates_known_unknown_and_evidence_discipline():
    payload={'case':{'case_no':'VOIP-1','status':'WAITING_USER','summary':'电流音'},'headline':'候选方向，需补证','known':['RTP无丢包'],'unknown':['电流音发生时间未知'],'excluded':['已排除注册失败'],
             'hypotheses':[{'title':'RTP抖动','fault_domain':'RTP','status':'OPEN','confidence':0.68,'rationale':'仅能解释卡顿'}],
             'similar_cases':[],'diagnosis_run':{'reasoner_name':'Deterministic','reasoner_version':'1','workflow_version':'m5'},'traceability':{'note':'历史Case和AI推断不能单独确认根因。'}}
    html=render_report_html(payload)
    assert '已确认事实' in html and '仍未知 / 待补证' in html and '已排除方向' in html
    assert '历史Case和AI推断不能单独确认根因' in html
    assert 'ROOT_CAUSE_CONFIRMED' not in html
