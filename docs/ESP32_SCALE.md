# Flashing the scale’s ESP32: what is possible

The scale hardware and firmware are open source: the Half Decent Scale / Decent
Open Scale is an ESP32-S3 board (HDS 1.x uses ESP32-S3N16, ADS1232, SH1106/
SH1116, BSS138 level shifting, BMA400; older boards use HX711/SSD1306). The
upstream firmware is built with PlatformIO and exposes BLE (Decent Scale API)
and, on newer firmware, Wi-Fi with `/snapshot` WebSocket and signed OTA.

## Verdict

| Goal | Possible? | Why |
|---|---|---|
| Flash the scale ESP32 with OpenScale/custom firmware | **Yes, in principle** | Open hardware and firmware; PlatformIO USB upload and signed Wi-Fi OTA are documented |
| Direct **BLE from the scale to the E64 grinder** | **No** | The E64 has no BLE scale client; its ESP32 is a Wi-Fi co-processor and Bluetooth is compiled out |
| Direct BLE from the scale to a phone/host | **Yes** | Already the Decent Scale API; a custom firmware can keep or extend it |
| Direct **Wi-Fi Xenia M2M** from the scale to a stock E64 | **Yes, worth testing** | E64 polls a Xenia machine over plain HTTP; the ESP32 can serve the four routes |
| Direct WSS to a stock E64 | **No** | `AddBrewEvent`/GBS actions are development-only; stock closes the connection |
| Direct WSS to a dev-unlocked/rooted E64 | **Yes** | The scale can call the same local WSS commands as the host bridge |

“Direct BT” in the sense of the scale pairing with the grinder is therefore not
a firmware problem; the grinder has no BLE radio client for scales. The useful
on-scale path is **Wi-Fi Xenia M2M**, with BLE retained for a phone/host.

## Hardware/firmware evidence

From the public `decentespresso/openscale` repository:

- `platformio.ini`: `board = esp32-s3-devkitc-1`, Arduino framework,
  `upload_speed = 921600`, 8 MB partition table, optional
  `esp32s3-grinder`/`esp32s3-custom` environments.
- HDS 1.x has 16 MB flash; HDS 2.0 moves to 8 MB. The firmware already fits in
  8 MB, so the partition table is intentionally pinned.
- `include/webserver.h`: `AsyncWebServer server(80)` and
  `AsyncWebSocket websocket("/snapshot")`.
- `include/ble.h` and `include/decent_protocol.h`: the BLE Decent Scale API.
- `keys/ota/`: three official OTA manifest public keys plus custom-build keys.
- `README.md` documents USB flashing at:
  `bootloader.bin 0x0000`, `partitions.bin 0x8000`, `firmware.bin 0x10000`,
  `littlefs.bin 0x670000`, and states that a full erase requires recalibration.
- Wi-Fi OTA is signed and verifies model/PCB/chip/flash/partition/filesystem
  and asset hashes before writing.

No secure-boot/eFuse lock is asserted by the upstream public documentation.
Before flashing a specific unit, verify the PCB revision, flash size, bootloader
state and whether the unit is a custom/secure build. Do not assume a DevKit
layout from the public `platformio.ini`.

## Build and flash (USB, development)

This is an **exploration recipe**, not a tested binary for a specific scale:

```sh
git clone https://github.com/decentespresso/openscale
cd openscale
python3 -m pip install -r requirements-platformio.txt
pio run -e esp32s3
# connect USB; put the board in download mode if needed
pio run -e esp32s3 -t upload
pio device monitor
```

The first flash should preserve a full flash dump and the calibration values.
OpenScale requires recalibration after a full erase. A signed Wi-Fi OTA cannot
carry a self-built image unless it is signed by a key the installed firmware
trusts; USB is the practical development route.

## Integrating the Xenia M2M routes

A minimal scale-side addition would register four handlers in
`include/webserver.h` (or a new header called from `startWebServer()`):

| Route | Mapping |
|---|---|
| `GET /api/v2/mako` | `MA_STATUS` from scale timer/weight state; `REAL_EXTRACTION_TIME_MS` from the shot timer; `PU_SENS_FLOW_METER_VOLUME` from the scale or 0; fixed `BG_SET_TEMP/BG_SENS_TEMP/TANK_LEVEL`; `TIMESTAMP` |
| `GET /api/v2/machine` | `MA_TYPE=XENIA`, `MA_SN`, firmware fields |
| `POST /api/v2/brewratio` | store `SYNC_BEVERAGE_WEIGHT`/`SYNC_FILTER` and echo `MA_SYNC_*` |
| `POST /api/v2/scripts/execute` | start/stop the scale timer or no-op with 200 |

The E64 only polls the scale; it does not need a cloud account. The scale can
keep the existing BLE API and `/snapshot` stream. A shot is:
`idle -> active/serving -> finishing -> idle`, with the measured timer in
`REAL_EXTRACTION_TIME_MS`.

This is the same contract implemented by `tools/xenia_emulator.py`; use that
fixture to validate the JSON before porting it to the ESP32.

## Risks and recovery

- **Calibration loss:** a full erase drops load-cell calibration. Back up the
  values or recalibrate with a known mass.
- **OTA trust:** Wi-Fi OTA is signed; a custom image needs a trusted key or USB.
- **Board mismatch:** HDS 1.x vs 2.0 flash size/partition and PCB revision differ.
- **Bricking:** keep the USB cable and a known-good OpenScale release; the
  bootloader/UF2/WebSerial path is the recovery route.
- **Warranty/terms:** flashing is owner-authorized hardware modification.

## Acceptance test

1. Flash the unmodified OpenScale build; confirm BLE and `/snapshot` still work.
2. Add the four M2M routes; confirm `curl http://<scale>/api/v2/mako` returns
   the expected JSON.
3. Point the E64’s Machine-to-Machine host at the scale; confirm the grinder
   polls it.
4. Pull a real espresso shot and compare the next grind’s dial/GBS result
   against the same shot with the Xenia fixture. Only that A/B test proves the
   GBS consumes the synthetic extraction time.
5. Confirm BLE still connects to the phone/host after the Wi-Fi change.

## Bottom line

Flashing the scale ESP32 is feasible and useful, but it cannot add a BLE radio
to the E64. The high-value firmware change is to make the scale serve the
Xenia M2M contract over Wi-Fi; the existing host-side `xenia_emulator.py` is
the reference implementation and the physical A/B test is still required.
