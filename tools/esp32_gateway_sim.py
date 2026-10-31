#!/usr/bin/env python3
"""Simulate the ESP32 BLE gateway's HTTP side for dev/QEMU tests.

It runs the same shot detector as the host bridge, builds the same state
payload the ESP32 firmware sends, and POSTs it to the E64 `/api/v2/ingest`
endpoint.  It does not use BLE; use it to validate the ingest/HMI path before
flashing hardware.

    python3 tools/esp32_gateway_sim.py --host 127.0.0.1 --port 28082 \
        --token "$TOKEN" --synthetic --speed 20 --once

The token is read from --token or SCALE_BRIDGE_INGEST_TOKEN and is never
printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

try:
    from tools.scale_bridge import ScaleSample, ShotDetector, iter_replay
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from scale_bridge import ScaleSample, ShotDetector, iter_replay


def synthetic_samples() -> Iterable[ScaleSample]:
    ms = 0
    for _ in range(8):
        yield ScaleSample(0.0, ms, source="esp32-sim")
        ms += 100
    # 18 g over 18 s -> a valid >15 s shot, then a stable hold.
    for step in range(1, 181):
        yield ScaleSample(step * 0.1, ms, source="esp32-sim")
        ms += 100
    for _ in range(20):
        yield ScaleSample(18.0, ms, source="esp32-sim")
        ms += 100
    for _ in range(10):
        yield ScaleSample(0.0, ms, source="esp32-sim")
        ms += 100


def state_payload(
    detector: ShotDetector,
    grams: float,
    ms: int,
    *,
    flow: float,
    last_shot: dict[str, Any] | None,
    shot_count: int,
    finishing_until: int,
) -> dict[str, Any]:
    if detector.active:
        phase = "serving"
        active_ms = int(ms - detector.start_ms)
    elif finishing_until > 0 and ms < finishing_until:
        phase = "finishing"
        active_ms = 0
    else:
        phase = "idle"
        active_ms = 0
    return {
        "source": "esp32-sim",
        "phase": phase,
        "live_grams": round(grams, 3),
        "live_ms": int(ms),
        "flow_g_s": round(flow, 3),
        "active_ms": active_ms,
        "finishing_until_ms": finishing_until,
        "shot_count": shot_count,
        "reconnects": 0,
        "error": "",
        "last_shot": last_shot,
    }


def post(url: str, token: str, payload: dict[str, Any], timeout: float) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Scale-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"ingest HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"ingest HTTP {exc.code}: {detail}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=28082)
    parser.add_argument("--path", default="/api/v2/ingest")
    parser.add_argument("--token", default=os.environ.get("SCALE_BRIDGE_INGEST_TOKEN", ""))
    parser.add_argument("--synthetic", action="store_true", help="generate a valid 18 g shot")
    parser.add_argument("--replay", help="JSONL samples; default is synthetic")
    parser.add_argument("--speed", type=float, default=1.0, help="playback speed")
    parser.add_argument("--post-every", type=int, default=2, help="post every N samples")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="exit after the first shot + finishing window")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.token:
        raise SystemExit("provide --token or SCALE_BRIDGE_INGEST_TOKEN")
    url = f"http://{args.host}:{args.port}{args.path}"
    samples = iter_replay(args.replay) if args.replay else synthetic_samples()
    detector = ShotDetector()
    flow = 0.0
    last_g: float | None = None
    last_ms: int | None = None
    last_shot: dict[str, Any] | None = None
    shot_count = 0
    finishing_until = 0
    posted = 0
    try:
        for sample in samples:
            grams = float(sample.grams)
            ms = int(round(sample.ms))
            if last_g is not None and last_ms is not None and ms > last_ms:
                instant = max(0.0, (grams - last_g) / ((ms - last_ms) / 1000.0))
                flow = instant if flow == 0 else 0.7 * flow + 0.3 * instant
            last_g, last_ms = grams, ms
            shot = detector.add(ScaleSample(grams, sample.ms, source="esp32-sim"))
            if shot is not None:
                shot_count += 1
                last_shot = {
                    "mass_g": round(shot.mass_g, 3),
                    "duration_ms": round(shot.duration_ms, 1),
                    "valid": shot.valid,
                    "reason": shot.reason,
                    "at_ms": ms,
                }
                finishing_until = ms + 3000
                flow = 0.0
                print(f"shot: {shot.mass_g:.1f} g in {shot.duration_ms / 1000.0:.1f} s", flush=True)
            if args.once and last_shot is not None and ms >= finishing_until:
                # Keep the last POST a finishing state; do not overwrite it
                # with an idle sample at the window boundary.
                break
            if posted % max(1, args.post_every) == 0:
                payload = state_payload(
                    detector, grams, ms, flow=flow, last_shot=last_shot,
                    shot_count=shot_count, finishing_until=finishing_until,
                )
                post(url, args.token, payload, args.timeout)
            posted += 1
            if args.speed > 0:
                time.sleep(0.05 / args.speed)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"esp32-gateway-sim: {exc}", file=sys.stderr)
        return 1
    print(f"posted {posted} samples, shots={shot_count}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
