"""Tests for the on-device OpenScale -> Xenia state daemon.

The fake scale speaks just enough RFC 6455 for the daemon's trusted ws://
client.  No hardware, no grinder and no PHP server are needed.
"""

from __future__ import annotations

import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ondevice.scalebridged import ScaleDaemon
from tools.scale_bridge import ShotDetector


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
            persisted = json.loads(state_path.read_text())
            self.assertEqual(persisted["schema"], 1)
            self.assertEqual(persisted["shot_count"], state["shot_count"])
            lines = shots_path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["mass_g"], last["mass_g"])
            daemon.stop()
            thread.join(timeout=3)

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
