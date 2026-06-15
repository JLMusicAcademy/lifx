# LIFX LAN Control

A single-file, dependency-free Python script to control a LIFX bulb directly
over your local network. It speaks the [LIFX LAN protocol](https://lan.developer.lifx.com/docs)
(UDP, port 56700) — no cloud, no account, no `pip install` required.

## Requirements

- Python 3.6+
- A LIFX bulb powered on and connected to the **same Wi-Fi network / subnet**
  as the machine running the script (it must already be set up via the LIFX
  app once, so it has Wi-Fi credentials).

## Usage

```bash
# 1. Find your bulb(s) and note the IP
python3 lifx_control.py discover

# 2. Power
python3 lifx_control.py on  --duration 2      # fade on over 2s
python3 lifx_control.py off --duration 2      # fade off over 2s

# 3. Colors (by name, or precise HSBK)
python3 lifx_control.py color --name red --duration 3
python3 lifx_control.py color --hue 240 --saturation 100 --brightness 80

# 4. White at a color temperature (2500K warm -> 9000K cool)
python3 lifx_control.py white --kelvin 2700 --brightness 100

# 5. Brightness only (dim to 20% over 5s)
python3 lifx_control.py brightness --brightness 20 --duration 5

# 6. Effects
python3 lifx_control.py pulse   --name blue  --cycles 5 --period 1
python3 lifx_control.py breathe --name green --cycles 5 --period 2

# 7. Read current state
python3 lifx_control.py state
```

By default every command is **broadcast** to all LIFX bulbs on the LAN — so
`on`, `color`, etc. change *every* bulb at once. To target one bulb, pass
either its IP or its label/name (put the flag *before* the subcommand):

```bash
# By IP (fastest; find it with `discover`)
python3 lifx_control.py --ip 192.168.1.50 color --name purple --duration 2

# By the name you gave it in the LIFX app
python3 lifx_control.py --label "Kitchen" on
```

`discover` prints each bulb's IP and label so you know what to pass:

```
Found 2 bulb(s):
  192.168.1.50  "Kitchen"               (MAC d0:73:d5:11:22:33)
  192.168.1.51  "Living Room"           (MAC d0:73:d5:44:55:66)
```

## Commands

| Command      | What it does                                              |
|--------------|----------------------------------------------------------|
| `discover`   | Broadcast a discovery request and list bulbs (IP + MAC)  |
| `state`      | Show current power, color, brightness and temperature    |
| `on` / `off` | Turn the bulb on/off, optionally with a `--duration` fade |
| `color`      | Set a color by `--name` or `--hue/--saturation/--brightness`; `--duration` gives a fade-in/transition |
| `white`      | Set white at a `--kelvin` temperature and `--brightness` |
| `brightness` | Change only brightness (keeps current color)             |
| `pulse`      | Sharp on/off blink effect (`--cycles`, `--period`)       |
| `breathe`    | Smooth fade in/out effect (`--cycles`, `--period`)       |

### Named colors

`red`, `orange`, `yellow`, `green`, `cyan`, `blue`, `purple`, `magenta`,
`pink`, `white`.

## QLab integration (Art-Net DMX)

The script can present itself as an **Art-Net DMX node** so QLab's Light
workspace drives your bulbs natively — with the RGB(A) color wheel, intensity,
fade cues, **and the bulbs' own effects** (breathe, pulse, strobe, rainbow,
etc.). It discovers every LIFX bulb on the network and maps each to a DMX
address. Each bulb is an **8-channel fixture**:

| Offset | Channel   | Notes |
|--------|-----------|-------|
| +0     | Red       | color |
| +1     | Green     | color |
| +2     | Blue      | color |
| +3     | Amber     | color (folded into the mix at hue ~45 deg) |
| +4     | Intensity | master dimmer |
| +5     | Effect / Mode | ranged selector (see table below) |
| +6     | Effect Speed  | period/rate for the selected effect (slow -> fast) |
| +7     | Strobe        | dedicated overlay: 0 = off, 1-255 = slow -> fast |

At 8 channels each, 64 bulbs fit in one 512-channel universe; beyond that the
mapping rolls over into additional universes automatically. There's no hard
limit on bulb count.

### Effect / Mode channel (ch+5)

| DMX value | Mode | How it runs |
|-----------|------|-------------|
| 0-9     | Static (steady color) | `SetColor` |
| 10-39   | Breathe | native firmware (SINE on brightness) |
| 40-69   | Pulse / blink | native firmware (PULSE on brightness) |
| 70-99   | Triangle | native firmware |
| 100-129 | Saw | native firmware |
| 130-169 | Color pulse | native firmware (pulses color <-> its complement) |
| 170-209 | Rainbow cycle | script-generated hue sweep |
| 210-239 | Color loop | script-generated stepped palette |
| 240-255 | Candle flicker | script-generated warm flicker |

**Native vs script effects matters for traffic.** Breathe / pulse / triangle /
saw / color-pulse / strobe run on the **bulb's own firmware** — the script just
arms them, so they cost almost no network traffic no matter how many bulbs.
Rainbow / color-loop / candle have **no single-bulb firmware equivalent**, so
the script animates them by streaming `SetColor` at the rate cap — fine for a
handful of bulbs, heavier with many running at once.

The **Strobe** channel (ch+7) is an independent overlay: any non-zero value
strobes the current color and takes priority over the Effect channel.
`Effect Speed` (ch+6) sets the rate for whichever effect is selected.

### Quick start (auto-discover & assign)

```bash
# Discovers bulbs, assigns DMX addresses, and starts listening for Art-Net.
python3 lifx_control.py listen
```

It prints the fixture map it built, e.g.:

```
DMX fixture map (5ch each: R, G, B, Amber, Intensity):
  U0   addr   1  "Kitchen"               -> 192.168.1.50
  U0   addr   6  "Living Room"           -> 192.168.1.51

Art-Net listener on 0.0.0.0:6454  (2 fixture(s) across 1 universe(s))
Rate limit: 20 updates/s per bulb. Press Ctrl-C to stop.
```

### Stable patch (recommended for many bulbs)

Auto-assign ordering can shift if bulbs come and go. For a fixed, reproducible
patch, generate a fixture map once and edit it to taste:

```bash
python3 lifx_control.py dmxmap --out fixtures.json   # writes an editable map
# ...edit fixtures.json: set each bulb's universe/address as you like...
python3 lifx_control.py listen --map fixtures.json
```

The map is keyed by each bulb's **MAC address**, so the patch survives DHCP IP
changes. Use `--rediscover 30` to re-resolve IPs every 30s while listening.

### Configuring QLab

1. In QLab, open **Settings -> Light** (or the Light patch) and add a network
   DMX (Art-Net) output pointed at the IP of the machine running this script,
   using the universe(s) shown in the fixture map.
2. Patch a generic **8-channel** fixture (R, G, B, Amber, Intensity, Mode,
   Speed, Strobe) at each DMX address the script printed.
3. Use Light cues as normal — the color wheel and fade sliders drive the bulbs.
   QLab streams the fade frame-by-frame; the script rate-limits to `--max-hz`
   (default 20/s per bulb) so Wi-Fi keeps up, and uses a short `--smooth`
   transition so fades look continuous. Set the Mode/Speed/Strobe channels
   (e.g. as fixed levels in a Light cue) to trigger effects.

### `listen` options

| Option            | Default | Purpose                                            |
|-------------------|---------|----------------------------------------------------|
| `--map FILE`      | (auto)  | Use a saved fixture map instead of auto-discovery  |
| `--universe N`    | 0       | Base universe for auto-assign                      |
| `--address N`     | 1       | Base DMX address for auto-assign                   |
| `--kelvin N`      | 3500    | White temperature when color is desaturated        |
| `--max-hz N`      | 20      | Max LIFX updates/sec per bulb (rate limit)         |
| `--smooth S`      | 0.12    | LIFX transition time per update, seconds           |
| `--rediscover S`  | 0 (off) | Re-resolve bulb IPs every S seconds                |
| `--no-poll-reply` | off     | Don't answer ArtPoll node discovery                |
| `--verbose`       | off     | Print each DMX -> HSBK update                       |

## Web UI (configuration & control)

`lifx_web.py` is an optional, dependency-free web app that wraps everything
above in a browser UI — designed to run headless on a Raspberry Pi 5 next to
QLab. Start it with:

```bash
python3 lifx_web.py        # serves http://<device>:8080
```

Default login: **admin / admin123** (change it in Settings).

What it does:

- **Bulbs** — discover bulbs, **Identify** (flash a bulb so you can tell which
  physical fixture it is), rename them (Entrance, Wall, Overhead…), and control
  any bulb directly (color, intensity, effects) from a browser or phone.
- **Patch** — auto-assign DMX addresses, flag address conflicts, edit each
  bulb's universe/address/group, and export a **patch CSV** for QLab.
- **Effects** — reference table for the Mode/Speed/Strobe channels.
- **Monitor** — start/stop the Art-Net listener, see the port status, and watch
  **live DMX in** (so you can confirm QLab is actually sending).
- **Network** — set this device's IP to **DHCP or a static address** via
  NetworkManager (`nmcli`, Raspberry Pi), with a **subnet scan** that finds
  free addresses to avoid conflicts.
- **Settings** — listener tuning (`max-hz`, `smooth`, kelvin…) and change
  password.

**Password recovery:** if the password is lost, restart the device **3 times in
a row, each within 60 seconds** of the previous boot, and the password resets to
`admin` / `admin123`. The window is configurable (`LIFX_RESTART_WINDOW`) and is
set above one Pi boot cycle so back-to-back reboots register.

Run it as a service with the provided `lifx-bridge.service` (root is needed for
the Network tab's `nmcli` changes). Config lives in `~/.lifx-bridge/` (or
`LIFX_BRIDGE_DIR`).

## Value ranges

- **hue**: 0–360 degrees
- **saturation / brightness**: 0–100 (%)
- **kelvin**: 2500 (warm) – 9000 (cool); only matters when saturation is 0
- **duration / period**: seconds

## How it works

Each command is packed into a 36-byte LIFX header (Frame + Frame Address +
Protocol Header) plus a message-specific payload, then sent as a UDP datagram.
Colors use the bulb's native **HSBK** representation (Hue, Saturation,
Brightness, Kelvin), each a 16-bit value. Fades are handled by the bulb
firmware via the `duration` field on `SetColor`/`SetPower`, and the `pulse`/
`breathe` effects use the `SetWaveform` message.

## Troubleshooting

- **No bulbs found**: confirm the bulb is on the same subnet, that UDP
  broadcast isn't blocked by your router/AP ("client isolation"), and that any
  host firewall allows outbound UDP to port 56700.
- **Commands work but `state` times out**: some networks drop the unicast
  reply; try targeting the bulb directly with `--ip`.
