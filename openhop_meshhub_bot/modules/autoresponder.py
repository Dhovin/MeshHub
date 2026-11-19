"""Autoresponder module with configurable channels, keywords, hop filter, and template."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from ..core.rate_limiter import PerUserRateLimiter
from ..mesh.text import extract_template_fields, format_template
from ..mesh.txqueue import PRIORITY_REPLY
from .base import BaseModule

logger = logging.getLogger("openhop.meshhub-bot.autoresponder")

SPONGEBOB_TIMEOUT_SECONDS = 120.0
SPONGEBOB_RATE_LIMIT_SECONDS = 5.0
BOT_SECRET_CHANNEL = "#bots"

BOTS_MENU_MESSAGE = (
    "Checkout:\n"
    "#ack - bot autoresponders\n"
    "#net - weekly Saturday net\n"
    "#wx or #weather - weather\n"
    "#bot - Lodestar bot"
)

SECRET_COMMANDS: dict[str, tuple[str, str, bool]] = {
    "!marco": ("Polo", "marco_polo", False),
    "!never": ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "secret_cmd", True),
}

SPONGEBOB_STEPS: tuple[tuple[frozenset[str], str], ...] = (
    (
        frozenset({"!ready", "are ya ready kids", "are you ready kids"}),
        "Aye, aye, Captain!",
    ),
    (
        frozenset({"i cant hear you"}),
        "AYE, AYE, CAPTAIN!",
    ),
    (
        frozenset({"!pineapple", "who lives in a pineapple under the sea"}),
        "SpongeBob SquarePants!",
    ),
)


class AutoresponderModule(BaseModule):
    """Responds to matching test/ping keywords on configured channels."""

    def __init__(self, hub: Any, config: dict[str, Any]):
        self.channels: list[str] = ["#test"]
        self.keywords: list[str] = ["test", "ping"]
        self.exact_match: bool = True
        self.max_hops: int | None = 12
        self.response_template: str = "@[{sender}] ACK | Path: {hops_label}"
        self.match_count: int = 0
        self.last_match: dict[str, Any] | None = None
        self._spongebob_state: dict[str, tuple[int, float]] = {}
        self._spongebob_limiter = PerUserRateLimiter(SPONGEBOB_RATE_LIMIT_SECONDS)
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

        raw_keywords = self.config.get("keywords", ["test", "ping"])
        if isinstance(raw_keywords, str):
            raw_keywords = [x.strip() for x in raw_keywords.split(",") if x.strip()]
        elif isinstance(raw_keywords, list):
            raw_keywords = [str(x).strip() for x in raw_keywords if str(x).strip()]
        else:
            raw_keywords = ["test", "ping"]
        self.keywords = raw_keywords or ["test", "ping"]

        self.exact_match = bool(self.config.get("exactMatch", True))

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
        channels = list(self.channels)
        bot_norm = BOT_SECRET_CHANNEL.strip().lower().lstrip("#")
        if not any(str(ch).strip().lower().lstrip("#") == bot_norm for ch in channels):
            channels.append(BOT_SECRET_CHANNEL)
        return channels

    def _normalize_text(self, text: str) -> str:
        """Normalize incoming message text while preserving a leading '!' command prefix."""
        clean = re.sub(r"^@\[[^\]]+\]\s*", "", str(text or "")).strip()
        clean = re.sub(r"^[^:\[\]\r\n]{1,40}:\s+", "", clean).strip()
        # Preserve leading '!' so commands like '!marco' (or custom '!ping') match accurately
        return clean.lstrip(" \t\r\n?,.:;-").rstrip(" \t\r\n!?,.:;-").casefold()

    def _normalize_secret_key(self, text: str) -> str:
        norm = self._normalize_text(text)
        if not norm:
            return ""
        return re.sub(r"\s+", " ", norm.replace("'", "").replace("\u2019", "").replace(",", "")).strip()

    def _match_secret_command(self, text: str) -> tuple[str, str, bool] | None:
        key = self._normalize_secret_key(text)
        if not key:
            return None
        return SECRET_COMMANDS.get(key)

    def _match_spongebob_step(self, text: str) -> tuple[int, str] | None:
        key = self._normalize_secret_key(text)
        if not key:
            return None
        for idx, (triggers, reply) in enumerate(SPONGEBOB_STEPS):
            if key in triggers:
                return idx, reply
        return None

    def _matches_keyword(self, text: str) -> bool:
        lower = self._normalize_text(text)
        if not lower:
            return False
        for kw in self.keywords:
            k = str(kw or "").strip().casefold()
            if not k:
                continue
            if k == "*":
                return True
            if self.exact_match:
                if lower == k:
                    return True
            else:
                if k in lower:
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
        norm = self._normalize_text(text)
        secret = self._match_secret_command(text)
        sb_match = None if secret is not None else self._match_spongebob_step(text)

        on_bots_ch = self.hub.matches_channel(channel, BOT_SECRET_CHANNEL)
        matched_ar_ch: str | None = None
        for ch_name in self.channels:
            if self.hub.matches_channel(channel, ch_name):
                matched_ar_ch = ch_name
                break

        matched_channel_name: str | None = None
        reply: str = ""
        activity_event: str = "auto_ack"
        active_sb: tuple[int, str] | None = None
        ch_key = ""
        now = time.monotonic()

        if sb_match is not None:
            if not on_bots_ch:
                return
            matched_channel_name = BOT_SECRET_CHANNEL
            ch_key = matched_channel_name.casefold()
            step_idx, sb_reply = sb_match
            state = self._spongebob_state.get(ch_key)
            if state is not None and now > state[1]:
                self._spongebob_state.pop(ch_key, None)
                state = None
            if step_idx != 0 and (state is None or state[0] != step_idx):
                return
            active_sb = (step_idx, sb_reply)
            reply = sb_reply
            activity_event = "secret_cmd"
        elif secret is not None and secret[2]:
            if not on_bots_ch:
                return
            matched_channel_name = BOT_SECRET_CHANNEL
            reply, activity_event, _ = secret
        elif secret is not None and matched_ar_ch is not None:
            matched_channel_name = matched_ar_ch
            reply, activity_event, _ = secret
        elif matched_ar_ch is not None and self._matches_keyword(text):
            matched_channel_name = matched_ar_ch
            fields = extract_template_fields(msg, timezone_str=self.hub.timezone)
            reply = format_template(self.response_template, fields)
            activity_event = "auto_ack"
        elif on_bots_ch and (norm.startswith("!") or norm == "/help"):
            matched_channel_name = BOT_SECRET_CHANNEL
            reply = BOTS_MENU_MESSAGE
            activity_event = "bots_menu"
        else:
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

        if active_sb is not None:
            if not self._spongebob_limiter.can_send(sender):
                logger.info("[autoresponce] SpongeBob rate-limited sender '%s'", sender)
                return
        elif not self.hub.can_send_user(sender):
            logger.info("[autoresponce] Rate-limited sender '%s'", sender)
            return

        if active_sb is not None:
            step_idx = active_sb[0]
            if step_idx + 1 < len(SPONGEBOB_STEPS):
                self._spongebob_state[ch_key] = (step_idx + 1, now + SPONGEBOB_TIMEOUT_SECONDS)
            else:
                self._spongebob_state.pop(ch_key, None)
            self._spongebob_limiter.record_send(sender)
        else:
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
            event=activity_event,
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
            "exactMatch": self.exact_match,
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
