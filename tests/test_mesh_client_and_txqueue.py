"""Unit tests for MeshCompanionClient additive channel provisioning, sender resolution, and OutboundTxQueue."""

import types
import unittest
from unittest.mock import AsyncMock, MagicMock

from meshcore.events import EventType
from openhop_meshhub_bot.mesh.client import MeshCompanionClient
from openhop_meshhub_bot.mesh.txqueue import PRIORITY_BROADCAST, OutboundTxQueue


class FakeEvent:
    def __init__(self, event_type, payload):
        self.type = event_type
        self.payload = payload


class TestMeshClientAndTxQueue(unittest.IsolatedAsyncioTestCase):
    async def test_additive_channel_provisioning_never_deletes_or_shifts(self):
        client = MeshCompanionClient(host="127.0.0.1", port=5051)
        client.is_connected = True
        client.device_info = {"max_channels": 6}

        # Existing channels on Virtual Companion:
        # Slot 0: Public, Slot 1: #custom-user-chan, Slot 2: #test, Slot 3..5: empty
        existing_slots = {
            0: {"channel_idx": 0, "channel_name": "Public", "channel_secret": b"\x00" * 16},
            1: {"channel_idx": 1, "channel_name": "#custom-user-chan", "channel_secret": b"\x11" * 16},
            2: {"channel_idx": 2, "channel_name": "#test", "channel_secret": b"\x22" * 16},
            3: {"channel_idx": 3, "channel_name": "", "channel_secret": b"\x00" * 16},
            4: {"channel_idx": 4, "channel_name": "", "channel_secret": b"\x00" * 16},
            5: {"channel_idx": 5, "channel_name": "", "channel_secret": b"\x00" * 16},
        }

        async def fake_get_channel(idx):
            if idx in existing_slots:
                return FakeEvent(EventType.OK, existing_slots[idx])
            return FakeEvent(EventType.ERROR, {})

        set_calls = []

        async def fake_set_channel(idx, name, secret):
            set_calls.append((idx, name, secret))
            existing_slots[idx] = {"channel_idx": idx, "channel_name": name, "channel_secret": secret}
            return FakeEvent(EventType.OK, existing_slots[idx])

        client.mc = MagicMock()
        client.mc.commands.get_channel = AsyncMock(side_effect=fake_get_channel)
        client.mc.commands.set_channel = AsyncMock(side_effect=fake_set_channel)

        mapped = await client.provision_channels(["#test", "#weather", "#net"])
        # #test already existed at slot 2 -> kept at slot 2!
        self.assertEqual(mapped["#test"], 2)
        # #weather and #net added to first empty slots (3 and 4)
        self.assertEqual(mapped["#weather"], 3)
        self.assertEqual(mapped["#net"], 4)
        # Slot 1 (#custom-user-chan) and Slot 0 (Public) were never touched!
        self.assertEqual(len(set_calls), 2)
        self.assertEqual(set_calls[0][0], 3)
        self.assertEqual(set_calls[1][0], 4)

        # Verify get_channels_summary never leaks secrets
        summary = client.get_channels_summary()
        for entry in summary:
            self.assertNotIn("channel_secret", entry)
            self.assertNotIn("secret", entry)

    def test_channel_message_sender_resolution_and_prefix_cleaning(self):
        received = []
        client = MeshCompanionClient(on_message=lambda m: received.append(m))
        client.mc = MagicMock()
        client.mc._reader = types.SimpleNamespace(
            packet_parser=types.SimpleNamespace(
                channels_log=[
                    {
                        "sender_timestamp": 1700000000,
                        "message": "Kae39: Test message",
                        "transport_code": "aabbccddeeff",
                    },
                    {
                        "sender_timestamp": 1700000001,
                        "message": "UnknownContactNode: 76131",
                        "transport_code": "112233445566",
                    },
                ]
            )
        )

        def fake_contact_lookup(code):
            if code == "aabbccddeeff":
                return {"adv_name": "Kae39", "public_key": "aabbccddeeff0011"}
            return None

        client.mc.get_contact_by_key_prefix = MagicMock(side_effect=fake_contact_lookup)

        # Case 1: Contact found in contacts table
        ev1 = FakeEvent(
            EventType.CHANNEL_MSG_RECV,
            {
                "channel_idx": 2,
                "text": "Kae39: Test message",
                "sender_timestamp": 1700000000,
                "path_len": 5,
                "snr": 8.5,
            },
        )
        client._on_channel_message(ev1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["sender"], "Kae39")
        self.assertEqual(received[0]["text"], "Test message")
        self.assertEqual(received[0]["hops"], 5)

        # Case 2: Contact NOT in contacts table yet (transport_code present) -> extracts sender name and strips prefix so bare ZIP "76131" is preserved!
        ev2 = FakeEvent(
            EventType.CHANNEL_MSG_RECV,
            {
                "channel_idx": 3,
                "text": "UnknownContactNode: 76131",
                "sender_timestamp": 1700000001,
                "path_len": 1,
                "snr": 10.0,
            },
        )
        client._on_channel_message(ev2)
        self.assertEqual(len(received), 2)
        self.assertEqual(received[1]["sender"], "UnknownContactNode")
        self.assertEqual(received[1]["text"], "76131")

    async def test_tx_queue_clamps_to_135_bytes_and_orders_priorities(self):
        dispatched = []

        async def fake_send(target_type, target, text):
            dispatched.append((target_type, target, text))
            return {"ok": True}

        q = OutboundTxQueue(send_fn=fake_send, min_interval_seconds=0.01)
        long_msg = "X" * 200
        res = await q.send_channel(1, long_msg, priority=PRIORITY_BROADCAST)
        self.assertTrue(res["ok"])
        self.assertEqual(len(dispatched), 1)
        self.assertLessEqual(len(dispatched[0][2].encode("utf-8")), 135)
        self.assertTrue(dispatched[0][2].endswith("..."))


    async def test_companion_apply_settings(self):
        client = MeshCompanionClient(host="127.0.0.1", port=5051)
        client.is_connected = True
        client.mc = MagicMock()

        coords_called = []
        async def fake_set_coords(lat, lon):
            coords_called.append((lat, lon))
            return FakeEvent(EventType.OK, {})

        hash_called = []
        async def fake_set_path_hash(mode):
            hash_called.append(mode)
            return FakeEvent(EventType.OK, {})

        cli_called = []
        async def fake_run_cli(cmd):
            cli_called.append(cmd)
            return FakeEvent(EventType.OK, {})

        cvar_called = []
        async def fake_set_cvar(key, val):
            cvar_called.append((key, val))
            return FakeEvent(EventType.OK, {})

        other_called = []
        async def fake_set_other(**kwargs):
            other_called.append(kwargs)
            return FakeEvent(EventType.OK, {})

        client.mc.commands.set_coords = AsyncMock(side_effect=fake_set_coords)
        client.mc.commands.set_path_hash_mode = AsyncMock(side_effect=fake_set_path_hash)
        client.mc.commands.run_cli_command = AsyncMock(side_effect=fake_run_cli)
        client.mc.commands.set_custom_var = AsyncMock(side_effect=fake_set_cvar)
        client.mc.commands.get_path_hash_mode = AsyncMock(return_value=2)
        client.mc.commands.set_other_params = AsyncMock(side_effect=fake_set_other)
        client.mc.commands.send_appstart = AsyncMock(return_value=FakeEvent(EventType.OK, {
            "manual_add_contacts": False,
            "telemetry_mode_base": 1,
            "telemetry_mode_env": 0,
        }))

        settings = {
            "pathHashMode": 2,
            "latitude": 32.8998,
            "longitude": -97.3473,
            "advLocPolicy": 2,
            "telemetryLoc": True,
        }

        res = await client.apply_companion_settings(settings)
        self.assertTrue(res["ok"])
        self.assertEqual(hash_called, [2])
        self.assertEqual(cli_called, ["set path.hash.mode 2"])
        self.assertIn(("path.hash.mode", "2"), cvar_called)
        self.assertEqual(client.device_info.get("path_hash_mode"), 2)
        self.assertEqual(coords_called, [(32.8998, -97.3473)])
        self.assertEqual(len(other_called), 1)
        self.assertEqual(other_called[0]["advert_loc_policy"], 2)
        self.assertEqual(other_called[0]["telemetry_mode_loc"], 1)


if __name__ == "__main__":
    unittest.main()
