# Mahlkönig E64 WS scale bridge

A local, cloudless bridge that turns a third-party espresso scale into the brew
event shape a Mahlkönig E64 WS accepts. It supports the **Half Decent Scale /
OpenScale** over Wi-Fi, the **Decent / Half Decent BLE** service, the standard
BLE Weight Scale service, and deterministic JSONL replay.

> **Status.** Software-verified against the from-SWU QEMU grinder. The
> `AddBrewEvent` path is development-gated on the vendor image; stock-firmware
> options are manual brew time, a local Xenia emulator, or an owner-account
> cloud event. No physical grinder was used for this repository.

## Quick start

```sh
python3 -m pip install -r requirements.txt

# deterministic replay (no hardware)
python3 tools/scale_bridge.py --replay examples/shot.jsonl --once --json

# Half Decent Scale / OpenScale Wi-Fi
python3 tools/scale_bridge.py --openscale hds.local --once

# Decent / Half Decent BLE, or a generic 0x181D scale
python3 tools/scale_bridge.py --ble AA:BB:CC:DD:EE:FF --ble-kind auto --once

# Send to a dev-unlocked E64 (stock refuses AddBrewEvent)
python3 tools/scale_bridge.py --openscale hds.local \
  --grinder 192.168.1.50 --login-role Owner --apply --yes
```

`--apply` changes the grinder and requires `--yes`. The bridge prints the event
by default. Tokens are never printed.

## What it does

```text
scale sample -> shot detector -> AddBrewEvent data -> E64 WSS
```

The detector needs a short stable baseline, starts on a rise above
`--start-threshold-g`, ends on a stable window or cup removal, and marks the
shot valid only at `>15 s` (the documented Sync window). The event uses
milliseconds and milligrams.

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
writes live state. PHP serves the four Xenia routes from the grinder's own web
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
the E64. GBS consumption of the Xenia extraction time is still the physical
A/B gate. See [docs/ON_DEVICE_INSTALL.md](docs/ON_DEVICE_INSTALL.md) and the
feasibility report [ON_DEVICE_SCALE_BRIDGE.md](https://github.com/MaxRink/mahlkoenig-e64ws-local-research/blob/main/docs/ON_DEVICE_SCALE_BRIDGE.md).

The companion Decaid plugin (read-only grinder telemetry) is
[MaxRink/decaid-e64ws-plugin](https://github.com/MaxRink/decaid-e64ws-plugin);
the assisted dial-in plugin is
[MaxRink/decaid-assisted-dialin-plugin](https://github.com/MaxRink/decaid-assisted-dialin-plugin).

## Tests

```sh
python3 -m unittest discover -s tests -v
```

47 Python tests cover the Decent/generic parsers, shot detection, event units,
the Xenia emulator routes and the on-device daemon. Seven PHP 8.4 tests cover
the on-device Xenia endpoints and WebUI page (`./tools/run_php_tests.sh`). The
bridge was also exercised against the QEMU E64: an
18 g / 24.6 s replay produced `brewTimeMs=24600`, `brewWeightMg=18000`,
`validity=true`, and `AddBrewEvent` returned `{"success": true}` in development
mode.

## Scope and limitations

- No cloud access, vendor keys or credentials are included.
- No physical hardware validation; BLE needs a real scale.
- Stock grinder control is gated; see the stock options.
- The Xenia emulator is a protocol fixture, not a coffee-machine replacement.

License: GPL-3.0-or-later. See [LICENSE](LICENSE).
