import os
import json
import logging
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("MeshwarsBotModule")

class MeshwarsBot:
    def __init__(self):
        self.name = "meshwars_bot"
        self.api = None
        self.config = {}
        self.config_schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "channel": {"type": "string"},
                "day_of_week": {"type": "string"},
                "time": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
                "duration": {"type": "number", "minimum": 0.1},
                "opening_message": {"type": "string"},
                "halfway_message": {"type": "string"},
                "final_hour_message": {"type": "string"},
                "closing_message": {"type": "string"},
                "enable_reminders": {"type": "boolean"},
                "enable_mid_net_updates": {"type": "boolean"},
                "state_file": {"type": "string"},
                "timezone": {"type": "string"}
            },
            "required": ["enabled", "channel", "day_of_week", "time"]
        }
        
        self.channel = "#meshwars"
        self.day_of_week = "Tuesday"
        self.time = "18:00"
        self.duration = 6.0
        self.opening_message = (
            "Welcome to the MeshWars net! Make sure to register on https://meshwars.com "
            "and send any message in this chat. This net runs every tuesday from 6pm to midnight."
        )
        self.halfway_message = (
            "MeshWars Net Update: We are halfway through tonight's net! "
            "{count} {user_str} checked in so far. Register on https://meshwars.com and send any message in this chat."
        )
        self.final_hour_message = (
            "MeshWars Net Update: 1 hour remaining in tonight's net! "
            "{count} {user_str} checked in so far. Register on https://meshwars.com and send any message in this chat."
        )
        self.closing_message = (
            "The MeshWars net has concluded. Thank you for participating! "
            "{count} {user_str} checked in today."
        )
        self.enable_reminders = True
        self.enable_mid_net_updates = True
        self.state_file = "meshwars_state.json"
        self.timezone = "UTC"
        self.state_file_path = ""
        
        # Subscriptions and task handles
        self.unsubscribe_msg = None
        self.unschedule_start = None
        self.unschedule_r1pm = None
        self.unschedule_r30m = None
        self.net_end_task = None
        self.halfway_task = None
        self.final_hour_task = None
        
        # Active session state
        self.is_active = False
        self.net_start_time = None
        self.checkins = []
        self.halfway_sent = False
        self.final_hour_sent = False
        self.channel_idx = 0

    def run_config(self, current_config):
        """
        Interactive configuration wizard for the MeshwarsBot module.
        Prompts the user for key settings.
        """
        config = dict(current_config) if current_config else {}
        
        print("\n--- Configure MeshWars Net Settings ---")
        
        # 1. Enabled
        current_enabled = config.get("enabled", True)
        val = input(f"Enable MeshWars Module? (y/n) [current: {'y' if current_enabled else 'n'}]: ").strip().lower()
        if val:
            config["enabled"] = val in ("y", "yes", "true", "1")
        elif "enabled" not in config:
            config["enabled"] = current_enabled
            
        # 2. Channel
        current_channel = config.get("channel", "#meshwars")
        val = input(f"Enter Channel to monitor [current: {current_channel}]: ").strip()
        if val:
            config["channel"] = val
        elif "channel" not in config:
            config["channel"] = current_channel
            
        # 3. Day of week
        current_dow = config.get("day_of_week", "Tuesday")
        val = input(f"Enter Day of the week for the Net [current: {current_dow}]: ").strip()
        if val:
            config["day_of_week"] = val.capitalize()
        elif "day_of_week" not in config:
            config["day_of_week"] = current_dow
            
        # 4. Time
        current_time = config.get("time", "18:00")
        val = input(f"Enter Time (HH:MM) for the Net [current: {current_time}]: ").strip()
        if val:
            config["time"] = val
        elif "time" not in config:
            config["time"] = current_time

        # 5. Duration (in hours)
        current_duration = config.get("duration", 6.0)
        val = input(f"Enter Net Duration in hours [current: {current_duration}]: ").strip()
        if val:
            try:
                num = float(val)
                config["duration"] = int(num) if num.is_integer() else num
            except ValueError:
                print("Invalid number, keeping current duration.")
                config["duration"] = current_duration
        elif "duration" not in config:
            config["duration"] = current_duration

        # 6. Opening Message
        current_open_msg = config.get("opening_message", self.opening_message)
        val = input(f"Enter Opening Message [current: {current_open_msg}]: ").strip()
        if val:
            config["opening_message"] = val
        elif "opening_message" not in config:
            config["opening_message"] = current_open_msg

        # 7. Halfway Message
        current_half_msg = config.get("halfway_message", self.halfway_message)
        val = input(f"Enter Halfway Message [current: {current_half_msg}]: ").strip()
        if val:
            config["halfway_message"] = val
        elif "halfway_message" not in config:
            config["halfway_message"] = current_half_msg

        # 8. Final Hour Message
        current_final_msg = config.get("final_hour_message", self.final_hour_message)
        val = input(f"Enter 1-Hour Remaining Message [current: {current_final_msg}]: ").strip()
        if val:
            config["final_hour_message"] = val
        elif "final_hour_message" not in config:
            config["final_hour_message"] = current_final_msg

        # 9. Closing Message
        current_close_msg = config.get("closing_message", self.closing_message)
        val = input(f"Enter Closing Message [current: {current_close_msg}]: ").strip()
        if val:
            config["closing_message"] = val
        elif "closing_message" not in config:
            config["closing_message"] = current_close_msg

        # 10. Enable Reminders
        current_reminders = config.get("enable_reminders", True)
        val = input(f"Enable Reminders (1pm & 30m prior)? (y/n) [current: {'y' if current_reminders else 'n'}]: ").strip().lower()
        if val:
            config["enable_reminders"] = val in ("y", "yes", "true", "1")
        elif "enable_reminders" not in config:
            config["enable_reminders"] = current_reminders

        # 11. Enable Mid-net Updates
        current_mid_updates = config.get("enable_mid_net_updates", True)
        val = input(f"Enable Halfway & 1-Hour Updates? (y/n) [current: {'y' if current_mid_updates else 'n'}]: ").strip().lower()
        if val:
            config["enable_mid_net_updates"] = val in ("y", "yes", "true", "1")
        elif "enable_mid_net_updates" not in config:
            config["enable_mid_net_updates"] = current_mid_updates

        # 12. State file
        current_state_file = config.get("state_file", "meshwars_state.json")
        val = input(f"Enter State File Name [current: {current_state_file}]: ").strip()
        if val:
            config["state_file"] = val
        elif "state_file" not in config:
            config["state_file"] = current_state_file

        # 13. Timezone
        current_tz = config.get("timezone", "America/Chicago")
        val = input(f"Enter Timezone (e.g. America/Chicago, America/New_York) [current: {current_tz}]: ").strip()
        if val:
            config["timezone"] = val
        elif "timezone" not in config:
            config["timezone"] = current_tz
            
        return config

    def init(self, api, config):
        self.api = api
        self.config = config
        self.channel = config.get("channel", "#meshwars")
        self.day_of_week = config.get("day_of_week", "Tuesday")
        self.time = config.get("time", "18:00")
        try:
            self.duration = float(config.get("duration", config.get("duration_hours", 6.0)))
        except (ValueError, TypeError):
            self.duration = 6.0
            
        self.opening_message = config.get("opening_message", self.opening_message)
        self.halfway_message = config.get("halfway_message", self.halfway_message)
        self.final_hour_message = config.get("final_hour_message", self.final_hour_message)
        self.closing_message = config.get("closing_message", self.closing_message)
        self.enable_reminders = config.get("enable_reminders", True)
        self.enable_mid_net_updates = config.get("enable_mid_net_updates", True)
        self.state_file = config.get("state_file", "meshwars_state.json")
        
        # Try to inherit from bot timezone if not explicitly provided
        self.timezone = config.get("timezone") or getattr(api.bot, "timezone", "UTC")
        
        config_dir = os.path.dirname(os.path.abspath(self.api.bot.config_path))
        self.state_file_path = os.path.join(config_dir, self.state_file)
        
        # Declare the channel index/name to the module manager
        self.api.declare_channels(self.channel)
        logger.info(f"[{self.name}] Initialized with channel: {self.channel}, timezone: {self.timezone}")

    async def start(self):
        logger.info(f"[{self.name}] Starting MeshwarsBot module...")
        
        # 1. Resolve channel index
        try:
            self.channel_idx = await self.api.request_channel(self.channel)
            logger.info(f"[{self.name}] MeshWars channel '{self.channel}' mapped to index {self.channel_idx}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to request channel '{self.channel}': {e}")
            self.channel_idx = 0
            
        # 2. Subscribe to incoming messages (for silent participant tracking)
        self.unsubscribe_msg = self.api.subscribe("message", self._on_message)
        
        # 3. Schedule periodic tasks
        net_cron, r1pm_cron, r30m_cron = self._get_cron_expressions(self.day_of_week, self.time)
        
        logger.info(f"[{self.name}] Scheduling MeshWars Net start with cron: '{net_cron}'")
        self.unschedule_start = self.api.schedule_task(net_cron, self._on_net_start, timezone=self.timezone)
        
        if self.enable_reminders:
            logger.info(f"[{self.name}] Scheduling 1 PM reminder with cron: '{r1pm_cron}'")
            self.unschedule_r1pm = self.api.schedule_task(r1pm_cron, self._on_1pm_reminder, timezone=self.timezone)
            
            logger.info(f"[{self.name}] Scheduling 30-min reminder with cron: '{r30m_cron}'")
            self.unschedule_r30m = self.api.schedule_task(r30m_cron, self._on_30m_reminder, timezone=self.timezone)
        
        # 4. Load persisted state and check if we should resume
        self._load_state()
        if self.is_active and self.net_start_time:
            try:
                start_dt = datetime.fromisoformat(self.net_start_time)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=ZoneInfo(self.timezone))
                
                now = datetime.now(ZoneInfo(self.timezone))
                elapsed = (now - start_dt).total_seconds()
                duration_seconds = self.duration * 3600
                
                if elapsed < duration_seconds and elapsed >= 0:
                    remaining = duration_seconds - elapsed
                    logger.info(f"[{self.name}] Resuming active MeshWars Net started at {self.net_start_time}. Remaining time: {remaining:.1f}s")
                    self.net_end_task = asyncio.create_task(self._wait_and_end_net(remaining))
                    self._schedule_mid_net_updates(duration_seconds, elapsed=elapsed)
                else:
                    logger.info(f"[{self.name}] Active MeshWars Net from state file has expired (elapsed: {elapsed:.1f}s). Ending it.")
                    await self._end_net()
            except Exception as e:
                logger.error(f"[{self.name}] Error trying to resume MeshWars Net: {e}", exc_info=True)
                self.is_active = False
                self.checkins = []
                self.halfway_sent = False
                self.final_hour_sent = False
                self._save_state()

    def stop(self):
        logger.info(f"[{self.name}] Stopping MeshwarsBot module...")
        
        if self.unsubscribe_msg:
            self.unsubscribe_msg()
            self.unsubscribe_msg = None
        if self.unschedule_start:
            self.unschedule_start()
            self.unschedule_start = None
        if self.unschedule_r1pm:
            self.unschedule_r1pm()
            self.unschedule_r1pm = None
        if self.unschedule_r30m:
            self.unschedule_r30m()
            self.unschedule_r30m = None
            
        if self.net_end_task:
            self.net_end_task.cancel()
            self.net_end_task = None
            
        if self.halfway_task:
            self.halfway_task.cancel()
            self.halfway_task = None
            
        if self.final_hour_task:
            self.final_hour_task.cancel()
            self.final_hour_task = None
            
        logger.info(f"[{self.name}] Stopped successfully.")

    def _get_cron_expressions(self, day_of_week_str, time_str):
        day_map = {
            "sunday": 0, "mon": 1, "monday": 1, "tue": 2, "tuesday": 2,
            "wed": 3, "wednesday": 3, "thu": 4, "thursday": 4,
            "fri": 5, "friday": 5, "sat": 6, "saturday": 6
        }
        dow = day_map.get(day_of_week_str.strip().lower())
        if dow is None:
            raise ValueError(f"Invalid day_of_week: {day_of_week_str}")
        
        parts = time_str.strip().split(':')
        if len(parts) != 2:
            raise ValueError(f"Invalid time format: {time_str}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            raise ValueError(f"Invalid hour/minute: {time_str}")
            
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

    def _load_state(self):
        if not os.path.exists(self.state_file_path):
            self.is_active = False
            self.net_start_time = None
            self.checkins = []
            self.halfway_sent = False
            self.final_hour_sent = False
            return
            
        try:
            with open(self.state_file_path, 'r') as f:
                data = json.load(f)
                self.is_active = data.get("is_active", False)
                self.net_start_time = data.get("net_start_time")
                self.checkins = data.get("checkins", [])
                self.halfway_sent = data.get("halfway_sent", False)
                self.final_hour_sent = data.get("final_hour_sent", False)
                logger.info(f"[{self.name}] Loaded state: is_active={self.is_active}, checkins_count={len(self.checkins)}, halfway_sent={self.halfway_sent}, final_hour_sent={self.final_hour_sent}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to load state from {self.state_file_path}: {e}")
            self.is_active = False
            self.net_start_time = None
            self.checkins = []
            self.halfway_sent = False
            self.final_hour_sent = False

    def _save_state(self):
        try:
            data = {
                "is_active": self.is_active,
                "net_start_time": self.net_start_time,
                "checkins": self.checkins,
                "halfway_sent": self.halfway_sent,
                "final_hour_sent": self.final_hour_sent
            }
            with open(self.state_file_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to save state to {self.state_file_path}: {e}")

    def _on_message(self, data):
        if not self.is_active:
            return
            
        sender = data.get("sender", "unknown")
        channel = data.get("channel")
        
        # Don't record messages from ourselves
        if self.api.is_self(sender):
            return
            
        # Direct messages do not map to the target Net channel
        if channel is None:
            return

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._handle_message_async(sender, channel))
        except RuntimeError:
            pass

    async def _handle_message_async(self, sender, channel):
        if not await self.api.matches_channel(channel, self.channel):
            return
            
        # Deduplicate participants
        if sender in self.checkins:
            return
            
        # Silently record the participant without sending any reply message
        self.checkins.append(sender)
        self._save_state()
        logger.info(f"[{self.name}] Recorded MeshWars participant: {sender} (total: {len(self.checkins)})")

    async def _send_broadcast(self, text):
        res = await self.api.bot.connection_manager.execute(["chan", str(self.channel_idx), text])
        logger.debug(f"[{self.name}] Broadcast message response: {res}")

    def _schedule_mid_net_updates(self, total_duration_seconds, elapsed=0.0):
        if self.halfway_task:
            self.halfway_task.cancel()
            self.halfway_task = None
        if self.final_hour_task:
            self.final_hour_task.cancel()
            self.final_hour_task = None

        if not self.enable_mid_net_updates:
            return

        # Halfway mark
        halfway_target = total_duration_seconds / 2.0
        halfway_delay = halfway_target - elapsed
        if halfway_delay > 0 and not self.halfway_sent:
            self.halfway_task = asyncio.create_task(self._wait_and_send_halfway(halfway_delay))

        # Final hour mark (1 hour before end = total_duration_seconds - 3600.0)
        final_hour_target = total_duration_seconds - 3600.0
        final_hour_delay = final_hour_target - elapsed
        # Only schedule final hour update if it occurs strictly after the halfway mark
        if final_hour_target > halfway_target and final_hour_delay > 0 and not self.final_hour_sent:
            self.final_hour_task = asyncio.create_task(self._wait_and_send_final_hour(final_hour_delay))

    async def _wait_and_send_halfway(self, delay):
        try:
            await asyncio.sleep(delay)
            await self._send_halfway_update()
        except asyncio.CancelledError:
            logger.debug(f"[{self.name}] Halfway update timer cancelled.")

    async def _send_halfway_update(self):
        if not self.is_active:
            return
        logger.info(f"[{self.name}] Sending halfway update message.")
        count = len(self.checkins)
        user_str = "user" if count == 1 else "users"
        participant_str = "participant" if count == 1 else "participants"
        if self.halfway_message:
            try:
                msg = self.halfway_message.format(
                    count=count,
                    user_str=user_str,
                    participant_str=participant_str,
                    channel=self.channel
                )
            except Exception:
                msg = f"MeshWars Net Update: We are halfway through tonight's net! {count} {user_str} checked in so far."
            await self._send_broadcast(msg)
        self.halfway_sent = True
        self._save_state()

    async def _wait_and_send_final_hour(self, delay):
        try:
            await asyncio.sleep(delay)
            await self._send_final_hour_update()
        except asyncio.CancelledError:
            logger.debug(f"[{self.name}] Final hour update timer cancelled.")

    async def _send_final_hour_update(self):
        if not self.is_active:
            return
        logger.info(f"[{self.name}] Sending final hour update message.")
        count = len(self.checkins)
        user_str = "user" if count == 1 else "users"
        participant_str = "participant" if count == 1 else "participants"
        if self.final_hour_message:
            try:
                msg = self.final_hour_message.format(
                    count=count,
                    user_str=user_str,
                    participant_str=participant_str,
                    channel=self.channel
                )
            except Exception:
                msg = f"MeshWars Net Update: 1 hour remaining in tonight's net! {count} {user_str} checked in so far."
            await self._send_broadcast(msg)
        self.final_hour_sent = True
        self._save_state()

    async def _on_net_start(self):
        logger.info(f"[{self.name}] Weekly MeshWars Net start triggered!")
        self.is_active = True
        self.net_start_time = datetime.now(ZoneInfo(self.timezone)).isoformat()
        self.checkins = []
        self.halfway_sent = False
        self.final_hour_sent = False
        self._save_state()
        
        if self.opening_message:
            await self._send_broadcast(self.opening_message)
        
        if self.net_end_task:
            self.net_end_task.cancel()
        total_seconds = self.duration * 3600
        self.net_end_task = asyncio.create_task(self._wait_and_end_net(total_seconds))
        self._schedule_mid_net_updates(total_seconds, elapsed=0.0)

    async def _on_1pm_reminder(self):
        logger.info(f"[{self.name}] 1 PM reminder triggered.")
        reminder_msg = f"Reminder: The weekly MeshWars net is today at {self.time}."
        await self._send_broadcast(reminder_msg)

    async def _on_30m_reminder(self):
        logger.info(f"[{self.name}] 30-minute reminder triggered.")
        reminder_msg = f"Reminder: The weekly MeshWars net starts in 30 minutes at {self.time}."
        await self._send_broadcast(reminder_msg)

    async def _wait_and_end_net(self, duration):
        try:
            await asyncio.sleep(duration)
            await self._end_net()
        except asyncio.CancelledError:
            logger.info(f"[{self.name}] Net end timer cancelled.")

    async def _end_net(self):
        logger.info(f"[{self.name}] Ending the MeshWars Net session.")
        count = len(self.checkins)
        user_str = "user" if count == 1 else "users"
        participant_str = "participant" if count == 1 else "participants"
        
        if self.closing_message:
            try:
                end_msg = self.closing_message.format(
                    count=count,
                    user_str=user_str,
                    participant_str=participant_str,
                    channel=self.channel
                )
            except Exception:
                end_msg = f"The MeshWars net has concluded. Thank you for participating! {count} {user_str} checked in today."
            await self._send_broadcast(end_msg)
        
        if self.halfway_task:
            self.halfway_task.cancel()
            self.halfway_task = None
        if self.final_hour_task:
            self.final_hour_task.cancel()
            self.final_hour_task = None
            
        self.is_active = False
        self.checkins = []
        self.halfway_sent = False
        self.final_hour_sent = False
        self._save_state()
