from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.actions.registry import ActionRegistry, RegistryError
from app.core.config import settings
from app.platforms.contracts import PlatformProfileDefinition
from app.platforms.resolvers import PARSERS


@dataclass(frozen=True)
class LoadedPlatformProfile:
    definition: PlatformProfileDefinition
    checksum: str
    source_path: Path


class PlatformProfileRegistryError(RuntimeError):
    pass


class PlatformProfileRegistry:
    def __init__(self, root: Path | None = None):
        base = Path(root or settings.profile_root)
        if not base.exists():
            base = Path(__file__).resolve().parents[3] / 'profiles'
        self.base = base
        self.root = base / 'platforms'
        self._profiles: dict[str, LoadedPlatformProfile] = {}
        self.reload()

    def reload(self):
        profiles: dict[str, LoadedPlatformProfile] = {}
        action_registry = ActionRegistry(self.base)
        for path in sorted(self.root.glob('*.yaml')):
            doc = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
            raw_profiles = doc.get('profiles')
            # Backward compatibility for the original minimal generic_openwrt.yaml.
            if raw_profiles is None and 'platform' in doc:
                continue
            for raw in raw_profiles or []:
                definition = PlatformProfileDefinition.model_validate(raw)
                if definition.id in profiles:
                    raise PlatformProfileRegistryError(f'DUPLICATE_PLATFORM_PROFILE:{definition.id}')
                for action_id in definition.readonly_actions + definition.autonomous_reproduction_actions:
                    try:
                        action_registry.action(action_id)
                    except RegistryError as exc:
                        raise PlatformProfileRegistryError(
                            f'PLATFORM_UNKNOWN_ACTION:{definition.id}:{action_id}'
                        ) from exc
                for resolver_name, resolver in {
                    **definition.voice_runtime_context,
                    **definition.realtime_event_sources,
                }.items():
                    if resolver.parser_status == 'VERIFIED' and resolver.parser_id not in PARSERS:
                        raise PlatformProfileRegistryError(
                            f'PLATFORM_RESOLVER_UNKNOWN_PARSER:{definition.id}:{resolver_name}:{resolver.parser_id}'
                        )
                    for action_id in [resolver.command_action_id, resolver.verification_action_id]:
                        if not action_id:
                            continue
                        try:
                            action_registry.action(action_id)
                        except RegistryError as exc:
                            raise PlatformProfileRegistryError(
                                f'PLATFORM_RESOLVER_UNKNOWN_ACTION:{definition.id}:{resolver_name}:{action_id}'
                            ) from exc
                profiles[definition.id] = LoadedPlatformProfile(
                    definition=definition,
                    checksum=definition.checksum(),
                    source_path=path,
                )
        self._profiles = profiles

    def get(self, platform_id: str) -> LoadedPlatformProfile:
        try:
            return self._profiles[platform_id]
        except KeyError as exc:
            raise PlatformProfileRegistryError(f'UNKNOWN_PLATFORM_PROFILE:{platform_id}') from exc

    def list(self) -> list[LoadedPlatformProfile]:
        return [self._profiles[k] for k in sorted(self._profiles)]
