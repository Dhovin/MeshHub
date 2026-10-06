import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from core.bot import MeshHub

class TestStartupChannelSync(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_get_used_channels(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.config = {
                "modules": {
                    "autoresponce": {
                        "enabled": True,
                        "channels": ["#test", "#testing"]
                    },
                    "net_bot": {
                        "enabled": True,
                        "channel": "#net"
                    },
                    "weather_bot": {
                        "enabled": True,
                        "channels": {
                            "alerts": "#weather_alerts",
                            "weather": "#weather"
                        }
                    },
                    "disabled_module": {
                        "enabled": False,
                        "channel": "#disabled_channel"
                    },
                    "template": {
                        "enabled": True,
                        "logChannel": 0
                    }
                }
            }
            
            # Module manager also tracks declared channels
            bot.module_manager.module_channels = {
                "autoresponce": {"#test", "#testing"},
                "net_bot": {"#net"}
            }

            used = bot.get_used_channels()
            self.assertIn("#test", used)
            self.assertIn("#testing", used)
            self.assertIn("#net", used)
            self.assertIn("#weather_alerts", used)
            self.assertIn("#weather", used)
            self.assertIn(0, used)
            # Disabled module's channel must not be included
            self.assertNotIn("#disabled_channel", used)

    def test_sync_channels_removes_unused_hash_channel(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = True
            
            bot.config = {
                "modules": {
                    "net_bot": {"enabled": True, "channel": "#net"}
                }
            }
            bot.module_manager.module_channels = {"net_bot": {"#net"}}

            node_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": "#net"},
                {"channel_idx": 2, "channel_name": "#old_channel"}
            ]

            executed_commands = []
            async def mock_execute(cmd):
                executed_commands.append(cmd)
                if cmd == "channels":
                    return node_channels
                elif isinstance(cmd, list) and cmd[0] == "remove_channel":
                    idx = int(cmd[1])
                    for ch in node_channels:
                        if ch["channel_idx"] == idx:
                            ch["channel_name"] = ""
                    return {"ok": f"channel {idx} removed"}
                return {"ok": True}

            bot.connection_manager.execute = AsyncMock(side_effect=mock_execute)

            self.loop.run_until_complete(bot.sync_channels())

            # Slot 2 (#old_channel) must be removed
            bot.connection_manager.execute.assert_any_call(["remove_channel", "2"])
            # Slot 0 (primary) and Slot 1 (#net) must NOT be removed
            self.assertNotIn(["remove_channel", "0"], executed_commands)
            self.assertNotIn(["remove_channel", "1"], executed_commands)

    def test_sync_channels_preserves_non_hash_channel(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = True
            
            bot.config = {
                "modules": {
                    "net_bot": {"enabled": True, "channel": "#net"}
                }
            }
            bot.module_manager.module_channels = {"net_bot": {"#net"}}

            node_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": "private_custom_key"},
                {"channel_idx": 2, "channel_name": ""}
            ]

            executed_commands = []
            async def mock_execute(cmd):
                executed_commands.append(cmd)
                if cmd == "channels":
                    return node_channels
                elif isinstance(cmd, list) and cmd[0] == "set_channel":
                    return {"ok": True}
                return {"ok": True}

            bot.connection_manager.execute = AsyncMock(side_effect=mock_execute)

            self.loop.run_until_complete(bot.sync_channels())

            # Non-hash channel "private_custom_key" must NOT be removed
            self.assertNotIn(["remove_channel", "1"], executed_commands)
            # '#net' should be added into the empty slot 2
            bot.connection_manager.execute.assert_any_call(["set_channel", "2", "#net"])

    def test_sync_channels_adds_used_hash_channel(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = True
            
            bot.config = {
                "modules": {
                    "net_bot": {"enabled": True, "channel": "#net"},
                    "weather_bot": {"enabled": True, "channel": "#weather"}
                }
            }
            bot.module_manager.module_channels = {
                "net_bot": {"#net"},
                "weather_bot": {"#weather"}
            }

            node_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": "#net"},
                {"channel_idx": 2, "channel_name": ""}
            ]

            bot.connection_manager.execute = AsyncMock(side_effect=lambda cmd: node_channels if cmd == "channels" else {"ok": True})

            self.loop.run_until_complete(bot.sync_channels())

            bot.connection_manager.execute.assert_any_call(["set_channel", "2", "#weather"])

    def test_sync_channels_always_leaves_public_channel(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = True
            
            # App only uses '#net'
            bot.config = {
                "modules": {
                    "net_bot": {"enabled": True, "channel": "#net"}
                }
            }
            bot.module_manager.module_channels = {"net_bot": {"#net"}}

            # Channel 0 is empty name, channel 1 is empty name
            node_channels = [
                {"channel_idx": 0, "channel_name": ""},
                {"channel_idx": 1, "channel_name": ""}
            ]

            executed_commands = []
            async def mock_execute(cmd):
                executed_commands.append(cmd)
                if cmd == "channels":
                    return node_channels
                return {"ok": True}

            bot.connection_manager.execute = AsyncMock(side_effect=mock_execute)

            self.loop.run_until_complete(bot.sync_channels())

            # Channel 0 must NEVER be removed
            self.assertNotIn(["remove_channel", "0"], executed_commands)
            # Channel 0 must NEVER be overwritten with '#net'; it must be placed at slot 1
            self.assertNotIn(["set_channel", "0", "#net"], executed_commands)
            bot.connection_manager.execute.assert_any_call(["set_channel", "1", "#net"])

    def test_sync_channels_removes_and_reuses_slot(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = True
            
            bot.config = {
                "modules": {
                    "meshwars": {"enabled": True, "channel": "#meshwars"}
                }
            }
            bot.module_manager.module_channels = {"meshwars": {"#meshwars"}}

            node_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": "#obsolete"}
            ]

            executed_commands = []
            async def mock_execute(cmd):
                executed_commands.append(cmd)
                if cmd == "channels":
                    return node_channels
                elif isinstance(cmd, list) and cmd[0] == "remove_channel":
                    for ch in node_channels:
                        if ch["channel_idx"] == int(cmd[1]):
                            ch["channel_name"] = ""
                    return {"ok": True}
                return {"ok": True}

            bot.connection_manager.execute = AsyncMock(side_effect=mock_execute)

            self.loop.run_until_complete(bot.sync_channels())

            # #obsolete was removed from slot 1
            bot.connection_manager.execute.assert_any_call(["remove_channel", "1"])
            # #meshwars reuses the newly freed slot 1
            bot.connection_manager.execute.assert_any_call(["set_channel", "1", "#meshwars"])

    def test_sync_channels_case_insensitivity(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = True
            
            bot.config = {
                "modules": {
                    "test_mod": {"enabled": True, "channel": "#test"}
                }
            }
            bot.module_manager.module_channels = {"test_mod": {"#test"}}

            node_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": "#TEST"}
            ]

            executed_commands = []
            async def mock_execute(cmd):
                executed_commands.append(cmd)
                if cmd == "channels":
                    return node_channels
                return {"ok": True}

            bot.connection_manager.execute = AsyncMock(side_effect=mock_execute)

            self.loop.run_until_complete(bot.sync_channels())

            # Slot 1 must not be removed or re-added
            self.assertNotIn(["remove_channel", "1"], executed_commands)
            self.assertNotIn(["set_channel", "1", "#test"], executed_commands)

    def test_sync_channels_when_disconnected(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = False
            bot.connection_manager.execute = AsyncMock()

            self.loop.run_until_complete(bot.sync_channels())

            bot.connection_manager.execute.assert_not_called()

    def test_weather_module_channels_without_hash_are_normalized_and_added(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = True
            
            # Weather module configured with plain 'weather' (no # prefix)
            bot.config = {
                "modules": {
                    "weather_bot": {
                        "enabled": True,
                        "channels": {"alerts": "weather", "weather": "weather"}
                    }
                }
            }
            # Module manager has 'weather'
            bot.module_manager.module_channels = {"weather_bot": {"weather"}}

            node_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": ""}
            ]

            executed_commands = []
            async def mock_execute(cmd):
                executed_commands.append(cmd)
                if cmd == "channels":
                    return node_channels
                return {"ok": True}

            bot.connection_manager.execute = AsyncMock(side_effect=mock_execute)

            self.loop.run_until_complete(bot.sync_channels())

            # Must normalize 'weather' to '#weather' and add to slot 1
            bot.connection_manager.execute.assert_any_call(["set_channel", "1", "#weather"])
            self.assertNotIn(["remove_channel", "0"], executed_commands)

    def test_weather_module_dual_channels_added(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = True
            
            # Weather module configured with two distinct channels
            bot.config = {
                "modules": {
                    "weather_bot": {
                        "enabled": True,
                        "channels": {"alerts": "#weather_alerts", "weather": "#weather"}
                    }
                }
            }
            bot.module_manager.module_channels = {"weather_bot": {"#weather_alerts", "#weather"}}

            node_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": ""},
                {"channel_idx": 2, "channel_name": ""}
            ]

            executed_commands = []
            async def mock_execute(cmd):
                executed_commands.append(cmd)
                if cmd == "channels":
                    return node_channels
                return {"ok": True}

            bot.connection_manager.execute = AsyncMock(side_effect=mock_execute)

            self.loop.run_until_complete(bot.sync_channels())

            # Both channels must be added
            self.assertTrue(
                any(c[0] == "set_channel" and "#weather" in c for c in executed_commands),
                "Expected #weather to be set"
            )
            self.assertTrue(
                any(c[0] == "set_channel" and "#weather_alerts" in c for c in executed_commands),
                "Expected #weather_alerts to be set"
            )

    def test_weather_module_channels_retained_on_node(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = True
            
            bot.config = {
                "modules": {
                    "weather_bot": {
                        "enabled": True,
                        "channels": {"alerts": "weather", "weather": "weather"}
                    }
                }
            }
            bot.module_manager.module_channels = {"weather_bot": {"weather"}}

            # Node already has #weather
            node_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": "#weather"}
            ]

            executed_commands = []
            async def mock_execute(cmd):
                executed_commands.append(cmd)
                if cmd == "channels":
                    return node_channels
                return {"ok": True}

            bot.connection_manager.execute = AsyncMock(side_effect=mock_execute)

            self.loop.run_until_complete(bot.sync_channels())

            # Should not remove #weather and should not re-add
            self.assertNotIn(["remove_channel", "1"], executed_commands)
            self.assertNotIn(["set_channel", "1", "#weather"], executed_commands)

    def test_weather_module_with_wx_channel_synced_and_obsolete_weather_removed(self):
        with patch.object(MeshHub, 'load_and_validate_config'), \
             patch.object(MeshHub, 'setup_logging'):
            bot = MeshHub()
            bot.connection_manager.isConnected = True
            
            # Weather module configured with #wx because user changed #weather to #wx
            bot.config = {
                "modules": {
                    "weather_bot": {
                        "enabled": True,
                        "channels": {"alerts": "#wx", "weather": "#wx"}
                    }
                }
            }
            bot.module_manager.module_channels = {"weather_bot": {"#wx"}}

            # Node currently has #weather on slot 2, but app uses #wx now
            node_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": ""},
                {"channel_idx": 2, "channel_name": "#weather"}
            ]

            executed_commands = []
            async def mock_execute(cmd):
                executed_commands.append(cmd)
                if cmd == "channels":
                    return node_channels
                return {"ok": True}

            bot.connection_manager.execute = AsyncMock(side_effect=mock_execute)

            self.loop.run_until_complete(bot.sync_channels())

            # Slot 2 (#weather) must be removed because app no longer uses #weather
            self.assertIn(["remove_channel", "2"], executed_commands)
            # Slot 1 must be set to #wx (keyless hash channel)
            self.assertIn(["set_channel", "1", "#wx"], executed_commands)
            # Slot 0 must not be touched
            self.assertNotIn(["remove_channel", "0"], executed_commands)

    def test_weather_module_default_channels_use_wx(self):
        from modules.weather_bot import WeatherBot
        wbot = WeatherBot()
        self.assertEqual(wbot.channel_names.get("weather"), "#wx")
        self.assertEqual(wbot.channel_names.get("alerts"), "#wx")
        self.assertIn("#wx", wbot.channels)

if __name__ == '__main__':
    unittest.main()
