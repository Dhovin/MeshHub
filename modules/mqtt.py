import logging
import asyncio
import json
import hashlib
import hmac
import random
import time
import re
import os
import ssl
import base64
from datetime import datetime, timezone
from pathlib import Path
from enum import Enum, Flag
from typing import Any, Optional, Dict, List, Tuple
from contextlib import asynccontextmanager
from dataclasses import dataclass

from meshcore import EventType

logger = logging.getLogger("MQTTModule")

# Import paho-mqtt
try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    logger.warning("paho-mqtt not installed. MQTT publishing will be unavailable. Install it with 'pip install paho-mqtt'.")

# Import PyNaCl for Ed25519 signing
try:
    import nacl.bindings
    import nacl.signing
    import nacl.exceptions
    PYNACL_AVAILABLE = True
except ImportError:
    PYNACL_AVAILABLE = False
    logger.warning("PyNaCl not installed. Local Ed25519 token signing will be unavailable. Install it with 'pip install pynacl'.")

# Import cryptography for payload decryption
try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False
    logger.warning("cryptography package not installed. Payload decryption will be unavailable. Install it with 'pip install cryptography'.")


# ==============================================================================
# Protocol Enums & Helpers (from meshcore-packet-capture)
# ==============================================================================

class AdvertFlags(Flag):
    """Advertisement flags for MeshCore packets"""
    ADV_TYPE_NONE = 0x00
    ADV_TYPE_CHAT = 0x01
    ADV_TYPE_REPEATER = 0x02
    ADV_TYPE_ROOM = 0x03
    ADV_TYPE_SENSOR = 0x04
    
    ADV_LATLON_MASK = 0x10    # Has location data
    ADV_FEAT1_MASK = 0x20     # Future feature 1
    ADV_FEAT2_MASK = 0x40     # Future feature 2
    ADV_NAME_MASK = 0x80      # Has name data
    
    IsCompanion = ADV_TYPE_CHAT
    IsRepeater = ADV_TYPE_REPEATER
    IsRoomServer = ADV_TYPE_ROOM
    HasLocation = ADV_LATLON_MASK
    HasName = ADV_NAME_MASK


class PayloadType(Enum):
    """Payload types for MeshCore packets"""
    REQ = 0x00
    RESPONSE = 0x01
    TXT_MSG = 0x02
    ACK = 0x03
    ADVERT = 0x04
    GRP_TXT = 0x05
    GRP_DATA = 0x06
    ANON_REQ = 0x07
    PATH = 0x08
    TRACE = 0x09
    MULTIPART = 0x0A
    CONTROL = 0x0B
    Type12 = 0x0C
    Type13 = 0x0D
    Type14 = 0x0E
    RAW_CUSTOM = 0x0F


class PayloadVersion(Enum):
    VER_1 = 0x00
    VER_2 = 0x01
    VER_3 = 0x02
    VER_4 = 0x03


class RouteType(Enum):
    TRANSPORT_FLOOD = 0x00
    FLOOD = 0x01
    DIRECT = 0x02
    TRANSPORT_DIRECT = 0x03


class DeviceRole(Enum):
    Companion = "Companion"
    Repeater = "Repeater"
    RoomServer = "RoomServer"


# Group order constant for Ed25519
L_ORDER = 2**252 + 27742317777372353535851937790883648493


def int_to_bytes_le(value: int, length: int) -> bytes:
    return value.to_bytes(length, byteorder='little')


def bytes_to_int_le(data: bytes) -> int:
    return int.from_bytes(data, byteorder='little')


def ed25519_sign_with_expanded_key(message: bytes, scalar: bytes, prefix: bytes, public_key: bytes) -> bytes:
    """RFC 8032 Ed25519 signing using pre-expanded private key (orlp format: scalar || prefix)"""
    if not PYNACL_AVAILABLE:
        raise ImportError("PyNaCl is required for native Ed25519 signing.")
    
    # 1. Nonce r = H(prefix || message) mod L
    h_r = hashlib.sha512(prefix + message).digest()
    r = bytes_to_int_le(h_r) % L_ORDER
    r_bytes = int_to_bytes_le(r, 32)
    
    # 2. R = r * B
    R = nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(r_bytes)
    
    # 3. Challenge k = H(R || public_key || message) mod L
    h_k = hashlib.sha512(R + public_key + message).digest()
    k = bytes_to_int_le(h_k) % L_ORDER
    
    # 4. s = (r + k * scalar) mod L
    scalar_int = bytes_to_int_le(scalar)
    s = (r + k * scalar_int) % L_ORDER
    s_bytes = int_to_bytes_le(s, 32)
    
    return R + s_bytes


class AuthTokenPayload:
    def __init__(self, public_key: str, iat: Optional[int] = None, exp: Optional[int] = None, aud: Optional[str] = None, **kwargs):
        self.public_key = public_key.upper()
        self.iat = iat if iat is not None else int(time.time())
        self.exp = exp
        self.aud = aud
        self.custom_claims = kwargs

    def to_dict(self):
        payload = {
            'publicKey': self.public_key,
            'iat': self.iat
        }
        if self.exp is not None:
            payload['exp'] = self.exp
        if self.aud is not None:
            payload['aud'] = self.aud
        for key, value in self.custom_claims.items():
            if value is not None:
                payload[key] = value
        return payload


def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def create_auth_token_internal(payload: AuthTokenPayload, private_key_hex: str, public_key_hex: str) -> str:
    header = {'alg': 'Ed25519', 'typ': 'JWT'}
    header_json = json.dumps(header, separators=(',', ':')).encode('utf-8')
    header_encoded = base64url_encode(header_json)
    
    payload_dict = payload.to_dict()
    payload_json = json.dumps(payload_dict, separators=(',', ':')).encode('utf-8')
    payload_encoded = base64url_encode(payload_json)
    
    signing_input = f"{header_encoded}.{payload_encoded}".encode('utf-8')
    
    private_key_bytes = bytes.fromhex(private_key_hex.strip())
    public_key_bytes = bytes.fromhex(public_key_hex.strip())
    
    if len(private_key_bytes) == 64:
        scalar = private_key_bytes[:32]
        prefix = private_key_bytes[32:]
    elif len(private_key_bytes) == 32:
        h = hashlib.sha512(private_key_bytes).digest()
        scalar = bytearray(h[:32])
        scalar[0] &= 248
        scalar[31] &= 127
        scalar[31] |= 64
        scalar = bytes(scalar)
        prefix = h[32:]
    else:
        raise ValueError(f"Invalid private key length: {len(private_key_bytes)} bytes")
        
    signature = ed25519_sign_with_expanded_key(signing_input, scalar, prefix, public_key_bytes)
    return f"{header_encoded}.{payload_encoded}.{signature.hex()}"


@asynccontextmanager
async def _maybe_lock(lock):
    """Hold lock if supplied, otherwise no-op."""
    if lock is None:
        yield
    else:
        async with lock:
            yield


# ==============================================================================
# Payload Decoding & GRP_TXT Decryption (from meshcore-packet-capture)
# ==============================================================================

ADV_TYPE_MASK = 0x0F
ADV_TYPE_CHAT = 0x01
ADV_TYPE_REPEATER = 0x02
ADV_TYPE_ROOM = 0x03
ADV_TYPE_SENSOR = 0x04
ADV_LATLON_MASK = 0x10
ADV_FEAT1_MASK = 0x20
ADV_FEAT2_MASK = 0x40
ADV_NAME_MASK = 0x80

ADV_TYPE_NAMES = {
    ADV_TYPE_CHAT: "Companion",
    ADV_TYPE_REPEATER: "Repeater",
    ADV_TYPE_ROOM: "RoomServer",
    ADV_TYPE_SENSOR: "Sensor",
}

# The well-known MeshCore default "Public" channel key
DEFAULT_PUBLIC_CHANNEL_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")


def derive_hashtag_key(name: str) -> bytes:
    """Derive a public/hashtag channel key from its name (first 16 bytes of SHA256 of #name)."""
    if not name.startswith("#"):
        name = "#" + name
    return hashlib.sha256(name.lower().encode("utf-8")).digest()[:16]


def channel_hash_for_key(key16: bytes) -> str:
    """Return the 2-hex channel hash (first byte of SHA256(key)) for a channel key."""
    return f"{hashlib.sha256(key16).digest()[0]:02x}"


class ChannelKeyStore:
    """Maps channel_hash -> candidate 16-byte keys (handles hash collisions)."""

    def __init__(self) -> None:
        self._by_hash: Dict[str, List[Tuple[bytes, Optional[str]]]] = {}

    def add_secret(self, key16: bytes, name: Optional[str] = None) -> None:
        if not key16 or len(key16) != 16:
            logger.debug(f"Ignoring channel key with invalid length: {key16!r}")
            return
        h = channel_hash_for_key(key16)
        bucket = self._by_hash.setdefault(h, [])
        if any(existing == key16 for existing, _ in bucket):
            return
        bucket.append((key16, name))

    def add_hex(self, key_hex: str, name: Optional[str] = None) -> None:
        try:
            self.add_secret(bytes.fromhex(key_hex.strip()), name)
        except ValueError:
            logger.debug(f"Ignoring non-hex channel key: {key_hex!r}")

    def add_hashtag(self, name: str) -> None:
        normalized = name if name.startswith("#") else "#" + name
        self.add_secret(derive_hashtag_key(name), normalized.lower())

    def has(self, channel_hash: str) -> bool:
        return channel_hash.lower() in self._by_hash

    def keys_for(self, channel_hash: str) -> List[Tuple[bytes, Optional[str]]]:
        return self._by_hash.get(channel_hash.lower(), [])

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_hash.values())


def decrypt_group_text(ciphertext: bytes, cipher_mac: bytes, key16: bytes) -> Optional[Dict[str, Any]]:
    """Verify+decrypt a GRP_TXT ciphertext with a single 16-byte channel key."""
    if not CRYPTOGRAPHY_AVAILABLE:
        return None
    if len(ciphertext) < 16 or len(ciphertext) % 16 != 0:
        return None

    # MAC: HMAC-SHA256 over ciphertext with 32-byte secret (key16 + 16 zero bytes)
    key32 = key16 + bytes(16)
    calc_mac = hmac.new(key32, ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(calc_mac[:2], cipher_mac[:2]):
        return None

    # Decrypt: AES-128-ECB, no padding
    try:
        decryptor = Cipher(algorithms.AES(key16), modes.ECB(), backend=default_backend()).decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    except Exception as e:
        logger.debug(f"AES decrypt failed: {e}")
        return None

    if len(plaintext) < 5:
        return None

    timestamp = int.from_bytes(plaintext[0:4], "little")
    flags = plaintext[4]

    text = plaintext[5:].decode("utf-8", errors="ignore")
    nul = text.find(chr(0))
    if nul >= 0:
        text = text[:nul]

    # Split "sender: message" when prefix looks like a name
    sender: Optional[str] = None
    content = text
    colon = text.find(": ")
    if 0 < colon < 50:
        candidate = text[:colon]
        if not any(c in candidate for c in ":[]"):
            sender = candidate
            content = text[colon + 2:]

    return {"timestamp": timestamp, "flags": flags, "sender": sender, "text": content}


def _iso_utc(unix_ts: int) -> Optional[str]:
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def decode_group_text(payload: bytes, key_store: Optional[ChannelKeyStore]) -> Dict[str, Any]:
    """Decode and decrypt a GRP_TXT payload using configured channel keys."""
    if len(payload) < 3:
        return {"kind": "GRP_TXT", "decrypted": False, "error": "payload_too_short"}

    channel_hash = f"{payload[0]:02x}"
    cipher_mac = payload[1:3]
    ciphertext = payload[3:]

    result: Dict[str, Any] = {
        "kind": "GRP_TXT",
        "channel_hash": channel_hash,
        "cipher_mac": cipher_mac.hex(),
        "ciphertext_len": len(ciphertext),
        "decrypted": False,
    }

    if key_store and key_store.has(channel_hash):
        for key16, name in key_store.keys_for(channel_hash):
            decrypted = decrypt_group_text(ciphertext, cipher_mac, key16)
            if decrypted:
                result["decrypted"] = True
                result["channel"] = name
                result["sender"] = decrypted["sender"]
                result["text"] = decrypted["text"]
                result["flags"] = decrypted["flags"]
                result["msg_timestamp"] = _iso_utc(decrypted["timestamp"])
                break

    return result


def parse_advert(payload: bytes) -> Dict[str, Any]:
    """Parse an ADVERT payload into structured dictionary."""
    result: Dict[str, Any] = {"kind": "ADVERT"}
    try:
        if len(payload) < 100:
            result.update({"advert_parse_ok": False, "advert_error": "payload_too_short_header"})
            return result

        result.update(
            {
                "advert_parse_ok": True,
                "public_key": payload[0:32].hex(),
                "advert_time": int.from_bytes(payload[32:36], "little"),
                "signature": payload[36:100].hex(),
            }
        )

        app_data = payload[100:]
        if not app_data:
            return result

        flags_byte = app_data[0]
        adv_type = flags_byte & ADV_TYPE_MASK
        result["mode"] = ADV_TYPE_NAMES.get(adv_type, f"Type{adv_type}")

        i = 1
        if flags_byte & ADV_LATLON_MASK:
            if len(app_data) < i + 8:
                return result
            lat = int.from_bytes(app_data[i:i + 4], "little", signed=True)
            lon = int.from_bytes(app_data[i + 4:i + 8], "little", signed=True)
            result["lat"] = round(lat / 1000000.0, 6)
            result["lon"] = round(lon / 1000000.0, 6)
            i += 8

        if flags_byte & ADV_FEAT1_MASK:
            if len(app_data) < i + 2:
                return result
            result["feat1"] = int.from_bytes(app_data[i:i + 2], "little")
            i += 2

        if flags_byte & ADV_FEAT2_MASK:
            if len(app_data) < i + 2:
                return result
            result["feat2"] = int.from_bytes(app_data[i:i + 2], "little")
            i += 2

        if flags_byte & ADV_NAME_MASK and len(app_data) > i:
            result["name"] = app_data[i:].decode("utf-8", errors="ignore").rstrip(chr(0))

        return result
    except Exception as e:
        logger.debug(f"Error parsing ADVERT: {e}")
        result.update({"advert_parse_ok": False, "advert_error": "exception", "advert_error_detail": str(e)})
        return result


def decode_payload(payload_type_value: int, payload: bytes, key_store: Optional[ChannelKeyStore] = None) -> Dict[str, Any]:
    """Decode a packet application payload into structured / plain-text fields."""
    if payload_type_value == PayloadType.GRP_TXT.value:
        return decode_group_text(payload, key_store)
    if payload_type_value == PayloadType.ADVERT.value:
        return parse_advert(payload)
    if payload_type_value == PayloadType.TXT_MSG.value:
        return {
            "kind": "TXT_MSG",
            "encrypted": True,
            "note": "direct message; not decryptable by observer",
        }
    if payload_type_value == PayloadType.ACK.value:
        return {"kind": "ACK", "ack": payload.hex()}

    type_name = "UNKNOWN"
    try:
        type_name = PayloadType(payload_type_value).name
    except ValueError:
        type_name = f"Type{payload_type_value}"
    return {"kind": type_name}


# ==============================================================================
# Neighbors Engine (from meshcore-packet-capture)
# ==============================================================================

STATUS_RESPONDED = "responded"
STATUS_TIMEOUT = "timeout"
STATUS_SEND_FAILED = "send_failed"

MIN_INTERVAL_HOURS = 12
MAX_INTERVAL_HOURS = 336
DEFAULT_INTERVAL_HOURS = 24

MIN_DISCOVER_WINDOW = 5.0
MIN_CYCLE_TIMEOUT = 10.0
MIN_COMMAND_TIMEOUT = 1.0
LIBRARY_MSG_SENT_TIMEOUT = 15.0

NEIGHBORS_JSON_BUDGET = 10240

DISCOVER_FILTER_REPEATER = 1 << ADV_TYPE_REPEATER


def clamp_interval_hours(hours: int) -> int:
    """Clamp to the firmware 12-336h band, falling back to 24h default."""
    if hours <= 0:
        return DEFAULT_INTERVAL_HOURS
    return max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, hours))


@dataclass
class NeighborsConfig:
    interval_hours: int = DEFAULT_INTERVAL_HOURS
    discover_window: float = 60.0
    command_timeout: float = 20.0
    scope_timeout: float = 0.0
    scope_min_timeout: float = 8.0
    scope_gap: float = 2.0
    cycle_timeout: float = 600.0
    max_neighbors: int = 32
    self_scopes: str = ""

    def __post_init__(self):
        self.interval_hours = clamp_interval_hours(self.interval_hours)
        if self.discover_window < MIN_DISCOVER_WINDOW:
            self.discover_window = MIN_DISCOVER_WINDOW
        if self.max_neighbors < 1:
            self.max_neighbors = 1
        if self.cycle_timeout < MIN_CYCLE_TIMEOUT:
            self.cycle_timeout = MIN_CYCLE_TIMEOUT
        if self.scope_gap < 0:
            self.scope_gap = 0.0
        if self.command_timeout < MIN_COMMAND_TIMEOUT:
            self.command_timeout = MIN_COMMAND_TIMEOUT

    @property
    def scope_request_budget(self) -> float:
        wait = self.scope_timeout if self.scope_timeout > 0 else self.scope_min_timeout
        return LIBRARY_MSG_SENT_TIMEOUT + max(wait, self.scope_min_timeout) + self.command_timeout

    @property
    def interval_seconds(self) -> float:
        return self.interval_hours * 3600.0


@dataclass
class NeighborEntry:
    pubkey: str
    snr: float
    heard_at: float
    scopes: str = ""
    status: str = STATUS_TIMEOUT

    def heard_secs_ago(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        return max(0, int(now - self.heard_at))


def sort_key(entry: NeighborEntry, now: Optional[float] = None) -> tuple:
    return (entry.heard_secs_ago(now), -entry.snr, entry.pubkey.lower())


def sort_entries(entries: List[NeighborEntry], now: Optional[float] = None) -> List[NeighborEntry]:
    return sorted(entries, key=lambda e: sort_key(e, now))


async def discover_neighbors(
    meshcore,
    cfg: NeighborsConfig,
    self_pubkey: Optional[str],
    log,
    *,
    debug: bool = False,
    still_valid=None,
    command_lock=None,
) -> Optional[List[NeighborEntry]]:
    collected: Dict[str, NeighborEntry] = {}
    self_key = (self_pubkey or "").lower()
    closed = False

    tag = random.randint(1, 0xFFFFFFFF)
    expected_tag = tag.to_bytes(4, "little").hex()

    async def on_discover_response(event):
        if closed:
            return
        payload = getattr(event, "payload", None) or {}
        pubkey = str(payload.get("pubkey", "")).lower()
        if len(pubkey) != 64:
            return
        if self_key and pubkey == self_key:
            return
        if payload.get("node_type") != ADV_TYPE_REPEATER:
            return
        if str(payload.get("tag", "")).lower() != expected_tag:
            return

        snr = float(payload.get("SNR", 0) or 0)
        existing = collected.get(pubkey)
        if existing is None:
            collected[pubkey] = NeighborEntry(pubkey=pubkey, snr=snr, heard_at=time.time())
        else:
            existing.heard_at = time.time()
            existing.snr = max(existing.snr, snr)

    subscription = meshcore.subscribe(EventType.DISCOVER_RESPONSE, on_discover_response)
    try:
        try:
            async with _maybe_lock(command_lock):
                result = await asyncio.wait_for(
                    meshcore.commands.send_node_discover_req(
                        DISCOVER_FILTER_REPEATER,
                        prefix_only=False,
                        tag=tag,
                    ),
                    timeout=cfg.command_timeout,
                )
        except asyncio.TimeoutError:
            log.warning(f"Neighbors: node-discover request did not complete within {cfg.command_timeout:.0f}s")
            return None

        if result is None or getattr(result, "type", None) == EventType.ERROR:
            reason = (getattr(result, "payload", None) or {}).get("reason", "") if result else ""
            log.debug(f"Neighbors: node-discover request failed ({reason})")
            return None

        if debug:
            log.debug(f"Neighbors: node-discover sent (tag={expected_tag}), collecting for {cfg.discover_window:.0f}s")
        await asyncio.sleep(cfg.discover_window)

        if still_valid is not None and not still_valid():
            log.warning("Neighbors: session invalidated during discovery window, abandoning cycle")
            return None
    finally:
        closed = True
        try:
            meshcore.unsubscribe(subscription)
        except Exception as exc:
            log.debug(f"Neighbors: error unsubscribing discover handler: {exc}")

    return sort_entries(list(collected.values()))


async def collect_scopes(
    meshcore,
    entries: List[NeighborEntry],
    cfg: NeighborsConfig,
    log,
    *,
    debug: bool = False,
    command_lock=None,
) -> None:
    if not entries:
        return

    deadline = time.time() + cfg.cycle_timeout
    timeout = cfg.scope_timeout if cfg.scope_timeout > 0 else 0

    for index, entry in enumerate(entries):
        if time.time() >= deadline:
            dropped = len(entries) - index
            log.warning(f"Neighbors: cycle budget ({cfg.cycle_timeout:.0f}s) reached, {dropped} neighbors left unqueried")
            break

        if index > 0 and cfg.scope_gap > 0:
            await asyncio.sleep(cfg.scope_gap)

        try:
            async with _maybe_lock(command_lock):
                scopes = await asyncio.wait_for(
                    meshcore.commands.req_regions_sync(
                        entry.pubkey,
                        timeout=timeout,
                        min_timeout=cfg.scope_min_timeout,
                    ),
                    timeout=cfg.scope_request_budget,
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            entry.status = STATUS_SEND_FAILED
            log.warning(f"Neighbors: scope request to {entry.pubkey[:12]} exceeded budget")
            continue
        except Exception as exc:
            entry.status = STATUS_SEND_FAILED
            log.debug(f"Neighbors: scope request to {entry.pubkey[:12]} failed: {exc}")
            continue

        if scopes is None:
            entry.status = STATUS_TIMEOUT
            if debug:
                log.debug(f"Neighbors: no scope response from {entry.pubkey[:12]}")
            continue

        entry.scopes = str(scopes).strip()
        entry.status = STATUS_RESPONDED
        if debug:
            log.debug(f"Neighbors: {entry.pubkey[:12]} scopes={entry.scopes or '(none)'}")


async def fetch_self_scopes(meshcore, cfg: NeighborsConfig, log, command_lock=None) -> str:
    if cfg.self_scopes:
        return cfg.self_scopes

    commands = getattr(meshcore, "commands", None)
    getter = getattr(commands, "get_default_flood_scope", None)
    if not callable(getter):
        return ""

    try:
        async with _maybe_lock(command_lock):
            result = await asyncio.wait_for(getter(), timeout=cfg.command_timeout)
    except Exception as exc:
        log.debug(f"Neighbors: could not read default flood scope: {exc}")
        return ""

    if result is None or getattr(result, "type", None) == EventType.ERROR:
        return ""
    return str((getattr(result, "payload", None) or {}).get("scope_name", "") or "").strip()


def build_neighbors_message(
    origin: str,
    origin_id: str,
    self_scopes: str,
    entries: List[NeighborEntry],
    *,
    timestamp: Optional[str] = None,
    now: Optional[float] = None,
    budget: int = NEIGHBORS_JSON_BUDGET,
    total_neighbors: Optional[int] = None,
) -> Tuple[Dict[str, Any], int]:
    now = time.time() if now is None else now
    queried = len(entries)
    total = queried if total_neighbors is None else total_neighbors
    message: Dict[str, Any] = {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "origin_id": origin_id,
        "total_neighbors": total,
        "queried_neighbors": queried,
        "truncated": total > queried,
        "self": {"scopes": self_scopes or ""},
        "neighbors": [],
    }

    neighbors = message["neighbors"]
    dropped = 0
    for position, entry in enumerate(sort_entries(entries, now)):
        neighbors.append(
            {
                "pubkey": entry.pubkey.upper(),
                "snr": entry.snr,
                "heard_secs_ago": entry.heard_secs_ago(now),
                "scopes": entry.scopes or "",
                "status": entry.status,
            }
        )
        if len(json.dumps(message)) >= budget:
            neighbors.pop()
            dropped = len(entries) - position
            break

    message["truncated"] = bool(dropped) or total > queried
    return message, dropped


def normalize_packet_stats(payload: dict) -> dict:
    """Add stable packet counter aliases to MeshCore packet stats."""
    normalized = dict(payload)
    if "sent" in normalized and "packets_sent" not in normalized:
        normalized["packets_sent"] = normalized["sent"]
    if "recv" in normalized and "packets_received" not in normalized:
        normalized["packets_received"] = normalized["recv"]
    return normalized


# ==============================================================================
# MeshHub Packet Capture Module
# ==============================================================================

class Mqtt:
    def __init__(self):
        self.name = "mqtt"
        self.api = None
        self.config = {}
        
        # Schema for validation of configuration in config.json under modules.mqtt
        self.config_schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "output_file": {"type": "string"},
                "verbose": {"type": "boolean"},
                "debug": {"type": "boolean"},
                "iata": {"type": "string"},
                "decode_payloads": {"type": "boolean"},
                "decode_include_public": {"type": "boolean"},
                "decode_hashtag_channels": {"type": "array", "items": {"type": "string"}},
                "decode_channel_keys": {"type": "object"},
                "neighbors_interval_hours": {"type": "integer", "minimum": 1, "maximum": 336},
                "neighbors_self_scopes": {"type": "string"},
                "send_raw": {"type": "boolean"},
                "send_neighbors": {"type": "boolean"},
                "token_owner": {"type": "string"},
                "token_email": {"type": "string"},
                "brokers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "server": {"type": "string"},
                            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                            "topic_status": {"type": "string"},
                            "topic_packets": {"type": "string"},
                            "topic_raw": {"type": "string"},
                            "topic_neighbors": {"type": "string"},
                            "use_tls": {"type": "boolean"},
                            "tls_verify": {"type": "boolean"},
                            "use_ws": {"type": "boolean"},
                            "websocket_path": {"type": "string"},
                            "token_private_key": {"type": "string"},
                            "token_audience": {"type": "string"},
                            "token_owner": {"type": "string"},
                            "token_email": {"type": "string"},
                            "token_ttl": {"type": "integer"},
                            "neighbors": {"type": "boolean"},
                            "send_neighbors": {"type": "boolean"},
                            "send_raw": {"type": "boolean"},
                            "raw": {"type": "boolean"},
                            "include_decoded": {"type": "boolean"},
                            "iata": {"type": "string"},
                            "qos": {"type": "integer", "minimum": 0, "maximum": 2},
                            "retain": {"type": "boolean"}
                        },
                        "required": ["server", "port"]
                    }
                }
            },
            "required": ["enabled"]
        }
        
        self.mqtt_clients = []
        self.mqtt_connected = {}
        self.jwt_tokens = {}
        
        self.rf_data_cache = {}
        self.recent_rf_packets = {}
        self.raw_duplicate_window = 2.0
        self.rf_data_timeout = 15.0
        self.packet_count = 0
        
        # Device details
        self.device_name = None
        self.device_public_key = None
        self.device_private_key = None
        
        # Local packet output handle
        self.output_handle = None
        
        # Payload decoding
        self.decode_payloads = True
        self.channel_key_store = None
        
        # Neighbors
        self.neighbors_config = NeighborsConfig()
        self.last_neighbors_publish = 0.0
        self.neighbors_capability_state = None
        self.neighbors_discover_failures = 0
        self._neighbors_task = None
        
        # Scheduled tasks
        self.unschedule_jwt = None
        self.unschedule_status = None
        
        # Event subscriptions
        self.unsubscribe_rx_log = None
        self.unsubscribe_raw = None
        self.unsubscribe_connect = None
        self.unsubscribe_advert = None
        self.unsubscribe_path_update = None
        self.unsubscribe_new_contact = None

    def run_config(self):
        """CLI module setup wizard allowing interactive configuration of community presets and brokers."""
        config_path = Path("config/config.json")
        data = {}
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
                
        modules = data.setdefault("modules", {})
        config = modules.setdefault("mqtt", {})
        
        print("\n================================================================================")
        print("                  MeshCore Packet Capture (MQTT) Configuration                  ")
        print("================================================================================")
        
        enabled_val = config.get("enabled", True)
        val = input(f"Enable Packet Capture Module? (y/n) [current: {'y' if enabled_val else 'n'}]: ").strip().lower()
        if val:
            config["enabled"] = val in ("y", "yes", "true")
            
        current_iata = config.get("iata", "LOC")
        val = input(f"Global IATA Regional Code (e.g. ORD, DFW, JFK) [current: {current_iata}]: ").strip().upper()
        if val:
            config["iata"] = val
            
        current_output = config.get("output_file", "")
        val = input(f"Local file path to write captured packets JSON (empty to disable) [current: {current_output}]: ").strip()
        if val is not None:
            config["output_file"] = val

        send_raw_val = config.get("send_raw", True)
        val = input(f"Publish dedicated raw packet topic by default? (y/n) [current: {'y' if send_raw_val else 'n'}]: ").strip().lower()
        if val:
            config["send_raw"] = val in ("y", "yes", "true")
        else:
            config.setdefault("send_raw", True)

        send_neigh_val = config.get("send_neighbors", True)
        val = input(f"Publish neighbor discovery snapshots by default? (y/n) [current: {'y' if send_neigh_val else 'n'}]: ").strip().lower()
        if val:
            config["send_neighbors"] = val in ("y", "yes", "true")
        else:
            config.setdefault("send_neighbors", True)

        current_brokers = config.get("brokers", [])
        
        # Full 24 Community presets reflecting upstream repository updates
        presets = [
            # Mapping & Global Platforms
            {"name": "Let's Mesh US Server", "server": "mqtt-us-v1.letsmesh.net", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt-us-v1.letsmesh.net"},
            {"name": "Let's Mesh EU Server", "server": "mqtt-eu-v1.letsmesh.net", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt-eu-v1.letsmesh.net"},
            {"name": "MeshMapper", "server": "mqtt.meshmapper.net", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt.meshmapper.net"},
            {"name": "Waev", "server": "mqtt.waev.app", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt.waev.app"},
            {"name": "Meshomatic", "server": "us-east.meshomatic.net", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "us-east.meshomatic.net", "websocket_path": "/mqtt"},
            {"name": "MeshRank", "server": "meshrank.net", "port": 8883, "use_tls": True, "use_ws": False},
            
            # US Northeast & Midwest Communities
            {"name": "Greater Boston Mesh", "server": "mqttmc01.bostonme.sh", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqttmc01.bostonme.sh"},
            {"name": "Chicago Mesh (ChiMesh)", "server": "mqtt.chimesh.org", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt.chimesh.org", "neighbors": True},
            {"name": "Connecticut Mesh (CTMesh)", "server": "mqtt.ctmesh.org", "port": 1883, "use_tls": False, "use_ws": False},
            {"name": "East Idaho Mesh", "server": "broker.eastidahomesh.net", "port": 443, "use_tls": True, "use_ws": True},
            {"name": "Inland Northwest Mesh (INWMesh)", "server": "scope.inwmesh.org", "port": 8883, "use_tls": True, "use_ws": False},
            {"name": "Nashville Mesh (NashMesh)", "server": "mqtt.nashme.sh", "port": 1883, "use_tls": False, "use_ws": False},
            {"name": "Tennessee Mesh (TennMesh)", "server": "mqtt.tennmesh.com", "port": 1883, "use_tls": False, "use_ws": False},
            
            # US South & West Communities
            {"name": "North Texas Mesh (NTX)", "server": "ntxmesh.dhovin.me", "port": 8883, "use_tls": True, "use_ws": True, "token_audience": "ntxmesh.dhovin.me"},
            {"name": "Colorado Mesh", "server": "mqtt.meshcore.coloradomesh.org", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt.meshcore.coloradomesh.org"},
            {"name": "Cascadia Mesh", "server": "mqtt-v1.cascadiamesh.org", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt-v1.cascadiamesh.org"},
            {"name": "Florida Mesh (FLMesh)", "server": "mcmqtt.jntconnections.com", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mcmqtt.jntconnections.com"},
            
            # International Communities
            {"name": "MeshCore Canada 1", "server": "mqtt1.meshcore.ca", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt1.meshcore.ca"},
            {"name": "MeshCore Canada 2", "server": "mqtt2.meshcore.ca", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt2.meshcore.ca"},
            {"name": "CzechMesh 1 (CZ)", "server": "mqtt1.meshcore.cz", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt1.meshcore.cz"},
            {"name": "CzechMesh 2 (CZ)", "server": "mqtt2.meshcore.website", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "mqtt2.meshcore.website"},
            {"name": "BSMesh (Germany)", "server": "mqtt.bsmesh.de", "port": 8885, "use_tls": True, "use_ws": True, "token_audience": "mqtt.bsmesh.de"},
            {"name": "MeshAt Sweden", "server": "meshcore-mqtt.meshat.se", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "meshcore-mqtt.meshat.se"},
            {"name": "NZ Analyzer (New Zealand)", "server": "meshcore-mqtt-1.baird.io", "port": 443, "use_tls": True, "use_ws": True, "token_audience": "meshcore-mqtt-1.baird.io"}
        ]

        print("\n--- Preset community MQTT brokers available ---")
        categories = [
            ("Mapping & Global Platforms", [0, 1, 2, 3, 4, 5]),
            ("US Northeast & Midwest Communities", [6, 7, 8, 9, 10, 11, 12]),
            ("US South & West Communities", [13, 14, 15, 16]),
            ("International Communities", [17, 18, 19, 20, 21, 22, 23])
        ]

        for cat_name, idxs in categories:
            print(f"\n{cat_name}:")
            for idx in idxs:
                preset = presets[idx]
                server = preset["server"]
                is_active = any(b.get("server", "").lower() == server.lower() for b in current_brokers)
                status_str = " [Already Added]" if is_active else ""
                
                features = []
                if preset.get("use_tls"):
                    features.append("TLS")
                if preset.get("use_ws"):
                    features.append("WS")
                if preset.get("neighbors"):
                    features.append("Neighbors")
                features_str = f" ({', '.join(features)})" if features else ""
                
                print(f"  [{idx + 1}] {preset['name']} ({server}:{preset['port']}{features_str}){status_str}")

        print("\n--------------------------------------------------------------------------------")
        val = input("Enter comma-separated numbers to add (e.g. 1,4,14), 'all' to add all, or 'none' to skip: ").strip().lower()
        
        selected_indices = []
        if val == "all":
            selected_indices = list(range(len(presets)))
        elif val and val not in ("none", "skip"):
            parts = val.split(",")
            for p in parts:
                p = p.strip()
                if p.isdigit():
                    idx = int(p) - 1
                    if 0 <= idx < len(presets):
                        selected_indices.append(idx)

        added_count = 0
        for idx in selected_indices:
            preset = presets[idx]
            server = preset["server"]
            is_active = any(b.get("server", "").lower() == server.lower() for b in current_brokers)
            if not is_active:
                broker_config = {
                    "server": preset["server"],
                    "port": preset["port"]
                }
                if preset.get("use_tls") is not None:
                    broker_config["use_tls"] = preset["use_tls"]
                if preset.get("use_ws") is not None:
                    broker_config["use_ws"] = preset["use_ws"]
                if preset.get("websocket_path") is not None:
                    broker_config["websocket_path"] = preset["websocket_path"]
                if preset.get("token_audience") is not None:
                    broker_config["token_audience"] = preset["token_audience"]
                if preset.get("neighbors") is not None:
                    broker_config["neighbors"] = preset["neighbors"]
                
                current_brokers.append(broker_config)
                added_count += 1
                
        if added_count > 0:
            config["brokers"] = current_brokers
            print(f"Added {added_count} community broker preset(s).")

        # Additional Custom Brokers loop
        while True:
            if not current_brokers:
                add_broker = input("\nNo MQTT Brokers configured. Add a custom MQTT Broker config? (y/n) [n]: ").strip().lower()
            else:
                print(f"\nCurrently configured brokers: {len(current_brokers)}")
                add_broker = input("Add an additional custom MQTT Broker config? (y/n) [n]: ").strip().lower()

            if add_broker not in ("y", "yes", "true"):
                break
                
            broker = {}
            broker["server"] = input("Broker IP or Hostname: ").strip()
            port_val = input("Port [1883]: ").strip()
            broker["port"] = int(port_val) if port_val.isdigit() else 1883
            
            auth_type = input("Authentication type (none/token/userpass) [none]: ").strip().lower()
            if auth_type in ("userpass", "user", "up"):
                broker["username"] = input("Username: ").strip()
                broker["password"] = input("Password: ").strip()
            elif auth_type == "token":
                aud_val = input("JWT Audience (e.g. broker domain) [leave empty to default to server]: ").strip()
                broker["token_audience"] = aud_val if aud_val else broker["server"]
                pk_val = input("Token Private Key (hex) [empty to use on-device signing]: ").strip()
                if pk_val:
                    broker["token_private_key"] = pk_val
                    
            use_tls_val = input("Use TLS? (y/n) [n]: ").strip().lower()
            broker["use_tls"] = use_tls_val == "y"
            
            use_ws_val = input("Use WebSockets? (y/n) [n]: ").strip().lower()
            if use_ws_val in ("y", "yes", "true"):
                broker["use_ws"] = True
                ws_path_val = input("WebSocket Path [/]: ").strip()
                if ws_path_val:
                    broker["websocket_path"] = ws_path_val
            
            raw_val = input("Publish dedicated raw packet topic to this broker? (y/n) [y]: ").strip().lower()
            if raw_val in ("n", "no", "false"):
                broker["send_raw"] = False

            neighbors_val = input("Publish Neighbor Discovery to this broker? (y/n) [y]: ").strip().lower()
            if neighbors_val in ("n", "no", "false"):
                broker["neighbors"] = False
            else:
                broker["neighbors"] = True
            
            is_dup = any(b.get("server", "").lower() == broker["server"].lower() and b.get("port") == broker["port"] for b in current_brokers)
            if is_dup:
                print(f"Broker with server '{broker['server']}' and port {broker['port']} is already configured.")
                continue
                
            current_brokers.append(broker)
            config["brokers"] = current_brokers
            print(f"Added custom broker: {broker['server']}:{broker['port']}")
                
        return config

    def init(self, api, config):
        """Lifecycle hook: save API and config, initialize key store and neighbors config."""
        self.api = api
        self.config = config
        self.verbose = config.get("verbose", False)
        self.debug = config.get("debug", False)
        
        output_file = config.get("output_file", "")
        if output_file:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
                self.output_handle = open(output_file, 'a', encoding='utf-8')
                logger.info(f"[{self.name}] Output file opened: {output_file}")
            except Exception as e:
                logger.error(f"[{self.name}] Failed to open output file '{output_file}': {e}")

        # Initialize payload decoding
        self.decode_payloads = config.get("decode_payloads", True)
        self.channel_key_store = ChannelKeyStore()
        if config.get("decode_include_public", True):
            self.channel_key_store.add_secret(DEFAULT_PUBLIC_CHANNEL_KEY, "public")

        for ch in config.get("decode_hashtag_channels", []):
            if isinstance(ch, str) and ch.strip():
                self.channel_key_store.add_hashtag(ch.strip())

        keys_dict = config.get("decode_channel_keys", {})
        if isinstance(keys_dict, dict):
            for name, key_hex in keys_dict.items():
                if isinstance(key_hex, str) and key_hex.strip():
                    self.channel_key_store.add_hex(key_hex.strip(), str(name))

        logger.info(f"[{self.name}] Payload decoding configured with {len(self.channel_key_store)} key(s)")

        # Initialize neighbors config
        interval_hours = config.get("neighbors_interval_hours", DEFAULT_INTERVAL_HOURS)
        self.neighbors_config = NeighborsConfig(
            interval_hours=interval_hours,
            self_scopes=config.get("neighbors_self_scopes", "")
        )

    async def start(self):
        """Lifecycle hook: subscribe to event bus, connect MQTT, and start background tasks."""
        logger.info(f"[{self.name}] Starting packet capture module...")
        
        self.unsubscribe_rx_log = self.api.subscribe("rx_log_data", self._on_rx_log_data)
        self.unsubscribe_raw = self.api.subscribe("raw_data", self._on_raw_data)
        self.unsubscribe_connect = self.api.subscribe("connect", self._on_connect)
        self.unsubscribe_advert = self.api.subscribe("advert", self._on_advert)
        self.unsubscribe_path_update = self.api.subscribe("path_update", self._on_path_update)
        self.unsubscribe_new_contact = self.api.subscribe("new_contact", self._on_new_contact)
        
        await self._sync_device_info()
        
        if MQTT_AVAILABLE:
            await self._connect_mqtt()
            
        self.unschedule_jwt = self.api.schedule_task("*/10 * * * *", self._periodic_jwt_check)
        self.unschedule_status = self.api.schedule_task("*/5 * * * *", self._periodic_status)
        
        # Start background neighbors scheduler task
        self._neighbors_task = asyncio.create_task(self._neighbors_scheduler())
        
        logger.info(f"[{self.name}] Started, background tasks active.")

    def stop(self):
        """Lifecycle hook: stop background tasks, shut down MQTT loops, close handles."""
        logger.info(f"[{self.name}] Stopping packet capture module...")
        
        if self._neighbors_task and not self._neighbors_task.done():
            self._neighbors_task.cancel()
        
        if self.unsubscribe_rx_log: self.unsubscribe_rx_log()
        if self.unsubscribe_raw: self.unsubscribe_raw()
        if self.unsubscribe_connect: self.unsubscribe_connect()
        if self.unsubscribe_advert: self.unsubscribe_advert()
        if self.unsubscribe_path_update: self.unsubscribe_path_update()
        if self.unsubscribe_new_contact: self.unsubscribe_new_contact()
        
        if self.unschedule_jwt: self.unschedule_jwt()
        if self.unschedule_status: self.unschedule_status()
        
        for client_info in self.mqtt_clients:
            broker_num = client_info["broker_num"]
            client = client_info.get("client")
            if client:
                try:
                    logger.info(f"[{self.name}] Disconnecting MQTT broker {broker_num}...")
                    status_topic = self.get_topic("status", broker_num)
                    if status_topic and self.mqtt_connected.get(broker_num, False):
                        payload = json.dumps({
                            "status": "offline",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "origin": self.device_name or "MeshCore Device",
                            "origin_id": self.device_public_key.upper() if self.device_public_key else 'DEVICE'
                        })
                        client.publish(status_topic, payload, qos=0, retain=True)
                    client.disconnect()
                    client.loop_stop()
                except Exception as e:
                    logger.warning(f"[{self.name}] Error disconnecting broker {broker_num}: {e}")
                    
        self.mqtt_clients.clear()
        self.mqtt_connected.clear()
        
        if self.output_handle:
            try:
                self.output_handle.close()
                self.output_handle = None
            except Exception:
                pass
                
        logger.info(f"[{self.name}] Stopped cleanly.")

    # ==============================================================================
    # Event Handlers
    # ==============================================================================

    async def _on_connect(self, *args, **kwargs):
        logger.info(f"[{self.name}] Hardware connected. Syncing info...")
        await self._sync_device_info_and_reconnect_mqtt()

    async def _on_rx_log_data(self, data):
        try:
            if not isinstance(data, dict):
                return
            snr = data.get("snr")
            rssi = data.get("rssi")
            raw_hex = None
            if data.get("payload"):
                raw_hex = data["payload"]
            elif data.get("raw_hex"):
                raw_hex = data["raw_hex"]
                if len(raw_hex) >= 4:
                    raw_hex = raw_hex[4:]  # Skip first 2 bytes (SNR and RSSI)

            if not raw_hex:
                return

            if raw_hex.startswith("0x") or raw_hex.startswith("0X"):
                raw_hex = raw_hex[2:]

            raw_hex = raw_hex.upper()
            packet_prefix = raw_hex[:32]
            now = time.time()

            rf_data = {
                "snr": snr,
                "rssi": rssi,
                "timestamp": now,
                "raw_hex": raw_hex,
                "payload_length": data.get("payload_length", len(raw_hex) // 2)
            }
            self.rf_data_cache[packet_prefix] = rf_data

            # Clean up old RF cache entries
            self.rf_data_cache = {
                k: v for k, v in self.rf_data_cache.items()
                if now - v["timestamp"] < self.rf_data_timeout
            }

            # Deduplication: record in recent_rf_packets so subsequent RAW_DATA is skipped
            self.recent_rf_packets[raw_hex] = now
            self.recent_rf_packets = {
                k: v for k, v in self.recent_rf_packets.items()
                if now - v < self.raw_duplicate_window
            }

            # Directly format and publish the RF packet
            formatted = self._format_packet_data(raw_hex, rf_data)
            self._output_packet(formatted)
        except Exception as e:
            logger.error(f"[{self.name}] Error in _on_rx_log_data: {e}", exc_info=True)

    async def _on_raw_data(self, data):
        try:
            if not isinstance(data, dict):
                return
            raw_hex = data.get("data") or data.get("raw_hex") or data.get("payload") or data.get("raw")
            if not raw_hex:
                return

            if raw_hex.startswith("0x") or raw_hex.startswith("0X"):
                raw_hex = raw_hex[2:]

            raw_hex = raw_hex.upper()
            now = time.time()

            # Deduplication: skip if already processed from RX_LOG_DATA
            recent_time = self.recent_rf_packets.get(raw_hex)
            if recent_time is not None and (now - recent_time) < self.raw_duplicate_window:
                if self.debug:
                    logger.debug(f"[{self.name}] Skipping RAW_DATA already processed from RX_LOG_DATA")
                return

            self.recent_rf_packets = {
                k: v for k, v in self.recent_rf_packets.items()
                if now - v < self.raw_duplicate_window
            }

            packet_prefix = raw_hex[:32]
            rf_data = self.rf_data_cache.get(packet_prefix)

            formatted = self._format_packet_data(raw_hex, rf_data)
            self._output_packet(formatted)
        except Exception as e:
            logger.error(f"[{self.name}] Error in _on_raw_data: {e}", exc_info=True)

    async def _on_advert(self, data):
        if self.verbose:
            logger.debug(f"[{self.name}] Advert event: {data}")

    async def _on_path_update(self, data):
        if self.verbose:
            logger.debug(f"[{self.name}] Path update: {data}")

    async def _on_new_contact(self, data):
        if self.verbose:
            logger.debug(f"[{self.name}] New contact discovered: {data}")

    def _output_packet(self, packet_data: dict):
        self.packet_count += 1
        route = packet_data.get("route", "")
        ptype = packet_data.get("packet_type", "")
        logger.info(f"📦 [{self.name}] Captured packet #{self.packet_count}: Route={route} Type={ptype} Len={packet_data.get('len')} SNR={packet_data.get('SNR')} RSSI={packet_data.get('RSSI')} Hash={packet_data.get('hash')}")
            
        if self.output_handle:
            try:
                self.output_handle.write(json.dumps(packet_data) + "\n")
                self.output_handle.flush()
            except Exception as e:
                logger.error(f"[{self.name}] Failed writing to output file: {e}")
                
        self._publish_packet_to_mqtt(packet_data)

    # ==============================================================================
    # Packet Decoding & Formatting
    # ==============================================================================

    def _format_packet_data(self, raw_hex: str, rf_data: Optional[dict] = None) -> dict:
        current_time = datetime.now(timezone.utc)
        timestamp = current_time.isoformat()
        
        decoded = self._decode_packet(raw_hex)
        packet_len = len(raw_hex) // 2
        
        route = "U"
        packet_type = "0"
        payload_len = "0"
        
        firmware_payload_len = rf_data.get("payload_length") if rf_data else None
        
        if decoded:
            route_map = {
                "TRANSPORT_FLOOD": "F",
                "FLOOD": "F",
                "DIRECT": "D",
                "TRANSPORT_DIRECT": "T"
            }
            route = route_map.get(decoded.get("route_type", ""), "U")
            
            payload_type_map = {
                "REQ": "0", "RESPONSE": "1", "TXT_MSG": "2", "ACK": "3",
                "ADVERT": "4", "GRP_TXT": "5", "GRP_DATA": "6", "ANON_REQ": "7",
                "PATH": "8", "TRACE": "9", "MULTIPART": "10", "CONTROL": "11",
                "Type12": "12", "Type13": "13", "Type14": "14", "RAW_CUSTOM": "15"
            }
            packet_type = payload_type_map.get(decoded.get("payload_type", ""), "0")
            
            if firmware_payload_len is not None:
                payload_len = str(firmware_payload_len)
            else:
                path_len_bytes = decoded.get('path_byte_len')
                if path_len_bytes is None:
                    path_len_bytes = len(decoded.get('path', []))
                has_transport = decoded.get('route_type') in ['TRANSPORT_FLOOD', 'TRANSPORT_DIRECT']
                transport_bytes = 4 if has_transport else 0
                payload_len = str(max(0, packet_len - 1 - transport_bytes - 1 - path_len_bytes))
        else:
            payload_len = str(max(0, packet_len - 1))
            
        origin_id = (self.device_public_key.upper() if self.device_public_key and self.device_public_key != "Unknown" else "DEVICE")
        
        packet_data = {
            "origin": self.device_name or "MeshCore Device",
            "origin_id": origin_id,
            "timestamp": timestamp,
            "type": "PACKET",
            "direction": "rx",
            "time": current_time.strftime("%H:%M:%S"),
            "date": current_time.strftime("%d/%m/%Y"),
            "len": str(packet_len),
            "packet_type": packet_type,
            "route": route,
            "payload_len": payload_len,
            "raw": raw_hex.upper(),
            "SNR": str(rf_data.get('snr', 'Unknown')) if rf_data else "Unknown",
            "RSSI": str(rf_data.get('rssi', 'Unknown')) if rf_data else "Unknown",
            "hash": self._calculate_packet_hash(raw_hex, decoded.get('payload_type_value') if decoded else None)
        }
        
        if route == "D" and decoded and 'path' in decoded:
            packet_data["path"] = ",".join(decoded['path'])

        # Attach decoded application payload if enabled
        if self.decode_payloads and self.channel_key_store is not None and decoded:
            try:
                payload_type_val = decoded.get('payload_type_value', 0)
                payload_bytes = decoded.get('payload_bytes')
                if payload_bytes is None and decoded.get('payload_hex'):
                    payload_bytes = bytes.fromhex(decoded['payload_hex'])
                if payload_bytes is not None:
                    decoded_obj = decode_payload(int(payload_type_val), payload_bytes, self.channel_key_store)
                    if decoded.get('path') and "path" not in packet_data:
                        decoded_obj["path"] = list(decoded['path'])
                    packet_data["decoded"] = decoded_obj
            except Exception as e:
                if self.debug:
                    logger.debug(f"[{self.name}] Payload decode failed: {e}")
            
        return packet_data

    def _decode_packet(self, raw_hex: str) -> Optional[dict]:
        byte_data = bytes.fromhex(raw_hex)
        if len(byte_data) < 2:
            return None
            
        try:
            header = byte_data[0]
            route_type = RouteType(header & 0x03)
            has_transport = route_type in [RouteType.TRANSPORT_FLOOD, RouteType.TRANSPORT_DIRECT]
            
            offset = 5 if has_transport else 1
            if len(byte_data) <= offset:
                return None
                
            path_len_byte = byte_data[offset]
            offset += 1
            
            path_byte_len, path_hash_bytes = self._decode_packed_path_length(path_len_byte)
            if len(byte_data) < offset + path_byte_len:
                return None
                
            path_bytes = byte_data[offset:offset + path_byte_len]
            offset += path_byte_len
            
            payload = byte_data[offset:]
            payload_version = PayloadVersion((header >> 6) & 0x03)
            if payload_version != PayloadVersion.VER_1:
                return None
                
            payload_type = PayloadType((header >> 2) & 0x0F)
            path_values = self._split_path_hops(path_bytes, path_hash_bytes)
            
            message = {
                "payload_type": payload_type.name,
                "payload_type_value": payload_type.value,
                "payload_version": payload_version.name,
                "route_type": route_type.name,
                "path": path_values,
                "path_len_byte": path_len_byte,
                "path_byte_len": path_byte_len,
                "path_hash_bytes": path_hash_bytes,
                "payload_hex": payload.hex(),
                "payload_bytes": payload,
            }
            
            if payload_type is PayloadType.ADVERT:
                advert_data = self._parse_advert(payload)
                if advert_data.get("advert_parse_ok"):
                    message.update(advert_data)
                    
            return message
        except Exception:
            return None

    def _decode_packed_path_length(self, path_len_byte: int) -> tuple:
        path_byte_len = path_len_byte & 0x3F
        hash_size_bits = (path_len_byte >> 6) & 0x03
        hash_sizes = [1, 2, 4, 32]
        path_hash_bytes = hash_sizes[hash_size_bits]
        return path_byte_len, path_hash_bytes

    def _split_path_hops(self, path_bytes: bytes, hash_size: int) -> list:
        hops = []
        if hash_size <= 0 or not path_bytes:
            return hops
        for i in range(0, len(path_bytes), hash_size):
            chunk = path_bytes[i:i + hash_size]
            hops.append(chunk.hex())
        return hops

    def _calculate_packet_hash(self, raw_hex: str, payload_type_val: Optional[int] = None) -> str:
        try:
            byte_data = bytes.fromhex(raw_hex)
            header = byte_data[0]
            if payload_type_val is None:
                payload_type_val = (header >> 2) & 0x0F
                
            route_type = header & 0x03
            has_transport = route_type in [0x00, 0x03]
            offset = 5 if has_transport else 1
            
            if len(byte_data) <= offset:
                return "0000000000000000"
                
            path_len_byte = byte_data[offset]
            offset += 1
            
            path_byte_len, _ = self._decode_packed_path_length(path_len_byte)
            payload_start = offset + path_byte_len
            if payload_start > len(byte_data):
                return "0000000000000000"
                
            payload_data = byte_data[payload_start:]
            
            hash_obj = hashlib.sha256()
            hash_obj.update(bytes([payload_type_val]))
            if payload_type_val == 9:  # TRACE
                hash_obj.update(path_len_byte.to_bytes(2, byteorder='little'))
            hash_obj.update(payload_data)
            
            return hash_obj.hexdigest()[:16].upper()
        except Exception:
            return "0000000000000000"

    def _parse_advert(self, payload: bytes) -> dict:
        return parse_advert(payload)

    # ==============================================================================
    # Neighbors Discovery & Publishing
    # ==============================================================================

    def _broker_wants_raw(self, broker_num: int) -> bool:
        """Check if raw packet publishing is enabled for this broker (default: True)."""
        client_info = self._get_client_info(broker_num)
        if not client_info:
            return False
        b_cfg = client_info["config"]
        if "send_raw" in b_cfg:
            return bool(b_cfg["send_raw"])
        if "raw" in b_cfg:
            return bool(b_cfg["raw"])
        return self.config.get("send_raw", True)

    def _broker_wants_neighbors(self, broker_num: int) -> bool:
        """Check if neighbor snapshot publishing is enabled for this broker (default: True)."""
        client_info = self._get_client_info(broker_num)
        if not client_info:
            return False
        b_cfg = client_info["config"]
        if "send_neighbors" in b_cfg:
            return bool(b_cfg["send_neighbors"])
        if "neighbors" in b_cfg:
            return bool(b_cfg["neighbors"])
        return self.config.get("send_neighbors", True)

    def neighbors_broker_nums(self, warn_unroutable: bool = False) -> List[int]:
        """Return list of broker indices that have neighbors publishing enabled and a resolvable topic."""
        routable = []
        for c in self.mqtt_clients:
            b_num = c["broker_num"]
            if self._broker_wants_neighbors(b_num):
                topic = self.get_topic("neighbors", b_num)
                if topic:
                    routable.append(b_num)
                elif warn_unroutable:
                    logger.warning(f"[{self.name}] Broker {b_num} has neighbors enabled but topic could not resolve (missing IATA).")
        return routable

    def neighbors_commands_available(self) -> bool:
        """Check if the connected MeshCore hardware supports zero-hop node discover and anon regions."""
        if not self.api or not self.api.bot or not self.api.bot.connection_manager:
            return False
        mc = getattr(self.api.bot.connection_manager, "mc", None)
        if not mc or not hasattr(mc, "commands"):
            return False
            
        commands = mc.commands
        required = ["send_node_discover_req", "req_regions_sync"]
        available = all(callable(getattr(commands, attr, None)) for attr in required)
        
        state = "available" if available else "missing"
        if state != self.neighbors_capability_state:
            if available:
                logger.info(f"[{self.name}] MeshCore neighbors commands detected - neighbors publishing enabled.")
            else:
                logger.warning(f"[{self.name}] MeshCore neighbors commands not available on this radio/library.")
            self.neighbors_capability_state = state
        return available

    async def _run_neighbors_cycle(self) -> bool:
        """Run one zero-hop neighbor discover + scope collection pass and publish to opted-in brokers."""
        if not self.api.bot.connection_manager.isConnected:
            return False
            
        mc = getattr(self.api.bot.connection_manager, "mc", None)
        if not mc or not self.neighbors_commands_available():
            return False
            
        broker_nums = self.neighbors_broker_nums(warn_unroutable=True)
        if not broker_nums:
            if self.debug:
                logger.debug(f"[{self.name}] Neighbors cycle skipped: no connected broker has neighbors enabled.")
            return False

        logger.info(f"[{self.name}] Starting zero-hop neighbors discovery cycle...")
        cmd_lock = getattr(self.api.bot.connection_manager, "_cmd_lock", None)
        
        session = mc
        def session_intact():
            return getattr(self.api.bot.connection_manager, "mc", None) is session and self.api.bot.connection_manager.isConnected

        entries = await discover_neighbors(
            mc,
            self.neighbors_config,
            self.device_public_key,
            logger,
            debug=self.debug,
            still_valid=session_intact,
            command_lock=cmd_lock,
        )
        
        if entries is None:
            self.neighbors_discover_failures += 1
            if self.neighbors_discover_failures == 1:
                logger.warning(f"[{self.name}] Neighbors discovery request failed; will retry on configured interval.")
            return False
            
        self.neighbors_discover_failures = 0
        total_discovered = len(entries)
        
        if len(entries) > self.neighbors_config.max_neighbors:
            logger.info(f"[{self.name}] {len(entries)} discovered, querying {self.neighbors_config.max_neighbors} most useful.")
            entries = entries[:self.neighbors_config.max_neighbors]
            
        logger.info(f"[{self.name}] {len(entries)} neighbor(s) discovered, querying scopes...")
        await collect_scopes(
            mc,
            entries,
            self.neighbors_config,
            logger,
            debug=self.debug,
            command_lock=cmd_lock,
        )
        
        if not session_intact():
            logger.warning(f"[{self.name}] Device session reset during scope collection, discarding cycle.")
            return False
            
        self_scopes = await fetch_self_scopes(mc, self.neighbors_config, logger, command_lock=cmd_lock)
        origin_id = self.device_public_key.upper() if self.device_public_key and self.device_public_key != "Unknown" else "DEVICE"
        
        message, dropped = build_neighbors_message(
            self.device_name or "MeshCore Device",
            origin_id,
            self_scopes,
            entries,
            total_neighbors=total_discovered,
        )
        
        if dropped:
            logger.warning(f"[{self.name}] Neighbors payload budget reached: dropped {dropped} least-useful entry(ies).")
            
        payload_str = json.dumps(message)
        published_count = 0
        
        for b_num in broker_nums:
            c_info = self._get_client_info(b_num)
            if not c_info or not self.mqtt_connected.get(b_num, False):
                continue
            topic = self.get_topic("neighbors", b_num)
            if topic:
                try:
                    c_info["client"].publish(topic, payload_str, qos=0, retain=False)
                    published_count += 1
                    logger.info(f"📡 [{self.name}] Published {len(message['neighbors'])} neighbor(s) to broker {b_num} on {topic}")
                except Exception as e:
                    logger.error(f"[{self.name}] Failed publishing neighbors to broker {b_num}: {e}")
                    
        if published_count > 0:
            self.last_neighbors_publish = time.time()
            return True
        return False

    async def _neighbors_scheduler(self):
        """Background loop publishing neighbor snapshots on the configured interval."""
        interval_seconds = self.neighbors_config.interval_seconds
        logger.info(f"[{self.name}] Neighbors scheduler started ({self.neighbors_config.interval_hours}h interval).")
        
        # Initial small delay before first cycle on startup
        await asyncio.sleep(30)
        
        while True:
            try:
                now = time.time()
                time_since_last = now - self.last_neighbors_publish
                if time_since_last < interval_seconds:
                    sleep_time = interval_seconds - time_since_last
                    if self.debug:
                        logger.debug(f"[{self.name}] Next neighbors publish in {sleep_time/3600:.1f} hours.")
                    await asyncio.sleep(min(sleep_time, 300))
                    continue
                    
                published = await self._run_neighbors_cycle()
                if not published:
                    await asyncio.sleep(300)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.name}] Error in neighbors scheduler: {e}", exc_info=True)
                await asyncio.sleep(300)

    # ==============================================================================
    # Device State Synchronizer
    # ==============================================================================

    async def _sync_device_info(self):
        """Query ConnectionManager and state cache for details about the connected radio."""
        state = self.api.get_state()
        self.device_name = state.get("deviceName")
        self.device_public_key = state.get("publicKey")
        
        if not self.device_public_key or self.device_public_key == "Unknown":
            if self.api.bot.connection_manager.isConnected and self.api.bot.connection_manager.mc:
                mc = self.api.bot.connection_manager.mc
                if mc and mc.self_info:
                    self.device_name = mc.self_info.get("name")
                    self.device_public_key = mc.self_info.get("public_key")
                    if isinstance(self.device_public_key, bytes):
                        self.device_public_key = self.device_public_key.hex()
                        
        if self.device_public_key:
            self.device_public_key = self.device_public_key.upper()
            
        logger.info(f"[{self.name}] Synced device info: Name={self.device_name}, PubKey={self.device_public_key}")

    async def _sync_device_info_and_reconnect_mqtt(self):
        await self._sync_device_info()
        if MQTT_AVAILABLE:
            await self._connect_mqtt()

    # ==============================================================================
    # MQTT Connections & JWT renewal
    # ==============================================================================

    async def _connect_mqtt(self):
        # Clean up any existing brokers
        for client_info in self.mqtt_clients:
            client = client_info.get("client")
            if client:
                try:
                    client.disconnect()
                    client.loop_stop()
                except Exception:
                    pass
        self.mqtt_clients.clear()
        self.mqtt_connected.clear()

        brokers = self.config.get("brokers", [])
        if not brokers:
            logger.info(f"[{self.name}] No MQTT brokers configured.")
            return

        for idx, b_cfg in enumerate(brokers, 1):
            try:
                server = b_cfg.get("server")
                port = b_cfg.get("port", 1883)
                
                is_letsmesh = 'letsmesh.net' in server.lower() or 'letsmesh.net' in b_cfg.get("token_audience", "").lower()
                iata_code = b_cfg.get("iata") or self.config.get("iata", "LOC")
                if is_letsmesh and iata_code == "LOC":
                    logger.warning(f"[{self.name}] Let's Mesh broker requires a valid IATA regional code. Skipping broker {idx} ({server}).")
                    continue
                
                client_id = f"meshhub_{self.device_public_key or 'device'}"
                if idx > 1:
                    client_id += f"_{idx}"
                transport = "websockets" if b_cfg.get("use_ws", False) else "tcp"
                client = mqtt.Client(client_id=client_id, clean_session=True, transport=transport)
                client.reconnect_delay_set(min_delay=1, max_delay=120)
                client.user_data_set({"broker_num": idx})
                
                self.mqtt_clients.append({
                    "client": client,
                    "broker_num": idx,
                    "config": b_cfg
                })
                
                client.on_connect = self._on_mqtt_connect
                client.on_disconnect = self._on_mqtt_disconnect
                
                # Setup Last Will and Testament
                status_topic = self.get_topic("status", idx)
                if status_topic:
                    lwt_payload = json.dumps({
                        "status": "offline",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "origin": self.device_name or "MeshCore Device",
                        "origin_id": self.device_public_key.upper() if self.device_public_key else 'DEVICE'
                    })
                    client.will_set(status_topic, lwt_payload, qos=0, retain=True)

                # Setup authentication (Username/password or Token/JWT)
                token_aud = b_cfg.get("token_audience")
                if token_aud:
                    username = f"v1_{self.device_public_key.upper()}"
                    token = await self._generate_jwt(token_aud, idx, b_cfg)
                    if token:
                        client.username_pw_set(username, token)
                        logger.info(f"[{self.name}] Broker {idx}: Configured with JWT authentication.")
                    else:
                        logger.error(f"[{self.name}] Broker {idx}: Failed to generate JWT token. Skipping auth.")
                else:
                    uname = b_cfg.get("username")
                    pword = b_cfg.get("password")
                    if uname:
                        client.username_pw_set(uname, pword)

                # TLS Configuration
                if b_cfg.get("use_tls", False):
                    tls_verify = b_cfg.get("tls_verify", True)
                    if tls_verify:
                        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
                        client.tls_insecure_set(False)
                    else:
                        client.tls_set(cert_reqs=ssl.CERT_NONE)
                        client.tls_insecure_set(True)
                        logger.warning(f"[{self.name}] Broker {idx}: TLS verification disabled (insecure).")

                # WebSocket path configuration
                if transport == "websockets":
                    ws_path = b_cfg.get("websocket_path", "/")
                    client.ws_set_options(path=ws_path, headers=None)
                    keepalive = b_cfg.get("keepalive", 120)
                else:
                    keepalive = b_cfg.get("keepalive", 60)
                    
                client.connect(server, port, keepalive=keepalive)
                client.loop_start()
                
                logger.info(f"[{self.name}] Broker {idx}: Loop started for {server}:{port} (transport={transport})")
            except Exception as e:
                self.mqtt_clients = [c for c in self.mqtt_clients if c["broker_num"] != idx]
                logger.error(f"[{self.name}] Failed to connect to MQTT broker {idx}: {e}")

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        broker_num = userdata.get("broker_num", 1)
        if rc == 0:
            self.mqtt_connected[broker_num] = True
            logger.info(f"🟢 [{self.name}] Successfully connected to MQTT Broker {broker_num}.")
            if self.api and self.api.bot and self.api.bot.loop:
                asyncio.run_coroutine_threadsafe(self._publish_status_online(broker_num), self.api.bot.loop)
        else:
            self.mqtt_connected[broker_num] = False
            logger.error(f"🔴 [{self.name}] Connection to MQTT Broker {broker_num} failed with code {rc}.")

    def _on_mqtt_disconnect(self, client, userdata, rc):
        broker_num = userdata.get("broker_num", 1)
        self.mqtt_connected[broker_num] = False
        logger.warning(f"🟡 [{self.name}] Disconnected from MQTT Broker {broker_num} (code {rc}).")

    async def _publish_status_online(self, broker_num):
        status_topic = self.get_topic("status", broker_num)
        if not status_topic:
            return
            
        client_info = self._get_client_info(broker_num)
        if not client_info:
            return
        client = client_info["client"]
        
        state = self.api.get_state()
        
        freq = state.get("radio_freq") or 0
        bw = state.get("radio_bw") or 0
        sf = state.get("radio_sf") or 0
        cr = state.get("radio_cr") or 0
        radio_val = f"{freq},{bw},{sf},{cr}"
        
        status_payload = {
            "status": "online",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "origin": self.device_name or "MeshCore Device",
            "origin_id": self.device_public_key.upper() if self.device_public_key else 'DEVICE',
            "firmware": state.get("fwVersion", "unknown"),
            "firmware_version": state.get("fwVersion", "unknown"),
            "model": state.get("model", "unknown"),
            "battery": state.get("battery") if state.get("battery") is not None else 100,
            "neighbors": state.get("neighborCount", 0),
            "client": "meshhub",
            "client_version": "meshhub",
            "radio": radio_val,
            "sf": state.get("radio_sf"),
            "bw": state.get("radio_bw"),
            "cr": state.get("radio_cr"),
            "uptime": state.get("uptime"),
            "noise_floor": state.get("noise_floor")
        }
        
        stats_obj = {}
        if state.get("uptime_secs") is not None:
            stats_obj["uptime_secs"] = state.get("uptime_secs")
        if state.get("battery_mv") is not None:
            stats_obj["battery_mv"] = state.get("battery_mv")
        if state.get("errors") is not None:
            stats_obj["errors"] = state.get("errors")
        if state.get("queue_len") is not None:
            stats_obj["queue_len"] = state.get("queue_len")
        if state.get("noise_floor") is not None:
            stats_obj["noise_floor"] = state.get("noise_floor")
            
        # Query hardware stats if mc commands available
        mc = getattr(self.api.bot.connection_manager, "mc", None)
        if mc and hasattr(mc, "commands") and self.api.bot.connection_manager.isConnected:
            try:
                cmd_lock = getattr(self.api.bot.connection_manager, "_cmd_lock", None)
                async with _maybe_lock(cmd_lock):
                    if hasattr(mc.commands, "get_stats_core"):
                        res = await asyncio.wait_for(mc.commands.get_stats_core(), timeout=5.0)
                        if res and getattr(res, "type", None) != EventType.ERROR and res.payload:
                            stats_obj.update(res.payload)
                    if hasattr(mc.commands, "get_stats_packets"):
                        res_p = await asyncio.wait_for(mc.commands.get_stats_packets(), timeout=5.0)
                        if res_p and getattr(res_p, "type", None) != EventType.ERROR and res_p.payload:
                            stats_obj.update(normalize_packet_stats(res_p.payload))
            except Exception as e:
                logger.debug(f"[{self.name}] Error querying hardware stats: {e}")
            
        if stats_obj:
            status_payload["stats"] = stats_obj
        
        try:
            client.publish(status_topic, json.dumps(status_payload), qos=0, retain=True)
            logger.info(f"🟢 [{self.name}] Published online status to broker {broker_num} on topic {status_topic}.")
        except Exception as e:
            logger.error(f"[{self.name}] Error publishing online status to broker {broker_num}: {e}")

    async def _generate_jwt(self, audience: str, broker_num: int, b_cfg: dict) -> str:
        """Create signed JWT token using configuration private key, on-device signing, or fallback."""
        if not self.device_public_key:
            logger.warning(f"[{self.name}] Cannot generate JWT without device public key. Handshake incomplete?")
            return ""

        prv_key_hex = b_cfg.get("token_private_key") or self.device_private_key
        
        claims = {
            "aud": audience,
            "client": "MeshHub/packet-capture-module"
        }
        
        # Add owner public key claim if configured
        owner_pk = b_cfg.get("token_owner") or self.config.get("token_owner")
        if owner_pk and len(owner_pk.strip()) == 64:
            claims["owner"] = owner_pk.strip().upper()
            
        # Add email claim if configured
        email = b_cfg.get("token_email") or self.config.get("token_email")
        if email and re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email.strip()):
            claims["email"] = email.strip().lower()
            
        # Optional expiration
        exp = None
        ttl = b_cfg.get("token_ttl") or self.config.get("token_ttl")
        if ttl and int(ttl) > 0:
            exp = int(time.time()) + int(ttl)
        elif b_cfg.get("token_exp"):
            exp = b_cfg.get("token_exp")
        
        payload = AuthTokenPayload(
            public_key=self.device_public_key,
            exp=exp,
            **claims
        )
        
        # Method 1: Local PyNaCl signing with explicit private key
        if prv_key_hex and PYNACL_AVAILABLE:
            try:
                token = create_auth_token_internal(payload, prv_key_hex, self.device_public_key)
                self.jwt_tokens[broker_num] = {
                    "token": token,
                    "expires_at": payload.exp,
                    "audience": audience
                }
                return token
            except Exception as e:
                logger.error(f"[{self.name}] Local JWT signing failed: {e}")

        # Method 2: On-device signing fallback
        if self.api.bot.connection_manager.isConnected and self.api.bot.connection_manager.mc:
            mc = self.api.bot.connection_manager.mc
            if hasattr(mc, 'commands') and hasattr(mc.commands, 'sign'):
                try:
                    logger.info(f"[{self.name}] Requesting on-device Ed25519 signature from hardware...")
                    header = {'alg': 'Ed25519', 'typ': 'JWT'}
                    payload_dict = payload.to_dict()
                    header_encoded = base64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))
                    payload_encoded = base64url_encode(json.dumps(payload_dict, separators=(',', ':')).encode('utf-8'))
                    signing_input = f"{header_encoded}.{payload_encoded}"
                    signing_input_bytes = signing_input.encode('utf-8')
                    
                    cmd_lock = getattr(self.api.bot.connection_manager, "_cmd_lock", None)
                    async with _maybe_lock(cmd_lock):
                        sig_evt = await mc.commands.sign(signing_input_bytes)
                        
                    if sig_evt and hasattr(sig_evt, 'type') and sig_evt.type != EventType.ERROR:
                        sig_bytes = sig_evt.payload.get("signature")
                        if sig_bytes:
                            signature_hex = sig_bytes.hex() if isinstance(sig_bytes, bytes) else sig_bytes
                            token = f"{header_encoded}.{payload_encoded}.{signature_hex}"
                            self.jwt_tokens[broker_num] = {
                                "token": token,
                                "expires_at": payload.exp,
                                "audience": audience
                            }
                            return token
                except Exception as e:
                    logger.error(f"[{self.name}] Hardware on-device signing request failed: {e}")

        # Method 3: Fetch private key from device for future local signings
        if not prv_key_hex and self.api.bot.connection_manager.isConnected:
            try:
                mc = self.api.bot.connection_manager.mc
                if hasattr(mc, 'commands') and hasattr(mc.commands, 'export_private_key'):
                    logger.info(f"[{self.name}] Attempting to export private key from device...")
                    cmd_lock = getattr(self.api.bot.connection_manager, "_cmd_lock", None)
                    async with _maybe_lock(cmd_lock):
                        res = await mc.commands.export_private_key()
                    if res and hasattr(res, 'type') and res.payload:
                        prv_key = res.payload.get("private_key")
                        if prv_key:
                            self.device_private_key = prv_key.hex() if isinstance(prv_key, bytes) else prv_key
                            logger.info(f"[{self.name}] Successfully cached private key from hardware.")
                            if PYNACL_AVAILABLE:
                                token = create_auth_token_internal(payload, self.device_private_key, self.device_public_key)
                                self.jwt_tokens[broker_num] = {
                                    "token": token,
                                    "expires_at": payload.exp,
                                    "audience": audience
                                }
                                return token
            except Exception as e:
                logger.error(f"[{self.name}] Failed to export private key from device: {e}")

        logger.error(f"[{self.name}] Could not generate JWT for broker {broker_num}. Local libraries or keys missing.")
        return ""

    def _get_client_info(self, broker_num: int) -> Optional[dict]:
        for c in self.mqtt_clients:
            if c["broker_num"] == broker_num:
                return c
        return None

    # ==============================================================================
    # MQTT Publishing & Topics
    # ==============================================================================

    def _publish_packet_to_mqtt(self, packet_data: dict):
        if not MQTT_AVAILABLE or not self.mqtt_clients:
            return
            
        for client_info in self.mqtt_clients:
            broker_num = client_info["broker_num"]
            client = client_info["client"]
            b_cfg = client_info["config"]
            if not self.mqtt_connected.get(broker_num, False):
                continue
                
            try:
                # Per-broker toggle for including decoded payload
                include_decoded = b_cfg.get("include_decoded", True)
                if not include_decoded and "decoded" in packet_data:
                    data_to_send = {k: v for k, v in packet_data.items() if k != "decoded"}
                else:
                    data_to_send = packet_data

                # 1. Publish standard packet
                packets_topic = self.get_topic("packets", broker_num)
                if packets_topic:
                    client.publish(packets_topic, json.dumps(data_to_send), qos=0, retain=False)
                    logger.info(f"📤 [{self.name}] Published packet to broker {broker_num} on topic: {packets_topic}")
                    
                # 2. Publish raw packet (defaults to ON, can be disabled per broker or globally with send_raw: false)
                if self._broker_wants_raw(broker_num):
                    raw_topic = self.get_topic("raw", broker_num)
                    if raw_topic:
                        raw_payload = {
                            "origin": packet_data["origin"],
                            "origin_id": packet_data["origin_id"],
                            "timestamp": packet_data["timestamp"],
                            "type": "RAW",
                            "data": packet_data["raw"]
                        }
                        client.publish(raw_topic, json.dumps(raw_payload), qos=0, retain=False)
                        logger.info(f"📤 [{self.name}] Published raw packet to broker {broker_num} on topic: {raw_topic}")
            except Exception as e:
                logger.error(f"[{self.name}] Failed to publish packet to broker {broker_num}: {e}")

    def get_topic(self, topic_type: str, broker_num: int) -> Optional[str]:
        topic_type_upper = topic_type.upper()
        client_info = self._get_client_info(broker_num)
        if not client_info:
            return None
        b_cfg = client_info["config"]
        
        # Check broker-specific override topic
        config_key = f"topic_{topic_type.lower()}"
        custom_topic = b_cfg.get(config_key)
        if custom_topic:
            return self.resolve_topic_template(custom_topic, broker_num)
            
        # Standard MeshCore observer defaults containing region and public key
        iata_defaults = {
            'STATUS': 'meshcore/{IATA}/{PUBLIC_KEY}/status',
            'PACKETS': 'meshcore/{IATA}/{PUBLIC_KEY}/packets',
            'RAW': 'meshcore/{IATA}/{PUBLIC_KEY}/raw',
            'NEIGHBORS': 'meshcore/{IATA}/{PUBLIC_KEY}/neighbors',
        }
        
        chosen_default = iata_defaults.get(topic_type_upper)
        if not chosen_default:
            return None
            
        return self.resolve_topic_template(chosen_default, broker_num)

    def resolve_topic_template(self, template: str, broker_num: int) -> str:
        if not template:
            return template
            
        client_info = self._get_client_info(broker_num)
        b_cfg = client_info["config"] if client_info else {}
        
        iata = b_cfg.get("iata") or self.config.get("iata", "LOC")
        pubkey = self.device_public_key or "DEVICE"
        
        resolved = template
        resolved = resolved.replace('{IATA}', iata.upper())
        resolved = resolved.replace('{IATA_lower}', iata.lower())
        resolved = resolved.replace('{PUBLIC_KEY}', pubkey.upper())
        return resolved

    # ==============================================================================
    # Periodic tasks (Scheduled via bot scheduler)
    # ==============================================================================

    async def _periodic_status(self):
        """Periodically publishes node stats & battery state to all connected brokers."""
        if not MQTT_AVAILABLE or not self.mqtt_clients:
            return
            
        await self._sync_device_info()
        
        for client_info in self.mqtt_clients:
            broker_num = client_info["broker_num"]
            if self.mqtt_connected.get(broker_num, False):
                await self._publish_status_online(broker_num)

    async def _periodic_jwt_check(self):
        """Checks if any active JWT token is nearing expiry and proactively renews it."""
        if not MQTT_AVAILABLE or not self.mqtt_clients:
            return
            
        current_time = time.time()
        for client_info in self.mqtt_clients:
            idx = client_info["broker_num"]
            b_cfg = client_info["config"]
            if not b_cfg.get("token_audience"):
                continue
                
            token_info = self.jwt_tokens.get(idx)
            if not token_info or token_info.get("expires_at") is None:
                continue

            # Renew if missing, expired, or expiring in < 10 mins (600 seconds)
            if (token_info["expires_at"] - current_time) < 600:
                logger.info(f"[{self.name}] JWT token for broker {idx} nearing expiry. Renewing...")
                new_token = await self._generate_jwt(b_cfg.get("token_audience"), idx, b_cfg)
                if new_token:
                    logger.info(f"[{self.name}] Successfully generated new JWT for broker {idx}. Reconnecting client...")
                    client = client_info["client"]
                    username = f"v1_{self.device_public_key.upper()}"
                    client.username_pw_set(username, new_token)
                    client.reconnect()
