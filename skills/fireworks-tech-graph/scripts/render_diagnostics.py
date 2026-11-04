"""Structured failures shared by the SVG generator and command line."""
from __future__ import annotations

from typing import Any


class RenderError(ValueError):
    """Carry actionable diagnostic data without requiring a rendered SVG."""

    def __init__(self, message: str, diagnostic: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic
