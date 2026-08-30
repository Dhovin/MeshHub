import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

def decode_escape_sequences(text: str) -> str:
    """
    Decodes escaped sequences like \n and \t while preserving \\n as literal \n.
    """
    if not text:
        return ""
    # Replace double-escaped with temporary tokens
    token_nl = "___MESH_LITERAL_NL___"
    token_tab = "___MESH_LITERAL_TAB___"
    token_bs = "___MESH_LITERAL_BS___"

    res = text.replace("\\\\", token_bs)
    res = res.replace("\\n", "\n")
    res = res.replace("\\t", "\t")
    res = res.replace("\\r", "\r")

    res = res.replace(token_bs, "\\")
    return res


def extract_template_fields(message_data: dict, timezone_str: str = "UTC", state_cache: dict = None) -> dict:
    """
    Extracts standard template placeholder variables from a message packet dict.
    """
    data = message_data or {}
    sender = str(data.get("sender", "unknown"))
    channel = data.get("channel", "0")
    
    # SNR and RSSI
    snr_val = data.get("snr")
    rssi_val = data.get("rssi")
    
    snr_str = f"{snr_val}dB" if snr_val is not None else "N/A"
    rssi_str = f"{rssi_val}dBm" if rssi_val is not None else "N/A"

    # Path & Hops
    path = data.get("path")
    hops = 0
    if path:
        if isinstance(path, list):
            hops = max(0, len(path) - 1)
            path_str = " > ".join(str(p) for p in path)
        else:
            path_str = str(path)
            # Count hops if separated by commas or arrows
            if ">" in path_str:
                hops = path_str.count(">")
            elif "," in path_str:
                hops = path_str.count(",")
    else:
        path_str = "Direct"
        hops = 0

    if hops == 0:
        hops_label = "Direct (0 hops)"
    elif hops == 1:
        hops_label = "1 hop"
    else:
        hops_label = f"{hops} hops"

    # Connection Info summary
    conn_parts = []
    if snr_val is not None:
        conn_parts.append(f"SNR: {snr_str}")
    if rssi_val is not None:
        conn_parts.append(f"RSSI: {rssi_str}")
    if path_str:
        conn_parts.append(f"Path: {hops_label}")
    conn_info = " | ".join(conn_parts) if conn_parts else "LoRa"

    # Timezone-aware timestamp
    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = ZoneInfo("UTC")
    now_dt = datetime.now(tz)
    timestamp_str = now_dt.strftime("%H:%M:%S")

    # Elapsed latency if packet timestamp is provided
    msg_ts = data.get("timestamp")
    elapsed_str = ""
    if msg_ts:
        try:
            # If in milliseconds
            if msg_ts > 1e11:
                msg_ts = msg_ts / 1000.0
            elapsed_sec = max(0.0, time.time() - float(msg_ts))
            elapsed_str = f"{elapsed_sec:.1f}s"
        except Exception:
            elapsed_str = ""

    # Battery from state cache if present
    battery_str = "N/A"
    if state_cache:
        telemetry = state_cache.get("telemetry", {})
        batt = telemetry.get("battery") or state_cache.get("battery")
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
        "battery": battery_str
    }


def format_template(template: str, fields: dict) -> str:
    """
    Renders template replacing placeholders like {sender}, {snr}, etc.
    Missing variables are safely omitted or left empty.
    Processes newline escape sequences.
    """
    if not template:
        return ""
        
    decoded = decode_escape_sequences(template)
    
    # Regex replacement for {placeholder}
    def replacer(match):
        key = match.group(1).strip()
        val = fields.get(key)
        if val is not None:
            return str(val)
        return match.group(0) # Keep unmodified if key not found
        
    return re.sub(r'\{([a-zA-Z0-9_]+)\}', replacer, decoded)
