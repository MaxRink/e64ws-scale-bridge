# Mahlkönig E64 WS scale bridge

A local, cloudless bridge that turns a third-party espresso scale into the brew
event shape a Mahlkönig E64 WS accepts. It supports the **Half Decent Scale /
OpenScale** over Wi-Fi, the **Decent / Half Decent BLE** service, the standard
BLE Weight Scale service, and deterministic JSONL replay.

> **Status.** Software-verified against the from-SWU QEMU grinder. The
> `AddBrewEvent` path is development-gated on the vendor image; stock-firmware
> options are manual brew time, a local Xenia emulator, or an owner-account
> cloud event. A physical E64 WS was reached read-only on 2026-11-18; the
> always-on reads and the database/log exports were captured, but
> `AddBrewEvent` was **not** sent because the unit is a `custom` build with no
> development tree. See [docs/LIVE_VALIDATION.md](docs/LIVE_VALIDATION.md).

## Quick start

```sh
python3 -m pip install -r requirements.txt

# deterministic replay (no hardware)
python3 tools/scale_bridge.py --replay examples/shot.jsonl --once --json

# Half Decent Scale / OpenScale Wi-Fi
python3 tools/scale_bridge.py --openscale hds.local --once

# Decent / Half Decent BLE, or a generic 0x181D scale
python3 tools/scale_bridge.py --ble AA:BB:CC:DD:EE:FF --ble-kind auto --once

# Send to a dev-unlocked E64 (stock refuses AddBrewEvent).
# Use a token file, or log in with the current Owner password.
python3 tools/scale_bridge.py --openscale hds.local \
  --grinder 192.168.1.50 --token-file /tmp/e64.token --apply --yes
python3 tools/scale_bridge.py --openscale hds.local \
  --grinder 192.168.1.50 --login-role Owner \
  --password-file /tmp/e64-owner.pass --apply --yes
```

`--allow-no-password` is the explicit FW-01 workaround for an owner-controlled
unit with no Owner password. `--apply` changes the grinder and requires `--yes`.
The bridge prints the event by default, before any write, and prints the
grinder's response afterwards. A failed send therefore still records the event
it attempted. A response with `success: false` is a *refusal*: the reason is
printed to stderr and the command exits 3, so a script can tell a refused event
from a delivered one (exit 2 is a transport or configuration error). Tokens and
passwords are never printed -- including inside the grinder's own response
payload, which is redacted before it is printed.

Read-only preflight (no scale source and no `AddBrewEvent`):

```sh
python3 tools/scale_bridge.py --grinder 192.168.1.50 \
  --login-role Owner --password-file /tmp/e64-owner.pass --insecure --preflight
```

## What it does

```text
scale sample -> shot detector -> AddBrewEvent data -> E64 WSS
```

The detector needs a short stable baseline, starts on a rise above
`--start-threshold-g`, ends on a stable window or cup removal, and marks the
shot valid only at `>15 s` (the documented Sync window). The event uses
milliseconds and milligrams. A shot that never settles is closed at
`--max-shot-ms` (default 120 s) as `validity: false` / `reason: "timeout"`
instead of staying active forever. The idle baseline re-seeds when the reading
settles at a new level, so removing the cup after a shot does not disarm the
next one.

## Supported inputs

| Input | Command |
|---|---|
| OpenScale / Half Decent Scale Wi-Fi `ws://<host>/snapshot` | `--openscale HOST` |
| Decent / Half Decent BLE `FFF0`/`FFF4` | `--ble ADDRESS --ble-kind decent` |
| Standard BLE Weight Scale `181D`/`2A9D` | `--ble ADDRESS --ble-kind generic` |
| JSONL replay | `--replay FILE` |

BLE requires the optional `bleak` package. The parsers are unit-tested; the BLE
transport needs a real scale.

## Stock firmware

`AddBrewEvent` is one of the `dev=1` WSS records. On a stock E64 the same
command closes the connection. The practical stock options are:

1. **Manual brew time** — the scale measures the shot; enter the duration on
   the grinder's on-screen brew-time field. This is the only fully stock,
   cloud-free path.
2. **Local Xenia emulator** — `tools/xenia_emulator.py` serves the four local
   M2M routes. Whether the grinder's GBS consumes the synthetic extraction time
   needs a physical test.
3. **Owner-account cloud event** — post a brew event for the owner's grinder
   under the owner's own cloud account. This needs a captured API contract and
   must not clone an official scale identity.

“Show the custom scale as an official scale” is **not a BLE path**: the E64 has
no BLE scale client and the official app has no Bluetooth permission. The
official scale is Wi-Fi/cloud and serial-bound.

See [docs/ESP32_SCALE.md](docs/ESP32_SCALE.md), [docs/STOCK_OPTIONS.md](docs/STOCK_OPTIONS.md), [docs/XENIA_GUIDE.md](docs/XENIA_GUIDE.md), [docs/OWNER_CLOUD_EVENT.md](docs/OWNER_CLOUD_EVENT.md) and [docs/GUIDE.md](docs/GUIDE.md).

## On-device bridge (rooted E64)

The WiFi bridge can run **on the grinder itself** on a rooted unit. A
dependency-free daemon reads `ws://<scale>/snapshot`, detects a shot, and
writes live state. PHP serves the five Xenia routes from the grinder's own web
root, so the E64 M2M client can point at `127.0.0.1` or the grinder's LAN
address. The vendor HMI is not patched; the stock M2M screen is the display.

```sh
./ondevice/install.sh --dry-run
./ondevice/install.sh          # as root, after unlockrfs/unlockapp
vi /db/scale-bridge/config.json
/etc/init.d/S95scalebridge restart
# open http://<grinder>/scale/
```

This path is rooted-stock, not stock no-root, and it does **not** add BLE to
the E64. The installer adds a narrow lighttpd exception for `/api/v2/` because
the E64 Xenia client is HTTP-only while the vendor web server redirects HTTP to
HTTPS. That exception is plain HTTP, so the five local Xenia routes — including
the `POST /api/v2/brewratio` write and the `X-Scale-Token` header on
`/api/v2/ingest` — cross the LAN in cleartext. That is acceptable only on a
trusted, isolated LAN: do not expose port 80 to the internet or to an untrusted
network. The built-in HMI already has a Xenia Diagnosis screen; the bridge feeds
its Output Amount (live grams), Flow Rate, Real Extraction Time and Machine
Status fields without patching QML. GBS consumption of the Xenia extraction
time is still the physical A/B gate.

A future ESP32 BLE-central firmware can feed the same HMI through the
token-protected `/api/v2/ingest` endpoint; see
[docs/ESP32_BLE_GATEWAY.md](docs/ESP32_BLE_GATEWAY.md). See [docs/ON_DEVICE_INSTALL.md](docs/ON_DEVICE_INSTALL.md) and the
feasibility report [ON_DEVICE_SCALE_BRIDGE.md](https://github.com/MaxRink/mahlkoenig-e64ws-local-research/blob/main/docs/ON_DEVICE_SCALE_BRIDGE.md).

The companion Decaid plugin (read-only grinder telemetry) is
[MaxRink/decaid-e64ws-plugin](https://github.com/MaxRink/decaid-e64ws-plugin);
the assisted dial-in plugin is
[MaxRink/decaid-assisted-dialin-plugin](https://github.com/MaxRink/decaid-assisted-dialin-plugin).

## Tests

```sh
python3 -m unittest discover -s tests -v
```

164 Python tests cover the Decent/generic parsers, shot detection, event units,
the WSS client's timeout/redaction paths, the Xenia emulator routes, the
on-device daemon, the lighttpd snapshot/restore patch and these docs. Fifteen
of them exercise the on-device Xenia endpoints, WebUI page, ESP32 ingest and
state freshness; they skip as a group when `php` is not on `PATH`. A further
set drives `install.sh`, `uninstall.sh` and the init script for real inside a
container and skips when Docker is unavailable. `./tools/run_php_tests.sh` runs
the fifteen against PHP 8.4 in Docker and
`python3 -m unittest tests.test_ondevice_shell` runs the shell suite. The bridge was also exercised against the QEMU E64: an
18 g / 24.6 s replay produced `brewTimeMs=24600`, `brewWeightMg=18000`,
`validity=true`, and `AddBrewEvent` returned `{"success": true}` in development
mode.

## Scope and limitations

- No cloud access, vendor keys or credentials are included.
- No physical write-path validation; BLE needs a real scale. The 2026-11-18
  physical check was transport/login-gate plus a read-only state capture.
- Stock grinder control is gated; see the stock options.
- The Xenia emulator is a protocol fixture, not a coffee-machine replacement.
- Known review gaps (token refresh, TLS pinning, WSS reconnect, tare, the
  dropout event path) are listed with their status in
  [docs/REVIEW_FOLLOWUPS.md](docs/REVIEW_FOLLOWUPS.md); they are not silently
  dropped.

License: GPL-3.0-or-later. See [LICENSE](LICENSE).
