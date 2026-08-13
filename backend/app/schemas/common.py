from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar('T')


class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
