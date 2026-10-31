# ESP32 BLE gateway for the E64 (design + ingest contract)

Status: **the PHP ingest contract is implemented and tested; the ESP32 BLE
central firmware is not yet written or flashed.** The on-device WiFi bridge and
the existing HMI Xenia diagnosis screen are verified on QEMU. Hardware BLE,
WiFi coexistence and the ESP32-to-grinder HTTP path remain unproven.

## Why this exists

The E64 has no BLE central, no BlueZ and no BLE scale client. The ESP32
co-processor is the only always-on radio in the grinder, so it is the natural
place for a BLE-to-local-state gateway. The gateway does **not** patch the STM32
or the vendor HMI:

```text
BLE scale (Decent FFF0/FFF4 or generic 181D/2A9D)
        |
        v
ESP32 custom firmware
  NimBLE central + scale parser + shot detector + state machine
        |
        | HTTP POST /api/v2/ingest  (X-Scale-Token)
        v
E64 lighttpd/PHP
  esp-state.json  (fresh for 5 s)
        |
        +--> /api/v2/mako --> vendor HMI Xenia Diagnosis / GBS
        +--> /scale/      --> phone/laptop WebUI
```

The WiFi daemon (`scalebridged`) and the ESP32 gateway can coexist: a fresh
ESP snapshot wins for `SCALE_BRIDGE_ESP_FRESH_MS` (5 s), then the WiFi state
resumes. This makes source switching safe without restarting PHP.

## Ingest contract (implemented)

`POST /api/v2/ingest` with:

```http
X-Scale-Token: <ingest_token from /db/scale-bridge/config.json>
Content-Type: application/json
```

```json
{
  "source": "esp32-ble",
  "phase": "serving",
  "live_grams": 17.4,
  "live_ms": 123456,
  "flow_g_s": 1.3,
  "active_ms": 4200,
  "last_shot": {
    "mass_g": 18.0,
    "duration_ms": 24600,
    "valid": true,
    "reason": "stable weight",
    "at_ms": 323657392
  },
  "finishing_until_ms": 0,
  "shot_count": 3,
  "reconnects": 1,
  "error": ""
}
```

Rules:

- `phase`: `idle`, `serving` or `finishing`.
- `live_grams`, `flow_g_s`, `active_ms`, `shot_count`, `finishing_until_ms`
  are finite non-negative numbers.
- `last_shot` is `null` or the object above; `at_ms` uses the same 32-bit
  millisecond window as the rest of the on-device code.
- The server sets `updated_ms`; the client clock is not trusted.
- `ingest_token` empty disables the endpoint (HTTP 503). Wrong token is 403.
- Body limit is 16 KiB. The endpoint writes `/run/scale-bridge/esp-state.json`
  atomically and never exposes control actions.

The four Xenia routes are unchanged; the HMI sees the ESP state through
`/api/v2/mako` exactly as it sees the WiFi daemon.

## Implemented in dev

- `build.sh gateway` builds the BLE-central gateway image
  (`build/fwgateway/build/Ng2EspApp.bin`, 1,234,192 bytes; ~20.6 KB IRAM free).
- `src/main/ScaleGateway.{h,cpp}` is a dependency-free parser, shot detector
  and state serializer. `tests/run_scale_gateway_test.sh` compiles and runs its
  host tests.
- `src/main/BleScaleGateway.{h,cpp}` is the NimBLE-central + HTTP transport.
  It is compiled and linked; no BLE/radio test has been run.
- `tools/esp32_gateway_sim.py` simulates the ESP32 HTTP side against the E64
  `/api/v2/ingest` endpoint.
- `tests/run_host_gateway_test.sh` drives the **production**
  `BleScaleGateway` through a virtual scale at the raw NimBLE C boundary:
  advertisement → connect → service/characteristic/CCCD → notifications →
  HTTP ingest, plus connect failure, missing characteristic, missing CCCD,
  withheld notification, malformed frame and link-drop re-scan.

Build and host-test:

```sh
cd tools/rebuild/esp-app/recon
ROOT=$PWD ./build.sh gateway
./tests/run_scale_gateway_test.sh
# from the research repo root: python3 tools/esp_ble_client.py --help
```

Host BLE harness:

```sh
cd tools/rebuild/esp-app/recon
./tests/run_host_gateway_test.sh
```

This adapts the Furble simulator pattern (`sim/BleSim.cpp` +
`lib/testing/nimble/MockNimBLE`) to our raw-C NimBLE gateway: Furble drives
production camera objects over C++ `MockNimBLE`; our harness drives the
production gateway over the same virtual-peer/fault model at the C API
boundary. It found a real CCCD-completion bug: NimBLE reports descriptor
discovery completion with a null descriptor after the CCCD write, which the
gateway had treated as “CCCD missing” and disconnected. The production state
machine now tracks `m_cccdWritten`.

Dev ingest simulation (after installing the bridge on the E64):

```sh
python3 tools/esp32_gateway_sim.py --host 127.0.0.1 --port 28082 \
  --token "$(ssh ... cat /db/scale-bridge/config.json | jq -r .ingest_token)" \
  --synthetic --speed 20 --once
```

Then `GET /api/v2/mako` should show the simulated Output Amount, Flow Rate and
Real Extraction Time. QEMU returned `MA_STATUS=4`,
`REAL_EXTRACTION_TIME_MS=18400`, `PU_SENS_FLOW_METER_VOLUME=18` for an 18 g
simulated shot. The simulator is transport-only; it does not prove BLE.

The vendor HMI rendered the same values in `CmXeniaDiagnosis`:
`Output Amount 18g`, `Flow Rate 0g/s`, `Real Extraction Time 18400ms`,
`Script SCALE` (see `docs/assets/ondevice-xenia-diagnosis-metrics.png`). The
screenshot comes from the QML stub, not a physical panel.

## Firmware plan

Add a feature-gated `ble-gateway` build, preserving the existing UART/SDIO/LED/
watchdog and esp-hosted paths:

1. **NimBLE central**
   - Scan for Decent/Half Decent `FFF0` with notify `FFF4`; fallback to generic
     Weight Scale `181D`/`2A9D`.
   - Connect, subscribe, parse little-endian weight frames; keep the parser in
     a host-testable C++ unit.
   - Reconnect with backoff; never block the UART/network tasks.
2. **Shot detector / state**
   - Reuse the same thresholds as the Python/daemon detector: baseline,
     start threshold, stable window, >15 s validity, cup-removal end.
   - Maintain `phase`, live grams, flow (EMA), active time, last shot and
     `shot_count` in a small state object.
3. **HTTP client**
   - `esp_http_client` POST to `http://<grinder-ip>/api/v2/ingest`.
   - Header `X-Scale-Token`; 1 s timeout; retry with backoff; do not log the
     token or full URLs with query strings.
   - Send at ~5–10 Hz while serving, on every phase change and once on idle.
4. **Configuration**
   - NVS or `/db`-backed config for WiFi SSID/password, grinder host, token,
     scale address/kind, and enable flag.
   - A compile-time/runtime disable path and a documented revert to the stock
     ESP image.
5. **Resource budget**
   - The current BLE-peripheral image leaves ~20 KB IRAM free. A central +
     HTTP client + coexistence may not fit without reducing WiFi buffers,
     disabling unused NimBLE roles, or requiring a PSRAM module. Measure before
     promising a build; if it does not fit, use an external sidecar.
6. **Network path**
   - The ESP32 is the grinder's WiFi NIC (esp-hosted). Confirm it can open an
     HTTP connection to the STM32's LAN IP while esp-hosted is active. If it
     cannot, the fallback is an external BLE sidecar or a host-side parser
     change; do not assume this path works until measured.

## Why not serve Xenia directly from the ESP32?

It is tempting to run an HTTP server on the ESP32 and point the E64 M2M host at
it. The E64 Xenia client hardcodes port 80 and `http://<host>/api/v2/...`, and
the ESP32's address/port ownership in esp-hosted is not established. Posting
to the existing PHP endpoint is smaller, keeps one Xenia implementation, and
avoids a second HTTP server competing with lighttpd.

## Security

- The ingest token is a local shared secret; generate it with the installer or
  set `ingest_token` manually. Do not reuse a cloud or vendor credential.
- The endpoint is LAN-local; do not expose port 80 to the internet.
- The ESP32 config contains the WiFi password and token; protect the flash
  dump and do not commit either.
- No vendor key, official-scale identity or cloud account is used.

## Verification plan

Host (now):

```sh
./tools/run_php_tests.sh          # includes ingest token/state/freshness tests
python3 -m unittest discover -s tests -v
```

QEMU (next):

1. Install the bridge, set `ingest_token`.
2. POST a simulated ESP snapshot; confirm `/api/v2/mako` shows Output Amount,
   Flow Rate, Real Extraction Time and Machine Status.
3. Stop posting; confirm the state falls back to the WiFi daemon after 5 s.
4. Open the vendor HMI **Machine To Machine → XENIA → Diagnosis** and verify
   the fields; the VM security modal may block the screen.

Physical (owner authorization required):

1. Identify the ESP32 module/PCB, dump flash, record eFuse/secure-boot state.
2. Flash the gateway build with a recovery image; verify UART/SDIO/LED/watchdog
   and esp-hosted WiFi first.
3. Connect a Decent/generic BLE scale; verify samples, shot detection, POSTs,
   reconnect, WiFi coexistence and 24 h soak.
4. Pull a real shot; verify HMI Output Amount/Flow/Time and the GBS A/B.
5. Measure heap/IRAM, packet loss and eMMC writes; document the revert.

## Open questions

1. Can the ESP32 open an HTTP connection to the STM32's LAN IP while esp-hosted
   is active?
2. Does the 4 MB ESP32 module have PSRAM, and does the gateway fit IRAM/DRAM?
3. Which BLE scale service/firmware variant is the owner's actual scale?
4. Does the vendor HMI's M2M toggle persist `M2m.CoffeeMachinesConfig` in a way
   the installer can preseed safely?
5. Does GBS consume the Xenia time on hardware, independent of the source?
