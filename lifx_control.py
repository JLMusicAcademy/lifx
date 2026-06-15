#!/usr/bin/env python3
"""
lifx_control.py - Control a LIFX bulb directly over your LAN.

LIFX bulbs listen for UDP packets on port 56700 and speak a documented binary
"LAN protocol". This script implements just enough of that protocol to discover
bulbs and control color, power, brightness, temperature, fades and effects -
with no external dependencies (standard library only).

Protocol reference: https://lan.developer.lifx.com/docs

Quick start:
    # Find bulbs on your network
    python3 lifx_control.py discover

    # Turn on / off (with a 2 second fade)
    python3 lifx_control.py on  --duration 2
    python3 lifx_control.py off --duration 2

    # Set a color by name, with a fade-in over 3 seconds
    python3 lifx_control.py color --name red --duration 3

    # Set a precise color (hue 0-360, sat/bright 0-100, kelvin 2500-9000)
    python3 lifx_control.py color --hue 240 --saturation 100 --brightness 80

    # Set white at a color temperature
    python3 lifx_control.py white --kelvin 2700 --brightness 100

    # Just change brightness (dim to 20% over 5 seconds)
    python3 lifx_control.py brightness --brightness 20 --duration 5

    # Effects
    python3 lifx_control.py pulse   --name blue  --cycles 5 --period 1
    python3 lifx_control.py breathe --name green --cycles 5 --period 2

    # Read the current state of the bulb
    python3 lifx_control.py state

By default commands are broadcast to every LIFX bulb on the LAN. Target a
single bulb with --ip 192.168.1.50 (find it with `discover`).
"""

import argparse
import colorsys
import hashlib
import json
import math
import random
import select
import socket
import struct
import sys
import time

# --- LIFX LAN protocol constants -------------------------------------------

LIFX_PORT = 56700
BROADCAST_ADDR = "255.255.255.255"

# Message types (see https://lan.developer.lifx.com/docs/messages)
MSG_GET_SERVICE = 2
MSG_STATE_SERVICE = 3
MSG_GET_COLOR = 101      # "Get" -> device replies with State (107)
MSG_STATE = 107          # "State" reply for a light
MSG_SET_COLOR = 102
MSG_SET_WAVEFORM = 103
MSG_GET_POWER = 116
MSG_SET_POWER = 117      # light SetPower (supports a duration / fade)
MSG_STATE_POWER = 118
MSG_SET_WAVEFORM_OPTIONAL = 119  # like SetWaveform but oscillate only chosen attrs

# Device-info / maintenance messages (all officially documented over the LAN).
MSG_GET_HOST_FIRMWARE = 14
MSG_STATE_HOST_FIRMWARE = 15
MSG_GET_WIFI_INFO = 16
MSG_STATE_WIFI_INFO = 17
MSG_GET_WIFI_FIRMWARE = 18
MSG_STATE_WIFI_FIRMWARE = 19
MSG_GET_LABEL = 23
MSG_SET_LABEL = 24
MSG_STATE_LABEL = 25
MSG_GET_VERSION = 32
MSG_STATE_VERSION = 33
MSG_GET_INFO = 34
MSG_STATE_INFO = 35
MSG_GET_LOCATION = 48
MSG_SET_LOCATION = 49
MSG_STATE_LOCATION = 50
MSG_GET_GROUP = 51
MSG_SET_GROUP = 52
MSG_STATE_GROUP = 53
MSG_ECHO_REQUEST = 58
MSG_ECHO_RESPONSE = 59

# A small map of LIFX product IDs -> (name, has_color). Anything not listed
# falls back to "Product <id>". (LIFX publishes the full list as products.json.)
LIFX_PRODUCTS = {
    1: ("Original 1000", True), 3: ("Color 650", True),
    10: ("White 800 (Low Voltage)", False), 11: ("White 800 (High Voltage)", False),
    18: ("White 900 BR30", False), 20: ("Color 1000 BR30", True),
    22: ("Color 1000", True), 27: ("LIFX A19", True), 28: ("LIFX BR30", True),
    29: ("LIFX A19 Night Vision", True), 30: ("LIFX BR30 Night Vision", True),
    31: ("LIFX Z", True), 32: ("LIFX Z", True), 38: ("LIFX Beam", True),
    43: ("LIFX A19", True), 44: ("LIFX BR30", True),
    45: ("LIFX A19 Night Vision", True), 46: ("LIFX BR30 Night Vision", True),
    49: ("LIFX Mini Color", True), 50: ("LIFX Mini White", False),
    51: ("LIFX Mini White", False), 52: ("LIFX GU10", True),
    55: ("LIFX Tile", True), 57: ("LIFX Candle", True), 59: ("LIFX Mini Color", True),
    62: ("LIFX A19", True), 63: ("LIFX BR30", True), 68: ("LIFX Candle", True),
    81: ("LIFX Candle White", False), 82: ("LIFX Filament", False),
    90: ("LIFX Clean", True), 97: ("LIFX A19", True), 98: ("LIFX BR30", True),
    99: ("LIFX Clean", True), 109: ("LIFX A19 Night Vision", True),
    111: ("LIFX A19", True),
}

# Waveform identifiers used by SetWaveform
WAVEFORM_SAW = 0
WAVEFORM_SINE = 1
WAVEFORM_HALF_SINE = 2
WAVEFORM_TRIANGLE = 3
WAVEFORM_PULSE = 4

# A small palette of named colors -> (hue 0-360, saturation 0-100).
COLORS = {
    "red": (0, 100),
    "orange": (36, 100),
    "yellow": (60, 100),
    "green": (120, 100),
    "cyan": (180, 100),
    "blue": (250, 100),
    "purple": (280, 100),
    "magenta": (300, 100),
    "pink": (330, 100),
    "white": (0, 0),
}


# --- Packet construction ----------------------------------------------------

def _scale(value, in_max, out_max=65535):
    """Scale a 0..in_max value into the protocol's 0..65535 range."""
    value = max(0, min(in_max, value))
    return int(round(value / in_max * out_max))


def build_packet(msg_type, payload=b"", source=2, sequence=0,
                 tagged=True, ack_required=False, res_required=False,
                 target=0):
    """Build a complete LIFX LAN packet (36-byte header + payload).

    The header has three sections: Frame, Frame Address and Protocol Header.
    All multi-byte integers are little-endian.
    """
    size = 36 + len(payload)

    # --- Frame header (8 bytes) ---
    # protocol(12 bits)=1024, addressable=1, tagged, origin(2 bits)=0
    flags = 1024 | (1 << 12) | ((1 if tagged else 0) << 13)
    frame = struct.pack("<HHI", size, flags, source)

    # --- Frame address (16 bytes) ---
    # 8-byte target (MAC, 0 = broadcast), 6 reserved bytes,
    # 1 byte of response flags, 1 byte sequence.
    target_bytes = struct.pack("<Q", target)
    response = (1 if res_required else 0) | ((1 if ack_required else 0) << 1)
    frame_address = target_bytes + b"\x00" * 6 + struct.pack("<BB", response, sequence)

    # --- Protocol header (12 bytes) ---
    # 8 reserved bytes, 2-byte message type, 2 reserved bytes.
    protocol_header = struct.pack("<QHH", 0, msg_type, 0)

    return frame + frame_address + protocol_header + payload


def parse_header(data):
    """Return (msg_type, payload, target_mac_int) from a received packet."""
    if len(data) < 36:
        return None, b"", 0
    target = struct.unpack_from("<Q", data, 8)[0]
    msg_type = struct.unpack_from("<H", data, 32)[0]
    return msg_type, data[36:], target


def hsbk_payload(hue, saturation, brightness, kelvin):
    """Pack an HSBK color (the bulb's native color representation)."""
    return struct.pack(
        "<HHHH",
        _scale(hue % 360, 360),
        _scale(saturation, 100),
        _scale(brightness, 100),
        int(max(2500, min(9000, kelvin))),
    )


# --- Networking -------------------------------------------------------------

def make_socket(timeout=1.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    sock.bind(("", 0))
    return sock


def send(packet, ip=None):
    """Send a packet to a specific bulb IP, or broadcast to all bulbs."""
    sock = make_socket()
    try:
        sock.sendto(packet, (ip or BROADCAST_ADDR, LIFX_PORT))
    finally:
        sock.close()


def discover(timeout=3.0, attempts=3):
    """Discover LIFX bulbs and return a sorted list of (ip, mac).

    UDP is lossy, so a single broadcast can miss a bulb (or its reply can be
    dropped). We re-broadcast a few times across the listen window and poll
    continuously with a short socket timeout, rather than giving up the moment
    the network goes quiet. This makes multi-bulb discovery reliable.
    """
    sock = make_socket(0.3)  # short timeout so we keep polling until the deadline
    found = {}
    probe = build_packet(MSG_GET_SERVICE, res_required=True)
    try:
        deadline = time.time() + timeout
        next_probe = 0.0
        sent = 0
        while time.time() < deadline:
            # Spread `attempts` broadcasts evenly over the first ~half window.
            now = time.time()
            if sent < attempts and now >= next_probe:
                sock.sendto(probe, (BROADCAST_ADDR, LIFX_PORT))
                sent += 1
                next_probe = now + (timeout / 2) / attempts
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            msg_type, _, target = parse_header(data)
            if msg_type == MSG_STATE_SERVICE:
                mac = ":".join(f"{b:02x}" for b in struct.pack("<Q", target)[:6])
                found[addr[0]] = mac
    finally:
        sock.close()
    return sorted(found.items())


def query(packet, ip=None, expect_type=None, timeout=2.0):
    """Send a packet and wait for a reply (optionally of a given type)."""
    sock = make_socket(timeout)
    try:
        sock.sendto(packet, (ip or BROADCAST_ADDR, LIFX_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                break
            msg_type, payload, _ = parse_header(data)
            if expect_type is None or msg_type == expect_type:
                return addr[0], msg_type, payload
    finally:
        sock.close()
    return None, None, b""


# --- High level commands ----------------------------------------------------

def set_power(on, duration_ms, ip=None):
    level = 65535 if on else 0
    payload = struct.pack("<HI", level, duration_ms)
    send(build_packet(MSG_SET_POWER, payload), ip)


def set_color(hue, saturation, brightness, kelvin, duration_ms, ip=None):
    # SetColor payload: 1 reserved byte, HSBK (8 bytes), duration uint32.
    payload = b"\x00" + hsbk_payload(hue, saturation, brightness, kelvin) \
        + struct.pack("<I", duration_ms)
    send(build_packet(MSG_SET_COLOR, payload), ip)


def set_waveform(hue, saturation, brightness, kelvin, period_ms, cycles,
                 waveform, transient=True, skew_ratio=0, ip=None):
    """Run an effect (pulse, breathe, ...) by oscillating toward a color."""
    payload = struct.pack("<BB", 0, 1 if transient else 0)
    payload += hsbk_payload(hue, saturation, brightness, kelvin)
    payload += struct.pack("<IfhB", period_ms, float(cycles),
                           int(skew_ratio), waveform)
    send(build_packet(MSG_SET_WAVEFORM, payload), ip)


def set_waveform_optional(hue, saturation, brightness, kelvin, period_ms, cycles,
                          waveform, transient=True, skew_ratio=0,
                          set_hue=False, set_saturation=False,
                          set_brightness=False, set_kelvin=False, ip=None):
    """Like set_waveform, but only the chosen HSBK attributes oscillate.

    The bulb runs this autonomously once armed (e.g. a brightness-only PULSE
    is a strobe; a brightness-only SINE is a breathe), so it costs no ongoing
    network traffic. `cycles` is how many times to run before settling.
    """
    payload = struct.pack("<BB", 0, 1 if transient else 0)
    payload += hsbk_payload(hue, saturation, brightness, kelvin)
    payload += struct.pack("<IfhB", period_ms, float(cycles),
                           int(skew_ratio), waveform)
    payload += struct.pack("<BBBB", int(set_hue), int(set_saturation),
                           int(set_brightness), int(set_kelvin))
    send(build_packet(MSG_SET_WAVEFORM_OPTIONAL, payload), ip)


def get_state(ip=None):
    """Query a bulb's current color/power. Returns a dict or None."""
    addr, msg_type, payload = query(
        build_packet(MSG_GET_COLOR, res_required=True),
        ip, expect_type=MSG_STATE)
    if msg_type != MSG_STATE or len(payload) < 12:
        return None
    hue, sat, bri, kelvin, _reserved, power = struct.unpack_from("<HHHHhH", payload, 0)
    # Label is 32 bytes starting at offset 12 (after a 2-byte reserved field).
    label = payload[12:44].split(b"\x00", 1)[0].decode("utf-8", "replace")
    return {
        "ip": addr,
        "label": label,
        "power": "on" if power else "off",
        "hue": round(hue / 65535 * 360, 1),
        "saturation": round(sat / 65535 * 100, 1),
        "brightness": round(bri / 65535 * 100, 1),
        "kelvin": kelvin,
    }


# --- Device info / maintenance (replicates much of the LIFX app) ------------

def _name_guid(kind, name):
    """Deterministic 16-byte GUID from a name, so bulbs sharing a group/location
    name share the same GUID (which is how the LIFX app groups them)."""
    return hashlib.sha1(f"{kind}:{name}".encode()).digest()[:16]


def get_version(ip):
    """Return (vendor, product_id, model_name, has_color) or None."""
    _, mt, p = query(build_packet(MSG_GET_VERSION, res_required=True), ip,
                     MSG_STATE_VERSION)
    if mt != MSG_STATE_VERSION or len(p) < 12:
        return None
    vendor, product = struct.unpack_from("<II", p, 0)
    name, has_color = LIFX_PRODUCTS.get(product, (f"Product {product}", True))
    return vendor, product, name, has_color


def get_host_firmware(ip):
    """Return the bulb's firmware version as 'major.minor', or None."""
    _, mt, p = query(build_packet(MSG_GET_HOST_FIRMWARE, res_required=True), ip,
                     MSG_STATE_HOST_FIRMWARE)
    if mt != MSG_STATE_HOST_FIRMWARE or len(p) < 20:
        return None
    minor, major = struct.unpack_from("<HH", p, 16)
    return f"{major}.{minor}"


def get_wifi_info(ip):
    """Return {'signal_mw', 'dbm', 'bars'(0-4), 'label'} for Wi-Fi signal, or None."""
    _, mt, p = query(build_packet(MSG_GET_WIFI_INFO, res_required=True), ip,
                     MSG_STATE_WIFI_INFO)
    if mt != MSG_STATE_WIFI_INFO or len(p) < 4:
        return None
    (signal,) = struct.unpack_from("<f", p, 0)
    if signal and signal > 0:
        dbm = 10 * math.log10(signal)
    else:
        return {"signal_mw": signal, "dbm": None, "bars": 0, "label": "unknown"}
    if dbm >= -50:
        bars, label = 4, "excellent"
    elif dbm >= -60:
        bars, label = 3, "good"
    elif dbm >= -70:
        bars, label = 2, "fair"
    elif dbm >= -80:
        bars, label = 1, "weak"
    else:
        bars, label = 0, "very weak"
    return {"signal_mw": signal, "dbm": round(dbm, 1), "bars": bars, "label": label}


def get_uptime(ip):
    """Return the bulb's uptime in seconds, or None."""
    _, mt, p = query(build_packet(MSG_GET_INFO, res_required=True), ip,
                     MSG_STATE_INFO)
    if mt != MSG_STATE_INFO or len(p) < 24:
        return None
    _time, uptime_ns, _downtime = struct.unpack_from("<QQQ", p, 0)
    return uptime_ns / 1e9


def get_label(ip):
    _, mt, p = query(build_packet(MSG_GET_LABEL, res_required=True), ip,
                     MSG_STATE_LABEL)
    if mt != MSG_STATE_LABEL:
        return None
    return p[:32].split(b"\x00", 1)[0].decode("utf-8", "replace")


def set_label(ip, label):
    """Write the bulb's name into the device itself (as the LIFX app does)."""
    payload = label.encode("utf-8")[:32].ljust(32, b"\x00")
    send(build_packet(MSG_SET_LABEL, payload), ip)


def _get_named(ip, get_type, state_type):
    _, mt, p = query(build_packet(get_type, res_required=True), ip, state_type)
    if mt != state_type or len(p) < 48:
        return None
    return p[16:48].split(b"\x00", 1)[0].decode("utf-8", "replace")


def get_group(ip):
    return _get_named(ip, MSG_GET_GROUP, MSG_STATE_GROUP)


def get_location(ip):
    return _get_named(ip, MSG_GET_LOCATION, MSG_STATE_LOCATION)


def _set_named(ip, set_type, kind, name):
    guid = _name_guid(kind, name)
    label = name.encode("utf-8")[:32].ljust(32, b"\x00")
    updated_at = int(time.time() * 1e9)
    send(build_packet(set_type, guid + label + struct.pack("<Q", updated_at)), ip)


def set_group(ip, name):
    """Assign the bulb to a named group (shared GUID groups them in the app)."""
    _set_named(ip, MSG_SET_GROUP, "group", name)


def set_location(ip, name):
    _set_named(ip, MSG_SET_LOCATION, "location", name)


def echo(ip, timeout=1.5):
    """Round-trip connectivity test. Returns latency in ms, or None if no reply."""
    payload = b"lifx-bridge-echo".ljust(64, b"\x00")
    start = time.time()
    _, mt, p = query(build_packet(MSG_ECHO_REQUEST, res_required=True), ip,
                     MSG_ECHO_RESPONSE, timeout=timeout)
    if mt != MSG_ECHO_RESPONSE:
        return None
    return round((time.time() - start) * 1000, 1)


def device_info(ip):
    """Aggregate the maintenance/diagnostic info for one bulb (several queries)."""
    ver = get_version(ip)
    return {
        "ip": ip,
        "model": ver[2] if ver else None,
        "product_id": ver[1] if ver else None,
        "has_color": ver[3] if ver else None,
        "firmware": get_host_firmware(ip),
        "wifi": get_wifi_info(ip),
        "uptime_s": get_uptime(ip),
        "label": get_label(ip),
        "group": get_group(ip),
        "location": get_location(ip),
        "latency_ms": echo(ip),
    }


# --- Art-Net DMX listener ---------------------------------------------------
#
# QLab's Light workspace outputs DMX over Art-Net (UDP port 6454). We listen
# for those packets and treat each LIFX bulb as an 8-channel fixture:
#
#   ch+0 Red   ch+1 Green   ch+2 Blue   ch+3 Amber   ch+4 Intensity
#   ch+5 Effect/Mode   ch+6 Effect Speed   ch+7 Strobe
#
# That lets QLab drive the bulbs with its native color wheel and fade cues,
# plus the bulbs' own effects (breathe/pulse/strobe run on the bulb firmware;
# rainbow/color-loop/candle are generated by this script). Many bulbs are
# supported: each gets its own DMX start address (rolling over into additional
# universes once a 512-channel universe fills up).

ARTNET_ID = b"Art-Net\x00"
ARTNET_PORT = 6454
OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
OP_DMX = 0x5000
CHANNELS_PER_FIXTURE = 8  # R, G, B, Amber, Intensity, Mode, Speed, Strobe

# Effect/Mode channel (ch+5) value ranges -> mode name.
EFFECT_RANGES = [
    (10, "static"),       # 0-9    steady color (SetColor)
    (40, "breathe"),      # 10-39  native SINE on brightness
    (70, "pulse"),        # 40-69  native PULSE on brightness (blink)
    (100, "triangle"),    # 70-99  native TRIANGLE on brightness
    (130, "saw"),         # 100-129 native SAW on brightness
    (170, "color_pulse"), # 130-169 native PULSE between color and complement
    (210, "rainbow"),     # 170-209 script hue sweep
    (240, "color_loop"),  # 210-239 script stepped palette
    (256, "candle"),      # 240-255 script warm flicker
]

# Whole-revolution palette used by the color-loop effect (hues in degrees).
LOOP_PALETTE = [0, 40, 60, 120, 180, 210, 240, 280, 300, 330]

# How long (seconds) a native firmware effect is armed for before we re-arm it
# to keep it looping. Cycles are chosen to fill this window so loops are seamless.
REARM_WINDOW = 4.0


def decode_mode(value):
    """Map the Effect/Mode channel byte (0-255) to a mode name."""
    for threshold, name in EFFECT_RANGES:
        if value < threshold:
            return name
    return "static"


def speed_to_period_ms(speed_byte, fast_ms=80, slow_ms=8000):
    """Map the Speed channel (0=slow .. 255=fast) to an effect period in ms."""
    frac = max(0, min(255, speed_byte)) / 255.0
    return slow_ms + (fast_ms - slow_ms) * frac


def strobe_to_period_ms(strobe_byte, fast_ms=50, slow_ms=1200):
    """Map the Strobe channel (1=slow .. 255=fast) to a flash period in ms."""
    frac = max(1, min(255, strobe_byte)) / 255.0
    return slow_ms + (fast_ms - slow_ms) * frac


def _hsbk_key(hsbk):
    return (round(hsbk[0], 1), round(hsbk[1], 1), round(hsbk[2], 1), int(hsbk[3]))


def native_effect_params(mode, base_hsbk):
    """Return (waveform, target_hsbk, set_flags) for a native waveform effect.

    set_flags is (set_hue, set_saturation, set_brightness, set_kelvin) telling
    the bulb which attributes to oscillate. Returns None for non-native modes.
    """
    h, s, b, k = base_hsbk
    dim = (h, s, 0, k)  # oscillate brightness down to 0
    bright_only = (False, False, True, False)
    if mode == "breathe":
        return WAVEFORM_SINE, dim, bright_only
    if mode == "pulse":
        return WAVEFORM_PULSE, dim, bright_only
    if mode == "triangle":
        return WAVEFORM_TRIANGLE, dim, bright_only
    if mode == "saw":
        return WAVEFORM_SAW, dim, bright_only
    if mode == "color_pulse":
        return WAVEFORM_PULSE, ((h + 180) % 360, 100, b, k), (True, True, False, False)
    return None


def rgba_to_hsbk(r, g, b, a, intensity, kelvin):
    """Convert DMX RGBA + master intensity (each 0-255) to LIFX HSBK.

    Amber is folded into the red/green mix (amber ~ hue 45 deg), then we take
    HSV and scale brightness by the intensity channel (a master dimmer).
    """
    af = a / 255.0
    rf = min(1.0, r / 255.0 + af)
    gf = min(1.0, g / 255.0 + 0.75 * af)  # amber ~ RGB(255,191,0) -> hue 45 deg
    bf = b / 255.0
    h, s, v = colorsys.rgb_to_hsv(rf, gf, bf)
    return h * 360.0, s * 100.0, v * (intensity / 255.0) * 100.0, kelvin


def discover_bulbs(timeout=3.0):
    """Discover bulbs and return a list of {ip, mac, label} dicts."""
    bulbs = []
    for ip, mac in discover(timeout):
        state = get_state(ip)
        bulbs.append({"ip": ip, "mac": mac,
                      "label": state["label"] if state else ""})
    return bulbs


def auto_assign(bulbs, base_universe=0, base_address=1):
    """Assign each bulb a DMX universe/address, packing fixtures sequentially.

    Rolls over to the next universe when a 512-channel universe is full.
    Ordering is deterministic (by label then IP) so the patch is repeatable.
    """
    fixtures = []
    universe, address = base_universe, base_address
    for bulb in sorted(bulbs, key=lambda b: (b.get("label") or "", b["ip"])):
        if address + CHANNELS_PER_FIXTURE - 1 > 512:
            universe += 1
            address = 1
        fixtures.append({**bulb, "universe": universe, "address": address})
        address += CHANNELS_PER_FIXTURE
    return fixtures


def save_map(path, fixtures):
    data = {
        "channels_per_fixture": CHANNELS_PER_FIXTURE,
        "fixtures": [{"label": f.get("label", ""), "ip": f.get("ip", ""),
                      "mac": f.get("mac", ""), "universe": f["universe"],
                      "address": f["address"]} for f in fixtures],
    }
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)


def load_map(path):
    with open(path) as fh:
        return json.load(fh)["fixtures"]


def refresh_ips(fixtures, timeout=2.0):
    """Re-resolve each fixture's live IP by MAC (handles DHCP changes)."""
    by_mac = {mac: ip for ip, mac in discover(timeout)}
    for f in fixtures:
        f["live_ip"] = by_mac.get(f.get("mac"), f.get("ip"))
    return fixtures


def print_fixture_table(fixtures):
    print("DMX fixture map (8ch each: R,G,B,Amber,Intensity,Mode,Speed,Strobe):")
    for f in fixtures:
        name = f.get("label") or "?"
        print(f"  U{f['universe']:<3} addr {f['address']:>3}  {name:24} "
              f"-> {f.get('live_ip') or f.get('ip')}")


def local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        sock.close()


def build_artpoll_reply(node_ip, universe):
    """Minimal ArtPollReply so the bridge shows up as a node to Art-Net tools."""
    pkt = bytearray(239)
    pkt[0:8] = ARTNET_ID
    struct.pack_into("<H", pkt, 8, OP_POLL_REPLY)
    try:
        pkt[10:14] = bytes(int(o) for o in node_ip.split("."))
    except ValueError:
        pass
    struct.pack_into("<H", pkt, 14, ARTNET_PORT)  # port (low byte first)
    pkt[17] = 14                                    # firmware version low
    pkt[18] = (universe >> 8) & 0x7f                # NetSwitch
    pkt[19] = (universe >> 4) & 0x0f                # SubSwitch
    short, long_ = b"LIFX-LAN", b"LIFX LAN Art-Net bridge"
    pkt[26:26 + len(short)] = short
    pkt[44:44 + len(long_)] = long_
    pkt[173] = 1                                     # NumPortsLo = 1
    pkt[174] = 0x80                                  # PortType[0]: DMX output
    pkt[182] = 0x80                                  # GoodOutput[0]: transmitting
    pkt[190] = universe & 0x0f                       # SwOut[0]
    return bytes(pkt)


def decode_controls(dmx, i, kelvin):
    """Read an 8-channel fixture starting at index i into a control dict."""
    r, g, b, a, it = dmx[i], dmx[i + 1], dmx[i + 2], dmx[i + 3], dmx[i + 4]
    return {
        "hsbk": rgba_to_hsbk(r, g, b, a, it, kelvin),
        "intensity": it / 255.0 * 100.0,
        "kelvin": kelvin,
        "mode": decode_mode(dmx[i + 5]),
        "speed": dmx[i + 6],
        "strobe": dmx[i + 7],
    }


def _arm_native(f, waveform, base_hsbk, target_hsbk, flags, period_ms, sig,
                now, verbose, label):
    """(Re)arm a native firmware waveform effect on the bulb if needed."""
    if f.get("armed") == sig and now < f.get("rearm_at", 0):
        return
    ip = f["live_ip"]
    n_cycles = max(1, round(REARM_WINDOW * 1000.0 / period_ms))
    # Establish the base color instantly, then start the oscillation.
    set_color(base_hsbk[0], base_hsbk[1], base_hsbk[2], base_hsbk[3], 0, ip)
    set_waveform_optional(
        target_hsbk[0], target_hsbk[1], target_hsbk[2], target_hsbk[3],
        int(period_ms), n_cycles, waveform, transient=True,
        set_hue=flags[0], set_saturation=flags[1],
        set_brightness=flags[2], set_kelvin=flags[3], ip=ip)
    f["armed"] = sig
    f["rearm_at"] = now + n_cycles * period_ms / 1000.0
    f["last_key"] = None  # force a fresh SetColor when we later leave the effect
    if verbose:
        print(f"[{label}] arm {sig[0]} period={period_ms:.0f}ms cycles={n_cycles}")


def _send_throttled(f, hsbk, now, min_interval, smooth_ms, verbose, label, tag):
    """Send a SetColor for script effects/static, rate-limited and de-duplicated."""
    if now - f["last_send"] < min_interval:
        return
    key = _hsbk_key(hsbk)
    if key == f["last_key"]:
        return
    set_color(hsbk[0], hsbk[1], hsbk[2], hsbk[3], smooth_ms, f["live_ip"])
    f["last_key"], f["last_send"] = key, now
    if verbose:
        print(f"[{label}] {tag} -> H{hsbk[0]:6.1f} S{hsbk[1]:5.1f} B{hsbk[2]:5.1f}")


def service_fixture(f, c, now, min_interval, smooth_ms, verbose):
    """Apply one fixture's current DMX control state to its bulb."""
    label = f.get("label") or f["live_ip"]
    base = c["hsbk"]

    # 1) Strobe (dedicated channel) is an overlay that takes priority.
    if c["strobe"] > 0:
        period = strobe_to_period_ms(c["strobe"])
        target = (base[0], base[1], 0, base[3])
        sig = ("strobe", round(period), _hsbk_key(base))
        _arm_native(f, WAVEFORM_PULSE, base, target, (False, False, True, False),
                    period, sig, now, verbose, label)
        return

    # 2) Native firmware effects (run on the bulb; almost no ongoing traffic).
    native = native_effect_params(c["mode"], base)
    if native is not None:
        waveform, target, flags = native
        period = speed_to_period_ms(c["speed"])
        sig = (c["mode"], round(period), _hsbk_key(base), _hsbk_key(target))
        _arm_native(f, waveform, base, target, flags, period, sig,
                    now, verbose, label)
        return

    # 3) Static + script effects own the color outright; drop any armed effect.
    f["armed"] = None
    intensity, kelvin = c["intensity"], c["kelvin"]

    if c["mode"] == "rainbow":
        if now - f["last_send"] >= min_interval:
            dt = now - f.get("anim_last", now)
            f["anim_last"] = now
            deg_per_s = 6 + (c["speed"] / 255.0) * 174  # 6..180 deg/s
            f["phase"] = (f.get("phase", 0.0) + deg_per_s * dt) % 360
            _send_throttled(f, (f["phase"], 100, intensity, kelvin), now,
                            0, smooth_ms, verbose, label, "rainbow")
    elif c["mode"] == "color_loop":
        period_s = speed_to_period_ms(c["speed"]) / 1000.0
        if now - f.get("step_at", 0) >= period_s:
            f["step_at"] = now
            idx = f.get("step", 0) % len(LOOP_PALETTE)
            f["step"] = idx + 1
            set_color(LOOP_PALETTE[idx], 100, intensity, kelvin,
                      int(period_s * 1000), f["live_ip"])
            if verbose:
                print(f"[{label}] color_loop -> hue {LOOP_PALETTE[idx]}")
    elif c["mode"] == "candle":
        if now - f["last_send"] >= max(min_interval, 0.08):
            bri = max(0.0, min(100.0, intensity * random.uniform(0.55, 1.0)))
            set_color(random.uniform(25, 38), random.uniform(45, 70), bri,
                      kelvin, 120, f["live_ip"])
            f["last_send"] = now
    else:  # static
        _send_throttled(f, base, now, min_interval, smooth_ms, verbose,
                        label, "static")


def listen_artnet(fixtures, max_hz=20.0, smooth_ms=120, kelvin=3500,
                  bind_host="0.0.0.0", poll_reply=True, rediscover=0,
                  verbose=False, state=None, quiet=False):
    """Receive Art-Net DMX and drive each mapped bulb, rate-limited per bulb.

    Optional `state` is a duck-typed object the web UI passes in to control the
    loop while it runs in a background thread. When provided it may expose:
      - stop_event.is_set()    -> stop the loop cleanly
      - paused (bool)          -> receive DMX but don't drive bulbs (manual/blackout)
      - note_dmx(uni, dmx, t)  -> tap for the live DMX monitor
    and individual fixtures may carry f["manual"] = True to be skipped (the web
    UI is driving that bulb directly).
    """
    max_hz = max(1.0, max_hz)
    min_interval = 1.0 / max_hz

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # SO_REUSEPORT lets the bridge share UDP 6454 with another Art-Net app on
    # the same machine (e.g. QLab running on the same Mac). Not on every OS.
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        sock.bind((bind_host, ARTNET_PORT))
    except OSError as exc:
        msg = (f"Could not bind Art-Net port {ARTNET_PORT}: {exc}\n"
               f"Another app is using it. Find it with:  lsof -nP -i UDP:{ARTNET_PORT}\n"
               f"If it's a stale copy of this script:    pkill -f 'lifx_control.*listen'")
        if state is not None:
            raise OSError(msg)  # let the web layer surface it instead of exiting
        sys.exit(msg)
    node_ip = local_ip()

    # Index fixtures by universe and init per-fixture effect state.
    by_universe = {}
    for f in fixtures:
        f.setdefault("live_ip", f.get("ip"))
        f["controls"] = None
        f["last_key"], f["last_send"] = None, 0.0
        f["armed"], f["rearm_at"] = None, 0.0
        f["phase"], f["anim_last"] = 0.0, 0.0
        f["step"], f["step_at"] = 0, 0.0
        by_universe.setdefault(f["universe"], []).append(f)

    if not quiet:
        print_fixture_table(fixtures)
        print(f"\nArt-Net listener on {bind_host}:{ARTNET_PORT}  "
              f"({len(fixtures)} fixture(s) across {len(by_universe)} universe(s))")
        print(f"Rate limit: {max_hz:g} updates/s per bulb. Press Ctrl-C to stop.")

    def stopping():
        return state is not None and getattr(state, "stop_event", None) is not None \
            and state.stop_event.is_set()

    next_rediscover = (time.time() + rediscover) if rediscover else None
    try:
        while not stopping():
            ready, _, _ = select.select([sock], [], [], min_interval)
            now = time.time()
            if ready:
                data, addr = sock.recvfrom(2048)
                if len(data) >= 10 and data[:8] == ARTNET_ID:
                    opcode = struct.unpack_from("<H", data, 8)[0]
                    if opcode == OP_DMX and len(data) >= 18:
                        universe = data[14] | (data[15] << 8)
                        length = (data[16] << 8) | data[17]
                        dmx = data[18:18 + length]
                        if state is not None and hasattr(state, "note_dmx"):
                            state.note_dmx(universe, dmx, now)
                        for f in by_universe.get(universe, ()):
                            i = f["address"] - 1
                            if i + CHANNELS_PER_FIXTURE <= len(dmx):
                                f["controls"] = decode_controls(dmx, i, kelvin)
                    elif opcode == OP_POLL and poll_reply:
                        reply = build_artpoll_reply(
                            node_ip, fixtures[0]["universe"] if fixtures else 0)
                        sock.sendto(reply, (addr[0], ARTNET_PORT))

            # Service every fixture each tick: static colors are throttled, native
            # effects are (re)armed on the bulb, script effects are animated.
            # Skip when globally paused or when the web UI is driving a fixture.
            paused = state is not None and getattr(state, "paused", False)
            if not paused:
                for f in fixtures:
                    if f["controls"] is not None and not f.get("manual"):
                        service_fixture(f, f["controls"], now, min_interval,
                                        smooth_ms, verbose)

            if next_rediscover and now >= next_rediscover:
                refresh_ips(fixtures)
                next_rediscover = now + rediscover
    except KeyboardInterrupt:
        if not quiet:
            print("\nStopped.")
    finally:
        sock.close()


# --- Argument helpers -------------------------------------------------------

def find_by_label(label, timeout=3.0):
    """Find a bulb's IP by its label/name (as set in the LIFX app)."""
    target = label.strip().lower()
    for ip, _mac in discover(timeout):
        state = get_state(ip)
        if state and state["label"].strip().lower() == target:
            return ip
    return None


def resolve_target(args):
    """Resolve which bulb to talk to: --ip wins, then --label, else broadcast."""
    if args.ip:
        return args.ip
    if args.label:
        ip = find_by_label(args.label)
        if not ip:
            sys.exit(f"No bulb found with label '{args.label}'. "
                     f"Run `discover` to see available bulbs.")
        return ip
    return None  # broadcast to all bulbs


def resolve_color(args):
    """Resolve hue/saturation from --name or explicit --hue/--saturation."""
    if args.name:
        if args.name not in COLORS:
            sys.exit(f"Unknown color '{args.name}'. "
                     f"Choices: {', '.join(sorted(COLORS))}")
        hue, sat = COLORS[args.name]
    else:
        hue, sat = args.hue, args.saturation
    return hue, sat


# --- CLI --------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Control a LIFX bulb over the LAN (UDP).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--ip", help="Target one bulb by IP (default: broadcast to all)")
    parser.add_argument("--label", help="Target one bulb by its name/label (from the LIFX app)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_color_args(p, default_bright=100):
        p.add_argument("--name", help=f"Named color: {', '.join(sorted(COLORS))}")
        p.add_argument("--hue", type=float, default=0, help="Hue 0-360")
        p.add_argument("--saturation", type=float, default=100, help="Saturation 0-100")
        p.add_argument("--brightness", type=float, default=default_bright,
                       help="Brightness 0-100")
        p.add_argument("--kelvin", type=int, default=3500,
                       help="White temperature 2500-9000 (used when saturation=0)")

    sub.add_parser("discover", help="Find LIFX bulbs on the network")
    sub.add_parser("state", help="Show the bulb's current state")

    p_on = sub.add_parser("on", help="Turn the bulb on")
    p_on.add_argument("--duration", type=float, default=0, help="Fade time in seconds")
    p_off = sub.add_parser("off", help="Turn the bulb off")
    p_off.add_argument("--duration", type=float, default=0, help="Fade time in seconds")

    p_color = sub.add_parser("color", help="Set a color (with optional fade)")
    add_color_args(p_color)
    p_color.add_argument("--duration", type=float, default=0,
                         help="Fade time in seconds (fade-in/out/transition)")

    p_white = sub.add_parser("white", help="Set white at a color temperature")
    p_white.add_argument("--kelvin", type=int, default=3500, help="2500-9000")
    p_white.add_argument("--brightness", type=float, default=100, help="0-100")
    p_white.add_argument("--duration", type=float, default=0, help="Fade time in seconds")

    p_bri = sub.add_parser("brightness", help="Change brightness only")
    p_bri.add_argument("--brightness", type=float, required=True, help="0-100")
    p_bri.add_argument("--duration", type=float, default=0, help="Fade time in seconds")

    p_pulse = sub.add_parser("pulse", help="Pulse effect (sharp on/off blink)")
    add_color_args(p_pulse)
    p_pulse.add_argument("--period", type=float, default=1, help="Seconds per cycle")
    p_pulse.add_argument("--cycles", type=float, default=5, help="Number of cycles")

    p_breathe = sub.add_parser("breathe", help="Breathe effect (smooth fade in/out)")
    add_color_args(p_breathe)
    p_breathe.add_argument("--period", type=float, default=2, help="Seconds per cycle")
    p_breathe.add_argument("--cycles", type=float, default=5, help="Number of cycles")

    p_map = sub.add_parser("dmxmap",
                           help="Discover bulbs and write an editable DMX fixture map")
    p_map.add_argument("--out", default="fixtures.json", help="Output file (default fixtures.json)")
    p_map.add_argument("--universe", type=int, default=0, help="Base Art-Net universe (default 0)")
    p_map.add_argument("--address", type=int, default=1, help="Base DMX address (default 1)")

    p_listen = sub.add_parser("listen",
                              help="Run as an Art-Net DMX node so QLab Light cues drive the bulbs")
    p_listen.add_argument("--map", help="Fixture map JSON from `dmxmap` (default: auto-discover & assign)")
    p_listen.add_argument("--universe", type=int, default=0, help="Base universe for auto-assign (default 0)")
    p_listen.add_argument("--address", type=int, default=1, help="Base DMX address for auto-assign (default 1)")
    p_listen.add_argument("--kelvin", type=int, default=3500, help="White temperature when color is desaturated")
    p_listen.add_argument("--max-hz", type=float, default=20, help="Max LIFX updates/sec per bulb (default 20)")
    p_listen.add_argument("--smooth", type=float, default=0.12, help="LIFX transition per update, seconds (default 0.12)")
    p_listen.add_argument("--bind", default="0.0.0.0", help="Local interface to bind for Art-Net")
    p_listen.add_argument("--rediscover", type=float, default=0, help="Re-resolve bulb IPs every N seconds (0=off)")
    p_listen.add_argument("--no-poll-reply", action="store_true", help="Don't answer ArtPoll discovery")
    p_listen.add_argument("--verbose", action="store_true", help="Print DMX -> HSBK updates")

    args = parser.parse_args(argv)

    if args.command == "discover":
        bulbs = discover()
        if not bulbs:
            print("No LIFX bulbs found. Make sure the bulb is powered and on "
                  "the same network/subnet.")
            return
        print(f"Found {len(bulbs)} bulb(s):")
        for bulb_ip, mac in bulbs:
            # Read each bulb's label so the user knows what to pass to --label.
            state = get_state(bulb_ip)
            label = f'"{state["label"]}"' if state and state["label"] else "?"
            print(f"  {bulb_ip}  {label:24} (MAC {mac})")
        return

    if args.command == "dmxmap":
        bulbs = discover_bulbs()
        if not bulbs:
            print("No bulbs found to map. Check that they're powered and on "
                  "the same network.")
            return
        fixtures = auto_assign(bulbs, args.universe, args.address)
        for f in fixtures:
            f["live_ip"] = f["ip"]
        print_fixture_table(fixtures)
        save_map(args.out, fixtures)
        print(f"\nWrote {len(fixtures)} fixture(s) to {args.out} "
              f"(edit it to fix addresses/ordering, then `listen --map {args.out}`).")
        return

    if args.command == "listen":
        if args.map:
            fixtures = load_map(args.map)
            refresh_ips(fixtures)
        else:
            bulbs = discover_bulbs()
            if not bulbs:
                sys.exit("No bulbs found to map. Check the network, or pass "
                         "--map with a saved fixture file.")
            fixtures = auto_assign(bulbs, args.universe, args.address)
            for f in fixtures:
                f["live_ip"] = f["ip"]
        listen_artnet(fixtures, args.max_hz, int(args.smooth * 1000), args.kelvin,
                      args.bind, not args.no_poll_reply, args.rediscover,
                      args.verbose)
        return

    # All other commands target a single bulb (--ip/--label) or broadcast.
    ip = resolve_target(args)

    if args.command == "state":
        state = get_state(ip)
        if not state:
            print("No response from bulb. Try `discover` and pass --ip.")
            return
        for key, value in state.items():
            print(f"  {key:12}: {value}")

    elif args.command in ("on", "off"):
        set_power(args.command == "on", int(args.duration * 1000), ip)
        print(f"Turned {args.command}" +
              (f" over {args.duration}s" if args.duration else ""))

    elif args.command == "color":
        hue, sat = resolve_color(args)
        set_color(hue, sat, args.brightness, args.kelvin,
                  int(args.duration * 1000), ip)
        print(f"Set color (hue={hue}, sat={sat}, bright={args.brightness})" +
              (f" over {args.duration}s" if args.duration else ""))

    elif args.command == "white":
        set_color(0, 0, args.brightness, args.kelvin,
                  int(args.duration * 1000), ip)
        print(f"Set white {args.kelvin}K at {args.brightness}% brightness")

    elif args.command == "brightness":
        # Re-use the current hue/sat/kelvin; only change brightness.
        current = get_state(ip)
        if current:
            set_color(current["hue"], current["saturation"], args.brightness,
                      current["kelvin"], int(args.duration * 1000), ip)
        else:
            # Fall back to a neutral white if we couldn't read the state.
            set_color(0, 0, args.brightness, 3500, int(args.duration * 1000), ip)
        print(f"Set brightness to {args.brightness}%" +
              (f" over {args.duration}s" if args.duration else ""))

    elif args.command in ("pulse", "breathe"):
        hue, sat = resolve_color(args)
        waveform = WAVEFORM_PULSE if args.command == "pulse" else WAVEFORM_SINE
        set_waveform(hue, sat, args.brightness, args.kelvin,
                     int(args.period * 1000), args.cycles, waveform, ip=ip)
        print(f"Running {args.command}: {args.cycles} cycle(s), "
              f"{args.period}s each")


if __name__ == "__main__":
    main()
