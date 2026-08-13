from types import SimpleNamespace
import pytest
from app.services.rules import activate_rule_version

class FakeDB:
    def scalars(self,*args,**kwargs): return []
    def add(self,*args,**kwargs): pass
    def flush(self): pass

def test_rule_self_approval_is_rejected_before_db_mutation():
    d=SimpleNamespace(id='d',rule_key='R',active_version=None,enabled=1)
    v=SimpleNamespace(id='v',created_by='alice',status='DRAFT',version='1',approved_by=None,approved_at=None)
    with pytest.raises(ValueError,match='RULE_SELF_APPROVAL_NOT_ALLOWED'):
        activate_rule_version(FakeDB(),d,v,actor='alice')


def test_system_seed_can_be_approved_by_a_reviewer():
    d=SimpleNamespace(id='d',rule_key='R',active_version=None,enabled=1)
    v=SimpleNamespace(id='v',created_by='system',status='DRAFT',version='1',approved_by=None,approved_at=None)

    activate_rule_version(FakeDB(),d,v,actor='reviewer')

    assert v.status=='ACTIVE'
    assert v.approved_by=='reviewer'
