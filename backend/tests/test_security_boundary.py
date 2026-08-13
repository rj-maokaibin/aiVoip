from pathlib import Path
import pytest
from app.actions.registry import ActionRegistry, RegistryError

def test_unknown_action_rejected():
    r=ActionRegistry(Path(__file__).resolve().parents[2]/'profiles')
    with pytest.raises(RegistryError): r.action('rm -rf /')
