"""Local OpenScale WiFi WebSocket client and frame parser.

OpenScale's current WiFi interface is an unauthenticated WebSocket at
``ws://<host>/snapshot``.  This module keeps the transport optional and
provides a conservative command surface: measurements, status, battery,
info, and stream-rate negotiation.  Tare is opt-in; power/display/sleep and
arbitrary command JSON are not exposed by the safe helper.

This is an OpenScale transport, not an E64 protocol implementation.  The
caller must correlate samples with a shot and must use the E64 adapter's
separate policy before recording or actuating anything on the grinder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Any, Callable, Mapping


class OpenScaleProtocolError(ValueError):
    """Raised for malformed frames or commands outside the safe boundary."""


@dataclass(frozen=True)
class OpenScaleSnapshot:
    grams: float
    milliseconds: float
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class OpenScaleTypedFrame:
    frame_type: str
    raw: Mapping[str, Any]


def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise OpenScaleProtocolError(f"{field} must be a JSON number")
    result = float(value)
    if not isfinite(result):
        raise OpenScaleProtocolError(f"{field} must be finite")
    return result


def parse_frame(value: Mapping[str, Any] | str | bytes) -> OpenScaleSnapshot | OpenScaleTypedFrame:
    """Parse one OpenScale JSON frame without changing device state."""

    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise OpenScaleProtocolError("frame is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise OpenScaleProtocolError("frame must be a JSON object")
    raw = dict(value)
    frame_type = raw.get("type")
    if frame_type is None:
        if "grams" not in raw or "ms" not in raw:
            raise OpenScaleProtocolError("untyped frame must contain grams and ms")
        return OpenScaleSnapshot(
            grams=_finite_number(raw["grams"], field="grams"),
            milliseconds=_finite_number(raw["ms"], field="ms"),
            raw=raw,
        )
    if not isinstance(frame_type, str) or not frame_type:
        raise OpenScaleProtocolError("typed frame type must be a non-empty string")
    return OpenScaleTypedFrame(frame_type=frame_type, raw=raw)


SAFE_READ_COMMANDS = frozenset({"status", "battery", "info"})
SAFE_RATE_HZ = frozenset({2, 5, 10})


class OpenScaleClient:
    """Owner-controlled local transport with a deliberately narrow command API."""

    def __init__(
        self,
        host: str = "hds.local",
        *,
        port: int = 80,
        path: str = "/snapshot",
        timeout: float = 6.0,
        websocket_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.host = host.removeprefix("ws://").removeprefix("http://").rstrip("/")
        self.port = port
        self.path = path if path.startswith("/") else "/" + path
        self.timeout = timeout
        self._factory = websocket_factory
        self._socket: Any = None

    @property
    def url(self) -> str:
        suffix = "" if self.port == 80 else f":{self.port}"
        return f"ws://{self.host}{suffix}{self.path}"

    def connect(self) -> None:
        if self._socket is not None:
            return
        factory = self._factory
        if factory is None:
            try:
                from websocket import create_connection  # type: ignore[import-not-found]
            except ImportError as exc:
                raise OpenScaleProtocolError(
                    "OpenScale transport requires websocket-client"
                ) from exc
            factory = create_connection
        try:
            self._socket = factory(self.url, timeout=self.timeout)
        except Exception as exc:
            raise OpenScaleProtocolError(f"OpenScale connection failed: {exc}") from exc

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def send_text(self, command: str) -> None:
        if not isinstance(command, str) or not command:
            raise OpenScaleProtocolError("command must be non-empty text")
        self.connect()
        self._socket.send(command)

    def send_safe(self, command: str, *, allow_tare: bool = False) -> None:
        """Send only read/status/rate commands, plus explicitly enabled tare."""

        if not isinstance(command, str):
            raise OpenScaleProtocolError("command must be non-empty text")
        normalized = command.strip().lower()
        if normalized == "tare" and allow_tare:
            self.send_text("tare")
            return
        if normalized in SAFE_READ_COMMANDS:
            self.send_text(normalized)
            return
        if normalized in {"rate 2k", "rate 5k", "rate 10k"}:
            self.send_text(normalized)
            return
        raise OpenScaleProtocolError("command is outside the safe OpenScale boundary")

    def set_rate(self, rate_hz: int) -> None:
        if rate_hz not in SAFE_RATE_HZ:
            raise OpenScaleProtocolError("OpenScale rate must be 2, 5, or 10 Hz")
        self.send_safe(f"rate {rate_hz}k")

    def recv_frame(self) -> OpenScaleSnapshot | OpenScaleTypedFrame:
        self.connect()
        try:
            return parse_frame(self._socket.recv())
        except OpenScaleProtocolError:
            raise
        except Exception as exc:
            raise OpenScaleProtocolError(f"OpenScale receive failed: {exc}") from exc


__all__ = [
    "OpenScaleClient",
    "OpenScaleProtocolError",
    "OpenScaleSnapshot",
    "OpenScaleTypedFrame",
    "SAFE_RATE_HZ",
    "SAFE_READ_COMMANDS",
    "parse_frame",
]
