from pathlib import Path
from app.actions.registry import ActionRegistry

def test_voip_basic_registry_loads():
    root=Path(__file__).resolve().parents[2]/'profiles'
    r=ActionRegistry(root)
    p=r.profile('voip_basic')
    assert 'GET_DEVICE_INFO' in p.actions
    assert r.action('GET_SIP_REGISTER').executor == 'aim'
    assert all(r.action(a).risk_level in {'L0','L1'} for a in p.actions)
