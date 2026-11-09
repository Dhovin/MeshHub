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
    def test_defaults_fort_worth_and_consolidated_net_bot(self):
        self.assertNotIn("meshwars_bot", DEFAULT_CONFIG["modules"])
        wx = DEFAULT_CONFIG["modules"]["weather_bot"]
        self.assertEqual(wx["zipCode"], "76131")
        self.assertAlmostEqual(wx["latitude"], 32.8998, places=4)
        self.assertAlmostEqual(wx["longitude"], -97.3473, places=4)

        net = DEFAULT_CONFIG["modules"]["net_bot"]
        self.assertTrue(net["enabled"])
        self.assertEqual(net["channel"], "#net")
        self.assertEqual(net["day_of_week"], "Saturday")
        self.assertEqual(net["time"], "18:00")
        self.assertEqual(net["duration"], 6.0)
        self.assertEqual(net["checkin_keyword"], "!checkin")
        self.assertIn("MeshWars", net["opening_message"])

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
        migrated, changed = migrate_config(v1_cfg)
        self.assertTrue(changed)
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
        self.assertNotIn("meshwars_bot", migrated["modules"])

    def test_meshwars_bot_migrates_into_net_bot_on_net_channel(self):
        legacy_v2 = {
            "schema_version": 2,
            "modules": {
                "weather_bot": {
                    "zipCode": "20001",
                    "latitude": 38.9072,
                    "longitude": -77.0369,
                },
                "net_bot": {
                    "enabled": False,
                    "channel": "#net",
                    "day_of_week": "Tuesday",
                    "time": "19:00",
                    "duration": 2.0,
                },
                "meshwars_bot": {
                    "enabled": True,
                    "channel": "#meshwars",
                    "day_of_week": "Saturday",
                    "time": "18:00",
                    "duration": 6.0,
                    "checkin_keyword": "!checkin",
                },
            },
        }
        migrated, changed = migrate_config(legacy_v2)
        self.assertTrue(changed)
        self.assertNotIn("meshwars_bot", migrated["modules"])
        net = migrated["modules"]["net_bot"]
        self.assertTrue(net["enabled"])
        self.assertEqual(net["channel"], "#net")
        self.assertEqual(net["day_of_week"], "Saturday")
        self.assertEqual(net["time"], "18:00")
        self.assertEqual(net["duration"], 6.0)
        self.assertEqual(migrated["modules"]["weather_bot"]["zipCode"], "76131")

    def test_hhmm_weather_alarm_normalized_to_cron(self):
        cfg, _ = migrate_config({"modules": {"weather_bot": {"weatherAlarm": "06:30"}}})
        self.assertEqual(cfg["modules"]["weather_bot"]["weatherAlarm"], "30 6 * * *")

    def test_load_config_strips_runtime_from_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "schema_version": 2,
                        "connection": {"tcpPort": 5051},
                        "_runtime": {"connected": True},
                    },
                    fh,
                )

            clean, _ = load_config_file(cfg_path)
            self.assertNotIn("_runtime", clean)
            with open(cfg_path, "r", encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertNotIn("_runtime", on_disk)

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
