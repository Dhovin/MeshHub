# MeshHub Bot - openHop Repeater Plugin (`openhop.meshhub-bot`)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![openHop Profile](https://img.shields.io/badge/openHop-python--service--v1-green.svg)](https://docs.openhop.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**MeshHub Bot** (`openhop.meshhub-bot`) is a dedicated, fully integrated bot and community automation plugin for **[openHop Repeater](https://github.com/openhop-dev/openhop_repeater)** (the MeshCore LoRa radio infrastructure daemon).

Designed to run supervised out-of-process under `openhop-plugin-manager`, MeshHub Bot bridges a virtual companion identity via openHop's native **TCP Companion Frame Server** (`127.0.0.1:5000`) and provides essential community radio utilities directly over the mesh.

---

## Key Features

1. **Native openHop Companion Integration**: Connects over TCP directly to openHop Repeater's Companion Frame Server, operating as a dedicated repeater companion bot identity without duplicating radio drivers, time synchronization, or GPS tracking.
2. **`!help` Command**: Responds to over-the-air `!help` queries with a single concise message explaining active bot commands, strictly capped at **$\le 135$ characters**.
3. **135-Character Hard Limit Policy**: Features a framework-wide transmission guard that strictly clamps all outgoing LoRa text frames to 135 characters/bytes, preventing packet fragmentation and airtime congestion.
4. **Automated Weather Alerts (`weather_bot`)**: Delivers real-time National Weather Service (NWS) meteorological warnings, hazard alerts, and scheduled forecasts over the mesh.
5. **Scheduled Radio Net Controller (`net_bot`)**: Automatically conducts weekly or daily roll-call check-in nets, logs callsigns/nodes (`#checkin`), and summarizes participants.
6. **MeshWars Competition Net (`meshwars_bot`)**: Conducts automated MeshWars competition net sessions and logs participant bonus check-ins (`!checkin`).
7. **Keyword Autoresponder (`autoresponce`)**: Intelligent auto-reply bot with dynamic placeholder templates (`{sender}`, `{snr}`, `{hops}`, `{path}`) and per-channel filtering.
8. **Anti-Spam Airtime Cooldown**: Enforces per-user rate limiting (default: 30s) on command requests to protect limited sub-GHz LoRa airwaves.
9. **Integrated openHop Dashboard Card**: Serves embedded UI assets (`ui/index.html`, `ui/app.js`, `ui/styles.css`) mounted natively by openHop Repeater at `/plugins/openhop.meshhub-bot/`.

---

## Over-The-Air Commands ($\le 135$ Chars)

| Command | Module | Description | Single Message Limit |
| :--- | :--- | :--- | :--- |
| **`!help`** | Core Dispatcher | Sends single help message summarizing active commands. | $\le 135$ characters |
| **`#checkin`** | `net_bot` | Checks user into the active scheduled radio net session. | $\le 135$ characters |
| **`!checkin`** | `meshwars_bot` | Checks user into the active MeshWars net session. | $\le 135$ characters |
| **Keyword** | `autoresponce` | Responds to matching keywords (e.g. `test`) with ACK & path info. | $\le 135$ characters |

---

## Installation

### One-Command Install on openHop Repeater
```bash
openhop-plugin-manager install https://github.com/Dhovin/meshhub-bot-openhop/releases/latest/download/openhop_meshhub_bot-1.0.7-py3-none-any.whl
```

Or install directly from git:
```bash
openhop-plugin-manager install git+https://github.com/Dhovin/meshhub-bot-openhop.git
```

---

## Directory Structure

```
meshhub-bot-openhop/
├── pyproject.toml              # PEP 621 packaging for openHop (python-service-v1)
├── setup.py                    # Wheel data_files packaging definition
├── catalogue_entry.json        # Catalogue metadata for openhop-plugin-catalogue
├── openhop_meshhub_bot/
│   ├── __init__.py
│   ├── main.py                 # openHop console_script entrypoint
│   ├── core/
│   │   ├── bot.py              # Hub coordinator & 135-char clamping engine
│   │   ├── command_dispatcher.py # !help command & rate-limited dispatcher
│   │   ├── connection_manager.py # TCP Companion client with auto-reconnect
│   │   ├── event_bus.py        # Asynchronous event broker
│   │   ├── module_manager.py   # Module loader & channel sandbox
│   │   ├── rate_limiter.py     # Per-user anti-spam cooldown limiter
│   │   ├── scheduler.py        # Asyncio cron task scheduler
│   │   ├── state_cache.py      # Telemetry & contact store
│   │   ├── template_engine.py  # Response templating & placeholder engine
│   │   └── validator.py        # Zero-dependency JSON schema validator
│   ├── modules/
│   │   ├── autoresponce.py     # Keyword auto-reply
│   │   ├── meshwars_bot.py     # MeshWars net controller
│   │   ├── net_bot.py          # Scheduled radio net controller
│   │   ├── template.py         # Blueprint template for custom bots
│   │   └── weather_bot.py      # NWS weather forecasts & emergency alerts
│   └── share/openhop/plugins/openhop.meshhub-bot/
│       ├── openhop-plugin.json # openHop Schema 2 plugin manifest
│       ├── config.default.json # Default openHop plugin configuration
│       ├── schema.json         # Configuration JSON Schema
│       └── ui/
│           ├── index.html      # Embedded openHop settings card
│           ├── app.js          # Live status & !help preview client
│           └── styles.css      # openHop dark/light styling
├── scripts/
│   ├── pre_push.py             # Pre-push validation (tests & schema check)
│   └── validate_config.py      # Configuration schema validator
├── tests/
│   ├── test_openhop_plugin.py  # !help, 135-char limit, & manifest tests
│   └── ...                     # Comprehensive unit test suites
├── LICENSE
└── README.md
```

---

## openHop Repeater Setup

### 1. Enable Companion in openHop Repeater
In `/etc/openhop_repeater/config.yaml`, ensure a Companion is configured on port 5000:

```yaml
companions:
  - name: "MeshHubBot"
    identity_key: "your_companion_identity_key_hex"
    settings:
      node_name: "MeshHub"
      tcp_port: 5000
      bind_address: "127.0.0.1"
      tcp_timeout: 0
```

### 2. Building the Plugin Wheel
To build the distribution wheel for installation:

```bash
pip install build
python -m build --wheel
```

The compiled wheel is created in `dist/openhop_meshhub_bot-1.0.7-py3-none-any.whl`.

### 3. Running Development Unit Tests
```bash
python -m unittest discover -s tests -p "*.py"
```

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
