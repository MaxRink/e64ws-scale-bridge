#!/usr/bin/env python3
"""Local Xenia coffee-machine fixture for the E64's stock M2M client.

The stock grinder polls a Xenia machine over plain HTTP on port 80:

    GET  /api/v2/mako
    GET  /api/v2/machine
    POST /api/v2/brewratio
    POST /api/v2/scripts/execute

This server implements those four shapes unchanged.  It also exposes a local
control API and a replay input so an owner can complete a synthetic shot:

    GET  /control/state
    POST /control/start              start the serving phase
    POST /control/shot               finish directly with duration/mass
    POST /control/sample             feed one {"grams": ..., "ms": ...} sample
    POST /control/idle               abort to idle and reset the detector
    POST /control/reset              return to the initial state

The scale path uses the same :class:`tools.scale_bridge.ShotDetector` as the
bridge.  A replay file is JSONL, one ``{"grams": 12.3, "ms": 1000}`` object per
line; blank lines and ``#`` comments are ignored.

Safety: both listeners bind to loopback by default.  The fixture makes no
outbound network calls and never contacts a grinder, cloud, or vendor service.

It is a research fixture, not a Xenia replacement.  The grinder must be pointed
at the host manually; on stock firmware the M2M/cloud flag and the Xenia host
are set in the UI.  Whether ``REAL_EXTRACTION_TIME_MS`` reaches GbsService and
moves the dial still needs a physical test.  See docs/XENIA_GUIDE.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # package mode (repo root) and script mode (tools/ on sys.path)
    from tools.scale_bridge import ScaleSample, Shot, ShotDetector, iter_replay
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from scale_bridge import ScaleSample, Shot, ShotDetector, iter_replay


PHASE_IDLE = "idle"
PHASE_SERVING = "serving"
PHASE_FINISHING = "finishing"

_PHASE_BY_STATUS = {1: PHASE_IDLE, 3: PHASE_SERVING, 4: PHASE_FINISHING}


def _phase_for(status: int) -> str:
    return _PHASE_BY_STATUS.get(status, "unknown")


@dataclass
class XeniaState:
    """Thread-safe state behind the four E64-facing routes.

    ``ma_status`` is the state-machine source of truth:

    * ``1`` idle (``extraction_status`` 0)
    * ``3`` serving/active (``extraction_status`` 1)
    * ``4`` finishing (``extraction_status`` 2)

    A completed shot sets ``REAL_EXTRACTION_TIME_MS``.  The next
    ``/api/v2/mako`` poll returns the finishing values, then decrements
    ``finishing_polls_remaining``; the final configured poll moves the state
    back to idle.
    """

    ma_status: int = 1
    extraction_status: int = 0
    extraction_time_ms: int = 0
    mass_g: float = 0.0
    flow_ml: float = 0.0
    sync_weight_g: int = 0
    sync_filter: int = 0
    script: str = ""
    timestamp: int = 0
    finish_pending: bool = False
    finishing_polls_remaining: int = 0
    last_shot: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _touch(self) -> None:
        self.timestamp = int(time.time() * 1000)

    def _phase_locked(self) -> str:
        return _phase_for(self.ma_status)

    @property
    def phase(self) -> str:
        with self.lock:
            return self._phase_locked()

    def start_brew(self) -> None:
        """Enter the serving/active phase (E64 ``scripts/execute``)."""

        with self.lock:
            self.ma_status = 3
            self.extraction_status = 1
            self.extraction_time_ms = 0
            self.finish_pending = False
            self.finishing_polls_remaining = 0
            self._touch()

    def finish_shot(
        self,
        duration_ms: float,
        mass_g: float,
        *,
        finishing_polls: int = 1,
        reason: str = "",
    ) -> None:
        """Set the measured shot and enter the finishing phase.

        A direct control shot may arrive without a preceding
        ``scripts/execute``.  In that case the state visibly passes through
        serving before finishing, so the reported sequence is always
        idle -> serving -> finishing -> idle.
        """

        try:
            duration = float(duration_ms)
            mass = float(mass_g)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("duration_ms and mass_g must be finite numbers") from exc
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("duration_ms must be a finite non-negative number")
        if not math.isfinite(mass) or mass < 0:
            raise ValueError("mass_g must be a finite non-negative number")
        if finishing_polls < 1:
            raise ValueError("finishing_polls must be at least 1")

        with self.lock:
            if self.ma_status == 1:
                self.ma_status = 3
                self.extraction_status = 1
                self._touch()
            self.ma_status = 4
            self.extraction_status = 2
            self.extraction_time_ms = int(round(duration))
            self.mass_g = mass
            self.finish_pending = True
            self.finishing_polls_remaining = int(finishing_polls)
            self.last_shot = {
                "duration_ms": self.extraction_time_ms,
                "mass_g": mass,
                "reason": reason,
            }
            self._touch()

    def idle(self) -> None:
        """Return to idle without discarding the last measured shot."""

        with self.lock:
            self.ma_status = 1
            self.extraction_status = 0
            self.finish_pending = False
            self.finishing_polls_remaining = 0
            self._touch()

    def reset(self) -> None:
        """Return to the initial state, including brew-ratio and last shot."""

        with self.lock:
            self.ma_status = 1
            self.extraction_status = 0
            self.extraction_time_ms = 0
            self.mass_g = 0.0
            self.flow_ml = 0.0
            self.sync_weight_g = 0
            self.sync_filter = 0
            self.script = ""
            self.finish_pending = False
            self.finishing_polls_remaining = 0
            self.last_shot = None
            self._touch()

    def machine(self) -> dict[str, Any]:
        with self.lock:
            return {
                "MA_TYPE": "XENIA",
                "ma_type": "xenia",
                "MA_SN": "VG-XENIA-0001",
                "FW_VERSION_MAJOR": 3,
                "FW_VERSION_MINOR": 87,
                "ESP_FW_MAJOR": 3,
                "ESP_FW_MINOR": 87,
                "TIMESTAMP": self.timestamp,
            }

    def mako(self) -> dict[str, Any]:
        """Return the exact E64-facing Mako shape and advance finishing->idle.

        The returned document describes the state *before* the transition, so
        a completed shot is visible for at least one poll.
        """

        with self.lock:
            value = {
                "MA_STATUS": self.ma_status,
                "MA_EXTRACTION_STATUS": self.extraction_status,
                "REAL_EXTRACTION_TIME_MS": self.extraction_time_ms,
                "PU_SENS_FLOW_METER_VOLUME": round(self.flow_ml, 3),
                "BG_SET_TEMP": 93,
                "BG_SENS_TEMP": 92,
                "TANK_LEVEL": 1,
                "script": self.script,
                "TIMESTAMP": self.timestamp,
                "MA_SYNC_BEVERAGE_WEIGHT": self.sync_weight_g,
                "MA_SYNC_FILTER": self.sync_filter,
            }
            if self.finishing_polls_remaining > 0:
                self.finishing_polls_remaining -= 1
                if self.finishing_polls_remaining == 0:
                    self.finish_pending = False
                    self.ma_status = 1
                    self.extraction_status = 0
                    self._touch()
            return value

    def snapshot(self) -> dict[str, Any]:
        """Control/diagnostic view; never part of the four E64 routes."""

        with self.lock:
            return {
                "phase": self._phase_locked(),
                "ma_status": self.ma_status,
                "extraction_status": self.extraction_status,
                "extraction_time_ms": self.extraction_time_ms,
                "mass_g": self.mass_g,
                "flow_ml": self.flow_ml,
                "sync_weight_g": self.sync_weight_g,
                "sync_filter": self.sync_filter,
                "timestamp": self.timestamp,
                "finishing_polls_remaining": self.finishing_polls_remaining,
                "last_shot": dict(self.last_shot) if self.last_shot else None,
            }


class _Handler(BaseHTTPRequestHandler):
    server_version = "XeniaEmulator/2"
    protocol_version = "HTTP/1.1"

    @property
    def xenia(self) -> "XeniaServer":
        return self.server.xenia_server  # type: ignore[attr-defined]

    @property
    def state(self) -> XeniaState:
        return self.xenia.state

    @property
    def is_control_listener(self) -> bool:
        return self.server.xenia_role == "control"  # type: ignore[attr-defined]

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Log method/path/status only, never headers, bodies or query strings."""

        if not self.xenia.verbose:
            return
        # send_error() can call this before parse_request() has set command/path.
        command = getattr(self, "command", "-") or "-"
        path = (getattr(self, "path", "") or "").split("?", 1)[0] or "-"
        self.xenia.log(f"{self.client_address[0]} {command} {path} {code} {size}")

    def log_message(self, _fmt: str, *_args: Any) -> None:
        if not self.xenia.verbose:
            return
        # BaseHTTPRequestHandler calls this for protocol/parse errors and may
        # pass the raw request line.  Do not echo it: a malformed line could
        # contain a token or query string.
        self.xenia.log(f"{self.client_address[0]} protocol error")

    def _json(self, status: int, value: Mapping[str, Any] | list[Any]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length > 1024 * 1024:
            raise ValueError("body exceeds 1 MiB")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/control/") and not self.is_control_listener:
            # The control API is never exposed on the E64-facing listener.
            self._json(404, {"error": "unknown path", "path": path})
        elif path == "/api/v2/mako":
            self._json(200, self.state.mako())
        elif path == "/api/v2/machine":
            self._json(200, self.state.machine())
        elif path == "/control/state":
            self._json(200, self.state.snapshot())
        else:
            self._json(404, {"error": "unknown path", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        allowed_paths = _POST_PATHS if self.is_control_listener else _E64_POST_PATHS
        if path not in allowed_paths:
            # Route first so an unknown path is 404 even with a broken body.
            # Close without consuming the body so leftover bytes are not
            # parsed as another request on a keep-alive connection.
            self.close_connection = True
            self._json(404, {"error": "unknown path", "path": path})
            return
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            self.close_connection = True
            self._json(400, {"error": str(exc)})
            return
        try:
            if path == "/api/v2/brewratio":
                with self.state.lock:
                    weight = _int_field(body.get("SYNC_BEVERAGE_WEIGHT", 0), "SYNC_BEVERAGE_WEIGHT")
                    filter_value = _int_field(body.get("SYNC_FILTER", 0), "SYNC_FILTER")
                    self.state.sync_weight_g = weight
                    self.state.sync_filter = filter_value
                    self.state._touch()
                self._json(200, {
                    "MA_SYNC_BEVERAGE_WEIGHT": weight,
                    "MA_SYNC_FILTER": filter_value,
                })
            elif path == "/api/v2/scripts/execute":
                self.state.start_brew()
                self._json(200, {"ok": True, "ID": body.get("ID")})
            elif path == "/control/start":
                self.state.start_brew()
                self._json(200, self.state.snapshot())
            elif path == "/control/shot":
                duration = _number_field(body, "duration_ms", non_negative=True)
                mass = _number_field(body, "mass_g", non_negative=True)
                self.state.finish_shot(
                    duration,
                    mass,
                    finishing_polls=self.xenia.finishing_polls,
                    reason="control",
                )
                self._json(200, self.state.snapshot())
            elif path == "/control/sample":
                grams = _number_field(body, "grams", non_negative=False)
                milliseconds = _number_field(body, "ms", non_negative=True)
                shot = self.xenia.feed_sample(
                    ScaleSample(grams, milliseconds, source="control")
                )
                self._json(200, {
                    "shot": shot.as_dict() if shot is not None else None,
                    "state": self.state.snapshot(),
                })
            elif path == "/control/idle":
                self.xenia.idle()
                self._json(200, self.state.snapshot())
            elif path == "/control/reset":
                self.xenia.reset()
                self._json(200, self.state.snapshot())
            else:  # pragma: no cover - guarded by _POST_PATHS
                self._json(404, {"error": "unknown path", "path": path})
        except (TypeError, ValueError) as exc:
            self.close_connection = True
            self._json(400, {"error": str(exc)})


_POST_PATHS = frozenset({
    "/api/v2/brewratio",
    "/api/v2/scripts/execute",
    "/control/start",
    "/control/shot",
    "/control/sample",
    "/control/idle",
    "/control/reset",
})

_E64_POST_PATHS = frozenset({
    "/api/v2/brewratio",
    "/api/v2/scripts/execute",
})


def _int_field(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _number_field(body: Mapping[str, Any], name: str, *, non_negative: bool) -> float:
    if name not in body:
        raise ValueError(f"{name} is required")
    value = body[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if non_negative and result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


class XeniaServer:
    """Two HTTP listeners sharing one :class:`XeniaState`.

    Both default to loopback; the E64-facing listener never serves the
    ``/control/*`` API.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        control_host: str = "127.0.0.1",
        control_port: int = 8081,
        verbose: bool = False,
        log_stream: Any | None = None,
        finishing_polls: int = 1,
    ) -> None:
        if finishing_polls < 1:
            raise ValueError("finishing_polls must be at least 1")
        self.host = host
        self.control_host = control_host
        self.verbose = verbose
        self.finishing_polls = int(finishing_polls)
        self.state = XeniaState()
        self.detector = ShotDetector()
        self._detector_lock = threading.Lock()
        self._log_stream = log_stream if log_stream is not None else sys.stderr
        self._log_lock = threading.Lock()
        self._replay_stop = threading.Event()
        self._replay_thread: threading.Thread | None = None
        self._threads: list[threading.Thread] = []
        self._started = False
        self._closed = False
        self.http = _make_server(host, port, self, role="e64")
        try:
            self.control = _make_server(control_host, control_port, self, role="control")
        except OSError:
            self.http.server_close()
            raise

    @property
    def port(self) -> int:
        return int(self.http.server_address[1])

    @property
    def control_port(self) -> int:
        return int(self.control.server_address[1])

    def log(self, message: str) -> None:
        with self._log_lock:
            print(f"xenia: {message}", file=self._log_stream, flush=True)

    def start(self) -> "XeniaServer":
        if self._started:
            return self
        for server in (self.http, self.control):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._threads.append(thread)
        self._started = True
        return self

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._replay_stop.set()
        if not self._started:
            for server in (self.http, self.control):
                server.server_close()
            return
        for server in (self.http, self.control):
            server.shutdown()
            server.server_close()
        if self._replay_thread is not None:
            self._replay_thread.join(timeout=2)
        for thread in self._threads:
            thread.join(timeout=2)

    def __enter__(self) -> "XeniaServer":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def idle(self) -> None:
        """Abort to idle and discard any partially detected shot."""

        self.state.idle()
        with self._detector_lock:
            self.detector = ShotDetector()

    def reset(self) -> None:
        """Return state and detector to their initial values."""

        self.state.reset()
        with self._detector_lock:
            self.detector = ShotDetector()

    def feed_sample(self, sample: ScaleSample) -> Shot | None:
        """Feed one sample to the persistent control detector."""

        with self._detector_lock:
            shot = self.detector.add(sample)
        if shot is not None:
            self.state.finish_shot(
                shot.duration_ms,
                shot.mass_g,
                finishing_polls=self.finishing_polls,
                reason=shot.reason,
            )
        return shot

    def feed_samples(
        self,
        samples: Iterable[ScaleSample],
        *,
        speed: float = 0.0,
        detector: ShotDetector | None = None,
        stop_event: threading.Event | None = None,
    ) -> list[Shot]:
        """Feed samples through a (normally fresh) detector and finish shots.

        ``speed`` is a wall-clock multiplier.  ``0`` feeds immediately; ``1``
        sleeps by the sample's ``ms`` deltas; ``2`` runs twice as fast.
        """

        if not math.isfinite(speed) or speed < 0:
            raise ValueError("speed must be a finite non-negative number")
        active = detector if detector is not None else ShotDetector()
        shots: list[Shot] = []
        previous_ms: float | None = None
        for sample in samples:
            if stop_event is not None and stop_event.is_set():
                break
            if speed > 0 and previous_ms is not None:
                delay = max(0.0, (sample.ms - previous_ms) / 1000.0 / speed)
                if delay:
                    if stop_event is not None:
                        if stop_event.wait(delay):
                            break
                    else:
                        time.sleep(delay)
            previous_ms = sample.ms
            with self._detector_lock:
                shot = active.add(sample)
            if shot is not None:
                self.state.finish_shot(
                    shot.duration_ms,
                    shot.mass_g,
                    finishing_polls=self.finishing_polls,
                    reason=shot.reason,
                )
                shots.append(shot)
        return shots

    def replay_file(
        self,
        path: str | Path,
        *,
        speed: float = 0.0,
        detector: ShotDetector | None = None,
        stop_event: threading.Event | None = None,
    ) -> list[Shot]:
        """Synchronously replay a JSONL file through the real ShotDetector."""

        return self.feed_samples(
            iter_replay(path),
            speed=speed,
            detector=detector,
            stop_event=stop_event,
        )

    def start_replay(self, path: str | Path, *, speed: float = 1.0) -> threading.Thread:
        """Parse a JSONL file now and feed it from a daemon thread."""

        return self.start_replay_samples(list(iter_replay(path)), speed=speed)

    def start_replay_samples(
        self,
        samples: Iterable[ScaleSample],
        *,
        speed: float = 1.0,
    ) -> threading.Thread:
        if not math.isfinite(speed) or speed < 0:
            raise ValueError("speed must be a finite non-negative number")
        if self._replay_thread is not None and self._replay_thread.is_alive():
            raise RuntimeError("a replay is already running")
        self._replay_stop.clear()
        thread = threading.Thread(
            target=self._replay_worker,
            args=(list(samples), speed),
            name="xenia-replay",
            daemon=True,
        )
        self._replay_thread = thread
        thread.start()
        return thread

    def _replay_worker(self, samples: list[ScaleSample], speed: float) -> None:
        try:
            shots = self.feed_samples(
                samples,
                speed=speed,
                stop_event=self._replay_stop,
            )
        except (OSError, ValueError) as exc:
            self.log(f"replay error: {exc}")
            return
        if self._replay_stop.is_set():
            return
        if self.verbose:
            self.log(f"replay complete: {len(shots)} shot(s)")


def _make_server(host: str, port: int, owner: XeniaServer, *, role: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.xenia_server = owner  # type: ignore[attr-defined]
    server.xenia_state = owner.state  # type: ignore[attr-defined]
    server.xenia_role = role  # type: ignore[attr-defined]
    return server


def _display_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bind = parser.add_argument_group("bind")
    bind.add_argument(
        "--host",
        default="127.0.0.1",
        help="E64-facing bind address (default 127.0.0.1; use a LAN address deliberately)",
    )
    bind.add_argument("--port", type=int, default=8080, help="E64-facing HTTP port (80 on the real path)")
    bind.add_argument(
        "--control-host",
        default="127.0.0.1",
        help="control bind address (default loopback, independent of --host)",
    )
    bind.add_argument("--control-port", type=int, default=8081)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log method/path/status; headers, bodies and query strings are never logged",
    )
    parser.add_argument(
        "--finishing-polls",
        type=int,
        default=1,
        help="number of /api/v2/mako polls that report finishing before idle (default 1)",
    )
    replay = parser.add_argument_group("scale replay")
    replay.add_argument(
        "--replay",
        metavar="FILE",
        help='JSONL samples: {"grams": 12.3, "ms": 1000}',
    )
    replay.add_argument(
        "--replay-speed",
        type=float,
        default=1.0,
        help="wall-clock multiplier for --replay; 0 feeds immediately (default 1.0)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.finishing_polls < 1:
        parser.error("--finishing-polls must be at least 1")
    if not math.isfinite(args.replay_speed) or args.replay_speed < 0:
        parser.error("--replay-speed must be finite and non-negative")

    replay_samples: list[ScaleSample] | None = None
    if args.replay:
        try:
            replay_samples = list(iter_replay(args.replay))
        except (OSError, ValueError) as exc:
            print(f"xenia-emulator: {exc}", file=sys.stderr)
            return 2

    try:
        server = XeniaServer(
            host=args.host,
            port=args.port,
            control_host=args.control_host,
            control_port=args.control_port,
            verbose=args.verbose,
            finishing_polls=args.finishing_polls,
        ).start()
    except OSError as exc:
        print(f"xenia-emulator: bind failed: {exc}", file=sys.stderr)
        return 2

    if not _is_loopback(args.host):
        print(
            "warning: the E64-facing listener is not loopback; "
            "use it only on a trusted local network",
            file=sys.stderr,
        )
    print(f"Xenia emulator: http://{_display_host(args.host)}:{server.port}/api/v2/mako", flush=True)
    print(f"control:        http://{_display_host(args.control_host)}:{server.control_port}/control/state", flush=True)

    try:
        if replay_samples is not None:
            print(
                f"replay:         {args.replay} "
                f"({len(replay_samples)} samples, speed {args.replay_speed:g})"
            )
            server.start_replay_samples(replay_samples, speed=args.replay_speed)
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
