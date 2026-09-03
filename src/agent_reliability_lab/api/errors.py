"""Stable API errors that never serialize exceptions or request values."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ApiError(Exception):
    """An intentional public failure with a stable wire representation."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, error: ApiError) -> JSONResponse:
        return error_response(
            error.status_code, error.code, error.message, error.details
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _: Request, error: RequestValidationError
    ) -> JSONResponse:
        issues = [
            {
                "location": [str(part) for part in issue.get("loc", ())],
                "code": str(issue.get("type", "validation_error")),
            }
            for issue in error.errors()
        ]
        return error_response(
            422,
            "validation_error",
            "Request validation failed.",
            {"issues": issues},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        _: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        if error.status_code == 404:
            return error_response(404, "not_found", "Resource not found.")
        if error.status_code == 405:
            return error_response(
                405, "method_not_allowed", "HTTP method is not allowed."
            )
        return error_response(error.status_code, "http_error", "HTTP request failed.")

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, __: Exception) -> JSONResponse:
        return error_response(500, "internal_error", "Internal server error.")


class RequestBodyLimitMiddleware:
    """Enforce the byte bound on both declared and streamed request bodies."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await error_response(
                413,
                "request_too_large",
                "Request body exceeds the configured limit.",
                {"limit_bytes": self.max_bytes},
            )(scope, receive, send)
            return
        if scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        parts: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, _single_message_receive(message), send)
                return
            part = message.get("body", b"")
            received += len(part)
            if received > self.max_bytes:
                await error_response(
                    413,
                    "request_too_large",
                    "Request body exceeds the configured limit.",
                    {"limit_bytes": self.max_bytes},
                )(scope, receive, send)
                return
            parts.append(part)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": b"".join(parts),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _single_message_receive(message: Message) -> Receive:
    used = False

    async def replay() -> Message:
        nonlocal used
        if used:
            return {"type": "http.disconnect"}
        used = True
        return message

    return replay


ExceptionHandler = Callable[[Request, Any], Awaitable[JSONResponse]]

__all__ = [
    "ApiError",
    "RequestBodyLimitMiddleware",
    "error_response",
    "install_error_handlers",
]
