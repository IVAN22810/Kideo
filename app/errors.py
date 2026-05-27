"""Stripe-style error envelope for the Minor API.

All API errors render as:
    {"error": {"type": "...", "code": "...", "message": "..."}}

This is the developer-facing contract — integrators reading our docs should see
the exact same shape they see from Stripe, Plaid, Mercury, etc.
"""
from fastapi import Request
from fastapi.responses import JSONResponse


class MinorAPIError(Exception):
    """Raised by routers/services to emit a Stripe-style error response."""

    def __init__(self, *, status_code: int, type: str, code: str, message: str) -> None:
        self.status_code = status_code
        self.type = type
        self.code = code
        self.message = message
        super().__init__(message)


async def minor_api_error_handler(request: Request, exc: MinorAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "type": exc.type,
                "code": exc.code,
                "message": exc.message,
            }
        },
    )
