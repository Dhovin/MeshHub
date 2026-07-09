import unittest
import unittest.mock
import asyncio
from datetime import datetime
from core.event_bus import EventBus
from core.state_cache import StateCache
from core.scheduler import Scheduler, parse_field
from core.validator import validate as validate_schema

class TestEventBus(unittest.TestCase):
    def setUp(self):
        self.eb = EventBus()

    def test_sync_subscription_and_publish(self):
        received = []
        def handler(data):
            received.append(data)
            
        unsub = self.eb.subscribe("test_event", handler)
        self.eb.publish("test_event", "hello")
        self.assertEqual(received, ["hello"])
        
        unsub()
        self.eb.publish("test_event", "world")
        self.assertEqual(received, ["hello"]) # unchanged since unsubscribed

    def test_async_subscription(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            received = []
            async def handler(data):
                received.append(data)
                
            self.eb.subscribe("async_event", handler)
            
            async def run_test():
                self.eb.publish("async_event", "async_payload")
                await asyncio.sleep(0.05)
                
            loop.run_until_complete(run_test())
            self.assertEqual(received, ["async_payload"])
        finally:
            loop.close()

    def test_sync_listener_exception_resilience(self):
        # Verify that if a listener raises an exception, the event bus continues to process other listeners
        received = []
        def bad_listener(data):
            raise RuntimeError("Intended test crash")
        def good_listener(data):
            received.append(data)
            
        self.eb.subscribe("test_err_event", bad_listener)
        self.eb.subscribe("test_err_event", good_listener)
        
        try:
            self.eb.publish("test_err_event", "resilient_payload")
        except Exception as e:
            self.fail(f"EventBus publish raised exception: {e}")
            
        self.assertEqual(received, ["resilient_payload"])

    def test_async_listener_exception_resilience(self):
        # Verify that an async listener raising an exception is caught safely inside the EventBus task loop wrapper
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            received = []
            async def bad_async_listener(data):
                raise RuntimeError("Intended test async crash")
            async def good_async_listener(data):
                received.append(data)
                
            self.eb.subscribe("async_err_event", bad_async_listener)
            self.eb.subscribe("async_err_event", good_async_listener)
            
            async def run_test():
                self.eb.publish("async_err_event", "async_resilient_payload")
                await asyncio.sleep(0.05)
                
            loop.run_until_complete(run_test())
            self.assertEqual(received, ["async_resilient_payload"])
        finally:
            loop.close()

class TestStateCache(unittest.TestCase):
    def setUp(self):
        self.cache = StateCache()

    def test_update_and_get_state(self):
        self.cache.update("battery", 85)
        state = self.cache.get_state()
        self.assertEqual(state["battery"], 85)
        self.assertIsNotNone(state["lastUpdated"])
        
        # Verify read-only safety (modifying the returned copy shouldn't mutate cache)
        state["battery"] = 99
        self.assertEqual(self.cache.get_state()["battery"], 85)

    def test_update_from_telemetry(self):
        tel = {
            "battery": 92,
            "uptime": 3600,
            "neighbors": ["node1", "node2"],
            "model": "T-Echo",
            "ver": "3.1.0",
            "radio_freq": 915.0,
            "radio_bw": 125.0,
            "radio_sf": 7,
            "radio_cr": "4/5",
            "name": "MyNode",
            "public_key": "01020304"
        }
        self.cache.update_from_telemetry(tel)
        state = self.cache.get_state()
        self.assertEqual(state["battery"], 92)
        self.assertEqual(state["uptime"], 3600)
        self.assertEqual(state["neighborCount"], 2)
        self.assertEqual(state["neighbors"], ["node1", "node2"])
        self.assertEqual(state["model"], "T-Echo")
        self.assertEqual(state["fwVersion"], "3.1.0")
        self.assertEqual(state["radio_freq"], 915.0)
        self.assertEqual(state["radio_bw"], 125.0)
        self.assertEqual(state["radio_sf"], 7)
        self.assertEqual(state["radio_cr"], "4/5")
        self.assertEqual(state["deviceName"], "MyNode")
        self.assertEqual(state["publicKey"], "01020304")

        # Test binary public key encoding conversion
        tel_bytes = {
            "public_key": b"\x01\x02\x03\x04"
        }
        self.cache.update_from_telemetry(tel_bytes)
        state = self.cache.get_state()
        self.assertEqual(state["publicKey"], "01020304")

class TestScheduler(unittest.TestCase):
    def test_parse_field_wildcard(self):
        matcher = parse_field('*', 0, 59)
        self.assertTrue(matcher(0))
        self.assertTrue(matcher(30))
        self.assertTrue(matcher(59))

    def test_parse_field_ranges_and_steps(self):
        # Step: every 5 minutes
        matcher = parse_field('*/5', 0, 59)
        self.assertTrue(matcher(0))
        self.assertTrue(matcher(5))
        self.assertTrue(matcher(10))
        self.assertFalse(matcher(3))
        
        # Range with step
        matcher = parse_field('10-20/2', 0, 59)
        self.assertTrue(matcher(10))
        self.assertTrue(matcher(12))
        self.assertFalse(matcher(8))
        self.assertFalse(matcher(22))
        
        # Lists
        matcher = parse_field('1,3,5', 0, 59)
        self.assertTrue(matcher(1))
        self.assertTrue(matcher(3))
        self.assertFalse(matcher(2))

    def test_cron_matching(self):
        sched = Scheduler()
        called = False
        def task():
            nonlocal called
            called = True
            
        sched.schedule("15 10 * * *", task)
        
        matching_time = datetime(2026, 6, 8, 10, 15, 0)
        non_matching_time = datetime(2026, 6, 8, 10, 16, 0)
        
        self.assertTrue(sched.tasks[0]["match"](matching_time))
        self.assertFalse(sched.tasks[0]["match"](non_matching_time))

    def test_scheduler_timezone_fallback(self):
        from zoneinfo import ZoneInfo
        from datetime import timezone as dt_timezone
        sched = Scheduler()
        sched.timezone = "America/New_York"  # UTC-4 in June
        
        called = False
        def task():
            nonlocal called
            called = True
            
        # 15:15 in America/New_York is 19:15 UTC
        sched.schedule("15 15 * * *", task)
        
        # Pass a UTC-aware datetime representing 19:15 UTC (which is 15:15 New York time)
        matching_time = datetime(2026, 6, 8, 19, 15, 0, tzinfo=dt_timezone.utc)
        non_matching_time = datetime(2026, 6, 8, 19, 16, 0, tzinfo=dt_timezone.utc)
        
        self.assertTrue(sched.tasks[0]["match"](matching_time))
        self.assertFalse(sched.tasks[0]["match"](non_matching_time))

    def test_scheduler_tick_exception_resilience(self):
        sched = Scheduler()
        sync_called = False
        async_called = False
        
        def bad_sync_task():
            raise RuntimeError("Intended sync schedule crash")
            
        async def bad_async_task():
            raise RuntimeError("Intended async schedule crash")
            
        def good_sync_task():
            nonlocal sync_called
            sync_called = True
            
        async def good_async_task():
            nonlocal async_called
            async_called = True
            
        sched.schedule("* * * * *", bad_sync_task, name="bad_sync")
        sched.schedule("* * * * *", bad_async_task, name="bad_async")
        sched.schedule("* * * * *", good_sync_task, name="good_sync")
        sched.schedule("* * * * *", good_async_task, name="good_async")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async def run_test():
                sched._tick()
                await asyncio.sleep(0.05)
            loop.run_until_complete(run_test())
            
            self.assertTrue(sync_called)
            self.assertTrue(async_called)
        finally:
            loop.close()

class TestValidator(unittest.TestCase):
    def test_validator_types(self):
        schema = {
            "type": "object",
            "properties": {
                "val_str": {"type": "string"},
                "val_int": {"type": "integer"},
                "val_bool": {"type": "boolean"},
                "val_num": {"type": "number"}
            },
            "required": ["val_str", "val_int"]
        }
        
        # Valid data
        valid_data = {
            "val_str": "hello",
            "val_int": 42,
            "val_bool": True,
            "val_num": 3.14
        }
        errors = validate_schema(schema, valid_data)
        self.assertEqual(errors, [])
        
        # Invalid data
        invalid_data = {
            "val_str": 123,
            "val_int": "forty-two",
            "val_bool": "true",
            "val_num": False
        }
        errors = validate_schema(schema, invalid_data)
        self.assertEqual(len(errors), 4)

    def test_validator_required_missing(self):
        schema = {
            "type": "object",
            "required": ["key_a", "key_b"]
        }
        data = {
            "key_a": 1
        }
        errors = validate_schema(schema, data)
        self.assertEqual(errors, ["Path 'key_b' is required"])

class TestModuleAPIRequestChannel(unittest.TestCase):
    def test_request_channel_exists(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            from core.module_manager import ModuleAPI
            from unittest.mock import MagicMock, AsyncMock
            
            bot = MagicMock()
            bot.connection_manager.isConnected = True
            
            # Mock execute to return existing channels list
            existing_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": "weather"},
                {"channel_idx": 2, "channel_name": ""}
            ]
            bot.connection_manager.execute = AsyncMock(return_value=existing_channels)
            
            api = ModuleAPI("test_module", bot)
            
            async def run_test():
                idx = await api.request_channel("weather")
                self.assertEqual(idx, 1)
                bot.connection_manager.execute.assert_called_with("channels")
                self.assertEqual(bot.connection_manager.execute.call_count, 1)
                
            loop.run_until_complete(run_test())
        finally:
            loop.close()

    def test_request_channel_not_exists_adds_it(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            from core.module_manager import ModuleAPI
            from unittest.mock import MagicMock, AsyncMock
            
            bot = MagicMock()
            bot.connection_manager.isConnected = True
            
            # Mock execute to return channel lists and set_channel result
            existing_channels = [
                {"channel_idx": 0, "channel_name": "primary"},
                {"channel_idx": 1, "channel_name": ""}, # empty slot
                {"channel_idx": 2, "channel_name": ""}
            ]
            
            async def mock_execute(cmd):
                if cmd == "channels":
                    return existing_channels
                elif isinstance(cmd, list) and cmd[0] == "set_channel":
                    idx = int(cmd[1])
                    existing_channels[idx]["channel_name"] = cmd[2]
                    return existing_channels[idx]
                return None
                
            bot.connection_manager.execute = AsyncMock(side_effect=mock_execute)
            
            api = ModuleAPI("test_module", bot)
            
            async def run_test():
                idx = await api.request_channel("weather")
                self.assertEqual(idx, 1)
                bot.connection_manager.execute.assert_any_call(["set_channel", "1", "weather"])
                
            loop.run_until_complete(run_test())
        finally:
            loop.close()

class TestTimezoneScheduler(unittest.TestCase):
    def test_timezone_matching(self):
        sched = Scheduler()
        called = False
        def task():
            nonlocal called
            called = True
            
        sched.schedule("15 10 * * *", task, name="tz_task", timezone="America/Chicago")
        
        from zoneinfo import ZoneInfo
        from datetime import timezone
        
        tz_chicago = ZoneInfo("America/Chicago")
        utc_time = datetime(2026, 6, 8, 15, 15, 0, tzinfo=timezone.utc)
        self.assertTrue(sched.tasks[0]["match"](utc_time))
        
        utc_time_non_match = datetime(2026, 6, 8, 16, 15, 0, tzinfo=timezone.utc)
        self.assertFalse(sched.tasks[0]["match"](utc_time_non_match))

class TestChannelRestrictions(unittest.TestCase):
    def test_channel_restrictions(self):
        from core.module_manager import ModuleAPI, active_module_var, ModuleManager
        from core.connection_manager import ConnectionManager
        from unittest.mock import MagicMock, AsyncMock
        from meshcore.events import EventType
        
        bot = MagicMock()
        bot.connection_manager = ConnectionManager(bot)
        bot.connection_manager.isConnected = True
        bot.connection_manager.mc = MagicMock()
        bot.connection_manager.mc.channels = [
            {"channel_idx": 0, "channel_name": "primary"},
            {"channel_idx": 1, "channel_name": "weather"},
            {"channel_idx": 2, "channel_name": "alerts"}
        ]
        
        bot.module_manager = ModuleManager(bot)
        
        active_module_var.set("test_bot")
        api = ModuleAPI("test_bot", bot)
        
        api.declare_channels(["weather", 2])
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async def run_test():
                res_mock = MagicMock()
                res_mock.type = EventType.ACK if hasattr(EventType, 'ACK') else 0
                res_mock.payload = {"ok": True}
                bot.connection_manager.mc.commands.send_chan_msg = AsyncMock(return_value=res_mock)
                
                res = await bot.connection_manager.execute(["chan", "1", "hello"])
                self.assertNotIn("error", res)
                
                res = await bot.connection_manager.execute(["chan", "2", "hello"])
                self.assertNotIn("error", res)
                
                res = await bot.connection_manager.execute(["chan", "0", "hello"])
                self.assertIn("error", res)
                self.assertTrue(res["error"].startswith("Access denied"))
                
                res = await bot.connection_manager.execute(["chan", "primary", "hello"])
                self.assertIn("error", res)
                self.assertTrue(res["error"].startswith("Access denied"))

                res = await bot.connection_manager.execute(["public", "hello"])
                self.assertIn("error", res)
                
            loop.run_until_complete(run_test())
        finally:
            active_module_var.set(None)
            loop.close()

class TestEventMessageParsing(unittest.TestCase):
    def test_on_channel_message_parsing(self):
        from core.connection_manager import ConnectionManager
        from unittest.mock import MagicMock
        
        bot = MagicMock()
        cm = ConnectionManager(bot)
        
        class MockEvent:
            def __init__(self, payload):
                self.payload = payload
                
        evt1 = MockEvent({
            "text": "Dhovin: 76246",
            "channel_idx": 2,
            "sender_timestamp": 1234567,
            "SNR": 4.5,
            "RSSI": -90
        })
        
        cm._on_channel_message(evt1)
        
        bot.event_bus.publish.assert_called()
        published_args = bot.event_bus.publish.call_args[0]
        self.assertEqual(published_args[0], "message")
        msg = published_args[1]
        self.assertEqual(msg["sender"], "Dhovin")
        self.assertEqual(msg["text"], "76246")
        self.assertEqual(msg["channel"], 2)
        self.assertEqual(msg["snr"], 4.5)
        self.assertEqual(msg["rssi"], -90)

        # Test channel index 0 mapping
        evt_chan0 = MockEvent({
            "text": "Hello public chat",
            "channel_idx": 0,
            "sender": "Dhovin",
            "sender_timestamp": 1234567
        })
        cm._on_channel_message(evt_chan0)
        published_args = bot.event_bus.publish.call_args[0]
        msg_chan0 = published_args[1]
        self.assertEqual(msg_chan0["channel"], 0)
        self.assertEqual(msg_chan0["text"], "Hello public chat")

    def test_on_private_message_parsing(self):
        from core.connection_manager import ConnectionManager
        from unittest.mock import MagicMock
        
        bot = MagicMock()
        cm = ConnectionManager(bot)
        
        class MockEvent:
            def __init__(self, payload):
                self.payload = payload
                
        cm.mc = MagicMock()
        cm.mc.get_contact_by_key_prefix = MagicMock(return_value={"adv_name": "Dhovin", "public_key": "083137..."})
        
        evt2 = MockEvent({
            "text": "hello private",
            "pubkey_prefix": "083137",
            "sender_timestamp": 1234568
        })
        
        cm._on_private_message(evt2)
        
        bot.event_bus.publish.assert_called()
        published_args = bot.event_bus.publish.call_args[0]
        self.assertEqual(published_args[0], "message")
        msg = published_args[1]
        self.assertEqual(msg["sender"], "Dhovin")
        self.assertEqual(msg["text"], "hello private")
        self.assertIsNone(msg["channel"])

    def test_on_channel_message_sender_resolution(self):
        from core.connection_manager import ConnectionManager
        from unittest.mock import MagicMock
        
        bot = MagicMock()
        cm = ConnectionManager(bot)
        
        # Set up mock mc
        mc = MagicMock()
        mc._reader = MagicMock()
        parser = MagicMock()
        parser.channels_log = []
        mc._reader.packet_parser = parser
        cm.mc = mc
        
        # Mock get_contact_by_key_prefix
        contacts = {
            "083137aabb": {"adv_name": "Dhovin", "public_key": "083137aabb"},
            "1122334455": {"adv_name": "Alice", "public_key": "1122334455"}
        }
        def mock_get_contact(prefix):
            for pubkey, c in contacts.items():
                if pubkey.startswith(prefix.lower()):
                    return c
            return None
        mc.get_contact_by_key_prefix = mock_get_contact
        
        class MockEvent:
            def __init__(self, payload):
                self.payload = payload
                
        # 1. Matching transport_code in channels_log
        parser.channels_log = [{
            "sender_timestamp": 1000,
            "message": "hello",
            "transport_code": "083137aa",
            "path": ""
        }]
        
        evt = MockEvent({
            "text": "hello",
            "channel_idx": 1,
            "sender_timestamp": 1000
        })
        
        cm._on_channel_message(evt)
        published_msg = bot.event_bus.publish.call_args[0][1]
        self.assertEqual(published_msg["sender"], "Dhovin")
        
        # 2. Guess sender from text prefix
        bot.event_bus.publish.reset_mock()
        evt = MockEvent({
            "text": "Alice: hello there",
            "channel_idx": 1,
            "sender_timestamp": 2000
        })
        
        cm._on_channel_message(evt)
        published_msg = bot.event_bus.publish.call_args[0][1]
        self.assertEqual(published_msg["sender"], "Alice")
        self.assertEqual(published_msg["text"], "hello there")
        
        # 3. Fallback to Unknown-{prefix}
        bot.event_bus.publish.reset_mock()
        parser.channels_log = [{
            "sender_timestamp": 3000,
            "message": "hi",
            "transport_code": "99999999"
        }]
        
        evt = MockEvent({
            "text": "hi",
            "channel_idx": 1,
            "sender_timestamp": 3000
        })
        
        cm._on_channel_message(evt)
        published_msg = bot.event_bus.publish.call_args[0][1]
        self.assertEqual(published_msg["sender"], "Unknown-999999")


class TestTimezoneResolution(unittest.TestCase):
    def test_configured_timezone(self):
        from core.bot import MeshBot
        from unittest.mock import patch, MagicMock
        
        with patch.object(MeshBot, 'load_and_validate_config'), \
             patch.object(MeshBot, 'setup_logging'):
            bot = MeshBot()
            bot.config = {"core": {"timezone": "Europe/London"}}
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(bot.resolve_timezone())
                self.assertEqual(bot.timezone, "Europe/London")
            finally:
                loop.close()

    def test_auto_timezone_resolution(self):
        from core.bot import MeshBot
        from unittest.mock import patch, MagicMock
        
        with patch.object(MeshBot, 'load_and_validate_config'), \
             patch.object(MeshBot, 'setup_logging'), \
             patch('urllib.request.urlopen') as mock_urlopen:
            
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"America/Chicago\n"
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            
            bot = MeshBot()
            bot.config = {"core": {"timezone": "auto"}}
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(bot.resolve_timezone())
                self.assertEqual(bot.timezone, "America/Chicago")
            finally:
                loop.close()

class TestGPSCoordinatePersistence(unittest.TestCase):
    @unittest.mock.patch('core.connection_manager.MeshCore')
    def test_coords_push_on_handshake(self, mock_meshcore):
        from core.connection_manager import ConnectionManager
        from unittest.mock import MagicMock, AsyncMock
        
        bot = MagicMock()
        bot.config = {
            "core": {
                "latitude": 32.7767,
                "longitude": -96.7970
            }
        }
        
        cm = ConnectionManager(bot)
        cm.mc = MagicMock()
        cm.mc.commands.send_device_query = AsyncMock(return_value=MagicMock(type=None, payload={}))
        cm.mc.commands.send_appstart = AsyncMock(return_value=MagicMock(type=None))
        cm.mc.commands.set_coords = AsyncMock(return_value=MagicMock(type=None))
        cm.mc.commands.get_stats_core = AsyncMock(return_value=MagicMock(type=None, payload={}))
        cm.mc.commands.get_stats_radio = AsyncMock(return_value=MagicMock(type=None, payload={}))
        cm.mc.ensure_contacts = AsyncMock()
        cm.mc.start_auto_message_fetching = AsyncMock()
        cm._load_contacts = MagicMock()
        cm._save_contacts = MagicMock()
        cm.sync_time = AsyncMock()
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(cm._run_handshake())
            cm.mc.commands.set_coords.assert_awaited_with(32.7767, -96.7970)
        finally:
            loop.close()

    @unittest.mock.patch('core.connection_manager.MeshCore')
    def test_telemetry_modes_push_on_handshake(self, mock_meshcore):
        from core.connection_manager import ConnectionManager
        from unittest.mock import MagicMock, AsyncMock
        
        bot = MagicMock()
        bot.config = {
            "core": {
                "latitude": 32.7767,
                "longitude": -96.7970,
                "advert_loc_policy": "share",
                "telemetry_mode_loc": "always"
            }
        }
        
        cm = ConnectionManager(bot)
        cm.mc = MagicMock()
        cm.mc.commands.send_device_query = AsyncMock(return_value=MagicMock(type=None, payload={}))
        cm.mc.commands.send_appstart = AsyncMock(return_value=MagicMock(type=None))
        cm.mc.commands.set_coords = AsyncMock(return_value=MagicMock(type=None))
        cm.mc.commands.set_advert_loc_policy = AsyncMock(return_value=MagicMock(type=None))
        cm.mc.commands.set_telemetry_mode_loc = AsyncMock(return_value=MagicMock(type=None))
        cm.mc.commands.get_stats_core = AsyncMock(return_value=MagicMock(type=None, payload={}))
        cm.mc.commands.get_stats_radio = AsyncMock(return_value=MagicMock(type=None, payload={}))
        cm.mc.ensure_contacts = AsyncMock()
        cm.mc.start_auto_message_fetching = AsyncMock()
        cm._load_contacts = MagicMock()
        cm._save_contacts = MagicMock()
        cm.sync_time = AsyncMock()
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(cm._run_handshake())
            cm.mc.commands.set_coords.assert_awaited_with(32.7767, -96.7970)
            cm.mc.commands.set_advert_loc_policy.assert_awaited_with(1)
            cm.mc.commands.set_telemetry_mode_loc.assert_awaited_with(2)
        finally:
            loop.close()

    def test_coords_save_on_cli_set(self):
        from core.connection_manager import ConnectionManager
        from unittest.mock import MagicMock, AsyncMock
        
        bot = MagicMock()
        bot.config = {
            "core": {}
        }
        bot.config_path = "config/config.json"
        
        cm = ConnectionManager(bot)
        cm.isConnected = True
        cm.mc = MagicMock()
        cm.mc.commands.set_coords = AsyncMock(return_value=MagicMock(type=None, payload={}))
        cm.save_config = MagicMock()
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # 1. Test set coords
            res = loop.run_until_complete(cm.execute(["set", "coords", "32.7767,-96.7970"]))
            self.assertEqual(res, {"lat": 32.7767, "lon": -96.7970})
            cm.mc.commands.set_coords.assert_awaited_with(32.7767, -96.7970)
            self.assertEqual(bot.config["core"]["latitude"], 32.7767)
            self.assertEqual(bot.config["core"]["longitude"], -96.7970)
            cm.save_config.assert_called()
            
            # 2. Test set lat
            cm.save_config.reset_mock()
            cm.mc.commands.set_coords.reset_mock()
            res = loop.run_until_complete(cm.execute(["set", "lat", "45.0"]))
            cm.mc.commands.set_coords.assert_awaited_with(45.0, -96.7970)
            self.assertEqual(bot.config["core"]["latitude"], 45.0)
            cm.save_config.assert_called()
            
            # 3. Test set lon
            cm.save_config.reset_mock()
            cm.mc.commands.set_coords.reset_mock()
            res = loop.run_until_complete(cm.execute(["set", "lon", "-100.0"]))
            cm.mc.commands.set_coords.assert_awaited_with(45.0, -100.0)
            self.assertEqual(bot.config["core"]["longitude"], -100.0)
            cm.save_config.assert_called()
        finally:
            loop.close()

class TestNodeTelemetrySyncAndMQTTStatus(unittest.TestCase):
    def test_sync_telemetry_updates_cache(self):
        from core.connection_manager import ConnectionManager
        from core.state_cache import StateCache
        from unittest.mock import MagicMock, AsyncMock
        
        bot = MagicMock()
        bot.state_cache = StateCache()
        
        cm = ConnectionManager(bot)
        cm.isConnected = True
        cm.mc = MagicMock()
        
        # Mock get_stats_core to return core info (uptime, battery_mv)
        cm.mc.commands.get_stats_core = AsyncMock(return_value=MagicMock(
            type=None,
            payload={"uptime_secs": 500, "battery_mv": 3800}
        ))
        
        # Mock get_stats_radio to return radio info (noise_floor)
        cm.mc.commands.get_stats_radio = AsyncMock(return_value=MagicMock(
            type=None,
            payload={"noise_floor": -104}
        ))
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(cm.sync_telemetry())
            state = bot.state_cache.get_state()
            self.assertEqual(state["uptime"], 500)
            self.assertEqual(state["uptime_secs"], 500)
            self.assertEqual(state["battery"], 60)  # (3800 - 3200) / 10 = 60%
            self.assertEqual(state["battery_mv"], 3800)
            self.assertEqual(state["noise_floor"], -104)
        finally:
            loop.close()

    def test_mqtt_publish_status_online(self):
        from modules.mqtt import Mqtt
        from core.state_cache import StateCache
        from unittest.mock import MagicMock, AsyncMock
        import json
        
        # Set up mock bot, api and state cache
        bot = MagicMock()
        state_cache = StateCache()
        state_cache.update("fwVersion", "v1.2.3")
        state_cache.update("model", "T-Beam")
        state_cache.update("battery", 88)
        state_cache.update("battery_mv", 3800)
        state_cache.update("uptime", 1234)
        state_cache.update("uptime_secs", 1234)
        state_cache.update("errors", 0)
        state_cache.update("queue_len", 0)
        state_cache.update("noise_floor", -98)
        state_cache.update("radio_freq", 915.2)
        state_cache.update("radio_bw", 125.0)
        state_cache.update("radio_sf", 7)
        state_cache.update("radio_cr", "4/5")
        state_cache.update("deviceName", "ObsNode")
        state_cache.update("publicKey", "aabbccdd")
        
        api = MagicMock()
        api.bot = bot
        api.get_state = state_cache.get_state
        
        mqtt_mod = Mqtt()
        mqtt_mod.api = api
        mqtt_mod.device_name = "ObsNode"
        mqtt_mod.device_public_key = "aabbccdd"
        mqtt_mod.config = {
            "brokers": [
                {
                    "server": "test.mosquitto.org",
                    "port": 1883,
                    "topic_status": "mesh/status/{PUBLIC_KEY}"
                }
            ]
        }
        
        mock_client = MagicMock()
        mqtt_mod.mqtt_clients = [
            {
                "broker_num": 1,
                "client": mock_client,
                "config": mqtt_mod.config["brokers"][0]
            }
        ]
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(mqtt_mod._publish_status_online(1))
            
            # Check publish was called on mock_client
            mock_client.publish.assert_called_once()
            args, kwargs = mock_client.publish.call_args
            topic = args[0]
            payload_json = args[1]
            
            self.assertEqual(topic, "mesh/status/AABBCCDD")
            payload = json.loads(payload_json)
            self.assertEqual(payload["status"], "online")
            self.assertEqual(payload["origin"], "ObsNode")
            self.assertEqual(payload["origin_id"], "AABBCCDD")
            self.assertEqual(payload["firmware"], "v1.2.3")
            self.assertEqual(payload["firmware_version"], "v1.2.3")
            self.assertEqual(payload["model"], "T-Beam")
            self.assertEqual(payload["battery"], 88)
            self.assertEqual(payload["client"], "meshcore-bot")
            self.assertEqual(payload["client_version"], "meshcore-bot")
            self.assertEqual(payload["radio"], "915.2,125.0,7,4/5")
            self.assertEqual(payload["sf"], 7)
            self.assertEqual(payload["bw"], 125.0)
            self.assertEqual(payload["cr"], "4/5")
            self.assertEqual(payload["uptime"], 1234)
            self.assertEqual(payload["noise_floor"], -98)
            
            # Check stats sub-object
            self.assertIn("stats", payload)
            self.assertEqual(payload["stats"]["uptime_secs"], 1234)
            self.assertEqual(payload["stats"]["battery_mv"], 3800)
            self.assertEqual(payload["stats"]["errors"], 0)
            self.assertEqual(payload["stats"]["queue_len"], 0)
            self.assertEqual(payload["stats"]["noise_floor"], -98)
        finally:
            loop.close()

if __name__ == '__main__':
    unittest.main()
