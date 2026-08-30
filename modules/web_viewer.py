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
        self.unsub_new = self.api.subscribe("new_contact", self._on_advert)
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
        for unsub in (self.unsub_msg, self.unsub_adv, self.unsub_new, self.unsub_con, self.unsub_path):
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

    def _find_contact(self, key_or_name):
        cm = getattr(self.api.bot, "connection_manager", None)
        mc = getattr(cm, "mc", None) if cm else None
        if not mc or not key_or_name:
            return None
        
        # 1. Search by key prefix if hex
        if hasattr(mc, "get_contact_by_key_prefix"):
            try:
                found = mc.get_contact_by_key_prefix(str(key_or_name))
                if isinstance(found, dict):
                    return found
            except Exception:
                pass
                
        # 2. Search by name
        if hasattr(mc, "get_contact_by_name"):
            try:
                found = mc.get_contact_by_name(str(key_or_name))
                if isinstance(found, dict):
                    return found
            except Exception:
                pass

        # 3. Search in mc.contacts or mc._contacts dict
        contacts_dict = getattr(mc, "contacts", None)
        if not contacts_dict or not isinstance(contacts_dict, (dict, list)):
            contacts_dict = getattr(mc, "_contacts", None)
            
        if contacts_dict and isinstance(contacts_dict, dict):
            key_lower = str(key_or_name).lower()
            for pk, c in contacts_dict.items():
                if not isinstance(c, dict):
                    continue
                c_pk = str(c.get("public_key", pk)).lower()
                c_name = str(c.get("adv_name") or c.get("name", "")).lower()
                if c_pk.startswith(key_lower) or c_name == key_lower:
                    return c
        elif contacts_dict and isinstance(contacts_dict, list):
            key_lower = str(key_or_name).lower()
            for c in contacts_dict:
                if not isinstance(c, dict):
                    continue
                c_pk = str(c.get("public_key", "")).lower()
                c_name = str(c.get("adv_name") or c.get("name", "")).lower()
                if c_pk.startswith(key_lower) or c_name == key_lower:
                    return c
        return None

    def _is_repeater(self, contact_or_payload, name=""):
        data = contact_or_payload or {}
        if not isinstance(data, dict):
            data = {}
        c_type = data.get("type")
        if c_type in (2, 3):
            return True
        if data.get("is_repeater"):
            return True
        role_val = str(data.get("role", "")).lower()
        if "repeater" in role_val or "roomserver" in role_val:
            return True
        check_name = (name or data.get("adv_name") or data.get("name") or "").lower()
        if any(w in check_name for w in ["repeater", "roompeater", "relay", "gateway", "room server", "roomserver"]):
            return True
        if check_name.startswith("r-") or check_name.startswith("rpt-") or check_name.startswith("rpt "):
            return True
        return False

    async def handle_api_contacts(self, request):
        contacts_dict = {}
        cm = getattr(self.api.bot, "connection_manager", None)
        mc = getattr(cm, "mc", None) if cm else None
        
        # 1. Populate from mc.contacts / mc._contacts (radio node contacts)
        source_contacts = None
        if mc:
            if hasattr(mc, "contacts") and isinstance(mc.contacts, (dict, list)) and mc.contacts:
                source_contacts = mc.contacts
            elif hasattr(mc, "_contacts") and isinstance(mc._contacts, (dict, list)) and mc._contacts:
                source_contacts = mc._contacts

        if source_contacts:
            if isinstance(source_contacts, dict):
                items = source_contacts.items()
            elif isinstance(source_contacts, list):
                items = [(c.get("public_key", f"item_{idx}"), c) for idx, c in enumerate(source_contacts) if isinstance(c, dict)]
            else:
                items = []

            for pk, c in items:
                if not isinstance(c, dict):
                    continue
                pubkey = c.get("public_key") or pk
                name = c.get("adv_name") or c.get("name") or pubkey[:8]
                is_rep = self._is_repeater(c, name)
                c_type = c.get("type", 2 if is_rep else 1)
                role = "Repeater" if is_rep else ("RoomServer" if c_type == 3 else "Companion")
                
                contacts_dict[pubkey] = {
                    "public_key": pubkey,
                    "name": name,
                    "adv_name": name,
                    "role": role,
                    "is_repeater": is_rep,
                    "type": c_type,
                    "lat": c.get("adv_lat") or c.get("lat"),
                    "lon": c.get("adv_lon") or c.get("lon"),
                    "hops": c.get("out_path_len") if (isinstance(c.get("out_path_len"), int) and c.get("out_path_len") >= 0) else None,
                    "last_seen": c.get("last_advert") or c.get("lastmod") or time.time()
                }

        # 2. Merge observed nodes from runtime traffic
        for pk, node in self.nodes.items():
            matched_key = None
            if pk in contacts_dict:
                matched_key = pk
            else:
                # Try matching by name
                for k, v in contacts_dict.items():
                    if v.get("name") and v.get("name") == node.get("name"):
                        matched_key = k
                        break
                        
            if matched_key:
                entry = contacts_dict[matched_key]
                if node.get("snr") is not None:
                    entry["snr"] = node.get("snr")
                if node.get("rssi") is not None:
                    entry["rssi"] = node.get("rssi")
                if node.get("hops") is not None:
                    entry["hops"] = node.get("hops")
                if node.get("last_seen"):
                    entry["last_seen"] = node.get("last_seen")
                if node.get("name") and node["name"] != pk[:8] and not entry.get("name"):
                    entry["name"] = node["name"]
            else:
                contacts_dict[pk] = dict(node)

        # Ensure display names and sort
        result = []
        for c in contacts_dict.values():
            if not c.get("name"):
                c["name"] = c.get("adv_name") or (c.get("public_key", "")[:8] if c.get("public_key") else "Unnamed")
            result.append(c)

        result.sort(key=lambda x: x.get("last_seen") or 0, reverse=True)
        return web.json_response(result)

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
        hops = data.get("hops")
        if hops is None and data.get("path_len") is not None:
            pl = data.get("path_len")
            hops = 0 if pl == 255 else pl
        
        msg_record = {
            "sender": sender,
            "text": text,
            "channel": channel,
            "snr": snr,
            "rssi": rssi,
            "path": path,
            "hops": hops,
            "time": time.strftime("%H:%M:%S")
        }
        self.messages.append(msg_record)
        
        if sender and sender != "unknown":
            contact = self._find_contact(sender)
            pk = contact.get("public_key") if contact else sender
            name = sender
            if contact and (contact.get("adv_name") or contact.get("name")):
                name = contact.get("adv_name") or contact.get("name")
                
            is_rep = self._is_repeater(contact, name)
            role = "Repeater" if is_rep else "Companion"
            
            self.nodes[pk] = {
                "name": name,
                "public_key": pk,
                "role": role,
                "is_repeater": is_rep,
                "snr": snr,
                "rssi": rssi,
                "hops": hops,
                "last_seen": time.time()
            }
                
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

        contact = self._find_contact(pk)
        name = payload.get("adv_name") or payload.get("name") or payload.get("node_name")
        if not name and contact:
            name = contact.get("adv_name") or contact.get("name")
        if not name:
            name = str(pk)[:8]

        is_rep = self._is_repeater(payload, name) or (contact and self._is_repeater(contact, name))
        role = "Repeater" if is_rep else "Companion"
        lat = payload.get("lat") or payload.get("adv_lat") or (contact.get("adv_lat") if contact else None)
        lon = payload.get("lon") or payload.get("adv_lon") or (contact.get("adv_lon") if contact else None)
        
        self.nodes[pk] = {
            "public_key": pk,
            "name": name,
            "role": role,
            "is_repeater": is_rep,
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
