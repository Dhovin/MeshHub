"""Event-driven runtime.json writer for openHop Repeater telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("openhop.meshhub-bot.runtime_writer")


class RuntimeWriter:
    """Publishes live telemetry strictly to data/runtime.json (never modifying config.json)."""

    def __init__(
        self,
        hub: Any,
        *,
        heartbeat_seconds: float = 15.0,
        mirror_min_interval: float = 15.0,
    ):
        self.hub = hub
        self.heartbeat_seconds = max(2.0, float(heartbeat_seconds))
        self.mirror_min_interval = max(2.0, float(mirror_min_interval))
        self.data_dir = Path(hub.get_data_dir())
        self.runtime_file = self.data_dir / "runtime.json"
        self.config_file = self.data_dir / "config.json"

        self._running = False
        self._task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        self._last_payload_json = ""

    def notify_change(self) -> None:
        """Signal the writer loop to immediately flush an updated runtime snapshot."""
        self._wake_event.set()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("RuntimeWriter started (publishing to %s)", self.runtime_file)

    async def stop(self) -> None:
        self._running = False
        self._wake_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=1.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._task = None

    async def _loop(self) -> None:
        # Write an initial snapshot right away
        self.write_once()
        while self._running:
            try:
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(), timeout=self.heartbeat_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                self._wake_event.clear()
                if not self._running:
                    break
                self.write_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in RuntimeWriter loop: %s", exc, exc_info=True)
                await asyncio.sleep(2.0)

    def write_once(self, *, force_mirror: bool = False) -> bool:
        """Atomically write runtime.json."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = self.hub.get_runtime_payload()
            payload_no_ts = {k: v for k, v in payload.items() if k != "timestamp"}
            canonical = json.dumps(payload_no_ts, sort_keys=True, default=str)
            self._last_payload_json = canonical

            self._atomic_write_json(self.runtime_file, payload)
            return True
        except Exception as exc:
            logger.error("Failed writing runtime snapshot: %s", exc, exc_info=True)
            return False

    @staticmethod
    def _atomic_write_json(target: Path, data: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str)
                fh.write("\n")
                fh.flush()
            os.replace(tmp_path, target)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
