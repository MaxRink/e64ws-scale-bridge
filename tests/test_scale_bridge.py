import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.scale_bridge import (
    Shot,
    ShotDetector,
    ScaleSample,
    build_add_brew_event,
    build_parser,
    main,
    parse_ble_weight,
    parse_decent_weight,
    parse_gatt_weight,
    _print_record,
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

    def test_auto_parser_keeps_a_decent_zero_frame(self) -> None:
        # Regression: 0.0 is falsy, so ``decent or gatt`` used to parse the same
        # bytes as an imperial GATT frame and reported 934.4 g instead of 0 g.
        # 03 ce 0000 0000 b6 is a legitimate 0.0 g Decent notification.
        self.assertEqual(parse_ble_weight(bytes.fromhex("03ce00000000b6"), "auto"), 0.0)
        # The generic fallback must still work when the Decent parse declines.
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

    def test_never_flat_series_times_out_instead_of_hanging(self) -> None:
        # A 5 minute stable 18 g plateau with a 0.2 g wobble never satisfies the
        # <=0.15 g finish window.  The max-shot bound must close it, otherwise
        # the detector stays active forever and wedges the on-device status.
        detector = ShotDetector(min_duration_ms=1000.0)
        self._feed(detector, [(0.0, ms) for ms in (0.0, 200.0, 400.0, 600.0, 800.0)])
        shot = None
        grams = 18.0
        for ms in range(1000, 301_000, 500):
            shot = detector.add(ScaleSample(grams, float(ms)))
            grams = 18.2 if grams == 18.0 else 18.0
            if shot is not None:
                break
        self.assertIsNotNone(shot, "an active shot must not stay active forever")
        assert shot is not None
        self.assertEqual(shot.reason, "timeout")
        self.assertFalse(shot.valid)
        self.assertFalse(detector.active)
        self.assertLessEqual(shot.duration_ms, 300_000.0)

    def test_max_shot_ms_is_configurable_and_invalid(self) -> None:
        self.assertEqual(ShotDetector().max_shot_ms, 120_000.0)
        detector = ShotDetector(min_duration_ms=1000.0, max_shot_ms=30_000.0)
        self._feed(detector, [(0.0, ms) for ms in (0.0, 200.0, 400.0, 600.0, 800.0)])
        shot = None
        grams = 18.0
        for ms in range(1000, 60_000, 500):
            shot = detector.add(ScaleSample(grams, float(ms)))
            grams = 18.2 if grams == 18.0 else 18.0
            if shot is not None:
                break
        self.assertIsNotNone(shot)
        assert shot is not None
        self.assertEqual(shot.reason, "timeout")
        self.assertAlmostEqual(shot.duration_ms, 30_000.0, places=1)
        self.assertAlmostEqual(shot.mass_g, 18.0, places=1)
        with self.assertRaises(ValueError):
            ShotDetector(max_shot_ms=0.0)

    def test_stable_shot_still_finishes_before_the_bound(self) -> None:
        detector = ShotDetector(
            baseline_samples=3,
            min_duration_ms=1000.0,
            valid_min_ms=3000.0,
            stable_ms=1000.0,
            max_shot_ms=30_000.0,
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
        self.assertEqual(shot.reason, "stable weight")
        self.assertTrue(shot.valid)

    def test_idle_reseeds_after_the_cup_is_removed(self) -> None:
        # Regression: after a shot the idle window seeded on the post-shot
        # level (the cup still on the scale) and then froze when the cup was
        # removed, so the next shot never started again.
        detector = ShotDetector(
            baseline_samples=3,
            min_duration_ms=1000.0,
            valid_min_ms=1000.0,
            stable_ms=500.0,
        )
        first = self._feed(detector, [
            (0.0, 0.0), (0.0, 200.0), (0.0, 400.0),
            (2.0, 600.0), (10.0, 800.0),
            (18.0, 1000.0), (18.0, 1200.0), (18.0, 1400.0), (18.0, 1600.0),
        ])
        self.assertIsNotNone(first)
        assert first is not None
        self.assertAlmostEqual(first.mass_g, 18.0, places=1)
        # The cup is still on the scale, so the idle baseline seeds at 18 g.
        self._feed(detector, [(18.0, ms) for ms in (1800.0, 2000.0, 2200.0, 2400.0)])
        # ... and is then removed: the idle level is 0 g, far outside the window.
        self._feed(detector, [(0.0, ms) for ms in (2600.0, 2800.0, 3000.0, 3200.0)])
        second = self._feed(detector, [
            (2.0, 3400.0), (10.0, 3600.0),
            (18.0, 3800.0), (18.0, 4000.0), (18.0, 4200.0), (18.0, 4400.0),
        ])
        self.assertIsNotNone(second, "the detector must re-arm after the level moved")
        assert second is not None
        self.assertAlmostEqual(second.baseline_g, 0.0, places=3)
        self.assertAlmostEqual(second.mass_g, 18.0, places=1)

    def test_idle_drift_reseeds_instead_of_keeping_a_stale_baseline(self) -> None:
        detector = ShotDetector(baseline_samples=3, start_threshold_g=0.5)
        self._feed(detector, [(0.0, 0.0), (0.0, 200.0), (0.0, 400.0)])
        # A single 0.3 g step is above the 0.15 g stability window but below the
        # 0.5 g start threshold: the resting level is now 0.3 g.
        self._feed(detector, [(0.3, ms) for ms in (600.0, 800.0, 1000.0, 1200.0)])
        # A 0.4 g rise from that level is not a shot.  With a frozen 0 g
        # baseline the same rise looks like a 0.7 g rise and starts a phantom
        # shot.
        self._feed(detector, [(0.7, 1400.0)])
        self.assertFalse(detector.active)
        self.assertAlmostEqual(detector._baseline, 0.3, places=3)

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


class ApplyAuditTests(unittest.TestCase):
    """The event is printed before the grinder write.

    These tests use an in-memory stdout, so they pin the *order* only; the
    flush that the guarantee also needs is pinned in ``AuditFlushTests``,
    which writes to a real file.
    """

    POINTS = [
        (0.0, 0.0), (0.0, 200.0),
        (2.0, 400.0), (10.0, 600.0),
        (18.0, 800.0), (18.0, 1000.0), (18.0, 1200.0),
    ]

    def _replay(self, directory: str) -> str:
        path = os.path.join(directory, "shot.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for grams, ms in self.POINTS:
                handle.write(json.dumps({"grams": grams, "ms": float(ms)}) + "\n")
        return path

    def _args(self, replay: str, *extra: str) -> list[str]:
        return [
            "--replay", replay, "--grinder", "192.0.2.1", "--token", "test-token",
            "--apply", "--yes", "--once",
            "--baseline-samples", "2", "--min-duration-ms", "400",
            "--valid-min-ms", "400", "--stable-ms", "200",
            *extra,
        ]

    def test_event_line_is_flushed_before_a_failing_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            replay = self._replay(tmp)
            output = io.StringIO()
            seen: dict[str, str] = {}

            def failing_send(*_args: object, **_kwargs: object) -> dict:
                seen["at_send"] = output.getvalue()
                raise RuntimeError("transport refused the connection")

            with mock.patch("tools.scale_bridge.send_add_brew_event", failing_send):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    code = main(self._args(replay))

            text = output.getvalue()
            self.assertEqual(code, 2)
            # The audit record is on stdout before the transport is used.
            self.assertIn("brewWeightMg", seen["at_send"])
            self.assertNotIn("transport refused", seen["at_send"])
            # ... and it still precedes the error the CLI reports.
            self.assertIn("transport refused the connection", text)
            self.assertLess(
                text.index("brewWeightMg"), text.index("transport refused the connection")
            )

    def test_response_is_printed_after_the_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            replay = self._replay(tmp)
            output = io.StringIO()
            seen: dict[str, str] = {}

            def ok_send(*_args: object, **_kwargs: object) -> dict:
                seen["at_send"] = output.getvalue()
                return {"success": True}

            with mock.patch("tools.scale_bridge.send_add_brew_event", ok_send):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    code = main(self._args(replay))

            self.assertEqual(code, 0)
            self.assertIn("brewWeightMg", seen["at_send"])
            self.assertNotIn("success", seen["at_send"])
            text = output.getvalue()
            self.assertLess(text.index("brewWeightMg"), text.index('"success"'))

    def test_json_mode_keeps_the_event_and_the_response_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            replay = self._replay(tmp)
            output = io.StringIO()

            def ok_send(*_args: object, **_kwargs: object) -> dict:
                return {"success": True}

            with mock.patch("tools.scale_bridge.send_add_brew_event", ok_send):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    code = main(self._args(replay, "--json"))

            self.assertEqual(code, 0)
            decoder = json.JSONDecoder()
            text = output.getvalue()
            documents = []
            index = 0
            while True:
                while index < len(text) and text[index].isspace():
                    index += 1
                if index >= len(text):
                    break
                document, index = decoder.raw_decode(text, index)
                documents.append(document)
            self.assertEqual(len(documents), 2)
            self.assertNotIn("response", documents[0])
            self.assertEqual(documents[0]["event"]["brewWeightMg"], 18000)
            self.assertEqual(documents[1], {"response": {"success": True}})


class RejectedActionTests(unittest.TestCase):
    """A refusal by the grinder must not look like a delivered event."""

    TOKEN = "s3cr3t-token-value"
    # The documented exit status for "sent and refused", pinned here on
    # purpose: it is a contract with the operator's scripts, not an internal
    # constant of this file.
    REFUSED_EXIT = 3

    def _args(self, replay: str) -> list[str]:
        return [
            "--replay", replay, "--grinder", "192.0.2.1", "--token", self.TOKEN,
            "--apply", "--yes", "--once",
            "--baseline-samples", "2", "--min-duration-ms", "400",
            "--valid-min-ms", "400", "--stable-ms", "200",
        ]

    def _run(self, response: dict) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shot.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for grams, ms in ApplyAuditTests.POINTS:
                    handle.write(json.dumps({"grams": grams, "ms": float(ms)}) + "\n")
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch("tools.scale_bridge.send_add_brew_event",
                            return_value=response), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                code = main(self._args(path))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_refused_add_brew_event_exits_non_zero_and_says_why(self) -> None:
        code, stdout, stderr = self._run(
            {"success": False, "error": "AddBrewEvent not permitted (dev gated)"}
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(code, self.REFUSED_EXIT)
        self.assertIn("AddBrewEvent not permitted (dev gated)", stderr)
        self.assertIn("refused AddBrewEvent", stderr)
        # The audit record is still printed before the refusal is reported.
        self.assertIn("brewWeightMg", stdout)

    def test_accepted_add_brew_event_still_exits_zero(self) -> None:
        code, _, stderr = self._run({"success": True})
        self.assertEqual(code, 0)
        self.assertNotIn("refused", stderr)

    def test_a_refusal_without_a_reason_still_fails_loudly(self) -> None:
        code, _, stderr = self._run({"success": False})
        self.assertEqual(code, self.REFUSED_EXIT)
        self.assertIn("no reason", stderr)

    def test_a_response_without_success_keeps_the_old_behaviour(self) -> None:
        # Only an explicit success:false is a refusal; an older payload that
        # does not carry the field must not start failing.
        code, _, stderr = self._run({"type": "GrinderActionResult", "refId": 1})
        self.assertEqual(code, 0)
        self.assertNotIn("refused", stderr)

    def test_the_refusal_reason_is_redacted(self) -> None:
        code, stdout, stderr = self._run({
            "success": False,
            "error": f"denied while talking to wss://192.0.2.1:9997/?token={self.TOKEN}",
        })
        self.assertEqual(code, self.REFUSED_EXIT)
        self.assertNotIn(self.TOKEN, stderr)
        self.assertIn("<redacted>", stderr)


class AuditFlushTests(unittest.TestCase):
    """Pin the flush that the audit guarantee depends on.

    ``redirect_stdout(io.StringIO())`` cannot tell a flushed print from a
    buffered one, so deleting ``sys.stdout.flush()`` from ``_print_record``
    used to leave the suite green.  These two tests write to a real file
    instead: bytes are on disk only after a flush, and a hard ``os._exit``
    skips every remaining buffer.
    """

    POINTS = ApplyAuditTests.POINTS

    CRASH_SCRIPT = """
import os
import sys

sys.path.insert(0, sys.argv[1])
from tools import scale_bridge


def crash(*_args, **_kwargs):
    # The transport takes the process down before the interpreter can flush
    # anything at exit: only an explicit flush survives.
    os._exit(9)


scale_bridge.send_add_brew_event = crash
raise SystemExit(scale_bridge.main(sys.argv[2:]))
"""

    def _args(self, replay: str) -> list[str]:
        return [
            "--replay", replay, "--grinder", "192.0.2.1", "--token", "test-token",
            "--apply", "--yes", "--once",
            "--baseline-samples", "2", "--min-duration-ms", "400",
            "--valid-min-ms", "400", "--stable-ms", "200",
        ]

    def _replay(self, tmp: str) -> str:
        path = os.path.join(tmp, "shot.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for grams, ms in self.POINTS:
                handle.write(json.dumps({"grams": grams, "ms": float(ms)}) + "\n")
        return path

    def test_the_record_reaches_a_real_file_before_the_handle_is_closed(self) -> None:
        record = {
            "shot": {"mass_g": 18.0, "duration_ms": 600.0, "valid": True,
                     "reason": "stable weight"},
            "event": {"brewWeightMg": 18000, "brewTimeMs": 600, "validity": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.txt"
            handle = open(path, "w", encoding="utf-8")
            try:
                with contextlib.redirect_stdout(handle):
                    _print_record(record, as_json=False)
                # Nothing has closed or flushed *handle* here: the only way
                # these bytes can be on disk is _print_record's own flush.
                self.assertIn("brewWeightMg=18000", path.read_text(encoding="utf-8"))
            finally:
                handle.close()

    def test_the_record_survives_a_transport_that_kills_the_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            replay = self._replay(tmp)
            audit = Path(tmp) / "audit.txt"
            with open(audit, "w", encoding="utf-8") as handle:
                proc = subprocess.run(
                    [sys.executable, "-c", self.CRASH_SCRIPT,
                     str(Path(__file__).resolve().parent.parent), *self._args(replay)],
                    stdout=handle,
                    stderr=subprocess.DEVNULL,
                    timeout=120,
                    check=False,
                )
            self.assertEqual(proc.returncode, 9, "the crash path must have run")
            text = audit.read_text(encoding="utf-8")
            self.assertIn("brewWeightMg", text)
            self.assertNotIn("response", text)


class CliPreflightTests(unittest.TestCase):
    def test_preflight_accepts_password_file(self) -> None:
        args = build_parser().parse_args([
            "--replay", "shot.jsonl", "--grinder", "192.0.2.1",
            "--preflight", "--allow-no-password", "--password-file", "/tmp/pw",
        ])
        self.assertTrue(args.preflight)
        self.assertTrue(args.allow_no_password)
        self.assertEqual(args.password_file, "/tmp/pw")

    def test_preflight_requires_grinder(self) -> None:
        with self.assertRaises(SystemExit):
            main(["--replay", "shot.jsonl", "--preflight"])

    def test_preflight_rejects_apply(self) -> None:
        with self.assertRaises(SystemExit):
            main(["--replay", "shot.jsonl", "--grinder", "192.0.2.1",
                  "--preflight", "--apply", "--yes"])


if __name__ == "__main__":
    unittest.main()
