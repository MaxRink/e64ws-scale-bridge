# Xenia emulator guide

`tools/xenia_emulator.py` is a local, stdlib-only HTTP fixture that imitates the
four Xenia machine-to-machine (M2M) routes a stock E64 WS polls. It is intended
for protocol work, replay-driven tests, and a controlled physical experiment. It
is **not** a coffee-machine emulator, a cloud replacement, or proof that the
grinder's GBS logic consumes the synthetic extraction time.

The fixture and its tests make no outbound network calls. Both listeners bind
to loopback by default. They use no cloud account, vendor service, or
official-scale identity, and no grinder is contacted by the test suite.

## 1. Requirements and setup

- Python 3.10 or newer.
- The repository checkout; the emulator imports `tools/scale_bridge.py` for the
  same `ShotDetector` and JSONL replay reader used by the bridge.
- No `pip install` is needed for the emulator itself. `requirements.txt` is for
  the E64 WSS, OpenScale Wi-Fi, and optional BLE paths.

Run the test suite from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

Start the fixture with the safe defaults:

```sh
python3 tools/xenia_emulator.py
```

It prints the two loopback URLs:

```text
Xenia emulator: http://127.0.0.1:8080/api/v2/mako
control:        http://127.0.0.1:8081/control/state
```

The E64-facing listener defaults to `127.0.0.1:8080`; the control listener
defaults to `127.0.0.1:8081`. The control listener is always loopback unless
`--control-host` is changed explicitly, even when the E64-facing listener is
exposed. The E64-facing listener serves only the four E64 routes; every
`/control/*` request there returns `404`, so exposing the E64 port does not
expose the control API.

### Binding for a real grinder

The recovered Xenia route is plain HTTP on port 80. The emulator defaults to
8080 so an unprivileged local run does not need port 80. For a physical test,
choose one of these operator-managed options:

```sh
# Bind the E64-facing side to the LAN on 8080, keep control on loopback.
python3 tools/xenia_emulator.py --host 0.0.0.0 --port 8080 --verbose

# Or bind port 80 directly (may require elevated privileges on your OS).
sudo python3 tools/xenia_emulator.py --host 0.0.0.0 --port 80 --verbose
```

If the E64's M2M screen accepts only a host and assumes port 80, use `--port 80`
or a local port forward from 80 to 8080. Port forwarding is outside this tool.

A non-loopback E64-facing bind prints a warning. There is no authentication or
TLS. Use it only on a trusted local network, and keep `--control-host` on
loopback unless you have a specific reason not to.

## 2. E64-facing routes and exact shapes

These four routes keep the original JSON shapes. No extra keys are added.

| Method | Path | Success | Notes |
|---|---|---|---|
| `GET` | `/api/v2/mako` | `200` | Machine state, extraction state, last measured time, sync pair, fixed sensor fields |
| `GET` | `/api/v2/machine` | `200` | Identity and firmware fields |
| `POST` | `/api/v2/brewratio` | `200` | Stores and echoes `SYNC_BEVERAGE_WEIGHT` / `SYNC_FILTER` |
| `POST` | `/api/v2/scripts/execute` | `200` | Enters serving/active; echoes the request's `ID` |

### `GET /api/v2/mako`

Keys, exactly:

```text
MA_STATUS                       int   1 idle, 3 serving/active, 4 finishing
MA_EXTRACTION_STATUS            int   0 idle, 1 serving/active, 2 finishing
REAL_EXTRACTION_TIME_MS         int   0 until a shot completes; last shot retained while idle
PU_SENS_FLOW_METER_VOLUME       float fixed 0.0 in this fixture
BG_SET_TEMP                     int   fixed 93
BG_SENS_TEMP                    int   fixed 92
TANK_LEVEL                      int   fixed 1
script                          str   empty in this fixture
TIMESTAMP                       int   fixture wall-clock milliseconds
MA_SYNC_BEVERAGE_WEIGHT         int   last value echoed by /api/v2/brewratio
MA_SYNC_FILTER                  int   last value echoed by /api/v2/brewratio
```

The first poll after a completed shot returns the finishing state (`MA_STATUS`
4, `MA_EXTRACTION_STATUS` 2) and the measured `REAL_EXTRACTION_TIME_MS`. After
the configured number of polls (`--finishing-polls`, default 1) the same route
returns idle (`1`/`0`). The measured time remains readable while idle until the
next `start_brew`.

### `GET /api/v2/machine`

```text
MA_TYPE             "XENIA"
ma_type             "xenia"
MA_SN               "VG-XENIA-0001"
FW_VERSION_MAJOR    3
FW_VERSION_MINOR    87
ESP_FW_MAJOR        3
ESP_FW_MINOR        87
TIMESTAMP           int
```

### `POST /api/v2/brewratio`

Request body:

```json
{"SYNC_BEVERAGE_WEIGHT": 18, "SYNC_FILTER": 1}
```

Response, exactly these two keys:

```json
{"MA_SYNC_BEVERAGE_WEIGHT":18,"MA_SYNC_FILTER":1}
```

The pair is stored and returned in subsequent `/api/v2/mako` documents. Missing
fields default to `0`; values that cannot be converted to an integer return
`400`.

### `POST /api/v2/scripts/execute`

Request body may contain an `ID`; the fixture echoes it and nothing else:

```json
{"ok":true,"ID":9}
```

The route enters serving/active (`MA_STATUS` 3, `MA_EXTRACTION_STATUS` 1). It
does not start a real motor, pump, or grinder.

## 3. Control API

The control API is local-only by default and is served only on the control
listener. The E64-facing listener returns `404` for every `/control/*` path. It
can inject a shot, feed scale samples, abort a shot, and reset state.

| Method | Path | Body | Response |
|---|---|---|---|
| `GET` | `/control/state` | none | diagnostic snapshot |
| `POST` | `/control/start` | optional `{}` | snapshot after entering serving |
| `POST` | `/control/shot` | `{"duration_ms": 25400, "mass_g": 18.2}` | snapshot in finishing |
| `POST` | `/control/sample` | `{"grams": 18.0, "ms": 25000}` | `{"shot": <shot or null>, "state": <snapshot>}` |
| `POST` | `/control/idle` | optional `{}` | snapshot in idle; resets the detector |
| `POST` | `/control/reset` | optional `{}` | initial snapshot; resets state and detector |

`duration_ms`, `mass_g`, `grams`, and `ms` must be finite JSON numbers.
`duration_ms`, `mass_g`, and `ms` must be non-negative. Missing required fields,
wrong types, non-finite values, invalid JSON, non-object JSON, and non-monotonic
sample timestamps return `400` with `{"error": "..."}`. Unknown paths return
`404` with `{"error": "unknown path", "path": "..."}` for both `GET` and `POST`.

The snapshot has these keys:

```text
phase                         "idle" | "serving" | "finishing"
ma_status                     1 | 3 | 4
extraction_status             0 | 1 | 2
extraction_time_ms            int, last completed shot
mass_g                        float, last completed shot
flow_ml                       float, 0.0
sync_weight_g                 int
sync_filter                   int
timestamp                     int
finishing_polls_remaining     int
last_shot                     null or {"duration_ms", "mass_g", "reason"}
```

### Exact control examples

```sh
# Inspect state.
curl -s http://127.0.0.1:8081/control/state

# Enter serving/active without starting a real machine.
curl -s -X POST http://127.0.0.1:8081/control/start \
  -H 'content-type: application/json' -d '{}'

# Complete a shot: 25.4 s, 18.2 g.
curl -s -X POST http://127.0.0.1:8081/control/shot \
  -H 'content-type: application/json' \
  -d '{"duration_ms":25400,"mass_g":18.2}'

# The first Mako poll shows finishing and the exact injected time.
curl -s http://127.0.0.1:8080/api/v2/mako

# The next poll returns idle; the last time is retained.
curl -s http://127.0.0.1:8080/api/v2/mako

# Echo the Sync pair.
curl -s -X POST http://127.0.0.1:8080/api/v2/brewratio \
  -H 'content-type: application/json' \
  -d '{"SYNC_BEVERAGE_WEIGHT":18,"SYNC_FILTER":1}'

# Abort to idle and discard a partially detected shot.
curl -s -X POST http://127.0.0.1:8081/control/idle \
  -H 'content-type: application/json' -d '{}'

# Return to the initial state (also clears the Sync pair and last shot).
curl -s -X POST http://127.0.0.1:8081/control/reset \
  -H 'content-type: application/json' -d '{}'
```

## 4. Driving the fixture from a scale replay

`--replay FILE` reads JSONL, one object per line:

```json
{"grams": 0.0, "ms": 0}
{"grams": 0.0, "ms": 100}
{"grams": 18.0, "ms": 25000}
```

Blank lines and lines starting with `#` are ignored. `ms` values must strictly
increase. The file is fed through the same `ShotDetector` used by
`tools/scale_bridge.py`; the detector's default policy is:

| Setting | Default |
|---|---:|
| baseline samples | 5 |
| shot start threshold | 0.5 g |
| stability tolerance | 0.15 g |
| stable finish window | 1000 ms |
| minimum detector duration | 3000 ms |
| Sync validity threshold | 15000 ms |
| cup-removal drop | 2.0 g |

The emulator only uses the detector's `duration_ms` and `mass_g` when a shot
completes. The detector's `valid` flag is not sent on the four E64 routes; the
E64 receives the measured time and the finishing state.

```sh
# Real-time replay: approximately one fixture second per replay second.
python3 tools/xenia_emulator.py --replay examples/shot.jsonl --verbose

# Fast deterministic replay: feed all samples immediately.
python3 tools/xenia_emulator.py --replay examples/shot.jsonl --replay-speed 0 --verbose
```

`--replay-speed 1.0` is the default; `2.0` runs twice as fast, and `0` feeds
immediately. Replay starts in a daemon thread after the listeners are up. If the
replay completes before the grinder polls, the finishing state still waits for
`--finishing-polls` Mako polls, but the timing will not match a live shot. For a
controlled physical experiment, use `/control/shot` at the moment you want the
fixture to report a completed shot.

The example replay produces an 18.0 g, 24.0 s shot with the current detector
defaults. A compact 3 s synthetic shot is also used in the tests; it is a valid
state-machine input even though it is below the 15 s Sync policy threshold.

### OpenScale / Half Decent Scale input

The emulator does not open a WebSocket or Bluetooth connection itself. It
exposes one clean adapter contract: `POST /control/sample` with the scale's
`grams` and monotonically increasing `ms`. Any owner-supplied HDS/OpenScale
reader can forward frames to it. This standard-library example is enough to
adapt a reader:

```python
import json
import urllib.request


def send(grams: float, ms: float) -> dict:
    body = json.dumps({"grams": grams, "ms": ms}).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:8081/control/sample",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


# Example: five idle samples, a rise, then a stable shot. Use the real
# timestamps from your HDS/OpenScale stream and never reuse or decrease ms.
for grams, ms in [
    (0.0, 0),
    (0.0, 500),
    (0.0, 1000),
    (0.0, 1500),
    (0.0, 2000),
    (2.0, 2500),
    (10.0, 3000),
    (18.0, 4000),
    (18.0, 5000),
    (18.0, 6000),
]:
    print(send(grams, ms))
```

When the detector reports a completed shot, the response contains a non-null
`shot` and the fixture has entered finishing. `/control/idle` resets the
detector for the next attempt. This path is optional; `--replay` is the
deterministic path used by the tests.

## 5. State machine

```text
          scripts/execute or /control/start
idle  ------------------------------------->  serving/active
(1/0)                                          (3/1)
  ^                                               |
  |                                               | shot completion:
  |                                               | /control/shot
  |                                               | /control/sample
  |                                               | --replay
  |                                               v
  +------------------------------------------  finishing
       finishing poll(s), default 1              (4/2)
                                                REAL_EXTRACTION_TIME_MS set
```

- A direct `/control/shot` from idle passes through serving before finishing,
  so the observable sequence is always idle -> serving -> finishing -> idle.
- `--finishing-polls N` makes the finishing state visible to `N` Mako polls
  before returning to idle. The default is 1.
- `REAL_EXTRACTION_TIME_MS` is retained while idle after a shot. The next
  `scripts/execute` or `/control/start` resets it to 0.
- `/control/idle` aborts to idle and discards a partially detected shot;
  `/control/reset` also clears the Sync pair and the last-shot diagnostic.

## 6. Logging

`--verbose` logs one access line per request to stderr:

```text
xenia: 127.0.0.1 GET /api/v2/mako 200 -
xenia: 127.0.0.1 POST /api/v2/brewratio 200 -
```

Access logs contain the client address, method, path without the query string,
status, and response size. Request headers and bodies are never logged. A token
placed in an `Authorization` header, a JSON body, or a query string therefore
does not appear in the access log. Protocol/parse errors are logged as a generic
`protocol error` without echoing the raw request line.

Logging is off by default. `--verbose` is for a trusted local run; do not put
secrets in control requests regardless.

## 7. Physical-test acceptance

This repository has **not** been tested against a physical E64 WS. The following
is the acceptance procedure that would prove the stock grinder's GBS logic
consumes `REAL_EXTRACTION_TIME_MS`, rather than merely receiving it.

### A. Prove the E64 polls the fixture

1. Start the fixture on a host reachable from the grinder, with verbose logging:
   `python3 tools/xenia_emulator.py --host 0.0.0.0 --port 80 --verbose`
   (or 8080 plus an operator-managed forward from 80).
2. Enter that host in the E64's Machine-to-Machine screen. Keep the grinder on
   the same trusted LAN.
3. Confirm the log shows repeated `GET /api/v2/mako` requests from the E64's
   address, and at least one `GET /api/v2/machine`.

Acceptance A: the grinder is actually polling this fixture. No GBS conclusion
is possible without this.

### B. Prove the fixture emits the injected value

1. With the fixture idle, inject a distinctive value:
   `curl -s -X POST http://127.0.0.1:8081/control/shot -H 'content-type: application/json' -d '{"duration_ms":25400,"mass_g":18.2}'`
2. Confirm `/control/state` reports `phase: "finishing"` and
   `extraction_time_ms: 25400`.
3. Confirm the next `/api/v2/mako` (or the verbose request log plus a
   `--finishing-polls` setting that leaves a poll for inspection) returns
   `MA_STATUS: 4`, `MA_EXTRACTION_STATUS: 2`, and
   `REAL_EXTRACTION_TIME_MS: 25400`.

Acceptance B: the fixture side is correct. This does **not** prove GBS used it.

### C. A/B test that isolates GBS consumption

Use the same beans, dose, basket, grind setting, and machine warm-up for every
run. Change only the injected extraction time. Alternate the values to reduce
drift.

1. **Short control:** while the E64 is in its normal brew/shot cycle and the
   fixture is being polled, inject `duration_ms=15000` with the same mass for
   every run (for example `mass_g=18.2`). Record the grinder's post-shot GBS
   result: the next-grind recommendation, dial position, or displayed
   adjustment, whichever the firmware exposes.
2. **Long control:** reset the fixture
   (`POST /control/reset`), then inject `duration_ms=45000` with the same mass
   and the same physical procedure. Record the same GBS result.
3. Repeat each arm at least three times, alternating short/long, and keep the
   physical dose and shot preparation identical.
4. **No-injection control:** run the same procedure with the fixture idle and no
   synthetic shot. The GBS output should not show the short/long difference.

What would count as proof:

- The E64 polled the fixture while the finishing state was available
  (acceptance A + the log timing).
- Changing only `REAL_EXTRACTION_TIME_MS` produces a reproducible,
  dose-controlled difference in the next GBS output, in the direction the
  vendor's GBS policy implies.
- The no-injection control does not produce that difference.
- The effect reverses when the injection is removed.

A single dial movement, a change explained by dose/time, or an effect seen
without the E64 polling the finishing state is **not** proof. If the A/B result
is indistinguishable, the honest conclusion is that GBS did not consume the
synthetic value in that configuration; it may use other Mako fields, the cloud,
a specific brew mode, or a vendor-side event instead.

Record the evidence: verbose access log, `/control/state` before and after each
injection, the exact curl command, grinder screenshots, and timestamps. Do not
contact the vendor cloud or a vendor service as part of this test.

## 8. Limitations

- **Synthetic fixture, not a machine.** Temperatures, tank level, flow volume,
  script, and sensor values are fixed or trivial. It does not model a boiler,
  pump, grinder, or real extraction curve.
- **No physical test in this repository.** The GBS effect of
  `REAL_EXTRACTION_TIME_MS` is unproven. Section 7 defines what would prove it;
  it has not been run here.
- **Not a Xenia replacement.** It implements only the four documented E64-facing
  routes plus the local control API. It does not implement the vendor cloud,
  firmware update, account flow, script language, or official-scale identity.
- **No authentication or TLS.** Both listeners are plain HTTP. Loopback is the
  default; a non-loopback bind exposes an unauthenticated control API. Use only
  a trusted LAN and keep the control host on loopback when possible.
- **Port 80 is operator-managed.** The E64 path assumes port 80; the fixture
  defaults to 8080. Binding 80 may need privileges, and forwarding is outside
  this tool.
- **Poll-driven finishing.** The finishing state is consumed by `GET
  /api/v2/mako` polls. Multiple clients share the counter. `--finishing-polls`
  changes visibility, not the underlying GBS behavior; too many finishing polls
  may not match a real single event.
- **Replay is wall-clock paced, not grinder-synchronized.** `--replay-speed 0`
  is immediate; the default 1.0 follows the file's `ms` deltas. If the replay
  finishes before the grinder is ready, the timing is not a live-shot analogue.
- **Detector policy is not a wire rule.** The 15 s validity threshold is a
  documented Sync policy choice; the fixture sends measured duration and mass,
  not the detector's `valid` flag.
- **No built-in HDS/OpenScale/BLE client.** `/control/sample` is the adapter
  contract; the owner supplies the reader. `--replay` and the control endpoints
  are the tested paths.
- **No persistence.** State is in memory. Restarting the process clears the
  Sync pair, last shot, and extraction time.
- **Tests are loopback-only.** The suite uses ephemeral loopback ports and
  synthetic replay; it makes no external network connections.
