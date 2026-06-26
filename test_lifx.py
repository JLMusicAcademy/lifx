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


class ChannelWidthTest(unittest.TestCase):
    def test_eight_channels(self):
        self.assertEqual(lifx.CHANNELS_PER_FIXTURE, 8)

    def test_decode_controls_reads_fx(self):
        dmx = [0] * 8
        dmx[7] = 80                       # ch8 = Spooky band
        c = lifx.decode_controls(dmx, 0, 3500)
        self.assertEqual(c["fx_scene"], "spooky")
        self.assertEqual(c["strobe"], 0)

    def test_decode_controls_no_amber(self):
        # Pure blue with the (now-removed) amber slot at full must stay blue:
        # channel 4 is Intensity, not Amber, so 255 there = full brightness.
        dmx = [0, 0, 255, 255, 0, 0, 0, 0]   # R G B Intensity Mode Speed Strobe FX
        c = lifx.decode_controls(dmx, 0, 3500)
        h, s, b, k = c["hsbk"]
        self.assertAlmostEqual(h, 240.0, places=0)   # blue hue
        self.assertAlmostEqual(s, 100.0, places=0)   # fully saturated (no amber wash)
        self.assertGreater(b, 0)                     # intensity 255 -> lit

    def test_auto_assign_packs_64_per_universe(self):
        bulbs = [{"label": f"b{i:02d}", "ip": f"10.0.0.{i}", "mac": f"m{i}"}
                 for i in range(70)]
        fx = lifx.auto_assign(bulbs)
        # 8 ch each -> 64 fixtures fit (64*8=512); the 65th rolls to universe 1.
        self.assertEqual(fx[63]["universe"], 0)
        self.assertEqual(fx[64]["universe"], 1)
        self.assertEqual(fx[64]["address"], 1)


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


if __name__ == "__main__":
    unittest.main()
