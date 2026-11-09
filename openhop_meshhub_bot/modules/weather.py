"""US NWS Weather & Severe Alert module (no MQTT/Nominatim dependencies)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any

import requests

from ..mesh.text import format_template, shorten_to_bytes, split_to_byte_chunks
from ..mesh.txqueue import PRIORITY_ALERT, PRIORITY_BROADCAST, PRIORITY_REPLY
from .base import BaseModule

logger = logging.getLogger("openhop.meshhub-bot.weather")

MAX_ALERTS_PER_POLL = 3


def get_emoji_for_forecast(forecast_text: str) -> str:
    text = (forecast_text or "").lower()
    if "thunder" in text or "storm" in text:
        return "⛈️"
    if any(x in text for x in ("snow", "ice", "sleet", "freeze", "flurry")):
        return "❄️"
    if any(x in text for x in ("rain", "shower", "drizzle")):
        return "🌧️"
    if any(x in text for x in ("fog", "mist", "haze")):
        return "🌫️"
    if any(x in text for x in ("wind", "breezy", "windy")):
        return "💨"
    if any(x in text for x in ("mostly sunny", "partly sunny", "mostly clear", "partly cloudy")):
        return "🌤️"
    if any(x in text for x in ("sunny", "clear")):
        return "☀️"
    if any(x in text for x in ("cloud", "overcast", "gloomy")):
        return "☁️"
    return "⛅"


def format_compressed_forecast(label_or_zip: str, periods: list[dict[str, Any]]) -> str:
    """Format a compact 3-day LoRa weather forecast string."""
    groups: dict[str, dict[str, Any]] = {}
    for p in periods:
        start_time = str(p.get("startTime", ""))
        if not start_time:
            continue
        date_str = start_time[:10]
        if date_str not in groups:
            groups[date_str] = {"dateStr": date_str, "daytime": None, "nighttime": None}
        if p.get("isDaytime"):
            groups[date_str]["daytime"] = p
        else:
            groups[date_str]["nighttime"] = p

    sorted_groups = [groups[k] for k in sorted(groups.keys())][:3]
    header = f"Wx {label_or_zip}:\n" if label_or_zip else "Wx:\n"
    weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    lines: list[str] = []
    for idx, group in enumerate(sorted_groups):
        if idx == 0:
            day_label = "Today"
        else:
            try:
                dt = datetime.strptime(group["dateStr"], "%Y-%m-%d")
                day_label = weekdays[(dt.weekday() + 1) % 7]
            except Exception:
                day_label = group["dateStr"]

        rep = group["daytime"] or group["nighttime"]
        forecast_text = rep.get("shortForecast", "") if rep else ""
        emoji = get_emoji_for_forecast(forecast_text)

        parts: list[str] = []
        if group["daytime"]:
            parts.append(f"H:{group['daytime'].get('temperature', 0)}")
        if group["nighttime"]:
            parts.append(f"L:{group['nighttime'].get('temperature', 0)}")
        lines.append(f"{day_label}: {emoji} {' '.join(parts)}")

    return header + "\n".join(lines)


def format_iso_short(date_str: str | None) -> str:
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(date_str))
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return str(date_str)[:16]


async def resolve_us_zip(zipcode: str, user_agent: str) -> dict[str, Any] | None:
    """Resolve a 5-digit US ZIP code via api.zippopotam.us."""
    zipcode = str(zipcode or "").strip()
    if not re.match(r"^\d{5}$", zipcode):
        return None
    url = f"https://api.zippopotam.us/us/{zipcode}"

    def _fetch():
        res = requests.get(url, headers={"User-Agent": user_agent}, timeout=8)
        if res.status_code == 200:
            return res.json()
        return None

    try:
        data = await asyncio.to_thread(_fetch)
        if data and isinstance(data.get("places"), list) and data["places"]:
            place = data["places"][0]
            return {
                "lat": float(place["latitude"]),
                "lon": float(place["longitude"]),
                "displayName": f"{place.get('place name', '')}, {place.get('state abbreviation', '')}".strip(
                    ", "
                ),
            }
    except Exception as exc:
        logger.warning("ZIP lookup failed for %s: %s", zipcode, exc)
    return None


async def fetch_nws_json(url: str, user_agent: str) -> dict[str, Any] | None:
    """Fetch JSON from api.weather.gov in a worker thread."""
    def _fetch():
        headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            return res.json()
        logger.warning("NWS HTTP %s for %s", res.status_code, url)
        return None

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warning("Error fetching NWS URL %s: %s", url, exc)
        return None


class WeatherBotModule(BaseModule):
    """NWS Severe Alerts, Scheduled Daily Forecast, OTA ZIP lookup, and DM Subscriptions."""

    def __init__(self, hub: Any, config: dict[str, Any]):
        self.zip_code: str = "76131"
        self.latitude: float = 32.8998
        self.longitude: float = -97.3473
        self.weather_channel: str = "#weather"
        self.alerts_channel: str = "#weather"
        self.weather_alarm: str = "0 7 * * *"
        self.nws_alerts_enabled: bool = True
        self.alert_poll_seconds: int = 300
        self.user_agent: str = (
            "openHopMeshHubBot/2.0.1 (https://github.com/Dhovin/meshhub-bot-openhop)"
        )
        self.ignore_severities: list[str] = ["Minor", "Unknown"]
        self.ignore_events: list[str] = []
        self.alert_template: str = (
            "⚠️ {event} ({severity}) for {region} until {end}: {headline}"
        )
        self.clear_template: str = "🟢 CLEAR: {event} has ended for {region}."
        self.forecast_template: str = ""

        self.location_name: str = "Fort Worth, TX"
        self.nws_timezone: str = ""
        self.cached_forecast_url: str | None = None
        self.last_forecast_ts: int | None = None
        self.last_alert_check_ts: int | None = None

        self._unschedule_alarm: Any = None
        self._alert_task: asyncio.Task | None = None
        self._running: bool = False

        self._seen_alerts: dict[str, dict[str, Any]] = {}
        self._zip_cache: dict[str, dict[str, Any]] = {
            "76131": {
                "lat": 32.8998,
                "lon": -97.3473,
                "displayName": "Fort Worth, TX",
            }
        }
        super().__init__("weather_bot", hub, config)
        loaded_seen = self._load_json_file(self._seen_alerts_path(), {})
        if isinstance(loaded_seen, dict):
            self._seen_alerts = loaded_seen
        loaded_cache = self._load_json_file(self._cache_path(), {})
        if isinstance(loaded_cache, dict):
            self._zip_cache.update(loaded_cache)

    def _subscriptions_path(self) -> str:
        return os.path.join(self.hub.get_data_dir(), "weather_subscriptions.json")

    def _seen_alerts_path(self) -> str:
        return os.path.join(self.hub.get_data_dir(), "weather_seen_alerts.json")

    def _cache_path(self) -> str:
        return os.path.join(self.hub.get_data_dir(), "weather_cache.json")

    @staticmethod
    def _load_json_file(path: str, default: Any) -> Any:
        if not path or not os.path.isfile(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return default

    @staticmethod
    def _save_json_file(path: str, data: Any) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("Failed saving %s: %s", path, exc)

    def apply_config(self, config: dict[str, Any]) -> None:
        prev_zip = getattr(self, "zip_code", None)
        prev_lat = getattr(self, "latitude", None)
        prev_lon = getattr(self, "longitude", None)
        prev_alarm = getattr(self, "weather_alarm", None)
        prev_enabled = getattr(self, "enabled", False)

        super().apply_config(config)
        self.zip_code = str(self.config.get("zipCode", "76131") or "76131").strip()
        try:
            self.latitude = float(self.config.get("latitude", 32.8998))
            self.longitude = float(self.config.get("longitude", -97.3473))
        except (ValueError, TypeError):
            self.latitude = 32.8998
            self.longitude = -97.3473
        if self.zip_code == "76131" and not self.location_name:
            self.location_name = "Fort Worth, TX"

        self.weather_channel = str(
            self.config.get("weatherChannel")
            or self.config.get("channel")
            or "#weather"
        ).strip()
        self.alerts_channel = str(
            self.config.get("alertsChannel")
            or self.config.get("channel")
            or self.weather_channel
        ).strip()
        self.weather_alarm = str(
            self.config.get("weatherAlarm") or self.config.get("cron") or "0 7 * * *"
        ).strip()
        self.nws_alerts_enabled = bool(self.config.get("nws_alerts", True))
        try:
            self.alert_poll_seconds = max(60, int(self.config.get("alertPollSeconds", 300)))
        except (ValueError, TypeError):
            self.alert_poll_seconds = 300

        self.user_agent = str(
            self.config.get("userAgent")
            or "openHopMeshHubBot/2.0.0 (https://github.com/Dhovin/meshhub-bot-openhop)"
        ).strip()

        raw_ign_sev = self.config.get("ignore_severities", ["Minor", "Unknown"])
        self.ignore_severities = (
            [str(x).strip() for x in raw_ign_sev if str(x).strip()]
            if isinstance(raw_ign_sev, list)
            else ["Minor", "Unknown"]
        )
        raw_ign_ev = self.config.get("ignore_events", [])
        if isinstance(raw_ign_ev, str):
            self.ignore_events = [x.strip() for x in raw_ign_ev.split(",") if x.strip()]
        elif isinstance(raw_ign_ev, list):
            self.ignore_events = [str(x).strip() for x in raw_ign_ev if str(x).strip()]
        else:
            self.ignore_events = []

        self.alert_template = str(
            self.config.get("alertTemplate")
            or "⚠️ {event} ({severity}) for {region} until {end}: {headline}"
        )
        self.clear_template = str(
            self.config.get("clearTemplate") or "🟢 CLEAR: {event} has ended for {region}."
        )
        self.forecast_template = str(self.config.get("forecastTemplate") or "")

        if (
            prev_zip != self.zip_code
            or prev_lat != self.latitude
            or prev_lon != self.longitude
        ):
            self.cached_forecast_url = None

        # If already running, reschedule or restart tasks on config change
        if getattr(self, "_running", False):
            if not self.enabled and prev_enabled:
                asyncio.create_task(self.stop())
            elif self.enabled:
                if prev_alarm != self.weather_alarm or not prev_enabled:
                    self._schedule_daily_alarm()
                if prev_zip != self.zip_code or prev_lat != self.latitude or prev_lon != self.longitude:
                    asyncio.create_task(self._resolve_default_location())
                self._ensure_alert_loop()

    def required_channels(self) -> list[str]:
        if not self.enabled:
            return []
        chs: list[str] = []
        if self.weather_channel:
            chs.append(self.weather_channel)
        if self.alerts_channel and self.alerts_channel not in chs:
            chs.append(self.alerts_channel)
        return chs

    async def start(self) -> None:
        self._running = True
        if not self.enabled:
            return
        await self._resolve_default_location()
        self._schedule_daily_alarm()
        self._ensure_alert_loop()
        logger.info(
            "[weather_bot] Started (location=%s, weatherChannel=%s, alertsChannel=%s, alarm=%s)",
            self.location_name or f"{self.latitude:.4f},{self.longitude:.4f}",
            self.weather_channel,
            self.alerts_channel,
            self.weather_alarm,
        )

    async def stop(self) -> None:
        self._running = False
        if self._unschedule_alarm:
            self._unschedule_alarm()
            self._unschedule_alarm = None
        if self._alert_task and not self._alert_task.done():
            self._alert_task.cancel()
            try:
                await asyncio.wait_for(self._alert_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._alert_task = None

    def _schedule_daily_alarm(self) -> None:
        if self._unschedule_alarm:
            self._unschedule_alarm()
            self._unschedule_alarm = None
        if not self.enabled:
            return
        tz = self.nws_timezone or self.hub.timezone
        try:
            self._unschedule_alarm = self.hub.scheduler.schedule(
                self.weather_alarm,
                self.broadcast_now,
                name="weather_daily_forecast",
                timezone=tz,
            )
        except Exception as exc:
            logger.error("[weather_bot] Invalid weatherAlarm cron '%s': %s", self.weather_alarm, exc)

    def _ensure_alert_loop(self) -> None:
        if not self.enabled or not self.nws_alerts_enabled:
            if self._alert_task and not self._alert_task.done():
                self._alert_task.cancel()
            self._alert_task = None
            return
        if self._alert_task is None or self._alert_task.done():
            self._alert_task = asyncio.create_task(self._alert_poll_loop())

    async def _resolve_default_location(self) -> None:
        """Resolve zipCode (if set) or latitude/longitude against NWS /points."""
        if self.zip_code and re.match(r"^\d{5}$", self.zip_code):
            cached = self._zip_cache.get(self.zip_code)
            if not cached:
                cached = await resolve_us_zip(self.zip_code, self.user_agent)
                if cached:
                    self._zip_cache[self.zip_code] = cached
                    self._save_json_file(self._cache_path(), self._zip_cache)
            if cached:
                self.latitude = float(cached["lat"])
                self.longitude = float(cached["lon"])
                self.location_name = str(cached.get("displayName", self.zip_code))

        points_url = f"https://api.weather.gov/points/{self.latitude:.4f},{self.longitude:.4f}"
        points_data = await fetch_nws_json(points_url, self.user_agent)
        if points_data and isinstance(points_data.get("properties"), dict):
            props = points_data["properties"]
            self.cached_forecast_url = props.get("forecast")
            tz = props.get("timeZone")
            if tz:
                self.nws_timezone = str(tz)
                self._schedule_daily_alarm()
            rel_loc = props.get("relativeLocation", {}).get("properties", {})
            if not self.location_name and rel_loc.get("city"):
                self.location_name = f"{rel_loc.get('city')}, {rel_loc.get('state', '')}".strip(", ")

    # ------------------------------------------------------------------
    # Subscriptions management
    # ------------------------------------------------------------------

    def read_subscriptions(self) -> dict[str, dict[str, Any]]:
        data = self._load_json_file(self._subscriptions_path(), {})
        return data if isinstance(data, dict) else {}

    def write_subscriptions(self, subs: dict[str, dict[str, Any]]) -> None:
        self._save_json_file(self._subscriptions_path(), subs)

    async def add_subscriber_manual(self, recipient: str, zipcode: str) -> dict[str, Any]:
        recipient = str(recipient or "").strip()
        zipcode = str(zipcode or "").strip()
        if not recipient:
            return {"error": "Recipient name or public key required"}
        if not re.match(r"^\d{5}$", zipcode):
            return {"error": "Valid 5-digit US ZIP code required"}

        res = self._zip_cache.get(zipcode) or await resolve_us_zip(zipcode, self.user_agent)
        if not res:
            return {"error": f"Could not resolve ZIP code {zipcode}"}
        self._zip_cache[zipcode] = res
        self._save_json_file(self._cache_path(), self._zip_cache)

        points_url = f"https://api.weather.gov/points/{res['lat']:.4f},{res['lon']:.4f}"
        points = await fetch_nws_json(points_url, self.user_agent)
        forecast_url = (
            points.get("properties", {}).get("forecast") if isinstance(points, dict) else None
        )

        subs = self.read_subscriptions()
        subs[recipient] = {
            "recipient": recipient,
            "publicKeyHex": recipient,
            "zipCode": zipcode,
            "displayName": res.get("displayName", zipcode),
            "lat": res["lat"],
            "lon": res["lon"],
            "forecastUrl": forecast_url,
            "subscribedAt": int(time.time() * 1000),
        }
        self.write_subscriptions(subs)
        return {
            "ok": True,
            "message": f"Added subscriber {recipient} ({res.get('displayName', zipcode)})",
        }

    def remove_subscriber_manual(self, recipient: str) -> dict[str, Any]:
        recipient = str(recipient or "").strip()
        subs = self.read_subscriptions()
        if recipient in subs:
            del subs[recipient]
            self.write_subscriptions(subs)
            return {"ok": True, "message": f"Removed subscriber {recipient}"}
        return {"error": f"Subscriber '{recipient}' not found"}

    # ------------------------------------------------------------------
    # Forecast & NWS Alerts Execution
    # ------------------------------------------------------------------

    async def fetch_forecast_text(
        self,
        *,
        zipcode: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        forecast_url: str | None = None,
    ) -> str:
        target_zip = zipcode if zipcode is not None else self.zip_code
        target_lat = lat if lat is not None else self.latitude
        target_lon = lon if lon is not None else self.longitude
        url = forecast_url or (self.cached_forecast_url if zipcode is None else None)

        if not url:
            points_url = f"https://api.weather.gov/points/{target_lat:.4f},{target_lon:.4f}"
            points = await fetch_nws_json(points_url, self.user_agent)
            if points and isinstance(points.get("properties"), dict):
                url = points["properties"].get("forecast")
                if zipcode is None:
                    self.cached_forecast_url = url
        if not url:
            return "Weather Forecast Unavailable: NWS gridpoint unresolved."

        data = await fetch_nws_json(url, self.user_agent)
        if not data or not isinstance(data.get("properties"), dict):
            return "Weather Forecast Unavailable: NWS fetch failed."
        periods = data["properties"].get("periods") or []
        if not periods:
            return "Weather Forecast Unavailable: No forecast periods."

        label = target_zip or self.location_name or f"{target_lat:.2f},{target_lon:.2f}"
        if self.forecast_template:
            p0 = periods[0]
            return format_template(
                self.forecast_template,
                {
                    "location": label,
                    "zip": target_zip or "",
                    "period": p0.get("name", "Today"),
                    "temp": f"{p0.get('temperature', '')}{p0.get('temperatureUnit', 'F')}",
                    "short": p0.get("shortForecast", ""),
                    "wind": f"{p0.get('windSpeed', '')} {p0.get('windDirection', '')}".strip(),
                },
            )
        return format_compressed_forecast(label, periods)

    async def broadcast_now(self) -> dict[str, Any]:
        """Broadcast the 3-day forecast to weatherChannel and DM subscribers."""
        if not self.cached_forecast_url:
            await self._resolve_default_location()

        forecast_text = await self.fetch_forecast_text()
        chunks = split_to_byte_chunks(forecast_text, 130)
        target_ch = self.hub.resolve_channel_target(self.weather_channel)
        for chunk in chunks:
            await self.hub.tx_queue.send_channel(
                target_ch,
                chunk,
                priority=PRIORITY_BROADCAST,
                module="weather_bot",
                min_spacing=3.0,
                wait=False,
            )

        subs = self.read_subscriptions()
        for recipient, sub in list(subs.items()):
            asyncio.create_task(self._send_subscriber_dm(recipient, sub))

        self.last_forecast_ts = int(time.time())
        self.hub.record_activity(
            module="weather_bot",
            event="forecast_broadcast",
            sender="weather_bot",
            channel=self.weather_channel,
            detail=forecast_text.replace("\n", " | "),
        )
        self.hub.notify_state_change()
        return {"ok": True, "message": f"Broadcasted forecast to {self.weather_channel}"}

    async def _send_subscriber_dm(self, recipient: str, sub: dict[str, Any]) -> None:
        try:
            text = await self.fetch_forecast_text(
                zipcode=sub.get("zipCode"),
                lat=sub.get("lat"),
                lon=sub.get("lon"),
                forecast_url=sub.get("forecastUrl"),
            )
            for chunk in split_to_byte_chunks(text, 130):
                await self.hub.tx_queue.send_dm(
                    recipient,
                    chunk,
                    priority=PRIORITY_BROADCAST,
                    module="weather_bot",
                    min_spacing=3.0,
                    wait=False,
                )
        except Exception as exc:
            logger.warning("Failed sending subscriber DM to %s: %s", recipient, exc)

    async def _alert_poll_loop(self) -> None:
        while self._running and self.enabled and self.nws_alerts_enabled:
            try:
                await self.check_alerts_now()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[weather_bot] Alert poll error: %s", exc, exc_info=True)
            try:
                await asyncio.sleep(self.alert_poll_seconds)
            except asyncio.CancelledError:
                break

    async def check_alerts_now(self) -> dict[str, Any]:
        """Poll NWS active alerts for the configured coordinates and broadcast new/cleared alerts."""
        now_ms = int(time.time() * 1000)
        retention_ms = 24 * 3600 * 1000
        # Prune old cleared alerts (>24h)
        dirty_seen = False
        for fid, entry in list(self._seen_alerts.items()):
            if entry.get("cleared") and (now_ms - int(entry.get("timestamp", 0))) > retention_ms:
                del self._seen_alerts[fid]
                dirty_seen = True

        url = f"https://api.weather.gov/alerts/active?point={self.latitude:.4f},{self.longitude:.4f}"
        data = await fetch_nws_json(url, self.user_agent)
        self.last_alert_check_ts = int(time.time())
        if not data or not isinstance(data.get("features"), list):
            if dirty_seen:
                self._save_json_file(self._seen_alerts_path(), self._seen_alerts)
            return {"ok": False, "message": "NWS active alerts request failed"}

        ignore_sev = {s.lower() for s in self.ignore_severities}
        ignore_ev = [e.lower() for e in self.ignore_events if e]
        active_ids: set[str] = set()
        new_warnings: list[dict[str, Any]] = []

        for feature in data["features"]:
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(props, dict):
                continue
            fid = str(props.get("identifier") or feature.get("id") or "").strip()
            if not fid:
                continue

            end_str = props.get("expires") or props.get("ends")
            if end_str:
                try:
                    if datetime.fromisoformat(str(end_str)).timestamp() < time.time():
                        continue
                except Exception:
                    pass

            severity = str(props.get("severity") or "Unknown").strip()
            event_name = str(props.get("event") or "Weather Alert").strip()
            if severity.lower() in ignore_sev:
                continue
            if ignore_ev and any(kw in event_name.lower() for kw in ignore_ev):
                continue

            active_ids.add(fid)
            if fid in self._seen_alerts:
                continue

            new_warnings.append(
                {
                    "id": fid,
                    "event": event_name,
                    "severity": severity,
                    "certainty": str(props.get("certainty") or ""),
                    "region": str(props.get("areaDesc") or self.location_name or "Local Area"),
                    "start": format_iso_short(props.get("onset") or props.get("effective")),
                    "end": format_iso_short(end_str),
                    "headline": str(props.get("headline") or ""),
                }
            )

        target_ch = self.hub.resolve_channel_target(self.alerts_channel)
        sent_count = 0

        for item in new_warnings[:MAX_ALERTS_PER_POLL]:
            msg_text = format_template(self.alert_template, item)
            shortened = shorten_to_bytes(msg_text, 135)
            await self.hub.tx_queue.send_channel(
                target_ch,
                shortened,
                priority=PRIORITY_ALERT,
                module="weather_bot",
                min_spacing=4.0,
                wait=False,
            )
            self._seen_alerts[item["id"]] = {
                "timestamp": now_ms,
                "event": item["event"],
                "severity": item["severity"],
                "region": item["region"],
                "end": item["end"],
                "cleared": False,
            }
            dirty_seen = True
            sent_count += 1
            self.hub.record_activity(
                module="weather_bot",
                event="nws_alert",
                sender="NWS",
                channel=self.alerts_channel,
                detail=shortened,
            )

        # Record any excess warnings beyond cap as seen so we don't spam later
        for item in new_warnings[MAX_ALERTS_PER_POLL:]:
            self._seen_alerts[item["id"]] = {
                "timestamp": now_ms,
                "event": item["event"],
                "severity": item["severity"],
                "region": item["region"],
                "end": item["end"],
                "cleared": False,
            }
            dirty_seen = True

        # Check for cleared alerts
        for fid, cached in list(self._seen_alerts.items()):
            if fid in active_ids or cached.get("cleared"):
                continue
            clear_text = format_template(
                self.clear_template,
                {
                    "event": cached.get("event", "Weather Alert"),
                    "region": cached.get("region", "Local Area"),
                },
            )
            shortened_clear = shorten_to_bytes(clear_text, 135)
            await self.hub.tx_queue.send_channel(
                target_ch,
                shortened_clear,
                priority=PRIORITY_ALERT,
                module="weather_bot",
                min_spacing=4.0,
                wait=False,
            )
            cached["cleared"] = True
            cached["timestamp"] = now_ms
            dirty_seen = True
            self.hub.record_activity(
                module="weather_bot",
                event="nws_clear",
                sender="NWS",
                channel=self.alerts_channel,
                detail=shortened_clear,
            )

        if dirty_seen:
            self._save_json_file(self._seen_alerts_path(), self._seen_alerts)
        self.hub.notify_state_change()
        return {
            "ok": True,
            "active_count": len(active_ids),
            "new_broadcasted": sent_count,
            "message": f"Checked NWS alerts ({len(active_ids)} active, {sent_count} new)",
        }

    # ------------------------------------------------------------------
    # Over-the-Air Message Handling
    # ------------------------------------------------------------------

    async def on_message(self, msg: dict[str, Any]) -> None:
        if not self.enabled:
            return
        sender = str(msg.get("sender", "unknown"))
        if self.hub.is_self(sender):
            return
        raw_text = str(msg.get("text", "")).strip()
        if not raw_text:
            return

        channel = msg.get("channel")
        is_dm = channel is None

        # Strip optional @[BotName] mention or any "<SenderName>: " prefix
        clean_text = re.sub(r"^@\[[^\]]+\]\s*", "", raw_text).strip()
        clean_text = re.sub(r"^[^:\[\]\r\n]{1,40}:\s*", "", clean_text).strip()
        clean_text = re.sub(r"^@\[[^\]]+\]\s*", "", clean_text).strip()
        lower = clean_text.lower()

        if is_dm:
            if lower in ("help", "menu", "wx"):
                help_msg = "WxBot: Send 5-digit ZIP for forecast, 'subscribe <ZIP>' for daily DM, or 'unsub' to stop."
                await self.hub.tx_queue.send_dm(
                    sender, help_msg, priority=PRIORITY_REPLY, module="weather_bot", wait=False
                )
                return

            if lower.startswith("subscribe") or lower.startswith("sub "):
                m = re.match(r"^(?:subscribe|sub)\s+(\d{5})(?:\s+.*)?$", clean_text, re.IGNORECASE)
                if not m:
                    await self.hub.tx_queue.send_dm(
                        sender,
                        "Usage: subscribe <5-digit ZIP>",
                        priority=PRIORITY_REPLY,
                        module="weather_bot",
                        wait=False,
                    )
                    return
                z = m.group(1)
                res = await self.add_subscriber_manual(sender, z)
                reply = (
                    f"Subscribed to daily forecasts for {z}!"
                    if res.get("ok")
                    else f"Error: {res.get('error', 'Failed')}"
                )
                await self.hub.tx_queue.send_dm(
                    sender, reply, priority=PRIORITY_REPLY, module="weather_bot", wait=False
                )
                self.hub.notify_state_change()
                return

            if lower in ("unsubscribe", "unsub"):
                res = self.remove_subscriber_manual(sender)
                reply = (
                    "Unsubscribed from daily forecasts."
                    if res.get("ok")
                    else "No active subscription found."
                )
                await self.hub.tx_queue.send_dm(
                    sender, reply, priority=PRIORITY_REPLY, module="weather_bot", wait=False
                )
                self.hub.notify_state_change()
                return

        # Interactive ZIP query (DM or weather channel)
        zipcode: str | None = None
        if re.match(r"^\d{5}$", clean_text):
            zipcode = clean_text
        else:
            m_wx = re.match(r"^[!/#]?(?:weather|wx)(?:\s+(\d{5}))?$", clean_text, re.IGNORECASE)
            if m_wx:
                zipcode = m_wx.group(1) or self.zip_code

        if not zipcode:
            return

        if not is_dm:
            if not (
                self.hub.matches_channel(channel, self.weather_channel)
                or self.hub.matches_channel(channel, self.alerts_channel)
            ):
                return

        if not self.hub.can_send_user(sender):
            return
        self.hub.record_user_send(sender)

        loc = self._zip_cache.get(zipcode) or await resolve_us_zip(zipcode, self.user_agent)
        if not loc:
            err_msg = f"Could not resolve ZIP {zipcode}."
            if is_dm:
                await self.hub.tx_queue.send_dm(sender, err_msg, module="weather_bot", wait=False)
            else:
                target_ch = self.hub.resolve_channel_target(self.weather_channel, fallback=channel)
                await self.hub.tx_queue.send_channel(
                    target_ch, err_msg, priority=PRIORITY_REPLY, module="weather_bot", wait=False
                )
            return

        self._zip_cache[zipcode] = loc
        self._save_json_file(self._cache_path(), self._zip_cache)
        forecast_text = await self.fetch_forecast_text(
            zipcode=zipcode, lat=loc["lat"], lon=loc["lon"]
        )
        chunks = split_to_byte_chunks(forecast_text, 130)
        for chunk in chunks:
            if is_dm:
                await self.hub.tx_queue.send_dm(
                    sender,
                    chunk,
                    priority=PRIORITY_REPLY,
                    module="weather_bot",
                    min_spacing=2.5,
                    wait=False,
                )
            else:
                target_ch = self.hub.resolve_channel_target(self.weather_channel, fallback=channel)
                await self.hub.tx_queue.send_channel(
                    target_ch,
                    chunk,
                    priority=PRIORITY_REPLY,
                    module="weather_bot",
                    min_spacing=2.5,
                    wait=False,
                )
        self.hub.record_activity(
            module="weather_bot",
            event="wx_query",
            sender=sender,
            channel="DM" if is_dm else self.weather_channel,
            detail=f"ZIP {zipcode} -> {forecast_text.splitlines()[0] if forecast_text else ''}",
        )

    def get_runtime_state(self) -> dict[str, Any]:
        subs = self.read_subscriptions()
        active_alerts = [
            {"id": fid, **info}
            for fid, info in self._seen_alerts.items()
            if not info.get("cleared")
        ]
        return {
            "enabled": self.enabled,
            "zipCode": self.zip_code,
            "location": self.location_name or f"{self.latitude:.4f}, {self.longitude:.4f}",
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.nws_timezone or self.hub.timezone,
            "weatherChannel": self.weather_channel,
            "alertsChannel": self.alerts_channel,
            "weatherAlarm": self.weather_alarm,
            "nws_alerts": self.nws_alerts_enabled,
            "alertPollSeconds": self.alert_poll_seconds,
            "active_alerts": active_alerts,
            "subscribers": list(subs.values()),
            "subscriber_count": len(subs),
            "last_forecast_ts": self.last_forecast_ts,
            "last_alert_check_ts": self.last_alert_check_ts,
        }

    def get_help_segment(self) -> str:
        if not self.enabled:
            return ""
        return f"<ZIP>({self.weather_channel})"
