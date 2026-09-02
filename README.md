# MeshHub Central Hub Framework

MeshHub is a modular, secure, and cross-platform Python-based central hub for companion radio nodes running the Meshcore protocol. Rather than wrapping CLI subprocesses, MeshHub connects natively to the official Python `meshcore` library. It translates incoming message telemetry and node diagnostics into structured JSON, which it broadcasts over an internal event bus, alongside providing a custom task scheduler, centralized read-only state cache, dynamic plugin system, and browser-based Web Viewer dashboard.

---

## Key Features

1. **Native Hardware Link**: Imports the official Python `meshcore` library directly for native Serial/BLE/TCP communication, avoiding subprocess pipelines and pipe buffering latency.
2. **Auto-Discovery**: Scans serial ports (filtering for typical USB serial bridges), Bluetooth Low Energy nodes (names beginning with `MeshCore-`), or falls back to TCP.
3. **Cron Task Scheduler**: Features a custom, zero-dependency asyncio cron parser supporting standard 5-field cron expressions on self-aligning minute boundaries.
4. **Time Sync**: Automatically synchronizes the radio RTC clock on connection and periodically pushes time updates to prevent drift.
5. **Centralized State Cache**: Maintains a read-only store of telemetry (battery, uptime, neighbors) and returns deep-copied state dictionaries to prevent plugin mutation.
6. **Plugin System & Lifecycles**: Discovers, validates, and loads scripts from the `/modules` folder. Executes `init`, `start`, and `stop` lifecycle hooks.
7. **Graceful Shutdown**: Intercepts `SIGINT`/`SIGTERM` to safely close connections and halt plugins (enforcing a strict 10-second stop timeout limit).
8. **Command Validation**: Sanitizes command strings to block multi-line shell injections.
9. **Persistent GPS Coordinates & Telemetry**: Saves `latitude`, `longitude`, `advert_loc_policy`, and `telemetry_mode_loc` in the configuration, automatically pushing them to the hardware node upon connection to ensure advertisements always broadcast the correct location.
10. **LoRa Airtime Rate Limiting & Safety**: Built-in TX delay throttling, per-user cooldowns, and channel-level rate limiters to comply with duty cycles and prevent RF congestion.
11. **Dynamic Response Templating**: Rich placeholder substitution (`{sender}`, `{connection_info}`, `{snr}`, `{rssi}`, `{timestamp}`, `{elapsed}`, `{path}`, `{hops}`) with newline escape handling.
12. **Web Viewer & Topology Graph**: Integrated lightweight, zero-external-dependency async browser dashboard featuring real-time node diagnostics, heard contacts table, live packet activity stream, radio controls, and an interactive physics-based force-directed topology graph.

---

## Directory Structure

```
MeshHub/
├── bin/
│   ├── meshhub             # Primary Shebang-executable CLI & setup wizard
│   └── meshbot             # Backward-compatible CLI alias
├── config/
│   ├── config.json         # Centralized configuration settings
│   ├── schema.json         # JSON Schema for config.json validation
│   └── meshhub.pid         # Process ID lockfile generated at startup
├── core/
│   ├── bot.py              # Main hub coordinator & bootstrapper
│   ├── connection_manager.py # Native meshcore library connector & auto-discovery
│   ├── event_bus.py        # Asynchronous sync/async event broker
│   ├── module_manager.py   # Dynamic importlib module loader & sandbox
│   ├── rate_limiter.py     # Airtime TX, per-user, and channel rate limiters
│   ├── scheduler.py        # Custom asyncio-based cron scheduler
│   ├── state_cache.py      # Telemetry state store with deep copies
│   ├── template_engine.py  # Response templating & placeholder engine
│   └── validator.py        # Zero-dependency JSON Schema validator
├── modules/
│   ├── autoresponce.py     # Channel auto-reply with templating & rate limiting
│   ├── mqtt.py             # Multi-broker packet capture with Ed25519 tokens
│   ├── net_bot.py          # Automated Ham/Mesh radio check-in net controller
│   ├── template.py         # Blueprint template for custom modules
│   ├── weather_bot.py      # NWS and Open-Meteo weather forecasts & alerts
│   └── web_viewer.py       # Web Viewer dashboard & interactive topology graph
├── scripts/
│   ├── pre_push.py         # Runs pre-push validation (tests & schema check)
│   └── validate_config.py  # Configuration schema validator script
├── tests/
│   └── test_*.py           # Unittest test suites
├── install.sh              # Linux installation and systemd setup script
├── uninstall.sh            # Linux service removal script
├── setup-dev.sh            # Developer git repository configuration script
├── LICENSE                 # MIT License with community attribution
└── README.md
```

---

## Installation & Deployment

### Prerequisites

- **Python**: Version 3.10 or higher.
- **System Packages** (for BLE and C-extension build support): `bluez`, `libffi-dev`, `libssl-dev`, and build essentials.

### Development Environment Setup

Initialize the repository, update origin remote, and register the git pre-push hook:
```bash
chmod +x setup-dev.sh
./setup-dev.sh
```

To run unit tests manually during development:
```bash
python -m unittest discover -s tests -p "*.py"
```

### Linux Deployment

#### Quick One-Liner Installation & Uninstall

To automatically download the code, clone it into your home directory (`~/MeshHub`), set up the service, dependencies, virtual environment, and install the global `meshhub` (and `meshbot`) CLI commands with a single line:
```bash
curl -sSL https://raw.githubusercontent.com/Dhovin/MeshHub/main/install.sh | bash
```

To completely stop services, wipe configuration and databases, and clean up the system-wide installation wrappers:
```bash
curl -sSL https://raw.githubusercontent.com/Dhovin/MeshHub/main/uninstall.sh | bash
```

#### Manual Installation

Alternatively, if you already cloned the repository manually, you can execute the installer inside the repository directory:
```bash
chmod +x install.sh
./install.sh
```

To uninstall manually from within the repository directory:
```bash
chmod +x uninstall.sh
./uninstall.sh
```

---

## Command Line Interface (`meshhub`)

Once installed, the global `meshhub` tool is accessible (symlinked via wrapper to `/usr/local/bin/meshhub` and `/usr/local/bin/meshbot`):

- **Start Daemon**: Starts the daemon. Interoperates with systemd on Linux (`sudo systemctl start meshhub`), and runs in the foreground on Windows.
  ```bash
  meshhub start
  ```
- **Stop Daemon**: Stops the running daemon process. Runs `sudo systemctl stop meshhub` on Linux, and terminates the PID on Windows.
  ```bash
  meshhub stop
  ```
- **Restart Daemon**: Restarts the daemon. Runs `sudo systemctl restart meshhub` on Linux, and spawns a background process on Windows.
  ```bash
  meshhub restart
  ```
- **Configuration Wizard**: Runs an interactive wizard using readline to scan serial ports or BLE nodes, prompting the user for parameters before generating and validating `config.json`.
  ```bash
  meshhub config
  ```
- **Status Dashboard**: Prints service diagnostics. Runs `sudo systemctl status meshhub` on Linux, and reads active PID lockfile status on Windows.
  ```bash
  meshhub status
  ```
- **Troubleshooting Logs**: Streams/tails the logs. Invokes `journalctl -u meshhub -f` when systemd is active on Linux, and falls back to a real-time tail of `config/meshhub.log` on Windows.
  ```bash
  meshhub logs
  ```
*(Note: `meshbot` remains available as a backward-compatible alias for all CLI commands).*

---

## Safe Push Git Pipeline

To prevent push of broken code, the registered Git pre-push hook runs the script `/scripts/pre_push.py` automatically before any `git push` command is allowed to complete.

The pipeline performs the following tasks:
1. **Schema Validation**: Validates `config/config.json` against `config/schema.json` to ensure configuration integrity.
2. **Automated Unit Tests**: Runs the `tests/test_framework.py` test suite. If any tests fail, the git push is blocked.

---

## Creating Custom Modules

Modules are loaded from `/modules` using dynamic imports. To create a custom module, create a python script that exports a class whose name is the TitleCase equivalent of the file name (e.g. class `Template` in `template.py` or class `Module` as a fallback).

You can use [modules/template.py](modules/template.py) as a reference blueprint.

### Module Interface

A valid module must implement:
- `name` (string): Unique identifier matching the configuration block in `config.json`.
- `config_schema` (optional dict): JSON Schema matching its properties.
- `init(api, config)` (sync or async function): Invoked at boot with the module API and its configuration block.
- `start()` (sync or async function): Invoked after all modules are loaded.
- `stop()` (sync or async function): Invoked on graceful shutdown. Clean up subscriptions and tasks here.

### Module API Reference

The framework injects a `ModuleAPI` instance into the `init` hook:

- **`api.subscribe(event_name, callback)`**:
  Subscribe to internal events. Returns an unsubscribe function.
  - Events:
    - `'message'`: Receives structured packet dictionary `{ sender, text, channel, timestamp, snr, rssi, path }`.
    - `'connect'`: Receives device details on connection.
    - `'advert'`: Receives raw node advertisement payloads.
    - `'path_update'`: Receives path/route update payloads.
- **`await api.send(command_string)`**:
  Send a command to the connected hardware node. Sanitized against multi-line shell injection. Returns a dictionary containing the node response.
  - Example: `await api.send('msg alice "Hello Alice"')`
- **`api.get_state()`**:
  Returns a read-only deep copy of the central state cache (battery, neighbor count, uptime, etc.).
- **`api.schedule_task(cron_expression, callback)`**:
  Schedules a task on a cron schedule. Returns a cancel function.
  - Example: `api.schedule_task('*/5 * * * *', self.my_periodic_task)`
- **`api.format_template(template_string, message_data, extra_fields=None)`**:
  Renders a string template substituting `{sender}`, `{connection_info}`, `{snr}`, `{rssi}`, `{timestamp}`, `{elapsed}`, `{path}`, `{hops}`, etc.
  - Example: `api.format_template('Ack to {sender} | {connection_info}', data)`
- **`api.can_send_user(user_key)` / `api.record_user_send(user_key)`**:
  Checks and records per-user cooldowns to prevent user-targeted spam.
- **`api.can_send_channel(channel)` / `api.record_channel_send(channel)`**:
  Checks and records per-channel spacing cooldowns.

---

## Web Viewer & Dashboard

MeshHub includes a built-in, lightweight web dashboard.
- **Port**: Default `8080` (configurable in `config/config.json`).
- **Access**: Open `http://<device-ip>:8080` in your web browser.
- **Features**:
  - **Live Contacts**: Real-time table of heard nodes with roles (Repeater/Companion), SNR, RSSI, and hops.
  - **Interactive Topology Graph**: Force-directed physics canvas showing RF connections, signal qualities, and hop paths.
  - **Real-Time Stream**: Live SSE event feed of channel messages and adverts.
  - **Remote Controls**: Trigger radio RTC time synchronization or radio reboots directly from your browser.

---

## License & Acknowledgements

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

### Acknowledgements
- Special thanks to **Adam Gessaman and contributors** ([agessaman/meshcore-bot](https://github.com/agessaman/meshcore-bot)) for pioneering bot command concepts, rate-limiting ideas, and inspirations that helped shape this project.
- Thanks to the **MeshCore** project for the official Python `meshcore` library.
