"""Unit tests for rate limiters and template rendering."""

import unittest

from openhop_meshhub_bot.core.rate_limiter import (
    BotTxRateLimiter,
    ChannelRateLimiter,
    PerUserRateLimiter,
)
from openhop_meshhub_bot.mesh.text import extract_template_fields, format_template


class TestRateLimiterAndTemplates(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limiters(self):
        tx = BotTxRateLimiter(0.05)
        self.assertTrue(tx.can_tx())
        await tx.wait_for_tx()
        self.assertFalse(tx.can_tx())

        user_lim = PerUserRateLimiter(10.0)
        self.assertTrue(user_lim.can_send("Alice"))
        user_lim.record_send("Alice")
        self.assertFalse(user_lim.can_send("alice"))
        self.assertTrue(user_lim.can_send("Bob"))

        ch_lim = ChannelRateLimiter({"#test": 5.0})
        self.assertTrue(ch_lim.can_send("#test"))
        ch_lim.record_send("test")
        self.assertFalse(ch_lim.can_send("#test"))

    def test_template_extraction_and_rendering(self):
        msg = {
            "sender": "Dhovin",
            "channel": 2,
            "snr": 10.5,
            "rssi": -72,
            "path": "a1>b2>c3",
            "hops": 3,
        }
        fields = extract_template_fields(msg, timezone_str="UTC")
        rendered = format_template("@[{sender}] ACK | Path: {hops_label} ({snr})", fields)
        self.assertEqual(rendered, "@[Dhovin] ACK | Path: 3 hops (10.5dB)")


if __name__ == "__main__":
    unittest.main()
