# MeshHub: Module Development Guide

This guide provides a step-by-step walkthrough on how to develop custom modules (plugins) for the **MeshHub** Central Hub and integrate them into the main daemon.

---

## 1. Overview of the Module System

MeshHub uses a dynamic, event-driven, and sandboxed plugin system. At startup, the bot:
1. Resolves module paths and checks for directory traversal safety.
2. Dynamically loads modules from the `/modules` folder using Python's `importlib` utility.
3. Instantiates the module class and validates that it implements the required lifecycle hooks.
4. Reads the module's configuration block from `config/config.json` and validates it against the module's optional `config_schema` using our zero-dependency JSON Schema validator.
5. Injects a custom `ModuleAPI` interface and the validated configuration block into the module.

---

## 2. Naming Conventions

To ensure the framework can dynamically locate and import your module:
* **Filename**: Must be lowercase with underscores (e.g., `alarm_bot.py`).
* **Class Name**: Must be the **TitleCase** equivalent of the filename (e.g., class `AlarmBot` in `alarm_bot.py`).
* **Fallback Class Name**: If the TitleCase mapping does not match, the framework will look for a class named `Module`.

---

## 3. Module Anatomy & Lifecycle Hooks

A valid module must be a class implementing three required lifecycle hooks:

```python
class MyModule:
    def __init__(self):
        self.name = "mymodule"  # Unique identifier matching config key
        self.api = None
        self.config = {}

    def init(self, api, config):
        """
        [Required] Hook executed upon module load.
        Injected with the ModuleAPI instance and the module configuration block.
        Can be synchronous or an asynchronous coroutine.
        """
        self.api = api
        self.config = config

    def start(self):
        """
        [Required] Hook executed when the connection handshake is complete.
        Use this to register event subscriptions and scheduled tasks.
        Can be synchronous or an asynchronous coroutine.
        """
        pass

    def stop(self):
        """
        [Required] Hook executed during graceful daemon shutdown.
        Use this to release resources, unsubscribe, and cancel tasks.
        Can be synchronous or an asynchronous coroutine. Must finish within 10s.
        """
        pass
```

---

## 4. Leveraging the Module API

The `api` object injected during the `init` hook provides access to the central bot capabilities:

### A. Subscribing to Events (`api.subscribe`)
Listen to events broadcast over the Event Bus. Returns an `unsubscribe` function which you **must** call in your `stop` hook.
```python
# Synchronous callback
self.unsub_msg = self.api.subscribe("message", self._on_message)

# Asynchronous callback
self.unsub_conn = self.api.subscribe("connect", self._on_connect)
```
**Supported Events:**
* `"message"`: Triggered when private or channel messages are received. Payload:
  ```json
  {
    "sender": "Alice",
    "text": "Hello bot!",
    "channel": 0,
    "timestamp": 1718000000,
    "snr": 9.5,
    "rssi": -85,
    "path": ["nodeA_hash", "nodeB_hash"]
  }
  ```
* `"connect"`: Broadcasts when the connection handshake completes. Payload contains device info.
* `"disconnect"`: Broadcasts when connection to the node is lost.
* `"advert"`: Broadcasts raw node telemetry/advertising frames.
* `"path_update"`: Broadcasts node routing/path updates.

### B. Sending Commands (`await api.send`)
Send structured commands directly to the hardware node. Commands are sanitized against multi-line shell injection. Returns a dictionary with the response:
```python
# Sending a private message
res = await self.api.send('msg "Alice" "Hello!"')

# Retrieving node channels
channels = await self.api.send('channels')
```

### C. Accessing Cache State (`api.get_state`)
Retrieves a read-only deep copy of the central state cache (e.g. battery telemetry, neighbors, uptime) to prevent direct mutation:
```python
state = self.api.get_state()
battery = state.get("battery")  # Returns battery percentage (int)
```

### D. Scheduling Tasks (`api.schedule_task`)
Schedules callbacks using standard 5-field cron strings (`minute hour day-of-month month day-of-week`). Runs on minute-aligned ticks inside the asyncio loop. Returns a cancel function.
```python
# Run a status check task every 5 minutes
self.cancel_task = self.api.schedule_task("*/5 * * * *", self.my_periodic_task)
```

---

## 5. Schema Validation

To guarantee your module receives valid parameters, define a `config_schema` dictionary conforming to the JSON Schema Draft-07 specification. If validation fails, the daemon logs the errors and refuses to start.

```python
class MyModule:
    def __init__(self):
        self.name = "mymodule"
        self.config_schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "alertThreshold": {"type": "integer", "minimum": 1, "maximum": 100},
                "logRecipient": {"type": "string"}
            },
            "required": ["enabled", "logRecipient"]
        }
```

---

## 6. Integrating Your Module

To connect your module up to the main application:

### Step 1: Copy to the `/modules` Directory
Place your Python script (e.g., `battery_monitor.py`) directly inside the `modules/` folder of the project.

### Step 2: Configure in `config/config.json`
Add a configuration block matching your module's `name` attribute under the `"modules"` object in `config/config.json`:

```json
{
  "connection": {
    "type": "auto"
  },
  "core": {
    "timeSyncInterval": "0 0 * * *",
    "shutdownTimeoutMs": 10000
  },
  "modules": {
    "template": {
      "enabled": true,
      "messagePrefix": "[MeshHub]",
      "logChannel": 0
    },
    "battery_monitor": {
      "enabled": true,
      "alertThreshold": 20,
      "logRecipient": "OperatorNode"
    }
  }
}
```

---

## 7. Full Code Example: Battery Monitor Bot

Below is a complete, production-ready module (`modules/battery_monitor.py`) that monitors the node's battery percentage every hour, alerts an operator if it drops below a configured threshold, and responds to ping requests:

```python
import logging
import asyncio

logger = logging.getLogger("BatteryMonitor")

class BatteryMonitor:
    def __init__(self):
        self.name = "battery_monitor"
        self.api = None
        self.config = {}
        
        self.config_schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "alertThreshold": {"type": "integer", "minimum": 5, "maximum": 95},
                "logRecipient": {"type": "string"}
            },
            "required": ["enabled", "alertThreshold", "logRecipient"]
        }
        
        self.unsub_msg = None
        self.cancel_cron = None

    def init(self, api, config):
        self.api = api
        self.config = config
        logger.info("[BatteryMonitor] Module initialized.")

    def start(self):
        logger.info("[BatteryMonitor] Starting lifecycle tasks...")
        
        # 1. Listen for incoming operator messages
        self.unsub_msg = self.api.subscribe("message", self._on_message)
        
        # 2. Schedule hourly battery check (Run at minute 0 of every hour)
        self.cancel_cron = self.api.schedule_task("0 * * * *", self.check_battery)
        
        # Trigger immediate check on startup
        asyncio.create_task(self.check_battery())

    def stop(self):
        logger.info("[BatteryMonitor] Cleaning up subscriptions...")
        if self.unsub_msg:
            self.unsub_msg()
        if self.cancel_cron:
            self.cancel_cron()
        logger.info("[BatteryMonitor] Gracefully stopped.")

    async def check_battery(self):
        # Read battery telemetry from cache
        state = self.api.get_state()
        battery = state.get("battery")
        
        if battery is None:
            logger.warning("[BatteryMonitor] Battery level not cached yet.")
            return

        threshold = self.config.get("alertThreshold", 20)
        recipient = self.config.get("logRecipient")
        
        logger.info(f"[BatteryMonitor] Battery level check: {battery}% (Threshold: {threshold}%)")
        
        if battery < threshold:
            alert_msg = f"[ALERT] Companion node battery level is critical: {battery}%!"
            logger.warning(f"[BatteryMonitor] Alerting operator: {alert_msg}")
            # Send alert natively to operator node
            await self.api.send(f'msg "{recipient}" "{alert_msg}"')

    def _on_message(self, data):
        sender = data.get("sender", "unknown")
        text = data.get("text", "").strip().lower()
        
        # Respond to status commands from operator
        if sender == self.config.get("logRecipient") and text == "!status":
            asyncio.create_task(self._send_status(sender))

    async def _send_status(self, recipient):
        state = self.api.get_state()
        battery = state.get("battery", "Unknown")
        uptime = state.get("uptime", "Unknown")
        
        status_msg = f"[Status] Battery: {battery}%, Uptime: {uptime}s"
        await self.api.send(f'msg "{recipient}" "{status_msg}"')
```
