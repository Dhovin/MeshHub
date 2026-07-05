import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import time
import json
from core.connection_manager import ConnectionManager
from meshcore.events import EventType

class TestConnectionManagerCommands(unittest.TestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.config = {
            "core": {
                "latitude": 32.7767,
                "longitude": -96.7970
            }
        }
        self.bot.config_path = "config/config.json"
        
        self.cm = ConnectionManager(self.bot)
        self.cm.isConnected = True
        self.cm.mc = MagicMock()
        self.cm.mc.self_info = {"name": "TestNode", "tx_power": 12}
        self.cm.mc.channels = [{"channel_idx": 0, "channel_name": "primary", "channel_secret": b"\0"*16}]
        
        self.cm.save_config = MagicMock()
        self.cm._save_contacts = MagicMock()
        self.cm._load_contacts = MagicMock()
        
    def mock_event(self, event_type, payload=None):
        evt = MagicMock()
        evt.type = event_type
        evt.payload = payload or {}
        return evt

    def test_query_and_ver_commands(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.send_appstart = AsyncMock(return_value=self.mock_event(EventType.OK))
            
            res = loop.run_until_complete(self.cm.execute("query"))
            self.assertEqual(res, self.cm.mc.self_info)
            self.cm.mc.commands.send_appstart.assert_called_once()
        finally:
            loop.close()

    def test_self_telemetry(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            telemetry_data = {"battery": 95}
            self.cm.mc.commands.get_self_telemetry = AsyncMock(return_value=self.mock_event(EventType.OK, telemetry_data))
            
            res = loop.run_until_complete(self.cm.execute("t"))
            self.assertEqual(res, telemetry_data)
            self.cm.mc.commands.get_self_telemetry.assert_called_once()
        finally:
            loop.close()

    def test_clock_commands(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            time_data = {"time": 123456789}
            self.cm.mc.commands.get_time = AsyncMock(return_value=self.mock_event(EventType.OK, time_data))
            self.cm.mc.commands.set_time = AsyncMock(return_value=self.mock_event(EventType.OK))
            
            # get time
            res = loop.run_until_complete(self.cm.execute("clock"))
            self.assertEqual(res, time_data)
            
            # sync time
            res = loop.run_until_complete(self.cm.execute("sync_time"))
            self.assertEqual(res, {"ok": "time synced"})
            
            # set time
            res = loop.run_until_complete(self.cm.execute("time 123456790"))
            self.assertEqual(res, {"ok": "time set"})
            self.cm.mc.commands.set_time.assert_any_call(123456790)
        finally:
            loop.close()

    def test_reboot(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.reboot = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            
            res = loop.run_until_complete(self.cm.execute("reboot"))
            self.assertEqual(res, {"ok": True})
            self.cm.mc.commands.reboot.assert_called_once()
        finally:
            loop.close()

    def test_sleep_and_wait_key(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            res = loop.run_until_complete(self.cm.execute("sleep 0"))
            self.assertEqual(res, {"ok": "slept for 0 seconds"})
            
            res = loop.run_until_complete(self.cm.execute("wait_key"))
            self.assertEqual(res, {"info": "wait_key ignored in non-interactive mode"})
        finally:
            loop.close()

    def test_message_sending(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.send_msg = AsyncMock(return_value=self.mock_event(EventType.MSG_SENT, {"expected_ack": b"\x01\x02"}))
            self.cm._get_contact = AsyncMock(return_value={"public_key": "aabbcc"})
            
            res = loop.run_until_complete(self.cm.execute("msg aabbcc hello"))
            self.assertEqual(res["expected_ack"], "0102")
        finally:
            loop.close()

    def test_wait_ack(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.wait_for_event = AsyncMock(return_value=self.mock_event(EventType.ACK, {"ack": True}))
            
            res = loop.run_until_complete(self.cm.execute("wait_ack"))
            self.assertEqual(res, {"ack": True})
            self.cm.mc.wait_for_event.assert_called_with(EventType.ACK, timeout=5)
        finally:
            loop.close()

    def test_channel_sending(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.send_chan_msg = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            
            res = loop.run_until_complete(self.cm.execute("chan 0 hello"))
            self.assertEqual(res, {"ok": True})
            self.cm.mc.commands.send_chan_msg.assert_called_with(0, "hello")
            
            res = loop.run_until_complete(self.cm.execute("public world"))
            self.assertEqual(res, {"ok": True})
            self.cm.mc.commands.send_chan_msg.assert_called_with(0, "world")
        finally:
            loop.close()

    def test_recv_sync_and_wait_msg(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.get_msg = AsyncMock(side_effect=[
                self.mock_event(EventType.CONTACT_MSG_RECV, {"text": "hello"}),
                self.mock_event(EventType.NO_MORE_MSGS)
            ])
            self.cm.mc.wait_for_event = AsyncMock(return_value=True)
            
            # recv
            res = loop.run_until_complete(self.cm.execute("recv"))
            self.assertEqual(res, {"text": "hello"})
            
            # sync_msgs
            self.cm.mc.commands.get_msg.reset_mock()
            self.cm.mc.commands.get_msg.side_effect = [
                self.mock_event(EventType.CONTACT_MSG_RECV, {"text": "msg1"}),
                self.mock_event(EventType.NO_MORE_MSGS)
            ]
            res = loop.run_until_complete(self.cm.execute("sync_msgs"))
            self.assertEqual(res, [{"text": "msg1"}])
            
            # wait_msg
            self.cm.mc.commands.get_msg.reset_mock()
            self.cm.mc.commands.get_msg.side_effect = [
                self.mock_event(EventType.CONTACT_MSG_RECV, {"text": "msg2"})
            ]
            res = loop.run_until_complete(self.cm.execute("wait_msg"))
            self.assertEqual(res, {"text": "msg2"})
        finally:
            loop.close()

    def test_channel_management(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.get_channel = AsyncMock(side_effect=[
                self.mock_event(EventType.CHANNEL_INFO, {"channel_idx": 0, "channel_name": "primary", "channel_secret": b"\0"*16}),
                self.mock_event(EventType.ERROR)
            ])
            self.cm.mc.commands.set_channel = AsyncMock(return_value=self.mock_event(EventType.OK))
            
            # channels
            res = loop.run_until_complete(self.cm.execute("channels"))
            self.assertEqual(res, [{"channel_idx": 0, "channel_name": "primary", "channel_secret": 16 * "00"}])
            
            # get_channel
            self.cm.mc.commands.get_channel.reset_mock()
            self.cm.mc.commands.get_channel.side_effect = [
                self.mock_event(EventType.CHANNEL_INFO, {"channel_idx": 0, "channel_name": "primary", "channel_secret": b"\0"*16})
            ]
            res = loop.run_until_complete(self.cm.execute("get_channel 0"))
            self.assertEqual(res["channel_name"], "primary")
            
            # set_channel
            self.cm.mc.commands.get_channel.reset_mock()
            self.cm.mc.commands.get_channel.side_effect = [
                self.mock_event(EventType.CHANNEL_INFO, {"channel_idx": 1, "channel_name": "testchan", "channel_secret": b"\x01"*16})
            ]
            res = loop.run_until_complete(self.cm.execute("set_channel 1 testchan"))
            self.assertEqual(res["channel_name"], "testchan")
            self.assertEqual(res["channel_secret"], 16 * "01")
            
            # remove_channel
            self.cm.mc.commands.get_channel.reset_mock()
            self.cm.mc.commands.get_channel.side_effect = [
                self.mock_event(EventType.CHANNEL_INFO, {"channel_idx": 1, "channel_name": "", "channel_secret": b"\0"*16})
            ]
            res = loop.run_until_complete(self.cm.execute("remove_channel 1"))
            self.assertEqual(res, {"ok": "channel 1 removed"})
        finally:
            loop.close()

    def test_scope_and_advert(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.set_flood_scope = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            self.cm.mc.commands.send_advert = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            
            # scope
            res = loop.run_until_complete(self.cm.execute("scope #myregion"))
            self.assertEqual(res, {"ok": True})
            self.cm.mc.commands.set_flood_scope.assert_called_with("#myregion")
            
            # advert
            res = loop.run_until_complete(self.cm.execute("advert"))
            self.assertEqual(res, {"ok": True})
            
            # floodadv
            res = loop.run_until_complete(self.cm.execute("floodadv"))
            self.assertEqual(res, {"ok": True})
            self.cm.mc.commands.send_advert.assert_any_call(flood=True)
        finally:
            loop.close()

    def test_get_command(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.send_appstart = AsyncMock(return_value=self.mock_event(EventType.OK))
            self.cm.mc.commands.send_device_query = AsyncMock(return_value=self.mock_event(EventType.OK, {"repeat": True, "path_hash_mode": 1}))
            self.cm.mc.commands.get_bat = AsyncMock(return_value=self.mock_event(EventType.OK, {"voltage": 3800, "percent": 90}))
            self.cm.mc.commands.export_private_key = AsyncMock(return_value=self.mock_event(EventType.OK, {"private_key": b"\x03"*32}))
            
            self.cm.mc.self_info = {
                "name": "NodeName",
                "tx_power": 12,
                "adv_lat": 32.7767,
                "adv_lon": -96.7970,
                "radio_freq": 915.0,
                "radio_bw": 125.0,
                "radio_sf": 7,
                "radio_cr": 1,
                "multi_acks": True,
                "manual_add_contacts": False,
                "telemetry_mode_base": 1,
                "telemetry_mode_loc": 2,
                "telemetry_mode_env": 3,
                "adv_loc_policy": 4
            }
            
            res = loop.run_until_complete(self.cm.execute("get name"))
            self.assertEqual(res["name"], "NodeName")
            
            res = loop.run_until_complete(self.cm.execute("get coords"))
            self.assertEqual(res, {"lat": 32.7767, "lon": -96.7970})
            
            res = loop.run_until_complete(self.cm.execute("get radio"))
            self.assertEqual(res["radio_freq"], 915.0)
            self.assertEqual(res["repeat"], True)
            
            res = loop.run_until_complete(self.cm.execute("get bat"))
            self.assertEqual(res["percent"], 90)
            
            res = loop.run_until_complete(self.cm.execute("get private_key"))
            self.assertEqual(res["private_key"], 32 * "03")
        finally:
            loop.close()

    def test_set_command(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.set_name = AsyncMock(return_value=self.mock_event(EventType.OK, {"name": "NewName"}))
            self.cm.mc.commands.set_tx_power = AsyncMock(return_value=self.mock_event(EventType.OK, {"tx_power": 10}))
            self.cm.mc.commands.set_devicepin = AsyncMock(return_value=self.mock_event(EventType.OK, {"pin": 1234}))
            self.cm.mc.commands.set_radio = AsyncMock(return_value=self.mock_event(EventType.OK, {"radio": True}))
            self.cm.mc.commands.set_path_hash_mode = AsyncMock(return_value=self.mock_event(EventType.OK, {"path_hash_mode": 2}))
            self.cm.mc.commands.set_autoadd_config = AsyncMock(return_value=self.mock_event(EventType.OK, {"autoadd_config": 1}))
            
            res = loop.run_until_complete(self.cm.execute("set name NewName"))
            self.assertEqual(res["name"], "NewName")
            
            res = loop.run_until_complete(self.cm.execute("set tx 10"))
            self.assertEqual(res["tx_power"], 10)
            
            res = loop.run_until_complete(self.cm.execute("set pin 1234"))
            self.assertEqual(res["pin"], 1234)
            
            res = loop.run_until_complete(self.cm.execute("set radio 915.2,125.0,7,1"))
            self.assertEqual(res, {"radio": True})
            
            res = loop.run_until_complete(self.cm.execute("set path_hash_mode 2"))
            self.assertEqual(res, {"path_hash_mode": 2})
            
            res = loop.run_until_complete(self.cm.execute("set autoadd_config 1"))
            self.assertEqual(res, {"autoadd_config": 1})
        finally:
            loop.close()

    def test_node_discover(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.send_node_discover_req = AsyncMock(return_value=self.mock_event(EventType.OK, {"tag": 1234}))
            self.cm.mc.wait_for_event = AsyncMock(side_effect=[
                self.mock_event(EventType.DISCOVER_RESPONSE, {"public_key": "abc"}),
                None
            ])
            
            res = loop.run_until_complete(self.cm.execute("node_discover all"))
            self.assertEqual(res, [{"public_key": "abc"}])
            self.cm.mc.commands.send_node_discover_req.assert_called_with(0xFF, prefix_only=True)
        finally:
            loop.close()

    def test_contacts_commands(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.ensure_contacts = AsyncMock()
            self.cm.mc.commands.get_contacts = AsyncMock()
            self.cm.mc.commands.share_contact = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            self.cm.mc.commands.export_contact = AsyncMock(return_value=self.mock_event(EventType.OK, {"uri": "meshcore://1234"}))
            self.cm.mc.commands.import_contact = AsyncMock(return_value=self.mock_event(EventType.OK))
            self.cm.mc.commands.remove_contact = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            self.cm.mc.commands.change_contact_flags = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            
            self.cm.mc._contacts = {"key1": {"public_key": "key1", "adv_name": "Bob"}}
            self.cm._get_contact = AsyncMock(return_value=self.cm.mc._contacts["key1"])
            
            # list contacts
            res = loop.run_until_complete(self.cm.execute("contacts"))
            self.assertIn("key1", res)
            
            # reload contacts
            res = loop.run_until_complete(self.cm.execute("reload_contacts"))
            self.assertIn("key1", res)
            
            # contact_info
            res = loop.run_until_complete(self.cm.execute("contact_info Bob"))
            self.assertEqual(res["adv_name"], "Bob")
            
            # contact_timeout
            res = loop.run_until_complete(self.cm.execute("contact_timeout Bob 15.0"))
            self.assertEqual(res, {"ok": "timeout set to 15.0 for Bob"})
            self.assertEqual(self.cm.mc._contacts["key1"]["timeout"], 15.0)
            
            # share_contact
            res = loop.run_until_complete(self.cm.execute("share_contact Bob"))
            self.assertEqual(res, {"ok": True})
            
            # export_contact
            res = loop.run_until_complete(self.cm.execute("export_contact Bob"))
            self.assertEqual(res, {"uri": "meshcore://1234"})
            
            # import_contact
            res = loop.run_until_complete(self.cm.execute("import_contact meshcore://1234"))
            self.assertEqual(res, {"ok": "contact imported"})
            
            # change_flags
            res = loop.run_until_complete(self.cm.execute("change_flags Bob 2"))
            self.assertEqual(res, {"ok": True})
            
            # remove_contact
            res = loop.run_until_complete(self.cm.execute("remove_contact Bob"))
            self.assertEqual(res, {"ok": True})
        finally:
            loop.close()

    def test_path_commands(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.send_path_discovery_sync = AsyncMock(return_value=self.mock_event(EventType.OK, {"path": "abc"}))
            self.cm.mc.commands.reset_path = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            self.cm.mc.commands.change_contact_path = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            self.cm.mc.commands.get_advert_path = AsyncMock(return_value=self.mock_event(EventType.OK, {"path": "adv_path"}))
            
            contact = {"public_key": "key1", "adv_name": "Bob", "out_path": "old_path", "out_path_len": 2}
            self.cm._get_contact = AsyncMock(return_value=contact)
            
            # path
            res = loop.run_until_complete(self.cm.execute("path Bob"))
            self.assertEqual(res["out_path"], "old_path")
            
            # disc_path
            res = loop.run_until_complete(self.cm.execute("disc_path Bob"))
            self.assertEqual(res, {"path": "abc"})
            
            # reset_path
            res = loop.run_until_complete(self.cm.execute("reset_path Bob"))
            self.assertEqual(res, {"ok": True})
            self.assertEqual(contact["out_path"], "")
            
            # change_path
            res = loop.run_until_complete(self.cm.execute("change_path Bob 01020304"))
            self.assertEqual(res, {"ok": True})
            
            # advert_path
            res = loop.run_until_complete(self.cm.execute("advert_path Bob"))
            self.assertEqual(res, {"path": "adv_path"})
        finally:
            loop.close()

    def test_peer_sync_queries(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.req_acl_sync = AsyncMock(return_value={"acl": True})
            self.cm.mc.commands.req_telemetry_sync = AsyncMock(return_value={"telemetry": True})
            self.cm.mc.commands.req_regions_sync = AsyncMock(return_value={"regions": [1, 2]})
            self.cm.mc.commands.req_owner_sync = AsyncMock(return_value={"owner": "me"})
            self.cm.mc.commands.req_basic_sync = AsyncMock(return_value={"data": "f0e1d2c3"})
            self.cm.mc.commands.req_mma_sync = AsyncMock(return_value={"mma": True})
            self.cm.mc.commands.req_status_sync = AsyncMock(return_value={"status": "active"})
            self.cm.mc.commands.fetch_all_neighbours = AsyncMock(return_value={"neighbours": []})
            self.cm.mc.commands.req_binary = AsyncMock(return_value={"binary": "data"})
            self.cm.mc.commands.send_trace = AsyncMock(return_value=self.mock_event(EventType.OK, {"expected_ack": b"\x01\0\0\0", "suggested_timeout": 5000}))
            self.cm.mc.wait_for_event = AsyncMock(return_value=self.mock_event(EventType.TRACE_DATA, {"trace": "done"}))
            
            contact = {"public_key": "key1", "adv_name": "Bob"}
            self.cm._get_contact = AsyncMock(return_value=contact)
            
            # req_acl
            res = loop.run_until_complete(self.cm.execute("req_acl Bob"))
            self.assertEqual(res, {"acl": True})
            
            # req_telemetry
            res = loop.run_until_complete(self.cm.execute("req_telemetry Bob"))
            self.assertEqual(res["name"], "Bob")
            self.assertEqual(res["lpp"], {"telemetry": True})
            
            # req_regions
            res = loop.run_until_complete(self.cm.execute("req_regions Bob"))
            self.assertEqual(res["regions"], {"regions": [1, 2]})
            
            # req_owner
            res = loop.run_until_complete(self.cm.execute("req_owner Bob"))
            self.assertEqual(res, {"owner": "me"})
            
            # req_clock
            res = loop.run_until_complete(self.cm.execute("req_clock Bob"))
            self.assertEqual(res, {"clock": 3285377520})
            
            # req_mma
            res = loop.run_until_complete(self.cm.execute("req_mma Bob 1h 2h"))
            self.assertEqual(res, {"mma": True})
            
            # req_status
            res = loop.run_until_complete(self.cm.execute("req_status Bob"))
            self.assertEqual(res, {"status": "active"})
            
            # req_neighbours
            res = loop.run_until_complete(self.cm.execute("req_neighbours Bob"))
            self.assertEqual(res, {"neighbours": []})
            
            # req_binary
            res = loop.run_until_complete(self.cm.execute("req_binary Bob aabb"))
            self.assertEqual(res, {"binary": "data"})
            
            # trace
            res = loop.run_until_complete(self.cm.execute("trace 01020304"))
            self.assertEqual(res, {"trace": "done"})
        finally:
            loop.close()

    def test_pending_contacts(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.pending_contacts = {"key1": {"public_key": "key1", "adv_name": "Bob"}}
            self.cm.mc.flush_pending_contacts = MagicMock()
            self.cm.mc.pop_pending_contact = MagicMock(return_value=self.cm.mc.pending_contacts["key1"])
            self.cm.mc.commands.add_contact = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            
            res = loop.run_until_complete(self.cm.execute("pending_contacts"))
            self.assertIn("key1", res)
            
            res = loop.run_until_complete(self.cm.execute("flush_pending"))
            self.assertEqual(res, {"ok": "pending contacts flushed"})
            self.cm.mc.flush_pending_contacts.assert_called_once()
            
            res = loop.run_until_complete(self.cm.execute("add_pending Bob"))
            self.assertEqual(res, {"ok": True})
            self.cm.mc.pop_pending_contact.assert_called_with("Bob")
        finally:
            loop.close()

    def test_session_commands(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.cm.mc.commands.send_login_sync = AsyncMock(return_value=self.mock_event(EventType.LOGIN_SUCCESS))
            self.cm.mc.commands.send_logout = AsyncMock(return_value=self.mock_event(EventType.OK, {"ok": True}))
            self.cm.mc.commands.send_cmd = AsyncMock(return_value=self.mock_event(EventType.OK, {"expected_ack": b"\x01"}))
            
            contact = {"public_key": "key1", "adv_name": "Bob"}
            self.cm._get_contact = AsyncMock(return_value=contact)
            
            # login
            res = loop.run_until_complete(self.cm.execute("login Bob secret"))
            self.assertEqual(res, {"login_success": True})
            
            # logout
            res = loop.run_until_complete(self.cm.execute("logout Bob"))
            self.assertEqual(res, {"ok": True})
            
            # cmd
            res = loop.run_until_complete(self.cm.execute("cmd Bob mycmd"))
            self.assertEqual(res["expected_ack"], "01")
        finally:
            loop.close()
