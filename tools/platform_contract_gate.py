#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from app.platforms.registry import PlatformProfileRegistry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform-id', default='RUIJIE_VOIP_AIM_V1')
    parser.add_argument('--capability', default='AUTONOMOUS_REPRODUCTION')
    parser.add_argument('--require-production-ready', action='store_true')
    args = parser.parse_args()

    registry = PlatformProfileRegistry(ROOT / 'profiles')
    loaded = registry.get(args.platform_id)
    definition = loaded.definition
    blocking = definition.blocking_gaps_for(args.capability)
    result = {
        'status': 'PASS' if not args.require_production_ready else ('PASS' if definition.production_ready_for(args.capability) else 'BLOCKED'),
        'platform_id': definition.id,
        'version': definition.version,
        'profile_status': definition.status.value,
        'checksum': loaded.checksum,
        'capability': args.capability,
        'production_ready': definition.production_ready_for(args.capability),
        'readonly_actions': definition.readonly_actions,
        'autonomous_reproduction_actions': definition.autonomous_reproduction_actions,
        'blocking_gaps': [gap.model_dump(mode='json') for gap in blocking],
        'all_gaps': [gap.model_dump(mode='json') for gap in definition.gaps],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'PASS' else 3


if __name__ == '__main__':
    raise SystemExit(main())
