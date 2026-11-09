"""Base module interface for static module registry and live hot-reload."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..hub import BotHub


class BaseModule:
    """Base contract for all MeshHub Bot modules."""

    def __init__(self, name: str, hub: BotHub, config: dict[str, Any]):
        self.name = name
        self.hub = hub
        self.config: dict[str, Any] = {}
        self.enabled: bool = False
        self.apply_config(config)

    def apply_config(self, config: dict[str, Any]) -> None:
        """Apply or hot-reload configuration in place without losing state."""
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", False))

    def required_channels(self) -> list[str]:
        """Return #channel names required by this module when enabled."""
        if not self.enabled:
            return []
        return []

    async def start(self) -> None:
        """Start background timers/schedules."""

    async def stop(self) -> None:
        """Stop background timers/schedules cleanly."""

    async def on_message(self, msg: dict[str, Any]) -> None:
        """Handle an incoming channel or direct message event."""

    def get_runtime_state(self) -> dict[str, Any]:
        """Return JSON-serializable state for runtime.json / Web UI."""
        return {"enabled": self.enabled}

    def get_help_segment(self) -> str:
        """Return compact help description for !help replies."""
        return ""
