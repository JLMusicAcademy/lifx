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
