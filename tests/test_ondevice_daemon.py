"""Tests for the on-device OpenScale -> Xenia state daemon.

The fake scale speaks just enough RFC 6455 for the daemon's trusted ws://
client.  No hardware, no grinder and no PHP server are needed.
"""

from __future__ import annotations

import json
import re
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ondevice.scalebridged import ScaleDaemon, main
from tools.scale_bridge import ShotDetector

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "ondevice" / "install.sh"


def installer_config() -> dict:
    """Return the config.json body that install.sh writes on a fresh install.

    Read out of the installer itself so this test follows the real default
    config instead of a copy of it that can drift.
    """

    text = INSTALLER.read_text(encoding="utf-8")
    for block in re.findall(r"<<EOF\n(.*?)\nEOF\n", text, re.DOTALL):
        try:
            value = json.loads(block.replace("$TOKEN", "0" * 32))
        except ValueError:
            continue
        if isinstance(value, dict) and "ingest_token" in value:
            return value
    raise AssertionError("install.sh no longer writes a JSON config heredoc")


def _frame(text: str) -> bytes:
    payload = text.encode()
    length = len(payload)
    if length < 126:
        header = bytes([0x81, length])
    elif length < 65536:
        header = bytes([0x81, 126]) + struct.pack("!H", length)
    else:
        header = bytes([0x81, 127]) + struct.pack("!Q", length)
    return header + payload


class FakeScale:
    def __init__(self, samples: list[dict]) -> None:
        self.samples = samples
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "FakeScale":
        self.thread.start()
        return self

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"\r\n"
            )
            for sample in self.samples:
                if self.stop.is_set():
                    return
                conn.sendall(_frame(json.dumps(sample)))
                time.sleep(0.02)
            # Hold the connection open while the test inspects the state.
            while not self.stop.wait(0.05):
                pass

    def close(self) -> None:
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(timeout=2)


def shot_samples() -> list[dict]:
    samples = [{"grams": 0.0, "ms": i * 100} for i in range(5)]
    samples += [
        {"grams": 2.0, "ms": 500},
        {"grams": 10.0, "ms": 1000},
        {"grams": 18.0, "ms": 1500},
        {"grams": 18.0, "ms": 2000},
        {"grams": 18.0, "ms": 2500},
        {"grams": 18.0, "ms": 3000},
        {"grams": 18.0, "ms": 3500},
    ]
    return samples


class CliConfigPrecedenceTests(unittest.TestCase):
    """The command line wins over config.json; the config is the fallback.

    The fresh-install case is the one that broke: install.sh always writes
    ``max_shot_ms``, so a rule that lets the config win makes ``--max-shot-ms``
    dead on every installed unit.  The fixture is therefore the installer's own
    config body.
    """

    def _main(self, config: dict | None, argv: list[str]) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            if config is not None:
                path.write_text(json.dumps(config))
            with mock.patch("ondevice.scalebridged.ScaleDaemon") as daemon_cls, \
                    mock.patch("ondevice.scalebridged.drop_privileges"), \
                    mock.patch("ondevice.scalebridged.signal.signal"):
                code = main([
                    "--config", str(path),
                    "--state", str(Path(tmp) / "state.json"),
                    "--no-shots",
                    *argv,
                ])
        self.assertEqual(daemon_cls.call_count, 1)
        kwargs = dict(daemon_cls.call_args.kwargs)
        kwargs["url"] = daemon_cls.call_args.args[0]
        return code, kwargs

    def test_max_shot_ms_flag_overrides_a_fresh_install_config(self) -> None:
        code, kwargs = self._main(installer_config(), ["--max-shot-ms", "30000"])
        self.assertEqual(code, 0)
        self.assertEqual(kwargs["detector"].max_shot_ms, 30_000.0)

    def test_max_shot_ms_config_is_used_when_the_flag_is_absent(self) -> None:
        config = installer_config()
        config["max_shot_ms"] = 45_000
        _, kwargs = self._main(config, [])
        self.assertEqual(kwargs["detector"].max_shot_ms, 45_000.0)

    def test_max_shot_ms_falls_back_to_the_documented_default(self) -> None:
        _, kwargs = self._main(None, [])
        self.assertEqual(kwargs["detector"].max_shot_ms, 120_000.0)

    def test_finishing_ms_config_is_honoured(self) -> None:
        # The daemon used to ignore config.json's finishing_ms because the
        # flag's truthy default always won.
        config = installer_config()
        config["finishing_ms"] = 7500
        _, kwargs = self._main(config, [])
        self.assertEqual(kwargs["finishing_ms"], 7500)

    def test_finishing_ms_flag_overrides_the_config(self) -> None:
        config = installer_config()
        config["finishing_ms"] = 7500
        _, kwargs = self._main(config, ["--finishing-ms", "1200"])
        self.assertEqual(kwargs["finishing_ms"], 1200)

    def test_timeout_config_is_honoured(self) -> None:
        config = installer_config()
        config["timeout"] = 25
        _, kwargs = self._main(config, [])
        self.assertEqual(kwargs["timeout"], 25.0)

    def test_path_config_is_honoured(self) -> None:
        config = installer_config()
        config["path"] = "/weigh"
        _, kwargs = self._main(config, [])
        self.assertEqual(kwargs["url"], "ws://hds.local:80/weigh")

    def test_path_flag_overrides_the_config(self) -> None:
        config = installer_config()
        config["path"] = "/weigh"
        _, kwargs = self._main(config, ["--path", "/snapshot"])
        self.assertEqual(kwargs["url"], "ws://hds.local:80/snapshot")

    def test_a_bad_config_number_is_a_clean_refusal(self) -> None:
        config = installer_config()
        config["max_shot_ms"] = "soon"
        with self.assertRaises(SystemExit) as caught:
            self._main(config, [])
        self.assertIn("max_shot_ms", str(caught.exception))


class OnDeviceDaemonTests(unittest.TestCase):
    def test_shot_updates_state_and_history(self) -> None:
        scale = FakeScale(shot_samples()).start()
        self.addCleanup(scale.close)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "run" / "state.json"
            shots_path = Path(tmp) / "db" / "shots.jsonl"
            detector = ShotDetector(
                stable_ms=500,
                min_duration_ms=1000,
                valid_min_ms=1000,
            )
            daemon = ScaleDaemon(
                f"ws://127.0.0.1:{scale.port}/snapshot",
                state_path=str(state_path),
                shots_path=str(shots_path),
                detector=detector,
                finishing_ms=5000,
                timeout=2,
            )
            thread = threading.Thread(target=daemon.run_forever, daemon=True)
            thread.start()
            self.addCleanup(daemon.stop)
            deadline = time.time() + 8
            while time.time() < deadline:
                state = daemon.state()
                if state.get("shot_count", 0) >= 1:
                    break
                time.sleep(0.05)
            state = daemon.state()
            self.assertGreaterEqual(state["shot_count"], 1)
            self.assertEqual(state["phase"], "finishing")
            self.assertEqual(state["ma_status"], 4)
            last = state["last_shot"]
            self.assertIsNotNone(last)
            self.assertAlmostEqual(last["mass_g"], 18.0, places=1)
            self.assertGreaterEqual(last["duration_ms"], 1000)
            self.assertTrue(state_path.is_file())
            # The writer is throttled; wait for the forced shot write.
            persisted = {}
            deadline = time.time() + 2
            while time.time() < deadline:
                persisted = json.loads(state_path.read_text())
                if persisted.get("shot_count") == state["shot_count"]:
                    break
                time.sleep(0.02)
            self.assertEqual(persisted["schema"], 1)
            self.assertEqual(persisted["shot_count"], state["shot_count"])
            lines = shots_path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["mass_g"], last["mass_g"])
            daemon.stop()
            thread.join(timeout=3)

    def test_unsettled_reading_times_out_and_leaves_serving(self) -> None:
        # The daemon must not stay in serving because a reading never settles:
        # the bounded shot closes as invalid and the status advances.
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScaleDaemon(
                "ws://127.0.0.1:1/snapshot",
                state_path=str(Path(tmp) / "state.json"),
                shots_path=None,
                detector=ShotDetector(min_duration_ms=1000.0, max_shot_ms=5000.0),
            )
            for ms in range(0, 1000, 200):
                daemon.feed(0.0, float(ms))
            grams = 18.0
            for ms in range(1000, 20_000, 500):
                daemon.feed(grams, float(ms))
                grams = 18.2 if grams == 18.0 else 18.0
                if daemon.state()["shot_count"]:
                    break
            state = daemon.state()
            self.assertEqual(state["shot_count"], 1)
            self.assertEqual(state["phase"], "finishing")
            self.assertEqual(state["last_shot"]["reason"], "timeout")
            self.assertFalse(state["last_shot"]["valid"])
            self.assertFalse(daemon.detector.active)

    def test_detector_reset_keeps_the_shot_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScaleDaemon(
                "ws://127.0.0.1:1/snapshot",
                state_path=str(Path(tmp) / "state.json"),
                shots_path=None,
                detector=ShotDetector(max_shot_ms=7000.0, stability_g=0.2),
            )
            daemon._reset_detector()
            self.assertEqual(daemon.detector.max_shot_ms, 7000.0)
            self.assertEqual(daemon.detector.stability_g, 0.2)

    def test_dropout_discards_an_active_shot_and_rearms(self) -> None:
        # A dropped stream must not leave the detector active while the state
        # file says idle: the partial shot is discarded and the detector is
        # re-armed for the next stream.
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScaleDaemon(
                "ws://127.0.0.1:1/snapshot",
                state_path=str(Path(tmp) / "state.json"),
                shots_path=None,
                detector=ShotDetector(baseline_samples=3, min_duration_ms=1000.0),
            )
            for ms in (0.0, 200.0, 400.0):
                daemon.feed(0.0, ms)
            daemon.feed(18.0, 600.0)
            self.assertTrue(daemon.detector.active)
            self.assertEqual(daemon.state()["phase"], "serving")
            daemon._connection_lost(OSError("scale socket closed"))
            state = daemon.state()
            self.assertFalse(daemon.detector.active)
            self.assertEqual(state["phase"], "idle")
            self.assertEqual(state["ma_status"], 1)
            self.assertEqual(state["extraction_status"], 0)
            self.assertFalse(state["connected"])
            self.assertIn("scale socket closed", state["error"])
            self.assertEqual(state["shot_count"], 0)

    def test_non_monotonic_scale_clock_resets_detector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScaleDaemon(
                "ws://127.0.0.1:1/snapshot",
                state_path=str(Path(tmp) / "state.json"),
                shots_path=None,
            )
            daemon.feed(0.0, 1000)
            daemon.feed(0.0, 1100)
            # A reconnect may restart the scale clock; this must not raise.
            daemon.feed(0.0, 0)
            daemon.feed(0.0, 100)
            self.assertEqual(daemon.state()["shot_count"], 0)


if __name__ == "__main__":
    unittest.main()
