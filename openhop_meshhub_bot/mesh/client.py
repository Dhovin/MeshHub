"""Virtual Companion TCP client for openHop Repeater using meshcore."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any, Callable

from meshcore.events import EventType
from meshcore.meshcore import MeshCore

from .text import MAX_LORA_BYTES, clamp_lora_message

logger = logging.getLogger("openhop.meshhub-bot.mesh")

ADVERT_COOLDOWN_SECONDS = 60.0

IGNORED_SENDER_PREFIXES = frozenset(
    {
        "warning",
        "note",
        "info",
        "error",
        "http",
        "https",
        "link",
        "attention",
        "msg",
        "message",
        "sender",
        "receiver",
        "broadcast",
        "all",
        "group",
        "chan",
        "channel",
        "alert",
        "notice",
        "time",
        "date",
    }
)


class MeshCompanionClient:
    """Focused MeshCore TCP client for openHop Virtual Companion."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 5051,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        on_status_change: Callable[[], None] | None = None,
    ):
        self.host = host
        self.port = int(port)
        self._on_message = on_message
        self._on_status_change = on_status_change

        self.mc: Any = None
        self.is_connected: bool = False
        self.device_info: dict[str, Any] = {}
        self.self_info: dict[str, Any] = {}
        self.channels: list[dict[str, Any]] = []
        self.last_error: str | None = None
        self._last_advert_mono: float = 0.0
        self._cmd_lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task | None = None
        self._stopping: bool = False
        self._on_connected_hook: Callable[[], Any] | None = None

    def set_on_connected_hook(self, cb: Callable[[], Any] | None) -> None:
        self._on_connected_hook = cb

    @property
    def device_name(self) -> str:
        if isinstance(self.self_info, dict) and self.self_info.get("name"):
            return str(self.self_info["name"])
        if isinstance(self.device_info, dict) and self.device_info.get("name"):
            return str(self.device_info["name"])
        return "MeshHub-Bot"

    @property
    def public_key(self) -> str:
        for src in (self.self_info, self.device_info):
            if isinstance(src, dict):
                pk = src.get("public_key") or src.get("pubkey")
                if isinstance(pk, bytes):
                    return pk.hex()
                if isinstance(pk, str) and pk:
                    return pk
        return ""

    def is_self_sender(self, sender_name: str) -> bool:
        if not sender_name:
            return False
        s_clean = str(sender_name).strip().lower()
        my_name = self.device_name.strip().lower()
        return bool(my_name and s_clean == my_name)

    def start_background_connect(self) -> None:
        """Start non-blocking background connection & reconnect supervision."""
        self._stopping = False
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._connect_loop())

    async def update_endpoint(self, host: str, port: int) -> None:
        """Reconnect in place when connection.host or connection.tcpPort changes."""
        new_host = str(host or "127.0.0.1").strip() or "127.0.0.1"
        new_port = int(port)
        if new_host == self.host and new_port == self.port and self.is_connected:
            return
        logger.info("Companion endpoint updated to %s:%s; reconnecting...", new_host, new_port)
        self.host = new_host
        self.port = new_port
        await self.disconnect(stop_loop=False)
        self.start_background_connect()

    async def _connect_loop(self) -> None:
        backoff = 1.5
        max_backoff = 30.0
        attempt = 0
        while not self._stopping:
            if self.is_connected and self.mc is not None:
                await asyncio.sleep(2.0)
                continue

            attempt += 1
            try:
                logger.info(
                    "Connecting to openHop Virtual Companion at %s:%s (attempt %d)...",
                    self.host,
                    self.port,
                    attempt,
                )
                await self.connect_once()
                attempt = 0
                backoff = 1.5
                if self._on_connected_hook:
                    res = self._on_connected_hook()
                    if asyncio.iscoroutine(res):
                        await res
                if self._on_status_change:
                    self._on_status_change()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.is_connected = False
                self.last_error = str(exc)
                logger.warning(
                    "Companion connection attempt %d failed (%s); retrying in %.1fs",
                    attempt,
                    exc,
                    backoff,
                )
                if self._on_status_change:
                    self._on_status_change()
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    break
                backoff = min(max_backoff, backoff * 1.8)

    async def connect_once(self) -> None:
        """Establish a single TCP session and perform the companion handshake."""
        await self._close_mc()
        mc = await MeshCore.create_tcp(host=self.host, port=self.port, only_error=True)
        if not mc:
            raise RuntimeError(f"Failed to connect to {self.host}:{self.port}")

        self.mc = mc
        if hasattr(self.mc, "set_decrypt_channel_logs"):
            self.mc.set_decrypt_channel_logs(True)
        if hasattr(self.mc, "auto_update_contacts"):
            self.mc.auto_update_contacts = True

        self._subscribe_events()
        await self._run_handshake()
        await self.refresh_channels()

        # Start contact sync & message fetching without blocking startup
        asyncio.create_task(self._post_connect_sync())

    async def _post_connect_sync(self) -> None:
        if not self.mc or not self.is_connected:
            return
        try:
            await self.mc.ensure_contacts()
        except Exception as exc:
            logger.debug("ensure_contacts warning: %s", exc)
        try:
            await self.mc.start_auto_message_fetching()
        except Exception as exc:
            logger.warning("start_auto_message_fetching warning: %s", exc)

    def _subscribe_events(self) -> None:
        if not self.mc:
            return
        self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_private_message)
        self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_message)
        self.mc.subscribe(EventType.ADVERTISEMENT, self._on_advertisement)
        self.mc.subscribe(EventType.NEW_CONTACT, self._on_new_contact)
        self.mc.subscribe(EventType.DISCONNECTED, self._on_disconnect)
        self.mc.subscribe(EventType.SELF_INFO, self._on_self_info)

    async def _run_handshake(self) -> None:
        res = None
        for attempt in range(2):
            try:
                res = await self.mc.commands.send_device_query()
                if res and res.type != EventType.ERROR:
                    break
                if attempt == 0:
                    await asyncio.sleep(0.5)
            except Exception as exc:
                logger.debug("send_device_query attempt %d error: %s", attempt + 1, exc)
                if attempt == 0:
                    await asyncio.sleep(0.5)

        if res and res.type != EventType.ERROR and isinstance(res.payload, dict):
            self.device_info = dict(res.payload)
        elif hasattr(self.mc, "self_info") and isinstance(self.mc.self_info, dict):
            self.device_info = dict(self.mc.self_info)
        else:
            raise RuntimeError(f"Companion handshake failed: {res}")

        try:
            app_res = await self.mc.commands.send_appstart()
            if app_res and app_res.type != EventType.ERROR and isinstance(app_res.payload, dict):
                self.self_info = dict(app_res.payload)
            elif hasattr(self.mc, "self_info") and isinstance(self.mc.self_info, dict):
                self.self_info = dict(self.mc.self_info)
        except Exception as exc:
            logger.warning("send_appstart warning: %s", exc)

        self.is_connected = True
        self.last_error = None
        logger.info(
            "Connected to Virtual Companion '%s' (%s:%s)",
            self.device_name,
            self.host,
            self.port,
        )

    async def _close_mc(self) -> None:
        if self.mc is not None:
            mc_ref = self.mc
            self.mc = None
            try:
                res = mc_ref.stop()
                if asyncio.iscoroutine(res):
                    await asyncio.wait_for(res, timeout=1.5)
            except Exception as exc:
                logger.debug("Error stopping meshcore instance: %s", exc)
        self.is_connected = False

    async def disconnect(self, *, stop_loop: bool = True) -> None:
        """Cleanly close the TCP client within the supervisor shutdown budget."""
        if stop_loop:
            self._stopping = True
            if self._reconnect_task and not self._reconnect_task.done():
                self._reconnect_task.cancel()
                try:
                    await asyncio.wait_for(self._reconnect_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            self._reconnect_task = None
        await self._close_mc()

    def _on_disconnect(self, event: Any) -> None:
        """Handle companion TCP disconnect without exiting the service process."""
        logger.warning("Virtual Companion TCP link disconnected; reconnecting in place...")
        self.is_connected = False
        self.last_error = "Disconnected from Virtual Companion"
        if self._on_status_change:
            self._on_status_change()
        if not self._stopping:
            try:
                loop = asyncio.get_running_loop()
                if self._reconnect_task is None or self._reconnect_task.done():
                    self._reconnect_task = loop.create_task(self._connect_loop())
            except RuntimeError:
                pass

    def _on_self_info(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            self.self_info.update(payload)
            if self._on_status_change:
                self._on_status_change()

    def _on_new_contact(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict) and self.mc:
            pubkey = payload.get("public_key")
            if pubkey:
                if not hasattr(self.mc, "_contacts") or self.mc._contacts is None:
                    self.mc._contacts = {}
                self.mc._contacts[pubkey] = payload

    def _on_advertisement(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict) or not self.mc:
            return
        pubkey = payload.get("public_key")
        if not pubkey:
            return
        if not hasattr(self.mc, "_contacts") or self.mc._contacts is None:
            self.mc._contacts = {}
        adv_name = payload.get("adv_name") or payload.get("name")
        if pubkey not in self.mc._contacts:
            self.mc._contacts[pubkey] = {
                "public_key": pubkey,
                "type": payload.get("type", 1),
                "flags": payload.get("flags", 0),
                "adv_name": adv_name or f"Node-{str(pubkey)[:6]}",
                "last_advert": payload.get("last_advert") or int(time.time()),
                "lastmod": payload.get("lastmod") or int(time.time()),
            }
        elif adv_name:
            self.mc._contacts[pubkey]["adv_name"] = adv_name

    def _on_private_message(self, event: Any) -> None:
        try:
            payload = dict(getattr(event, "payload", None) or {})
            text_val = (
                payload.get("text")
                or payload.get("message")
                or payload.get("crypted")
                or ""
            )
            sender = payload.get("sender") or payload.get("from")
            if not sender and "pubkey_prefix" in payload and self.mc:
                contact = self.mc.get_contact_by_key_prefix(payload["pubkey_prefix"])
                if contact:
                    sender = contact.get("adv_name") or contact.get("name")
            if not sender:
                sender = payload.get("pubkey_prefix") or "unknown"

            path_len = payload.get("path_len")
            msg = {
                "sender": str(sender),
                "pubkey_prefix": payload.get("pubkey_prefix"),
                "text": str(text_val),
                "channel": None,
                "timestamp": payload.get("time")
                or payload.get("sender_timestamp")
                or int(time.time()),
                "snr": payload.get("snr")
                if payload.get("snr") is not None
                else payload.get("SNR"),
                "rssi": payload.get("rssi")
                if payload.get("rssi") is not None
                else payload.get("RSSI"),
                "path": payload.get("path"),
                "path_len": path_len,
                "path_hash_mode": payload.get("path_hash_mode"),
                "hops": 0 if path_len == 255 else (path_len if path_len is not None else 0),
            }
            if self._on_message:
                self._on_message(msg)
        except Exception as exc:
            logger.error("Error handling private message: %s", exc, exc_info=True)

    def _on_channel_message(self, event: Any) -> None:
        try:
            payload = dict(getattr(event, "payload", None) or {})
            text_val = str(
                payload.get("text")
                or payload.get("message")
                or payload.get("crypted")
                or ""
            )
            channel_val = payload.get("channel_idx")
            if channel_val is None:
                channel_val = payload.get("chan_name") or payload.get("chan_nb") or 0

            sender = payload.get("sender") or payload.get("from")
            fallback_sender: str | None = None
            if (not sender or sender == "unknown") and self.mc:
                logged = None
                reader = getattr(self.mc, "_reader", None)
                parser = getattr(reader, "packet_parser", None) if reader else None
                if parser and hasattr(parser, "channels_log"):
                    for entry in reversed(parser.channels_log):
                        if (
                            entry.get("sender_timestamp") == payload.get("sender_timestamp")
                            and entry.get("message") == text_val
                        ):
                            logged = entry
                            break
                if logged:
                    transport_code = logged.get("transport_code")
                    if transport_code:
                        contact = self.mc.get_contact_by_key_prefix(transport_code)
                        if contact:
                            c_name = contact.get("adv_name") or contact.get("name")
                            if c_name:
                                sender = str(c_name)
                            else:
                                fallback_sender = (
                                    f"Unknown-{str(contact.get('public_key', transport_code))[:6]}"
                                )
                        else:
                            fallback_sender = f"Unknown-{str(transport_code)[:6]}"

            # MeshCore channel messages commonly embed "<SenderName>: <body text>" in payload["text"]
            stripped = False
            if sender and sender != "unknown":
                pattern1 = rf"^{re.escape(str(sender))}:\s*(.*)$"
                match1 = re.match(pattern1, text_val, re.IGNORECASE | re.DOTALL)
                if match1:
                    text_val = match1.group(1)
                    stripped = True
                else:
                    first_word = str(sender).split()[0] if str(sender).strip() else ""
                    if first_word and len(first_word) >= 3:
                        pattern2 = rf"^{re.escape(first_word)}:\s*(.*)$"
                        match2 = re.match(pattern2, text_val, re.IGNORECASE | re.DOTALL)
                        if match2:
                            text_val = match2.group(1)
                            stripped = True

            if not stripped:
                match = re.match(r"^([^:\[\]\r\n]{1,40}):\s+(.*)$", text_val, re.DOTALL)
                if match:
                    guessed = match.group(1).strip()
                    if guessed and guessed.lower() not in IGNORED_SENDER_PREFIXES:
                        if not sender or sender == "unknown" or str(sender).startswith("Unknown-"):
                            sender = guessed
                        text_val = match.group(2)

            if not sender or sender == "unknown":
                sender = fallback_sender or "unknown"

            path_len = payload.get("path_len")
            msg = {
                "sender": str(sender),
                "text": text_val,
                "channel": channel_val,
                "timestamp": payload.get("time")
                or payload.get("sender_timestamp")
                or int(time.time()),
                "snr": payload.get("snr")
                if payload.get("snr") is not None
                else payload.get("SNR"),
                "rssi": payload.get("rssi")
                if payload.get("rssi") is not None
                else payload.get("RSSI"),
                "path": payload.get("path"),
                "path_len": path_len,
                "path_hash_mode": payload.get("path_hash_mode"),
                "hops": 0 if path_len == 255 else (path_len if path_len is not None else 0),
            }
            if self._on_message:
                self._on_message(msg)
        except Exception as exc:
            logger.error("Error handling channel message: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Channel management (strictly additive, secret-redacted)
    # ------------------------------------------------------------------

    async def refresh_channels(self) -> list[dict[str, Any]]:
        """Query all channels from the Virtual Companion, redacting secrets."""
        if not self.mc or not self.is_connected:
            return self.channels
        async with self._cmd_lock:
            ch_idx = 0
            fetched: list[dict[str, Any]] = []
            max_ch = int(
                (self.device_info or {}).get("max_channels", 40)
                if isinstance(self.device_info, dict)
                else 40
            )
            max_ch = max(8, min(max_ch, 64))
            while ch_idx < max_ch:
                res = await self.mc.commands.get_channel(ch_idx)
                if not res or res.type == EventType.ERROR:
                    break
                payload = dict(res.payload) if isinstance(res.payload, dict) else {}
                name = str(payload.get("channel_name", "")).strip()
                fetched.append(
                    {
                        "channel_idx": int(payload.get("channel_idx", ch_idx)),
                        "channel_name": name,
                    }
                )
                ch_idx += 1
            self.channels = fetched
            self.mc.channels = fetched
            return fetched

    def get_channel_by_name(self, name: str) -> dict[str, Any] | None:
        """Find a channel entry by exact or normalized (#-agnostic) name."""
        target = str(name or "").strip()
        if not target:
            return None
        for ch in self.channels:
            if ch and str(ch.get("channel_name", "")).strip() == target:
                return ch
        target_norm = target.lower().lstrip("#")
        for ch in self.channels:
            if ch:
                ch_name = str(ch.get("channel_name", "")).strip()
                if ch_name and ch_name.lower().lstrip("#") == target_norm:
                    return ch
        return None

    def resolve_channel_index(self, channel_or_name: int | str) -> int | None:
        if isinstance(channel_or_name, int):
            return channel_or_name
        s = str(channel_or_name or "").strip()
        if s.isdigit():
            return int(s)
        found = self.get_channel_by_name(s)
        if found is not None:
            return int(found.get("channel_idx", 0))
        return None

    def matches_channel(self, incoming_channel: int | str | None, target_channel: str | int) -> bool:
        """Return True if incoming_channel matches target_channel by index or name."""
        if incoming_channel is None:
            return False
        if isinstance(target_channel, int) or str(target_channel).strip().isdigit():
            t_idx = int(target_channel)
            if isinstance(incoming_channel, int) or str(incoming_channel).strip().isdigit():
                return int(incoming_channel) == t_idx
        t_name = str(target_channel).strip()
        if t_name.lower() in ("public", "0"):
            return str(incoming_channel).strip().lower() in ("0", "public")

        resolved_idx = self.resolve_channel_index(t_name)
        if resolved_idx is not None and (
            isinstance(incoming_channel, int) or str(incoming_channel).strip().isdigit()
        ):
            return int(incoming_channel) == resolved_idx

        inc_norm = str(incoming_channel).strip().lower().lstrip("#")
        tgt_norm = t_name.lower().lstrip("#")
        return bool(inc_norm and inc_norm == tgt_norm)

    async def provision_channels(self, required_names: list[str]) -> dict[str, int]:
        """Additively provision required #channels on the Virtual Companion.

        Existing channels always retain their slot index. Missing channels are
        assigned to the first empty slot (idx >= 1, name == ''). Never deletes
        or modifies existing channels, and never touches slot 0 (Public).
        """
        if not self.mc or not self.is_connected:
            return {}

        await self.refresh_channels()
        mapped: dict[str, int] = {}

        # Deduplicate while preserving order
        seen_norm: set[str] = set()
        ordered: list[str] = []
        for raw_name in required_names:
            name = str(raw_name or "").strip()
            if not name:
                continue
            if name.lower() in ("public", "0"):
                mapped[name] = 0
                continue
            norm = name.lower().lstrip("#")
            if not norm or norm in seen_norm:
                continue
            seen_norm.add(norm)
            ordered.append(name if name.startswith("#") else f"#{name}")

        for ch_name in ordered:
            existing = self.get_channel_by_name(ch_name)
            if existing is not None:
                idx = int(existing.get("channel_idx", 0))
                mapped[ch_name] = idx
                continue

            # Find first empty slot >= 1
            empty_slot: int | None = None
            for ch in self.channels:
                idx = int(ch.get("channel_idx", 0))
                if idx >= 1 and not str(ch.get("channel_name", "")).strip():
                    empty_slot = idx
                    break
            if empty_slot is None:
                used = {int(ch.get("channel_idx", 0)) for ch in self.channels if ch}
                candidate = 1
                while candidate in used:
                    candidate += 1
                empty_slot = candidate

            async with self._cmd_lock:
                # Derive standard hashtag channel 16-byte PSK
                secret = hashlib.sha256(ch_name.encode("utf-8")).digest()[:16]
                res = await self.mc.commands.set_channel(empty_slot, ch_name, secret)
                if res and res.type == EventType.ERROR:
                    logger.error(
                        "Failed to provision channel %s on slot %d: %s",
                        ch_name,
                        empty_slot,
                        res,
                    )
                    continue
                logger.info("Provisioned channel '%s' on Virtual Companion slot %d", ch_name, empty_slot)

            # Update in-memory table immediately
            entry = {"channel_idx": empty_slot, "channel_name": ch_name}
            while len(self.channels) <= empty_slot:
                self.channels.append({"channel_idx": len(self.channels), "channel_name": ""})
            self.channels[empty_slot] = entry
            mapped[ch_name] = empty_slot

        return mapped

    def get_channels_summary(self) -> list[dict[str, Any]]:
        """Return non-empty channels without secrets for runtime.json."""
        out: list[dict[str, Any]] = []
        for ch in self.channels:
            if not ch:
                continue
            name = str(ch.get("channel_name", "")).strip()
            idx = int(ch.get("channel_idx", 0))
            if idx == 0 or name:
                out.append({"index": idx, "name": name or ("Public" if idx == 0 else "")})
        return out

    # ------------------------------------------------------------------
    # Outbound transmission primitives (called by OutboundTxQueue)
    # ------------------------------------------------------------------

    async def send_raw_tx(self, target_type: str, target: Any, text: str) -> dict[str, Any]:
        """Execute a single clamped channel or DM transmission over MeshCore."""
        if not self.mc or not self.is_connected:
            return {"error": "Virtual Companion not connected"}

        clamped = clamp_lora_message(text, MAX_LORA_BYTES)
        async with self._cmd_lock:
            if target_type == "channel":
                idx = self.resolve_channel_index(target)
                if idx is None:
                    # Try refreshing channels once
                    await self.refresh_channels()
                    idx = self.resolve_channel_index(target)
                if idx is None:
                    return {"error": f"Unknown channel: {target}"}
                res = await self.mc.commands.send_chan_msg(idx, clamped)
                if res and res.type == EventType.ERROR:
                    return {"error": f"Channel send failed: {res}"}
                return {"ok": True, "channel_idx": idx, "text": clamped}

            if target_type == "dm":
                contact = await self._resolve_contact(str(target))
                if not contact:
                    return {"error": f"Unknown contact: {target}"}
                res = await self.mc.commands.send_msg(contact, clamped)
                if res and res.type == EventType.ERROR:
                    return {"error": f"DM send failed: {res}"}
                return {"ok": True, "recipient": str(target), "text": clamped}

            return {"error": f"Unsupported target_type: {target_type}"}

    async def _resolve_contact(self, identifier: str) -> Any:
        if not self.mc:
            return None
        try:
            await self.mc.ensure_contacts()
        except Exception:
            pass
        contact = None
        try:
            int(identifier, 16)
            contact = self.mc.get_contact_by_key_prefix(identifier)
        except ValueError:
            pass
        if contact is None:
            contact = self.mc.get_contact_by_name(identifier)
        return contact

    async def send_advert(self, *, flood: bool = True) -> dict[str, Any]:
        """Broadcast a self advertisement with a 60-second cooldown."""
        if not self.mc or not self.is_connected:
            return {"error": "Virtual Companion not connected"}
        now = time.monotonic()
        if self._last_advert_mono > 0 and (now - self._last_advert_mono) < ADVERT_COOLDOWN_SECONDS:
            wait_s = int(ADVERT_COOLDOWN_SECONDS - (now - self._last_advert_mono))
            return {"error": f"Advert cooldown active ({wait_s}s remaining)"}
        async with self._cmd_lock:
            res = await self.mc.commands.send_advert(flood=flood)
            if res and res.type == EventType.ERROR:
                return {"error": f"Failed to send advert: {res}"}
            self._last_advert_mono = time.monotonic()
            return {"ok": True, "message": "Self advertisement broadcast sent"}
