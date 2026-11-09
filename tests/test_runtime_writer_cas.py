"""Unit tests for RuntimeWriter CAS race protection and BotHub idempotent command mailbox."""

import json
import os
import tempfile
import unittest

from openhop_meshhub_bot.hub import BotHub


class TestRuntimeWriterAndMailbox(unittest.IsolatedAsyncioTestCase):
    async def test_cas_mirror_skips_when_unprocessed_cmd_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 2, "connection": {"tcpPort": 5051}}, fh)

            hub = BotHub(config_path=cfg_path, data_dir=tmpdir)
            # Simulate openHop settings API writing a pending _cmd into config.json
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "schema_version": 2,
                        "connection": {"tcpPort": 5051},
                        "_cmd": {"id": "cmd-pending-1", "type": "net_clear"},
                    },
                    fh,
                )

            # RuntimeWriter must NOT overwrite config.json while cmd-pending-1 is unprocessed
            mirrored = hub.runtime_writer._cas_mirror_to_config(hub.get_runtime_payload())
            self.assertFalse(mirrored)

            with open(cfg_path, "r", encoding="utf-8") as fh:
                disk_cfg = json.load(fh)
            self.assertEqual(disk_cfg["_cmd"]["id"], "cmd-pending-1")

    async def test_idempotent_command_mailbox_deduplicates_across_restart(self):
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

            # Simulate service restart with the same _cmd still in config.json
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
