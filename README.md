# MeshHub Bot — openHop Repeater Plugin (`openhop.meshhub-bot`)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![openHop Plugin](https://img.shields.io/badge/openHop-plugin--schema--v2-green.svg)](https://github.com/openhop-dev/openhop_repeater)
[![Version: 2.0.2](https://img.shields.io/badge/version-2.0.2-blue.svg)](https://github.com/Dhovin/meshhub-bot-openhop/releases/tag/v2.0.2)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**MeshHub Bot** (`openhop.meshhub-bot`) is an official MeshCore companion bot and community automation plugin for **[openHop Repeater](https://github.com/openhop-dev/openhop_repeater)** (the MeshCore LoRa radio infrastructure daemon).

Supervised out-of-process under `openhop-plugin-manager`, MeshHub Bot connects to openHop Repeater's native **Virtual Companion TCP Frame Server** (`127.0.0.1:5051`) and provides essential community radio utilities directly over the mesh, coupled with a rich web dashboard mounted natively inside the openHop web interface.

---

## Architecture & Core Design

MeshHub Bot follows a dual-runtime architecture aligned with openHop Repeater's native capabilities:

1. **Virtual Companion TCP Link**: Communicates directly with openHop's Virtual Companion TCP port (default: `127.0.0.1:5051`). It operates without duplicating radio hardware, time synchronization, or GPS tracking.
2. **Embedded Web UI Control Center**: Served natively by openHop Repeater at `/plugins/openhop.meshhub-bot/`. Provides real-time telemetry, live check-in rosters, over-the-air `!help` previews, ad-hoc packet transmitters, and hot-reloadable configuration forms.
3. **Pure Configuration / Telemetry Separation**:
   - `data/config.json` stores user configuration settings **only**. Any edits made in the Web UI or on disk are hot-reloaded dynamically without restarting the bot daemon.
   - `data/runtime.json` publishes live telemetry snapshots consumed by openHop's `/api/plugins/runtime?id=openhop.meshhub-bot` endpoint. Runtime data is never stored in `config.json`.
4. **Resilient Non-Blocking Startup**: The Web UI and runtime writers initialize immediately upon startup, keeping the dashboard fully responsive even if the companion TCP connection is offline or reconnecting.
5. **135-Byte LoRa Transmission Policy**: All outgoing frames are strictly clamped to $\le 135$ UTF-8 bytes to ensure single-packet delivery, zero packet fragmentation, and minimal channel airtime.
6. **Additive Channel Provisioning**: Required channels (`#test`, `#weather`, `#net`) are additively provisioned on the Virtual Companion without altering existing channel slots or slot 0 (`Public`).

---

## Active Bot Modules

### 1. Keyword Autoresponder (`autoresponce`)
- **Channel**: `#test` (configurable, comma-separated)
- **Keywords**: `test`, `ping`, `ack` (fully **case-insensitive** substring matching; use `*` for all messages)
- **Hop Filter**: Configurable `maxHops` (default: `12`; messages exceeding this hop distance are ignored)
- **Response Template**: `@[{sender}] ACK | Path: {hops_label}`
- **Supported Variables**: `{sender}`, `{hops}`, `{hops_label}`, `{path}`, `{snr}`, `{rssi}`, `{timestamp}`
- **Airtime Protection**: Enforces a per-user rate limit cooldown (default: 30 seconds).

### 2. US NWS Weather & Severe Alerts (`weather_bot`)
- **Default Location**: Fort Worth, TX `76131` (`latitude: 32.8998`, `longitude: -97.3473`).
- **Over-the-Air 5-Digit ZIP Queries**:
  - Responds to **any bare 5-digit US ZIP code** sent directly in `#weather` (e.g. sending just `76131` returns the 3-day forecast with high/low temperatures and weather condition emojis).
  - Also accepts `wx <ZIP>` or `weather <ZIP>`.
- **Scheduled Daily Broadcast**: Broadcasts a compact 3-day forecast to `#weather` using a 5-field cron alarm (default: `0 7 * * *` = 7:00 AM local time).
- **Automated Severe Weather Alerts**:
  - Continuously polls the US National Weather Service (NWS) API for active Watches, Warnings, and Advisories matching the configured coordinates.
  - Automatically broadcasts urgent alerts to `#weather` and sends all-clear notifications once expired.
  - Filters out configured minor severities (`Minor`, `Unknown`) or specific events.
- **Direct Message Forecast Subscriptions**:
  - Mesh users can send direct messages to the bot:
    - `subscribe <5-digit ZIP>`: Subscribes the node to receive daily morning forecasts via DM.
    - `unsub`: Cancels an active DM subscription.

### 3. Weekly Radio Net (`net_bot`)
- **Channel**: `#net`
- **Schedule**: Every **Saturday** at **18:00** local time (`6.0` hours duration).
- **MeshWars Competition Integration**:
  - Automatically tracks **all chat messages** sent in `#net` during active net hours toward participation.
  - Explicit `!checkin` (case-insensitive) commands receive an instant acknowledgement reply (`Welcome {sender}, {count} users have checked in.`).
- **Scheduled Automated Broadcasts**:
  - **1:00 PM Reminder**: `Reminder: MeshWars Net starts today at {time} on {channel}!`
  - **30-Minute Reminder**: `MeshWars Net starts in 30 minutes on {channel}! Get your nodes ready.`
  - **Opening Message**: `Welcome to the MeshWars net! Send !checkin or any chat message to participate.`
  - **Hourly Bonus Reminder**: `Register on https://meshwars.com and any message sent in this chat counts toward your bonus points.`
  - **Halfway Update**: `MeshWars Net Update: Halfway through tonight's net! {count} users checked in so far.`
  - **Final-Hour Update**: `MeshWars Net Update: 1 hour remaining in tonight's net! {count} users checked in so far.`
  - **Closing Summary**: Concludes the net, reports final participant tally, and broadcasts the list of all checked-in callsigns.
- **Web UI Live Console**:
  - Real-time check-in table showing Callsign, Time, SNR, and Hop count.
  - Session control buttons: `Start Net Now`, `End Net & Summarize`, `Send Announcement`, `Clear Roster`, and `Export CSV`.

---

## Over-The-Air Commands ($\le 135$ Bytes)

| Command | Channel / Destination | Description | Frame Limit |
| :--- | :--- | :--- | :--- |
| **`!help`** or **`/help`** | Any Channel or DM | Dynamic summary of active bot modules and channels. | $\le 135$ bytes |
| **`76131`** (any 5-digit ZIP) | `#weather` or DM | Instant 3-day forecast for the requested US ZIP code. | $\le 135$ bytes |
| **`wx [ZIP]`** | `#weather` or DM | Alternate forecast trigger for default or specified ZIP. | $\le 135$ bytes |
| **`subscribe <ZIP>`** | Direct Message | Subscribes caller node to daily morning DM forecasts. | $\le 135$ bytes |
| **`unsub`** | Direct Message | Unsubscribes caller node from DM forecasts. | $\le 135$ bytes |
| **`!checkin`** | `#net` | Checks into the active Radio Net and receives an ACK. | $\le 135$ bytes |
| **Any chat message** | `#net` | Counts as participation during active Saturday net. | $\le 135$ bytes |
| **`test` / `ping` / `ack`** | `#test` | Auto-reply with sender signal SNR and hop count path. | $\le 135$ bytes |

---

## Installation & Deployment

### Quick Install via `openhop-plugin-manager`

On your openHop Repeater host:

```bash
openhop-plugin-manager install https://github.com/Dhovin/meshhub-bot-openhop/releases/download/v2.0.2/openhop_meshhub_bot-2.0.2-py3-none-any.whl
```

Or install directly from the git repository:

```bash
openhop-plugin-manager install git+https://github.com/Dhovin/meshhub-bot-openhop.git
```

To enable the plugin:

```bash
openhop-plugin-manager enable openhop.meshhub-bot
openhop-plugin-manager start openhop.meshhub-bot
```

---

## openHop Repeater Setup

Ensure openHop Repeater has a Virtual Companion enabled in `/etc/openhop_repeater/config.yaml` listening on TCP port `5051`:

```yaml
companions:
  - name: "MeshHubBot"
    settings:
      node_name: "MeshHub-bot"
      tcp_port: 5051
      bind_address: "127.0.0.1"
      tcp_timeout: 0
```

---

## Default Configuration (`config.default.json`)

```json
{
  "schema_version": 2,
  "connection": {
    "host": "127.0.0.1",
    "tcpPort": 5051
  },
  "core": {
    "timezone": "America/Chicago",
    "perUserRateLimitSeconds": 30,
    "txRateLimitSeconds": 1.5,
    "activityLog": true
  },
  "modules": {
    "autoresponce": {
      "enabled": true,
      "channels": ["#test"],
      "keywords": ["test", "ping", "ack"],
      "maxHops": 12,
      "responseTemplate": "@[{sender}] ACK | Path: {hops_label}"
    },
    "weather_bot": {
      "enabled": true,
      "zipCode": "76131",
      "latitude": 32.8998,
      "longitude": -97.3473,
      "weatherChannel": "#weather",
      "alertsChannel": "#weather",
      "weatherAlarm": "0 7 * * *",
      "nws_alerts": true,
      "alertPollSeconds": 300,
      "userAgent": "openHopMeshHubBot/2.0.2 (https://github.com/Dhovin/meshhub-bot-openhop)"
    },
    "net_bot": {
      "enabled": true,
      "channel": "#net",
      "day_of_week": "Saturday",
      "time": "18:00",
      "duration": 6.0,
      "checkin_keyword": "!checkin",
      "enable_reminders": true,
      "enable_hourly_message": true,
      "enable_mid_net_updates": true,
      "state_file": "net_state.json"
    }
  }
}
```

---

## Repository & File Structure

```
meshhub-bot-openhop/
├── openhop-plugin.json           # openHop Schema 1 plugin manifest
├── config.default.json           # Default configuration template (Schema 2)
├── catalogue_entry.json          # openHop plugin catalogue entry
├── pyproject.toml                # Wheel build definition (setuptools data-files)
├── ui/                           # Web Control Center (mounted at /plugins/openhop.meshhub-bot/)
│   ├── index.html                # App shell, forms, net console & auth modal
│   ├── app.js                    # Telemetry polling, hot-reloading & API client
│   └── styles.css                # Dark theme UI stylesheet
├── openhop_meshhub_bot/          # Python daemon package
│   ├── __init__.py               # Version definition (__version__ = "2.0.2")
│   ├── main.py                   # CLI entrypoint (meshhub-bot)
│   ├── hub.py                    # Central BotHub, hot-reloading & command mailbox
│   ├── config.py                 # Schema migration, validation & pure config I/O
│   ├── core/                     # Internal core engines
│   │   ├── scheduler.py          # Cron & interval scheduler
│   │   ├── rate_limiter.py       # Per-user cooldown limiter
│   │   ├── event_bus.py          # Async pub/sub event bus
│   │   ├── template_engine.py    # Placeholder template formatter
│   │   └── state_cache.py        # Telemetry cache
│   ├── mesh/                     # Virtual Companion networking
│   │   ├── client.py             # MeshCore TCP client & prefix cleaner
│   │   ├── txqueue.py            # Priority transmission queue with pacing
│   │   └── text.py               # 135-byte clamping & chunking utilities
│   ├── modules/                  # Bot automation modules
│   │   ├── base.py               # BaseModule contract
│   │   ├── autoresponder.py      # Case-insensitive keyword responder
│   │   ├── weather.py            # US NWS weather, severe alerts & ZIP lookups
│   │   └── net_engine.py         # Radio net session engine with MeshWars tracking
│   └── web/
│       └── runtime_writer.py     # Writes telemetry strictly to data/runtime.json
└── tests/                        # Comprehensive unit test suites (21 tests)
    ├── test_config_migration.py
    ├── test_mesh_client_and_txqueue.py
    ├── test_modules.py
    ├── test_packaging.py
    ├── test_rate_limiter_and_templates.py
    └── test_runtime_writer_cas.py
```

---

## Building & Testing

### Run Unit Tests
```bash
python -m unittest discover -s tests -v
```

### Build Plugin Wheel
```bash
python -m build --wheel
```
The compiled wheel will be generated in `dist/openhop_meshhub_bot-2.0.2-py3-none-any.whl`.

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
