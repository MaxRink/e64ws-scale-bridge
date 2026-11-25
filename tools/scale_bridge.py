#!/usr/bin/env python3
"""Local scale-to-grinder bridge for E64 WS grind-by-sync.

The grinder has no local Bluetooth or BLE scale transport.  This bridge runs on
an owner-controlled host, reads a scale, detects an espresso shot, and turns the
result into the one brew-event shape the E64 accepts:

    GrinderAction / AddBrewEvent

Supported inputs
----------------
* ``--replay FILE``      JSONL samples, for deterministic tests and post-shot
                         replay: ``{"grams": 0.0, "ms": 0}``.
* ``--openscale HOST``   Half Decent Scale / OpenScale WiFi WebSocket
                         (``ws://<host>/snapshot``).
* ``--ble ADDRESS``      BLE scale.  ``--ble-kind decent`` reads the Decent /
                         Half Decent Scale service (FFF0/FFF4), ``generic``
                         reads the standard Weight Scale service
                         (181D/2A9D), and ``auto`` probes both.

The E64 side is deliberately guarded.  ``AddBrewEvent`` is dev-only on the
shipped image (one of the 44 commands gated by the 14-token ``/etc/issue``),
so a stock grinder refuses it.  This tool prints the event by default and sends
it only with ``--apply --yes``.  See docs/GRIND_BY_SYNC_BT_SCALE.md.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import math
import queue
import statistics
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

try:  # package mode (repo root) and script mode (tools/ on sys.path)
    from tools.e64ws_protocol import E64WSProtocol
    from tools.openscale_client import (
        OpenScaleClient,
        OpenScaleProtocolError,
        OpenScaleSnapshot,
        OpenScaleTypedFrame,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from e64ws_protocol import E64WSProtocol
    from openscale_client import (
        OpenScaleClient,
        OpenScaleProtocolError,
        OpenScaleSnapshot,
        OpenScaleTypedFrame,
    )


def _e64_client():
    """Import the TLS/WSS client lazily.

    The on-device daemon imports this module for the detector only.  The E64's
    Python 3.10 has no ssl module, so a top-level import would make the module
    unusable there.
    """

    try:
        from tools.e64ws_client import login, login_no_password, run_request
    except ModuleNotFoundError:  # pragma: no cover - direct script execution
        from e64ws_client import login, login_no_password, run_request
    return login, login_no_password, run_request


def _redact(value: object, token: str) -> str:
    """Redact through the WSS client's one implementation (lazily imported)."""

    try:
        from tools.e64ws_client import redact
    except ModuleNotFoundError:  # pragma: no cover - direct script execution
        from e64ws_client import redact
    return redact(value, token)


REJECTED_EXIT = 3


def rejection_reason(response: Mapping[str, Any]) -> str:
    """Return the grinder's reason when it refused the action, else ``""``.

    An accepted action answers ``{"success": true}``.  An explicit
    ``success: false`` is a *refusal* -- not a transport error -- and it used
    to be indistinguishable from acceptance: the CLI exited 0, so automation
    and the audit line could not tell "sent and refused" from "sent and
    accepted".  Only an explicit ``false`` counts, so a payload that simply
    does not carry the field keeps the old behaviour.
    """

    if not isinstance(response, Mapping) or response.get("success") is not False:
        return ""
    for key in ("error", "message", "reason", "detail"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "the grinder gave no reason"


DECENT_SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
DECENT_NOTIFY = "0000fff4-0000-1000-8000-00805f9b34fb"
DECENT_WRITE = "000036f5-0000-1000-8000-00805f9b34fb"
GENERIC_SERVICE = "0000181d-0000-1000-8000-00805f9b34fb"
GENERIC_NOTIFY = "00002a9d-0000-1000-8000-00805f9b34fb"

LB_TO_G = 453.59237


# --------------------------------------------------------------------------- model


@dataclass(frozen=True)
class ScaleSample:
    grams: float
    ms: float
    source: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.grams):
            raise ValueError("grams must be finite")
        if not math.isfinite(self.ms) or self.ms < 0:
            raise ValueError("ms must be a finite non-negative timestamp")


@dataclass(frozen=True)
class Shot:
    start_ms: float
    end_ms: float
    duration_ms: float
    baseline_g: float
    mass_g: float
    peak_g: float
    sample_count: int
    valid: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "baseline_g": self.baseline_g,
            "mass_g": self.mass_g,
            "peak_g": self.peak_g,
            "sample_count": self.sample_count,
            "valid": self.valid,
            "reason": self.reason,
        }


class ShotDetector:
    """Small, deterministic espresso-shot detector.

    It assumes the scale is stable for a moment before the shot.  A shot starts
    when the reading rises by ``start_threshold_g`` above the idle baseline and
    ends after a stable window (or a cup-removal drop).  Legitimacy uses the
    documented >15 s Sync window; that is a policy decision, not a wire rule.
    """

    def __init__(
        self,
        *,
        baseline_samples: int = 5,
        start_threshold_g: float = 0.5,
        stability_g: float = 0.15,
        stable_ms: float = 1000.0,
        min_duration_ms: float = 3000.0,
        valid_min_ms: float = 15000.0,
        drop_threshold_g: float = 2.0,
        max_shot_ms: float = 120_000.0,
    ) -> None:
        if baseline_samples < 1:
            raise ValueError("baseline_samples must be positive")
        if start_threshold_g <= 0:
            raise ValueError("start_threshold_g must be positive")
        if stability_g < 0:
            raise ValueError("stability_g must not be negative")
        if stable_ms <= 0 or min_duration_ms <= 0 or valid_min_ms <= 0:
            raise ValueError("time windows must be positive")
        if not math.isfinite(max_shot_ms) or max_shot_ms <= 0:
            raise ValueError("max_shot_ms must be a positive number of milliseconds")
        self.baseline_samples = baseline_samples
        self.start_threshold_g = start_threshold_g
        self.stability_g = stability_g
        self.stable_ms = stable_ms
        self.min_duration_ms = min_duration_ms
        self.valid_min_ms = valid_min_ms
        self.drop_threshold_g = drop_threshold_g
        self.max_shot_ms = max_shot_ms
        self._idle: list[ScaleSample] = []
        self._drift: list[ScaleSample] = []
        self._baseline: float | None = None
        self._last_ms: float | None = None
        self._active = False
        self._samples: list[ScaleSample] = []
        self._start_ms = 0.0
        self._start_g = 0.0
        self._peak_g = 0.0

    @property
    def active(self) -> bool:
        """True between shot start and shot end."""

        return self._active

    @property
    def start_ms(self) -> float:
        """Scale timestamp of the current/last shot start (0 when idle)."""

        return self._start_ms

    def _reset_after_shot(self) -> None:
        self._idle = []
        self._drift = []
        self._baseline = None
        self._active = False
        self._samples = []
        self._start_ms = 0.0
        self._start_g = 0.0
        self._peak_g = 0.0

    def _finish(
        self,
        end_ms: float,
        mass_g: float,
        reason: str,
        *,
        valid: bool | None = None,
    ) -> Shot:
        duration = end_ms - self._start_ms
        if valid is None:
            valid = duration >= self.valid_min_ms and mass_g > 0
        shot = Shot(
            start_ms=self._start_ms,
            end_ms=end_ms,
            duration_ms=duration,
            baseline_g=self._start_g,
            mass_g=mass_g,
            peak_g=max(0.0, self._peak_g - self._start_g),
            sample_count=len(self._samples),
            valid=valid,
            reason=reason,
        )
        self._reset_after_shot()
        return shot

    def add(self, sample: ScaleSample) -> Shot | None:
        if self._last_ms is not None and sample.ms <= self._last_ms:
            raise ValueError("scale timestamps must strictly increase")
        self._last_ms = sample.ms

        if not self._active:
            if self._baseline is None:
                self._idle.append(sample)
                if len(self._idle) >= self.baseline_samples:
                    self._baseline = statistics.median(s.grams for s in self._idle)
                return None
            # Decide against the last stable baseline, never against a baseline
            # that already includes the rising sample.  Otherwise a two-sample
            # baseline reports 1.0 g for a 0 -> 2 g step and loses 1 g of mass.
            if sample.grams - self._baseline >= self.start_threshold_g:
                self._active = True
                self._start_ms = sample.ms
                self._start_g = self._baseline
                self._samples = list(self._idle)
                self._peak_g = sample.grams
                self._drift = []
                return None
            # Track slow drift only while the reading is genuinely stable.  A
            # continuously rising pre-infusion must not drag the baseline up
            # with it, which would hide the shot start.
            if abs(sample.grams - self._baseline) <= self.stability_g:
                self._drift = []
                self._idle.append(sample)
                if len(self._idle) > self.baseline_samples:
                    self._idle.pop(0)
                self._baseline = statistics.median(s.grams for s in self._idle)
                return None
            # Outside the window: the scale may simply have settled at a new
            # level (the cup removed after a shot, a drip tray, a re-tare).
            # Without this the window freezes on the old level forever and the
            # next shot is never detected.  Re-seed only from a candidate
            # window that is itself stable, so a rising trend is never absorbed
            # into the baseline.
            self._drift.append(sample)
            if len(self._drift) > self.baseline_samples:
                self._drift.pop(0)
            if len(self._drift) >= self.baseline_samples:
                levels = [s.grams for s in self._drift]
                if max(levels) - min(levels) <= self.stability_g:
                    self._idle = list(self._drift)
                    self._baseline = statistics.median(levels)
                    self._drift = []
            return None

        self._samples.append(sample)
        self._peak_g = max(self._peak_g, sample.grams)
        elapsed = sample.ms - self._start_ms

        if elapsed >= self.min_duration_ms:
            cutoff = sample.ms - self.stable_ms
            window = [s for s in self._samples if s.ms >= cutoff]
            if len(window) >= 2:
                recent = [s.grams for s in window]
                if max(recent) - min(recent) <= self.stability_g and abs(
                    recent[-1] - statistics.median(recent)
                ) <= self.stability_g:
                    mass = statistics.median(recent) - self._start_g
                    return self._finish(sample.ms, mass, "stable weight")
            if self._peak_g - sample.grams >= self.drop_threshold_g:
                previous = self._samples[-2] if len(self._samples) >= 2 else sample
                mass = previous.grams - self._start_g
                return self._finish(previous.ms, mass, "cup removed / weight drop")

        if elapsed >= self.max_shot_ms:
            # A reading that never settles (a drifting cup, a stuck sensor) must
            # not leave the detector active forever: that keeps the on-device
            # status in serving and nothing else can start.  Close it as an
            # explicitly invalid shot so the caller still sees an audit event.
            # Checked after the genuine ends so a real finish at the bound keeps
            # its own reason.
            mass = sample.grams - self._start_g
            return self._finish(sample.ms, mass, "timeout", valid=False)
        return None


# ------------------------------------------------------------------- scale parsers


def parse_decent_weight(payload: bytes) -> float | None:
    """Decent / Half Decent Scale 7- or 10-byte weight notification.

    Frames start with 0x03, carry a 0xCE or 0xCA command, then a big-endian
    16-bit weight in 0.1 g units.  Test frame ``03ce007b0000b6`` is 12.3 g.
    """

    if len(payload) not in (7, 10):
        return None
    if payload[0] != 0x03 or payload[1] not in (0xCE, 0xCA):
        return None
    return int.from_bytes(payload[2:4], "big") / 10.0


def parse_gatt_weight(payload: bytes) -> float | None:
    """Parse the standard BLE Weight Measurement characteristic (0x2A9D).

    Flags bit 0 selects imperial units.  The standard resolution is 0.005 kg
    (5 g) for SI and 0.01 lb for imperial.
    """

    if len(payload) < 3:
        return None
    flags = payload[0]
    raw = int.from_bytes(payload[1:3], "little")
    if flags & 0x01:
        return raw * 0.01 * LB_TO_G
    return raw * 5.0


def parse_ble_weight(payload: bytes, kind: str) -> float | None:
    if kind == "decent":
        return parse_decent_weight(payload)
    if kind == "generic":
        return parse_gatt_weight(payload)
    # ``auto``: try the Decent layout first, then the standard GATT one.  The
    # check must be ``is not None``: a valid 0 g frame parses to ``0.0``, which
    # is falsy, and an ``or`` would fall through and read the same bytes as an
    # imperial GATT measurement (934.4 g for 03ce00000000b6).
    decent = parse_decent_weight(payload)
    if decent is not None:
        return decent
    return parse_gatt_weight(payload)


def iter_replay(path: str | Path) -> Iterator[ScaleSample]:
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            value = json.loads(line)
            yield ScaleSample(float(value["grams"]), float(value["ms"]), source="replay")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}:{line_number}: invalid sample: {exc}") from exc


def iter_openscale(host: str, *, port: int = 80, timeout: float = 6.0) -> Iterator[ScaleSample]:
    client = OpenScaleClient(host, port=port, timeout=timeout)
    try:
        while True:
            frame = client.recv_frame()
            if isinstance(frame, OpenScaleSnapshot):
                yield ScaleSample(frame.grams, frame.milliseconds, source="openscale")
            elif isinstance(frame, OpenScaleTypedFrame):
                raw = frame.raw
                grams = raw.get("weight", raw.get("grams"))
                milliseconds = raw.get("ms", raw.get("timer_ms", raw.get("timestamp")))
                if isinstance(grams, (int, float)) and isinstance(milliseconds, (int, float)):
                    yield ScaleSample(float(grams), float(milliseconds), source="openscale-status")
    finally:
        client.close()


class BleSource:
    """BLE source using optional ``bleak``; tests cover the parsers only."""

    def __init__(self, address: str, *, kind: str = "auto", scan_timeout: float = 10.0) -> None:
        self.address = address
        self.kind = kind
        self.scan_timeout = scan_timeout
        self._queue: queue.Queue[ScaleSample | BaseException | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_ms = 0.0

    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        grams = parse_ble_weight(bytes(data), self.kind)
        if grams is None:
            return
        now = time.monotonic() * 1000.0
        if now <= self._last_ms:
            now = self._last_ms + 1.0
        self._last_ms = now
        self._queue.put(ScaleSample(grams, now, source=f"ble-{self.kind}"))

    async def _loop(self) -> None:
        try:
            from bleak import BleakClient  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("BLE input requires: python3 -m pip install bleak") from exc
        async with BleakClient(self.address) as client:
            services = {service.uuid.lower() for service in client.services}
            kind = self.kind
            if kind == "auto":
                kind = "decent" if DECENT_SERVICE in services else "generic"
            characteristic = DECENT_NOTIFY if kind == "decent" else GENERIC_NOTIFY
            if characteristic not in {
                char.uuid.lower()
                for service in client.services
                for char in service.characteristics
            }:
                raise RuntimeError(f"{self.address} has no notify characteristic for {kind}")
            await client.start_notify(characteristic, self._on_notify)
            while not self._stop.is_set():
                await asyncio.sleep(0.2)

    def _run(self) -> None:
        try:
            asyncio.run(self._loop())
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            self._queue.put(exc)
        finally:
            self._queue.put(None)

    def samples(self) -> Iterator[ScaleSample]:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            self._stop.set()


# ------------------------------------------------------------------ grinder side


def build_add_brew_event(
    shot: Shot,
    *,
    dose_index: int = 1,
    machine_name: str = "scale-bridge",
    group_head: str = "0",
) -> dict[str, Any]:
    if shot.mass_g <= 0:
        raise ValueError("shot mass must be positive")
    if shot.duration_ms <= 0:
        raise ValueError("shot duration must be positive")
    return {
        "grindUuid": str(uuid.uuid4()),
        "machineName": machine_name,
        "groupHead": group_head,
        "doseIndex": int(dose_index),
        "validity": bool(shot.valid),
        "brewTimeMs": int(round(shot.duration_ms)),
        "brewWeightMg": int(round(shot.mass_g * 1000.0)),
    }


def send_add_brew_event(
    host: str,
    token: str,
    event: Mapping[str, Any],
    *,
    insecure: bool = False,
    ca_file: str | None = None,
    timeout: float = 10.0,
    wss_port: int = 9997,
) -> dict[str, Any]:
    protocol = E64WSProtocol()
    message = protocol.action("AddBrewEvent", dict(event))
    _, _, run_request = _e64_client()
    return run_request(
        host,
        token,
        message,
        insecure=insecure,
        ca_file=ca_file,
        timeout=timeout,
        allow_hazardous=True,
        wss_port=wss_port,
    )


# -------------------------------------------------------------------------- CLI


def _samples_for(args: argparse.Namespace) -> Iterable[ScaleSample]:
    if args.replay:
        return iter_replay(args.replay)
    if args.openscale:
        return iter_openscale(args.openscale, port=args.openscale_port, timeout=args.timeout)
    if args.ble:
        return BleSource(args.ble, kind=args.ble_kind).samples()
    raise ValueError("choose one of --replay, --openscale or --ble")


def _token_for(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    if args.token_file:
        path = Path(args.token_file)
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            print(f"warning: {path} is mode {mode:o}; prefer 600", file=sys.stderr)
        token = path.read_text().strip()
        if not token:
            raise ValueError("token file is empty")
        return token
    if not args.login_role:
        raise ValueError("provide --token, --token-file or --login-role")
    login, login_no_password, _ = _e64_client()
    if args.password is not None:
        password = args.password
    elif args.password_file:
        password = Path(args.password_file).read_text().strip()
    elif args.allow_no_password:
        print("warning: using the FW-01 SELECT-only no-password workaround; "
              "use only on an owner-controlled device", file=sys.stderr)
        return login_no_password(args.grinder, insecure=args.insecure,
                                 ca_file=args.ca_file)
    else:
        password = getpass.getpass("local grinder password: ")
    return login(
        args.grinder,
        args.login_role,
        password,
        insecure=args.insecure,
        ca_file=args.ca_file,
    )


def _token_hint(args: argparse.Namespace) -> str:
    """Best-effort secret for redacting a failure that reaches ``main()``.

    ``_run`` knows the resolved token, but by the time an exception reaches
    ``main`` that local is gone.  Use what the arguments already carry; the
    ``token=``/``Bearer`` patterns inside :func:`redact` cover the rest.
    """

    token = getattr(args, "token", None)
    if token:
        return str(token)
    path = getattr(args, "token_file", None)
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def _run(args: argparse.Namespace) -> int:
    if args.preflight:
        token = _token_for(args)
        _, _, run_request = _e64_client()
        response = run_request(
            args.grinder,
            token,
            {"type": "RequestDriverState", "msgId": 1, "data": {}},
            insecure=args.insecure,
            ca_file=args.ca_file,
            timeout=args.timeout,
            wss_port=args.wss_port,
        )
        data = response.get("data", {}) if isinstance(response, dict) else {}
        print(json.dumps({
            "type": response.get("type") if isinstance(response, dict) else None,
            "ddValueDum": data.get("ddValueDum"),
            "ddSensorFailed": data.get("ddSensorFailed"),
            "grindState": data.get("grindState"),
            "ntc1TempDegC": data.get("ntc1TempDegC"),
            "fan1TachoRpm": data.get("fan1TachoRpm"),
        }, indent=2, sort_keys=True))
        return 0
    if args.apply and not args.yes:
        raise ValueError("--apply changes the grinder; add --yes after reviewing a dry run")
    detector = ShotDetector(
        baseline_samples=args.baseline_samples,
        start_threshold_g=args.start_threshold_g,
        stability_g=args.stability_g,
        stable_ms=args.stable_ms,
        min_duration_ms=args.min_duration_ms,
        valid_min_ms=args.valid_min_ms,
        drop_threshold_g=args.drop_threshold_g,
        max_shot_ms=args.max_shot_ms,
    )
    token = _token_for(args) if args.apply else ""
    for sample in _samples_for(args):
        shot = detector.add(sample)
        if shot is None:
            continue
        event = build_add_brew_event(
            shot,
            dose_index=args.dose_index,
            machine_name=args.machine_name,
            group_head=args.group_head,
        )
        record: dict[str, Any] = {"shot": shot.as_dict(), "event": event}
        # Print and flush the event *before* the write.  A failing transport
        # must not hide what was about to be sent: the event line is the audit
        # record (docs/GUIDE.md documents "printed before it is sent").
        _print_record(record, as_json=args.json)
        if args.apply:
            response = send_add_brew_event(
                args.grinder,
                token,
                event,
                insecure=args.insecure,
                ca_file=args.ca_file,
                timeout=args.timeout,
                wss_port=args.wss_port,
            )
            _print_response(response, as_json=args.json, token=token)
            refused = rejection_reason(response)
            if refused:
                # Loud on stderr, and non-zero: a rejected action must not look
                # like a delivered one to a script or to the operator.
                print(f"scale-bridge: the grinder refused AddBrewEvent: "
                      f"{_redact(refused, token)}", file=sys.stderr)
                return REJECTED_EXIT
        if args.once:
            return 0
    return 0


def _print_record(record: Mapping[str, Any], *, as_json: bool) -> None:
    """Emit the shot/event audit record and flush it immediately."""

    print(json.dumps(record, indent=2, sort_keys=True) if as_json else _human(record))
    sys.stdout.flush()


def _print_response(response: Mapping[str, Any], *, as_json: bool,
                    token: str = "") -> None:
    """Emit the grinder's answer, after the event has been recorded.

    The payload is the grinder's text, not ours: an error nested in it can
    quote the request it refused, including a URI that carries ``?token=``.
    Print the redacted form -- the README promise is that a token is never
    printed, and "only our own messages" was exactly the gap.
    """

    if as_json:
        text = json.dumps({"response": response}, indent=2, sort_keys=True)
    else:
        text = f"grinder: {json.dumps(response, sort_keys=True)}"
    print(_redact(text, token))
    sys.stdout.flush()


def _human(record: Mapping[str, Any]) -> str:
    shot = record["shot"]
    event = record["event"]
    return "\n".join([
        f"shot: {shot['mass_g']:.1f} g in {shot['duration_ms'] / 1000.0:.1f} s "
        f"({'valid' if shot['valid'] else 'invalid'}; {shot['reason']})",
        f"event: brewWeightMg={event['brewWeightMg']} brewTimeMs={event['brewTimeMs']} "
        f"validity={event['validity']}",
    ])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_argument_group("scale source (choose one)")
    source.add_argument("--replay", help="JSONL samples: {\"grams\": 12.3, \"ms\": 1000}")
    source.add_argument("--openscale", help="OpenScale / Half Decent Scale WiFi host")
    source.add_argument("--openscale-port", type=int, default=80)
    source.add_argument("--ble", help="BLE address/UUID of a scale")
    source.add_argument("--ble-kind", choices=("auto", "decent", "generic"), default="auto")
    parser.add_argument("--timeout", type=float, default=10.0)

    detect = parser.add_argument_group("shot detection")
    detect.add_argument("--baseline-samples", type=int, default=5)
    detect.add_argument("--start-threshold-g", type=float, default=0.5)
    detect.add_argument("--stability-g", type=float, default=0.15)
    detect.add_argument("--stable-ms", type=float, default=1000.0)
    detect.add_argument("--min-duration-ms", type=float, default=3000.0)
    detect.add_argument("--valid-min-ms", type=float, default=15000.0)
    detect.add_argument("--drop-threshold-g", type=float, default=2.0)
    detect.add_argument("--max-shot-ms", type=float, default=120_000.0,
                        help="close an active shot as invalid (timeout) after this long")
    detect.add_argument("--once", action="store_true", help="exit after the first completed shot")

    grinder = parser.add_argument_group("grinder (only with --apply)")
    grinder.add_argument("--grinder", help="E64 host[:port] for the local WSS/HTTPS API")
    grinder.add_argument("--token", help="existing local WSS token; never printed")
    grinder.add_argument("--token-file", help="read the WSS token from a 0600 file")
    grinder.add_argument("--login-role", help="login locally to obtain a token")
    grinder.add_argument("--password", help="avoid on the command line; prefer the prompt or --password-file")
    grinder.add_argument("--password-file", help="read the login password from a 0600 file")
    grinder.add_argument("--allow-no-password", action="store_true",
                         help="use the documented FW-01 SELECT-only workaround if no password is supplied")
    grinder.add_argument("--insecure", action="store_true")
    grinder.add_argument("--ca-file")
    grinder.add_argument("--dose-index", type=int, default=1)
    grinder.add_argument("--machine-name", default="scale-bridge")
    grinder.add_argument("--group-head", default="0")
    grinder.add_argument("--wss-port", type=int, default=9997)
    grinder.add_argument("--preflight", action="store_true",
                         help="log in and read RequestDriverState, then exit; no AddBrewEvent")
    grinder.add_argument("--apply", action="store_true", help="send AddBrewEvent to the grinder")
    grinder.add_argument("--yes", action="store_true", help="confirm --apply")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.apply and not args.grinder:
        parser.error("--apply requires --grinder")
    if args.preflight and not args.grinder:
        parser.error("--preflight requires --grinder")
    if args.preflight and args.apply:
        parser.error("--preflight cannot be combined with --apply")
    if not args.preflight and bool(args.replay) + bool(args.openscale) + bool(args.ble) != 1:
        parser.error("choose exactly one of --replay, --openscale or --ble")
    try:
        return _run(args)
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OpenScaleProtocolError, OSError) as exc:
        # OSError covers the transport and file paths that used to escape as a
        # traceback; the text is redacted for the same reason every other
        # message is.
        print(f"scale-bridge: {_redact(exc, _token_hint(args))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
