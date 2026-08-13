from __future__ import annotations

import uuid

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, error_definition


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id") or str(uuid.uuid4())


def _payload(*, request: Request, code: str, message: str | None = None, details: dict | None = None, http_status: int | None = None):
    definition=error_definition(code,http_status=http_status)
    return {
        'error': {
            'code': code,
            'message': message or definition.default_message,
            'retryable': definition.retryable,
            'category': definition.category.value,
            'details': details or {},
            'trace_id': _trace_id(request),
        }
    }


async def trace_id_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    return response


async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.http_status, content=exc.as_payload(_trace_id(request)))


async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=_payload(
            request=request,
            code='REQUEST_VALIDATION_FAILED',
            details={'errors': exc.errors()},
            http_status=422,
        ),
    )


async def http_exception_handler(request: Request, exc: HTTPException | StarletteHTTPException):
    # Convert FastAPI/Starlette HTTPException usage into the frozen V1 error envelope.
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        code = str(detail["code"])
        message = str(detail.get("message") or error_definition(code, http_status=exc.status_code).default_message)
        details = dict(detail.get("details") or {})
    else:
        text = str(detail)
        code = text if text and text.upper() == text and " " not in text else ("ROUTE_NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR")
        message = error_definition(code, http_status=exc.status_code).default_message if code != "HTTP_ERROR" else text
        details = {} if code != "HTTP_ERROR" else {"legacy_detail": text}
    payload = _payload(request=request,code=code,message=message,details=details,http_status=exc.status_code)
    return JSONResponse(status_code=exc.status_code, content=payload, headers=exc.headers or {})


async def unhandled_exception_handler(request: Request, exc: Exception):
    # Never expose traceback/exception strings through the public API contract.
    return JSONResponse(
        status_code=500,
        content=_payload(request=request,code='INTERNAL_ERROR',details={},http_status=500),
    )
