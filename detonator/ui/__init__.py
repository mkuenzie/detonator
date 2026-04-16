"""UI layer — server-rendered Jinja2 + HTMX pages over the detonator API."""

from __future__ import annotations

from detonator.ui.routes import mount_ui

__all__ = ["mount_ui"]
