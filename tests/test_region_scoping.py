import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from modules.mqtt import Mqtt, RouteType
from core.connection_manager import ConnectionManager
from core.template_engine import extract_template_fields, format_template
from meshcore.events import Event, EventType

UNSCOPED_HEX = "1540D9EE862B2BE9524BBDA3EC9119062DD41686F0E801B3F6784B14963EB3FE5FB0368D64"
SCOPED_HEX = "14C840000040D9B234E98FE4BFA544EE44DEEC5902566BF141E801B3F6784B14963EB3FE5FB0368D64"

def test_mqtt_decode_unscoped_packet():
    mqtt_mod = Mqtt()
    mqtt_mod.config = {}
    decoded = mqtt_mod._decode_packet(UNSCOPED_HEX)
    
    assert decoded is not None
    assert decoded["scoped"] is False
    assert decoded["route_type"] == "FLOOD"
    assert decoded["payload_type"] == "GRP_TXT"
    assert decoded["scope_code"] is None
    assert decoded["transport_codes"] is None
    assert decoded["path_len_byte"] == 0x40
    
    # Hash calculation matches the MeshHub screenshot (5bbad01e5a4dc60d)
    pkt_hash = mqtt_mod._calculate_packet_hash(UNSCOPED_HEX, decoded["payload_type_value"])
    assert pkt_hash.upper() == "5BBAD01E5A4DC60D"

def test_mqtt_decode_scoped_packet():
    mqtt_mod = Mqtt()
    mqtt_mod.config = {}
    decoded = mqtt_mod._decode_packet(SCOPED_HEX)
    
    assert decoded is not None
    assert decoded["scoped"] is True
    assert decoded["route_type"] == "TRANSPORT_FLOOD"
    assert decoded["payload_type"] == "GRP_TXT"
    assert decoded["scope_code"] == "C840"
    assert decoded["return_scope_code"] == "0000"
    assert decoded["transport_codes"] == "C8400000"
    assert decoded["path_len_byte"] == 0x40
    
    # Hash calculation matches the MeshHub screenshot (f9c9332840f1e172)
    pkt_hash = mqtt_mod._calculate_packet_hash(SCOPED_HEX, decoded["payload_type_value"])
    assert pkt_hash.upper() == "F9C9332840F1E172"

def test_mqtt_format_packet_with_scope():
    mqtt_mod = Mqtt()
    mqtt_mod.config = {}
    mqtt_mod.device_name = "TestNode"
    mqtt_mod.device_public_key = "01020304"
    
    formatted = mqtt_mod._format_packet_data(SCOPED_HEX)
    assert formatted["scoped"] is True
    assert formatted["scope_code"] == "C840"
    assert formatted["transport_codes"] == "C8400000"
    assert formatted["hash"] == "F9C9332840F1E172"
    assert formatted["route"] == "F"

def test_mqtt_scope_resolution():
    mqtt_mod = Mqtt()
    # Compute the expected code for a synthetic test payload
    payload_type = 5
    payload_bytes = bytes.fromhex("1122334455667788")
    expected_code = mqtt_mod._compute_scope_code("#DFW", payload_type, payload_bytes)
    
    # Construct a synthetic TRANSPORT_FLOOD packet
    # Header: 0x14 (TRANSPORT_FLOOD, GRP_TXT), Transport Codes: expected_code + 0000, Path: 0x40, Payload
    code_bytes = bytes.fromhex(expected_code)
    raw_bytes = bytes([0x14]) + code_bytes + b"\x00\x00" + b"\x40" + payload_bytes
    
    mqtt_mod.config = {"known_scopes": ["#DFW", "#texas"]}
    decoded = mqtt_mod._decode_packet(raw_bytes.hex())
    assert decoded is not None
    assert decoded["scoped"] is True
    assert decoded["scope_code"] == expected_code
    assert decoded["scope"] == "#DFW"

def test_channel_message_sender_resolution_scoped():
    """Verify that TRANSPORT_FLOOD does not overwrite sender with Unknown-<transport_code>."""
    bot_mock = MagicMock()
    conn_mgr = ConnectionManager(bot_mock)
    conn_mgr.mc = MagicMock()
    
    # Mock packet parser channels_log with TRANSPORT_FLOOD
    parser_mock = MagicMock()
    parser_mock.channels_log = [{
        "sender_timestamp": 1793640215,
        "message": "Dhovin \U0001f6fb: Test",
        "route_type": 0,
        "route_typename": "TC_FLOOD",
        "transport_code": "c8400000"
    }]
    conn_mgr.mc._reader = MagicMock()
    conn_mgr.mc._reader.packet_parser = parser_mock
    
    published_events = []
    bot_mock.event_bus.publish.side_effect = lambda evt, data: published_events.append((evt, data))
    
    event = MagicMock()
    event.payload = {
        "channel_idx": 0,
        "sender_timestamp": 1793640215,
        "text": "Dhovin \U0001f6fb: Test",
        "path_len": 0
    }
    
    conn_mgr._on_channel_message(event)
    
    assert len(published_events) == 1
    evt_name, msg = published_events[0]
    assert evt_name == "message"
    # Sender should be extracted as "Dhovin 🛻", NOT "Unknown-c84000"
    assert msg["sender"] == "Dhovin \U0001f6fb"
    assert msg["text"] == "Test"
    assert msg["scoped"] is True
    assert msg["route_type"] == "TRANSPORT_FLOOD"
    assert msg["scope_code"] == "c840"
    assert msg["return_scope_code"] == "0000"

def test_channel_message_sender_resolution_unscoped():
    """Verify that FLOOD resolves sender cleanly and marks scoped=False."""
    bot_mock = MagicMock()
    conn_mgr = ConnectionManager(bot_mock)
    conn_mgr.mc = MagicMock()
    
    parser_mock = MagicMock()
    parser_mock.channels_log = [{
        "sender_timestamp": 1793640286,
        "message": "Dhovin \U0001f6fb: Test",
        "route_type": 1,
        "route_typename": "FLOOD",
        "transport_code": None
    }]
    conn_mgr.mc._reader = MagicMock()
    conn_mgr.mc._reader.packet_parser = parser_mock
    
    published_events = []
    bot_mock.event_bus.publish.side_effect = lambda evt, data: published_events.append((evt, data))
    
    event = MagicMock()
    event.payload = {
        "channel_idx": 0,
        "sender_timestamp": 1793640286,
        "text": "Dhovin \U0001f6fb: Test",
        "path_len": 0
    }
    
    conn_mgr._on_channel_message(event)
    
    assert len(published_events) == 1
    evt_name, msg = published_events[0]
    assert evt_name == "message"
    assert msg["sender"] == "Dhovin \U0001f6fb"
    assert msg["text"] == "Test"
    assert msg["scoped"] is False
    assert msg["route_type"] == "FLOOD"
    assert msg["scope_code"] is None

def test_channel_message_direct_contact_matching():
    """Verify that TRANSPORT_DIRECT still resolves contact from transport_code when available."""
    bot_mock = MagicMock()
    conn_mgr = ConnectionManager(bot_mock)
    conn_mgr.mc = MagicMock()
    conn_mgr.mc.get_contact_by_key_prefix.return_value = {"adv_name": "RepeaterNode"}
    
    parser_mock = MagicMock()
    parser_mock.channels_log = [{
        "sender_timestamp": 1793640300,
        "message": "Hello from repeater",
        "route_type": 3,
        "route_typename": "TC_DIRECT",
        "transport_code": "11223344"
    }]
    conn_mgr.mc._reader = MagicMock()
    conn_mgr.mc._reader.packet_parser = parser_mock
    
    published_events = []
    bot_mock.event_bus.publish.side_effect = lambda evt, data: published_events.append((evt, data))
    
    event = MagicMock()
    event.payload = {
        "channel_idx": 0,
        "sender_timestamp": 1793640300,
        "text": "Hello from repeater",
        "path_len": 1
    }
    
    conn_mgr._on_channel_message(event)
    
    assert len(published_events) == 1
    evt_name, msg = published_events[0]
    assert msg["sender"] == "RepeaterNode"
    assert msg["route_type"] == "TRANSPORT_DIRECT"

def test_template_engine_scope_variables():
    msg_data = {
        "sender": "Dhovin \U0001f6fb",
        "channel": 0,
        "hops": 0,
        "scoped": True,
        "scope_code": "c840",
        "scope": "#DFW",
        "route_type": "TRANSPORT_FLOOD"
    }
    fields = extract_template_fields(msg_data)
    assert fields["scoped"] == "Yes"
    assert fields["scope"] == "#DFW"
    assert fields["scope_code"] == "c840"
    assert fields["route_type"] == "TRANSPORT_FLOOD"
    
    rendered = format_template("Msg from {sender} | Scoped: {scoped} ({scope})", fields)
    assert rendered == "Msg from Dhovin \U0001f6fb | Scoped: Yes (#DFW)"

@pytest.mark.asyncio
async def test_handshake_sets_default_flood_scope():
    bot_mock = MagicMock()
    bot_mock.config = {
        "core": {
            "default_flood_scope": "#DFW"
        }
    }
    conn_mgr = ConnectionManager(bot_mock)
    conn_mgr.mc = MagicMock()
    conn_mgr.sync_time = AsyncMock()
    conn_mgr.sync_telemetry = AsyncMock()
    conn_mgr._load_contacts = MagicMock()
    
    # Mock send_device_query and send_appstart
    query_evt = Event(EventType.DEVICE_INFO, {"name": "TestNode"})
    conn_mgr.mc.commands.send_device_query = AsyncMock(return_value=query_evt)
    conn_mgr.mc.commands.send_appstart = AsyncMock(return_value=query_evt)
    
    # Mock set_default_flood_scope
    scope_evt = Event(EventType.OK, {})
    conn_mgr.mc.commands.set_default_flood_scope = AsyncMock(return_value=scope_evt)
    
    await conn_mgr._run_handshake()
    
    conn_mgr.mc.commands.set_default_flood_scope.assert_called_once_with("#DFW")
