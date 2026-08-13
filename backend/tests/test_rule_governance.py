from types import SimpleNamespace
import pytest
from app.services.rules import activate_rule_version

class FakeDB:
    def scalars(self,*args,**kwargs): return []

def test_rule_self_approval_is_rejected_before_db_mutation():
    d=SimpleNamespace(id='d',rule_key='R',active_version=None,enabled=1)
    v=SimpleNamespace(id='v',created_by='alice',status='DRAFT',version='1',approved_by=None,approved_at=None)
    with pytest.raises(ValueError,match='RULE_SELF_APPROVAL_NOT_ALLOWED'):
        activate_rule_version(FakeDB(),d,v,actor='alice')
