# E64 brew-event contract used by the bridge

The bridge sends a `GrinderAction` with `data.actionType = "AddBrewEvent"`:

```json
{
  "type": "GrinderAction",
  "msgId": 1,
  "data": {
    "actionType": "AddBrewEvent",
    "grindUuid": "<uuid>",
    "machineName": "scale-bridge",
    "groupHead": "0",
    "doseIndex": 1,
    "validity": true,
    "brewTimeMs": 24600,
    "brewWeightMg": 18000
  }
}
```

| Field | Unit | Notes |
|---|---|---|
| `grindUuid` | string | UUID generated per event |
| `machineName` | string | bridge label |
| `groupHead` | string | `"0"` for the scale path |
| `doseIndex` | integer | invalid values fall back in the vendor parser |
| `validity` | bool | detector's >15 s result |
| `brewTimeMs` | integer | measured shot duration |
| `brewWeightMg` | integer | measured mass in milligrams |

The event is accepted only when the grinder is in the development profile.
Stock firmware closes the WSS connection. The bridge prints the event before
sending and requires `--apply --yes`.

## Scale parsers

- **Decent 7/10-byte:** `03 CE/CA` + big-endian `uint16` in 0.1 g.
- **Generic 0x2A9D:** flags bit 0 = imperial; SI resolution 0.005 kg, imperial
  0.01 lb.
- **OpenScale snapshot:** untyped `{"grams": number, "ms": number}` at 2/5/10 Hz.
