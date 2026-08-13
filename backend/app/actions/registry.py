from dataclasses import dataclass, field
from pathlib import Path
import yaml
from app.core.config import settings

ALLOWED_RISKS={'L0','L1','L2','L3','L4'}
ALLOWED_EXECUTORS={'shell','aim','mock'}
ALLOWED_CONTRACT_STATUS={'LEGACY','VERIFIED','PROVISIONAL','RESERVED','DISABLED'}


@dataclass(frozen=True)
class ActionDefinition:
    id: str
    risk_level: str
    executor: str
    command: str
    evidence_type: str
    timeout: float = 10.0
    description: str = ''
    cleanup_action: str | None = None
    cleanup_required: bool = False
    cleanup_idempotent: bool = False
    parameters: dict | None = None
    version: str = '1.0.0'
    contract_status: str = 'LEGACY'
    preconditions: list[str] = field(default_factory=list)
    success: dict | None = None
    retry: dict | None = None
    cleanup_verification: dict | None = None
    source_refs: list[str] = field(default_factory=list)
    supported_platforms: list[str] = field(default_factory=list)

    @property
    def executable(self) -> bool:
        return self.contract_status not in {'RESERVED', 'DISABLED'}


@dataclass(frozen=True)
class CollectProfile:
    id: str
    actions: list[str]
    description: str=''


class RegistryError(RuntimeError):
    pass


class ActionRegistry:
    def __init__(self, profile_root:Path|None=None):
        self.root=profile_root or settings.profile_root
        self.actions={}
        self.profiles={}
        self.reload()

    def reload(self):
        self.actions={}
        self.profiles={}
        for path in sorted((self.root/'actions').glob('*.yaml')):
            doc=yaml.safe_load(path.read_text(encoding='utf-8')) or {}
            for item in doc.get('actions',[]):
                action_id=item['id']
                if action_id in self.actions:
                    raise RegistryError(f'DUPLICATE_ACTION:{action_id}')
                if item['risk_level'] not in ALLOWED_RISKS:
                    raise RegistryError(f"INVALID_RISK:{action_id}")
                if item['executor'] not in ALLOWED_EXECUTORS:
                    raise RegistryError(f"INVALID_EXECUTOR:{action_id}")
                status=item.get('contract_status','LEGACY')
                if status not in ALLOWED_CONTRACT_STATUS:
                    raise RegistryError(f'INVALID_CONTRACT_STATUS:{action_id}:{status}')
                self.actions[action_id]=ActionDefinition(**item)
        for path in sorted((self.root/'collect').glob('*.yaml')):
            doc=yaml.safe_load(path.read_text(encoding='utf-8')) or {}
            for item in doc.get('profiles',[]):
                profile_id=item['id']
                if profile_id in self.profiles:
                    raise RegistryError(f'DUPLICATE_COLLECT_PROFILE:{profile_id}')
                self.profiles[profile_id]=CollectProfile(**item)
        for profile in self.profiles.values():
            missing=[a for a in profile.actions if a not in self.actions]
            if missing:
                raise RegistryError(f"PROFILE_MISSING_ACTIONS:{profile.id}:{missing}")
            blocked=[a for a in profile.actions if not self.actions[a].executable]
            if blocked:
                raise RegistryError(f'PROFILE_CONTAINS_NON_EXECUTABLE_ACTIONS:{profile.id}:{blocked}')

    def action(self, action_id, *, require_executable: bool = True):
        try:
            action=self.actions[action_id]
        except KeyError as exc:
            raise RegistryError(f'UNKNOWN_ACTION:{action_id}') from exc
        if require_executable and not action.executable:
            raise RegistryError(f'ACTION_NOT_EXECUTABLE:{action_id}:{action.contract_status}')
        return action

    def profile(self, profile_id):
        try:
            return self.profiles[profile_id]
        except KeyError as exc:
            raise RegistryError(f'UNKNOWN_PROFILE:{profile_id}') from exc
