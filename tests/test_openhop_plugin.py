import os
import json
import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock

from openhop_meshhub_bot.core.command_dispatcher import CommandDispatcher, clamp_lora_message, MAX_LORA_MSG_LEN
from openhop_meshhub_bot.core.bot import MeshHub

class TestOpenHopPlugin(unittest.TestCase):
    def test_lora_message_clamping(self):
        """Verify strict 135 character/byte clamping."""
        short_msg = "Hello mesh!"
        self.assertEqual(clamp_lora_message(short_msg), short_msg)

        exact_msg = "A" * 135
        self.assertEqual(clamp_lora_message(exact_msg), exact_msg)
        self.assertEqual(len(clamp_lora_message(exact_msg)), 135)

        long_msg = "B" * 200
        clamped = clamp_lora_message(long_msg)
        self.assertTrue(len(clamped.encode("utf-8")) <= 135)
        self.assertTrue(clamped.endswith("..."))
        self.assertEqual(len(clamped), 135)

        # Unicode/emoji clamping
        emoji_msg = "⚡" * 80  # Each⚡ is 3 bytes in UTF-8
        clamped_emoji = clamp_lora_message(emoji_msg)
        self.assertTrue(len(clamped_emoji.encode("utf-8")) <= 135)
        self.assertTrue(clamped_emoji.endswith("..."))

    def test_help_message_single_frame_limit(self):
        """Verify !help output is always a single message <= 135 characters."""
        mock_bot = MagicMock()
        mock_bot.module_manager.is_enabled.side_effect = lambda mod: True

        dispatcher = CommandDispatcher(mock_bot)
        help_all = dispatcher.build_help_message()
        self.assertTrue(len(help_all.encode("utf-8")) <= 135)
        self.assertTrue(len(help_all) <= 135)
        self.assertIn("MeshHub Bot:", help_all)
        self.assertIn("#checkin", help_all)
        self.assertIn("!checkin", help_all)

        # Partial modules enabled
        mock_bot.module_manager.is_enabled.side_effect = lambda mod: mod in ("net_bot", "autoresponce")
        help_partial = dispatcher.build_help_message()
        self.assertTrue(len(help_partial.encode("utf-8")) <= 135)
        self.assertIn("#checkin", help_partial)
        self.assertNotIn("!checkin", help_partial)

        # No modules enabled
        mock_bot.module_manager.is_enabled.side_effect = lambda mod: False
        help_none = dispatcher.build_help_message()
        self.assertTrue(len(help_none.encode("utf-8")) <= 135)

    def test_command_dispatcher_help_execution(self):
        """Verify handle_message triggers send_reply with !help."""
        async def run_test():
            mock_bot = MagicMock()
            mock_bot.module_manager.is_enabled.return_value = True
            mock_bot.can_send_user.return_value = True
            mock_bot.send_reply = AsyncMock()

            dispatcher = CommandDispatcher(mock_bot)
            handled = await dispatcher.handle_message(
                sender="Alice",
                text="!help",
                channel="#net",
                packet_meta={}
            )
            self.assertTrue(handled)
            mock_bot.send_reply.assert_called_once()
            args = mock_bot.send_reply.call_args[1]
            self.assertEqual(args["sender"], "Alice")
            self.assertEqual(args["channel"], "#net")
            self.assertTrue(len(args["text"]) <= 135)

        asyncio.run(run_test())

    def test_command_dispatcher_rate_limiting(self):
        """Verify rapid repeated !help calls from same sender are rate limited."""
        async def run_test():
            mock_bot = MagicMock()
            mock_bot.module_manager.is_enabled.return_value = True
            mock_bot.can_send_user.side_effect = [True, False]
            mock_bot.send_reply = AsyncMock()

            dispatcher = CommandDispatcher(mock_bot)
            # 1st call
            res1 = await dispatcher.handle_message("Bob", "!help", None)
            self.assertTrue(res1)
            self.assertEqual(mock_bot.send_reply.call_count, 1)

            # 2nd call (rate limited)
            res2 = await dispatcher.handle_message("Bob", "!help", None)
            self.assertTrue(res2)
            # send_reply should NOT be called again
            self.assertEqual(mock_bot.send_reply.call_count, 1)

        asyncio.run(run_test())

    def test_openhop_plugin_manifest_schema(self):
        """Verify openhop-plugin.json manifest complies with OpenHop Schema 1."""
        manifest_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "openhop_meshhub_bot", "share", "openhop", "plugins", "openhop.meshhub-bot", "openhop-plugin.json"
        )
        self.assertTrue(os.path.exists(manifest_path), f"Manifest missing at {manifest_path}")

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertEqual(manifest.get("schema"), 1)
        self.assertEqual(manifest.get("id"), "openhop.meshhub-bot")
        self.assertEqual(manifest.get("profile"), "python-service-v1")
        service = manifest.get("service", {})
        self.assertEqual(service.get("console_script"), "openhop-meshhub-bot")
        self.assertEqual(service.get("package_root"), "openhop_meshhub_bot")
        self.assertEqual(service.get("module"), "openhop_meshhub_bot.main")
        self.assertEqual(service.get("callable"), "main")
        ui = manifest.get("ui", {})
        self.assertEqual(ui.get("entry"), "ui/index.html")

    def test_data_dir_isolation(self):
        """Verify get_data_dir honors OPENHOP_PLUGIN_DATA."""
        hub = MeshHub()
        old_val = os.environ.get("OPENHOP_PLUGIN_DATA")
        try:
            custom_dir = os.path.abspath("test_openhop_data_dir")
            os.environ["OPENHOP_PLUGIN_DATA"] = custom_dir
            self.assertEqual(hub.get_data_dir(), custom_dir)
            if os.path.exists(custom_dir):
                os.rmdir(custom_dir)
        finally:
            if old_val is not None:
                os.environ["OPENHOP_PLUGIN_DATA"] = old_val
            else:
                os.environ.pop("OPENHOP_PLUGIN_DATA", None)

if __name__ == "__main__":
    unittest.main()
