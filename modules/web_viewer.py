import os
import json
import time
import asyncio
import logging
from collections import deque
from aiohttp import web

logger = logging.getLogger("WebViewerModule")

class WebViewer:
    def __init__(self):
        self.name = "web_viewer"
        self.api = None
        self.config = {}
        self.host = "0.0.0.0"
        self.port = 8080
        self.password = ""
        
        self.config_schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "host": {"type": "string"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "password": {"type": "string"}
            },
            "required": ["enabled"]
        }
        
        self.app = None
        self.runner = None
        self.site = None
        self.messages = deque(maxlen=200)
        self.nodes = {}
        self.edges = {}
        self.sse_clients = set()
        
        self.unsub_msg = None
        self.unsub_adv = None
        self.unsub_con = None
        self.unsub_path = None
        self.html_content = ""

    def run_config(self, current_config):
        config = dict(current_config) if current_config else {}
        print("\n--- Configure Web Viewer Settings ---")
        cur_en = config.get("enabled", True)
        val = input(f"Enable Web Viewer Dashboard? (y/n) [current: {'y' if cur_en else 'n'}]: ").strip().lower()
        if val:
            config["enabled"] = val in ("y", "yes", "true")
            
        cur_port = config.get("port", 8080)
        val = input(f"Port to bind [current: {cur_port}]: ").strip()
        if val and val.isdigit():
            config["port"] = int(val)
            
        cur_pwd = config.get("password", "")
        val = input(f"Web Viewer Access Password (leave blank for none) [current: {'***' if cur_pwd else 'None'}]: ").strip()
        if val:
            config["password"] = val
            
        return config

    def init(self, api, config):
        self.api = api
        self.config = config
        self.host = config.get("host", "0.0.0.0")
        self.port = config.get("port", 8080)
        self.password = config.get("password", "")
        
        # Load index.html
        base_dir = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(base_dir, "web_viewer", "index.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                self.html_content = f.read()
        else:
            self.html_content = "<h1>MeshHub Web Viewer</h1><p>index.html not found.</p>"

        logger.info(f"[{self.name}] Initialized Web Viewer on {self.host}:{self.port}")

    async def start(self):
        logger.info(f"[{self.name}] Starting Web Viewer server...")
        self.app = web.Application()
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/api/status', self.handle_api_status)
        self.app.router.add_get('/api/contacts', self.handle_api_contacts)
        self.app.router.add_get('/api/graph', self.handle_api_graph)
        self.app.router.add_get('/api/messages', self.handle_api_messages)
        self.app.router.add_post('/api/radio/reboot', self.handle_radio_reboot)
        self.app.router.add_post('/api/radio/sync-time', self.handle_radio_synctime)
        self.app.router.add_post('/api/send', self.handle_api_send)
        self.app.router.add_get('/api/events', self.handle_sse)
        
        # Subscriptions
        self.unsub_msg = self.api.subscribe("message", self._on_message)
        self.unsub_adv = self.api.subscribe("advert", self._on_advert)
        self.unsub_con = self.api.subscribe("connect", self._on_connect)
        self.unsub_path = self.api.subscribe("path_update", self._on_path_update)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        try:
            await self.site.start()
            logger.info(f"[{self.name}] Web Viewer successfully running at http://{self.host}:{self.port}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to bind Web Viewer on port {self.port}: {e}")

    async def stop(self):
        logger.info(f"[{self.name}] Stopping Web Viewer...")
        for unsub in (self.unsub_msg, self.unsub_adv, self.unsub_con, self.unsub_path):
            if unsub:
                try:
                    unsub()
                except Exception:
                    pass
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        logger.info(f"[{self.name}] Web Viewer stopped.")

    def _check_auth(self, request):
        if not self.password:
            return True
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and auth_header[7:] == self.password:
            return True
        token = request.query.get("token")
        return token == self.password

    async def handle_index(self, request):
        return web.Response(text=self.html_content, content_type='text/html')

    async def handle_api_status(self, request):
        cm = getattr(self.api.bot, "connection_manager", None)
        conn_type = cm.connectionType if cm else "None"
        connected = cm.isConnected if cm else False
        mc = getattr(cm, "mc", None) if cm else None
        self_info = getattr(mc, "self_info", {}) if mc else {}
        
        state = self.api.get_state() or {}
        telemetry = state.get("telemetry", {})
        
        tx_stats = cm.tx_limiter.get_stats() if (cm and getattr(cm, "tx_limiter", None)) else {}
        
        data = {
            "name": "MeshHub",
            "connected": connected,
            "connection_type": conn_type,
            "self_info": self_info,
            "telemetry": telemetry,
            "timezone": getattr(self.api.bot, "timezone", "UTC"),
            "rate_limits": { "tx": tx_stats }
        }
        return web.json_response(data)

    async def handle_api_contacts(self, request):
        contacts = []
        cm = getattr(self.api.bot, "connection_manager", None)
        mc = getattr(cm, "mc", None) if cm else None
        
        if mc and hasattr(mc, "contacts"):
            for c in mc.contacts:
                if isinstance(c, dict):
                    contacts.append(c)
                elif hasattr(c, "__dict__"):
                    contacts.append(c.__dict__)

        for pk, node in self.nodes.items():
            if not any(c.get("public_key") == pk for c in contacts):
                contacts.append(node)
                
        return web.json_response(contacts)

    async def handle_api_graph(self, request):
        cm = getattr(self.api.bot, "connection_manager", None)
        mc = getattr(cm, "mc", None) if cm else None
        self_name = mc.self_info.get("name", "MeshHub Node") if (mc and mc.self_info) else "MeshHub Node"
        
        nodes_list = [{
            "id": "self",
            "name": self_name,
            "role": "Self",
            "is_self": True
        }]
        
        for pk, n in self.nodes.items():
            nodes_list.append({
                "id": pk,
                "name": n.get("name") or pk[:8],
                "role": n.get("role", "Companion"),
                "is_self": False
            })
            
        edges_list = list(self.edges.values())
        return web.json_response({"nodes": nodes_list, "edges": edges_list})

    async def handle_api_messages(self, request):
        return web.json_response(list(self.messages))

    async def handle_radio_reboot(self, request):
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        res = await self.api.send("reboot")
        return web.json_response(res)

    async def handle_radio_synctime(self, request):
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        res = await self.api.send("sync_time")
        return web.json_response(res)

    async def handle_api_send(self, request):
        if not self._check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        body = await request.json()
        cmd = body.get("command")
        if not cmd:
            return web.json_response({"error": "Missing command"}, status=400)
        res = await self.api.send(cmd)
        return web.json_response(res)

    async def handle_sse(self, request):
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
            }
        )
        await response.prepare(request)
        queue = asyncio.Queue()
        self.sse_clients.add(queue)
        try:
            while True:
                data = await queue.get()
                await response.write(f"data: {json.dumps(data)}\n\n".encode('utf-8'))
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self.sse_clients.discard(queue)
        return response

    def _broadcast_sse(self, event_type, data):
        payload = {"type": event_type, "data": data}
        for q in list(self.sse_clients):
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    def _on_message(self, data):
        sender = data.get("sender", "unknown")
        text = data.get("text", "")
        channel = data.get("channel")
        snr = data.get("snr")
        rssi = data.get("rssi")
        path = data.get("path")
        
        msg_record = {
            "sender": sender,
            "text": text,
            "channel": channel,
            "snr": snr,
            "rssi": rssi,
            "path": path,
            "time": time.strftime("%H:%M:%S")
        }
        self.messages.append(msg_record)
        
        if sender and sender != "unknown":
            pk = sender
            if pk not in self.nodes:
                self.nodes[pk] = {"name": sender, "public_key": pk, "role": "Companion", "last_seen": time.time()}
            else:
                self.nodes[pk]["last_seen"] = time.time()
                
            edge_key = f"self_{pk}"
            self.edges[edge_key] = {
                "from": "self",
                "to": pk,
                "snr": snr
            }

        self._broadcast_sse("message", msg_record)

    def _on_advert(self, payload):
        if not payload:
            return
        pk = payload.get("public_key") or payload.get("key")
        if not pk:
            return
            
        name = payload.get("name") or payload.get("node_name") or str(pk)[:8]
        role = "Repeater" if payload.get("is_repeater") else "Companion"
        lat = payload.get("lat")
        lon = payload.get("lon")
        
        self.nodes[pk] = {
            "public_key": pk,
            "name": name,
            "role": role,
            "lat": lat,
            "lon": lon,
            "last_seen": time.time()
        }
        self._broadcast_sse("advert", {"public_key": pk, "name": name, "role": role})

    def _on_connect(self, payload):
        self._broadcast_sse("connect", payload or {})

    def _on_path_update(self, payload):
        if not payload:
            return
        path = payload.get("path", [])
        if len(path) >= 2:
            for i in range(len(path) - 1):
                f = str(path[i])
                t = str(path[i+1])
                k = f"{f}_{t}"
                self.edges[k] = {"from": f, "to": t}
        self._broadcast_sse("path_update", payload)
