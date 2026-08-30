import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from aiohttp import web, ClientSession
from modules.web_viewer import WebViewer

class TestWebViewerModule(unittest.TestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.config = {
            "core": {
                "rateLimiting": {
                    "txRateLimitSeconds": 1.0
                }
            }
        }
        self.bot.timezone = "America/Chicago"
        self.bot.state_cache = MagicMock()
        self.bot.state_cache.get_state = MagicMock(return_value={"telemetry": {"battery": 95}})
        
        self.conn_mgr = MagicMock()
        self.conn_mgr.isConnected = True
        self.conn_mgr.connectionType = "serial"
        self.conn_mgr.mc = MagicMock()
        self.conn_mgr.mc.self_info = {"name": "TestNode", "public_key": "aabbccdd11223344"}
        self.conn_mgr.mc.contacts = [
            {"name": "Neighbor1", "public_key": "1122334455667788", "is_repeater": True}
        ]
        self.conn_mgr.tx_limiter = MagicMock()
        self.conn_mgr.tx_limiter.get_stats = MagicMock(return_value={"total_tx": 5, "total_throttled": 1})
        self.bot.connection_manager = self.conn_mgr

        self.api = MagicMock()
        self.api.bot = self.bot
        self.api.get_state = self.bot.state_cache.get_state
        self.api.send = AsyncMock(return_value={"ok": True})
        self.api.subscribe = MagicMock(return_value=lambda: None)

        self.mod = WebViewer()

    def test_init_and_config(self):
        self.mod.init(self.api, {"enabled": True, "host": "127.0.0.1", "port": 8899, "password": "secret"})
        self.assertEqual(self.mod.host, "127.0.0.1")
        self.assertEqual(self.mod.port, 8899)
        self.assertEqual(self.mod.password, "secret")
        self.assertIn("MeshHub", self.mod.html_content)

    def test_auth_check(self):
        self.mod.init(self.api, {"enabled": True, "password": "my_password"})
        
        req_no_auth = MagicMock()
        req_no_auth.headers = {}
        req_no_auth.query = {}
        self.assertFalse(self.mod._check_auth(req_no_auth))

        req_bearer = MagicMock()
        req_bearer.headers = {"Authorization": "Bearer my_password"}
        req_bearer.query = {}
        self.assertTrue(self.mod._check_auth(req_bearer))

        req_query = MagicMock()
        req_query.headers = {}
        req_query.query = {"token": "my_password"}
        self.assertTrue(self.mod._check_auth(req_query))

    def test_event_handlers_update_state(self):
        self.mod.init(self.api, {"enabled": True})
        
        # 1. Advert event adds node
        self.mod._on_advert({
            "public_key": "repeater_node_01",
            "name": "Hilltop Repeater",
            "is_repeater": True
        })
        self.assertIn("repeater_node_01", self.mod.nodes)
        self.assertEqual(self.mod.nodes["repeater_node_01"]["role"], "Repeater")

        # 2. Message event appends message and link edge
        self.mod._on_message({
            "sender": "repeater_node_01",
            "text": "Ping from hilltop",
            "channel": 0,
            "snr": 11.2,
            "rssi": -78
        })
        self.assertEqual(len(self.mod.messages), 1)
        self.assertIn("self_repeater_node_01", self.mod.edges)
        self.assertEqual(self.mod.edges["self_repeater_node_01"]["snr"], 11.2)

    def test_server_lifecycle_and_api(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Use random free high port for test
            test_port = 18088
            self.mod.init(self.api, {"enabled": True, "host": "127.0.0.1", "port": test_port, "password": ""})
            
            async def run_test():
                await self.mod.start()
                
                async with ClientSession() as session:
                    # 1. GET /
                    async with session.get(f"http://127.0.0.1:{test_port}/") as resp:
                        self.assertEqual(resp.status, 200)
                        text = await resp.text()
                        self.assertIn("MeshHub", text)

                    # 2. GET /api/status
                    async with session.get(f"http://127.0.0.1:{test_port}/api/status") as resp:
                        self.assertEqual(resp.status, 200)
                        data = await resp.json()
                        self.assertEqual(data["name"], "MeshHub")
                        self.assertTrue(data["connected"])

                    # 3. GET /api/contacts
                    async with session.get(f"http://127.0.0.1:{test_port}/api/contacts") as resp:
                        self.assertEqual(resp.status, 200)
                        data = await resp.json()
                        self.assertTrue(len(data) >= 1)

                    # 4. GET /api/graph
                    async with session.get(f"http://127.0.0.1:{test_port}/api/graph") as resp:
                        self.assertEqual(resp.status, 200)
                        data = await resp.json()
                        self.assertIn("nodes", data)
                        self.assertIn("edges", data)

                    # 5. POST /api/radio/sync-time
                    async with session.post(f"http://127.0.0.1:{test_port}/api/radio/sync-time") as resp:
                        self.assertEqual(resp.status, 200)

                await self.mod.stop()

            loop.run_until_complete(run_test())
        finally:
            loop.close()

if __name__ == "__main__":
    unittest.main()
