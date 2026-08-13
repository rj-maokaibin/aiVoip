"""Minimal import-only stubs for source-contract tooling.

These stubs are used only when optional runtime dependencies are absent in the analysis
container. They are never installed into the application package and never make runtime
health checks pass. Production/full-stack gates still require the real dependencies.
"""
from __future__ import annotations

import logging
import os
import sys
import types


def install() -> None:
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    os.environ.setdefault("AUTH_ALLOW_ANONYMOUS_DEV", "true")

    if "celery" not in sys.modules:
        try:
            __import__("celery")
        except ImportError:
            celery = types.ModuleType("celery")

            class _Conf:
                def update(self, **_kwargs):
                    return None

            class Celery:
                def __init__(self, *_args, **_kwargs):
                    self.conf = _Conf()

                def task(self, *_args, **_kwargs):
                    def deco(fn):
                        fn.delay = lambda *a, **k: None
                        fn.apply_async = lambda *a, **k: None
                        return fn
                    return deco

            celery.Celery = Celery
            sys.modules["celery"] = celery
            utils = types.ModuleType("celery.utils")
            log = types.ModuleType("celery.utils.log")
            log.get_task_logger = logging.getLogger
            sys.modules["celery.utils"] = utils
            sys.modules["celery.utils.log"] = log

    if "redis" not in sys.modules:
        try:
            __import__("redis")
        except ImportError:
            redis = types.ModuleType("redis")

            class Redis:
                @classmethod
                def from_url(cls, *_args, **_kwargs):
                    return cls()

                def ping(self):
                    raise RuntimeError("OFFLINE_IMPORT_STUB")

                def close(self):
                    return None

            redis.Redis = Redis
            sys.modules["redis"] = redis

    if "asyncssh" not in sys.modules:
        try:
            __import__("asyncssh")
        except ImportError:
            asyncssh = types.ModuleType("asyncssh")
            asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

            async def _connect(*_args, **_kwargs):
                raise RuntimeError("OFFLINE_IMPORT_STUB")

            asyncssh.connect = _connect
            sys.modules["asyncssh"] = asyncssh
