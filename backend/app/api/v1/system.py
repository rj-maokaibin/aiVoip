from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_permissions
from app.contracts.enums import PermissionName
from app.release_readiness import runtime_release_readiness
from app.production_config import production_config_readiness

router = APIRouter(tags=["system"])


@router.get("/system/release-readiness")
def release_readiness(_=Depends(require_permissions(PermissionName.ADMIN))):
    return runtime_release_readiness()


@router.get("/system/production-config-readiness")
def production_config_status(_=Depends(require_permissions(PermissionName.ADMIN))):
    return production_config_readiness()
