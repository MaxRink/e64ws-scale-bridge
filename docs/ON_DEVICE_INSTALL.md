# On-device scale bridge (rooted E64 WS)

Status: **implemented and host/PHP-tested; not yet run on a physical E64.**
This is the WiFi/rooted path from
[`ON_DEVICE_SCALE_BRIDGE.md`](ON_DEVICE_SCALE_BRIDGE.md): a small daemon reads
an OpenScale/HDS `ws://<scale>/snapshot` stream, detects a shot, and writes an
atomic state file. PHP serves the four Xenia routes on the grinder's own port
80, so the E64 M2M client can point at `127.0.0.1` or the grinder's LAN address.
The vendor HMI is **not** patched; the stock M2M screen is the display.

This does **not** add BLE support to the E64. The E64 has no BLE central,
BlueZ, or BLE scale client. BLE scales still need an external sidecar.

## Architecture

```text
OpenScale/HDS --ws /snapshot--> scalebridged (www-data)
                                    |
                                    +--> /run/scale-bridge/state.json
                                    +--> /db/scale-bridge/shots.jsonl
E64 CmXeniaDriver --HTTP :80--> lighttpd/PHP
                                  /api/v2/mako
                                  /api/v2/machine
                                  /api/v2/brewratio
                                  /api/v2/scripts/execute
                                    |
                              GbsService (stock)
```

The E64's own web UI gets `/scale/`, a read-only status page behind the vendor
session. No WSS, no dev mode and no REST login are required for this path.

## Requirements

- Rooted E64 (stock root is enough; dev mode is not required).
- Writable rootfs/app slot, or the vendor `unlockrfs`/`unlockapp` helpers.
- `/storage` and `/db` mounted (normal).
- A WiFi scale exposing `ws://<host>/snapshot` (OpenScale / Half Decent Scale).
- The grinder's Python 3.10 and lighttpd/PHP 8.4, both already present.

## Install

```sh
# 1. Preview
./ondevice/install.sh --dry-run

# 2. On the grinder, as root, after unlocking the app/rootfs slots
./ondevice/install.sh

# 3. Set the scale address
vi /db/scale-bridge/config.json
/etc/init.d/S95scalebridge restart

# 4. Open the vendor UI
#    http://<grinder>/scale/
```

Default config:

```json
{
  "host": "hds.local",
  "port": 80,
  "path": "/snapshot",
  "timeout": 10,
  "finishing_ms": 3000
}
```

`finishing_ms` is how long `/api/v2/mako` keeps returning
`MA_STATUS=4`/`REAL_EXTRACTION_TIME_MS` after a shot. Tune it to the E64 poll
interval if GBS misses the finishing state.

## What gets installed

| Path | Purpose | Survives signed update? |
|---|---|---|
| `/storage/scale-bridge/` | daemon + copied detector/parser modules | Usually (A/B); `upload_swu.php?prepare` wipes `/storage` |
| `/db/scale-bridge/config.json` | scale address and tuning | Yes |
| `/db/scale-bridge/shots.jsonl` | one JSON line per completed shot | Yes |
| `/run/scale-bridge/state.json` | live state for PHP/WebUI | No (tmpfs) |
| `/usr/local/www/api/v2/` | Xenia routes | No; re-run installer |
| `/usr/local/www/scale/` | WebUI status page | No; re-run installer |
| `/etc/init.d/S95scalebridge` | start/stop hook | No; re-run installer |

The installer refuses to overwrite an existing `/usr/local/www/api/v2` or init
script unless it owns it (marker) or `--force` is used. It never remounts,
edits lighttpd, patches the HMI, or touches the ESP32.

## Endpoints

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/v2/mako` | Xenia Mako shape; idle/serving/finishing from state |
| GET | `/api/v2/machine` | Xenia machine identity |
| POST | `/api/v2/brewratio` | validate + persist `SYNC_BEVERAGE_WEIGHT`/`SYNC_FILTER`, echo pair |
| POST | `/api/v2/scripts/execute` | record brew start, echo `{"ok":true,"ID":...}` |

The first two are unauthenticated by design because the E64 M2M client does not
log in. They expose only shot state, not grinder actions. Do not expose port 80
to the internet.

## State schema

```json
{
  "schema": 1,
  "source": "ws://hds.local:80/snapshot",
  "connected": true,
  "phase": "idle|serving|finishing",
  "ma_status": 1,
  "extraction_status": 0,
  "live_grams": 17.4,
  "live_ms": 12345,
  "last_shot": {
    "duration_ms": 24600,
    "mass_g": 18.0,
    "valid": true,
    "reason": "stable",
    "at_ms": 1760000000000
  },
  "finishing_until_ms": 0,
  "shot_count": 1,
  "reconnects": 1,
  "error": "",
  "updated_ms": 1760000000000
}
```

The daemon writes atomically (`state.json.tmp` + rename) and throttles live
updates to 10 Hz. Shot history is append-only JSONL.

## Security

- The daemon starts as root, reads the config, then drops to `www-data` before
  opening the scale connection or writing state.
- `/run/scale-bridge` and `/db/scale-bridge` are owned by `www-data`.
- The WebUI page uses the vendor `sessionManager.php`; it is not a new auth
  system. It is read-only.
- The Xenia endpoints are local-only by intent. If the grinder is on an
  untrusted LAN, restrict port 80 or put the unit behind a trusted network.
- No cloud calls, tokens, vendor keys or official-scale identity are used.

## Verification

Off-device:

```sh
# daemon + existing bridge tests (47 tests, 1 skip when PHP is absent)
python3 -m unittest discover -s tests -v

# PHP 8.4 endpoints and page, in Docker
./tools/run_php_tests.sh
```

The daemon test uses a fake RFC 6455 scale and a real shot sequence: five idle
samples, a rise, a stable 18 g window. It asserts state, phase, finishing,
history and non-monotonic clock handling. The PHP tests cover all four routes,
validation, finishing expiry and the WebUI page.

On a QEMU/virtual E64 (next):

1. Run `install.sh --dry-run` and then install on a disposable snapshot.
2. Verify the page loads after vendor login and `/scale/` does not create a
   token storm.
3. Run a local fake scale on the VM host; point `config.json` at it.
4. Poll `/api/v2/mako` from the VM and verify idle → serving → finishing → idle.
5. Confirm vendor pages, HMI, WSS and update UI still work.
6. Reboot; confirm the init hook and state recovery.

Physical (requires owner authorization):

1. Install on a rooted unit; verify only 8 WSS commands remain (root alone does
   not unlock dev actions).
2. Point the E64 M2M host at the grinder's own address.
3. Pull a real shot; verify `REAL_EXTRACTION_TIME_MS` and whether GBS moves the
   dial. This is the make-or-break claim.
4. Repeat at 15 s and 45 s, ≥3 reps, with a no-injection control.
5. Check `/db` survival, signed-update loss/reapply, and uninstall.

## Limitations

- **GBS consumption is unproven.** The E64 polls the Xenia shape, but whether
  GBS applies the extraction time to the dial needs a physical A/B.
- **HMI untouched.** There is no touch controller and no vendor extension
  point; patching `libNgxUi.so` is high-risk and lost on update. Use the stock
  M2M screen.
- **BLE not supported on-device.** Use an external sidecar or a WiFi scale.
- **Dev mode not required, but not a substitute for it.** `AddBrewEvent` and
  the 44 dev WSS actions remain unavailable; this path uses Xenia only.
- **No stock no-root install.** Writing `/usr/local/www` and `/etc/init.d`
  requires root.

## Recovery

```sh
# Stop and remove the service/page; keep /db config and shots
./ondevice/uninstall.sh --yes

# Also remove /db/scale-bridge
./ondevice/uninstall.sh --yes --purge
```

If the daemon misbehaves, `S95scalebridge stop` is enough; the rest of the
grinder is unaffected. The vendor HMI/update path is not modified.
