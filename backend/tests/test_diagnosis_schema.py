from datetime import datetime, timezone
from app.schemas.diagnosis import HypothesisOut

def test_hypothesis_api_normalizes_basis_points_to_score():
    d=HypothesisOut.model_validate({'id':'h','case_id':'c','diagnosis_run_id':None,'code':'X','title':'x','fault_domain':'x','status':'OPEN','confidence':6840,'rationale':None,'confirmable':1,'confirm_rule':None,'created_at':datetime.now(timezone.utc),'updated_at':datetime.now(timezone.utc)})
    assert d.confidence==0.684 and d.confirmable is True
