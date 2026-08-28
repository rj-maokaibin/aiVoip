#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import shlex


async def _resolve(*, sn: str, ip: str, product: str) -> str:
    from app.integrations.credentials import get_credential_provider

    provider = get_credential_provider()
    provider_id = str(getattr(provider, "provider_id", type(provider).__name__))
    if not bool(getattr(provider, "production_capable", False)):
        raise SystemExit(
            f"SIP_ABA_CREDENTIAL_PROVIDER_NOT_PRODUCTION_CAPABLE provider={provider_id}"
        )
    password = await provider.get_password(sn=sn, ip=ip, product=product or None)
    if not str(password or ""):
        raise SystemExit("SIP_ABA_CREDENTIAL_PROVIDER_EMPTY_PASSWORD")
    return str(password)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sn", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--product", required=True)
    args = parser.parse_args()

    password = asyncio.run(_resolve(sn=args.sn, ip=args.ip, product=args.product))
    # Caller redirects stdout directly into a 0600 private runtime file.
    # Never print metadata alongside this line and never upload the file.
    print(f"SIP_ABA_SSH_PASSWORD={shlex.quote(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
