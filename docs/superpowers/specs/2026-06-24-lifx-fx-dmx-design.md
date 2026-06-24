# LIFX Candle FX scenes over DMX — Design

**Date:** 2026-06-24
**Repo:** `~/lifx` (JLMusicAcademy/lifx)
**Status:** Approved design, pending implementation plan

## Goal

Let QLab trigger the LIFX Candle's built-in "FX" scenes (the looks under the
**FX** tab in the official LIFX app — Clouds, Flame, Morph, Sunrise, etc.) over
DMX/Art-Net, the same way the bridge already drives color, intensity and the
existing Mode/Strobe effects.

## Research findings (what is actually possible over the LAN)

The LIFX LAN protocol exposes **only three firmware effects** on a matrix device
like the Candle, all via one message — `SetTileEffect` (packet **719**):

| Effect | `type` | Configurable |
|---|---|---|
| OFF | 0 | — (stops the running effect) |
| MORPH | 2 | speed, duration, **palette of up to 16 HSBK colors** |
| FLAME | 3 | speed, duration (colors hard-coded) |
| SKY | 5 | `sky_type`: Sunrise(0) / Sunset(1) / Clouds(2); cloud saturation |

Everything the Candle shows for these effects (multiple zones, simultaneous
colors, flicker) runs **on the bulb firmware** — the bridge sends one packet and
the bulb animates it. With `duration = 0` the effect runs indefinitely, so unlike
the existing waveform effects there is **no re-arm loop** and near-zero ongoing
traffic.

Many app FX scenes are not standalone firmware effects; they are **Morph with a
particular palette** (Clouds-the-painterly-one, Color Cycle, Pastels, Spooky,
Rando) or app/cloud animations with no single LAN trigger.

**Not firmware-triggerable on a Candle** (decision: script-emulate where useful,
otherwise drop):

- EQ Visualizer, Visualizer — music-reactive; the phone app streams frames from
  its microphone. **Dropped** (no offline equivalent).
- Move — a *multizone* effect (LIFX Z / Beam strips). A Candle is a *matrix*, so
  `SetMultiZoneEffect` does not apply. **Dropped.**
- Flicker, Twinkle, Meteor — app-side animations. **Script-emulated** by the
  bridge streaming whole-bulb color frames (like the existing rainbow/candle
  modes).
- Strobe — already a dedicated overlay on ch8; **not duplicated** as an FX scene.

### Open items to verify during implementation (carried honestly)

1. The **top-level 719 byte layout is confirmed** against the LIFX reference
   implementation (mclarkk/lifxlan). The exact byte offsets **inside the
   `parameters` block** for SKY (`sky_type`, `cloud_saturation_min/max`) and any
   Morph parameter flags will be finalized against the Photons source
   (photons.delfick.com) and verified on the real Candle.
2. **SKY** was introduced for the LIFX Ceiling. Some Candle firmware supports it,
   some does not. Probe with `GetTileEffect` / test on the actual bulb; if SKY is
   unsupported, fall back gracefully (e.g. a warm Morph palette for Sunrise/Sunset
   and a blue/white Morph for Clouds) and note it.

## Channel layout (8 → 9 channels per fixture)

Add **ch9 = "FX Scene"**, a macro selector. The existing **Speed** (ch7) sets the
firmware/script effect rate; **Strobe** (ch8) stays the priority overlay.

```
Ch1  Red          Ch6  Mode  (existing effect selector)
Ch2  Green        Ch7  Speed (also drives FX rate)
Ch3  Blue         Ch8  Strobe (overlay)
Ch4  Amber        Ch9  FX Scene   ← NEW
Ch5  Intensity
```

### FX Scene bands (ch9) and canonical values

Each scene occupies a 20-wide band so cueing is forgiving, with a clean canonical
value (a multiple of 20) to remember and to publish in docs/CSV.

| Set ch9 to | Range | Scene | Engine |
|---|---|---|---|
| 0 | 0–9 | none (fall through to Mode ch6) | — |
| 20 | 10–29 | Morph | firmware MORPH (default palette) |
| 40 | 30–49 | Color Cycle | firmware MORPH (rainbow palette) |
| 60 | 50–69 | Pastels | firmware MORPH (pastel palette) |
| 80 | 70–89 | Spooky | firmware MORPH (orange/purple/green palette) |
| 100 | 90–109 | Rando | firmware MORPH (randomized palette each arm) |
| 120 | 110–129 | Flame | firmware FLAME |
| 140 | 130–149 | Sunrise | firmware SKY, sky_type=0 |
| 160 | 150–169 | Sunset | firmware SKY, sky_type=1 |
| 180 | 170–189 | Clouds | firmware SKY, sky_type=2 |
| 200 | 190–209 | Flicker | script (whole-bulb stream) |
| 220 | 210–229 | Twinkle | script (whole-bulb stream) |
| 240 | 230–255 | Meteor | script (whole-bulb stream) |

**Impact:** universe capacity 64 → 56 bulbs; QLab patches a **9-channel**
fixture. Auto-assign, conflict-check, CSV export and ArtPoll already key off
`CHANNELS_PER_FIXTURE`, so bumping that constant carries most of the change.

## Components

### 1. Protocol layer (`lifx_control.py`)

- Constants: `MSG_SET_TILE_EFFECT = 719`, `MSG_GET_TILE_EFFECT = 718`,
  `MSG_STATE_TILE_EFFECT = 720`; `TILE_EFFECT_OFF/MORPH/FLAME/SKY = 0/2/3/5`;
  `SKY_SUNRISE/SUNSET/CLOUDS = 0/1/2`.
- `FX_PALETTES`: module-level dict mapping preset name → list of (hue, sat) or
  HSBK colors (Color Cycle / Pastels / Spooky; Rando is generated per arm).
- `set_tile_effect(effect_type, speed_ms, palette=None, sky_type=None,
  duration_ms=0, ip=None)` — packs the confirmed 719 layout:
  `2 reserved bytes, instanceid u32 (random per arm), type u8, speed u32,
  duration u64, 2 reserved u32, parameters 8×u32, palette_count u8,
  palette[N] HSBK (8 bytes each, up to 16)`. MORPH writes the palette; SKY writes
  `sky_type` (+ default cloud saturation) into `parameters`; FLAME needs neither.
- `set_tile_effect_off(ip)` convenience wrapper (type = OFF).

`duration = 0` ⇒ effect runs until changed; send once on change, send OFF when
leaving. No re-arm window.

### 2. Effect dispatch (`decode_controls`, `service_fixture`)

- `decode_controls` reads `dmx[i+8]` → `fx_scene`, plus a `decode_fx_scene(value)`
  helper that maps the byte to a scene name (band table above).
- Precedence (decision): **Strobe > FX Scene > Mode**.
  1. `strobe > 0` → existing strobe overlay (unchanged).
  2. `fx_scene >= 10` → FX handling: firmware scenes call `set_tile_effect(...)`
     once when the `(scene, speed)` signature changes (tracked in
     `f["tile_armed"]`); script scenes animate via the existing throttled
     streamer (`_send_throttled` / `last_send`).
  3. else → existing Mode/static/script path (unchanged).
- **Leaving cleanup:** if a fixture had a firmware tile effect armed and the new
  state is not a firmware FX scene, send `set_tile_effect_off` once before
  resuming normal color, mirroring the existing `f["armed"] = None` pattern.

### 3. Script-emulated scenes (whole-bulb, reuse existing streaming infra)

Rate-limited by Speed (ch7) and the existing `min_interval`:

- **Flicker** — small rapid random brightness dips around the base color.
- **Twinkle** — brief brightness pops up/down at random intervals.
- **Meteor** — repeating fast asymmetric pulse (ramp up, decay). Weakest as
  whole-bulb (a Candle cannot do a true per-zone sweep through the single-color
  path); documented as an approximation.

### 4. Web UI + docs (`lifx_web.py`, `README.md`)

- Add the new scenes to the Bulbs-tab manual-control **Mode dropdown** so each is
  testable from the browser/phone (same dispatch path).
- Add an **FX Scene** reference (canonical-value table) to the Help tab.
- Update "How a bulb maps to DMX", the effect table, the README channel table,
  and `print_fixture_table` header to **9 channels**.
- Add a **canonical FX value column** to the exported patch CSV
  (`/api/patch.csv`) so each fixture row carries the scene→value cheat sheet.

### 5. Tests (`test_lifx.py`, stdlib `unittest`, no hardware)

The repo currently has no tests. Add pure-function tests:

- `set_tile_effect` encodes the exact 719 layout (total size, `type`, `speed`,
  `palette_count`, HSBK packing) for MORPH / FLAME / SKY.
- `decode_fx_scene` band boundaries (9/10, 29/30, … 229/230) map to the right
  scene and canonical values round-trip.
- Precedence: with strobe set, FX scene is ignored; with FX scene set and no
  strobe, Mode is ignored.
- `CHANNELS_PER_FIXTURE == 9`; `auto_assign` packs 56 fixtures per universe and
  rolls over correctly.

Runnable with `python3 -m unittest` — no bulb required.

## Non-goals (YAGNI)

- True per-zone / matrix pixel control (`Set64`) — the firmware effects animate
  zones internally; we only trigger them.
- Music-reactive visualizers (no offline trigger).
- Multizone Move effect (wrong device class for a Candle).
- A separate FX-param channel — Speed (ch7) is sufficient since only one effect
  runs at a time.

## Success criteria

- Sending the canonical ch9 value for each firmware scene starts the
  corresponding effect on a real Candle; ch9 = 0 (with no strobe) returns the
  bulb to normal Mode/color behavior with the effect stopped.
- Script scenes animate at a rate set by Speed and stop when ch9 returns to 0.
- `python3 -m unittest` passes with no hardware.
- README, Help, fixture table and CSV all reflect 9 channels and the FX value
  cheat sheet.
