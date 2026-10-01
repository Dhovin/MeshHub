import pytest
import time
import json
import hashlib
import hmac
from unittest.mock import MagicMock, AsyncMock

from modules.mqtt import (
    Mqtt,
    ChannelKeyStore,
    derive_hashtag_key,
    channel_hash_for_key,
    decrypt_group_text,
    decode_payload,
    parse_advert,
    AdvertFlags,
    PayloadType,
    NeighborsConfig,
    NeighborEntry,
    sort_entries,
    build_neighbors_message,
    clamp_interval_hours,
    normalize_packet_stats,
    AuthTokenPayload,
    DEFAULT_PUBLIC_CHANNEL_KEY,
    CRYPTOGRAPHY_AVAILABLE
)


def test_hashtag_key_derivation():
    # hashtag key is SHA256("#" + name.lower())[:16]
    key = derive_hashtag_key("mesh")
    expected = hashlib.sha256(b"#mesh").digest()[:16]
    assert key == expected

    key_with_hash = derive_hashtag_key("#test")
    expected_test = hashlib.sha256(b"#test").digest()[:16]
    assert key_with_hash == expected_test


def test_channel_key_store():
    store = ChannelKeyStore()
    assert len(store) == 0

    # Add default public key
    store.add_secret(DEFAULT_PUBLIC_CHANNEL_KEY, "public")
    assert len(store) == 1
    
    h = channel_hash_for_key(DEFAULT_PUBLIC_CHANNEL_KEY)
    assert store.has(h)
    keys = store.keys_for(h)
    assert len(keys) == 1
    assert keys[0][0] == DEFAULT_PUBLIC_CHANNEL_KEY
    assert keys[0][1] == "public"

    # Add hashtag
    store.add_hashtag("#general")
    assert len(store) == 2

    # Add hex
    store.add_hex("00" * 16, "custom")
    assert len(store) == 3


@pytest.mark.skipif(not CRYPTOGRAPHY_AVAILABLE, reason="cryptography package required for AES tests")
def test_group_text_decryption():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    key16 = b"1234567890abcdef"
    channel_hash = hashlib.sha256(key16).digest()[0]
    
    # Message: timestamp(4 bytes LE) + flags(1 byte) + text
    timestamp = 1700000000
    ts_bytes = timestamp.to_bytes(4, "little")
    flags = 0x00
    msg_text = b"Alice: Hello Mesh!"
    plaintext = ts_bytes + bytes([flags]) + msg_text
    
    # Pad to 16 bytes for AES-ECB
    pad_len = 16 - (len(plaintext) % 16)
    if pad_len != 16:
        plaintext += bytes(pad_len)
        
    encryptor = Cipher(algorithms.AES(key16), modes.ECB(), backend=default_backend()).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    
    # MAC
    key32 = key16 + bytes(16)
    calc_mac = hmac.new(key32, ciphertext, hashlib.sha256).digest()[:2]
    
    decrypted = decrypt_group_text(ciphertext, calc_mac, key16)
    assert decrypted is not None
    assert decrypted["timestamp"] == timestamp
    assert decrypted["sender"] == "Alice"
    assert decrypted["text"] == "Hello Mesh!"

    # Test through decode_payload
    store = ChannelKeyStore()
    store.add_secret(key16, "#testchannel")
    wire_payload = bytes([channel_hash]) + calc_mac + ciphertext
    
    res = decode_payload(PayloadType.GRP_TXT.value, wire_payload, store)
    assert res["kind"] == "GRP_TXT"
    assert res["decrypted"] is True
    assert res["channel"] == "#testchannel"
    assert res["sender"] == "Alice"
    assert res["text"] == "Hello Mesh!"


def test_advert_payload_parsing():
    pubkey = b"\xAA" * 32
    timestamp = 1700000000
    ts_bytes = timestamp.to_bytes(4, "little")
    sig = b"\xBB" * 64
    
    # app_data: flags (Companion + LatLon + Name)
    flags = AdvertFlags.ADV_TYPE_CHAT.value | AdvertFlags.ADV_LATLON_MASK.value | AdvertFlags.ADV_NAME_MASK.value
    # lat: 37774900 (37.7749), lon: -122419400 (-122.4194)
    lat_bytes = (37774900).to_bytes(4, "little", signed=True)
    lon_bytes = (-122419400).to_bytes(4, "little", signed=True)
    name_bytes = b"SF_Node_1"
    
    app_data = bytes([flags]) + lat_bytes + lon_bytes + name_bytes
    payload = pubkey + ts_bytes + sig + app_data
    
    adv = parse_advert(payload)
    assert adv["advert_parse_ok"] is True
    assert adv["public_key"] == pubkey.hex()
    assert adv["advert_time"] == timestamp
    assert adv["mode"] == "Companion"
    assert adv["lat"] == 37.7749
    assert adv["lon"] == -122.4194
    assert adv["name"] == "SF_Node_1"


def test_neighbors_config_and_clamping():
    assert clamp_interval_hours(0) == 24
    assert clamp_interval_hours(6) == 12  # Clamped to min 12h
    assert clamp_interval_hours(500) == 336  # Clamped to max 336h
    assert clamp_interval_hours(48) == 48

    cfg = NeighborsConfig(interval_hours=5, discover_window=2.0, max_neighbors=0)
    assert cfg.interval_hours == 12
    assert cfg.discover_window == 5.0  # Floor
    assert cfg.max_neighbors == 1  # Floor


def test_neighbors_entry_sorting_and_build_message():
    now = time.time()
    e1 = NeighborEntry(pubkey="11" * 32, snr=-2.0, heard_at=now - 50, scopes="flood")
    e2 = NeighborEntry(pubkey="22" * 32, snr=5.0, heard_at=now - 10, scopes="flood")
    e3 = NeighborEntry(pubkey="33" * 32, snr=-10.0, heard_at=now - 10, scopes="")

    # Most recently heard first, then higher SNR
    sorted_entries = sort_entries([e1, e2, e3], now)
    assert sorted_entries[0].pubkey == e2.pubkey  # heard 10s ago, snr 5.0
    assert sorted_entries[1].pubkey == e3.pubkey  # heard 10s ago, snr -10.0
    assert sorted_entries[2].pubkey == e1.pubkey  # heard 50s ago

    msg, dropped = build_neighbors_message(
        origin="TestBot",
        origin_id="MYPUBKEY",
        self_scopes="local",
        entries=[e1, e2, e3],
        now=now,
        budget=10240
    )
    assert dropped == 0
    assert msg["origin"] == "TestBot"
    assert msg["origin_id"] == "MYPUBKEY"
    assert msg["total_neighbors"] == 3
    assert msg["queried_neighbors"] == 3
    assert msg["truncated"] is False
    assert msg["self"]["scopes"] == "local"
    assert len(msg["neighbors"]) == 3
    assert msg["neighbors"][0]["pubkey"] == e2.pubkey.upper()


def test_neighbors_budget_truncation():
    now = time.time()
    entries = [
        NeighborEntry(pubkey=f"{i:02x}" * 32, snr=float(i), heard_at=now - i, scopes="long_scope_string_for_testing")
        for i in range(100)
    ]
    # Small budget to trigger truncation
    small_budget = 400
    msg, dropped = build_neighbors_message(
        origin="TestBot",
        origin_id="MYPUBKEY",
        self_scopes="local",
        entries=entries,
        now=now,
        budget=small_budget
    )
    assert dropped > 0
    assert msg["truncated"] is True
    assert len(json.dumps(msg)) <= small_budget


def test_auth_token_payload_and_claims():
    payload = AuthTokenPayload(
        public_key="00" * 32,
        exp=1800000000,
        aud="test.broker.org",
        owner="AA" * 32,
        email="operator@example.com"
    )
    d = payload.to_dict()
    assert d["publicKey"] == ("00" * 32).upper()
    assert d["exp"] == 1800000000
    assert d["aud"] == "test.broker.org"
    assert d["owner"] == ("AA" * 32).upper()
    assert d["email"] == "operator@example.com"


def test_normalize_packet_stats():
    raw_stats = {
        "uptime": 1234,
        "sent": 42,
        "recv": 99,
        "errors": 1
    }
    norm = normalize_packet_stats(raw_stats)
    assert norm["packets_sent"] == 42
    assert norm["packets_received"] == 99
    assert norm["uptime"] == 1234


def test_mqtt_topic_resolution():
    mqtt_module = Mqtt()
    mqtt_module.device_public_key = "ABCDEF123456"
    mqtt_module.config = {"iata": "DFW"}
    mqtt_module.mqtt_clients = [
        {
            "broker_num": 1,
            "config": {
                "server": "test.broker.org",
                "port": 1883
            }
        },
        {
            "broker_num": 2,
            "config": {
                "server": "regional.broker.org",
                "port": 443,
                "iata": "ORD",
                "topic_neighbors": "custom/{IATA}/{PUBLIC_KEY}/myneighbors"
            }
        }
    ]

    assert mqtt_module.get_topic("status", 1) == "meshcore/DFW/ABCDEF123456/status"
    assert mqtt_module.get_topic("packets", 1) == "meshcore/DFW/ABCDEF123456/packets"
    assert mqtt_module.get_topic("raw", 1) == "meshcore/DFW/ABCDEF123456/raw"
    assert mqtt_module.get_topic("neighbors", 1) == "meshcore/DFW/ABCDEF123456/neighbors"

    # Custom override and regional IATA override
    assert mqtt_module.get_topic("neighbors", 2) == "custom/ORD/ABCDEF123456/myneighbors"
    assert mqtt_module.get_topic("status", 2) == "meshcore/ORD/ABCDEF123456/status"


def test_broker_wants_raw_and_neighbors_defaults():
    mqtt_module = Mqtt()
    mqtt_module.config = {"send_raw": True, "send_neighbors": True}
    mqtt_module.mqtt_clients = [
        {
            "broker_num": 1,
            "config": {
                "server": "default.broker.org",
                "port": 1883
            }
        },
        {
            "broker_num": 2,
            "config": {
                "server": "disabled.broker.org",
                "port": 1883,
                "send_raw": False,
                "send_neighbors": False
            }
        }
    ]

    # Broker 1 defaults to True
    assert mqtt_module._broker_wants_raw(1) is True
    assert mqtt_module._broker_wants_neighbors(1) is True

    # Broker 2 explicitly disabled
    assert mqtt_module._broker_wants_raw(2) is False
    assert mqtt_module._broker_wants_neighbors(2) is False


def test_format_packet_with_decoded_payload():
    mqtt_module = Mqtt()
    mqtt_module.init(MagicMock(), {
        "enabled": True,
        "decode_payloads": True,
        "decode_include_public": True
    })
    mqtt_module.device_public_key = "1234567890ABCDEF"

    # Raw hex for a flood packet with public channel text
    raw_hex = "0100"  # minimal 2-byte header/path
    formatted = mqtt_module._format_packet_data(raw_hex)
    assert "origin" in formatted
    assert "origin_id" in formatted
    assert formatted["type"] == "PACKET"


@pytest.mark.asyncio
async def test_rx_log_data_dispatches_packet_and_raw():
    mqtt_module = Mqtt()
    mqtt_module.init(MagicMock(), {
        "enabled": True,
        "send_raw": True,
        "iata": "DFW"
    })
    mqtt_module.device_public_key = "0B313702B7F6CA3BE989091EC8124E3A5A334ACE2EAC8D4ECEA16006866C860E"
    
    mock_client = MagicMock()
    mqtt_module.mqtt_clients = [{
        "broker_num": 1,
        "client": mock_client,
        "config": {"server": "ntxmesh.dhovin.me"}
    }]
    mqtt_module.mqtt_connected = {1: True}

    # Simulate an incoming RX_LOG_DATA event
    log_data = {
        "snr": 9.25,
        "rssi": -65,
        "payload": "0100ABCD1234",
        "payload_length": 6
    }
    await mqtt_module._on_rx_log_data(log_data)

    assert mqtt_module.packet_count == 1
    # Check that client.publish was called for both packets and raw topics
    assert mock_client.publish.call_count == 2
    
    topics = [call[0][0] for call in mock_client.publish.call_args_list]
    assert "meshcore/DFW/0B313702B7F6CA3BE989091EC8124E3A5A334ACE2EAC8D4ECEA16006866C860E/packets" in topics
    assert "meshcore/DFW/0B313702B7F6CA3BE989091EC8124E3A5A334ACE2EAC8D4ECEA16006866C860E/raw" in topics

    # Now verify deduplication: an immediate raw_data event with identical hex should be skipped
    mock_client.publish.reset_mock()
    await mqtt_module._on_raw_data({"data": "0100ABCD1234"})
    assert mock_client.publish.call_count == 0

