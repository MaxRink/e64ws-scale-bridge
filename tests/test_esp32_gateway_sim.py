"""Tests for the ESP32 gateway HTTP simulator (no radio, no network)."""

from __future__ import annotations

import unittest

from tools.esp32_gateway_sim import state_payload, synthetic_samples
from tools.scale_bridge import ScaleSample, ShotDetector


class Esp32GatewaySimTests(unittest.TestCase):
    def test_synthetic_sequence_produces_a_valid_shot(self) -> None:
        detector = ShotDetector()
        shot = None
        samples = list(synthetic_samples())
        for sample in samples:
            shot = detector.add(sample) or shot
        self.assertIsNotNone(shot)
        self.assertGreaterEqual(shot.duration_ms, 15000)
        self.assertGreaterEqual(shot.mass_g, 17.5)

    def test_payload_phase_and_metrics(self) -> None:
        detector = ShotDetector()
        for sample in synthetic_samples():
            shot = detector.add(sample)
            if shot is not None:
                break
        payload = state_payload(
            detector, 18.0, 18400, flow=1.2, last_shot={"mass_g": 18.0},
            shot_count=1, finishing_until=21400,
        )
        self.assertEqual(payload["phase"], "finishing")
        self.assertEqual(payload["shot_count"], 1)
        self.assertEqual(payload["last_shot"]["mass_g"], 18.0)
        self.assertEqual(payload["source"], "esp32-sim")

    def test_idle_payload(self) -> None:
        payload = state_payload(
            ShotDetector(), 0.0, 100, flow=0.0, last_shot=None,
            shot_count=0, finishing_until=0,
        )
        self.assertEqual(payload["phase"], "idle")
        self.assertIsNone(payload["last_shot"])


if __name__ == "__main__":
    unittest.main()
