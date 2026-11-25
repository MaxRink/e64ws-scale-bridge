# Review follow-ups

A code review of this repository listed gaps that were not implemented when the
review was written. Each one is either implemented now or explicitly marked as
unimplemented here, with the place that enforces the limit and the workaround.
Nothing in this list is silently dropped; a change that implements one must
update this table.

Status values: **implemented** (in this repository), **partial** (implemented
with a documented limit), **unimplemented** (deliberate; use the workaround).

| Review item | Status | Where it stands |
|---|---|---|
| Token refresh | **unimplemented** | A token is supplied per run (`--token`, `--token-file`) or obtained once by `--login-role`. There is no background refresh or automatic re-login when the vendor expires the WSS token; an expired or rejected session ends as an error (exit 2). Re-run with a fresh token. The on-device daemon needs no WSS token at all. |
| TLS pinning | **partial** | `--ca-file` trusts exactly the certificate/CA you pass, which pins the device certificate, and `--insecure` disables verification. There is no SPKI pinning and no pin-rotation handling. See the TLS bullets in [GUIDE.md](GUIDE.md#5-replay-and-troubleshooting). |
| WSS reconnect | **unimplemented** in `tools/e64ws_client.py` | One connection per request, closed afterwards; no retry/backoff loop. A silent or dropped connection now ends in a redacted message and exit 2 instead of a traceback (`tests/test_e64ws_client.py:ReceiveTimeoutTests`), and re-running reconnects. The on-device daemon *does* reconnect the scale's `ws://` stream with exponential backoff. |
| Tare | **unimplemented** | `OpenScaleClient.send_safe(..., allow_tare=True)` exists, but no bridge path calls it: the bridge never tares the scale. Use the scale's own tare button/screen. The detector compensates for level changes by re-seeding its idle baseline instead. |
| Invalid-event path for dropout / early removal / retained samples | **partial** | Early removal ends the shot as `cup removed / weight drop` (`tests/test_scale_bridge.py:test_cup_removal_ends_the_shot`); a short shot is reported with `validity: false` (`test_short_shot_is_invalid_but_reported`); a shot that never settles closes at `--max-shot-ms` as `reason: "timeout"` (`test_never_flat_series_times_out_instead_of_hanging`). A **dropout mid-shot is discarded, not emitted**: the daemon resets the detector and returns to idle (`tests/test_ondevice_daemon.py:test_dropout_discards_an_active_shot_and_rearms`), because the samples are incomplete. The detector still **retains the pre-shot idle samples** in its sample list, so `sample_count` counts them; only samples after the `--stable-ms` cutoff feed the finish window. Unchanged. |
| Redaction in ON_DEVICE §Verification 5 | **implemented** | `e64ws_client.redact()` replaces the token in the `?token=` query value and any `Bearer` value, and the exception chain is cut so the unredacted text cannot resurface (`tests/test_e64ws_client.py:RedactionTests`). [ON_DEVICE_INSTALL.md](ON_DEVICE_INSTALL.md#verification) step 5 now requires redacting `ingest_token` and token-bearing headers before evidence leaves the device. |
| `/api/v2/*` HTTPS exemption | **documented trade-off** | The exemption exists because the E64 Xenia client is HTTP-only. The consequence is stated where the exception is configured (`ondevice/patch_lighttpd.py`), in the README and in [ON_DEVICE_INSTALL.md](ON_DEVICE_INSTALL.md#why-lighttpd-is-patched): the five local Xenia routes, including the `POST /api/v2/brewratio` write and the `X-Scale-Token` header for `/api/v2/ingest`, travel in cleartext on the LAN. Accepted only on a trusted, isolated LAN. Narrowing the exemption is not implemented because it would break the QEMU-verified client path. |
