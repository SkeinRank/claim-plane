"""Existing offset pagination behavior used by the technical-preview demo."""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def page(items: list[T], *, offset: int = 0, limit: int = 20) -> list[T]:
    """Return one bounded page without mutating the input list."""

    if offset < 0:
        raise ValueError("offset must be non-negative")
    if limit <= 0:
        raise ValueError("limit must be positive")
    return items[offset : offset + limit]
