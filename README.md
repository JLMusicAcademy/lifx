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
