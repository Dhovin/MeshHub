import unittest
import asyncio
import time
from core.rate_limiter import BotTxRateLimiter, PerUserRateLimiter, ChannelRateLimiter

class TestBotTxRateLimiter(unittest.TestCase):
    def test_can_tx_and_record(self):
        limiter = BotTxRateLimiter(seconds=0.1)
        self.assertTrue(limiter.can_tx())
        limiter.record_tx()
        self.assertFalse(limiter.can_tx())
        self.assertGreater(limiter.time_until_next_tx(), 0.0)
        time.sleep(0.12)
        self.assertTrue(limiter.can_tx())
        self.assertEqual(limiter.time_until_next_tx(), 0.0)

    def test_wait_for_tx(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            limiter = BotTxRateLimiter(seconds=0.05)
            t0 = time.monotonic()
            loop.run_until_complete(limiter.wait_for_tx())
            loop.run_until_complete(limiter.wait_for_tx())
            elapsed = time.monotonic() - t0
            self.assertGreaterEqual(elapsed, 0.04)
            stats = limiter.get_stats()
            self.assertEqual(stats["total_tx"], 2)
            self.assertEqual(stats["total_throttled"], 1)
        finally:
            loop.close()


class TestPerUserRateLimiter(unittest.TestCase):
    def test_user_rate_limiting(self):
        limiter = PerUserRateLimiter(seconds=0.1, max_entries=5)
        user = "Alice"
        self.assertTrue(limiter.can_send(user))
        limiter.record_send(user)
        self.assertFalse(limiter.can_send(user))
        self.assertFalse(limiter.can_send(" alice ")) # case-insensitive and stripped
        self.assertTrue(limiter.can_send("Bob")) # different user allowed

        time.sleep(0.12)
        self.assertTrue(limiter.can_send(user))

    def test_lru_eviction(self):
        limiter = PerUserRateLimiter(seconds=10.0, max_entries=2)
        limiter.record_send("user1")
        limiter.record_send("user2")
        self.assertFalse(limiter.can_send("user1"))
        self.assertFalse(limiter.can_send("user2"))

        # Adding 3rd user evicts oldest ("user1")
        limiter.record_send("user3")
        self.assertTrue(limiter.can_send("user1"))
        self.assertFalse(limiter.can_send("user2"))
        self.assertFalse(limiter.can_send("user3"))


class TestChannelRateLimiter(unittest.TestCase):
    def test_channel_limits(self):
        limiter = ChannelRateLimiter(
            channel_limits={"general": 0.1, "emergency": 0.0},
            default_seconds=0.05
        )
        self.assertTrue(limiter.can_send("#general"))
        limiter.record_send("#general")
        self.assertFalse(limiter.can_send("general"))

        # Emergency channel has 0 cooldown
        self.assertTrue(limiter.can_send("emergency"))
        limiter.record_send("emergency")
        self.assertTrue(limiter.can_send("emergency"))

        time.sleep(0.12)
        self.assertTrue(limiter.can_send("general"))

if __name__ == "__main__":
    unittest.main()
