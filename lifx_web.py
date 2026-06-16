#!/usr/bin/env python3
"""Web configuration & control UI for the LIFX Art-Net bridge.

Runs a small stdlib HTTP server (no third-party dependencies) that:
  - serves a login-protected web UI,
  - discovers LIFX bulbs and manages the DMX fixture map (rename, re-address,
    identify/flash, conflict-check, patch-sheet export),
  - lets you control bulbs directly (color/intensity/effects) from a browser
    or phone, independent of QLab,
  - runs the Art-Net listener in a background thread and shows live DMX in,
  - configures the host's IP (DHCP/static) on a Raspberry Pi via nmcli, with a
    subnet scan to find free addresses,
  - resets its own password if the service is restarted 3x in a row.

Designed to run headless on a Raspberry Pi 5 next to QLab on the network.
"""

import concurrent.futures
import hashlib
import hmac
import html
import http.server
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import urllib.parse

import lifx_control as lifx

# --- Configuration & paths --------------------------------------------------

CONFIG_DIR = os.environ.get(
    "LIFX_BRIDGE_DIR", os.path.expanduser("~/.lifx-bridge"))
AUTH_FILE = os.path.join(CONFIG_DIR, "auth.json")
MAP_FILE = os.path.join(CONFIG_DIR, "map.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
SCENES_FILE = os.path.join(CONFIG_DIR, "scenes.json")
RESTART_FILE = os.path.join(CONFIG_DIR, "restart.json")

DEFAULT_USER = "admin"
DEFAULT_PASS = "admin123"
PBKDF2_ITERS = 200_000
SESSION_TTL = 12 * 3600

# Restart-to-reset: N restarts each within WINDOW seconds of the previous one
# resets the password. WINDOW must exceed a single Pi boot (~20-35s), so the
# gap between two consecutive boots fits inside it. Default 60s, configurable.
RESTART_WINDOW = float(os.environ.get("LIFX_RESTART_WINDOW", "60"))
RESTART_NEEDED = int(os.environ.get("LIFX_RESTART_NEEDED", "3"))

DEFAULT_SETTINGS = {
    "web_port": 8080,
    "max_hz": 20.0,
    "smooth_ms": 120,
    "kelvin": 3500,
    "bind_host": "0.0.0.0",
    "rediscover": 0,
    "autostart": True,
}


def ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# --- Authentication ---------------------------------------------------------

def hash_password(password, salt=None, iters=PBKDF2_ITERS):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iters)
    return {"salt": salt, "hash": dk.hex(), "iters": iters}


def set_password(username, password):
    rec = hash_password(password)
    rec["user"] = username
    save_json(AUTH_FILE, rec)


def load_auth():
    auth = load_json(AUTH_FILE, None)
    if not auth:
        set_password(DEFAULT_USER, DEFAULT_PASS)
        auth = load_json(AUTH_FILE, None)
    return auth


def verify_login(username, password):
    auth = load_auth()
    if username != auth.get("user"):
        return False
    calc = hashlib.pbkdf2_hmac(
        "sha256", password.encode(),
        bytes.fromhex(auth["salt"]), auth["iters"]).hex()
    return hmac.compare_digest(calc, auth["hash"])


def reset_password_to_default():
    set_password(DEFAULT_USER, DEFAULT_PASS)


def record_restart_and_maybe_reset():
    """Append this start; reset password if RESTART_NEEDED happened in a row."""
    data = load_json(RESTART_FILE, {"starts": []})
    now = time.time()
    starts = sorted(float(t) for t in data.get("starts", []))
    starts.append(now)
    # Count the trailing run of starts each within RESTART_WINDOW of the prior.
    count = 1
    for i in range(len(starts) - 1, 0, -1):
        if starts[i] - starts[i - 1] <= RESTART_WINDOW:
            count += 1
        else:
            break
    streak = starts[len(starts) - count:]
    reset = count >= RESTART_NEEDED
    if reset:
        reset_password_to_default()
        streak = []  # consume the streak so it doesn't immediately re-trigger
    save_json(RESTART_FILE, {"starts": streak})
    return reset, count


# --- Session store ----------------------------------------------------------

class Sessions:
    def __init__(self):
        self._tokens = {}
        self._lock = threading.Lock()

    def create(self):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = time.time() + SESSION_TTL
        return token

    def valid(self, token):
        if not token:
            return False
        with self._lock:
            exp = self._tokens.get(token)
            if exp and exp > time.time():
                return True
            self._tokens.pop(token, None)
        return False

    def drop(self, token):
        with self._lock:
            self._tokens.pop(token, None)


# --- Host network configuration (Raspberry Pi / NetworkManager) -------------

def _have_nmcli():
    return shutil.which("nmcli") is not None and platform.system() == "Linux"


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def network_status():
    """Return the active connection's IP config (best effort)."""
    info = {"supported": _have_nmcli(), "local_ip": lifx.local_ip(),
            "system": platform.system()}
    if not info["supported"]:
        info["note"] = ("Host IP editing needs NetworkManager (nmcli) on Linux. "
                        "On this machine you can still scan for free addresses.")
        return info
    rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,DEVICE,TYPE", "connection",
                       "show", "--active"])
    con = dev = None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[2] in ("802-3-ethernet", "802-11-wireless"):
            con, dev = parts[0], parts[1]
            break
    info["connection"], info["device"] = con, dev
    if con:
        rc, out, _ = _run(["nmcli", "-t", "-f",
                           "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
                           "connection", "show", con])
        for line in out.splitlines():
            k, _, v = line.partition(":")
            if k == "ipv4.method":
                info["method"] = v
            elif k == "ipv4.addresses":
                info["addresses"] = v
            elif k == "ipv4.gateway":
                info["gateway"] = v
            elif k == "ipv4.dns":
                info["dns"] = v
    return info


def network_set_dhcp():
    if not _have_nmcli():
        return False, "nmcli not available on this host."
    con = network_status().get("connection")
    if not con:
        return False, "No active network connection found."
    for cmd in (["nmcli", "con", "mod", con, "ipv4.method", "auto",
                 "ipv4.gateway", "", "ipv4.addresses", "", "ipv4.dns", ""],
                ["nmcli", "con", "up", con]):
        rc, _, err = _run(cmd)
        if rc != 0:
            return False, err or "nmcli command failed (need root?)."
    return True, "Switched to DHCP. The IP may change; reconnect if needed."


def network_set_static(ip, prefix, gateway, dns):
    if not _have_nmcli():
        return False, "nmcli not available on this host."
    try:
        ipaddress.ip_address(ip)
        prefix = int(prefix)
        assert 1 <= prefix <= 32
    except (ValueError, AssertionError):
        return False, "Invalid IP address or prefix."
    con = network_status().get("connection")
    if not con:
        return False, "No active network connection found."
    dns = dns or "1.1.1.1 8.8.8.8"
    cmds = [["nmcli", "con", "mod", con,
             "ipv4.method", "manual",
             "ipv4.addresses", f"{ip}/{prefix}",
             "ipv4.gateway", gateway or "",
             "ipv4.dns", dns],
            ["nmcli", "con", "up", con]]
    for cmd in cmds:
        rc, _, err = _run(cmd)
        if rc != 0:
            return False, err or "nmcli command failed (need root?)."
    return True, f"Static IP set to {ip}/{prefix}. Reconnect to the new address."


def _ping(ip):
    if platform.system() == "Windows":
        cmd = ["ping", "-n", "1", "-w", "1000", ip]
    elif platform.system() == "Darwin":
        cmd = ["ping", "-c", "1", "-t", "1", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    rc, _, _ = _run(cmd, timeout=3)
    return ip if rc == 0 else None


def scan_subnet(base_ip=None):
    """Ping-sweep the local /24 and return used + suggested free addresses."""
    base_ip = base_ip or lifx.local_ip()
    try:
        net = ipaddress.ip_network(base_ip + "/24", strict=False)
    except ValueError:
        return {"error": "Could not determine local subnet."}
    hosts = [str(h) for h in net.hosts()]
    used = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for res in ex.map(_ping, hosts):
            if res:
                used.add(res)
    # Fold in the ARP table for hosts that ignore ping.
    rc, out, _ = _run(["arp", "-a"])
    for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)", out):
        if m.group(1) in hosts:
            used.add(m.group(1))
    free = [h for h in hosts if h not in used]
    # Suggest free addresses from the upper part of the range (less likely DHCP).
    suggestions = [h for h in free if int(h.split(".")[-1]) >= 200][:10] or free[:10]
    return {"subnet": str(net), "this_host": base_ip,
            "used": sorted(used, key=lambda x: tuple(int(o) for o in x.split("."))),
            "free_count": len(free), "suggestions": suggestions}


# --- Bridge (Art-Net listener) management -----------------------------------

class BridgeState:
    """Shared object the listener thread reads; the web layer writes."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.paused = False
        self.lock = threading.Lock()
        self.last_dmx = {}
        self.packets = 0
        self.last_packet_t = 0.0

    def note_dmx(self, universe, dmx, t):
        with self.lock:
            self.last_dmx[universe] = list(dmx[:512])
            self.packets += 1
            self.last_packet_t = t

    def monitor(self):
        with self.lock:
            return {"packets": self.packets,
                    "last_packet_age": (time.time() - self.last_packet_t
                                        if self.last_packet_t else None),
                    "universes": {str(u): v[:64] for u, v in self.last_dmx.items()}}


class Manager:
    """Owns the fixture map, settings, and the listener thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
        self.fixtures = self._load_map()
        self.state = None
        self.thread = None
        self.bind_error = None

    # Only these fields belong in the saved map. Everything else on a fixture
    # dict (manual, live_ip, controls, last_send, armed, ...) is runtime state
    # and must NOT be persisted, or stale flags survive restarts.
    STABLE_FIELDS = ("label", "ip", "mac", "universe", "address", "group")

    def _load_map(self):
        data = load_json(MAP_FILE, {"fixtures": []})
        fixtures = []
        for f in data.get("fixtures", []):
            clean = {k: f.get(k) for k in self.STABLE_FIELDS}
            clean["group"] = clean.get("group") or ""
            clean["live_ip"] = f.get("live_ip") or f.get("ip")
            fixtures.append(clean)
        return fixtures

    def save(self):
        fixtures = [{k: f.get(k) for k in self.STABLE_FIELDS} for f in self.fixtures]
        save_json(MAP_FILE, {"channels_per_fixture": lifx.CHANNELS_PER_FIXTURE,
                             "fixtures": fixtures})

    def save_settings(self):
        save_json(SETTINGS_FILE, self.settings)

    # -- listener thread --
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        with self.lock:
            if self.running() or not self.fixtures:
                return
            self.bind_error = None
            self.state = BridgeState()
            s = self.settings

            def run():
                try:
                    lifx.listen_artnet(
                        self.fixtures, max_hz=s["max_hz"], smooth_ms=s["smooth_ms"],
                        kelvin=s["kelvin"], bind_host=s["bind_host"],
                        rediscover=s["rediscover"], state=self.state, quiet=True)
                except OSError as exc:
                    self.bind_error = str(exc)

            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()

    def stop(self):
        if self.state:
            self.state.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.thread = None

    def restart(self):
        self.stop()
        time.sleep(0.3)
        self.start()

    # -- fixture operations --
    def find(self, fid):
        for f in self.fixtures:
            if f.get("mac") == fid or f.get("ip") == fid:
                return f
        return None

    def discover(self, timeout=3.0):
        found = lifx.discover_bulbs(timeout)
        known = {f.get("mac"): f for f in self.fixtures}
        added = 0
        for b in found:
            if b["mac"] in known:
                known[b["mac"]]["live_ip"] = b["ip"]
                known[b["mac"]]["ip"] = b["ip"]
                if b.get("label"):  # pick up renames made in the LIFX app
                    known[b["mac"]]["label"] = b["label"]
            else:
                self.fixtures.append({**b, "group": "", "universe": 0,
                                      "address": 0, "live_ip": b["ip"]})
                added += 1
        if added:
            self.auto_assign(only_unassigned=True)
        self.save()
        return len(found), added

    def auto_assign(self, only_unassigned=False, base_universe=0, base_address=1):
        if only_unassigned:
            assigned = [f for f in self.fixtures if f.get("address")]
            pending = [f for f in self.fixtures if not f.get("address")]
            # Resume packing after the highest used address.
            uni, addr = base_universe, base_address
            for f in assigned:
                uni = max(uni, f["universe"])
            for f in assigned:
                if f["universe"] == uni:
                    addr = max(addr, f["address"] + lifx.CHANNELS_PER_FIXTURE)
            for f in pending:
                if addr + lifx.CHANNELS_PER_FIXTURE - 1 > 512:
                    uni += 1
                    addr = 1
                f["universe"], f["address"] = uni, addr
                addr += lifx.CHANNELS_PER_FIXTURE
        else:
            packed = lifx.auto_assign(self.fixtures, base_universe, base_address)
            by_mac = {f["mac"]: f for f in packed}
            for f in self.fixtures:
                p = by_mac.get(f.get("mac"))
                if p:
                    f["universe"], f["address"] = p["universe"], p["address"]
        self.save()

    def conflicts(self):
        """Return list of (universe, address) collisions and overflow errors."""
        seen, issues = {}, []
        for f in self.fixtures:
            if not f.get("address"):
                continue
            end = f["address"] + lifx.CHANNELS_PER_FIXTURE - 1
            if end > 512:
                issues.append(f"{f.get('label') or f['ip']} overflows universe "
                              f"{f['universe']} (ends at ch {end})")
            for ch in range(f["address"], end + 1):
                key = (f["universe"], ch)
                if key in seen:
                    issues.append(f"U{f['universe']} ch{ch}: "
                                  f"{seen[key]} & {f.get('label') or f['ip']}")
                seen[key] = f.get("label") or f["ip"]
        return issues

    def identify(self, f):
        ip = f.get("live_ip") or f.get("ip")
        prev_manual = f.get("manual", False)
        f["manual"] = True

        def run():
            try:
                st = lifx.get_state(ip)
                lifx.set_power(True, 0, ip)  # SetColor won't show on a bulb that's off
                for _ in range(6):
                    lifx.set_color(55, 100, 100, 5000, 0, ip)
                    time.sleep(0.22)
                    lifx.set_color(0, 0, 2, 3500, 0, ip)
                    time.sleep(0.22)
                if st:  # restore the bulb's previous color and power state
                    lifx.set_color(st["hue"], st["saturation"], st["brightness"],
                                   st["kelvin"], 300, ip)
                    lifx.set_power(st["power"] == "on", 300, ip)
            except OSError as exc:
                print(f"[web] identify {ip} failed: {exc} (bulb unreachable?)")
            finally:
                f["manual"] = prev_manual

        threading.Thread(target=run, daemon=True).start()

    def manual_control(self, f, body):
        """Drive a bulb directly from the UI; takes it off Art-Net until released."""
        ip = f.get("live_ip") or f.get("ip")
        f["manual"] = True
        mode = body.get("mode", "static")
        intensity = int(body.get("intensity", 255))
        hsbk = lifx.rgba_to_hsbk(
            int(body.get("r", 0)), int(body.get("g", 0)), int(body.get("b", 0)),
            int(body.get("a", 0)), intensity, self.settings["kelvin"])
        smooth = self.settings["smooth_ms"]
        # A bulb whose power is OFF (Blackout, or the LIFX app) ignores SetColor,
        # so an explicit user action must also turn it on (or off at intensity 0).
        lifx.set_power(intensity > 0, smooth, ip)
        c = {"hsbk": hsbk, "intensity": intensity / 255 * 100,
             "kelvin": self.settings["kelvin"], "mode": mode,
             "speed": int(body.get("speed", 128)), "strobe": int(body.get("strobe", 0))}
        # Force a fresh, un-throttled send for an explicit click (no de-dup).
        f["last_send"], f["last_key"], f["armed"] = 0.0, None, None
        for k in ("rearm_at", "phase", "anim_last", "step", "step_at"):
            if not isinstance(f.get(k), (int, float)):
                f[k] = 0.0
        lifx.service_fixture(f, c, time.time(), 0.0, smooth, False)
        print(f"[web] manual {mode} intensity={intensity} -> {ip}")
        return ip

    def release(self, f):
        f["manual"] = False

    def blackout(self, on):
        """on=True: pause Art-Net and power every bulb off.
        on=False (Resume): un-pause and power every bulb back on."""
        if self.state:
            self.state.paused = on
        for f in self.fixtures:
            ip = f.get("live_ip") or f.get("ip")
            if ip:
                try:
                    lifx.set_power(not on, 400, ip)
                except OSError:
                    pass


MGR = Manager()
SESSIONS = Sessions()


# --- HTTP handler -----------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "LIFXBridge/1.0"

    def log_message(self, *a):
        pass  # quiet

    # -- helpers --
    def _cookie_token(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "session":
                return v
        return None

    def _authed(self):
        return SESSIONS.valid(self._cookie_token())

    def _send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    # -- routing --
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._send(200, LOGIN_HTML if not self._authed() else APP_HTML,
                              "text/html")
        if path == "/api/patch.csv":
            if not self._authed():
                return self._send(401, {"error": "auth"})
            return self._send(200, self._patch_csv(), "text/csv",
                              {"Content-Disposition": "attachment; "
                               "filename=lifx_patch.csv"})
        if path == "/api/state":
            if not self._authed():
                return self._send(401, {"error": "auth"})
            return self._send(200, self._state_payload())
        if path == "/api/network":
            if not self._authed():
                return self._send(401, {"error": "auth"})
            return self._send(200, network_status())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._json_body()

        if path == "/api/login":
            if verify_login(body.get("username", ""), body.get("password", "")):
                token = SESSIONS.create()
                return self._send(200, {"ok": True}, headers={
                    "Set-Cookie": f"session={token}; HttpOnly; SameSite=Lax; Path=/"})
            return self._send(401, {"error": "Invalid username or password."})

        if path == "/api/logout":
            SESSIONS.drop(self._cookie_token())
            return self._send(200, {"ok": True}, headers={
                "Set-Cookie": "session=; Max-Age=0; Path=/"})

        if not self._authed():
            return self._send(401, {"error": "auth"})

        try:
            return self._dispatch_post(path, body)
        except Exception as exc:  # surface errors to the UI rather than 500-blank
            return self._send(200, {"ok": False, "error": str(exc)})

    def _dispatch_post(self, path, body):
        if path == "/api/change-password":
            if not verify_login(load_auth()["user"], body.get("current", "")):
                return self._send(200, {"ok": False, "error": "Current password is wrong."})
            new = body.get("new", "")
            if len(new) < 6:
                return self._send(200, {"ok": False,
                                        "error": "New password must be 6+ characters."})
            set_password(load_auth()["user"], new)
            return self._send(200, {"ok": True})

        if path == "/api/discover":
            found, added = MGR.discover(float(body.get("timeout", 3)))
            return self._send(200, {"ok": True, "found": found, "added": added})

        if path == "/api/fixture/rename":
            f = MGR.find(body.get("id"))
            if f:
                name = body.get("label", "")[:32]
                f["label"] = name
                try:  # write the name onto the bulb itself (best effort)
                    lifx.set_label(f.get("live_ip") or f.get("ip"), name)
                except OSError:
                    pass
                MGR.save()
            return self._send(200, {"ok": bool(f)})

        if path == "/api/fixture/address":
            f = MGR.find(body.get("id"))
            if f:
                f["universe"] = max(0, int(body.get("universe", 0)))
                f["address"] = max(0, min(512, int(body.get("address", 0))))
                f["group"] = body.get("group", f.get("group", ""))[:32]
                MGR.save()
            return self._send(200, {"ok": bool(f), "conflicts": MGR.conflicts()})

        if path == "/api/fixture/identify":
            f = MGR.find(body.get("id"))
            if f:
                MGR.identify(f)
            return self._send(200, {"ok": bool(f)})

        if path == "/api/fixture/control":
            f = MGR.find(body.get("id"))
            if not f:
                return self._send(200, {"ok": False, "error": "Unknown fixture."})
            try:
                ip = MGR.manual_control(f, body)
            except OSError as exc:
                ip = f.get("live_ip") or f.get("ip")
                return self._send(200, {"ok": False, "error":
                    f"{ip} unreachable ({exc}). The bulb may be offline or its "
                    f"IP changed — try Discover bulbs to refresh."})
            return self._send(200, {"ok": True, "ip": ip})

        if path == "/api/fixture/release":
            f = MGR.find(body.get("id"))
            if f:
                MGR.release(f)
            return self._send(200, {"ok": bool(f)})

        if path == "/api/release-all":
            for f in MGR.fixtures:
                f["manual"] = False
            return self._send(200, {"ok": True})

        if path == "/api/fixture/remove":
            f = MGR.find(body.get("id"))
            if f:
                MGR.fixtures.remove(f)
                MGR.save()
            return self._send(200, {"ok": bool(f)})

        if path == "/api/auto-assign":
            uni = max(0, int(body.get("universe") or 0))
            addr = min(512, max(1, int(body.get("address") or 1)))
            MGR.auto_assign(only_unassigned=False,
                            base_universe=uni, base_address=addr)
            return self._send(200, {"ok": True, "conflicts": MGR.conflicts()})

        if path == "/api/blackout":
            MGR.blackout(bool(body.get("on", True)))
            return self._send(200, {"ok": True, "paused": bool(body.get("on", True))})

        if path == "/api/listener":
            action = body.get("action")
            if action == "start":
                MGR.start()
            elif action == "stop":
                MGR.stop()
            elif action == "restart":
                MGR.restart()
            return self._send(200, {"ok": True, "running": MGR.running(),
                                    "error": MGR.bind_error})

        if path == "/api/settings":
            for key in ("max_hz", "smooth_ms", "kelvin", "rediscover", "web_port"):
                if key in body:
                    MGR.settings[key] = type(DEFAULT_SETTINGS[key])(body[key])
            if "bind_host" in body:
                MGR.settings["bind_host"] = body["bind_host"]
            MGR.save_settings()
            return self._send(200, {"ok": True, "settings": MGR.settings,
                                    "note": "Restart the listener to apply."})

        if path == "/api/fixture/info":
            f = MGR.find(body.get("id"))
            if not f:
                return self._send(200, {"ok": False, "error": "Unknown fixture."})
            ip = f.get("live_ip") or f.get("ip")
            return self._send(200, {"ok": True, "info": lifx.device_info(ip)})

        if path == "/api/fixture/push-name":
            f = MGR.find(body.get("id"))
            if f:
                name = body.get("label", "")[:32]
                lifx.set_label(f.get("live_ip") or f.get("ip"), name)
                f["label"] = name
                MGR.save()
            return self._send(200, {"ok": bool(f)})

        if path == "/api/fixture/group":
            f = MGR.find(body.get("id"))
            if f:
                ip = f.get("live_ip") or f.get("ip")
                if body.get("group"):
                    lifx.set_group(ip, body["group"][:32])
                    f["group"] = body["group"][:32]
                if body.get("location"):
                    lifx.set_location(ip, body["location"][:32])
                MGR.save()
            return self._send(200, {"ok": bool(f)})

        if path == "/api/network/dhcp":
            ok, msg = network_set_dhcp()
            return self._send(200, {"ok": ok, "message": msg})

        if path == "/api/network/static":
            ok, msg = network_set_static(
                body.get("ip", ""), body.get("prefix", 24),
                body.get("gateway", ""), body.get("dns", ""))
            return self._send(200, {"ok": ok, "message": msg})

        if path == "/api/network/scan":
            return self._send(200, scan_subnet(body.get("base") or None))

        return self._send(404, {"error": "not found"})

    # -- payload builders --
    def _state_payload(self):
        fixtures = [{
            "id": f.get("mac") or f.get("ip"),
            "label": f.get("label", ""), "ip": f.get("live_ip") or f.get("ip"),
            "mac": f.get("mac", ""), "group": f.get("group", ""),
            "universe": f.get("universe", 0), "address": f.get("address", 0),
            "manual": bool(f.get("manual")),
        } for f in MGR.fixtures]
        return {
            "fixtures": fixtures,
            "conflicts": MGR.conflicts(),
            "channels_per_fixture": lifx.CHANNELS_PER_FIXTURE,
            "settings": MGR.settings,
            "listener": {"running": MGR.running(), "error": MGR.bind_error,
                         "port": lifx.ARTNET_PORT,
                         "port_free": port_is_free(lifx.ARTNET_PORT)},
            "monitor": MGR.state.monitor() if MGR.state else {},
            "effects": [{"range": f"{lo}", "name": name}
                        for lo, name in [(0, "static")] +
                        [(t, n) for t, n in lifx.EFFECT_RANGES]],
        }

    def _patch_csv(self):
        rows = ["Name,IP,MAC,Universe,StartAddress,Channels,Group"]
        for f in sorted(MGR.fixtures, key=lambda x: (x.get("universe", 0),
                                                     x.get("address", 0))):
            rows.append(",".join(str(x) for x in [
                f.get("label", ""), f.get("ip", ""), f.get("mac", ""),
                f.get("universe", 0), f.get("address", 0),
                lifx.CHANNELS_PER_FIXTURE, f.get("group", "")]))
        return "\n".join(rows)


def port_is_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# --- Frontend (login + single-page app) -------------------------------------

_STYLE = """
<style>
/* Mobile-first, light, soft & rounded. Larger tap targets & type. */
:root{
  --bg:#eef2f9; --card:#ffffff; --ink:#1e2533; --muted:#6b7488;
  --brand:#4f7cff; --brand-d:#3a67f0; --ok:#13a06a; --bad:#e44b3f;
  --line:#e7ebf3; --radius:18px;
  --shadow:0 8px 24px rgba(36,46,66,.10), 0 2px 6px rgba(36,46,66,.06);
  --shadow-sm:0 2px 8px rgba(36,46,66,.10);
}
*{box-sizing:border-box}
html{font-size:17px;-webkit-text-size-adjust:100%}
body{margin:0;line-height:1.5;color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
  background:linear-gradient(180deg,#f3f6fc,#e9eef7 240px) fixed,var(--bg)}
header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;
  padding:12px 14px;background:rgba(255,255,255,.82);backdrop-filter:saturate(180%) blur(12px);
  border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:18px;font-weight:800;letter-spacing:-.02em;
  background:linear-gradient(90deg,var(--brand),#7a5cff);-webkit-background-clip:text;
  background-clip:text;color:transparent;white-space:nowrap}
.iconbtn{background:#f1f4fb;border:1px solid var(--line);border-radius:12px;
  width:46px;height:46px;font-size:22px;line-height:1;cursor:pointer;color:var(--ink);
  display:flex;align-items:center;justify-content:center;box-shadow:var(--shadow-sm)}
.iconbtn:active{transform:translateY(1px)}
#drawer{position:fixed;top:0;left:0;height:100%;width:268px;max-width:82vw;z-index:40;
  background:var(--card);border-right:1px solid var(--line);box-shadow:var(--shadow);
  transform:translateX(-100%);transition:transform .22s ease;padding:14px 12px;
  display:flex;flex-direction:column;gap:6px;overflow-y:auto}
#drawer.open{transform:translateX(0)}
#drawer .brand{font-size:17px;font-weight:800;letter-spacing:-.02em;padding:8px 14px 12px;
  color:var(--ink)}
#drawer button{width:100%;text-align:left;background:transparent;border:0;color:var(--ink);
  padding:14px 16px;border-radius:12px;font-size:17px;font-weight:600;cursor:pointer;
  min-height:50px}
#drawer button:hover{background:#f1f4fb}
#drawer button.active{background:linear-gradient(180deg,var(--brand),var(--brand-d));
  color:#fff;box-shadow:0 6px 16px rgba(79,124,255,.4)}
#drawer button.signout{margin-top:auto;color:var(--bad);border-top:1px solid var(--line);
  border-radius:0 0 12px 12px}
#drawer button.signout:hover{background:#fdecea}
#backdrop{position:fixed;inset:0;background:rgba(20,28,46,.42);z-index:30;opacity:0;
  pointer-events:none;transition:opacity .22s}
#backdrop.open{opacity:1;pointer-events:auto}
main{padding:16px;max-width:980px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:18px;margin-bottom:16px;box-shadow:var(--shadow)}
h2{font-size:17px;margin:0 0 14px;font-weight:700}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.flex{display:flex;gap:14px;flex-wrap:wrap}
button{font-family:inherit}
button.act,button.ghost,button.warn,a.act{
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  border:0;border-radius:14px;padding:13px 20px;font-size:16px;font-weight:650;
  cursor:pointer;min-height:48px;transition:transform .06s ease, box-shadow .2s,filter .2s}
button.act,a.act{background:linear-gradient(180deg,var(--brand),var(--brand-d));color:#fff;
  box-shadow:0 6px 16px rgba(79,124,255,.38);text-decoration:none}
button.ghost{background:#fff;color:var(--ink);border:1px solid var(--line);
  box-shadow:var(--shadow-sm)}
button.warn{background:linear-gradient(180deg,#ff6a5d,var(--bad));color:#fff;
  box-shadow:0 6px 16px rgba(228,75,63,.34)}
button.act:hover,a.act:hover,button.warn:hover{filter:brightness(1.05)}
button:active{transform:translateY(1px)}
input,select{font-family:inherit;font-size:16px;padding:12px 13px;border-radius:13px;
  border:1px solid #d8deea;background:#fff;color:var(--ink);width:100%;
  box-shadow:inset 0 1px 2px rgba(36,46,66,.05)}
input:focus,select:focus{outline:none;border-color:var(--brand);
  box-shadow:0 0 0 4px rgba(79,124,255,.16)}
input[type=range]{padding:0;box-shadow:none;accent-color:var(--brand)}
.wheel{width:172px;height:172px;border-radius:50%;position:relative;cursor:crosshair;
  touch-action:none;background:
    radial-gradient(circle at center,#fff 0%,rgba(255,255,255,0) 72%),
    conic-gradient(from 0deg,red,#ff0,#0f0,#0ff,#00f,#f0f,red);
  box-shadow:inset 0 0 0 1px rgba(0,0,0,.08),var(--shadow-sm)}
.wmark{position:absolute;left:50%;top:50%;width:20px;height:20px;border-radius:50%;
  border:3px solid #fff;box-shadow:0 1px 5px rgba(0,0,0,.45);
  transform:translate(-50%,-50%);pointer-events:none}
.swatches{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px;max-width:172px}
.sw{width:32px;height:32px;border-radius:9px;border:2px solid #fff;padding:0;cursor:pointer;
  box-shadow:var(--shadow-sm)}
.sw:active{transform:translateY(1px)}
/* Compact accordion list of bulbs */
.bulb{border-bottom:1px solid var(--line)}
.bulb:last-child{border-bottom:0}
.bulbhead{display:flex;align-items:center;gap:11px;padding:14px 12px;cursor:pointer;
  user-select:none;min-height:56px}
.bulbhead:hover{background:#f6f8fc;border-radius:12px}
.cdot{width:22px;height:22px;border-radius:50%;border:2px solid #fff;flex:0 0 auto;
  box-shadow:0 0 0 1px var(--line),var(--shadow-sm)}
.bname{font-weight:650;font-size:16px}
.chev{font-size:22px;color:var(--muted);transition:transform .2s}
.bulb.open .chev{transform:rotate(90deg)}
.bulbbody{display:none;padding:4px 12px 18px}
.bulb.open .bulbbody{display:block}
.colorsq{width:60px;height:60px;border-radius:14px;cursor:pointer;border:3px solid #fff;
  box-shadow:0 0 0 1px var(--line),var(--shadow-sm)}
.colorsq:active{transform:translateY(1px)}
/* Modal */
.modal{position:fixed;inset:0;z-index:60;display:none;align-items:center;justify-content:center;
  background:rgba(20,28,46,.5);padding:18px}
.modal.open{display:flex}
.modalcard{background:var(--card);border-radius:20px;padding:22px;box-shadow:var(--shadow);
  display:flex;flex-direction:column;align-items:center;max-width:92vw}
label.fld{display:flex;flex-direction:column;gap:6px;font-size:13px;font-weight:600;
  color:var(--muted);min-width:120px}
.pill{font-size:13px;padding:4px 11px;border-radius:999px;background:#eef2fb;
  color:#4a546a;font-weight:600}
.ok{color:var(--ok)}.bad{color:var(--bad)}
.muted{color:var(--muted);font-size:15px}
.hide{display:none}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:15px;min-width:420px}
th,td{text-align:left;padding:11px 10px;border-bottom:1px solid var(--line)}
th{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%) translateY(10px);
  background:#1e2533;color:#fff;padding:13px 20px;border-radius:14px;font-weight:600;
  box-shadow:var(--shadow);opacity:0;transition:.25s;z-index:50;max-width:90vw}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media(min-width:560px){
  main{padding:24px}
  .card{padding:22px}
  header{padding:14px 22px}
}
</style>
"""

LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>LIFX Bridge — Sign in</title>""" + _STYLE + """</head><body>
<main style="max-width:360px;margin-top:12vh">
<div class=card>
<h1 style="margin:0 0 4px">LIFX DMX Bridge</h1>
<p class=muted>Sign in to configure your lights.</p>
<label class=fld>Username<input id=u value="admin"></label>
<label class=fld style="margin-top:10px">Password<input id=p type=password></label>
<button class=act style="margin-top:14px;width:100%" onclick=login()>Sign in</button>
<p id=err class=bad style="min-height:18px"></p>
<p class=muted>Forgot the password? Restart the device 3 times in a row
(each within 60s) to reset it to the defaults.</p>
</div></main>
<script>
async function login(){
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:u.value,password:p.value})});
  const j=await r.json();
  if(j.ok){location.reload()}else{err.textContent=j.error||'Login failed'}
}
p.addEventListener('keydown',e=>{if(e.key==='Enter')login()});
</script></body></html>"""

APP_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>LIFX DMX Bridge</title>""" + _STYLE + """</head><body>
<header>
  <button class=iconbtn onclick=toggleMenu() aria-label=Menu>&#9776;</button>
  <h1>LIFX DMX Bridge</h1>
</header>
<div id=backdrop onclick=closeMenu()></div>
<aside id=drawer></aside>
<main id=view></main>
<div id=picker class=modal onclick="if(event.target===this)closePicker()">
  <div class=modalcard>
    <h2 style="text-align:center">Pick a colour</h2>
    <div class=wheel id=pwheel><div class=wmark id=pmark></div></div>
    <div class=swatches id=pswatches style="max-width:none;justify-content:center"></div>
    <button class=act style="width:100%;margin-top:16px" onclick=closePicker()>Done</button>
  </div>
</div>
<div id=toast class=toast></div>
<script>
let S={};
const tabs=['Bulbs','Patch','Effects','Maintenance','Settings','Profile','Help'];
function toast(m){const t=document.getElementById('toast');t.textContent=m;
  t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1800)}
async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',
  headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});
  if(r.status===401){location.reload();return{}}return r.json()}
async function logout(){await api('/api/logout',{});location.reload()}
let tab='Bulbs';
function setTab(t){tab=t;render();drawNav();closeMenu()}
function drawNav(){
  const d=document.getElementById('drawer');
  d.innerHTML='<div class=brand>LIFX DMX Bridge</div>';
  tabs.forEach(t=>{const b=document.createElement('button');
    b.textContent=t;b.className=t===tab?'active':'';b.onclick=()=>setTab(t);d.appendChild(b)});
  const out=document.createElement('button');
  out.textContent='Sign out';out.className='signout';out.onclick=logout;d.appendChild(out)}
function toggleMenu(){
  document.getElementById('drawer').classList.toggle('open');
  document.getElementById('backdrop').classList.toggle('open')}
function closeMenu(){
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('backdrop').classList.remove('open')}
async function refresh(){S=await api('/api/state');render()}

function render(){
  if(tab==='Bulbs')return renderBulbs();
  if(tab==='Patch')return renderPatch();
  if(tab==='Effects')return renderEffects();
  if(tab==='Maintenance')return renderMaint();
  if(tab==='Settings')return renderSettings();
  if(tab==='Profile')return renderProfile();
  if(tab==='Help')return renderHelp();
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

// Compact accordion: one row per bulb; click to expand its settings. The colour
// square in the expanded panel opens a modal HSV wheel. picks[id]={h:0-360,s:0-1}.
const picks={};
let expandedId=null;
const SW=[['Red',0,1],['Amber',40,1],['Yellow',60,1],['Green',120,1],['Cyan',180,1],
  ['Blue',240,1],['Purple',280,1],['Pink',320,1],['White',0,0]];
function hsvToRgb(h,s,v){
  const c=v*s, x=c*(1-Math.abs((h/60)%2-1)), m=v-c; let r,g,b;
  if(h<60){r=c;g=x;b=0}else if(h<120){r=x;g=c;b=0}else if(h<180){r=0;g=c;b=x}
  else if(h<240){r=0;g=x;b=c}else if(h<300){r=x;g=0;b=c}else{r=c;g=0;b=x}
  return [Math.round((r+m)*255),Math.round((g+m)*255),Math.round((b+m)*255)]}
function swCss(h,s){const[r,g,b]=hsvToRgb(h,s,1);return `rgb(${r},${g},${b})`}
function colOf(id){const p=picks[id]||{h:0,s:0};return swCss(p.h,p.s)}

function renderBulbs(){
  const f=S.fixtures||[];
  let h=`<div class=card><div class=row>
    <button class=act onclick=discover()>Discover bulbs</button>
    <button class=ghost onclick="blackout(true)">Blackout</button>
    <button class=ghost onclick="blackout(false)">Resume</button>
    <button class=ghost onclick="releaseAll()">Release all to QLab</button>
    <span class=muted>${f.length} bulb(s)</span></div></div>`;
  if(!f.length)h+=`<div class=card class=muted>No bulbs yet. Click <b>Discover bulbs</b>.</div>`;
  if(f.length)h+=`<div class=card style="padding:6px">`;
  f.forEach(x=>{
    h+=`<div class=bulb id="bulb_${x.id}">
      <div class=bulbhead onclick="toggleBulb('${x.id}')">
        <span class=cdot id="cdot_${x.id}" style="background:${colOf(x.id)}"></span>
        <span class=bname>${esc(x.label)||'<span class=muted>'+(x.ip||'?')+'</span>'}</span>
        <span class=pill>${x.ip||'?'}</span>
        ${x.manual?'<span class=pill title="Web UI is driving this bulb instead of QLab.">web</span>':''}
        <span style="flex:1"></span><span class=chev>&#8250;</span>
      </div>
      <div class=bulbbody>
        <div class=row>
          <input value="${esc(x.label)}" id="lbl_${x.id}" placeholder="Name (e.g. Entrance)" style="flex:1"
            onchange="rename('${x.id}')" onkeydown="if(event.key==='Enter')this.blur()">
          <button class=act onclick="ident('${x.id}')">Identify</button>
        </div>
        <div class=row style="margin-top:12px;align-items:center">
          <div>
            <div class=muted style="font-size:13px;margin-bottom:5px">Colour</div>
            <div class=colorsq id="cs_${x.id}" style="background:${colOf(x.id)}" onclick="openPicker('${x.id}')"></div>
          </div>
          <label class=fld style="flex:1;min-width:160px">Intensity<input type=range min=0 max=255 value=255 id="int_${x.id}" onchange="control('${x.id}')"></label>
        </div>
        <div class=row style="margin-top:12px">
          <label class=fld>Mode<select id="mode_${x.id}" onchange="control('${x.id}')">
            <option value=static>Static</option><option value=breathe>Breathe</option>
            <option value=pulse>Pulse</option><option value=rainbow>Rainbow</option>
            <option value=color_loop>Color loop</option><option value=candle>Candle</option>
          </select></label>
          <label class=fld>Speed<input type=range min=0 max=255 value=128 id="spd_${x.id}" onchange="control('${x.id}')"></label>
          <label class=fld>Strobe<input type=range min=0 max=255 value=0 id="strb_${x.id}" onchange="control('${x.id}')"></label>
        </div>
        <div class=row style="margin-top:12px">
          <button class=act onclick="control('${x.id}')">Apply</button>
          <button class=ghost onclick="release('${x.id}')">Release to QLab</button>
        </div>
      </div></div>`;
  });
  if(f.length)h+=`</div>`;
  view.innerHTML=h;
  if(expandedId){const el=document.getElementById('bulb_'+expandedId);if(el)el.classList.add('open')}
}
function toggleBulb(id){
  const same=expandedId===id;
  document.querySelectorAll('.bulb').forEach(el=>el.classList.remove('open'));
  expandedId=same?null:id;
  if(expandedId){const el=document.getElementById('bulb_'+id);if(el)el.classList.add('open')}}
function updateSquares(id){const c=colOf(id);
  ['cs_','cdot_'].forEach(p=>{const e=document.getElementById(p+id);if(e)e.style.background=c})}

// --- Modal colour picker (single shared wheel) ---
let modalId=null;
function pmove(){const p=picks[modalId]||{h:0,s:0};const m=document.getElementById('pmark');
  if(!m)return;const a=p.h*Math.PI/180;
  m.style.left=(50+p.s*50*Math.sin(a))+'%';m.style.top=(50-p.s*50*Math.cos(a))+'%'}
function openPicker(id){modalId=id;document.getElementById('picker').classList.add('open');pmove()}
function closePicker(){document.getElementById('picker').classList.remove('open')}
function pickSwatchModal(hh,ss){picks[modalId]={h:hh,s:ss};pmove();updateSquares(modalId);control(modalId)}
function wirePicker(){
  document.getElementById('pswatches').innerHTML=SW.map(s=>
    `<button class=sw title="${s[0]}" style="background:${swCss(s[1],s[2])}" onclick="pickSwatchModal(${s[1]},${s[2]})"></button>`).join('');
  const w=document.getElementById('pwheel');let drag=false;
  const pick=e=>{const r=w.getBoundingClientRect();const pt=e.touches?e.touches[0]:e;
    const dx=pt.clientX-(r.left+r.width/2), dy=pt.clientY-(r.top+r.height/2);
    const sat=Math.min(1,Math.hypot(dx,dy)/(r.width/2));
    const hue=(Math.atan2(dx,-dy)*180/Math.PI+360)%360;
    picks[modalId]={h:hue,s:sat};pmove();updateSquares(modalId)};
  w.addEventListener('pointerdown',e=>{drag=true;w.setPointerCapture(e.pointerId);pick(e)});
  w.addEventListener('pointermove',e=>{if(drag)pick(e)});
  w.addEventListener('pointerup',()=>{drag=false;control(modalId)})}
async function discover(){toast('Scanning…');const j=await api('/api/discover',{timeout:3});
  toast(`Found ${j.found}, added ${j.added}`);refresh()}
async function rename(id){
  const v=document.getElementById('lbl_'+id).value;
  await api('/api/fixture/rename',{id,label:v});
  const fx=(S.fixtures||[]).find(x=>x.id===id);if(fx)fx.label=v;  // keep cache in sync
  toast('Name saved')}
async function ident(id){await api('/api/fixture/identify',{id});toast('Flashing bulb…')}
async function release(id){await api('/api/fixture/release',{id});toast('Back on QLab');refresh()}
async function releaseAll(){await api('/api/release-all',{});toast('All bulbs back on QLab');refresh()}
async function blackout(on){await api('/api/blackout',{on});toast(on?'Blackout':'Resumed')}
async function control(id){
  const p=picks[id]||{h:0,s:0};const [r,g,b]=hsvToRgb(p.h,p.s,1);
  updateSquares(id);
  const j=await api('/api/fixture/control',{id,r,g,b,a:0,
    intensity:+document.getElementById('int_'+id).value,
    mode:document.getElementById('mode_'+id).value,
    speed:+document.getElementById('spd_'+id).value,
    strobe:+document.getElementById('strb_'+id).value});
  toast(j.ok?('Sent → '+j.ip):('Error: '+(j.error||'failed')))}

function renderPatch(){
  const f=S.fixtures||[];const c=S.conflicts||[];
  let h=`<div class=card><div class=row style="align-items:flex-end">
    <label class=fld style="width:84px">Universe<input id=aa_u value="0" inputmode=numeric></label>
    <label class=fld style="width:104px">Start address<input id=aa_a placeholder="1" inputmode=numeric></label>
    <button class=act onclick=autoassign()>Auto-assign addresses</button>
    <a class=act href=/api/patch.csv style="text-decoration:none">Export patch CSV</a>
    <span class=muted>${S.channels_per_fixture} channels per fixture</span></div>
    <div class=muted style="margin-top:8px;font-size:13px">Auto-assign packs all bulbs sequentially from the chosen universe &amp; start address (blank start = 1), rolling into the next universe when one fills.</div>`;
  if(c.length)h+=`<div class=bad style="margin-top:10px"><b>Conflicts:</b><br>${c.map(esc).join('<br>')}</div>`;
  else h+=`<div class=ok style="margin-top:10px">No address conflicts.</div>`;
  h+=`</div><div class=card><div class=tablewrap><table><tr><th>Name</th><th>Universe</th><th>Start</th><th>Group</th><th></th></tr>`;
  f.forEach(x=>{h+=`<tr>
    <td>${esc(x.label)||'<span class=muted>'+x.ip+'</span>'}</td>
    <td><input style="width:64px" id="u_${x.id}" value="${x.universe}"></td>
    <td><input style="width:64px" id="a_${x.id}" value="${x.address}"></td>
    <td><input style="width:110px" id="g_${x.id}" value="${esc(x.group)}"></td>
    <td><button class=ghost onclick="setAddr('${x.id}')">Set</button>
        <button class=warn onclick="rm('${x.id}')">✕</button></td></tr>`});
  h+=`</table></div></div>`;view.innerHTML=h;
}
async function autoassign(){
  const u=document.getElementById('aa_u'), a=document.getElementById('aa_a');
  await api('/api/auto-assign',{universe:+(u&&u.value||0)||0,
    address:(a&&a.value.trim())?+a.value:1});
  toast('Re-addressed');refresh()}
async function setAddr(id){await api('/api/fixture/address',{id,
  universe:+document.getElementById('u_'+id).value,
  address:+document.getElementById('a_'+id).value,
  group:document.getElementById('g_'+id).value});toast('Set');refresh()}
async function rm(id){if(confirm('Remove this fixture from the map?')){
  await api('/api/fixture/remove',{id});refresh()}}

function renderEffects(){
  view.innerHTML=`<div class=card><h2>Effect / Mode channel (ch+5)</h2>
  <div class=tablewrap><table><tr><th>DMX value</th><th>Mode</th><th>Runs on</th></tr>
  <tr><td>0–9</td><td>Static</td><td>SetColor</td></tr>
  <tr><td>10–39</td><td>Breathe</td><td>bulb firmware</td></tr>
  <tr><td>40–69</td><td>Pulse / blink</td><td>bulb firmware</td></tr>
  <tr><td>70–99</td><td>Triangle</td><td>bulb firmware</td></tr>
  <tr><td>100–129</td><td>Saw</td><td>bulb firmware</td></tr>
  <tr><td>130–169</td><td>Color pulse</td><td>bulb firmware</td></tr>
  <tr><td>170–209</td><td>Rainbow cycle</td><td><b>script-generated</b></td></tr>
  <tr><td>210–239</td><td>Color loop</td><td><b>script-generated</b></td></tr>
  <tr><td>240–255</td><td>Candle flicker</td><td><b>script-generated</b></td></tr></table></div>
  <p class=muted>ch+6 = Effect Speed (slow→fast). ch+7 = Strobe overlay (0=off),
  which takes priority over the mode. Native firmware effects cost almost no
  network traffic; script effects stream at the rate cap.</p>
  <details style="margin-top:14px">
    <summary style="cursor:pointer;font-weight:650">What does “script-generated” mean?</summary>
    <div style="margin-top:10px;line-height:1.55">
      <p>It’s about <b>where the animation is computed</b>. Effects come from one of two engines:</p>
      <p><b>Bulb firmware</b> (Breathe, Pulse, Triangle, Saw, Color pulse).
      The bridge sends the bulb a single waveform command and the bulb’s own chip
      animates it. The bridge only re-arms it occasionally, so it costs
      <b>almost no network traffic</b> and keeps running even if the bridge pauses.</p>
      <p><b>Script-generated</b> (Rainbow, Color loop, Candle). LIFX firmware has no
      native version of these, so <b>the bridge computes every frame itself</b> and
      streams a continuous series of color updates to the bulb:</p>
      <ul style="margin:6px 0 6px 18px">
        <li><b>Rainbow</b> — advances a hue angle over time (≈6–180°/sec, set by Speed) for a smooth hue sweep.</li>
        <li><b>Color loop</b> — steps through a fixed palette, one color per step, dwell time set by Speed.</li>
        <li><b>Candle</b> — randomly flickers a dimmed warm hue every ~80&nbsp;ms.</li>
      </ul>
      <p>Because the bridge pushes these frames over the network (up to the rate cap,
      ~20&nbsp;updates/sec per bulb), script effects are the <b>bandwidth-heavy</b>
      ones. On a large rig, running many bulbs in Rainbow/Loop/Candle at once is what
      saturates the network — firmware effects scale far more cheaply. Script effects
      also stop if the bridge stops; firmware effects keep going on the bulb.</p>
    </div>
  </details></div>`;
}

function monitorHtml(){
  const L=S.listener||{};const m=S.monitor||{};
  let h=`<div class=card><h2>Listener status</h2>
  <p>Art-Net port ${L.port}: <b class="${L.running?'ok':'bad'}">${L.running?'listening':'stopped'}</b>
   ${L.port_free?'':'<span class=pill>port in use elsewhere</span>'}</p>
  ${L.error?`<p class=bad>${esc(L.error)}</p>`:''}
  <div class=row><button class=act onclick="listener('start')">Start</button>
  <button class=ghost onclick="listener('stop')">Stop</button>
  <button class=ghost onclick="listener('restart')">Restart</button></div></div>`;
  h+=`<div class=card><h2>Live DMX in</h2>`;
  h+=`<p class=muted>Packets: ${m.packets||0}${m.last_packet_age!=null?
    ' · last '+m.last_packet_age.toFixed(1)+'s ago':' · none yet'}</p>`;
  const u=m.universes||{};
  if(!Object.keys(u).length)h+=`<p class=muted>No Art-Net received yet — is QLab sending?</p>`;
  for(const [uni,vals] of Object.entries(u)){
    h+=`<p><b>Universe ${uni}</b><br><span class=muted style="font-family:monospace;font-size:12px">
      ${vals.map((v,i)=>String(v).padStart(3)).join(' ')}</span></p>`;
  }
  h+=`</div>`;return h;
}
async function listener(action){const j=await api('/api/listener',{action});
  toast('Listener '+action);if(j.error)toast(j.error);refresh()}

function renderMaint(){
  const f=S.fixtures||[];
  let h=`<div class=card><h2>Bulb maintenance</h2><p class=muted>
   Read diagnostics and write names/groups onto the bulbs themselves — most of
   what the LIFX app does day-to-day. (First-time Wi-Fi onboarding of a new bulb
   and firmware updates still need the LIFX app.)</p></div>`;
  if(!f.length)h+=`<div class=card class=muted>Discover bulbs first.</div>`;
  f.forEach(x=>{
    h+=`<div class=card>
      <div class=row><b>${esc(x.label)||x.ip}</b><span class=pill>${x.ip}</span>
        <button class=act onclick="loadInfo('${x.id}')">Load info</button></div>
      <div id="info_${x.id}" class=muted style="margin-top:8px"></div>
      <div class=row style="margin-top:10px">
        <label class=fld>Name on bulb<input id="mn_${x.id}" value="${esc(x.label)}"></label>
        <button class=ghost onclick="pushName('${x.id}')">Write name to bulb</button>
        <label class=fld>Group<input id="mg_${x.id}" value="${esc(x.group)}"></label>
        <button class=ghost onclick="setGroup('${x.id}')">Set group</button>
      </div></div>`;
  });
  view.innerHTML=h;
}
function bars(n){return '▁▂▃▄'.slice(0,Math.max(1,n)).padEnd(4,'·')}
async function loadInfo(id){
  const el=document.getElementById('info_'+id);el.textContent='Querying bulb…';
  const j=await api('/api/fixture/info',{id});
  if(!j.ok){el.textContent=j.error||'No reply';return}
  const i=j.info, w=i.wifi||{};
  const up=i.uptime_s?(i.uptime_s>86400?(i.uptime_s/86400).toFixed(1)+'d':
    (i.uptime_s/3600).toFixed(1)+'h'):'?';
  el.innerHTML=`<b>${esc(i.model||'?')}</b> · fw ${esc(i.firmware||'?')}
   · Wi-Fi ${w.dbm!=null?w.dbm+' dBm ('+w.label+')':'?'}
   · uptime ${up} · ${i.latency_ms!=null?i.latency_ms+'ms':'no echo'}
   · group ${esc(i.group||'—')} / room ${esc(i.location||'—')}`;
}
async function pushName(id){await api('/api/fixture/push-name',
  {id,label:document.getElementById('mn_'+id).value});toast('Name written to bulb');refresh()}
async function setGroup(id){await api('/api/fixture/group',
  {id,group:document.getElementById('mg_'+id).value});toast('Group set');refresh()}

async function fillNetwork(el){
  el.innerHTML=`<div class=card class=muted>Loading network info…</div>`;
  const n=await api('/api/network');
  if(tab!=='Settings'||subtab!=='Network')return;  // user navigated away
  let h=`<div class=card><h2>Host network (this device)</h2>
  <p>Current IP: <b>${n.local_ip||'?'}</b> ${n.method?`· mode: ${n.method}`:''}</p>`;
  if(!n.supported){h+=`<p class=muted>${esc(n.note||'')}</p>`}
  else{h+=`<p class=muted>Connection: ${esc(n.connection||'?')} (${esc(n.device||'?')})
    ${n.addresses?'· '+esc(n.addresses):''} ${n.gateway?'· gw '+esc(n.gateway):''}</p>
  <div class=row><button class=act onclick=setDhcp()>Use DHCP (automatic)</button></div>
  <h2 style="margin-top:16px">Set a static IP</h2>
  <div class=row>
   <label class=fld>IP address<input id=nip placeholder="192.168.1.50"></label>
   <label class=fld>Prefix<input id=npfx value=24 style="width:70px"></label>
   <label class=fld>Gateway<input id=ngw placeholder="192.168.1.1"></label>
   <label class=fld>DNS<input id=ndns placeholder="1.1.1.1 8.8.8.8"></label>
   <button class=act onclick=setStatic()>Apply static IP</button></div>`}
  h+=`</div><div class=card><h2>Find a free address</h2>
   <button class=act onclick=scan()>Scan my network</button>
   <div id=scanout class=muted style="margin-top:10px"></div></div>`;
  el.innerHTML=h;
}
async function setDhcp(){const j=await api('/api/network/dhcp',{});toast(j.message||'')}
async function setStatic(){const j=await api('/api/network/static',{
  ip:nip.value,prefix:+npfx.value,gateway:ngw.value,dns:ndns.value});toast(j.message||'')}
async function scan(){scanout.textContent='Scanning the subnet… (this takes ~10s)';
  const j=await api('/api/network/scan',{});
  if(j.error){scanout.textContent=j.error;return}
  scanout.innerHTML=`Subnet ${j.subnet}. ${j.used.length} addresses in use,
   ${j.free_count} free.<br><b>Suggested free addresses:</b><br>
   ${j.suggestions.map(s=>`<span class=pill onclick="nipFill('${s}')" style="cursor:pointer;margin:2px">${s}</span>`).join(' ')}`;
}
function nipFill(ip){const e=document.getElementById('nip');if(e)e.value=ip;toast('Filled '+ip)}

let subtab='General';
function setSub(s){subtab=s;renderSettings()}
function renderSettings(){
  const subs=['General','Monitor','Network'];
  view.innerHTML=`<div class=card style="padding:8px"><div class=row>`+
    subs.map(s=>`<button class="${s===subtab?'act':'ghost'}" onclick="setSub('${s}')">${s}</button>`).join('')+
    `</div></div><div id=subview></div>`;
  const sv=document.getElementById('subview');
  if(subtab==='Monitor')sv.innerHTML=monitorHtml();
  else if(subtab==='Network')fillNetwork(sv);
  else sv.innerHTML=settingsGeneralHtml();
}
function settingsGeneralHtml(){
  const s=S.settings||{};
  return `<div class=card><h2>Listener settings</h2><div class=flex>
   <label class=fld>Max updates/sec per bulb<input id=s_hz value="${s.max_hz}"></label>
   <label class=fld>Smooth fade (ms)<input id=s_sm value="${s.smooth_ms}"></label>
   <label class=fld>White temp (K)<input id=s_k value="${s.kelvin}"></label>
   <label class=fld>Rediscover (s, 0=off)<input id=s_rd value="${s.rediscover}"></label>
   <label class=fld>Bind host<input id=s_bh value="${s.bind_host}"></label>
  </div><button class=act style="margin-top:12px" onclick=saveSettings()>Save</button>
  <span class=muted>Restart the listener (Monitor section) to apply.</span></div>`;
}
function renderProfile(){
  view.innerHTML=`<div class=card><h2>Profile</h2>
   <p class=muted>Signed in as <b>admin</b>.</p></div>
  <div class=card><h2>Change password</h2><div class=row>
   <label class=fld>Current<input id=pw_c type=password></label>
   <label class=fld>New (6+ chars)<input id=pw_n type=password></label>
   <button class=act onclick=changePw()>Change password</button></div>
   <p class=muted>Lost the password? Restart the device 3× in a row (each within
   60s) to reset to admin / admin123.</p></div>`;
}
async function saveSettings(){await api('/api/settings',{max_hz:+s_hz.value,
  smooth_ms:+s_sm.value,kelvin:+s_k.value,rediscover:+s_rd.value,bind_host:s_bh.value});
  toast('Saved — restart listener to apply');refresh()}
async function changePw(){const j=await api('/api/change-password',
  {current:pw_c.value,new:pw_n.value});toast(j.ok?'Password changed':j.error)}

function renderHelp(){
  view.innerHTML=`<div class=card><h2>Quick start</h2><ol>
  <li><b>Settings → Network</b> → give this device a static IP (scan to find a free one).</li>
  <li><b>Bulbs tab</b> → Discover bulbs. Use <b>Identify</b> to flash each one and
   give it a name (Entrance, Wall, Overhead…).</li>
  <li><b>Patch tab</b> → Auto-assign addresses, fix any conflicts, Export the CSV.</li>
  <li>In QLab, patch one <b>8-channel</b> fixture per bulb at the addresses from the CSV
   (R,G,B,Amber,Intensity,Mode,Speed,Strobe).</li>
  <li><b>Settings → Monitor</b> → Start the listener and confirm DMX is arriving.</li>
  </ol>
  <p class=muted>Channels per fixture: 8. A 512-channel universe holds 64 bulbs.</p></div>`;
}

drawNav();wirePicker();refresh();
// Only the Monitor tab needs live polling. Never re-render while the user is
// interacting with a field (that was closing dropdowns mid-click).
setInterval(()=>{
  const a=document.activeElement;
  if(a && ['INPUT','SELECT','TEXTAREA'].includes(a.tagName))return;
  if(tab==='Settings'&&subtab==='Monitor')refresh();
},3000);
</script></body></html>"""


# --- Main -------------------------------------------------------------------

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def ensure_appliance_ip(ip):
    """Add a fixed link-local management IP *alongside* DHCP so a directly-
    connected computer can always reach the UI at a known address (the way an
    appliance like a Chauvet DMX-AN2 ships on a fixed IP). Additive and
    reversible — it does not disturb the existing DHCP lease. Pi/Linux only."""
    if not _have_nmcli():
        return False, "nmcli not available (appliance IP is Linux-only)."
    con = network_status().get("connection")
    if not con:
        return False, "No active network connection to attach the IP to."
    for cmd in (["nmcli", "con", "mod", con, "+ipv4.addresses", f"{ip}/16"],
                ["nmcli", "con", "up", con]):
        rc, _, err = _run(cmd)
        if rc != 0:
            return False, err or "nmcli failed (need root?)."
    return True, f"Reachable at http://{ip}:<port> from a directly-connected computer."


def main():
    import argparse
    ap = argparse.ArgumentParser(description="LIFX DMX Bridge web UI")
    ap.add_argument("--port", type=int, help="web UI port (default 8080)")
    ap.add_argument("--mode", choices=["dev", "appliance"], default="dev",
                    help="dev (default): serve on the normal network, never "
                         "touch host IP. appliance: also pin a fixed link-local "
                         "management IP for direct-connect setup (Pi only).")
    ap.add_argument("--appliance-ip", default="169.254.7.7",
                    help="fixed management IP used in appliance mode")
    args = ap.parse_args()

    ensure_dir()
    reset, count = record_restart_and_maybe_reset()
    if reset:
        print(f"** {count} restarts in a row — password reset to "
              f"{DEFAULT_USER}/{DEFAULT_PASS} **")
    else:
        print(f"Restart streak: {count}/{RESTART_NEEDED} "
              f"(reset password with {RESTART_NEEDED} restarts within "
              f"{RESTART_WINDOW:g}s of each other).")
    load_auth()  # ensure auth.json exists

    if args.mode == "appliance":
        ok, msg = ensure_appliance_ip(args.appliance_ip)
        print(f"Appliance mode: {msg}" if ok else f"Appliance mode skipped: {msg}")

    if MGR.settings.get("autostart") and MGR.fixtures:
        MGR.start()

    port = int(args.port or MGR.settings.get("web_port", 8080))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"LIFX Bridge web UI ({args.mode}) on http://0.0.0.0:{port}  "
          f"(default login {DEFAULT_USER}/{DEFAULT_PASS})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        MGR.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
