# Owner-account cloud brew event

This is the legitimate cloud path for the Mahlkönig E64 WS: post a brew event
for **your own grinder** under **your own cloud account**. It is not a way to
clone, spoof, or borrow an official scale identity.

`tools/owner_cloud_event.py` builds the E64 brew-event payload from a
`tools/scale_bridge.py` event and POSTs it once to a base URL and endpoint path
that you supply. It is stdlib-only. It does not create an account, log in,
register a device, refresh a token, or know a production hostname.

> **The cloud API contract is not shipped in the firmware.** The payload below
> is a documented proposal: the E64 `AddBrewEvent` fields plus a small
> owner-cloud envelope (`eventUuid`, `grinderId`). It must be compared with a
> consented capture of your own account before you send anything. Do not assume
> the vendor accepts it.

## What is not allowed

- Do not copy an official scale's serial number, certificate, provisioning
  blob, client ID, HMAC key, or device headers.
- Do not reuse another person's account, token, grinder ID, or capture.
- Do not patch, re-sign, or extract secrets from the official app to get around
  TLS pinning, attestation, or account controls.
- Do not point this tool at a host you do not own or administer, and do not use
  it to post events for a grinder you do not own.
- Do not commit captures, HAR files, tokens, cookies, or device serials.

**The E64 still needs a valid owner cloud account and a device binding.** The
grinder must already be claimed/bound in the official app and able to sync
normally. This tool does not perform that binding.

## Consent

Cloud captures and brew events can contain account identifiers, device serials,
location, and household usage. Before capturing:

1. Use your own phone, your own account, and your own grinder.
2. Tell everyone who shares the account or the grinder what you are doing and
   get their consent.
3. Prefer a dedicated test account and a test grinder if the vendor supports
   it. Otherwise keep the capture window short and the captured data minimal.
4. Do not capture shared, family, or third-party traffic. Do not leave the
   proxy or its CA certificate installed after the test.
5. Keep the capture and token outside the repository, preferably on an
   encrypted disk, and delete them when finished.

If the vendor's terms or your local rules do not permit third-party clients,
stop. This repository does not grant permission to use the service.

## Payload

The tool accepts the record printed by `scale_bridge.py --json`:

```json
{
  "shot": {"duration_ms": 24600.0, "mass_g": 18.0, "valid": true},
  "event": {
    "grindUuid": "…",
    "machineName": "scale-bridge",
    "groupHead": "0",
    "doseIndex": 1,
    "validity": true,
    "brewTimeMs": 24600,
    "brewWeightMg": 18000
  }
}
```

It also accepts a bare event object, a JSONL stream, or concatenated JSON
values (the last value wins), via `--replay-event FILE`, `--replay-event -`, or
`--stdin`.

The POST body is:

```json
{
  "eventUuid": "7b1d…",
  "grinderId": "owner-grinder-id",
  "grindUuid": "…",
  "brewTimeMs": 24600,
  "brewWeightMg": 18000,
  "validity": true,
  "doseIndex": 1,
  "groupHead": "0",
  "machineName": "scale-bridge"
}
```

| Field | Unit / type | Meaning |
|---|---|---|
| `eventUuid` | string | New idempotency key for this cloud event; generated per run unless overridden |
| `grinderId` | string | The owner's grinder id from the owner's account, not an official scale serial |
| `grindUuid` | string | Grind id; preserved from the scale-bridge event when present |
| `brewTimeMs` | integer ms | Measured shot duration |
| `brewWeightMg` | integer mg | Measured shot mass |
| `validity` | boolean | Source event value, else the documented `>15 s` Sync window |
| `doseIndex` | integer | Dose metadata, default `1` |
| `groupHead` | string | Group head, default `"0"` |
| `machineName` | string | Local label, default `"scale-bridge"` |

The default endpoint path is `/brew-events`. That is a placeholder, not a
vendor route. Set the real path from your capture with `--endpoint-path` or
`E64_OWNER_CLOUD_EVENT_PATH`.

| Setting | CLI | Environment |
|---|---|---|
| Base URL | `--base-url` | `E64_OWNER_CLOUD_BASE_URL` |
| Endpoint path | `--endpoint-path` | `E64_OWNER_CLOUD_EVENT_PATH` |
| Bearer token | `--token-file FILE` | `E64_OWNER_CLOUD_TOKEN` |
| Grinder id | `--grinder-id` | `E64_OWNER_CLOUD_GRINDER_ID` |
| Private test CA | `--ca-file` | `E64_OWNER_CLOUD_CA_FILE` |

## Owner workflow

### 1. Confirm the grinder is already bound

In the official app, confirm that the E64 is claimed to your account and that a
normal brew from the app or grinder is visible in the cloud. If that does not
work, fix the account/device binding first. A POST cannot substitute for it.

### 2. Capture one owner-account request

Capture only the request the official app makes for your own account and
grinder. You need the scheme, host, path, auth scheme, and body shape. Follow
[Capture with mitmproxy](#capture-with-mitmproxy) below, then redact the flow
before looking at it.

### 3. Choose the base URL and endpoint path

Use the redacted capture to set these explicitly:

```sh
export E64_OWNER_CLOUD_BASE_URL='https://owner-cloud.example.invalid'
export E64_OWNER_CLOUD_EVENT_PATH='/the/path/from/your/capture'
export E64_OWNER_CLOUD_GRINDER_ID='your-own-grinder-id'
```

`owner-cloud.example.invalid` is a reserved placeholder. There is no production
host default in the tool. If the capture uses a different path, change it.

### 4. Put the token in a file

```sh
install -d -m 700 ~/.config/e64ws
umask 077
# Paste the token only at the prompt, not on the command line.
read -r -s E64_TOKEN
printf '%s\n' "$E64_TOKEN" > ~/.config/e64ws/owner-cloud.token
unset E64_TOKEN
chmod 600 ~/.config/e64ws/owner-cloud.token
```

The tool also accepts `E64_OWNER_CLOUD_TOKEN` in the environment. There is no
`--token` flag, so a token cannot accidentally land in shell history or in the
process list.

### 5. Dry run

The default is a dry run. It prints the exact request shape, with the
Authorization header redacted, and sends nothing. It does not read the token
file.

```sh
python3 tools/scale_bridge.py --replay examples/shot.jsonl --once --json \
  > /tmp/owner-shot.json

python3 tools/owner_cloud_event.py \
  --replay-event /tmp/owner-shot.json \
  --grinder-id "$E64_OWNER_CLOUD_GRINDER_ID" \
  --base-url "$E64_OWNER_CLOUD_BASE_URL" \
  --endpoint-path "$E64_OWNER_CLOUD_EVENT_PATH" \
  --token-file ~/.config/e64ws/owner-cloud.token
```

Check:

- The URL is the owner cloud you intend, over HTTPS.
- The body keys and units match the redacted capture.
- `eventUuid` is new for each logical event. Pass an explicit `--event-uuid`
  if the dry run and the send must use the same id.
- `grinderId` is your grinder, not an official scale serial.

### 6. Send once

Review the dry run first. Then:

```sh
python3 tools/owner_cloud_event.py \
  --replay-event /tmp/owner-shot.json \
  --grinder-id "$E64_OWNER_CLOUD_GRINDER_ID" \
  --base-url "$E64_OWNER_CLOUD_BASE_URL" \
  --endpoint-path "$E64_OWNER_CLOUD_EVENT_PATH" \
  --token-file ~/.config/e64ws/owner-cloud.token \
  --send --yes
```

`--send` without `--yes` exits before any network call. The tool posts once; it
does not retry automatically. A successful run prints the HTTP status, event
UUID, grinder id, and response byte count. It never prints the response body or
the token.

If a request times out after the server may have accepted it, do not blindly
send a new event with a new UUID. Re-run with the same `--event-uuid` and the
same `--grind-uuid` so a server that deduplicates can recognise the retry. If
the server does not deduplicate, a retry can create a duplicate brew event.

### 7. Verify and clean up

Check the official app and the grinder. A cloud 200 does not prove that GBS
consumed the event or moved the dial. If the cloud accepted the event but the
grinder did not react, check device binding, sync, the event shape, and whether
the cloud exposes it to the grinder at all.

Then remove the capture, revoke or rotate the test token, and unset the
environment variables. Remove the mitmproxy CA from the phone.

## Token handling

- Prefer `--token-file` over an environment variable. Both are supported; the
  file wins when both are present.
- File mode `600`, directory `700`. Keep it outside the repository.
- Use a short-lived token from a test account if possible. Revoke it after the
  test.
- The token is read only for `--send`, never for a dry run.
- The token is never included in printed headers, error messages, or response
  excerpts. This is enforced by tests.
- The tool rejects tokens containing whitespace, control characters, or
  non-ASCII bytes, which also blocks header injection.
- The tool does not refresh tokens, perform OAuth, or store tokens.
- A token pasted into a shell, issue tracker, chat, or commit is compromised.
  Revoke it; do not try to scrub it after the fact.

## Capture with mitmproxy

This section is for capturing **your own** app traffic on **your own** phone
and account. If you are not the account owner, or have not got consent from
everyone who shares the account, do not capture.

### Prerequisites

- A trusted local network. Do not expose the proxy to the internet or to an
  untrusted LAN.
- `mitmproxy` installed on a computer you control.
- The official app on your phone, already logged in to your own account.
- The E64 bound to that account.

### Run the proxy and install its CA

```sh
# Terminal 1: capture to a file, listening only for the test window.
mitmdump --listen-host 0.0.0.0 --listen-port 8080 \
  -w ~/owner-brew.mitmproxy
```

On the phone, set the Wi-Fi HTTP proxy to the computer's LAN address and port
`8080`. Then open `http://mitm.it` in the phone browser and install the
mitmproxy CA certificate for your platform:

- **iOS:** install the profile, then enable full trust under
  Settings → General → About → Certificate Trust Settings.
- **Android:** install it as a user CA. Some apps ignore user CAs; that is a
  signal to stop, not to bypass.

Remove the proxy setting and the CA as soon as the capture is finished.

### Generate the event

Use the app to perform or open one brew event for your own grinder. Keep the
capture short. Do not browse unrelated account, payment, or household data
while the proxy is active.

Stop `mitmdump` with Ctrl-C.

### TLS pinning

If the app pins certificates, the capture will show connection failures or
TLS errors. Stop there. Do not patch the app, re-sign it, extract embedded
keys, or disable attestation to get around pinning. Without a supported capture
path, the cloud contract is simply not available; use the manual brew-time or
local Xenia options in [STOCK_OPTIONS.md](STOCK_OPTIONS.md) instead.

### Redact before reading

Never open an unredacted capture in a shared terminal, editor, or chat. Run a
redaction addon first. Save this as `redact.py` outside the repository:

```python
# redact.py - for the owner's own capture only.
import json

from mitmproxy import http

SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "proxy-authorization",
    "x-api-key", "x-auth-token", "x-csrf-token",
}
SENSITIVE_KEYS = {
    "authorization", "access_token", "refresh_token", "id_token", "token",
    "password", "secret", "api_key", "apikey", "cookie",
    "email", "phone", "name", "userid", "accountid", "customerid",
    "serial", "serialnumber", "deviceid", "machineid", "grinderid",
    "mac", "ip", "ssid", "latitude", "longitude", "location",
}


def scrub(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            normalized = key.lower().replace("_", "").replace("-", "")
            cleaned[key] = "<redacted>" if normalized in SENSITIVE_KEYS else scrub(item)
        return cleaned
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def scrub_message(message):
    content_type = message.headers.get("content-type", "").lower()
    if "json" not in content_type:
        return
    try:
        value = json.loads(message.get_text())
    except Exception:
        return
    message.set_text(json.dumps(scrub(value), indent=2))


def request(flow: http.HTTPFlow):
    for name in list(flow.request.headers):
        if name.lower() in SENSITIVE_HEADERS:
            flow.request.headers[name] = "<redacted>"
    # Query strings often carry tokens; keep the path shape, drop the query.
    flow.request.path = flow.request.path.split("?", 1)[0]
    scrub_message(flow.request)


def response(flow: http.HTTPFlow):
    for name in list(flow.response.headers):
        if name.lower() in SENSITIVE_HEADERS:
            flow.response.headers[name] = "<redacted>"
    scrub_message(flow.response)
```

Apply it to a copy and inspect only the copy:

```sh
mitmdump -nr ~/owner-brew.mitmproxy -s ./redact.py \
  -w ~/owner-brew-redacted.mitmproxy

# Readable terminal view of the redacted flow.
mitmdump -nr ~/owner-brew-redacted.mitmproxy --flow-detail 3
```

Redaction checklist:

- `Authorization`, cookies, CSRF tokens, API keys.
- Account e-mail, name, user/account/customer ids.
- Device serial, grinder id, machine id, certificates, provisioning blobs.
- MAC address, SSID, public IP, location, phone model/user agent.
- Query strings and URL path segments that contain ids.
- Response bodies and headers, not just the request.
- Screenshots and shell scrollback, not just the flow file.

The addon is a starting point, not a guarantee. Read the redacted flow manually.
If anything still looks like a credential or a personal identifier, delete the
capture and start again with a narrower test.

## Testing in a fixture

The unit tests use only loopback HTTP and HTTPS servers. They never contact a
cloud or resolve a public name. The HTTPS fixture uses the test-only self-signed
certificate embedded in `tests/test_owner_cloud_event.py`; that key is not a
production credential.

```sh
python3 -m unittest tests.test_owner_cloud_event -v
python3 -m unittest discover -s tests -v
```

The tests cover:

- payload keys, milliseconds/milligrams, dose metadata, and UUID handling;
- dry-run output with `Authorization: Bearer <redacted>` and zero requests;
- a single POST with the real bearer header on the wire;
- environment token/base-URL configuration;
- HTTP and HTTPS loopback fixtures, including `--ca-file`;
- 401, 403, and 500 handling with no token or response-body leakage;
- malformed JSON, non-finite numbers, missing mass/duration, invalid URLs, and
  send-without-`--yes`;
- the absence of a production host default.

For a manual dry run that cannot reach a cloud, use a reserved placeholder:

```sh
python3 tools/owner_cloud_event.py \
  --duration-ms 24600 --mass-g 18 \
  --grinder-id owner-grinder-id \
  --base-url https://owner-cloud.example.invalid
```

That prints the request shape and exits without DNS or network activity.

## Failure modes

| Symptom / exit | Meaning | Action |
|---|---|---|
| exit 2, `no base URL configured` | No `--base-url` or env value | Set the owner cloud base URL explicitly |
| exit 2, `plain HTTP is refused` | HTTP used without `--allow-http` | Use HTTPS; `--allow-http` is only for a loopback fixture |
| exit 2, `--allow-http is limited to loopback` | Tried to use HTTP on a LAN/public host | Use HTTPS or a loopback test |
| exit 2, `base URL must not contain userinfo` | Credentials in the URL | Remove them; use the token file |
| exit 2, `redirect refused` | The endpoint returned 3xx | Check base URL/path; do not rely on redirects |
| exit 2, `request rejected` | Other 4xx, usually 404/409/422 | Compare path and schema with the capture; check duplicate event |
| exit 3, HTTP 401 | Token expired, revoked, or wrong account | Re-authenticate in the owner app and use a fresh token |
| exit 3, HTTP 403 | Missing scope, consent, or device binding | Check account permissions and that the grinder is bound |
| exit 4, HTTP 429 | Rate limited | Wait; do not loop or retry automatically |
| exit 4, HTTP 5xx | Owner cloud server error | Retry later, once, after checking the event was not already accepted |
| exit 5, TLS verification failed | Private CA or wrong host | Use a valid certificate or `--ca-file` for your test CA |
| exit 5, connection refused / timeout | Wrong host, port, or proxy | Check the capture and network; the tool ignores environment proxies |
| exit 2, token file has multiple lines | Accidentally captured headers | Recreate the file with exactly one token |
| Cloud 200 but grinder unchanged | Binding, sync, or GBS did not consume the event | Check the app/grinder binding and test with the official app first |

The tool never retries a POST. A retry can create a duplicate brew event; the
server may or may not deduplicate by `eventUuid`.

## Limitations

- **The cloud API contract is not shipped in the firmware.** The payload shape,
  auth scheme, path, and idempotency semantics are not guaranteed. This tool
  encodes a proposal that must be validated against a consented owner capture.
- **No official-scale identity cloning.** The tool does not contain or accept
  an official serial, certificate, provisioning blob, client secret, or vendor
  credential, and it does not spoof device headers.
- **The E64 still needs a valid owner cloud account and device binding.** The
  tool does not create, claim, bind, unbind, or transfer a device.
- **Bearer tokens only.** There is no OAuth login, refresh, device-code flow,
  mTLS, client certificate, or token storage.
- **No retries, queue, or offline mode.** One invocation is one POST.
- **No response-body display.** This is deliberate; use the server's own
  dashboard or app for server-side diagnostics.
- **No proxy support.** Environment proxies are disabled so a token cannot be
  sent through an unexpected proxy. Use a direct connection or a loopback
  fixture.
- **No custom trust except `--ca-file`.** The system trust store is used
  otherwise. There is no `--insecure` switch.
- **No guarantee of grinder behavior.** A cloud acceptance is not proof that
  GBS consumes the event or changes the next grind.
- **No physical-hardware validation.** The repository's tests are loopback
  protocol tests, not a live cloud or grinder test.
- **Vendor terms and account rules still apply.** The owner is responsible for
  checking that a third-party client is permitted and for revoking test
  credentials afterwards.

See [STOCK_OPTIONS.md](STOCK_OPTIONS.md) for the manual brew-time and local
Xenia alternatives, and [PROTOCOL.md](PROTOCOL.md) for the local WSS event
shape.
