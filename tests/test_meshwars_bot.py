import unittest
import asyncio
import os
import json
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from modules.meshwars_bot import MeshwarsBot

class TestMeshwarsBotModule(unittest.TestCase):
    def setUp(self):
        self.module = MeshwarsBot()
        self.api = MagicMock()
        self.api.is_self = MagicMock(side_effect=lambda sender: sender == "TestBotDevice")
        self.api.matches_channel = AsyncMock(return_value=True)
        
        # Mock connection manager
        self.conn_manager = MagicMock()
        self.conn_manager.isConnected = True
        self.conn_manager.mc = MagicMock()
        self.conn_manager.mc.self_info = {"name": "TestBotDevice"}
        self.conn_manager.execute = AsyncMock(return_value={"ok": True})
        self.api.bot.connection_manager = self.conn_manager
        self.api.bot.timezone = "UTC"
        self.api.bot.config_path = "config/config.json"
        
        # Config block
        self.config = {
            "enabled": True,
            "channel": "#meshwars",
            "day_of_week": "Saturday",
            "time": "18:00",
            "duration": 6,
            "opening_message": "Welcome to the MeshWars net! Runs every Saturday from 6pm to midnight. Send !checkin or any chat message to participate.",
            "hourly_message": "Register on https://meshwars.com and any message sent in this chat counts toward your bonus points.",
            "halfway_message": "MeshWars Net Update: We are halfway through tonight's net! {count} {user_str} checked in so far.",
            "final_hour_message": "MeshWars Net Update: 1 hour remaining in tonight's net! {count} {user_str} checked in so far.",
            "closing_message": "The MeshWars net has concluded. Thank you for participating! {count} {user_str} checked in today.",
            "checkin_keyword": "!checkin",
            "checkin_ack_template": "Welcome {sender}, {count} {user_str} {have_str} checked in.",
            "enable_hourly_message": True,
            "enable_reminders": True,
            "enable_mid_net_updates": True,
            "state_file": "test_meshwars_state.json"
        }
        
        self.test_state_file = os.path.join("config", "test_meshwars_state.json")

    def tearDown(self):
        try:
            self.module.stop()
        except Exception:
            pass
        if os.path.exists(self.test_state_file):
            try:
                os.remove(self.test_state_file)
            except Exception:
                pass

    def test_init_defaults(self):
        self.module.init(self.api, self.config)
        self.assertEqual(self.module.api, self.api)
        self.assertEqual(self.module.channel, "#meshwars")
        self.assertEqual(self.module.day_of_week, "Saturday")
        self.assertEqual(self.module.time, "18:00")
        self.assertEqual(self.module.duration, 6.0)
        self.assertEqual(self.module.checkin_keyword, "!checkin")
        self.assertTrue(self.module.enable_hourly_message)
        self.assertTrue(self.module.enable_reminders)
        self.assertTrue(self.module.enable_mid_net_updates)
        self.assertEqual(self.module.timezone, "UTC")
        self.assertTrue(self.module.state_file_path.endswith("test_meshwars_state.json"))
        self.api.declare_channels.assert_called_with("#meshwars")

    def test_init_custom_duration_and_options(self):
        custom_config = dict(self.config)
        custom_config["duration"] = 4.5
        custom_config["enable_reminders"] = False
        custom_config["enable_mid_net_updates"] = False
        custom_config["enable_hourly_message"] = False
        custom_config["checkin_keyword"] = "!in"
        self.module.init(self.api, custom_config)
        self.assertEqual(self.module.duration, 4.5)
        self.assertFalse(self.module.enable_reminders)
        self.assertFalse(self.module.enable_mid_net_updates)
        self.assertFalse(self.module.enable_hourly_message)
        self.assertEqual(self.module.checkin_keyword, "!in")

    def test_all_messages_under_136_characters(self):
        self.module.init(self.api, self.config)
        
        # Test static messages
        self.assertLessEqual(len(self.module.opening_message), 136, "opening_message exceeds 136 chars")
        self.assertLessEqual(len(self.module.hourly_message), 136, "hourly_message exceeds 136 chars")
        
        # Test reminders
        r1 = f"Reminder: The weekly MeshWars net is today at {self.module.time}."
        r2 = f"Reminder: The weekly MeshWars net starts in 30 minutes at {self.module.time}."
        self.assertLessEqual(len(r1), 136, "1pm reminder exceeds 136 chars")
        self.assertLessEqual(len(r2), 136, "30m reminder exceeds 136 chars")
        
        # Test dynamic formatted messages with realistic worst-case values
        test_callsigns = ["KD9XYZ", "LONGNODENAME12345", "AliceInWonderland99"]
        test_counts = [1, 99, 999]
        
        for count in test_counts:
            user_str = "user" if count == 1 else "users"
            participant_str = "participant" if count == 1 else "participants"
            have_str = "has" if count == 1 else "have"
            
            half = self.module.halfway_message.format(
                count=count, user_str=user_str, participant_str=participant_str, channel=self.module.channel
            )
            self.assertLessEqual(len(half), 136, f"halfway_message with count={count} exceeds 136 chars: '{half}'")
            
            final = self.module.final_hour_message.format(
                count=count, user_str=user_str, participant_str=participant_str, channel=self.module.channel
            )
            self.assertLessEqual(len(final), 136, f"final_hour_message with count={count} exceeds 136 chars: '{final}'")
            
            close = self.module.closing_message.format(
                count=count, user_str=user_str, participant_str=participant_str, channel=self.module.channel
            )
            self.assertLessEqual(len(close), 136, f"closing_message with count={count} exceeds 136 chars: '{close}'")
            
            for callsign in test_callsigns:
                ack = self.module.checkin_ack_template.format(
                    sender=callsign, count=count, user_str=user_str, have_str=have_str, channel=self.module.channel
                )
                self.assertLessEqual(len(ack), 136, f"checkin_ack with sender={callsign}, count={count} exceeds 136 chars: '{ack}'")

    def test_cron_calculation_normal(self):
        # Saturday 18:00 -> Net at 18:00 (dow=6), 1pm reminder at 13:00 (dow=6), 30m prior at 17:30 (dow=6)
        net_cron, r1_cron, r2_cron = self.module._get_cron_expressions("Saturday", "18:00")
        self.assertEqual(net_cron, "0 18 * * 6")
        self.assertEqual(r1_cron, "0 13 * * 6")
        self.assertEqual(r2_cron, "30 17 * * 6")

    def test_cron_calculation_wrap(self):
        # Monday 00:15 -> Net at 00:15 (dow=1), 1pm reminder at 13:00 (dow=1), 30m prior at 23:45 on Sunday (dow=0)
        net_cron, r1_cron, r2_cron = self.module._get_cron_expressions("Monday", "00:15")
        self.assertEqual(net_cron, "15 0 * * 1")
        self.assertEqual(r1_cron, "0 13 * * 1")
        self.assertEqual(r2_cron, "45 23 * * 0")

    def test_cron_calculation_invalid(self):
        with self.assertRaises(ValueError):
            self.module._get_cron_expressions("BadDay", "18:00")
        with self.assertRaises(ValueError):
            self.module._get_cron_expressions("Saturday", "25:00")
        with self.assertRaises(ValueError):
            self.module._get_cron_expressions("Saturday", "18:65")

    def test_start_schedules_tasks_and_reminders(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.api.request_channel = AsyncMock(return_value=2)
            self.api.schedule_task = MagicMock(side_effect=[MagicMock(), MagicMock(), MagicMock()])
            self.module.init(self.api, self.config)
            
            loop.run_until_complete(self.module.start())
            
            self.assertEqual(self.module.channel_idx, 2)
            self.api.subscribe.assert_called_with("message", self.module._on_message)
            self.assertEqual(self.api.schedule_task.call_count, 3)
        finally:
            loop.close()

    def test_start_with_reminders_disabled(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            cfg = dict(self.config)
            cfg["enable_reminders"] = False
            self.api.request_channel = AsyncMock(return_value=2)
            self.api.schedule_task = MagicMock(return_value=MagicMock())
            self.module.init(self.api, cfg)
            
            loop.run_until_complete(self.module.start())
            
            # Only start task scheduled
            self.assertEqual(self.api.schedule_task.call_count, 1)
        finally:
            loop.close()

    def test_stop_cleans_up(self):
        unsub_msg = MagicMock()
        unsched_start = MagicMock()
        unsched_r1 = MagicMock()
        unsched_r2 = MagicMock()
        task_mock = MagicMock()
        half_mock = MagicMock()
        final_mock = MagicMock()
        hourly_task_mock = MagicMock()
        
        self.module.unsubscribe_msg = unsub_msg
        self.module.unschedule_start = unsched_start
        self.module.unschedule_r1pm = unsched_r1
        self.module.unschedule_r30m = unsched_r2
        self.module.net_end_task = task_mock
        self.module.halfway_task = half_mock
        self.module.final_hour_task = final_mock
        self.module.hourly_tasks = [hourly_task_mock]
        
        self.module.stop()
        
        unsub_msg.assert_called_once()
        unsched_start.assert_called_once()
        unsched_r1.assert_called_once()
        unsched_r2.assert_called_once()
        task_mock.cancel.assert_called_once()
        half_mock.cancel.assert_called_once()
        final_mock.cancel.assert_called_once()
        hourly_task_mock.cancel.assert_called_once()
        self.assertIsNone(self.module.net_end_task)
        self.assertIsNone(self.module.halfway_task)
        self.assertIsNone(self.module.final_hour_task)
        self.assertEqual(self.module.hourly_tasks, [])

    def _cleanup_loop(self, loop):
        tasks = [t for t in ([self.module.net_end_task, self.module.halfway_task, self.module.final_hour_task] + self.module.hourly_tasks) if t]
        for t in tasks:
            t.cancel()
        if tasks and loop and not loop.is_closed():
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        self.module.stop()
        if loop and not loop.is_closed():
            loop.close()

    def test_on_net_start_broadcasts_opening_and_schedules_updates(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            
            async def run_test():
                with patch.object(self.module, "_wait_and_end_net") as mock_wait:
                    await self.module._on_net_start()
                    
                    self.assertTrue(self.module.is_active)
                    self.assertIsNotNone(self.module.net_start_time)
                    self.assertEqual(self.module.checkins, [])
                    self.assertFalse(self.module.halfway_sent)
                    self.assertFalse(self.module.final_hour_sent)
                    self.assertEqual(self.module.hourly_sent, [])
                    
                    # Verifies opening message was broadcasted
                    self.conn_manager.execute.assert_called_with(["chan", "2", self.config["opening_message"]])
                    self.assertIsNotNone(self.module.net_end_task)
                    self.assertIsNotNone(self.module.halfway_task)
                    self.assertIsNotNone(self.module.final_hour_task)
                    self.assertEqual(len(self.module.hourly_tasks), 5) # hours 1..5 for 6h duration
                    mock_wait.assert_called_with(21600.0) # 6 hours * 3600
            loop.run_until_complete(run_test())
        finally:
            self._cleanup_loop(loop)

    def test_message_during_net_records_participant_silently_without_reply(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            self.module.is_active = True
            
            async def run_test():
                msg_data = {
                    "sender": "NodeAlpha",
                    "text": "Hello from NodeAlpha testing mesh!",
                    "channel": 2
                }
                self.module._on_message(msg_data)
                await asyncio.sleep(0.01)
                
                # Verified participant was recorded silently
                self.assertIn("NodeAlpha", self.module.checkins)
                self.assertEqual(len(self.module.checkins), 1)
                
                # Regular chat messages must NOT trigger a broadcast reply over RF!
                self.conn_manager.execute.assert_not_called()
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_checkin_command_broadcasts_ack(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            self.module.is_active = True
            
            async def run_test():
                msg_data = {
                    "sender": "NodeAlpha",
                    "text": "!checkin hello from my portable node!",
                    "channel": 2
                }
                self.module._on_message(msg_data)
                await asyncio.sleep(0.01)
                
                # Verified participant was recorded
                self.assertIn("NodeAlpha", self.module.checkins)
                self.assertEqual(len(self.module.checkins), 1)
                
                # Verified explicit !checkin sends ACK broadcast
                expected_ack = "Welcome NodeAlpha, 1 user has checked in."
                self.conn_manager.execute.assert_called_once_with(["chan", "2", expected_ack])
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_checkin_command_deduplication(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            self.module.is_active = True
            
            async def run_test():
                # First check-in
                self.module._on_message({"sender": "NodeAlpha", "text": "!checkin", "channel": 2})
                await asyncio.sleep(0.01)
                self.assertEqual(self.conn_manager.execute.call_count, 1)
                
                # Duplicate check-in from same node
                self.module._on_message({"sender": "NodeAlpha", "text": "!checkin again", "channel": 2})
                await asyncio.sleep(0.01)
                
                # Should not broadcast a second ACK
                self.assertEqual(self.conn_manager.execute.call_count, 1)
                self.assertEqual(self.module.checkins, ["NodeAlpha"])
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_message_deduplication_mixed(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            self.module.is_active = True
            
            async def run_test():
                # Regular message first
                self.module._on_message({"sender": "NodeAlpha", "text": "Regular message", "channel": 2})
                await asyncio.sleep(0.01)
                self.assertEqual(self.conn_manager.execute.call_count, 0)
                
                # Subsequent !checkin from already recorded user should not spam ACK
                self.module._on_message({"sender": "NodeAlpha", "text": "!checkin", "channel": 2})
                await asyncio.sleep(0.01)
                self.assertEqual(self.conn_manager.execute.call_count, 0)
                
                # Second user sends !checkin
                self.module._on_message({"sender": "NodeBeta", "text": "!checkin", "channel": 2})
                await asyncio.sleep(0.01)
                self.assertEqual(self.conn_manager.execute.call_count, 1)
                self.conn_manager.execute.assert_called_with(["chan", "2", "Welcome NodeBeta, 2 users have checked in."])
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_message_ignored_when_net_inactive(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.is_active = False
            
            self.module._on_message({"sender": "NodeAlpha", "text": "!checkin", "channel": 2})
            loop.run_until_complete(asyncio.sleep(0.01))
            
            self.assertEqual(self.module.checkins, [])
            self.conn_manager.execute.assert_not_called()
        finally:
            loop.close()

    def test_message_ignored_for_self_and_dm(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.is_active = True
            
            # Self message
            self.module._on_message({"sender": "TestBotDevice", "text": "!checkin", "channel": 2})
            # Direct message (channel is None)
            self.module._on_message({"sender": "NodeGamma", "text": "!checkin", "channel": None})
            
            loop.run_until_complete(asyncio.sleep(0.01))
            self.assertEqual(self.module.checkins, [])
        finally:
            loop.close()

    def test_reminders_broadcast(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            
            loop.run_until_complete(self.module._on_1pm_reminder())
            self.conn_manager.execute.assert_called_with(["chan", "2", "Reminder: The weekly MeshWars net is today at 18:00."])
            
            loop.run_until_complete(self.module._on_30m_reminder())
            self.conn_manager.execute.assert_called_with(["chan", "2", "Reminder: The weekly MeshWars net starts in 30 minutes at 18:00."])
        finally:
            loop.close()

    def test_hourly_message_broadcast(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            self.module.is_active = True
            
            loop.run_until_complete(self.module._send_hourly_update(1))
            
            self.assertIn(1, self.module.hourly_sent)
            self.conn_manager.execute.assert_called_with(["chan", "2", self.config["hourly_message"]])
        finally:
            loop.close()

    def test_halfway_update_broadcast(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            self.module.is_active = True
            self.module.checkins = ["Node1", "Node2"]
            
            loop.run_until_complete(self.module._send_halfway_update())
            
            self.assertTrue(self.module.halfway_sent)
            expected_msg = "MeshWars Net Update: We are halfway through tonight's net! 2 users checked in so far."
            self.conn_manager.execute.assert_called_with(["chan", "2", expected_msg])
        finally:
            loop.close()

    def test_final_hour_update_broadcast(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            self.module.is_active = True
            self.module.checkins = ["Node1"]
            
            loop.run_until_complete(self.module._send_final_hour_update())
            
            self.assertTrue(self.module.final_hour_sent)
            expected_msg = "MeshWars Net Update: 1 hour remaining in tonight's net! 1 user checked in so far."
            self.conn_manager.execute.assert_called_with(["chan", "2", expected_msg])
        finally:
            loop.close()

    def test_end_net_broadcasts_closing_with_count(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            self.module.is_active = True
            self.module.checkins = ["Node1", "Node2", "Node3"]
            
            loop.run_until_complete(self.module._end_net())
            
            self.assertFalse(self.module.is_active)
            self.assertEqual(self.module.checkins, [])
            expected_msg = "The MeshWars net has concluded. Thank you for participating! 3 users checked in today."
            self.conn_manager.execute.assert_called_with(["chan", "2", expected_msg])
        finally:
            loop.close()

    def test_end_net_broadcasts_singular_user(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.module.init(self.api, self.config)
            self.module.channel_idx = 2
            self.module.is_active = True
            self.module.checkins = ["Node1"]
            
            loop.run_until_complete(self.module._end_net())
            
            expected_msg = "The MeshWars net has concluded. Thank you for participating! 1 user checked in today."
            self.conn_manager.execute.assert_called_with(["chan", "2", expected_msg])
        finally:
            loop.close()

    def test_resume_net_active_and_mid_updates(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.api.request_channel = AsyncMock(return_value=2)
            self.module.init(self.api, self.config)
            
            # Started 4 hours ago (duration 6 hours -> halfway 3h has passed, 1-hour 5h is upcoming)
            start_time = (datetime.now(ZoneInfo("UTC")) - timedelta(hours=4)).isoformat()
            state_data = {
                "is_active": True,
                "net_start_time": start_time,
                "checkins": ["Alpha", "Beta"],
                "halfway_sent": True,
                "final_hour_sent": False,
                "hourly_sent": [1, 2, 3, 4]
            }
            with open(self.test_state_file, 'w') as f:
                json.dump(state_data, f)
                
            loop.run_until_complete(self.module.start())
            
            self.assertTrue(self.module.is_active)
            self.assertEqual(self.module.checkins, ["Alpha", "Beta"])
            self.assertTrue(self.module.halfway_sent)
            self.assertFalse(self.module.final_hour_sent)
            self.assertEqual(self.module.hourly_sent, [1, 2, 3, 4])
            # Halfway already sent, so halfway_task should NOT be scheduled
            self.assertIsNone(self.module.halfway_task)
            # 1-hour before end is at 5h elapsed (currently 4h), so final_hour_task SHOULD be scheduled
            self.assertIsNotNone(self.module.final_hour_task)
            # Only hour 5 remains for hourly announcements
            self.assertEqual(len(self.module.hourly_tasks), 1)
        finally:
            self._cleanup_loop(loop)

    def test_resume_net_expired(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.api.request_channel = AsyncMock(return_value=2)
            self.module.init(self.api, self.config)
            
            # Started 7 hours ago (duration 6 hours -> expired)
            start_time = (datetime.now(ZoneInfo("UTC")) - timedelta(hours=7)).isoformat()
            state_data = {
                "is_active": True,
                "net_start_time": start_time,
                "checkins": ["Alpha"]
            }
            with open(self.test_state_file, 'w') as f:
                json.dump(state_data, f)
                
            loop.run_until_complete(self.module.start())
            
            self.assertFalse(self.module.is_active)
            self.assertEqual(self.module.checkins, [])
            expected_msg = "The MeshWars net has concluded. Thank you for participating! 1 user checked in today."
            self.conn_manager.execute.assert_called_with(["chan", "2", expected_msg])
        finally:
            self.module.stop()
            loop.close()

    def test_run_config(self):
        inputs = [
            "y",                          # 1. enabled
            "#meshwars",                  # 2. channel
            "Saturday",                   # 3. day_of_week
            "18:00",                      # 4. time
            "6",                          # 5. duration
            "Welcome to MeshWars!",       # 6. opening_message
            "Register at meshwars.com!",  # 7. hourly_message
            "Halfway there!",             # 8. halfway_message
            "1 hour left!",               # 9. final_hour_message
            "Goodbye MeshWars {count}!",  # 10. closing_message
            "y",                          # 11. enable_reminders
            "y",                          # 12. enable_hourly_message
            "y",                          # 13. enable_mid_net_updates
            "meshwars_state.json",        # 14. state_file
            "America/Chicago"             # 15. timezone
        ]
        with patch("builtins.input", side_effect=inputs):
            res = self.module.run_config({})
            self.assertTrue(res["enabled"])
            self.assertEqual(res["channel"], "#meshwars")
            self.assertEqual(res["day_of_week"], "Saturday")
            self.assertEqual(res["time"], "18:00")
            self.assertEqual(res["duration"], 6)
            self.assertEqual(res["opening_message"], "Welcome to MeshWars!")
            self.assertEqual(res["hourly_message"], "Register at meshwars.com!")
            self.assertEqual(res["halfway_message"], "Halfway there!")
            self.assertEqual(res["final_hour_message"], "1 hour left!")
            self.assertEqual(res["closing_message"], "Goodbye MeshWars {count}!")
            self.assertTrue(res["enable_reminders"])
            self.assertTrue(res["enable_hourly_message"])
            self.assertTrue(res["enable_mid_net_updates"])
            self.assertEqual(res["timezone"], "America/Chicago")

if __name__ == '__main__':
    unittest.main()
