#!/usr/bin/env python3
"""On-device OpenScale/HDS -> Xenia state daemon for the E64 WS.

This is the dependency-free part of the bridge.  It runs on the grinder's
Python 3.10 (which has no ``ssl``, ``hashlib`` or ``pip``), reads the scale's
plain ``ws://<host>/snapshot`` stream, detects a shot with the same
``ShotDetector`` as the host bridge, and writes an atomic state file for the
PHP Xenia endpoints and the WebUI page.

It does not talk to the E64 WSS, does not use the cloud and does not need
root itself.  Install it with ``ondevice/install.sh``; the init script runs it
as ``www-data``.  See ``docs/ON_DEVICE_INSTALL.md``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.openscale_client import OpenScaleProtocolError, OpenScaleSnapshot, parse_frame  # noqa: E402
from tools.scale_bridge import ScaleSample, ShotDetector  # noqa: E402

DEFAULT_STATE = "/run/scale-bridge/state.json"
DEFAULT_SHOTS = "/db/scale-bridge/shots.jsonl"
DEFAULT_CONFIG = "/db/scale-bridge/config.json"
SCHEMA = 1


def now_ms() -> int:
    return int(time.time() * 1000)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(value, separators=(",", ":"), sort_keys=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class WebSocketError(RuntimeError):
    pass


class RawWebSocket:
    """Minimal RFC 6455 text client for a trusted, plain ``ws://`` LAN peer.

    The E64 Python lacks ``hashlib``, so the server's ``Sec-WebSocket-Accept``
    is not recomputed.  The OpenScale snapshot endpoint is unauthenticated and
    local; do not use this class for a TLS or authenticated peer.
    """

    def __init__(self, url: str, *, timeout: float = 10.0, max_frame: int = 1 << 20) -> None:
        parts = urlsplit(url)
        if parts.scheme != "ws":
            raise ValueError("only ws:// is supported")
        if not parts.hostname:
            raise ValueError("URL has no host")
        self.host = parts.hostname
        self.port = parts.port or 80
        self.path = parts.path or "/"
        if parts.query:
            self.path += "?" + parts.query
        self.timeout = timeout
        self.max_frame = max_frame
        self.sock: socket.socket | None = None
        self._buffer = b""

    def connect(self) -> None:
        if self.sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self.sock = sock
        try:
            sock.sendall(request)
            header = self._read_until(b"\r\n\r\n", limit=32768)
            first = header.split(b"\r\n", 1)[0]
            if b" 101 " not in first:
                raise WebSocketError(f"upgrade rejected: {first.decode('latin-1', 'replace')}")
        except Exception:
            sock.close()
            self.sock = None
            raise

    def _read_until(self, marker: bytes, *, limit: int) -> bytes:
        assert self.sock is not None
        data = self._buffer
        while marker not in data:
            if len(data) > limit:
                raise WebSocketError("handshake header too large")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            data += chunk
        head, _, rest = data.partition(marker)
        self._buffer = rest
        return head

    def _recv_exact(self, size: int) -> bytes:
        assert self.sock is not None
        while len(self._buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self._buffer)))
            if not chunk:
                raise WebSocketError("connection closed")
            self._buffer += chunk
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self.sock is None:
            raise WebSocketError("not connected")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        fragments = bytearray()
        while True:
            first, second = self._recv_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            if length > self.max_frame:
                raise WebSocketError("frame too large")
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise WebSocketError("peer closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2, 0x0):
                fragments.extend(payload)
                if fin:
                    return fragments.decode("utf-8", "replace")
                continue
            raise WebSocketError(f"unsupported opcode {opcode}")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
            try:
                self.sock.close()
            finally:
                self.sock = None
        self._buffer = b""


class ScaleDaemon:
    def __init__(
        self,
        url: str,
        *,
        state_path: str = DEFAULT_STATE,
        shots_path: str | None = DEFAULT_SHOTS,
        detector: ShotDetector | None = None,
        finishing_ms: int = 3000,
        timeout: float = 10.0,
        verbose: bool = False,
    ) -> None:
        self.url = url
        self.state_path = Path(state_path)
        self.shots_path = Path(shots_path) if shots_path else None
        self.detector = detector or ShotDetector()
        self.finishing_ms = max(0, int(finishing_ms))
        self.timeout = timeout
        self.verbose = verbose
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "schema": SCHEMA,
            "source": url,
            "connected": False,
            "phase": "idle",
            "ma_status": 1,
            "extraction_status": 0,
            "live_grams": 0.0,
            "live_ms": 0,
            "last_shot": None,
            "finishing_until_ms": 0,
            "shot_count": 0,
            "reconnects": 0,
            "error": "",
            "updated_ms": now_ms(),
        }
        self._last_write = 0.0

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"scalebridged: {message}", file=sys.stderr, flush=True)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _write_state(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < 0.1:
            return
        with self._lock:
            value = dict(self._state)
        value["updated_ms"] = now_ms()
        try:
            atomic_json(self.state_path, value)
            self._last_write = now
        except OSError as exc:
            self.log(f"state write failed: {exc}")

    def _set(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)
        self._write_state()

    def _append_shot(self, shot: dict[str, Any]) -> None:
        if self.shots_path is None:
            return
        try:
            self.shots_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.shots_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(shot, separators=(",", ":"), sort_keys=True) + "\n")
        except OSError as exc:
            self.log(f"shot log failed: {exc}")

    def _reset_detector(self) -> None:
        self.detector = ShotDetector(
            baseline_samples=self.detector.baseline_samples,
            start_threshold_g=self.detector.start_threshold_g,
            stability_g=self.detector.stability_g,
            stable_ms=self.detector.stable_ms,
            min_duration_ms=self.detector.min_duration_ms,
            valid_min_ms=self.detector.valid_min_ms,
            drop_threshold_g=self.detector.drop_threshold_g,
        )

    def feed(self, grams: float, milliseconds: float) -> dict[str, Any] | None:
        with self._lock:
            if (
                self._state.get("phase") == "finishing"
                and int(self._state.get("finishing_until_ms", 0)) <= now_ms()
            ):
                self._state["phase"] = "idle"
                self._state["ma_status"] = 1
                self._state["extraction_status"] = 0
        sample = ScaleSample(grams, milliseconds, source="openscale")
        was_active = self.detector.active
        try:
            shot = self.detector.add(sample)
        except ValueError:
            # OpenScale may restart its millisecond counter after a reconnect.
            self._reset_detector()
            shot = self.detector.add(sample)
            was_active = False
        if self.detector.active and not was_active:
            self._set(phase="serving", ma_status=3, extraction_status=1,
                      live_grams=grams, live_ms=int(milliseconds))
        if shot is not None:
            payload = shot.as_dict()
            payload["at_ms"] = now_ms()
            with self._lock:
                self._state["shot_count"] = int(self._state.get("shot_count", 0)) + 1
                self._state["last_shot"] = payload
                self._state["phase"] = "finishing"
                self._state["ma_status"] = 4
                self._state["extraction_status"] = 2
                self._state["finishing_until_ms"] = now_ms() + self.finishing_ms
                self._state["live_grams"] = grams
                self._state["live_ms"] = int(milliseconds)
            self._append_shot(payload)
            self._write_state(force=True)
            self.log(
                f"shot {shot.duration_ms:.0f} ms, {shot.mass_g:.1f} g, "
                f"valid={shot.valid}, reason={shot.reason}"
            )
            return payload
        self._set(live_grams=grams, live_ms=int(milliseconds))
        return None

    def _read_once(self) -> None:
        ws = RawWebSocket(self.url, timeout=self.timeout)
        ws.connect()
        with self._lock:
            self._state["connected"] = True
            self._state["error"] = ""
            self._state["reconnects"] = int(self._state.get("reconnects", 0)) + 1
        self._write_state(force=True)
        self.log(f"connected to {self.url}")
        try:
            while not self.stop_event.is_set():
                text = ws.recv_text()
                try:
                    frame = parse_frame(text)
                except OpenScaleProtocolError as exc:
                    self.log(f"ignoring malformed frame: {exc}")
                    continue
                if isinstance(frame, OpenScaleSnapshot):
                    self.feed(frame.grams, frame.milliseconds)
        finally:
            ws.close()
            with self._lock:
                self._state["connected"] = False
            self._write_state(force=True)

    def run_forever(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self._read_once()
                backoff = 1.0
            except (OSError, WebSocketError, OpenScaleProtocolError, ValueError) as exc:
                if self.stop_event.is_set():
                    break
                with self._lock:
                    self._state["connected"] = False
                    self._state["error"] = str(exc)
                    self._state["phase"] = "idle"
                    self._state["ma_status"] = 1
                    self._state["extraction_status"] = 0
                self._write_state(force=True)
                self.log(f"connection error: {exc}")
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def stop(self) -> None:
        self.stop_event.set()


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read config {path}: {exc}")
    if not isinstance(value, dict):
        raise SystemExit(f"config {path} must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="On-device OpenScale/HDS daemon for E64 Xenia")
    parser.add_argument("--config", default=os.environ.get("SCALE_BRIDGE_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--url", help="ws://host[:port]/snapshot (overrides config)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--path", default="/snapshot")
    parser.add_argument("--state", default=os.environ.get("SCALE_BRIDGE_STATE", DEFAULT_STATE))
    parser.add_argument("--shots", default=os.environ.get("SCALE_BRIDGE_SHOTS", DEFAULT_SHOTS))
    parser.add_argument("--no-shots", action="store_true", help="do not persist shot history")
    parser.add_argument("--finishing-ms", type=int, default=3000)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--stable-ms", type=float, default=1000.0)
    parser.add_argument("--min-duration-ms", type=float, default=3000.0)
    parser.add_argument("--valid-min-ms", type=float, default=15000.0)
    parser.add_argument("--once", action="store_true", help="exit after the first shot")
    parser.add_argument("--duration", type=float, default=0.0, help="exit after N seconds")
    parser.add_argument("--user", default=os.environ.get("SCALE_BRIDGE_USER", ""),
                        help="drop to this user when started as root")
    parser.add_argument("--verbose", action="store_true")
    return parser


def drop_privileges(user: str) -> None:
    """Drop root before opening the network or writing state."""

    if not user or os.geteuid() != 0:
        return
    import pwd

    entry = pwd.getpwnam(user)
    os.setgroups([])
    os.setgid(entry.pw_gid)
    os.setuid(entry.pw_uid)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    url = args.url or config.get("url")
    if not url:
        host = args.host or config.get("host") or "hds.local"
        port = args.port or int(config.get("port", 80))
        path = args.path or config.get("path", "/snapshot")
        url = f"ws://{host}:{port}{path if str(path).startswith('/') else '/' + str(path)}"
    drop_privileges(args.user)
    detector = ShotDetector(
        stable_ms=args.stable_ms,
        min_duration_ms=args.min_duration_ms,
        valid_min_ms=args.valid_min_ms,
    )
    daemon = ScaleDaemon(
        url,
        state_path=args.state,
        shots_path=None if args.no_shots else args.shots,
        detector=detector,
        finishing_ms=args.finishing_ms or int(config.get("finishing_ms", 3000)),
        timeout=args.timeout or float(config.get("timeout", 10.0)),
        verbose=args.verbose,
    )

    def _stop(_signum: int, _frame: Any) -> None:
        daemon.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    started = time.monotonic()
    thread = threading.Thread(target=daemon.run_forever, name="scale-ws", daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            if args.once and daemon.state().get("shot_count", 0) > 0:
                break
            if args.duration and time.monotonic() - started >= args.duration:
                break
            time.sleep(0.1)
    finally:
        daemon.stop()
        thread.join(timeout=5.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
