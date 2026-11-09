"""Event-driven runtime.json writer with Compare-And-Swap (CAS) config.json mirroring."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("openhop.meshhub-bot.runtime_writer")


class RuntimeWriter:
    """Publishes live telemetry to data/runtime.json and safely mirrors to data/config.json."""

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
        self._last_mirror_mono = 0.0
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
        self.write_once(force_mirror=True)
        while self._running:
            try:
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(), timeout=self.heartbeat_seconds
                    )
                    woken = True
                except asyncio.TimeoutError:
                    woken = False
                self._wake_event.clear()
                if not self._running:
                    break
                self.write_once(force_mirror=woken)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in RuntimeWriter loop: %s", exc, exc_info=True)
                await asyncio.sleep(2.0)

    def write_once(self, *, force_mirror: bool = False) -> bool:
        """Atomically write runtime.json and conditionally CAS-mirror into config.json."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = self.hub.get_runtime_payload()
            payload_no_ts = {k: v for k, v in payload.items() if k != "timestamp"}
            canonical = json.dumps(payload_no_ts, sort_keys=True, default=str)
            changed = canonical != self._last_payload_json
            self._last_payload_json = canonical

            self._atomic_write_json(self.runtime_file, payload)

            now_mono = time.monotonic()
            should_mirror = (
                force_mirror
                or changed
                or (now_mono - self._last_mirror_mono) >= self.mirror_min_interval
            )
            if should_mirror and self.config_file.is_file():
                if self._cas_mirror_to_config(payload):
                    self._last_mirror_mono = now_mono
            return True
        except Exception as exc:
            logger.error("Failed writing runtime snapshot: %s", exc, exc_info=True)
            return False

    def _cas_mirror_to_config(self, runtime_payload: dict[str, Any]) -> bool:
        """Mirror _runtime into config.json only if the file has not changed concurrently."""
        try:
            st_before = self.config_file.stat()
            raw_bytes = self.config_file.read_bytes()
            current_cfg = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(current_cfg, dict):
                return False

            # If a new _cmd is sitting in config.json that hasn't been processed yet,
            # do not touch config.json until ConfigWatcher processes it!
            cmd_obj = current_cfg.get("_cmd")
            if isinstance(cmd_obj, dict) and cmd_obj.get("id"):
                if not self.hub.is_cmd_processed(str(cmd_obj["id"])):
                    return False

            # Exclude full activity log from the config.json mirror to keep config.json compact
            compact_runtime = dict(runtime_payload)
            if "activity" in compact_runtime and isinstance(compact_runtime["activity"], list):
                compact_runtime["activity"] = compact_runtime["activity"][:15]
            current_cfg["_runtime"] = compact_runtime

            # Remove already-processed _cmd so config.json stays clean
            if isinstance(cmd_obj, dict) and cmd_obj.get("id"):
                if self.hub.is_cmd_processed(str(cmd_obj["id"])):
                    current_cfg.pop("_cmd", None)

            # Re-stat immediately before atomic replace to detect any concurrent write
            st_check = self.config_file.stat()
            if (
                st_check.st_mtime_ns != st_before.st_mtime_ns
                or st_check.st_size != st_before.st_size
            ):
                logger.debug("Skipping _runtime mirror due to concurrent config.json update")
                return False

            self._atomic_write_json(self.config_file, current_cfg)
            return True
        except Exception as exc:
            logger.debug("CAS mirror skipped: %s", exc)
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
