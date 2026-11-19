"""Unit tests for Autoresponder, WeatherBot, and NetSessionEngine modules."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from openhop_meshhub_bot.hub import BotHub


class TestModules(unittest.IsolatedAsyncioTestCase):
    async def test_autoresponder_case_insensitive_keywords_and_hops(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub = BotHub(data_dir=tmpdir)
            sent = []

            async def capture_send(target_type, target, text):
                sent.append((target_type, target, text))
                return {"ok": True}

            hub.tx_queue._send_fn = capture_send
            hub.tx_queue.min_interval_seconds = 0.001
            ar = hub.modules["autoresponce"]
            ar.apply_config(
                {
                    "enabled": True,
                    "channels": ["#test"],
                    "keywords": ["PiNg", "TeSt"],
                    "exactMatch": True,
                    "maxHops": 4,
                    "responseTemplate": "@[{sender}] ACK ({hops_label})",
                }
            )

            # Standalone uppercase keyword match with punctuation and hops <= 4
            await ar.on_message(
                {"sender": "Alice", "text": "PING!", "channel": "#test", "hops": 2}
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][2], "@[Alice] ACK (2 hops)")

            # Mention prefix with standalone keyword
            await ar.on_message(
                {"sender": "Charlie", "text": "@[MeshHub] tEsT", "channel": "#test", "hops": 0}
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[1][2], "@[Charlie] ACK (Direct (0 hops))")

            # Conversational sentence with keyword embedded -> ignored when exactMatch is True
            await ar.on_message(
                {"sender": "Dave", "text": "Can you test this link?", "channel": "#test", "hops": 1}
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(sent), 2)

            # Conversational extra words -> ignored
            await ar.on_message(
                {"sender": "Eve", "text": "ping check", "channel": "#test", "hops": 1}
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(sent), 2)

            # Exceeding maxHops -> ignored
            await ar.on_message(
                {"sender": "Bob", "text": "ping", "channel": "#test", "hops": 6}
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(sent), 2)

            # When exactMatch is False, substring matches work
            ar.apply_config(
                {"enabled": True, "channels": ["#test"], "exactMatch": False, "keywords": ["ping"]}
            )
            await ar.on_message(
                {"sender": "Frank", "text": "ping check", "channel": "#test", "hops": 1}
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(sent), 3)

    async def test_autoresponder_secret_marco_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub = BotHub(data_dir=tmpdir)
            sent = []

            async def capture_send(target_type, target, text):
                sent.append((target_type, target, text))
                return {"ok": True}

            hub.tx_queue._send_fn = capture_send
            hub.tx_queue.min_interval_seconds = 0.001
            ar = hub.modules["autoresponce"]
            ar.apply_config(
                {
                    "enabled": True,
                    "channels": ["#test"],
                    "keywords": ["test", "ping"],
                    "exactMatch": True,
                    "maxHops": 4,
                    "responseTemplate": "@[{sender}] ACK ({hops_label})",
                }
            )

            # Exact secret !marco triggers on #test within maxHops (distinct senders)
            await ar.on_message(
                {"sender": "Alice", "text": "!marco", "channel": "#test", "hops": 1}
            )
            await ar.on_message(
                {"sender": "Bob", "text": "!MARCO!", "channel": "#test", "hops": 0}
            )
            await ar.on_message(
                {"sender": "Charlie", "text": "@[MeshHub] !marco", "channel": "#test", "hops": 2}
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(sent), 3)
            self.assertEqual([item[2] for item in sent], ["Polo", "Polo", "Polo"])
            self.assertEqual(hub._activity[0]["event"], "marco_polo")

            # Missing leading '!' -> ignored
            await ar.on_message(
                {"sender": "Dave", "text": "marco", "channel": "#test", "hops": 1}
            )
            # Extra words around !marco -> ignored
            await ar.on_message(
                {"sender": "Eve", "text": "hey !marco there", "channel": "#test", "hops": 1}
            )
            # Unmonitored channel -> ignored
            await ar.on_message(
                {"sender": "Frank", "text": "!marco", "channel": "#general", "hops": 1}
            )
            # Exceeding maxHops -> ignored
            await ar.on_message(
                {"sender": "Grace", "text": "!marco", "channel": "#test", "hops": 5}
            )
            # Per-user rate limit suppresses immediate second !marco from Alice
            await ar.on_message(
                {"sender": "Alice", "text": "!marco", "channel": "#test", "hops": 1}
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(sent), 3)

    async def test_weather_bot_bare_zipcode_in_channel_and_default_location(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub = BotHub(data_dir=tmpdir)
            sent = []

            async def capture_send(target_type, target, text):
                sent.append((target_type, target, text))
                return {"ok": True}

            hub.tx_queue._send_fn = capture_send
            wx = hub.modules["weather_bot"]
            self.assertEqual(wx.zip_code, "76131")
            self.assertAlmostEqual(wx.latitude, 32.8998, places=4)
            self.assertAlmostEqual(wx.longitude, -97.3473, places=4)

            with patch.object(
                wx,
                "fetch_forecast_text",
                new=AsyncMock(return_value="Wx 76131:\nToday: ☀️ H:82 L:61"),
            ):
                # 1. Bare 5-digit ZIP code in #weather with no other text
                await wx.on_message(
                    {"sender": "Alice", "text": "76131", "channel": "#weather", "hops": 1}
                )
                await asyncio.sleep(0.05)
                self.assertEqual(len(sent), 1)
                self.assertIn("Wx 76131", sent[0][2])

                # 2. Bare 5-digit ZIP code preceded by MeshCore sender prefix
                await wx.on_message(
                    {
                        "sender": "Unknown-1a2b3c",
                        "text": "Kae39 📡: 76131",
                        "channel": "#weather",
                        "hops": 1,
                    }
                )
                await asyncio.sleep(0.05)
                self.assertEqual(len(sent), 2)
                self.assertIn("Wx 76131", sent[1][2])

    async def test_net_bot_consolidated_meshwars_behavior_on_net_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub = BotHub(data_dir=tmpdir)
            self.assertNotIn("meshwars_bot", hub.modules)
            self.assertIn("net_bot", hub.modules)
            sent = []

            async def capture_send(target_type, target, text):
                sent.append((target_type, target, text))
                return {"ok": True}

            hub.tx_queue._send_fn = capture_send
            net = hub.modules["net_bot"]
            self.assertEqual(net.channel, "#net")
            self.assertEqual(net.day_of_week, "Saturday")
            self.assertEqual(net.time, "18:00")
            self.assertEqual(net.duration, 6.0)
            self.assertEqual(net.keyword, "!checkin")

            await net.start_net_manual()
            self.assertTrue(net.is_active)

            # 1. Explicit !CHECKIN (case-insensitive) gets recorded + ACKed
            await net.on_message(
                {
                    "sender": "KI5ABC",
                    "text": "Hello !CHECKIN",
                    "channel": "#net",
                    "hops": 1,
                    "snr": 9.0,
                }
            )
            # 2. Regular chat message on #net also counts as participation (from meshwars_bot)
            await net.on_message(
                {
                    "sender": "W5XYZ",
                    "text": "Good evening everyone on the net!",
                    "channel": "#net",
                    "hops": 3,
                    "snr": 4.5,
                }
            )
            self.assertEqual(len(net.participants), 2)
            self.assertEqual(net.checkins, ["KI5ABC", "W5XYZ"])
            await net.stop()

            # Simulate restart while net is still within its 6-hour window
            hub2 = BotHub(data_dir=tmpdir)
            net2 = hub2.modules["net_bot"]
            await net2.start()
            self.assertTrue(net2.is_active)
            self.assertEqual(net2.checkins, ["KI5ABC", "W5XYZ"])
            await net2.stop()

    async def test_weather_seen_alerts_persisted_across_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub = BotHub(data_dir=tmpdir)
            sent = []

            async def capture_send(target_type, target, text):
                sent.append((target_type, target, text))
                return {"ok": True}

            hub.tx_queue._send_fn = capture_send
            wx = hub.modules["weather_bot"]

            future_iso = (datetime.now(ZoneInfo("UTC")) + timedelta(hours=3)).isoformat()
            fake_nws_response = {
                "features": [
                    {
                        "id": "urn:oid:2.49.0.1.840.0.test-alert-1",
                        "properties": {
                            "identifier": "alert-1",
                            "event": "Tornado Warning",
                            "severity": "Extreme",
                            "certainty": "Observed",
                            "areaDesc": "Tarrant County, TX",
                            "onset": datetime.now(ZoneInfo("UTC")).isoformat(),
                            "expires": future_iso,
                            "headline": "Take shelter now",
                        },
                    }
                ]
            }

            with patch(
                "openhop_meshhub_bot.modules.weather.fetch_nws_json",
                new=AsyncMock(return_value=fake_nws_response),
            ):
                res1 = await wx.check_alerts_now()
                self.assertEqual(res1["new_broadcasted"], 1)
                self.assertEqual(len(sent), 1)

                # Simulate service restart -> alert-1 must NOT be re-broadcasted!
                hub2 = BotHub(data_dir=tmpdir)
                hub2.tx_queue._send_fn = capture_send
                wx2 = hub2.modules["weather_bot"]
                res2 = await wx2.check_alerts_now()
                self.assertEqual(res2["new_broadcasted"], 0)
                self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
