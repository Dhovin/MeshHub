"""LoRa 135-byte message clamping, chunking, and template rendering utilities."""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

MAX_LORA_BYTES = 135


def clamp_lora_message(text: str, max_bytes: int = MAX_LORA_BYTES) -> str:
    """Clamp any UTF-8 string to at most max_bytes bytes, appending '...' when truncated."""
    if not text:
        return ""
    text = str(text)
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    if max_bytes <= 3:
        return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    truncated = text[: max_bytes - 3] + "..."
    while len(truncated.encode("utf-8")) > max_bytes and len(truncated) > 3:
        truncated = truncated[:-4] + "..."
    return truncated


def shorten_to_bytes(text: str, max_bytes: int = MAX_LORA_BYTES) -> str:
    """Shorten text to fit within max_bytes on a word boundary when possible."""
    if not isinstance(text, str) or max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    decoded = encoded[:max_bytes].decode("utf-8", errors="ignore")
    while len(decoded.encode("utf-8")) > max_bytes:
        decoded = decoded[:-1]
    match = re.match(r"^(.*)\s", decoded, re.DOTALL)
    if match and match.group(1):
        return match.group(1)
    return decoded


def split_to_byte_chunks(text: str, max_bytes: int = 130) -> list[str]:
    """Split a long string into a list of UTF-8 chunks each <= max_bytes."""
    if not isinstance(text, str) or max_bytes <= 0:
        return []
    chunks: list[str] = []
    remaining = text.strip()
    while remaining:
        encoded = remaining.encode("utf-8")
        if len(encoded) <= max_bytes:
            chunks.append(remaining)
            break
        candidate = encoded[:max_bytes].decode("utf-8", errors="ignore")
        while len(candidate.encode("utf-8")) > max_bytes:
            candidate = candidate[:-1]
        if not candidate:
            break
        split_idx = -1
        match_nl = re.search(r"^(.*)\n", candidate, re.DOTALL)
        if match_nl and match_nl.group(1).strip():
            split_idx = len(match_nl.group(1))
        else:
            match_sentence = re.search(r"^(.*[.?!])\s", candidate, re.DOTALL)
            if match_sentence:
                split_idx = len(match_sentence.group(1))
            else:
                match_space = re.search(r"^(.*)\s", candidate, re.DOTALL)
                if match_space:
                    split_idx = len(match_space.group(1))
        final_chunk = remaining[:split_idx] if split_idx > 0 else candidate
        chunks.append(final_chunk.strip())
        remaining = remaining[len(final_chunk) :].strip()
    return chunks


def decode_escape_sequences(text: str) -> str:
    """Decode escaped \\n and \\t while preserving \\\\n as literal \\n."""
    if not text:
        return ""
    token_bs = "___MESH_LITERAL_BS___"
    res = text.replace("\\\\", token_bs)
    res = res.replace("\\n", "\n")
    res = res.replace("\\t", "\t")
    res = res.replace("\\r", "\r")
    return res.replace(token_bs, "\\")


def extract_template_fields(
    message_data: dict[str, Any] | None,
    timezone_str: str = "UTC",
    state_cache: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Extract standard template placeholder variables from a message packet dict."""
    data = message_data or {}
    sender = str(data.get("sender", "unknown"))
    channel = data.get("channel", "0")

    snr_val = data.get("snr")
    rssi_val = data.get("rssi")
    snr_str = f"{snr_val}dB" if snr_val is not None else "N/A"
    rssi_str = f"{rssi_val}dBm" if rssi_val is not None else "N/A"

    path = data.get("path")
    path_len = data.get("path_len")
    path_hash_mode = data.get("path_hash_mode")
    hops_input = data.get("hops")

    hops: int | None = None
    if hops_input is not None:
        try:
            hops = int(hops_input)
        except (ValueError, TypeError):
            pass

    if hops is None and path_len is not None:
        try:
            pl = int(path_len)
            if pl == 255:
                hops = 0
            elif pl >= 0:
                hops = pl
        except (ValueError, TypeError):
            pass

    node_hashes: list[str] = []
    if path:
        if isinstance(path, list):
            node_hashes = [str(p) for p in path if str(p).strip()]
            if hops is None:
                hops = max(0, len(node_hashes) - 1) if len(node_hashes) > 1 else len(node_hashes)
        elif isinstance(path, str):
            p_str = path.strip()
            if ">" in p_str:
                node_hashes = [p.strip() for p in p_str.split(">") if p.strip()]
                if hops is None:
                    hops = max(0, len(node_hashes) - 1) if len(node_hashes) > 1 else len(node_hashes)
            elif "," in p_str:
                node_hashes = [p.strip() for p in p_str.split(",") if p.strip()]
                if hops is None:
                    hops = len(node_hashes)
            elif re.match(r"^[0-9a-fA-F]+$", p_str):
                bytes_per_hop = 1
                if isinstance(path_hash_mode, int) and path_hash_mode >= 0:
                    bytes_per_hop = path_hash_mode + 1
                chunk_len = bytes_per_hop * 2
                if len(p_str) >= chunk_len:
                    node_hashes = [
                        p_str[i : i + chunk_len] for i in range(0, len(p_str), chunk_len)
                    ]
                    if hops is None:
                        hops = len(node_hashes)
            elif "direct" in p_str.lower() or "0 hop" in p_str.lower():
                if hops is None:
                    hops = 0

    if hops is None:
        hops = 0

    if hops == 0:
        hops_label = "Direct (0 hops)"
        path_str = "Direct"
    elif hops == 1:
        hops_label = "1 hop"
        path_str = " > ".join(node_hashes) if node_hashes else "1 hop"
    else:
        hops_label = f"{hops} hops"
        path_str = " > ".join(node_hashes) if node_hashes else f"{hops} hops"

    if hops == 0:
        conn_info = "Path: Direct (0 hops)"
    elif node_hashes and len(node_hashes) > 1:
        conn_info = f"Path: {hops_label} ({path_str})"
    else:
        conn_info = f"Path: {hops_label}"

    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = ZoneInfo("UTC")
    timestamp_str = datetime.now(tz).strftime("%H:%M:%S")

    msg_ts = data.get("timestamp")
    elapsed_str = ""
    if msg_ts:
        try:
            ts_val = float(msg_ts)
            if ts_val > 1e11:
                ts_val /= 1000.0
            elapsed_sec = max(0.0, time.time() - ts_val)
            elapsed_str = f"{elapsed_sec:.1f}s"
        except Exception:
            elapsed_str = ""

    battery_str = "N/A"
    if state_cache:
        telemetry = state_cache.get("telemetry", {})
        batt = telemetry.get("battery") if isinstance(telemetry, dict) else None
        if batt is None:
            batt = state_cache.get("battery")
        if batt is not None:
            battery_str = f"{batt}%"

    return {
        "sender": sender,
        "channel": str(channel),
        "snr": snr_str,
        "rssi": rssi_str,
        "path": path_str,
        "hops": str(hops),
        "hops_label": hops_label,
        "connection_info": conn_info,
        "timestamp": timestamp_str,
        "elapsed": elapsed_str,
        "battery": battery_str,
    }


def format_template(template: str, fields: dict[str, Any]) -> str:
    """Render template replacing {placeholder} variables and decoding \\n sequences."""
    if not template:
        return ""
    decoded = decode_escape_sequences(template)

    def replacer(match: re.Match) -> str:
        key = match.group(1).strip()
        val = fields.get(key)
        if val is not None:
            return str(val)
        return match.group(0)

    return re.sub(r"\{([a-zA-Z0-9_]+)\}", replacer, decoded)
