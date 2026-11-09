"""Central BotHub coordinating lifecycle, ConfigWatcher, TX queue, modules, and command mailbox."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import (
    canonical_config_hash,
    load_config_file,
    migrate_config,
    redact_config,
    validate_timezone,
)
from . core.rate_limiter import PerUserRateLimiter
from .core.scheduler import Scheduler
from .mesh.client import MeshCompanionClient
from .mesh.text import MAX_LORA_BYTES, clamp_lora_message
from .mesh.txqueue import PRIORITY_REPLY, OutboundTxQueue, TxJob
from .modules.autoresponder import AutoresponderModule
from .modules.net_engine import NetSessionEngine
from .modules.weather import WeatherBotModule
from .web.runtime_writer import RuntimeWriter

logger = logging.getLogger("openhop.meshhub-bot.hub")

ALLOWED_COMMANDS = frozenset(
    {
        "net_start",
        "net_stop",
        "net_clear",
        "net_announce",
        "weather_broadcast_now",
        "weather_check_alerts_now",
        "weather_sub_add",
        "weather_sub_remove",
        "channels_provision",
        "send_advert",
    }
)


class BotHub:
    """Central runtime hub for openHop MeshHub Bot."""

    def __init__(self, config_path: str | None = None, data_dir: str | None = None):
        self.data_dir = os.path.abspath(
            data_dir
            or os.environ.get("OPENHOP_PLUGIN_DATA")
            or (os.path.dirname(os.path.abspath(config_path)) if config_path else ".")
        )
        os.makedirs(self.data_dir, exist_ok=True)
        self.config_path = os.path.abspath(
            config_path or os.path.join(self.data_dir, "config.json")
        )

        self.config, raw_disk = load_config_file(self.config_path)
        self._last_config_hash = canonical_config_hash(self.config)

        core_cfg = self.config.get("core", {})
        self.timezone: str = validate_timezone(core_cfg.get("timezone"), fallback="UTC")
        self.activity_enabled: bool = bool(core_cfg.get("activityLog", True))

        self.user_limiter = PerUserRateLimiter(
            float(core_cfg.get("perUserRateLimitSeconds", 30))
        )
        self.scheduler = Scheduler()
        self.scheduler.timezone = self.timezone

        conn_cfg = self.config.get("connection", {})
        self.mesh_client = MeshCompanionClient(
            host=str(conn_cfg.get("host", "127.0.0.1")),
            port=int(conn_cfg.get("tcpPort", 5051)),
            on_message=self._on_incoming_message,
            on_status_change=self.notify_state_change,
        )
        self.mesh_client.set_on_connected_hook(self._on_companion_connected)

        self.tx_queue = OutboundTxQueue(
            send_fn=self.mesh_client.send_raw_tx,
            min_interval_seconds=float(core_cfg.get("txRateLimitSeconds", 1.5)),
            on_tx_callback=self._on_tx_completed,
        )

        # Persisted bot state (processed command IDs + recent cmd_results)
        self._bot_state_path = os.path.join(self.data_dir, "bot_state.json")
        self._activity_path = os.path.join(self.data_dir, "activity.json")
        self._processed_cmd_ids: deque[str] = deque(maxlen=100)
        self._cmd_results: deque[dict[str, Any]] = deque(maxlen=25)
        self._activity: deque[dict[str, Any]] = deque(maxlen=40)
        self._load_bot_state()

        # Instantiate static module registry
        mods_cfg = self.config.get("modules", {})
        self.modules = {
            "autoresponce": AutoresponderModule(self, mods_cfg.get("autoresponce", {})),
            "weather_bot": WeatherBotModule(self, mods_cfg.get("weather_bot", {})),
            "net_bot": NetSessionEngine(
                "net_bot",
                self,
                mods_cfg.get("net_bot", {}),
                track_all_chat=False,
                default_channel="#net",
                default_day="Tuesday",
                default_time="19:00",
                default_duration=2.0,
                default_keyword="#checkin",
                default_state_file="net_state.json",
            ),
            "meshwars_bot": NetSessionEngine(
                "meshwars_bot",
                self,
                mods_cfg.get("meshwars_bot", {}),
                track_all_chat=True,
                default_channel="#meshwars",
                default_day="Saturday",
                default_time="18:00",
                default_duration=6.0,
                default_keyword="!checkin",
                default_state_file="meshwars_state.json",
            ),
        }

        self.runtime_writer = RuntimeWriter(self)
        self._watcher_task: asyncio.Task | None = None
        self._running = False
        self.shutdown_event = asyncio.Event()

    def get_data_dir(self) -> str:
        return self.data_dir

    # ------------------------------------------------------------------
    # State & Activity helpers
    # ------------------------------------------------------------------

    def _load_bot_state(self) -> None:
        if os.path.isfile(self._bot_state_path):
            try:
                with open(self._bot_state_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    for cid in data.get("processed_cmd_ids", [])[-100:]:
                        self._processed_cmd_ids.append(str(cid))
                    for res in data.get("cmd_results", [])[-25:]:
                        if isinstance(res, dict):
                            self._cmd_results.append(res)
            except Exception:
                pass
        if os.path.isfile(self._activity_path):
            try:
                with open(self._activity_path, "r", encoding="utf-8") as fh:
                    items = json.load(fh)
                if isinstance(items, list):
                    for entry in items[:40]:
                        if isinstance(entry, dict):
                            self._activity.append(entry)
            except Exception:
                pass

    def _save_bot_state(self) -> None:
        try:
            tmp = f"{self._bot_state_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "processed_cmd_ids": list(self._processed_cmd_ids),
                        "cmd_results": list(self._cmd_results),
                    },
                    fh,
                    indent=2,
                )
            os.replace(tmp, self._bot_state_path)
        except Exception as exc:
            logger.debug("Could not save bot_state.json: %s", exc)

    def _save_activity(self) -> None:
        try:
            tmp = f"{self._activity_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(list(self._activity), fh, indent=2)
            os.replace(tmp, self._activity_path)
        except Exception:
            pass

    def is_cmd_processed(self, cmd_id: str) -> bool:
        return str(cmd_id) in self._processed_cmd_ids

    def record_activity(
        self,
        *,
        module: str,
        event: str,
        sender: str = "",
        channel: str = "",
        detail: str = "",
    ) -> None:
        if not self.activity_enabled:
            return
        try:
            tz = ZoneInfo(self.timezone)
        except Exception:
            tz = ZoneInfo("UTC")
        entry = {
            "ts": int(time.time()),
            "time": datetime.now(tz).strftime("%H:%M:%S"),
            "module": str(module),
            "event": str(event),
            "sender": str(sender),
            "channel": str(channel),
            "detail": str(detail)[:200],
        }
        self._activity.appendleft(entry)
        self._save_activity()
        self.notify_state_change()

    def _on_tx_completed(self, job: TxJob, result: dict[str, Any]) -> None:
        if isinstance(result, dict) and "error" in result:
            logger.warning(
                "TX error (%s -> %s): %s", job.module, job.target, result.get("error")
            )

    def notify_state_change(self) -> None:
        if hasattr(self, "runtime_writer") and self.runtime_writer:
            self.runtime_writer.notify_change()

    # ------------------------------------------------------------------
    # Module helper methods
    # ------------------------------------------------------------------

    def is_self(self, sender_name: str) -> bool:
        return self.mesh_client.is_self_sender(sender_name)

    def can_send_user(self, sender: str) -> bool:
        return self.user_limiter.can_send(sender)

    def record_user_send(self, sender: str) -> None:
        self.user_limiter.record_send(sender)

    def matches_channel(self, incoming_channel: int | str | None, target_channel: str | int) -> bool:
        return self.mesh_client.matches_channel(incoming_channel, target_channel)

    def resolve_channel_target(
        self, channel_name: str, fallback: int | str | None = None
    ) -> int | str:
        idx = self.mesh_client.resolve_channel_index(channel_name)
        if idx is not None:
            return idx
        if fallback is not None:
            return fallback
        return channel_name

    def collect_required_channels(self) -> list[str]:
        required: list[str] = []
        for mod in self.modules.values():
            for ch in mod.required_channels():
                if ch and ch not in required:
                    required.append(ch)
        return required

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start RuntimeWriter, ConfigWatcher, TX queue, Scheduler, Modules, and Companion."""
        self._running = True
        self.scheduler.start()
        self.tx_queue.start()

        # Start RuntimeWriter and ConfigWatcher BEFORE connecting to companion
        # so the Web UI is live and responsive even if the companion TCP port is offline.
        self.runtime_writer.start()
        self._watcher_task = asyncio.create_task(self._config_watcher_loop())

        for mod in self.modules.values():
            await mod.start()

        # Check if there was a pending _cmd on disk at startup
        await self._check_disk_config_once()

        # Start non-blocking companion TCP connection loop
        self.mesh_client.start_background_connect()
        logger.info("MeshHub Bot hub started (timezone=%s)", self.timezone)

    async def stop(self) -> None:
        """Gracefully stop all tasks within the supervisor's 5-second SIGTERM budget."""
        logger.info("Stopping MeshHub Bot hub...")
        self._running = False
        self.shutdown_event.set()

        if self._watcher_task and not self._watcher_task.done():
            self._watcher_task.cancel()
            try:
                await asyncio.wait_for(self._watcher_task, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        self.scheduler.stop()
        await asyncio.gather(
            *(mod.stop() for mod in self.modules.values()), return_exceptions=True
        )
        await self.tx_queue.stop()
        await self.mesh_client.disconnect(stop_loop=True)
        self.runtime_writer.write_once(force_mirror=False)
        await self.runtime_writer.stop()
        logger.info("MeshHub Bot hub stopped cleanly.")

    async def _on_companion_connected(self) -> None:
        """Hook called every time the Virtual Companion TCP session completes handshake."""
        req_channels = self.collect_required_channels()
        if req_channels:
            await self.mesh_client.provision_channels(req_channels)
        self.notify_state_change()

    # ------------------------------------------------------------------
    # ConfigWatcher & Command Mailbox
    # ------------------------------------------------------------------

    async def _config_watcher_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(1.0)
                await self._check_disk_config_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ConfigWatcher error: %s", exc, exc_info=True)

    async def _check_disk_config_once(self) -> None:
        if not os.path.isfile(self.config_path):
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            return
        if not isinstance(raw, dict):
            return

        # 1. Process idempotent _cmd mailbox if present
        cmd_obj = raw.get("_cmd")
        if isinstance(cmd_obj, dict) and cmd_obj.get("id"):
            cmd_id = str(cmd_obj["id"]).strip()
            if cmd_id and not self.is_cmd_processed(cmd_id):
                await self.execute_mailbox_command(cmd_obj)

        # 2. Check if settings (excluding _runtime and _cmd) changed
        migrated, _ = migrate_config(raw)
        clean = redact_config(migrated, keep_runtime=False)
        new_hash = canonical_config_hash(clean)
        if new_hash != self._last_config_hash:
            logger.info("Detected configuration change in %s; hot-reloading...", self.config_path)
            self._last_config_hash = new_hash
            await self.apply_config_update(clean)

    async def apply_config_update(self, new_config: dict[str, Any]) -> None:
        """Hot-reload core, connection, and module settings in place."""
        prev_conn = dict(self.config.get("connection", {}))
        self.config = new_config

        core_cfg = self.config.get("core", {})
        self.timezone = validate_timezone(core_cfg.get("timezone"), fallback="UTC")
        self.scheduler.timezone = self.timezone
        self.activity_enabled = bool(core_cfg.get("activityLog", True))
        self.user_limiter.seconds = max(
            0.0, float(core_cfg.get("perUserRateLimitSeconds", 30))
        )
        self.tx_queue.set_min_interval(float(core_cfg.get("txRateLimitSeconds", 1.5)))

        mods_cfg = self.config.get("modules", {})
        for mod_name, mod in self.modules.items():
            mod.apply_config(mods_cfg.get(mod_name, {}))

        # Re-provision any newly required channels on the companion (additive-only)
        if self.mesh_client.is_connected:
            req_channels = self.collect_required_channels()
            if req_channels:
                await self.mesh_client.provision_channels(req_channels)

        # Reconnect companion if host or tcpPort changed
        new_conn = self.config.get("connection", {})
        if (
            new_conn.get("host") != prev_conn.get("host")
            or new_conn.get("tcpPort") != prev_conn.get("tcpPort")
        ):
            await self.mesh_client.update_endpoint(
                str(new_conn.get("host", "127.0.0.1")),
                int(new_conn.get("tcpPort", 5051)),
            )

        self.notify_state_change()

    async def execute_mailbox_command(self, cmd_obj: dict[str, Any]) -> dict[str, Any]:
        """Execute an allowlisted command from the Web UI mailbox idempotently."""
        cmd_id = str(cmd_obj.get("id", "")).strip()
        cmd_type = str(cmd_obj.get("type", "")).strip()
        params = cmd_obj.get("params") if isinstance(cmd_obj.get("params"), dict) else {}

        if cmd_id:
            self._processed_cmd_ids.append(cmd_id)

        if cmd_type not in ALLOWED_COMMANDS:
            outcome = {
                "id": cmd_id,
                "type": cmd_type,
                "ok": False,
                "message": f"Unsupported command type: {cmd_type}",
                "ts": int(time.time()),
            }
            self._cmd_results.appendleft(outcome)
            self._save_bot_state()
            self.notify_state_change()
            return outcome

        logger.info("Executing Web UI command %s (id=%s, params=%s)", cmd_type, cmd_id, params)
        try:
            res: dict[str, Any]
            if cmd_type in ("net_start", "net_stop", "net_clear", "net_announce"):
                mod_key = str(params.get("module") or "net_bot").strip()
                if mod_key not in ("net_bot", "meshwars_bot"):
                    res = {"error": f"Invalid net module: {mod_key}"}
                else:
                    net_mod: NetSessionEngine = self.modules[mod_key]  # type: ignore[assignment]
                    if cmd_type == "net_start":
                        res = await net_mod.start_net_manual()
                    elif cmd_type == "net_stop":
                        res = await net_mod.stop_net_manual(broadcast=True)
                    elif cmd_type == "net_clear":
                        res = net_mod.clear_roster_manual()
                    else:
                        res = await net_mod.announce_reminder_manual()

            elif cmd_type == "weather_broadcast_now":
                wx_mod: WeatherBotModule = self.modules["weather_bot"]  # type: ignore[assignment]
                res = await wx_mod.broadcast_now()

            elif cmd_type == "weather_check_alerts_now":
                wx_mod = self.modules["weather_bot"]  # type: ignore[assignment]
                res = await wx_mod.check_alerts_now()

            elif cmd_type == "weather_sub_add":
                wx_mod = self.modules["weather_bot"]  # type: ignore[assignment]
                res = await wx_mod.add_subscriber_manual(
                    str(params.get("recipient", "")),
                    str(params.get("zipCode", "")),
                )

            elif cmd_type == "weather_sub_remove":
                wx_mod = self.modules["weather_bot"]  # type: ignore[assignment]
                res = wx_mod.remove_subscriber_manual(str(params.get("recipient", "")))

            elif cmd_type == "channels_provision":
                extra = params.get("channels") if isinstance(params.get("channels"), list) else []
                req = self.collect_required_channels() + [str(c) for c in extra if c]
                if not self.mesh_client.is_connected:
                    res = {"error": "Virtual Companion is not connected"}
                else:
                    mapped = await self.mesh_client.provision_channels(req)
                    res = {
                        "ok": True,
                        "message": f"Provisioned/verified {len(mapped)} channels",
                        "channels": mapped,
                    }

            elif cmd_type == "send_advert":
                res = await self.mesh_client.send_advert(flood=True)
            else:
                res = {"error": "Command not implemented"}

            ok = bool(res.get("ok")) and "error" not in res
            msg = str(res.get("message") or res.get("error") or ("OK" if ok else "Failed"))
        except Exception as exc:
            logger.error("Error executing command %s: %s", cmd_type, exc, exc_info=True)
            ok = False
            msg = str(exc)

        outcome = {
            "id": cmd_id,
            "type": cmd_type,
            "ok": ok,
            "message": msg,
            "ts": int(time.time()),
        }
        self._cmd_results.appendleft(outcome)
        self._save_bot_state()
        self.notify_state_change()
        return outcome

    # ------------------------------------------------------------------
    # Message routing & Help generator
    # ------------------------------------------------------------------

    def _on_incoming_message(self, msg: dict[str, Any]) -> None:
        asyncio.create_task(self._dispatch_incoming_message(msg))

    async def _dispatch_incoming_message(self, msg: dict[str, Any]) -> None:
        sender = str(msg.get("sender", "unknown"))
        if self.is_self(sender):
            return
        text = str(msg.get("text", "")).strip()
        channel = msg.get("channel")

        # Built-in !help command (DM or channel)
        clean = re.sub(r"^@\[[^\]]+\]\s*", "", text).strip().lower()
        if clean in ("!help", "/help"):
            if self.can_send_user(sender):
                self.record_user_send(sender)
                help_str = self.generate_help_message()
                if channel is None:
                    await self.tx_queue.send_dm(
                        sender, help_str, priority=PRIORITY_REPLY, module="hub", wait=False
                    )
                else:
                    await self.tx_queue.send_channel(
                        channel, help_str, priority=PRIORITY_REPLY, module="hub", wait=False
                    )
            return

        for mod in self.modules.values():
            try:
                await mod.on_message(msg)
            except Exception as exc:
                logger.error("Error in module %s.on_message: %s", mod.name, exc, exc_info=True)

    def generate_help_message(self) -> str:
        segments = [
            mod.get_help_segment()
            for mod in self.modules.values()
            if mod.enabled and mod.get_help_segment()
        ]
        if not segments:
            return "MeshHub Bot: No active modules."
        raw = "MeshHub: " + " | ".join(segments)
        return clamp_lora_message(raw, MAX_LORA_BYTES)

    # ------------------------------------------------------------------
    # Runtime Telemetry Payload
    # ------------------------------------------------------------------

    def get_runtime_payload(self) -> dict[str, Any]:
        """Build the complete runtime.json snapshot for the Web UI."""
        active_modules = [name for name, m in self.modules.items() if m.enabled]
        help_msg = self.generate_help_message()
        companion_channels = self.mesh_client.get_channels_summary()
        req_channels = self.collect_required_channels()

        mod_states = {name: m.get_runtime_state() for name, m in self.modules.items()}

        return {
            "id": os.environ.get("OPENHOP_PLUGIN_ID", "openhop.meshhub-bot"),
            "version": "2.0.0",
            "connected": self.mesh_client.is_connected,
            "connection_type": "tcp",
            "companion": {
                "connected": self.mesh_client.is_connected,
                "host": self.mesh_client.host,
                "port": self.mesh_client.port,
                "device_name": self.mesh_client.device_name,
                "public_key": self.mesh_client.public_key,
                "last_error": self.mesh_client.last_error,
                "channels": companion_channels,
                "required_channels": req_channels,
            },
            "device_info": self.mesh_client.device_info,
            "timezone": self.timezone,
            "rate_limit_seconds": self.user_limiter.seconds,
            "tx_rate_limit_seconds": self.tx_queue.min_interval_seconds,
            "active_modules": active_modules,
            "modules": mod_states,
            "help_message": help_msg,
            "help_length": len(help_msg.encode("utf-8")),
            "cmd_results": list(self._cmd_results),
            "activity": list(self._activity),
            "timestamp": time.time(),
        }
