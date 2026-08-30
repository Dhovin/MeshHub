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

    # Path & Hops extraction
    path = data.get("path")
    path_len = data.get("path_len")
    path_hash_mode = data.get("path_hash_mode")
    hops_input = data.get("hops")

    hops = None
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

    node_hashes = []
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
            elif re.match(r'^[0-9a-fA-F]+$', p_str):
                bytes_per_hop = 1
                if isinstance(path_hash_mode, int) and path_hash_mode >= 0:
                    bytes_per_hop = path_hash_mode + 1
                chunk_len = bytes_per_hop * 2
                if len(p_str) >= chunk_len:
                    node_hashes = [p_str[i:i+chunk_len] for i in range(0, len(p_str), chunk_len)]
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

    # Connection Info summary
    conn_parts = []
    if snr_val is not None:
        conn_parts.append(f"SNR: {snr_str}")
    if rssi_val is not None:
        conn_parts.append(f"RSSI: {rssi_str}")
    
    if hops == 0:
        conn_parts.append("Path: Direct (0 hops)")
    else:
        if node_hashes and len(node_hashes) > 1:
            conn_parts.append(f"Path: {hops_label} ({path_str})")
        else:
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
