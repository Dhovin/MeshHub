import os
import json
import tempfile
import unittest
import asyncio
from openhop_meshhub_bot.web.runtime_writer import RuntimeWriter

class DummyHub:
    def __init__(self, payload):
        self.payload = payload

    def get_runtime_payload(self):
        return self.payload

class TestRuntimeWriter(unittest.TestCase):
    def test_publish_and_dual_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = RuntimeWriter(temp_dir)
            test_payload = {
                "connected": True,
                "active_modules": ["weather", "net"],
                "help_message": "!help test",
                "help_length": 10
            }

            writer.write_runtime(test_payload)

            # Check primary runtime.json
            runtime_file = os.path.join(temp_dir, "runtime.json")
            self.assertTrue(os.path.exists(runtime_file))
            with open(runtime_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertTrue(data.get("connected"))
            self.assertEqual(data.get("active_modules"), ["weather", "net"])

            # Check config.json fallback
            config_file = os.path.join(temp_dir, "config.json")
            self.assertTrue(os.path.exists(config_file))
            with open(config_file, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
            self.assertIn("_runtime", cfg_data)
            self.assertEqual(cfg_data["_runtime"].get("connected"), True)

    def test_run_loop_stop(self):
        async def run_loop_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                writer = RuntimeWriter(temp_dir)
                hub = DummyHub({"connected": True, "active_modules": ["test"]})
                stop_event = asyncio.Event()

                task = asyncio.create_task(writer.run(hub, stop_event, interval=0.05))
                await asyncio.sleep(0.12)
                stop_event.set()
                await task

                runtime_file = os.path.join(temp_dir, "runtime.json")
                self.assertTrue(os.path.exists(runtime_file))

        asyncio.run(run_loop_test())

if __name__ == "__main__":
    unittest.main()
