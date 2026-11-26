"""Tests for the on-device OpenScale -> Xenia state daemon.

The fake scale speaks just enough RFC 6455 for the daemon's trusted ws://
client.  No hardware, no grinder and no PHP server are needed.
"""

from __future__ import annotations

import json
import re
import socket
import struct
import subprocess
import sys
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
    def __init__(self, samples: list[dict], *, drop_after: int | None = None) -> None:
        self.samples = samples
        self.drop_after = drop_after
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
            for index, sample in enumerate(self.samples, 1):
                if self.stop.is_set():
                    return
                conn.sendall(_frame(json.dumps(sample)))
                time.sleep(0.02)
                if self.drop_after is not None and index >= self.drop_after:
                    # Drop the stream the way a scale does when it reboots.
                    return
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

    def test_a_non_numeric_config_port_is_a_clean_refusal(self) -> None:
        # The measured defect: `int(config.get("port", 80))` sat outside the
        # resolver, so this was a ValueError traceback and exit 1 with the
        # daemon never started.
        config = installer_config()
        config["port"] = "eighty"
        with self.assertRaises(SystemExit) as caught:
            self._main(config, [])
        self.assertIn("port", str(caught.exception))
        self.assertIn("eighty", str(caught.exception))

    def test_a_non_string_config_host_or_url_is_a_clean_refusal(self) -> None:
        for key, value in (("host", ["hds.local"]), ("url", 42), ("path", 7)):
            with self.subTest(key=key):
                config = installer_config()
                config[key] = value
                with self.assertRaises(SystemExit) as caught:
                    self._main(config, [])
                self.assertIn(key, str(caught.exception))

    def test_an_explicit_port_zero_is_refused_not_silently_ignored(self) -> None:
        # `args.port or ...` treated 0 as "not given" and used the config (or
        # 80) without a word.
        config = installer_config()
        with self.assertRaises(SystemExit) as caught:
            self._main(config, ["--port", "0"])
        self.assertIn("port", str(caught.exception))
        self.assertIn("0", str(caught.exception))

    def test_a_numeric_string_config_port_is_still_accepted(self) -> None:
        # JSON authors write numbers as strings; that is not the typo this
        # guards against.
        config = installer_config()
        config["port"] = "8123"
        _, kwargs = self._main(config, [])
        self.assertEqual(kwargs["url"], "ws://hds.local:8123/snapshot")

    def test_a_config_url_wins_over_host_and_port(self) -> None:
        config = installer_config()
        config["url"] = "ws://scale.lan:9000/weigh"
        _, kwargs = self._main(config, [])
        self.assertEqual(kwargs["url"], "ws://scale.lan:9000/weigh")

    def test_the_url_flag_wins_over_a_config_url(self) -> None:
        config = installer_config()
        config["url"] = "ws://scale.lan:9000/weigh"
        _, kwargs = self._main(config, ["--url", "ws://hds.local/snapshot"])
        self.assertEqual(kwargs["url"], "ws://hds.local/snapshot")

    def test_a_url_in_another_scheme_is_a_clean_refusal(self) -> None:
        # `ftp://` reached RawWebSocket, whose ValueError was caught by
        # run_forever and retried with backoff: the daemon printed retry lines
        # and exited 0, so a typo looked exactly like a flaky network.
        with self.assertRaises(SystemExit) as caught:
            self._main(installer_config(), ["--url", "ftp://hds.local/snapshot"])
        self.assertIn("ftp://hds.local/snapshot", str(caught.exception))
        self.assertIn("ws://", str(caught.exception))

    def test_a_wss_url_is_refused_with_the_tls_reason(self) -> None:
        # The scale's endpoint is a plain ws:// LAN peer and this client has no
        # TLS at all, so wss:// is refused instead of half-supported.
        with self.assertRaises(SystemExit) as caught:
            self._main(installer_config(), ["--url", "wss://hds.local/snapshot"])
        self.assertIn("wss://", str(caught.exception))
        self.assertIn("TLS", str(caught.exception))

    def test_a_zero_port_in_the_url_is_refused(self) -> None:
        # `parts.port or 80` swallowed an explicit 0 one layer below main(),
        # exactly like `args.port or ...` did on the line above.
        with self.assertRaises(SystemExit) as caught:
            self._main(installer_config(), ["--url", "ws://hds.local:0/x"])
        self.assertIn("ws://hds.local:0/x", str(caught.exception))
        self.assertIn("1..65535", str(caught.exception))

    def test_a_url_port_above_65535_is_refused(self) -> None:
        # urlsplit raises on the `.port` access, inside RawWebSocket, and the
        # retry loop swallowed it.
        with self.assertRaises(SystemExit) as caught:
            self._main(installer_config(), ["--url", "ws://hds.local:70000/x"])
        self.assertIn("ws://hds.local:70000/x", str(caught.exception))
        self.assertIn("65535", str(caught.exception))

    def test_a_url_without_a_host_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self._main(installer_config(), ["--url", "ws://:80/x"])
        self.assertIn("ws://:80/x", str(caught.exception))
        self.assertIn("host", str(caught.exception))

    def test_a_valid_ws_url_is_used_exactly_as_given(self) -> None:
        # The refusals above must not refuse a working URL: the default port,
        # an explicit port, a path and a query all still pass through.
        for url in ("ws://hds.local/snapshot",
                    "ws://hds.local:8123/weigh",
                    "ws://scale.lan:9000/snapshot?token=abc",
                    "ws://[::1]:8080/snapshot"):
            with self.subTest(url=url):
                _, kwargs = self._main(installer_config(), ["--url", url])
                self.assertEqual(kwargs["url"], url)


class UrlValidationCliTests(unittest.TestCase):
    """A typo'd ``--url`` is one line on stderr and a non-zero exit.

    ``CliConfigPrecedenceTests`` shows the refusal happens before the daemon is
    built; this runs the real CLI, because the defect was the *exit status* and
    the retry loop, which a mocked ``ScaleDaemon`` cannot show.  ``--duration
    1`` is passed so a regression that retries instead of refusing exits 0
    after a second instead of hanging the test.
    """

    BAD_URLS = (
        ("ftp://hds.local/snapshot", "the scheme must be ws://"),
        ("ws://hds.local:0/x", "the port must be 1..65535"),
        ("ws://hds.local:70000/x", "Port out of range"),
        ("ws://:80/x", "it has no host"),
        ("wss://hds.local/x", "wss:// needs TLS"),
    )

    def test_a_typo_in_url_exits_non_zero_with_one_line_of_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for url, reason in self.BAD_URLS:
                with self.subTest(url=url):
                    result = subprocess.run(
                        [sys.executable, str(REPO / "ondevice" / "scalebridged.py"),
                         "--config", str(Path(tmp) / "absent.json"),
                         "--state", str(Path(tmp) / "state.json"), "--no-shots",
                         "--duration", "1", "--url", url],
                        capture_output=True, text=True, timeout=120,
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr.count("\n"), 1, result.stderr)
                    self.assertIn(url, result.stderr)
                    self.assertIn(reason, result.stderr)
                    self.assertNotIn("connection error", result.stderr)


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

    def test_the_state_timestamp_stays_fresh_while_the_scale_is_silent(self) -> None:
        """The state file is a liveness signal, not just an activity log.

        The PHP layer now refuses to report a phase from an old file, so a
        healthy daemon that is merely waiting -- a connected scale that is not
        sending, a reconnect backoff of up to 30 s -- must keep refreshing
        ``updated_ms``.  Without the heartbeat the stamp froze at connect time
        and the HMI would have called a live daemon dead.
        """

        scale = FakeScale([]).start()  # accepts the upgrade, then says nothing
        self.addCleanup(scale.close)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            daemon = ScaleDaemon(
                f"ws://127.0.0.1:{scale.port}/snapshot",
                state_path=str(state_path),
                shots_path=None,
                timeout=5.0,  # long enough that recv is still blocked below
            )
            thread = threading.Thread(target=daemon.run_forever, daemon=True)
            thread.start()
            self.addCleanup(daemon.stop)
            deadline = time.time() + 5
            while time.time() < deadline and not state_path.is_file():
                time.sleep(0.02)
            self.assertTrue(state_path.is_file(), "the daemon never wrote state")
            time.sleep(0.3)
            first = json.loads(state_path.read_text())["updated_ms"]
            time.sleep(1.4)  # more than one heartbeat, far less than the timeout
            second = json.loads(state_path.read_text())["updated_ms"]
            self.assertTrue(daemon.state()["connected"],
                            "the stream must still be open while we sample")
            daemon.stop()
            thread.join(timeout=3)
        self.assertGreater(second, first, "updated_ms did not advance while idle")

    def test_a_stream_that_drops_mid_shot_is_discarded_through_run_forever(self) -> None:
        """The dropout handling must be wired into the read loop, not only callable.

        The older test calls daemon._connection_lost directly, so nothing
        proved that run_forever actually routes a dropped stream there.
        """

        samples = [{"grams": 0.0, "ms": index * 100} for index in range(5)]
        samples += [{"grams": 2.0, "ms": 500}, {"grams": 9.0, "ms": 700},
                    {"grams": 18.0, "ms": 900}]
        scale = FakeScale(samples, drop_after=len(samples)).start()
        self.addCleanup(scale.close)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            daemon = ScaleDaemon(
                f"ws://127.0.0.1:{scale.port}/snapshot",
                state_path=str(state_path),
                shots_path=None,
                detector=ShotDetector(baseline_samples=3, min_duration_ms=5000.0),
                timeout=0.5,
            )
            thread = threading.Thread(target=daemon.run_forever, daemon=True)
            thread.start()
            self.addCleanup(daemon.stop)

            deadline = time.time() + 10
            while time.time() < deadline and daemon.state()["ma_status"] != 3:
                time.sleep(0.02)
            self.assertEqual(daemon.state()["ma_status"], 3,
                             "the shot never started; the fake scale did not feed")

            deadline = time.time() + 10
            while time.time() < deadline:
                state = daemon.state()
                if state["phase"] == "idle" and state["connected"] is False:
                    break
                time.sleep(0.02)
            state = daemon.state()
            daemon.stop()
            thread.join(timeout=5)

        self.assertEqual(state["phase"], "idle")
        self.assertEqual(state["ma_status"], 1)
        self.assertEqual(state["extraction_status"], 0)
        self.assertFalse(state["connected"])
        self.assertEqual(state["shot_count"], 0, "a partial shot must not be reported")
        self.assertTrue(state["error"], "the drop must be recorded as an error")
        self.assertFalse(daemon.detector.active)

    def test_the_shot_log_rotates_instead_of_growing_forever(self) -> None:
        # /db survives signed updates, so shots.jsonl grows for the life of the
        # unit (~12.7 MB/year at four shots a day) with nothing to bound it.
        shot = {"mass_g": 18.0, "duration_ms": 24600.0, "valid": True,
                "reason": "stable", "at_ms": 1}
        with tempfile.TemporaryDirectory() as tmp:
            shots_path = Path(tmp) / "shots.jsonl"
            daemon = ScaleDaemon(
                "ws://127.0.0.1:1/snapshot",
                state_path=str(Path(tmp) / "state.json"),
                shots_path=str(shots_path),
            )
            # The documented cap, as a value: this test fails on the behaviour
            # (a log that keeps growing) rather than on a missing constant.
            # Pretend the log already reached it -- 4 MiB of real writes would
            # only slow the test down.
            cap = 4 << 20
            shots_path.write_text("x" * (cap + 1))
            daemon._append_shot(dict(shot))
            self.assertTrue(Path(f"{shots_path}.1").is_file(),
                            "the full log must move aside, not grow")
            self.assertEqual(Path(f"{shots_path}.1").stat().st_size, cap + 1)
            self.assertEqual(len(shots_path.read_text().splitlines()), 1)
            self.assertAlmostEqual(json.loads(shots_path.read_text())["mass_g"], 18.0)

    def test_a_small_shot_log_is_not_rotated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shots_path = Path(tmp) / "shots.jsonl"
            daemon = ScaleDaemon(
                "ws://127.0.0.1:1/snapshot",
                state_path=str(Path(tmp) / "state.json"),
                shots_path=str(shots_path),
            )
            daemon._append_shot({"mass_g": 18.0})
            daemon._append_shot({"mass_g": 19.0})
            self.assertFalse(Path(f"{shots_path}.1").exists())
            self.assertEqual(len(shots_path.read_text().splitlines()), 2)

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
