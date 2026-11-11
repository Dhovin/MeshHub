"""Configuration schema v2, defaults, migration, validation, and redaction."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("openhop.meshhub-bot.config")

SCHEMA_VERSION = 2

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "connection": {
        "host": "127.0.0.1",
        "tcpPort": 5051,
    },
    "companion": {
        "host": "127.0.0.1",
        "tcpPort": 5051,
        "pathHashMode": 0,
        "latitude": 32.8998,
        "longitude": -97.3473,
        "advLocPolicy": 1,
        "telemetryLoc": False,
    },
    "core": {
        "timezone": "America/Chicago",
        "perUserRateLimitSeconds": 30,
        "txRateLimitSeconds": 1.5,
        "activityLog": True,
    },
    "modules": {
        "autoresponce": {
            "enabled": True,
            "channels": ["#test"],
            "keywords": ["test", "ping", "ack"],
            "maxHops": 12,
            "responseTemplate": "@[{sender}] ACK | Path: {hops_label}",
        },
        "weather_bot": {
            "enabled": True,
            "zipCode": "76131",
            "latitude": 32.8998,
            "longitude": -97.3473,
            "weatherChannel": "#weather",
            "alertsChannel": "#weather",
            "weatherAlarm": "0 7 * * *",
            "nws_alerts": True,
            "alertPollSeconds": 300,
            "userAgent": "openHopMeshHubBot/2.0.2 (https://github.com/Dhovin/meshhub-bot-openhop)",
            "ignore_severities": ["Minor", "Unknown"],
            "ignore_events": [],
            "alertTemplate": "⚠️ {event} ({severity}) for {region} until {end}: {headline}",
            "clearTemplate": "🟢 CLEAR: {event} has ended for {region}.",
            "forecastTemplate": "",
        },
        "net_bot": {
            "enabled": True,
            "channel": "#net",
            "day_of_week": "Saturday",
            "time": "18:00",
            "duration": 6.0,
            "checkin_keyword": "!checkin",
            "enable_reminders": True,
            "enable_hourly_message": True,
            "enable_mid_net_updates": True,
            "opening_message": "Welcome to the MeshWars net! Send !checkin or any chat message to participate.",
            "hourly_message": "Register on https://meshwars.com and any message sent in this chat counts toward your bonus points.",
            "halfway_message": "MeshWars Net Update: Halfway through tonight's net! {count} {user_str} checked in so far.",
            "final_hour_message": "MeshWars Net Update: 1 hour remaining in tonight's net! {count} {user_str} checked in so far.",
            "closing_message": "The MeshWars net has concluded. Thank you for participating! {count} {user_str} checked in today.",
            "checkin_ack_template": "Welcome {sender}, {count} {user_str} {have_str} checked in.",
            "reminder_1pm_message": "Reminder: MeshWars Net starts today at {time} on {channel}!",
            "reminder_30m_message": "MeshWars Net starts in 30 minutes on {channel}! Get your nodes ready.",
            "state_file": "net_state.json",
        },
    },
}


def detect_system_timezone(fallback: str = "UTC") -> str:
    """Attempt to resolve a valid IANA timezone from environment or system clock."""
    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            ZoneInfo(tz_env)
            return tz_env
        except Exception:
            pass
    try:
        tzinfo = datetime.now().astimezone().tzinfo
        key = getattr(tzinfo, "key", None)
        if key:
            ZoneInfo(key)
            return str(key)
    except Exception:
        pass
    return fallback


def validate_timezone(tz_name: Any, fallback: str = "UTC") -> str:
    """Return tz_name if it is a valid IANA timezone, else fallback."""
    if isinstance(tz_name, str) and tz_name.strip():
        candidate = tz_name.strip()
        try:
            ZoneInfo(candidate)
            return candidate
        except Exception:
            logger.warning("Invalid timezone %r; falling back to %s", candidate, fallback)
    return fallback


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overrides onto a deep copy of base without losing unknown keys."""
    result = copy.deepcopy(base)
    if not isinstance(overrides, dict):
        return result
    for key, val in overrides.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def _normalize_alarm_to_cron(val: Any) -> str:
    """Convert either 'HH:MM' or a 5-field cron string into a valid 5-field cron string."""
    if not isinstance(val, str) or not val.strip():
        return "0 7 * * *"
    s = val.strip()
    parts = s.split()
    if len(parts) == 5:
        return s
    if ":" in s and len(parts) == 1:
        try:
            hh, mm = s.split(":", 1)
            h = max(0, min(23, int(hh)))
            m = max(0, min(59, int(mm)))
            return f"{m} {h} * * *"
        except (ValueError, TypeError):
            pass
    return "0 7 * * *"


def migrate_config(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate a v1.x or legacy v2 config dictionary to schema v2 and merge over DEFAULT_CONFIG.

    Returns (migrated_config, was_migrated_from_v1).
    """
    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULT_CONFIG), False

    raw_version = raw.get("schema_version", 1)
    was_v1 = raw_version < SCHEMA_VERSION
    migrated_any = was_v1

    working = copy.deepcopy(raw)
    modules_in = working.get("modules")
    if not isinstance(modules_in, dict):
        modules_in = {}
        working["modules"] = modules_in

    # --- Weather migration (including 20001 -> 76131 Fort Worth default upgrade) ---
    wx_in = modules_in.get("weather_bot")
    if isinstance(wx_in, dict):
        legacy_ch = wx_in.get("channel")
        legacy_chs = wx_in.get("channels")
        if isinstance(legacy_chs, dict):
            if "weatherChannel" not in wx_in and legacy_chs.get("weather"):
                wx_in["weatherChannel"] = str(legacy_chs["weather"])
            if "alertsChannel" not in wx_in and legacy_chs.get("alerts"):
                wx_in["alertsChannel"] = str(legacy_chs["alerts"])
        elif isinstance(legacy_chs, list) and legacy_chs:
            if "weatherChannel" not in wx_in:
                wx_in["weatherChannel"] = str(legacy_chs[0])
            if "alertsChannel" not in wx_in:
                wx_in["alertsChannel"] = str(legacy_chs[-1])
        if legacy_ch and isinstance(legacy_ch, str):
            if "weatherChannel" not in wx_in:
                wx_in["weatherChannel"] = legacy_ch
            if "alertsChannel" not in wx_in:
                wx_in["alertsChannel"] = legacy_ch

        if "cron" in wx_in and "weatherAlarm" not in wx_in:
            wx_in["weatherAlarm"] = _normalize_alarm_to_cron(wx_in.get("cron"))
        elif "weatherAlarm" in wx_in:
            wx_in["weatherAlarm"] = _normalize_alarm_to_cron(wx_in.get("weatherAlarm"))

        my_pos = wx_in.get("myPosition")
        if isinstance(my_pos, dict):
            if "latitude" not in wx_in and "lat" in my_pos:
                wx_in["latitude"] = my_pos["lat"]
            if "longitude" not in wx_in and "lon" in my_pos:
                wx_in["longitude"] = my_pos["lon"]

        # Upgrade old Washington DC 20001 default to Fort Worth TX 76131
        if str(wx_in.get("zipCode", "")).strip() == "20001":
            wx_in["zipCode"] = "76131"
            wx_in["latitude"] = 32.8998
            wx_in["longitude"] = -97.3473
            migrated_any = True
        elif was_v1 and "zipCode" not in wx_in and ("latitude" in wx_in or "longitude" in wx_in):
            wx_in["zipCode"] = ""

    # --- Consolidate meshwars_bot -> net_bot on '#net' ---
    mw_in = modules_in.pop("meshwars_bot", None)
    net_in = modules_in.get("net_bot")
    if isinstance(mw_in, dict):
        migrated_any = True
        # If net_bot was missing or had the old disabled Tuesday/#checkin placeholder, replace with meshwars_bot
        is_legacy_net = not isinstance(net_in, dict) or (
            net_in.get("day_of_week") == "Tuesday"
            and net_in.get("time") == "19:00"
            and not net_in.get("enabled", False)
        )
        if is_legacy_net:
            migrated_net = copy.deepcopy(DEFAULT_CONFIG["modules"]["net_bot"])
            migrated_net.update(mw_in)
            if str(migrated_net.get("channel", "")).strip().lower() in ("", "#meshwars", "meshwars"):
                migrated_net["channel"] = "#net"
            migrated_net["state_file"] = "net_state.json"
            modules_in["net_bot"] = migrated_net
    elif isinstance(net_in, dict):
        # Also upgrade standalone legacy Tuesday disabled net_bot placeholder to the new MeshWars-based net_bot
        if (
            net_in.get("day_of_week") == "Tuesday"
            and net_in.get("time") == "19:00"
            and not net_in.get("enabled", False)
            and net_in.get("keyword") == "#checkin"
        ):
            modules_in["net_bot"] = copy.deepcopy(DEFAULT_CONFIG["modules"]["net_bot"])
            migrated_any = True

    # --- Autoresponder migration ---
    ar_in = modules_in.get("autoresponce")
    if isinstance(ar_in, dict):
        if "keywords" not in ar_in or not isinstance(ar_in.get("keywords"), list):
            ar_in["keywords"] = ["test"]

    merged = deep_merge(DEFAULT_CONFIG, working)
    merged["schema_version"] = SCHEMA_VERSION
    merged["modules"].pop("meshwars_bot", None)

    # Validate & normalize core fields
    core = merged.setdefault("core", {})
    if not core.get("timezone"):
        core["timezone"] = detect_system_timezone("UTC")
    else:
        core["timezone"] = validate_timezone(core["timezone"], fallback="UTC")

    try:
        core["perUserRateLimitSeconds"] = max(0, int(float(core.get("perUserRateLimitSeconds", 30))))
    except (ValueError, TypeError):
        core["perUserRateLimitSeconds"] = 30

    try:
        core["txRateLimitSeconds"] = max(0.2, float(core.get("txRateLimitSeconds", 1.5)))
    except (ValueError, TypeError):
        core["txRateLimitSeconds"] = 1.5

    conn = merged.setdefault("connection", {})
    conn["host"] = str(conn.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        conn["tcpPort"] = max(1, min(65535, int(conn.get("tcpPort", 5051))))
    except (ValueError, TypeError):
        conn["tcpPort"] = 5051

    comp = merged.setdefault("companion", {})
    if "companion" not in working and "connection" in working:
        if "host" in working["connection"]:
            comp["host"] = conn["host"]
        if "tcpPort" in working["connection"]:
            comp["tcpPort"] = conn["tcpPort"]
    comp["host"] = str(comp.get("host") or conn["host"]).strip() or "127.0.0.1"
    conn["host"] = comp["host"]
    try:
        comp["tcpPort"] = max(1, min(65535, int(comp.get("tcpPort") or conn["tcpPort"])))
    except (ValueError, TypeError):
        comp["tcpPort"] = 5051
    conn["tcpPort"] = comp["tcpPort"]

    try:
        comp["pathHashMode"] = max(0, min(2, int(comp.get("pathHashMode", 0))))
    except (ValueError, TypeError):
        comp["pathHashMode"] = 0

    try:
        comp["latitude"] = max(-90.0, min(90.0, float(comp.get("latitude", 32.8998))))
    except (ValueError, TypeError):
        comp["latitude"] = 32.8998

    try:
        comp["longitude"] = max(-180.0, min(180.0, float(comp.get("longitude", -97.3473))))
    except (ValueError, TypeError):
        comp["longitude"] = -97.3473

    try:
        comp["advLocPolicy"] = max(0, min(2, int(comp.get("advLocPolicy", 1))))
    except (ValueError, TypeError):
        comp["advLocPolicy"] = 1

    comp["telemetryLoc"] = bool(comp.get("telemetryLoc", False))

    # Normalize weather alarm
    wx_merged = merged["modules"]["weather_bot"]
    wx_merged["weatherAlarm"] = _normalize_alarm_to_cron(wx_merged.get("weatherAlarm"))

    return merged, migrated_any


def redact_config(cfg: dict[str, Any], *, keep_runtime: bool = False) -> dict[str, Any]:
    """Return a copy of cfg with internal/secret keys removed."""
    if not isinstance(cfg, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for k, v in cfg.items():
        if k == "_cmd":
            continue
        if k == "_runtime" and not keep_runtime:
            continue
        if "secret" in str(k).lower() or "password" in str(k).lower():
            continue
        if isinstance(v, dict):
            cleaned[k] = redact_config(v, keep_runtime=keep_runtime)
        else:
            cleaned[k] = copy.deepcopy(v)
    return cleaned


def canonical_config_hash(cfg: dict[str, Any]) -> str:
    """Compute SHA-256 of config excluding volatile keys (_runtime, _cmd)."""
    clean = redact_config(cfg, keep_runtime=False)
    encoded = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_write_config_file(config_path: str, data: dict[str, Any]) -> None:
    """Atomically write clean config dictionary to disk."""
    parent = os.path.dirname(os.path.abspath(config_path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".config.", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
        os.replace(tmp_path, config_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def load_config_file(config_path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load, migrate, and validate config from disk.

    Returns (migrated_config_without_volatile, raw_disk_dict).
    Creates a one-time .v1.bak backup if migrating a v1 file on disk, and
    strips any legacy _runtime bloat from config.json on disk.
    """
    raw: dict[str, Any] = {}
    file_exists = bool(config_path and os.path.isfile(config_path))
    if file_exists:
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                raw = loaded
        except Exception as exc:
            logger.error("Failed to read config from %s: %s; using defaults", config_path, exc)

    migrated, was_v1 = migrate_config(raw)
    if was_v1 and file_exists:
        bak_path = f"{config_path}.v1.bak"
        if not os.path.exists(bak_path):
            try:
                shutil.copy2(config_path, bak_path)
                logger.info("Backed up v1 config to %s", bak_path)
            except OSError as exc:
                logger.warning("Could not write v1 config backup %s: %s", bak_path, exc)

    clean = redact_config(migrated, keep_runtime=False)

    # If the file on disk had _runtime bloat or legacy keys (and no pending _cmd),
    # rewrite config.json cleanly so runtime data is never stored in config.json.
    if file_exists and "_cmd" not in raw:
        has_legacy_bloat = (
            "_runtime" in raw
            or was_v1
            or "meshwars_bot" in (raw.get("modules") or {})
        )
        if has_legacy_bloat:
            try:
                atomic_write_config_file(config_path, clean)
                logger.info("Cleaned legacy keys/_runtime from %s", config_path)
            except Exception as exc:
                logger.warning("Could not rewrite cleaned config %s: %s", config_path, exc)

    return clean, raw
