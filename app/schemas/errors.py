"""
schemas/errors.py — Structured error response model

Shared by HTTPException and RequestValidationError handlers in main.py.
"""

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error: str
    status_code: int
    request_id: str = ""
    detail: list | None = None
