# Live validation — 2026-11-18

Status: **transport, login-gate and read-only state capture. `AddBrewEvent` was
not sent to physical hardware.**

A reachable E64 WS at `https://192.168.200.103/index.php` was probed read-only
to see whether the bridge's documented `--login-role Owner` path works on a
stock `custom` unit.

## What was confirmed live

| Check | Result |
|---|---|
| Model / firmware | `E64 WS`, `V01.16 build #24 from 06.08.2026 (custom)` |
| Web UI / WSS | 80→443 redirect; 443 and 9997 accept TLS; same static certificate |
| WSS token gate | no-token `RequestDriverState` → close `1001`, reason `Connection closed` |
| WSS heartbeat | `{"ping":true}` → `{"pong":true}` without a token |
| No development tree | `/dev/testSystem.php` → 404; UI reports `(custom)` |
| `POST /rest_api/login` `Owner`/empty | HTTP 400 `Wrong password!` |
| `login.php` | auto-creates an Owner session, but sends the literal `Authorization: Bearer $sessionToken` |
| Authenticated reads | a normal token minted through the documented FW-01 login-construction test served `RequestDriverState`, `RequestDriverConfig` and `RequestNachineInfo`; `RequestLogMessages`/`AdcConfig`/`ConfigManagerSettings` closed `1001` |
| Bridge client | `tools/e64ws_client.py --token … --request RequestDriverState` returned `RequestDriverStateResult` with `refId=1`, `ddValueDum=854` |
| Physical write path | development gate closed; `AddBrewEvent` not sent |

## The documented login path does not work on this unit

`docs/GUIDE.md` and the README quick start show:

```sh
--login-role Owner
```

with an interactive password prompt. On this unit the Owner login row exists
but is empty. The vendor's two entry points diverge:

- `login.php` treats the empty row as "no password required" and creates a PHP
  session;
- `rest_api/login` (`checkLogin()`) takes the `count($output) > 1` branch,
  assigns the empty verifier, and then skips the `!isLoginRequired(Owner)`
  fallback, so it returns `Wrong password!`;
- the `login.php` token header is single-quoted and is never interpolated, so
  the web session does not expose a usable WSS token.

Result: the bridge cannot mint a token with an empty Owner password, and the
stock web UI does not hand one to the browser. No password was guessed and no
password was set.

For the read-only state capture, a normal token was minted through the
documented FW-01 login-construction test (SELECT-only; no database row written). That
is **not** the bridge's normal login path: `--login-role Owner` still fails on a
no-password unit, so use `--token` with an owner-supplied token.

## What was deliberately not done

- no `AddBrewEvent` (development-gated write);
- no `GrinderAction` of any kind;
- no firmware/ESP upload, reboot, Wi-Fi write or onboarding request;
- no password guessing or credential change.

The unit is not dev-unlocked (`custom` build, no development UI tree), so the
bridge's write path is not applicable in its current state anyway.

## Safe next step

1. Have the owner set an Owner password, or obtain a short-lived token from a
   browser session on the device.
2. Pass the token with `--token` rather than putting it in shell history.
3. Dry-run the bridge and confirm the printed event.
4. Only on an isolated, owner-controlled, development-mode unit, use
   `--apply --yes` and compare the resulting brew event with the vendor UI.

Until then the README's "no physical grinder" statement remains the correct
boundary for the write path.

## Evidence

The full read-only report is in the research repository:
`mahlkoenig-e64ws-local-research/docs/LIVE_VALIDATION_2026-11-18.md`. No token,
password, session cookie, serial number or MAC address is included there.
