#!/usr/bin/env python3
"""Minimal Xenia coffee-machine emulator for the E64's local M2M client.

The grinder polls a Xenia machine over plain HTTP on port 80:

    GET  /api/v2/mako
    GET  /api/v2/machine
    POST /api/v2/brewratio
    POST /api/v2/scripts/execute

This server implements those shapes, plus a small control API so a scale bridge
can report a shot:

    POST /control/shot  {"duration_ms": 25000, "mass_g": 18.2}
    POST /control/idle
    GET  /control/state

It is a research fixture, not a Xenia replacement.  The grinder must be pointed
at the host manually; on stock firmware the M2M/cloud flag and the Xenia host
are set in the UI.  Whether `REAL_EXTRACTION_TIME_MS` reaches GbsService and
moves the dial still needs a physical test (docs/NO_ROOT_SCALE_PATHS.md).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping


@dataclass
class XeniaState:
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
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _touch(self) -> None:
        self.timestamp = int(time.time() * 1000)

    def start_brew(self) -> None:
        with self.lock:
            self.ma_status = 3
            self.extraction_status = 1
            self.extraction_time_ms = 0
            self.finish_pending = False
            self._touch()

    def finish_shot(self, duration_ms: int, mass_g: float) -> None:
        if duration_ms < 0 or mass_g < 0:
            raise ValueError("duration and mass must be non-negative")
        with self.lock:
            self.ma_status = 4
            self.extraction_status = 2
            self.extraction_time_ms = int(duration_ms)
            self.mass_g = float(mass_g)
            self.finish_pending = True
            self._touch()

    def idle(self) -> None:
        with self.lock:
            self.ma_status = 1
            self.extraction_status = 0
            self.finish_pending = False
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
            if self.finish_pending:
                # Give the grinder exactly one finishing poll, then return to
                # idle so its BrewState machine can complete the shot.
                self.finish_pending = False
                self.ma_status = 1
                self.extraction_status = 0
                self._touch()
            return value

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "ma_status": self.ma_status,
                "extraction_status": self.extraction_status,
                "extraction_time_ms": self.extraction_time_ms,
                "mass_g": self.mass_g,
                "flow_ml": self.flow_ml,
                "sync_weight_g": self.sync_weight_g,
                "sync_filter": self.sync_filter,
                "timestamp": self.timestamp,
            }


class _Handler(BaseHTTPRequestHandler):
    server_version = "XeniaEmulator/1"
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> XeniaState:
        return self.server.xenia_state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, value: Mapping[str, Any] | list[Any]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 1024 * 1024:
            raise ValueError("body exceeds 1 MiB")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/v2/mako":
            self._json(200, self.state.mako())
        elif path == "/api/v2/machine":
            self._json(200, self.state.machine())
        elif path == "/control/state":
            self._json(200, self.state.snapshot())
        else:
            self._json(404, {"error": "unknown path", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            if path == "/api/v2/brewratio":
                with self.state.lock:
                    self.state.sync_weight_g = int(body.get("SYNC_BEVERAGE_WEIGHT", 0))
                    self.state.sync_filter = int(body.get("SYNC_FILTER", 0))
                    self.state._touch()
                self._json(200, {
                    "MA_SYNC_BEVERAGE_WEIGHT": self.state.sync_weight_g,
                    "MA_SYNC_FILTER": self.state.sync_filter,
                })
            elif path == "/api/v2/scripts/execute":
                self.state.start_brew()
                self._json(200, {"ok": True, "ID": body.get("ID")})
            elif path == "/control/shot":
                self.state.finish_shot(
                    int(body.get("duration_ms", 0)),
                    float(body.get("mass_g", 0)),
                )
                self._json(200, self.state.snapshot())
            elif path == "/control/idle":
                self.state.idle()
                self._json(200, self.state.snapshot())
            else:
                self._json(404, {"error": "unknown path", "path": path})
        except (TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})


class XeniaServer:
    def __init__(self, *, port: int = 8080, control_port: int = 8081) -> None:
        self.state = XeniaState()
        self.http = _make_server(port, self.state)
        self.control = _make_server(control_port, self.state)
        self._threads: list[threading.Thread] = []

    @property
    def port(self) -> int:
        return int(self.http.server_address[1])

    @property
    def control_port(self) -> int:
        return int(self.control.server_address[1])

    def start(self) -> "XeniaServer":
        for server in (self.http, self.control):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def close(self) -> None:
        for server in (self.http, self.control):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)

    def __enter__(self) -> "XeniaServer":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _make_server(port: int, state: XeniaState) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.xenia_state = state  # type: ignore[attr-defined]
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8080, help="grinder-facing HTTP port (80 on the real path)")
    parser.add_argument("--control-port", type=int, default=8081)
    args = parser.parse_args()
    server = XeniaServer(port=args.port, control_port=args.control_port).start()
    print(f"Xenia emulator: http://127.0.0.1:{server.port}/api/v2/mako")
    print(f"control:        http://127.0.0.1:{server.control_port}/control/state")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
