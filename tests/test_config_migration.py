"""Unit tests for config schema v2 migration, validation, redaction, and hashing."""

import json
import os
import tempfile
import unittest

from openhop_meshhub_bot.config import (
    DEFAULT_CONFIG,
    SCHEMA_VERSION,
    canonical_config_hash,
    load_config_file,
    migrate_config,
    redact_config,
)


class TestConfigMigration(unittest.TestCase):
    def test_v1_weather_and_autoresponder_migration(self):
        v1_cfg = {
            "connection": {"host": "127.0.0.1", "tcpPort": 5051},
            "core": {"ipcPort": 5002, "perUserRateLimitSeconds": 30},
            "modules": {
                "autoresponce": {
                    "channels": ["#test"],
                    "enabled": True,
                    "maxHops": 12,
                    "responseTemplate": "@[{sender}] ACK | Path: {hops_label}",
                },
                "weather_bot": {
                    "channel": "#weather",
                    "cron": "0 7 * * *",
                    "enabled": True,
                    "latitude": 30.2672,
                    "longitude": -97.7431,
                    "nws_alerts": True,
                },
            },
        }
        migrated, was_v1 = migrate_config(v1_cfg)
        self.assertTrue(was_v1)
        self.assertEqual(migrated["schema_version"], SCHEMA_VERSION)

        wx = migrated["modules"]["weather_bot"]
        self.assertEqual(wx["weatherChannel"], "#weather")
        self.assertEqual(wx["alertsChannel"], "#weather")
        self.assertEqual(wx["weatherAlarm"], "0 7 * * *")
        self.assertEqual(wx["latitude"], 30.2672)
        self.assertEqual(wx["longitude"], -97.7431)
        # Since v1 specified custom lat/lon without zipCode, zipCode becomes "" so lat/lon win
        self.assertEqual(wx["zipCode"], "")

        ar = migrated["modules"]["autoresponce"]
        self.assertEqual(ar["keywords"], ["test"])
        self.assertIn("timezone", migrated["core"])

    def test_hhmm_weather_alarm_normalized_to_cron(self):
        cfg, _ = migrate_config({"modules": {"weather_bot": {"weatherAlarm": "06:30"}}})
        self.assertEqual(cfg["modules"]["weather_bot"]["weatherAlarm"], "30 6 * * *")

    def test_load_config_creates_v1_backup_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"connection": {"tcpPort": 5051}}, fh)

            clean, _ = load_config_file(cfg_path)
            self.assertEqual(clean["schema_version"], 2)
            self.assertTrue(os.path.isfile(f"{cfg_path}.v1.bak"))

    def test_redact_and_canonical_hash_ignore_runtime_and_cmd(self):
        base = dict(DEFAULT_CONFIG)
        with_volatile = dict(DEFAULT_CONFIG)
        with_volatile["_runtime"] = {"connected": True, "channel_secret": "deadbeef"}
        with_volatile["_cmd"] = {"id": "123", "type": "net_start"}

        self.assertEqual(canonical_config_hash(base), canonical_config_hash(with_volatile))
        redacted = redact_config(with_volatile)
        self.assertNotIn("_runtime", redacted)
        self.assertNotIn("_cmd", redacted)


if __name__ == "__main__":
    unittest.main()
