import unittest

from tools.scale_bridge import (
    Shot,
    ShotDetector,
    ScaleSample,
    build_add_brew_event,
    parse_ble_weight,
    parse_decent_weight,
    parse_gatt_weight,
)


class ScaleParserTests(unittest.TestCase):
    def test_decent_weight_frames(self) -> None:
        # 03 ce 00 7b 00 00 b6 -> command 0xCE, 0x007b = 123 -> 12.3 g
        self.assertEqual(parse_decent_weight(bytes.fromhex("03ce007b0000b6")), 12.3)
        self.assertEqual(
            parse_decent_weight(bytes.fromhex("03ce007b0102030000b6")), 12.3
        )
        self.assertEqual(
            parse_decent_weight(bytes.fromhex("03ca00c80102030000b2")), 20.0
        )

    def test_decent_weight_rejects_other_frames(self) -> None:
        self.assertIsNone(parse_decent_weight(b""))
        self.assertIsNone(parse_decent_weight(bytes.fromhex("03aa007b0000b6")))
        self.assertIsNone(parse_decent_weight(bytes.fromhex("010203")))

    def test_generic_gatt_weight_units(self) -> None:
        # flags 0 = kg, raw 200 * 0.005 kg = 1000 g
        self.assertAlmostEqual(parse_gatt_weight(bytes([0x00, 0xC8, 0x00])), 1000.0)
        # flags 1 = lb, raw 100 * 0.01 lb = 1 lb = 453.59237 g
        self.assertAlmostEqual(parse_gatt_weight(bytes([0x01, 0x64, 0x00])), 453.59237)
        self.assertIsNone(parse_gatt_weight(b"\x00"))

    def test_auto_parser_falls_back_to_generic(self) -> None:
        self.assertEqual(parse_ble_weight(bytes.fromhex("03ce007b0000b6"), "auto"), 12.3)
        self.assertAlmostEqual(
            parse_ble_weight(bytes([0x00, 0xC8, 0x00]), "auto"), 1000.0
        )


class ShotDetectorTests(unittest.TestCase):
    def _feed(self, detector: ShotDetector, points: list[tuple[float, float]]):
        shot = None
        for grams, ms in points:
            shot = detector.add(ScaleSample(grams, ms)) or shot
        return shot

    def test_detects_a_stable_shot(self) -> None:
        detector = ShotDetector(
            baseline_samples=3,
            min_duration_ms=1000.0,
            valid_min_ms=3000.0,
            stable_ms=1000.0,
        )
        points = [
            (0.0, 0.0), (0.0, 500.0), (0.0, 1000.0),
            (2.0, 1500.0), (6.0, 2000.0), (12.0, 2500.0),
            (18.0, 3000.0), (20.0, 3500.0), (20.0, 4000.0),
            (20.05, 4500.0), (20.0, 5000.0),
        ]
        shot = self._feed(detector, points)
        self.assertIsNotNone(shot)
        assert shot is not None
        self.assertAlmostEqual(shot.mass_g, 20.0, places=1)
        self.assertGreaterEqual(shot.duration_ms, 3000.0)
        self.assertTrue(shot.valid)
        self.assertEqual(shot.reason, "stable weight")

    def test_short_shot_is_invalid_but_reported(self) -> None:
        detector = ShotDetector(
            baseline_samples=2,
            min_duration_ms=200.0,
            valid_min_ms=15000.0,
            stable_ms=200.0,
        )
        points = [
            (0.0, 0.0), (0.0, 100.0),
            (1.0, 200.0), (5.0, 400.0), (5.0, 600.0), (5.0, 800.0),
        ]
        shot = self._feed(detector, points)
        self.assertIsNotNone(shot)
        assert shot is not None
        self.assertFalse(shot.valid)
        self.assertGreater(shot.mass_g, 0)

    def test_cup_removal_ends_the_shot(self) -> None:
        detector = ShotDetector(
            baseline_samples=2,
            min_duration_ms=500.0,
            stable_ms=10_000.0,
            drop_threshold_g=2.0,
        )
        points = [
            (0.0, 0.0), (0.0, 100.0),
            (2.0, 200.0), (20.0, 700.0), (20.0, 900.0), (2.0, 1100.0),
        ]
        shot = self._feed(detector, points)
        self.assertIsNotNone(shot)
        assert shot is not None
        self.assertEqual(shot.reason, "cup removed / weight drop")
        self.assertAlmostEqual(shot.mass_g, 20.0, places=1)

    def test_rejects_non_monotonic_timestamps(self) -> None:
        detector = ShotDetector()
        detector.add(ScaleSample(0.0, 10.0))
        with self.assertRaises(ValueError):
            detector.add(ScaleSample(0.0, 10.0))


class EventTests(unittest.TestCase):
    def test_event_units_and_keys(self) -> None:
        shot = Shot(
            start_ms=100.0,
            end_ms=27_000.0,
            duration_ms=26_900.0,
            baseline_g=0.0,
            mass_g=18.4,
            peak_g=18.4,
            sample_count=270,
            valid=True,
            reason="stable weight",
        )
        event = build_add_brew_event(shot, dose_index=2, machine_name="test")
        self.assertEqual(
            set(event),
            {
                "grindUuid", "machineName", "groupHead", "doseIndex",
                "validity", "brewTimeMs", "brewWeightMg",
            },
        )
        self.assertEqual(event["brewWeightMg"], 18400)
        self.assertEqual(event["brewTimeMs"], 26900)
        self.assertTrue(event["validity"])
        self.assertEqual(event["doseIndex"], 2)
        self.assertEqual(event["machineName"], "test")

    def test_event_rejects_empty_shot(self) -> None:
        shot = Shot(0, 0, 0, 0, 0, 0, 0, False, "none")
        with self.assertRaises(ValueError):
            build_add_brew_event(shot)


if __name__ == "__main__":
    unittest.main()
