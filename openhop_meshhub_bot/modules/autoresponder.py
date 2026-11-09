"""Autoresponder module with configurable channels, keywords, hop filter, and template."""

from __future__ import annotations

import logging
import re
from typing import Any

from ..mesh.text import extract_template_fields, format_template
from ..mesh.txqueue import PRIORITY_REPLY
from .base import BaseModule

logger = logging.getLogger("openhop.meshhub-bot.autoresponder")


class AutoresponderModule(BaseModule):
    """Responds to matching test/ping keywords on configured channels."""

    def __init__(self, hub: Any, config: dict[str, Any]):
        self.channels: list[str] = ["#test"]
        self.keywords: list[str] = ["test", "ping", "ack"]
        self.max_hops: int | None = 12
        self.response_template: str = "@[{sender}] ACK | Path: {hops_label}"
        self.match_count: int = 0
        self.last_match: dict[str, Any] | None = None
        super().__init__("autoresponce", hub, config)

    def apply_config(self, config: dict[str, Any]) -> None:
        super().apply_config(config)
        raw_channels = self.config.get("channels", ["#test"])
        if isinstance(raw_channels, str):
            raw_channels = [x.strip() for x in raw_channels.split(",") if x.strip()]
        elif isinstance(raw_channels, list):
            raw_channels = [str(x).strip() for x in raw_channels if str(x).strip()]
        else:
            raw_channels = ["#test"]
        self.channels = raw_channels or ["#test"]

        raw_keywords = self.config.get("keywords", ["test"])
        if isinstance(raw_keywords, str):
            raw_keywords = [x.strip() for x in raw_keywords.split(",") if x.strip()]
        elif isinstance(raw_keywords, list):
            raw_keywords = [str(x).strip() for x in raw_keywords if str(x).strip()]
        else:
            raw_keywords = ["test"]
        self.keywords = raw_keywords or ["test"]

        raw_hops = self.config.get("maxHops", 12)
        if raw_hops is None or str(raw_hops).strip().lower() in ("", "none", "unlimited"):
            self.max_hops = None
        else:
            try:
                self.max_hops = max(0, min(64, int(raw_hops)))
            except (ValueError, TypeError):
                self.max_hops = 12

        self.response_template = str(
            self.config.get("responseTemplate") or "@[{sender}] ACK | Path: {hops_label}"
        )

    def required_channels(self) -> list[str]:
        if not self.enabled:
            return []
        return list(self.channels)

    def _matches_keyword(self, text: str) -> bool:
        clean = re.sub(r"^@\[[^\]]+\]\s*", "", str(text or "")).strip()
        clean = re.sub(r"^[^:\[\]\r\n]{1,40}:\s+", "", clean).strip()
        lower = clean.casefold()
        if not lower:
            return False
        for kw in self.keywords:
            k = str(kw or "").strip().casefold()
            if not k:
                continue
            if k == "*" or k in lower:
                return True
        return False

    async def on_message(self, msg: dict[str, Any]) -> None:
        if not self.enabled:
            return
        channel = msg.get("channel")
        if channel is None:
            return
        sender = str(msg.get("sender", "unknown"))
        if self.hub.is_self(sender):
            return

        text = str(msg.get("text", "")).strip()
        if not self._matches_keyword(text):
            return

        # Check if incoming channel matches any configured channel
        matched_channel_name: str | None = None
        for ch_name in self.channels:
            if self.hub.matches_channel(channel, ch_name):
                matched_channel_name = ch_name
                break
        if not matched_channel_name:
            return

        # Hop distance check
        hops = msg.get("hops")
        if hops is None:
            pl = msg.get("path_len")
            hops = 0 if pl in (None, 255) else int(pl)
        else:
            hops = int(hops)

        if self.max_hops is not None and hops > self.max_hops:
            logger.info(
                "[autoresponce] Skipping '%s': hops %d > maxHops %d",
                sender,
                hops,
                self.max_hops,
            )
            return

        if not self.hub.can_send_user(sender):
            logger.info("[autoresponce] Rate-limited sender '%s'", sender)
            return

        fields = extract_template_fields(msg, timezone_str=self.hub.timezone)
        reply = format_template(self.response_template, fields)

        self.hub.record_user_send(sender)
        self.match_count += 1
        self.last_match = {
            "sender": sender,
            "text": text,
            "channel": matched_channel_name,
            "hops": hops,
            "snr": msg.get("snr"),
            "reply": reply,
        }
        self.hub.record_activity(
            module="autoresponce",
            event="auto_ack",
            sender=sender,
            channel=matched_channel_name,
            detail=f"{text} -> {reply}",
        )

        target_ch = self.hub.resolve_channel_target(matched_channel_name, fallback=channel)
        await self.hub.tx_queue.send_channel(
            target_ch,
            reply,
            priority=PRIORITY_REPLY,
            module="autoresponce",
            wait=False,
        )

    def get_runtime_state(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "channels": self.channels,
            "keywords": self.keywords,
            "maxHops": self.max_hops,
            "responseTemplate": self.response_template,
            "match_count": self.match_count,
            "last_match": self.last_match,
        }

    def get_help_segment(self) -> str:
        if not self.enabled:
            return ""
        ch_str = ",".join(self.channels[:2])
        return f"ACK({ch_str})"
