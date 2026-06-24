# LIFX Candle FX scenes over DMX — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let QLab trigger the LIFX Candle's firmware FX scenes (Morph, Flame, Sky) plus three script-emulated looks (Flicker, Twinkle, Meteor) through a new DMX "FX Scene" selector channel.

**Architecture:** Add a `SetTileEffect` (packet 719) builder to the existing dependency-free protocol module; widen each DMX fixture from 8 to 9 channels (ch9 = FX Scene macro selector); dispatch the new channel in `service_fixture` with precedence Strobe > FX Scene > Mode; expose the scenes in the web UI and docs.

**Tech Stack:** Python 3 standard library only (`struct`, `socket`, `random`, `unittest`). No third-party dependencies. Pure functions are unit-tested; network sends are tested by monkeypatching `lifx.send`.

## Global Constraints

- **Stdlib only** — no `pip install`, no new imports beyond Python's standard library.
- **`python3 -m unittest` stays green** after every task.
- **Behaviour parity** — existing channels (R,G,B,Amber,Intensity,Mode,Speed,Strobe) and existing Mode effects must behave exactly as before when ch9 = 0.
- **Precedence:** Strobe (ch8) > FX Scene (ch9) > Mode (ch6).
- **Canonical ch9 values** (multiples of 20): none=0, Morph=20, Color Cycle=40, Pastels=60, Spooky=80, Rando=100, Flame=120, Sunrise=140, Sunset=160, Clouds=180, Flicker=200, Twinkle=220, Meteor=240. Each scene owns a ±9 band.
- **Effect type enums (verbatim):** `SetTileEffect=719`, OFF=0, MORPH=2, FLAME=3, SKY=5; sky_type SUNRISE=0, SUNSET=1, CLOUDS=2.
- **719 payload layout (verbatim):** `reserved(u8), reserved(u8), instanceid(u32), type(u8), speed(u32, ms), duration(u64, ns; 0=forever), reserved(u32), reserved(u32), parameters(8×u32 = 32 bytes), palette_count(u8), palette(16×HSBK, 8 bytes each = 128 bytes)`. Total payload = 188 bytes.

---

## File Structure

- `lifx_control.py` (modify) — protocol builder, constants, FX decoding/palettes, dispatch, `CHANNELS_PER_FIXTURE`.
- `lifx_web.py` (modify) — FX dropdown on Bulbs tab, `manual_control` passes `fx_scene`, Help cheat-sheet.
- `README.md` (modify) — 9-channel table, FX scene section + value cheat sheet, fix stale "5ch"/"8ch" copy.
- `test_lifx.py` (create) — stdlib `unittest` for the protocol builder, FX decoding, channel width/packing, and dispatch precedence.

---

### Task 1: `SetTileEffect` packet builder + constants

**Files:**
- Modify: `lifx_control.py` (add constants near the other `MSG_*` definitions ~line 95; add functions after `set_waveform_optional` ~line 339)
- Test: `test_lifx.py` (create)

**Interfaces:**
- Consumes: existing `build_packet`, `hsbk_payload`, `send`, `parse_header`.
- Produces:
  - `MSG_SET_TILE_EFFECT = 719`, `MSG_GET_TILE_EFFECT = 718`, `MSG_STATE_TILE_EFFECT = 720`
  - `TILE_EFFECT_OFF=0`, `TILE_EFFECT_MORPH=2`, `TILE_EFFECT_FLAME=3`, `TILE_EFFECT_SKY=5`
  - `SKY_SUNRISE=0`, `SKY_SUNSET=1`, `SKY_CLOUDS=2`
  - `tile_effect_payload(effect_type, speed_ms, palette=None, sky_type=None, cloud_sat_min=51, cloud_sat_max=178, duration_ms=0, instanceid=0) -> bytes` (188 bytes)
  - `set_tile_effect(effect_type, speed_ms, palette=None, sky_type=None, duration_ms=0, ip=None)`
  - `set_tile_effect_off(ip=None)`
  - palette entries are `(hue 0-360, sat 0-100, brightness 0-100, kelvin)` tuples.

- [ ] **Step 1: Write the failing tests**

Create `test_lifx.py`:

```python
import struct
import unittest

import lifx_control as lifx


class TileEffectPayloadTest(unittest.TestCase):
    def test_morph_layout(self):
        palette = [(0, 100, 100, 3500), (240, 100, 100, 3500)]
        p = lifx.tile_effect_payload(lifx.TILE_EFFECT_MORPH, 5000,
                                     palette=palette, instanceid=0)
        self.assertEqual(len(p), 188)
        self.assertEqual(p[6], lifx.TILE_EFFECT_MORPH)        # type byte
        self.assertEqual(struct.unpack_from("<I", p, 7)[0], 5000)  # speed ms
        self.assertEqual(p[59], 2)                            # palette_count
        # first palette colour (HSBK) starts at offset 60
        hue, sat, bri, k = struct.unpack_from("<HHHH", p, 60)
        self.assertEqual(hue, lifx._scale(0, 360))
        self.assertEqual(sat, lifx._scale(100, 100))

    def test_flame_has_no_palette(self):
        p = lifx.tile_effect_payload(lifx.TILE_EFFECT_FLAME, 3000, instanceid=0)
        self.assertEqual(p[6], lifx.TILE_EFFECT_FLAME)
        self.assertEqual(p[59], 0)                            # palette_count == 0

    def test_sky_parameters(self):
        p = lifx.tile_effect_payload(lifx.TILE_EFFECT_SKY, 8000,
                                     sky_type=lifx.SKY_CLOUDS,
                                     cloud_sat_min=51, cloud_sat_max=178,
                                     instanceid=0)
        self.assertEqual(p[6], lifx.TILE_EFFECT_SKY)
        # parameters block starts at offset 27
        self.assertEqual(p[27], lifx.SKY_CLOUDS)              # sky_type @ params[0]
        self.assertEqual(p[31], 51)                           # cloud_sat_min @ params[4]
        self.assertEqual(p[35], 178)                          # cloud_sat_max @ params[8]

    def test_duration_zero_is_forever(self):
        p = lifx.tile_effect_payload(lifx.TILE_EFFECT_FLAME, 3000, instanceid=0)
        self.assertEqual(struct.unpack_from("<Q", p, 11)[0], 0)  # duration ns


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/lifx && python3 -m unittest test_lifx -v`
Expected: FAIL — `AttributeError: module 'lifx_control' has no attribute 'tile_effect_payload'`.

- [ ] **Step 3: Add the constants**

In `lifx_control.py`, after the existing `MSG_ECHO_RESPONSE = 59` block (~line 94):

```python
# Matrix (Tile/Candle) firmware effects — SetTileEffect.
MSG_GET_TILE_EFFECT = 718
MSG_SET_TILE_EFFECT = 719
MSG_STATE_TILE_EFFECT = 720
TILE_EFFECT_OFF = 0
TILE_EFFECT_MORPH = 2
TILE_EFFECT_FLAME = 3
TILE_EFFECT_SKY = 5
SKY_SUNRISE = 0
SKY_SUNSET = 1
SKY_CLOUDS = 2
```

- [ ] **Step 4: Implement the builder**

In `lifx_control.py`, after `set_waveform_optional` (~line 339):

```python
def tile_effect_payload(effect_type, speed_ms, palette=None, sky_type=None,
                        cloud_sat_min=51, cloud_sat_max=178, duration_ms=0,
                        instanceid=0):
    """Pack a SetTileEffect (719) payload (matrix devices like the Candle).

    Layout: 2 reserved bytes, instanceid u32, type u8, speed u32 (ms),
    duration u64 (ns; 0 = run forever), 2 reserved u32, parameters 8xu32
    (32 bytes), palette_count u8, palette (fixed 16 HSBK colours = 128 bytes,
    zero-padded). MORPH uses the palette; SKY writes sky_type + cloud
    saturation into parameters; FLAME needs neither.
    """
    palette = list(palette or [])[:16]
    params = bytearray(32)
    if effect_type == TILE_EFFECT_SKY and sky_type is not None:
        params[0] = sky_type & 0xFF
        params[4] = max(0, min(255, int(cloud_sat_min)))
        params[8] = max(0, min(255, int(cloud_sat_max)))

    head = struct.pack("<BBIBIQII",
                       0, 0,                       # 2 reserved bytes
                       instanceid & 0xFFFFFFFF,    # instanceid
                       effect_type,                # type
                       int(speed_ms),              # speed (ms)
                       int(duration_ms) * 1_000_000,  # duration (ms -> ns)
                       0, 0)                        # 2 reserved u32
    pal = bytearray()
    padded = palette + [(0, 0, 0, 0)] * (16 - len(palette))
    for hue, sat, bri, kelvin in padded:
        pal += hsbk_payload(hue, sat, bri, kelvin)
    return head + bytes(params) + struct.pack("<B", len(palette)) + bytes(pal)


def set_tile_effect(effect_type, speed_ms, palette=None, sky_type=None,
                    duration_ms=0, ip=None):
    """Start (or change) a matrix firmware effect on the bulb."""
    payload = tile_effect_payload(effect_type, speed_ms, palette=palette,
                                  sky_type=sky_type, duration_ms=duration_ms,
                                  instanceid=random.getrandbits(32))
    send(build_packet(MSG_SET_TILE_EFFECT, payload), ip)


def set_tile_effect_off(ip=None):
    """Stop any running matrix firmware effect."""
    send(build_packet(MSG_SET_TILE_EFFECT,
                      tile_effect_payload(TILE_EFFECT_OFF, 0)), ip)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd ~/lifx && python3 -m unittest test_lifx -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
cd ~/lifx && git add lifx_control.py test_lifx.py
git commit -m "feat: add SetTileEffect (719) builder for matrix firmware effects"
```

---

### Task 2: FX scene decoding, palettes, speed mapping

**Files:**
- Modify: `lifx_control.py` (after the `EFFECT_RANGES`/`decode_mode` block ~line 546)
- Test: `test_lifx.py`

**Interfaces:**
- Produces:
  - `FX_RANGES` (list of `(threshold, name)`) and `decode_fx_scene(value) -> str`
  - `FX_FIRMWARE` (set of firmware scene names), `FX_SCRIPT` (set of script scene names)
  - `FX_PALETTES` dict: scene name -> list of `(hue, sat)`
  - `fx_firmware_spec(scene) -> (effect_type, palette|None, sky_type|None)` where palette is a list of `(hue, sat, 100, 3500)`
  - `tile_speed_ms(speed_byte, fast_ms=1000, slow_ms=30000) -> int`
  - `SKY_SUPPORTED = True` flag and `FX_SKY_FALLBACK` palettes (used only when `SKY_SUPPORTED` is False)

- [ ] **Step 1: Write the failing tests**

Append to `test_lifx.py`:

```python
class FxSceneDecodeTest(unittest.TestCase):
    def test_band_boundaries(self):
        cases = {0: "none", 9: "none", 10: "morph", 20: "morph", 29: "morph",
                 40: "color_cycle", 60: "pastels", 80: "spooky", 100: "rando",
                 120: "flame", 140: "sunrise", 160: "sunset", 180: "clouds",
                 200: "flicker", 220: "twinkle", 240: "meteor", 255: "meteor"}
        for value, name in cases.items():
            self.assertEqual(lifx.decode_fx_scene(value), name, f"value {value}")

    def test_firmware_vs_script_partition(self):
        self.assertIn("clouds", lifx.FX_FIRMWARE)
        self.assertIn("flicker", lifx.FX_SCRIPT)
        self.assertFalse(lifx.FX_FIRMWARE & lifx.FX_SCRIPT)

    def test_firmware_spec_morph_and_sky(self):
        et, pal, sky = lifx.fx_firmware_spec("spooky")
        self.assertEqual(et, lifx.TILE_EFFECT_MORPH)
        self.assertTrue(pal and len(pal[0]) == 4)        # (hue,sat,bri,kelvin)
        et, pal, sky = lifx.fx_firmware_spec("sunset")
        self.assertEqual((et, sky), (lifx.TILE_EFFECT_SKY, lifx.SKY_SUNSET))
        et, pal, sky = lifx.fx_firmware_spec("flame")
        self.assertEqual(et, lifx.TILE_EFFECT_FLAME)

    def test_tile_speed_monotonic(self):
        self.assertGreater(lifx.tile_speed_ms(0), lifx.tile_speed_ms(255))
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/lifx && python3 -m unittest test_lifx.FxSceneDecodeTest -v`
Expected: FAIL — `AttributeError: ... 'decode_fx_scene'`.

- [ ] **Step 3: Implement the decoding + palettes**

In `lifx_control.py`, after `decode_mode` (~line 546):

```python
# FX Scene channel (ch9) value ranges -> scene name (canonical value in comment).
FX_RANGES = [
    (10, "none"),         # 0     no FX (fall through to the Mode channel)
    (30, "morph"),        # 20    firmware MORPH, default palette
    (50, "color_cycle"),  # 40    firmware MORPH, rainbow palette
    (70, "pastels"),      # 60    firmware MORPH, pastel palette
    (90, "spooky"),       # 80    firmware MORPH, orange/purple/green
    (110, "rando"),       # 100   firmware MORPH, random palette per arm
    (130, "flame"),       # 120   firmware FLAME
    (150, "sunrise"),     # 140   firmware SKY, sky_type=SUNRISE
    (170, "sunset"),      # 160   firmware SKY, sky_type=SUNSET
    (190, "clouds"),      # 180   firmware SKY, sky_type=CLOUDS
    (210, "flicker"),     # 200   script-generated
    (230, "twinkle"),     # 220   script-generated
    (256, "meteor"),      # 240   script-generated
]

FX_FIRMWARE = {"morph", "color_cycle", "pastels", "spooky", "rando",
               "flame", "sunrise", "sunset", "clouds"}
FX_SCRIPT = {"flicker", "twinkle", "meteor"}

# Morph palettes as (hue 0-360, saturation 0-100).
FX_PALETTES = {
    "morph":       [(0, 100), (40, 100), (200, 100), (280, 100), (120, 100)],
    "color_cycle": [(0, 100), (60, 100), (120, 100), (180, 100), (240, 100),
                    (300, 100)],
    "pastels":     [(0, 40), (50, 40), (120, 35), (200, 40), (280, 40),
                    (330, 40)],
    "spooky":      [(25, 100), (280, 100), (120, 100)],
}

# Used only if a Candle's firmware turns out not to support the SKY effect:
# flip SKY_SUPPORTED to False and the three SKY scenes become Morph palettes.
SKY_SUPPORTED = True
FX_SKY_FALLBACK = {
    "sunrise": [(20, 90), (35, 80), (50, 60)],
    "sunset":  [(10, 100), (300, 70), (30, 90)],
    "clouds":  [(210, 30), (0, 0), (220, 20)],
}


def decode_fx_scene(value):
    """Map the FX Scene channel byte (0-255) to a scene name."""
    for threshold, name in FX_RANGES:
        if value < threshold:
            return name
    return "meteor"


def tile_speed_ms(speed_byte, fast_ms=1000, slow_ms=30000):
    """Map the Speed channel (0=slow .. 255=fast) to a tile-effect period (ms)."""
    frac = max(0, min(255, speed_byte)) / 255.0
    return int(slow_ms + (fast_ms - slow_ms) * frac)


def fx_firmware_spec(scene):
    """Return (effect_type, palette, sky_type) for a firmware FX scene.

    palette is a list of (hue, sat, brightness, kelvin); sky_type is set only
    for SKY scenes. Returns None for script/none scenes.
    """
    def _pal(pairs):
        return [(h, s, 100, 3500) for (h, s) in pairs]

    if scene in ("morph", "color_cycle", "pastels", "spooky"):
        return TILE_EFFECT_MORPH, _pal(FX_PALETTES[scene]), None
    if scene == "rando":
        return (TILE_EFFECT_MORPH,
                [(random.uniform(0, 360), 100, 100, 3500) for _ in range(6)],
                None)
    if scene == "flame":
        return TILE_EFFECT_FLAME, None, None
    if scene in ("sunrise", "sunset", "clouds"):
        if not SKY_SUPPORTED:
            return TILE_EFFECT_MORPH, _pal(FX_SKY_FALLBACK[scene]), None
        sky = {"sunrise": SKY_SUNRISE, "sunset": SKY_SUNSET,
               "clouds": SKY_CLOUDS}[scene]
        return TILE_EFFECT_SKY, None, sky
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/lifx && python3 -m unittest test_lifx -v`
Expected: PASS (all tests so far).

- [ ] **Step 5: Commit**

```bash
cd ~/lifx && git add lifx_control.py test_lifx.py
git commit -m "feat: FX scene decoding, palettes, and speed mapping"
```

---

### Task 3: Widen fixtures to 9 channels (ch9 = FX Scene)

**Files:**
- Modify: `lifx_control.py` — `CHANNELS_PER_FIXTURE` (~line 518), `decode_controls` (~line 705), `print_fixture_table` (~line 652), listener per-fixture init (~line 848)
- Test: `test_lifx.py`

**Interfaces:**
- Consumes: `decode_fx_scene` (Task 2), `auto_assign` (existing).
- Produces: `CHANNELS_PER_FIXTURE == 9`; `decode_controls` output dict gains `"fx_scene"` key.

- [ ] **Step 1: Write the failing tests**

Append to `test_lifx.py`:

```python
class ChannelWidthTest(unittest.TestCase):
    def test_nine_channels(self):
        self.assertEqual(lifx.CHANNELS_PER_FIXTURE, 9)

    def test_decode_controls_reads_fx(self):
        dmx = [0] * 9
        dmx[8] = 80                       # ch9 = Spooky band
        c = lifx.decode_controls(dmx, 0, 3500)
        self.assertEqual(c["fx_scene"], "spooky")
        self.assertEqual(c["strobe"], 0)

    def test_auto_assign_packs_56_per_universe(self):
        bulbs = [{"label": f"b{i:02d}", "ip": f"10.0.0.{i}", "mac": f"m{i}"}
                 for i in range(60)]
        fx = lifx.auto_assign(bulbs)
        # 9 ch each -> 56 fixtures fit (56*9=504); the 57th rolls to universe 1.
        self.assertEqual(fx[55]["universe"], 0)
        self.assertEqual(fx[56]["universe"], 1)
        self.assertEqual(fx[56]["address"], 1)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/lifx && python3 -m unittest test_lifx.ChannelWidthTest -v`
Expected: FAIL — `CHANNELS_PER_FIXTURE` is 8 and `c["fx_scene"]` missing.

- [ ] **Step 3: Bump the constant and its comment**

In `lifx_control.py` (~line 518), change:

```python
CHANNELS_PER_FIXTURE = 8  # R, G, B, Amber, Intensity, Mode, Speed, Strobe
```
to:
```python
CHANNELS_PER_FIXTURE = 9  # R,G,B,Amber,Intensity,Mode,Speed,Strobe,FX Scene
```

- [ ] **Step 4: Read ch9 in `decode_controls`**

In `lifx_control.py` `decode_controls` (~line 705), add the `fx_scene` key:

```python
def decode_controls(dmx, i, kelvin):
    """Read a 9-channel fixture starting at index i into a control dict."""
    r, g, b, a, it = dmx[i], dmx[i + 1], dmx[i + 2], dmx[i + 3], dmx[i + 4]
    return {
        "hsbk": rgba_to_hsbk(r, g, b, a, it, kelvin),
        "intensity": it / 255.0 * 100.0,
        "kelvin": kelvin,
        "mode": decode_mode(dmx[i + 5]),
        "speed": dmx[i + 6],
        "strobe": dmx[i + 7],
        "fx_scene": decode_fx_scene(dmx[i + 8]),
    }
```

- [ ] **Step 5: Update the printed header + listener init**

In `print_fixture_table` (~line 653), change the header string:

```python
    print("DMX fixture map (9ch each: R,G,B,Amber,Intensity,Mode,Speed,Strobe,FX):")
```

In `listen_artnet`'s per-fixture init loop (~line 848), add a tile-effect arm slot next to the existing `f["armed"]` line:

```python
        f["armed"], f["rearm_at"] = None, 0.0
        f["tile_armed"] = None
```

- [ ] **Step 6: Run to verify pass**

Run: `cd ~/lifx && python3 -m unittest test_lifx -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd ~/lifx && git add lifx_control.py test_lifx.py
git commit -m "feat: widen DMX fixtures to 9 channels (ch9 = FX Scene)"
```

---

### Task 4: Dispatch FX firmware scenes in `service_fixture` (with precedence)

**Files:**
- Modify: `lifx_control.py` — `service_fixture` (~line 752), add `_drop_tile_effect` helper
- Test: `test_lifx.py`

**Interfaces:**
- Consumes: `fx_firmware_spec`, `tile_speed_ms`, `FX_FIRMWARE`, `FX_SCRIPT`, `set_tile_effect`, `set_tile_effect_off`, `_service_script_fx` (added in Task 5; for this task that branch may call a stub).
- Produces: `_drop_tile_effect(f)`; updated `service_fixture` precedence Strobe > FX > Mode.

- [ ] **Step 1: Write the failing tests**

Append to `test_lifx.py`:

```python
class DispatchPrecedenceTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._orig = lifx.send
        lifx.send = lambda pkt, ip=None: self.sent.append(lifx.parse_header(pkt)[0])

    def tearDown(self):
        lifx.send = self._orig

    def _fixture(self):
        f = {"live_ip": "127.0.0.1", "label": "t",
             "last_key": None, "armed": None, "tile_armed": None,
             "last_send": 0.0, "rearm_at": 0.0, "phase": 0.0,
             "anim_last": 0.0, "step": 0, "step_at": 0.0}
        return f

    def _controls(self, **over):
        c = {"hsbk": (0, 100, 100, 3500), "intensity": 100, "kelvin": 3500,
             "mode": "static", "speed": 128, "strobe": 0, "fx_scene": "none"}
        c.update(over)
        return c

    def test_strobe_beats_fx(self):
        f = self._fixture()
        c = self._controls(strobe=200, fx_scene="clouds")
        lifx.service_fixture(f, c, 1000.0, 0.0, 120, False)
        self.assertIn(lifx.MSG_SET_WAVEFORM_OPTIONAL, self.sent)   # strobe armed
        self.assertNotIn(lifx.MSG_SET_TILE_EFFECT, self.sent)      # FX suppressed

    def test_fx_firmware_arms_tile_effect(self):
        f = self._fixture()
        c = self._controls(fx_scene="clouds")
        lifx.service_fixture(f, c, 1000.0, 0.0, 120, False)
        self.assertIn(lifx.MSG_SET_TILE_EFFECT, self.sent)
        self.assertIsNotNone(f["tile_armed"])

    def test_fx_arms_only_once(self):
        f = self._fixture()
        c = self._controls(fx_scene="clouds")
        lifx.service_fixture(f, c, 1000.0, 0.0, 120, False)
        self.sent.clear()
        lifx.service_fixture(f, c, 1000.1, 0.0, 120, False)        # same sig
        self.assertEqual(self.sent, [])                            # no re-send

    def test_leaving_fx_sends_off(self):
        f = self._fixture()
        f["tile_armed"] = ("fx", "clouds", 30000)
        c = self._controls(fx_scene="none", mode="static")
        lifx.service_fixture(f, c, 1000.0, 0.0, 120, False)
        self.assertIn(lifx.MSG_SET_TILE_EFFECT, self.sent)         # OFF packet
        self.assertIsNone(f["tile_armed"])
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/lifx && python3 -m unittest test_lifx.DispatchPrecedenceTest -v`
Expected: FAIL — FX branch not present; `clouds` falls through to static color.

- [ ] **Step 3: Add the helper**

In `lifx_control.py`, just above `service_fixture` (~line 751):

```python
def _drop_tile_effect(f):
    """Stop a running firmware tile effect on the bulb, once, if one is armed."""
    if f.get("tile_armed"):
        try:
            set_tile_effect_off(f["live_ip"])
        except OSError:
            pass
        f["tile_armed"] = None
        f["last_key"] = None  # force a fresh SetColor when we resume normal output
```

- [ ] **Step 4: Wire precedence into `service_fixture`**

In `lifx_control.py`, edit `service_fixture`. Add `_drop_tile_effect(f)` inside the strobe block, then insert the FX block between strobe (step 1) and native (step 2):

```python
    # 1) Strobe (dedicated channel) is an overlay that takes priority.
    if c["strobe"] > 0:
        _drop_tile_effect(f)
        period = strobe_to_period_ms(c["strobe"])
        target = (base[0], base[1], 0, base[3])
        sig = ("strobe", round(period), _hsbk_key(base))
        _arm_native(f, WAVEFORM_PULSE, base, target, (False, False, True, False),
                    period, sig, now, verbose, label)
        return

    # 2) FX Scene (ch9): firmware matrix effects, then script-emulated looks.
    fx = c.get("fx_scene", "none")
    if fx in FX_FIRMWARE:
        speed_ms = tile_speed_ms(c["speed"])
        sig = ("fx", fx, round(speed_ms))
        if f.get("tile_armed") != sig:
            effect_type, palette, sky_type = fx_firmware_spec(fx)
            set_tile_effect(effect_type, speed_ms, palette=palette,
                            sky_type=sky_type, ip=f["live_ip"])
            f["tile_armed"] = sig
            f["armed"] = None
            if verbose:
                print(f"[{label}] fx {fx} speed={speed_ms}ms")
        return
    if fx in FX_SCRIPT:
        _drop_tile_effect(f)
        _service_script_fx(f, fx, c, now, min_interval, smooth_ms, verbose, label)
        return

    # Leaving any firmware tile effect -> turn it off once.
    _drop_tile_effect(f)
```

(The existing "2) Native firmware effects" comment/block becomes step 3 — renumber its comment to `# 3)` and the static block to `# 4)`. No code change beyond the comment numbers.)

- [ ] **Step 5: Add a temporary stub for `_service_script_fx`**

So Task 4 runs green before Task 5, add a stub just above `service_fixture` (it will be replaced in Task 5):

```python
def _service_script_fx(f, scene, c, now, min_interval, smooth_ms, verbose, label):
    pass  # replaced in Task 5
```

- [ ] **Step 6: Run to verify pass**

Run: `cd ~/lifx && python3 -m unittest test_lifx -v`
Expected: PASS (all classes).

- [ ] **Step 7: Commit**

```bash
cd ~/lifx && git add lifx_control.py test_lifx.py
git commit -m "feat: dispatch FX firmware scenes with Strobe > FX > Mode precedence"
```

---

### Task 5: Script-emulated scenes (Flicker, Twinkle, Meteor)

**Files:**
- Modify: `lifx_control.py` — replace the `_service_script_fx` stub
- Test: `test_lifx.py`

**Interfaces:**
- Consumes: `set_color`, `tile_speed_ms`, fixture state keys `last_send`.
- Produces: working `_service_script_fx(f, scene, c, now, min_interval, smooth_ms, verbose, label)` that streams whole-bulb `SetColor` frames.

- [ ] **Step 1: Write the failing test**

Append to `test_lifx.py`:

```python
class ScriptFxTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._orig = lifx.send
        lifx.send = lambda pkt, ip=None: self.sent.append(lifx.parse_header(pkt)[0])

    def tearDown(self):
        lifx.send = self._orig

    def test_flicker_streams_setcolor(self):
        f = {"live_ip": "127.0.0.1", "label": "t", "last_send": 0.0}
        c = {"hsbk": (30, 80, 100, 3500), "intensity": 100, "kelvin": 3500,
             "speed": 128}
        lifx._service_script_fx(f, "flicker", c, 1000.0, 0.0, 120, False, "t")
        self.assertIn(lifx.MSG_SET_COLOR, self.sent)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/lifx && python3 -m unittest test_lifx.ScriptFxTest -v`
Expected: FAIL — stub sends nothing (`self.sent` empty).

- [ ] **Step 3: Implement the script scenes**

Replace the `_service_script_fx` stub in `lifx_control.py` with:

```python
def _service_script_fx(f, scene, c, now, min_interval, smooth_ms, verbose, label):
    """Whole-bulb, bridge-streamed approximations of app FX (Flicker/Twinkle/
    Meteor). Rate-limited like the existing rainbow/candle effects."""
    base = c["hsbk"]
    intensity, kelvin = c["intensity"], c["kelvin"]
    if scene == "flicker":
        if now - f["last_send"] >= max(min_interval, 0.05):
            bri = max(0.0, min(100.0, intensity * random.uniform(0.6, 1.0)))
            set_color(base[0], base[1], bri, kelvin, 60, f["live_ip"])
            f["last_send"] = now
    elif scene == "twinkle":
        if now - f["last_send"] >= max(min_interval, 0.08):
            dip = random.random() < 0.5
            bri = (intensity if not dip
                   else max(0.0, intensity * random.uniform(0.2, 0.5)))
            set_color(base[0], base[1], bri, kelvin, 50, f["live_ip"])
            f["last_send"] = now
    elif scene == "meteor":
        # Fast asymmetric pulse: sharp on, decay to 0 (whole-bulb approximation).
        period_s = max(0.2, tile_speed_ms(c["speed"]) / 10000.0)
        phase = (now % period_s) / period_s
        if now - f["last_send"] >= min_interval:
            set_color(base[0], base[1], max(0.0, intensity * (1.0 - phase)),
                      kelvin, 40, f["live_ip"])
            f["last_send"] = now
    if verbose:
        print(f"[{label}] script fx {scene}")
```

- [ ] **Step 4: Run to verify pass**

Run: `cd ~/lifx && python3 -m unittest test_lifx -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/lifx && git add lifx_control.py test_lifx.py
git commit -m "feat: script-emulated Flicker/Twinkle/Meteor FX scenes"
```

---

### Task 6: Web UI — FX dropdown, manual control, Help cheat-sheet

**Files:**
- Modify: `lifx_web.py` — `manual_control` (~line 495), `renderBulbs` JS (~line 1083), `control()` JS (~line 1141), `renderHelp` JS (~line 1319)

**Interfaces:**
- Consumes: `lifx.service_fixture` already called by `manual_control`; the control dict it builds gains `"fx_scene"`.
- Produces: an FX dropdown per bulb whose value is POSTed as `fx` and applied via the same dispatch.

- [ ] **Step 1: Pass `fx_scene` through `manual_control`**

In `lifx_web.py` `manual_control` (~line 508), add `fx_scene` to the control dict `c`:

```python
        c = {"hsbk": hsbk, "intensity": intensity / 255 * 100,
             "kelvin": self.settings["kelvin"], "mode": mode,
             "speed": int(body.get("speed", 128)),
             "strobe": int(body.get("strobe", 0)),
             "fx_scene": body.get("fx", "none")}
```

- [ ] **Step 2: Add an FX dropdown to the bulb body**

In `lifx_web.py` `renderBulbs` (~line 1092), after the Mode/Speed/Strobe `.row`, add:

```javascript
        <div class=row style="margin-top:12px">
          <label class=fld>FX Scene<select id="fx_${x.id}" onchange="control('${x.id}')">
            <option value=none>None</option><option value=morph>Morph</option>
            <option value=color_cycle>Color cycle</option><option value=pastels>Pastels</option>
            <option value=spooky>Spooky</option><option value=rando>Rando</option>
            <option value=flame>Flame</option><option value=sunrise>Sunrise</option>
            <option value=sunset>Sunset</option><option value=clouds>Clouds</option>
            <option value=flicker>Flicker</option><option value=twinkle>Twinkle</option>
            <option value=meteor>Meteor</option>
          </select></label>
        </div>
```

- [ ] **Step 3: Send `fx` in `control()`**

In `lifx_web.py` `control()` JS (~line 1144), add `fx` to the POST body:

```javascript
  const j=await api('/api/fixture/control',{id,r,g,b,a:0,
    intensity:+document.getElementById('int_'+id).value,
    mode:document.getElementById('mode_'+id).value,
    speed:+document.getElementById('spd_'+id).value,
    strobe:+document.getElementById('strb_'+id).value,
    fx:document.getElementById('fx_'+id).value});
```

- [ ] **Step 4: Add the FX cheat-sheet to Help**

In `lifx_web.py` `renderHelp` (~line 1386), add a new card after the "Effect / Mode channel" card:

```javascript
  <div class="card help">
    <h2>FX Scene channel (channel 9)</h2>
    <p>Set channel 9 to one of these values to trigger a Candle FX scene.
    Morph/Flame/Sky run on the bulb firmware; Flicker/Twinkle/Meteor are
    generated by the bridge.</p>
    <div class=tablewrap><table><tr><th>Set ch9 to</th><th>Scene</th><th>Runs on</th></tr>
    <tr><td>0</td><td>None (use Mode channel)</td><td>—</td></tr>
    <tr><td>20</td><td>Morph</td><td>bulb firmware</td></tr>
    <tr><td>40</td><td>Color cycle</td><td>bulb firmware</td></tr>
    <tr><td>60</td><td>Pastels</td><td>bulb firmware</td></tr>
    <tr><td>80</td><td>Spooky</td><td>bulb firmware</td></tr>
    <tr><td>100</td><td>Rando</td><td>bulb firmware</td></tr>
    <tr><td>120</td><td>Flame</td><td>bulb firmware</td></tr>
    <tr><td>140</td><td>Sunrise</td><td>bulb firmware (Sky)</td></tr>
    <tr><td>160</td><td>Sunset</td><td>bulb firmware (Sky)</td></tr>
    <tr><td>180</td><td>Clouds</td><td>bulb firmware (Sky)</td></tr>
    <tr><td>200</td><td>Flicker</td><td><b>script-generated</b></td></tr>
    <tr><td>220</td><td>Twinkle</td><td><b>script-generated</b></td></tr>
    <tr><td>240</td><td>Meteor</td><td><b>script-generated</b></td></tr></table></div>
    <p class=muted style="margin-top:10px">Strobe (ch8) overrides FX; FX overrides the Mode channel.
    Speed (ch7) sets the FX rate. Each scene has a &plusmn;9 band around its value.</p>
  </div>
```

- [ ] **Step 5: Smoke-test the web module imports and serves**

Run: `cd ~/lifx && python3 -c "import lifx_web; print('ok', 'fx_' in lifx_web.APP_HTML)"`
Expected: `ok True`

- [ ] **Step 6: Commit**

```bash
cd ~/lifx && git add lifx_web.py
git commit -m "feat: web UI FX Scene dropdown + Help cheat-sheet"
```

---

### Task 7: Docs (README) + full verification

**Files:**
- Modify: `README.md` — channel table (~line 86), fixture-map examples that say "5ch"/"8ch" (~line 138), QLab patch instructions (~line 165), add FX value table.

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the fixture channel table**

In `README.md`, the "QLab integration" fixture table (~line 86) already lists 8 channels; add a row below Strobe:

```
| +8     | FX Scene  | ranged selector for Candle FX scenes (see table below) |
```

And change "Each bulb is an **8-channel fixture**" to "**9-channel fixture**", and "At 8 channels each, 64 bulbs fit" to "At 9 channels each, 56 bulbs fit".

- [ ] **Step 2: Fix stale channel-count copy**

In `README.md` search for `5ch` and `5 channels` (~line 138) and the auto-discover example, and change the printed example header to:
```
DMX fixture map (9ch each: R,G,B,Amber,Intensity,Mode,Speed,Strobe,FX):
```
Update the QLab patch step (~line 165) to say a **9-channel** fixture with channel order `R, G, B, Amber, Intensity, Mode, Speed, Strobe, FX Scene`.

- [ ] **Step 3: Add an FX Scene section**

In `README.md`, after the "Effect / Mode channel (ch+5)" table, add:

```markdown
### FX Scene channel (ch+8)

Candle (matrix) bulbs can run their built-in FX scenes. Set ch+8 to the
canonical value for a scene:

| ch+8 | Scene | Engine |
|------|-------|--------|
| 0    | none (use Mode channel) | — |
| 20   | Morph | firmware |
| 40   | Color cycle | firmware (Morph palette) |
| 60   | Pastels | firmware (Morph palette) |
| 80   | Spooky | firmware (Morph palette) |
| 100  | Rando | firmware (Morph palette) |
| 120  | Flame | firmware |
| 140  | Sunrise | firmware (Sky) |
| 160  | Sunset | firmware (Sky) |
| 180  | Clouds | firmware (Sky) |
| 200  | Flicker | script-generated |
| 220  | Twinkle | script-generated |
| 240  | Meteor | script-generated |

Strobe (ch+7) overrides FX; FX overrides the Mode channel (ch+5). Speed (ch+6)
sets the FX rate. Each scene occupies a ±9 band around its value. Sky scenes
(Sunrise/Sunset/Clouds) need Candle firmware that supports the SKY effect.
Music visualizers and the multizone Move effect have no LAN trigger and are
not included.
```

- [ ] **Step 4: Full verification**

Run: `cd ~/lifx && python3 -m unittest -v && python3 -c "import lifx_web" && python3 lifx_control.py --help >/dev/null && echo ALLGREEN`
Expected: all tests PASS and `ALLGREEN` printed.

- [ ] **Step 5: Commit**

```bash
cd ~/lifx && git add README.md
git commit -m "docs: document the FX Scene channel (ch9) and 9-channel fixtures"
```

---

## Post-implementation (outside the per-task loop)

- **Push** the `lifx-fx-dmx` branch to GitHub (user pre-authorized): `git push -u origin lifx-fx-dmx`.
- **Live verification on the Raspberry Pi 5 / real Candle** — confirm Morph/Flame trigger; confirm whether SKY (Sunrise/Sunset/Clouds) works on this Candle's firmware and, if not, flip `SKY_SUPPORTED = False` (one-line change activating the Morph fallback palettes). Finalize the SKY `parameters` byte offsets against the Photons source if the bulb shows them wrong.

## Self-Review

- **Spec coverage:** ch9 selector + bands ✓ (T2/T3), canonical values ✓ (T2 comments, T6/T7 tables), SetTileEffect builder ✓ (T1), Morph/Flame/Sky ✓ (T2/T4), palette presets ✓ (T2), script Flicker/Twinkle/Meteor ✓ (T5), precedence Strobe>FX>Mode ✓ (T4), leaving-cleanup OFF ✓ (T4), 9-channel packing/CSV/ArtPoll via constant ✓ (T3), web dropdown + Help ✓ (T6), README ✓ (T7), tests no-hardware ✓ (T1–T5), SKY caveat + fallback ✓ (T2 `SKY_SUPPORTED`), SKY/params verification ✓ (post-impl).
- **Deviation from spec:** the spec floated a "canonical FX value column in the patch CSV"; since the value table is global (identical for every fixture), it is published in Help + README instead of a redundant per-row CSV column. Flagged for user confirmation.
- **Placeholder scan:** none — every step has concrete code/commands. (The `_service_script_fx` stub in T4 is intentional and replaced in T5.)
- **Type consistency:** `tile_effect_payload`/`set_tile_effect` signatures, `fx_firmware_spec` 3-tuple, `decode_fx_scene` names, fixture key `tile_armed`, and the `_service_script_fx` 8-arg signature are consistent across T1–T6.
