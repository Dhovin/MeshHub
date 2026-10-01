import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from modules.autoresponce import Autoresponce

class TestAutoresponceHopsLimit(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.api.bot = MagicMock()
        self.api.bot.connection_manager = MagicMock()
        self.api.bot.connection_manager.execute = AsyncMock(return_value={"ok": True})
        self.api.is_self = MagicMock(return_value=False)
        self.api.matches_channel = AsyncMock(return_value=True)
        self.api.can_send_user = MagicMock(return_value=True)
        self.api.can_send_channel = MagicMock(return_value=True)
        self.api.format_template = MagicMock(side_effect=lambda t, d: f"ACK | {d.get('sender')}")
        self.api.record_user_send = MagicMock()
        self.api.record_channel_send = MagicMock()

    def test_max_hops_filtering(self):
        mod = Autoresponce()
        mod.init(self.api, {
            "enabled": True,
            "channels": ["#test"],
            "maxHops": 3
        })
        mod.channel_indices = {"#test": 0}

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # 1. Message with 2 hops <= 3: should send reply
            loop.run_until_complete(mod._handle_message_async({
                "sender": "CloseNode",
                "text": "test ping",
                "channel": 0,
                "hops": 2
            }))
            self.api.bot.connection_manager.execute.assert_called_once()
            self.api.bot.connection_manager.execute.reset_mock()

            # 2. Message with 6 hops > 3: should be skipped
            loop.run_until_complete(mod._handle_message_async({
                "sender": "DistantNode",
                "text": "test ping",
                "channel": 0,
                "path_len": 6
            }))
            self.api.bot.connection_manager.execute.assert_not_called()

            # 3. Direct packet (path_len 255 -> 0 hops) <= 3: should send reply
            loop.run_until_complete(mod._handle_message_async({
                "sender": "DirectNode",
                "text": "test ping",
                "channel": 0,
                "path_len": 255
            }))
            self.api.bot.connection_manager.execute.assert_called_once()
        finally:
            loop.close()

    def test_direct_only_mode(self):
        mod = Autoresponce()
        mod.init(self.api, {
            "enabled": True,
            "channels": ["#test"],
            "maxHops": 0
        })
        mod.channel_indices = {"#test": 0}

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # 1-hop packet: should be skipped in direct-only mode
            loop.run_until_complete(mod._handle_message_async({
                "sender": "HopNode",
                "text": "test ping",
                "channel": 0,
                "hops": 1
            }))
            self.api.bot.connection_manager.execute.assert_not_called()

            # 0-hop direct packet: should be answered
            loop.run_until_complete(mod._handle_message_async({
                "sender": "DirectNode",
                "text": "test ping",
                "channel": 0,
                "hops": 0
            }))
            self.api.bot.connection_manager.execute.assert_called_once()
        finally:
            loop.close()

    def test_autoresponce_while_net_active(self):
        from modules.net_bot import NetBot
        net = NetBot()
        net.init(self.api, {
            "enabled": True,
            "channel": "#net",
            "day_of_week": "Tuesday",
            "time": "19:00",
            "keyword": "#checkin"
        })
        net.is_active = True

        mod = Autoresponce()
        mod.init(self.api, {
            "enabled": True,
            "channels": ["#test"],
            "maxHops": 15
        })
        mod.channel_indices = {"#test": 1}

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # While net is active, a test message on channel 1 should still trigger reply
            loop.run_until_complete(mod._handle_message_async({
                "sender": "Tester",
                "text": "test ping",
                "channel": 1,
                "hops": 0
            }))
            self.api.bot.connection_manager.execute.assert_called_once_with(["chan", "1", "ACK | Tester"])
        finally:
            loop.close()

    def test_fallback_target_idx(self):
        mod = Autoresponce()
        mod.init(self.api, {
            "enabled": True,
            "channels": ["#test"]
        })
        mod.channel_indices = {}  # Empty channel indices

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(mod._handle_message_async({
                "sender": "Tester",
                "text": "test ping",
                "channel": 2,
                "hops": 0
            }))
            self.assertEqual(mod.channel_indices.get("#test"), 2)
            self.api.bot.connection_manager.execute.assert_called_once_with(["chan", "2", "ACK | Tester"])
        finally:
            loop.close()

if __name__ == "__main__":
    unittest.main()
