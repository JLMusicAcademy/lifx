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
