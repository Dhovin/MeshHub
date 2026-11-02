import asyncio
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("openhop.meshhub-bot.runtime_writer")

class RuntimeWriter:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.runtime_file = self.data_dir / "runtime.json"
        self.config_file = self.data_dir / "config.json"

    def write_runtime(self, payload: dict) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp_path = self.data_dir / f".runtime.{os.getpid()}.tmp"
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp_path.replace(self.runtime_file)

            # Dual State Publishing fallback: write _runtime under config.json
            try:
                cfg = {}
                if self.config_file.exists():
                    try:
                        loaded = json.loads(self.config_file.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            cfg = loaded
                    except Exception:
                        pass
                cfg["_runtime"] = payload
                cfg_temp = self.data_dir / f".config.{os.getpid()}.tmp"
                cfg_temp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                cfg_temp.replace(self.config_file)
            except Exception as e:
                logger.debug("Dual publishing to config.json skipped: %s", e)
        except Exception as exc:
            logger.error("Error writing runtime telemetry: %s", exc)

    async def run(self, hub, stop_event: asyncio.Event, interval: float = 3.0) -> None:
        while not stop_event.is_set():
            try:
                if hasattr(hub, "get_runtime_payload"):
                    payload = hub.get_runtime_payload()
                    self.write_runtime(payload)
            except Exception as exc:
                logger.error("Error updating runtime telemetry: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
