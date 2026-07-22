"""Shared Pydantic schema pieces.

Every response is wrapped in the same envelope as the Flask API:
    { "success": bool, "message": str, "data": ... }
so the existing frontend keeps working unchanged.
"""
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORMModel(BaseModel):
    """Base for schemas read from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class Envelope(BaseModel, Generic[T]):
    success: bool = True
    message: str = "OK"
    data: T | None = None


def ok(data: Any = None, message: str = "OK") -> dict:
    return {"success": True, "message": message, "data": data}
