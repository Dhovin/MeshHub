"""Unit tests for RuntimeWriter (no config.json pollution) and BotHub idempotent command mailbox."""

import json
import os
import tempfile
import unittest

from openhop_meshhub_bot.hub import BotHub


class TestRuntimeWriterAndMailbox(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_writer_writes_only_runtime_json_not_config_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 2, "connection": {"tcpPort": 5051}}, fh)

            hub = BotHub(config_path=cfg_path, data_dir=tmpdir)
            ok = hub.runtime_writer.write_once(force_mirror=True)
            self.assertTrue(ok)

            # runtime.json must exist and have telemetry
            rt_path = os.path.join(tmpdir, "runtime.json")
            self.assertTrue(os.path.isfile(rt_path))
            with open(rt_path, "r", encoding="utf-8") as fh:
                rt_data = json.load(fh)
            self.assertIn("modules", rt_data)
            self.assertEqual(rt_data["version"], "2.0.2")

            # config.json must NEVER have _runtime written into it
            with open(cfg_path, "r", encoding="utf-8") as fh:
                disk_cfg = json.load(fh)
            self.assertNotIn("_runtime", disk_cfg)

    async def test_idempotent_command_mailbox_deduplicates_and_scrubs_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "schema_version": 2,
                        "_cmd": {
                            "id": "cmd-unique-42",
                            "type": "net_clear",
                            "params": {"module": "net_bot"},
                        },
                    },
                    fh,
                )

            hub1 = BotHub(config_path=cfg_path, data_dir=tmpdir)
            await hub1._check_disk_config_once()
            self.assertTrue(hub1.is_cmd_processed("cmd-unique-42"))
            self.assertEqual(len(hub1._cmd_results), 1)
            self.assertTrue(hub1._cmd_results[0]["ok"])

            # _cmd should be scrubbed from config.json after processing
            with open(cfg_path, "r", encoding="utf-8") as fh:
                clean_disk = json.load(fh)
            self.assertNotIn("_cmd", clean_disk)
            self.assertNotIn("_runtime", clean_disk)

            # Simulate service restart even if the same _cmd were re-sent
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "schema_version": 2,
                        "_cmd": {
                            "id": "cmd-unique-42",
                            "type": "net_clear",
                            "params": {"module": "net_bot"},
                        },
                    },
                    fh,
                )
            hub2 = BotHub(config_path=cfg_path, data_dir=tmpdir)
            self.assertTrue(hub2.is_cmd_processed("cmd-unique-42"))
            await hub2._check_disk_config_once()
            # Must still have only 1 execution record (no replay)
            self.assertEqual(len(hub2._cmd_results), 1)

    async def test_unknown_command_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            hub = BotHub(config_path=cfg_path, data_dir=tmpdir)
            res = await hub.execute_mailbox_command(
                {"id": "cmd-bad-1", "type": "arbitrary_shell_exec"}
            )
            self.assertFalse(res["ok"])
            self.assertIn("Unsupported command type", res["message"])


if __name__ == "__main__":
    unittest.main()
