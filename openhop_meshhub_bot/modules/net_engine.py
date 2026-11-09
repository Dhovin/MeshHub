"""Unified NetSessionEngine powering both Radio Net (net_bot) and MeshWars (meshwars_bot)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..mesh.text import format_template, split_to_byte_chunks
from ..mesh.txqueue import PRIORITY_BROADCAST, PRIORITY_REPLY
from .base import BaseModule

logger = logging.getLogger("openhop.meshhub-bot.net_engine")

DAY_OF_WEEK_MAP = {
    "sunday": 0,
    "sun": 0,
    "monday": 1,
    "mon": 1,
    "tuesday": 2,
    "tue": 2,
    "wednesday": 3,
    "wed": 3,
    "thursday": 4,
    "thu": 4,
    "friday": 5,
    "fri": 5,
    "saturday": 6,
    "sat": 6,
}


def compute_net_crons(day_of_week_str: str, time_str: str) -> tuple[str, str, str]:
    """Return (net_start_cron, reminder_1pm_cron, reminder_30m_cron)."""
    dow = DAY_OF_WEEK_MAP.get(str(day_of_week_str or "Saturday").strip().lower(), 6)
    parts = str(time_str or "18:00").strip().split(":")
    try:
        hour = max(0, min(23, int(parts[0])))
        minute = max(0, min(59, int(parts[1]) if len(parts) > 1 else 0))
    except (ValueError, TypeError):
        hour, minute = 18, 0

    net_cron = f"{minute} {hour} * * {dow}"
    r1pm_cron = f"0 13 * * {dow}"

    total_minutes = hour * 60 + minute
    prior_minutes = total_minutes - 30
    prior_dow = dow
    if prior_minutes < 0:
        prior_minutes += 24 * 60
        prior_dow = (dow - 1) % 7
    prior_hour = prior_minutes // 60
    prior_minute = prior_minutes % 60
    r30m_cron = f"{prior_minute} {prior_hour} * * {prior_dow}"
    return net_cron, r1pm_cron, r30m_cron


class NetSessionEngine(BaseModule):
    """Scheduled & on-demand Radio Net manager (net_bot)."""

    def __init__(
        self,
        name: str,
        hub: Any,
        config: dict[str, Any],
        *,
        track_all_chat: bool = True,
        default_channel: str = "#net",
        default_day: str = "Saturday",
        default_time: str = "18:00",
        default_duration: float = 6.0,
        default_keyword: str = "!checkin",
        default_state_file: str = "net_state.json",
    ):
        self.track_all_chat = track_all_chat
        self._default_channel = default_channel
        self._default_day = default_day
        self._default_time = default_time
        self._default_duration = default_duration
        self._default_keyword = default_keyword
        self._default_state_file = default_state_file

        self.channel: str = default_channel
        self.day_of_week: str = default_day
        self.time: str = default_time
        self.duration: float = default_duration
        self.keyword: str = default_keyword
        self.state_file: str = default_state_file
        self.enable_reminders: bool = True
        self.enable_hourly_message: bool = track_all_chat
        self.enable_mid_net_updates: bool = track_all_chat

        self.opening_message: str = ""
        self.closing_message: str = ""
        self.hourly_message: str = ""
        self.halfway_message: str = ""
        self.final_hour_message: str = ""
        self.checkin_ack_template: str = ""
        self.reminder_1pm_message: str = ""
        self.reminder_30m_message: str = ""

        # Active session state
        self.is_active: bool = False
        self.net_start_time: str | None = None
        self.participants: list[dict[str, Any]] = []
        self.halfway_sent: bool = False
        self.final_hour_sent: bool = False
        self.hourly_sent: list[int] = []
        self.last_summary: dict[str, Any] | None = None

        # Handles
        self._unschedule_start: Any = None
        self._unschedule_r1pm: Any = None
        self._unschedule_r30m: Any = None
        self._net_end_task: asyncio.Task | None = None
        self._timer_tasks: list[asyncio.Task] = []
        self._running: bool = False

        super().__init__(name, hub, config)
        self._load_state()

    def _state_file_path(self) -> str:
        safe_name = os.path.basename(self.state_file or self._default_state_file)
        return os.path.join(self.hub.get_data_dir(), safe_name)

    def apply_config(self, config: dict[str, Any]) -> None:
        prev_day = getattr(self, "day_of_week", None)
        prev_time = getattr(self, "time", None)
        prev_rem = getattr(self, "enable_reminders", None)
        prev_enabled = getattr(self, "enabled", False)

        super().apply_config(config)
        self.channel = str(self.config.get("channel") or self._default_channel).strip()
        self.day_of_week = str(self.config.get("day_of_week") or self._default_day).strip()
        self.time = str(self.config.get("time") or self._default_time).strip()
        try:
            self.duration = max(
                0.1,
                float(self.config.get("duration", self.config.get("duration_hours", self._default_duration))),
            )
        except (ValueError, TypeError):
            self.duration = self._default_duration

        self.keyword = str(
            self.config.get("checkin_keyword")
            or self.config.get("keyword")
            or self._default_keyword
        ).strip()
        self.state_file = str(self.config.get("state_file") or self._default_state_file).strip()
        self.enable_reminders = bool(self.config.get("enable_reminders", True))
        self.enable_hourly_message = bool(
            self.config.get("enable_hourly_message", self.track_all_chat)
        )
        self.enable_mid_net_updates = bool(
            self.config.get("enable_mid_net_updates", self.track_all_chat)
        )

        self.opening_message = str(
            self.config.get("opening_message")
            or "Welcome to the MeshWars net! Send !checkin or any chat message to participate."
        )
        self.closing_message = str(
            self.config.get("closing_message")
            or "The MeshWars net has concluded. Thank you for participating! {count} {user_str} checked in today."
        )
        self.hourly_message = str(
            self.config.get("hourly_message")
            or "Register on https://meshwars.com and any message sent in this chat counts toward your bonus points."
        )
        self.halfway_message = str(
            self.config.get("halfway_message")
            or "MeshWars Net Update: Halfway through tonight's net! {count} {user_str} checked in so far."
        )
        self.final_hour_message = str(
            self.config.get("final_hour_message")
            or "MeshWars Net Update: 1 hour remaining in tonight's net! {count} {user_str} checked in so far."
        )
        self.checkin_ack_template = str(
            self.config.get("checkin_ack_template")
            or "Welcome {sender}, {count} {user_str} {have_str} checked in."
        )
        self.reminder_1pm_message = str(
            self.config.get("reminder_1pm_message")
            or "Reminder: MeshWars Net starts today at {time} on {channel}!"
        )
        self.reminder_30m_message = str(
            self.config.get("reminder_30m_message")
            or "MeshWars Net starts in 30 minutes on {channel}! Get your nodes ready."
        )

        if getattr(self, "_running", False):
            if not self.enabled and prev_enabled:
                self._clear_schedules()
            elif self.enabled and (
                not prev_enabled
                or prev_day != self.day_of_week
                or prev_time != self.time
                or prev_rem != self.enable_reminders
            ):
                self._setup_schedules()

    def required_channels(self) -> list[str]:
        if not self.enabled or not self.channel:
            return []
        return [self.channel]

    @property
    def checkins(self) -> list[str]:
        """List of participant callsigns/names (compatibility property)."""
        return [p["sender"] for p in self.participants]

    def _load_state(self) -> None:
        path = self._state_file_path()
        if not os.path.isfile(path):
            legacy_mw = os.path.join(self.hub.get_data_dir(), "meshwars_state.json")
            if os.path.isfile(legacy_mw):
                path = legacy_mw
            else:
                return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return
            self.is_active = bool(data.get("is_active", False))
            self.net_start_time = data.get("net_start_time")
            raw_checkins = data.get("participants") or data.get("checkins") or []
            norm_participants: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in raw_checkins:
                if isinstance(item, dict):
                    s = str(item.get("sender") or item.get("name") or "").strip()
                    if s and s.lower() not in seen:
                        seen.add(s.lower())
                        norm_participants.append(
                            {
                                "sender": s,
                                "timestamp": item.get("timestamp") or self.net_start_time or "",
                                "snr": item.get("snr"),
                                "hops": item.get("hops", 0),
                            }
                        )
                elif isinstance(item, str) and item.strip():
                    s = item.strip()
                    if s.lower() not in seen:
                        seen.add(s.lower())
                        norm_participants.append(
                            {
                                "sender": s,
                                "timestamp": self.net_start_time or "",
                                "snr": None,
                                "hops": 0,
                            }
                        )
            self.participants = norm_participants
            self.halfway_sent = bool(data.get("halfway_sent", False))
            self.final_hour_sent = bool(data.get("final_hour_sent", False))
            self.hourly_sent = list(data.get("hourly_sent", []))
            self.last_summary = data.get("last_summary")
        except Exception as exc:
            logger.warning("[%s] Could not load state from %s: %s", self.name, path, exc)

    def _save_state(self) -> None:
        path = self._state_file_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            payload = {
                "is_active": self.is_active,
                "net_start_time": self.net_start_time,
                "checkins": [p["sender"] for p in self.participants],
                "participants": self.participants,
                "halfway_sent": self.halfway_sent,
                "final_hour_sent": self.final_hour_sent,
                "hourly_sent": self.hourly_sent,
                "last_summary": self.last_summary,
            }
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("[%s] Could not save state to %s: %s", self.name, path, exc)

    async def start(self) -> None:
        self._running = True
        if self.enabled:
            self._setup_schedules()
        # Resume active net session if one was running before restart
        if self.is_active and self.net_start_time:
            try:
                tz = ZoneInfo(self.hub.timezone)
            except Exception:
                tz = ZoneInfo("UTC")
            try:
                start_dt = datetime.fromisoformat(self.net_start_time)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=tz)
                now = datetime.now(tz)
                elapsed = (now - start_dt).total_seconds()
                duration_secs = self.duration * 3600.0
                if 0 <= elapsed < duration_secs:
                    remaining = duration_secs - elapsed
                    logger.info(
                        "[%s] Resuming active net (remaining: %.0fs, participants: %d)",
                        self.name,
                        remaining,
                        len(self.participants),
                    )
                    self._start_session_timers(duration_secs, elapsed=elapsed)
                else:
                    logger.info("[%s] Active net from state file expired; closing session.", self.name)
                    await self.stop_net_manual(broadcast=True)
            except Exception as exc:
                logger.warning("[%s] Error resuming net session: %s", self.name, exc)
                self.is_active = False
                self._save_state()

    async def stop(self) -> None:
        self._running = False
        self._clear_schedules()
        self._cancel_session_timers()

    def _clear_schedules(self) -> None:
        for attr in ("_unschedule_start", "_unschedule_r1pm", "_unschedule_r30m"):
            fn = getattr(self, attr, None)
            if fn:
                fn()
                setattr(self, attr, None)

    def _setup_schedules(self) -> None:
        self._clear_schedules()
        if not self.enabled:
            return
        try:
            net_cron, r1pm_cron, r30m_cron = compute_net_crons(self.day_of_week, self.time)
            tz = self.hub.timezone
            self._unschedule_start = self.hub.scheduler.schedule(
                net_cron,
                self.start_net_manual,
                name=f"{self.name}_start",
                timezone=tz,
            )
            if self.enable_reminders:
                self._unschedule_r1pm = self.hub.scheduler.schedule(
                    r1pm_cron,
                    self._on_1pm_reminder,
                    name=f"{self.name}_1pm",
                    timezone=tz,
                )
                self._unschedule_r30m = self.hub.scheduler.schedule(
                    r30m_cron,
                    self._on_30m_reminder,
                    name=f"{self.name}_30m",
                    timezone=tz,
                )
            logger.info("[%s] Scheduled net start (%s) in tz %s", self.name, net_cron, tz)
        except Exception as exc:
            logger.error("[%s] Failed scheduling net: %s", self.name, exc)

    def _cancel_session_timers(self) -> None:
        if self._net_end_task and not self._net_end_task.done():
            self._net_end_task.cancel()
        self._net_end_task = None
        for t in self._timer_tasks:
            if not t.done():
                t.cancel()
        self._timer_tasks.clear()

    def _start_session_timers(self, duration_secs: float, elapsed: float = 0.0) -> None:
        self._cancel_session_timers()
        remaining = max(1.0, duration_secs - elapsed)
        self._net_end_task = asyncio.create_task(self._wait_and_end(remaining))

        if self.enable_mid_net_updates:
            halfway_delay = (duration_secs / 2.0) - elapsed
            if halfway_delay > 0 and not self.halfway_sent:
                self._timer_tasks.append(
                    asyncio.create_task(self._delayed_mid_update(halfway_delay, is_halfway=True))
                )
            final_hr_delay = (duration_secs - 3600.0) - elapsed
            if duration_secs > 3600.0 and final_hr_delay > 0 and not self.final_hour_sent:
                self._timer_tasks.append(
                    asyncio.create_task(self._delayed_mid_update(final_hr_delay, is_halfway=False))
                )

        if self.enable_hourly_message and self.hourly_message:
            total_hours = int(self.duration)
            for hr in range(1, total_hours):
                if hr in self.hourly_sent:
                    continue
                delay = (hr * 3600.0) - elapsed
                if delay > 0:
                    self._timer_tasks.append(
                        asyncio.create_task(self._delayed_hourly_msg(delay, hr))
                    )

    async def _wait_and_end(self, delay_secs: float) -> None:
        try:
            await asyncio.sleep(delay_secs)
            if self.is_active:
                await self.stop_net_manual(broadcast=True)
        except asyncio.CancelledError:
            pass

    async def _delayed_mid_update(self, delay_secs: float, *, is_halfway: bool) -> None:
        try:
            await asyncio.sleep(delay_secs)
            if not self.is_active:
                return
            count = len(self.participants)
            user_str = "user has" if count == 1 else "users have"
            tmpl = self.halfway_message if is_halfway else self.final_hour_message
            text = format_template(
                tmpl,
                {
                    "count": count,
                    "user_str": user_str,
                    "channel": self.channel,
                    "keyword": self.keyword,
                },
            )
            if is_halfway:
                self.halfway_sent = True
            else:
                self.final_hour_sent = True
            self._save_state()
            await self._broadcast_chunks(text)
        except asyncio.CancelledError:
            pass

    async def _delayed_hourly_msg(self, delay_secs: float, hour_mark: int) -> None:
        try:
            await asyncio.sleep(delay_secs)
            if not self.is_active:
                return
            self.hourly_sent.append(hour_mark)
            self._save_state()
            await self._broadcast_chunks(self.hourly_message)
        except asyncio.CancelledError:
            pass

    async def _broadcast_chunks(self, text: str) -> None:
        if not text:
            return
        target_ch = self.hub.resolve_channel_target(self.channel)
        for chunk in split_to_byte_chunks(text, 130):
            await self.hub.tx_queue.send_channel(
                target_ch,
                chunk,
                priority=PRIORITY_BROADCAST,
                module=self.name,
                min_spacing=2.5,
                wait=False,
            )

    # ------------------------------------------------------------------
    # Manual & Scheduled Session Controls
    # ------------------------------------------------------------------

    async def start_net_manual(self) -> dict[str, Any]:
        """Open a net session immediately and broadcast the opening message."""
        try:
            tz = ZoneInfo(self.hub.timezone)
        except Exception:
            tz = ZoneInfo("UTC")

        self.is_active = True
        self.net_start_time = datetime.now(tz).isoformat()
        self.participants = []
        self.halfway_sent = False
        self.final_hour_sent = False
        self.hourly_sent = []
        self._save_state()

        duration_secs = self.duration * 3600.0
        self._start_session_timers(duration_secs, elapsed=0.0)

        open_text = format_template(
            self.opening_message,
            {
                "channel": self.channel,
                "keyword": self.keyword,
                "duration": self.duration,
                "time": self.time,
            },
        )
        await self._broadcast_chunks(open_text)
        self.hub.record_activity(
            module=self.name,
            event="net_started",
            sender=self.name,
            channel=self.channel,
            detail=open_text,
        )
        self.hub.notify_state_change()
        return {"ok": True, "message": f"Started {self.name} on {self.channel}"}

    async def stop_net_manual(self, *, broadcast: bool = True) -> dict[str, Any]:
        """Close the active net session, save summary, and optionally broadcast closing message."""
        self._cancel_session_timers()
        count = len(self.participants)
        user_str = "user" if count == 1 else "users"
        callsigns = [p["sender"] for p in self.participants]

        self.last_summary = {
            "ended_at": datetime.now().isoformat(),
            "started_at": self.net_start_time,
            "count": count,
            "participants": list(self.participants),
        }
        self.is_active = False
        self.net_start_time = None
        self._save_state()

        if broadcast:
            close_text = format_template(
                self.closing_message,
                {
                    "count": count,
                    "user_str": user_str,
                    "channel": self.channel,
                    "participants": ", ".join(callsigns) if callsigns else "none",
                },
            )
            await self._broadcast_chunks(close_text)
            if callsigns:
                roster_line = f"Check-ins ({count}): " + ", ".join(callsigns)
                await self._broadcast_chunks(roster_line)

        self.hub.record_activity(
            module=self.name,
            event="net_ended",
            sender=self.name,
            channel=self.channel,
            detail=f"Ended with {count} {user_str}",
        )
        self.hub.notify_state_change()
        return {"ok": True, "message": f"Stopped {self.name} ({count} check-ins)"}

    def clear_roster_manual(self) -> dict[str, Any]:
        """Clear current participant check-in roster without stopping the net."""
        self.participants = []
        self._save_state()
        self.hub.notify_state_change()
        return {"ok": True, "message": f"Cleared {self.name} check-in roster"}

    async def announce_reminder_manual(self) -> dict[str, Any]:
        """Broadcast an immediate net announcement/reminder on the configured channel."""
        if self.is_active:
            count = len(self.participants)
            user_str = "user has" if count == 1 else "users have"
            msg = f"Net is ACTIVE on {self.channel}! {count} {user_str} checked in. Send {self.keyword} to join!"
        else:
            msg = format_template(
                self.reminder_30m_message,
                {
                    "channel": self.channel,
                    "time": self.time,
                    "day": self.day_of_week,
                    "keyword": self.keyword,
                },
            )
        await self._broadcast_chunks(msg)
        self.hub.record_activity(
            module=self.name,
            event="net_announce",
            sender=self.name,
            channel=self.channel,
            detail=msg,
        )
        return {"ok": True, "message": f"Sent announcement to {self.channel}"}

    async def _on_1pm_reminder(self) -> None:
        if self.is_active or not self.enabled:
            return
        msg = format_template(
            self.reminder_1pm_message,
            {
                "channel": self.channel,
                "time": self.time,
                "day": self.day_of_week,
                "keyword": self.keyword,
            },
        )
        await self._broadcast_chunks(msg)

    async def _on_30m_reminder(self) -> None:
        if self.is_active or not self.enabled:
            return
        msg = format_template(
            self.reminder_30m_message,
            {
                "channel": self.channel,
                "time": self.time,
                "day": self.day_of_week,
                "keyword": self.keyword,
            },
        )
        await self._broadcast_chunks(msg)

    # ------------------------------------------------------------------
    # Incoming Check-in Handling
    # ------------------------------------------------------------------

    async def on_message(self, msg: dict[str, Any]) -> None:
        if not self.is_active:
            return
        channel = msg.get("channel")
        if channel is None:
            return
        sender = str(msg.get("sender", "unknown")).strip()
        if not sender or self.hub.is_self(sender):
            return
        if not self.hub.matches_channel(channel, self.channel):
            return

        text = str(msg.get("text", "")).strip()
        if not text:
            return

        has_keyword = bool(self.keyword and self.keyword.casefold() in text.casefold())
        if not has_keyword and not self.track_all_chat:
            return

        already_checked_in = any(
            p["sender"].lower() == sender.lower() for p in self.participants
        )
        if not already_checked_in:
            try:
                tz = ZoneInfo(self.hub.timezone)
            except Exception:
                tz = ZoneInfo("UTC")
            hops = msg.get("hops")
            if hops is None:
                pl = msg.get("path_len")
                hops = 0 if pl in (None, 255) else int(pl)
            self.participants.append(
                {
                    "sender": sender,
                    "timestamp": datetime.now(tz).strftime("%H:%M:%S"),
                    "snr": msg.get("snr"),
                    "hops": int(hops),
                }
            )
            self._save_state()
            self.hub.record_activity(
                module=self.name,
                event="net_checkin",
                sender=sender,
                channel=self.channel,
                detail=f"Checked in (#{len(self.participants)})",
            )
            self.hub.notify_state_change()

        # Send ACK when keyword was used and user wasn't already checked in
        # (for regular net_bot, only first check-in gets ACK; for meshwars_bot, explicit !checkin gets ACK)
        if has_keyword and (not already_checked_in or self.track_all_chat):
            if not self.hub.can_send_user(sender):
                return
            self.hub.record_user_send(sender)
            count = len(self.participants)
            user_str = "user" if count == 1 else "users"
            have_str = "has" if count == 1 else "have"
            ack_reply = format_template(
                self.checkin_ack_template,
                {
                    "sender": sender,
                    "count": count,
                    "user_str": user_str,
                    "have_str": have_str,
                    "channel": self.channel,
                },
            )
            target_ch = self.hub.resolve_channel_target(self.channel, fallback=channel)
            await self.hub.tx_queue.send_channel(
                target_ch,
                ack_reply,
                priority=PRIORITY_REPLY,
                module=self.name,
                wait=False,
            )

    def get_runtime_state(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "channel": self.channel,
            "day_of_week": self.day_of_week,
            "time": self.time,
            "duration": self.duration,
            "keyword": self.keyword,
            "is_active": self.is_active,
            "net_start_time": self.net_start_time,
            "participant_count": len(self.participants),
            "participants": list(self.participants),
            "last_summary": self.last_summary,
        }

    def get_help_segment(self) -> str:
        if not self.enabled:
            return ""
        return f"{self.keyword}({self.channel})"
