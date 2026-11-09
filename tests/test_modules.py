"""Unit tests for Autoresponder, WeatherBot, and NetSessionEngine modules."""

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from openhop_meshhub_bot.hub import BotHub


class TestModules(unittest.IsolatedAsyncioTestCase):
    async def test_autoresponder_keywords_and_hops(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub = BotHub(data_dir=tmpdir)
            sent = []

            async def capture_send(target_type, target, text):
                sent.append((target_type, target, text))
                return {"ok": True}

            hub.tx_queue._send_fn = capture_send
            ar = hub.modules["autoresponce"]
            ar.apply_config(
                {
                    "enabled": True,
                    "channels": ["#test"],
                    "keywords": ["ping", "test"],
                    "maxHops": 4,
                    "responseTemplate": "@[{sender}] ACK ({hops_label})",
                }
            )

            # Matching keyword and hops <= 4
            await ar.on_message(
                {"sender": "Alice", "text": "ping check", "channel": "#test", "hops": 2}
            )
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][2], "@[Alice] ACK (2 hops)")

            # Exceeding maxHops -> ignored
            await ar.on_message(
                {"sender": "Bob", "text": "ping check", "channel": "#test", "hops": 6}
            )
            self.assertEqual(len(sent), 1)

    async def test_net_engine_checkin_and_resume_on_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hub = BotHub(data_dir=tmpdir)
            sent = []

            async def capture_send(target_type, target, text):
                sent.append((target_type, target, text))
                return {"ok": True}

            hub.tx_queue._send_fn = capture_send
            net = hub.modules["net_bot"]
            net.apply_config(
                {
                    "enabled": True,
                    "channel": "#net",
                    "day_of_week": "Tuesday",
                    "time": "19:00",
                    "duration": 2.0,
                    "keyword": "#checkin",
                }
            )

            await net.start_net_manual()
            self.assertTrue(net.is_active)

            # Check in two users
            await net.on_message(
                {"sender": "KI5ABC", "text": "Hello #checkin", "channel": "#net", "hops": 1, "snr": 9.0}
            )
            await net.on_message(
                {"sender": "W5XYZ", "text": "#checkin 73", "channel": "#net", "hops": 3, "snr": 4.5}
            )
            self.assertEqual(len(net.participants), 2)
            self.assertEqual(net.checkins, ["KI5ABC", "W5XYZ"])
            await net.stop()

            # Simulate restart while net is still within its 2-hour window
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
                            "areaDesc": "Travis County, TX",
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
