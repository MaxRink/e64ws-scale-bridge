#!/usr/bin/env python3
"""Post an owner-account brew event for the owner's own E64 WS grinder.

This is the legitimate cloud path: the event is posted under the owner's own
cloud account and for the owner's own grinder.  It does not log in, register a
device, refresh a token, or impersonate an official scale.  The cloud API
contract is not present in the shipped firmware, so the base URL and endpoint
path are deliberately configuration, not constants.

Safety properties
-----------------
* Dry-run by default.  ``--send --yes`` is required to perform a POST.
* No production host is built in.  Supply ``--base-url`` or
  ``E64_OWNER_CLOUD_BASE_URL``.
* Bearer token from ``--token-file`` or ``E64_OWNER_CLOUD_TOKEN``.  There is
  intentionally no ``--token`` option, to keep tokens out of shell history.
  The token is never printed and response bodies are never echoed.
* HTTPS is required for non-loopback hosts.  Plain HTTP is accepted only for
  an explicit loopback test fixture (``--allow-http``).
* Redirects are refused, environment proxies are ignored, and the request
  timeout is bounded.

The payload is the E64 ``AddBrewEvent`` field set (milliseconds, milligrams)
plus an owner-cloud envelope: ``eventUuid`` and ``grinderId``.  Compare it with
a consented capture of your own account before sending; see
docs/OWNER_CLOUD_EVENT.md.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

DEFAULT_BASE_URL = ""
DEFAULT_ENDPOINT_PATH = "/brew-events"
VALID_MIN_MS = 15_000
MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 300.0
REDACTED = "<redacted>"
USER_AGENT = "e64ws-owner-cloud-event/1"

ENV_BASE_URL = "E64_OWNER_CLOUD_BASE_URL"
ENV_ENDPOINT_PATH = "E64_OWNER_CLOUD_EVENT_PATH"
ENV_TOKEN = "E64_OWNER_CLOUD_TOKEN"
ENV_GRINDER_ID = "E64_OWNER_CLOUD_GRINDER_ID"
ENV_CA_FILE = "E64_OWNER_CLOUD_CA_FILE"

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


# --------------------------------------------------------------------- errors


class OwnerCloudError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 2


class UsageError(OwnerCloudError):
    exit_code = 2


class AuthenticationError(OwnerCloudError):
    exit_code = 3


class RemoteServerError(OwnerCloudError):
    exit_code = 4


class NetworkError(OwnerCloudError):
    exit_code = 5


# ------------------------------------------------------------------- redaction


def redact(value: object, secret: str = "") -> str:
    """Return *value* safe for a log line.

    The supplied secret is replaced literally first, then any ``Bearer`` value
    is replaced so an Authorization header cannot survive a formatting mistake.
    """

    text = str(value)
    if secret:
        text = text.replace(secret, REDACTED)
    return _BEARER_RE.sub(f"Bearer {REDACTED}", text)


def _clean_reason(value: object) -> str:
    text = "".join(
        char if 0x20 <= ord(char) <= 0x7E else " " for char in str(value)
    )
    text = " ".join(text.split())
    return text[:200] or "unknown error"


# ----------------------------------------------------------------------- URL


def is_loopback_host(host: str) -> bool:
    name = host.strip("[]").rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def validate_base_url(raw: str, *, allow_http: bool = False) -> str:
    """Validate and normalise a base URL.

    HTTPS is mandatory except for loopback fixtures explicitly enabled with
    ``allow_http``.  Userinfo, queries, fragments and non-ASCII/control bytes
    are rejected so the URL cannot smuggle credentials or log formatting.
    """

    if not isinstance(raw, str):
        raise UsageError("base URL must be a string")
    text = raw.strip()
    if not text:
        raise UsageError(
            "no base URL configured; pass --base-url or set "
            f"{ENV_BASE_URL} (there is no production default)"
        )
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in text):
        raise UsageError("base URL contains whitespace, control or non-ASCII characters")
    if "\\" in text:
        raise UsageError("base URL must not contain a backslash")

    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise UsageError("base URL must use http:// or https://")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UsageError("base URL must not contain userinfo or credentials")
    if not parsed.hostname:
        raise UsageError("base URL must include a host")
    try:
        parsed.port  # noqa: B018 - property validates the port
    except ValueError:
        raise UsageError("base URL has an invalid port") from None
    if parsed.query or parsed.fragment:
        raise UsageError("base URL must not contain a query or fragment")

    if scheme == "http":
        if not allow_http:
            raise UsageError(
                "plain HTTP is refused; use HTTPS (or --allow-http only for a "
                "loopback test fixture)"
            )
        if not is_loopback_host(parsed.hostname):
            raise UsageError("--allow-http is limited to loopback hosts")

    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def build_endpoint_url(
    base_url: str, endpoint_path: str, *, allow_http: bool = False
) -> str:
    base = validate_base_url(base_url, allow_http=allow_http)
    if not isinstance(endpoint_path, str) or not endpoint_path:
        raise UsageError("endpoint path must be a non-empty string")
    if not endpoint_path.startswith("/"):
        raise UsageError("endpoint path must start with '/'")
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in endpoint_path):
        raise UsageError("endpoint path contains whitespace or control characters")
    if "\\" in endpoint_path:
        raise UsageError("endpoint path must not contain a backslash")
    if "?" in endpoint_path or "#" in endpoint_path:
        raise UsageError("endpoint path must not contain a query or fragment")
    return base + endpoint_path


# ------------------------------------------------------------------- payload


def _as_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise UsageError(f"{name} must be finite")
    return number


def _as_int(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise UsageError(f"{name} must be an integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise UsageError(f"{name} must be an integer")
        number = int(value)
    else:
        raise UsageError(f"{name} must be an integer")
    if minimum is not None and number < minimum:
        raise UsageError(f"{name} must be at least {minimum}")
    return number


def _as_duration_ms(value: object) -> int:
    number = _as_number(value, "duration_ms")
    if number <= 0:
        raise UsageError("duration_ms must be positive")
    result = int(round(number))
    if result <= 0:
        raise UsageError("duration_ms is too small")
    return result


def _mass_g_to_mg(value: object) -> int:
    number = _as_number(value, "mass_g")
    if number <= 0:
        raise UsageError("mass_g must be positive")
    result = int(round(number * 1000.0))
    if result <= 0:
        raise UsageError("mass_g is too small to express in milligrams")
    return result


def _as_text(
    value: object,
    name: str,
    *,
    max_length: int,
    allow_int: bool = False,
) -> str:
    if allow_int and isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise UsageError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise UsageError(f"{name} must not be empty")
    if len(text) > max_length:
        raise UsageError(f"{name} is longer than {max_length} characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise UsageError(f"{name} contains control characters")
    return text


def build_brew_payload(
    *,
    duration_ms: float,
    grinder_id: str,
    mass_g: float | None = None,
    mass_mg: int | None = None,
    event_uuid: str | None = None,
    grind_uuid: str | None = None,
    dose_index: int = 1,
    group_head: str | int = "0",
    machine_name: str = "scale-bridge",
    validity: bool | None = None,
) -> dict[str, Any]:
    """Build the E64 brew-event payload with owner-cloud envelope fields.

    ``brewTimeMs`` is an integer number of milliseconds and ``brewWeightMg`` is
    an integer number of milligrams.  Exactly one of ``mass_g`` and ``mass_mg``
    must be supplied.  ``validity`` defaults to the documented >15 s Sync
    window when the source event does not carry a boolean.
    """

    duration = _as_duration_ms(duration_ms)
    if (mass_g is None) == (mass_mg is None):
        raise UsageError("provide exactly one of mass_g or mass_mg")
    if mass_mg is not None:
        mass_milligrams = _as_int(mass_mg, "mass_mg", minimum=1)
    else:
        mass_milligrams = _mass_g_to_mg(mass_g)

    resolved_grinder = _as_text(grinder_id, "grinder_id", max_length=256)
    resolved_event = (
        _as_text(event_uuid, "event_uuid", max_length=256)
        if event_uuid is not None
        else str(uuid.uuid4())
    )
    resolved_grind = (
        _as_text(grind_uuid, "grind_uuid", max_length=256)
        if grind_uuid is not None
        else str(uuid.uuid4())
    )
    if validity is None:
        resolved_validity = duration >= VALID_MIN_MS
    elif isinstance(validity, bool):
        resolved_validity = validity
    else:
        raise UsageError("validity must be a boolean when supplied")
    dose = _as_int(dose_index, "dose_index", minimum=0)
    group = _as_text(group_head, "group_head", max_length=32, allow_int=True)
    machine = _as_text(machine_name, "machine_name", max_length=128)

    return {
        "eventUuid": resolved_event,
        "grinderId": resolved_grinder,
        "grindUuid": resolved_grind,
        "brewTimeMs": duration,
        "brewWeightMg": mass_milligrams,
        "validity": resolved_validity,
        "doseIndex": dose,
        "groupHead": group,
        "machineName": machine,
    }


def _first(
    mapping: Mapping[str, Any], names: tuple[str, ...]
) -> tuple[bool, Any]:
    for name in names:
        if name in mapping:
            return True, mapping[name]
    return False, None


def _event_and_shot(
    record: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if not isinstance(record, dict):
        raise UsageError("event must be a JSON object")
    if "event" in record:
        event = record["event"]
        if not isinstance(event, dict):
            raise UsageError("event.event must be a JSON object")
        shot = record.get("shot", {})
        if shot is None:
            shot = {}
        if not isinstance(shot, dict):
            raise UsageError("event.shot must be a JSON object")
        return event, shot
    return record, {}


def payload_from_record(
    record: Mapping[str, Any],
    *,
    grinder_id: str | None = None,
    event_uuid: str | None = None,
    grind_uuid: str | None = None,
    dose_index: int | None = None,
    group_head: str | int | None = None,
    machine_name: str | None = None,
    validity: bool | None = None,
) -> dict[str, Any]:
    """Turn a ``tools/scale_bridge.py`` record or bare event into a payload.

    A ``scale_bridge.py --json`` record has the shape
    ``{"shot": {...}, "event": {...}}``.  A bare ``event`` object, a JSONL
    stream (the last value wins), and an already-enveloped object are accepted.
    Explicit keyword arguments override the values found in the record.
    """

    event, shot = _event_and_shot(record)

    found, duration = _first(
        event, ("brewTimeMs", "brew_time_ms", "duration_ms", "durationMs")
    )
    if not found:
        found, duration = _first(shot, ("duration_ms", "durationMs"))
    if not found:
        raise UsageError("event has no brew duration (brewTimeMs or duration_ms)")

    found_mg, mass_mg = _first(
        event, ("brewWeightMg", "brew_weight_mg", "massMg", "mass_mg")
    )
    mass_g: Any = None
    if found_mg:
        mass_mg_value: Any = mass_mg
    else:
        found_g, mass_g = _first(event, ("mass_g", "massG"))
        if not found_g:
            found_g, mass_g = _first(shot, ("mass_g", "massG"))
        if not found_g:
            raise UsageError("event has no brew mass (brewWeightMg or mass_g)")
        mass_mg_value = None

    found, record_grinder = _first(event, ("grinderId", "grinder_id"))
    resolved_grinder = grinder_id if grinder_id is not None else (
        record_grinder if found else None
    )
    if resolved_grinder is None:
        raise UsageError(
            "event has no grinder id; pass --grinder-id or set "
            f"{ENV_GRINDER_ID}"
        )

    if event_uuid is None:
        found, value = _first(event, ("eventUuid", "event_uuid"))
        event_uuid = value if found else None
    if grind_uuid is None:
        found, value = _first(event, ("grindUuid", "grind_uuid"))
        grind_uuid = value if found else None

    if dose_index is None:
        found, value = _first(event, ("doseIndex", "dose_index"))
        if not found:
            found, value = _first(shot, ("dose_index", "doseIndex"))
        dose_index = value if found else 1
    if group_head is None:
        found, value = _first(event, ("groupHead", "group_head"))
        group_head = value if found else "0"
    if machine_name is None:
        found, value = _first(event, ("machineName", "machine_name"))
        machine_name = value if found else "scale-bridge"
    if validity is None:
        found, value = _first(event, ("validity",))
        if not found:
            found, value = _first(shot, ("valid",))
        validity = value if found else None

    return build_brew_payload(
        duration_ms=duration,
        grinder_id=resolved_grinder,
        mass_g=mass_g,
        mass_mg=mass_mg_value,
        event_uuid=event_uuid,
        grind_uuid=grind_uuid,
        dose_index=dose_index,
        group_head=group_head,
        machine_name=machine_name,
        validity=validity,
    )


# ---------------------------------------------------------------- input files


def _read_text_file(path: str, *, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        raise UsageError(f"cannot read {what} {path}: {detail}") from None
    except UnicodeDecodeError:
        raise UsageError(f"{what} {path} is not valid UTF-8") from None


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def load_event(text: str, *, source: str = "event") -> dict[str, Any]:
    """Parse one JSON object, JSONL, or concatenated JSON values.

    If more than one value is present, the last one is used.  Error messages
    deliberately never include the raw document, because a capture may contain
    an Authorization header or other secrets.
    """

    decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
    index = 0
    values: list[Any] = []
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise UsageError(
                f"{source}: invalid JSON at line {exc.lineno}, "
                f"column {exc.colno}: {exc.msg}"
            ) from None
        except ValueError as exc:
            raise UsageError(f"{source}: invalid JSON: {exc}") from None
        values.append(value)
    if not values:
        raise UsageError(f"{source}: no JSON value found")
    record = values[-1]
    if not isinstance(record, dict):
        raise UsageError(f"{source}: top-level JSON value must be an object")
    return record


def _payload_for(args: argparse.Namespace) -> dict[str, Any]:
    env_grinder = os.environ.get(ENV_GRINDER_ID, "").strip()
    override_grinder = (
        args.grinder_id
        if args.grinder_id is not None
        else (env_grinder or None)
    )

    uses_stdin = bool(args.stdin) or args.replay_event == "-"
    if args.replay_event is not None or args.stdin:
        if args.duration_ms is not None or args.mass_g is not None:
            raise UsageError(
                "use either --replay-event/--stdin or --duration-ms/--mass-g, "
                "not both"
            )
        if uses_stdin:
            text = sys.stdin.read()
            source = "stdin"
        else:
            assert args.replay_event is not None
            text = _read_text_file(args.replay_event, what="event file")
            source = args.replay_event
        record = load_event(text, source=source)
        return payload_from_record(
            record,
            grinder_id=override_grinder,
            event_uuid=args.event_uuid,
            grind_uuid=args.grind_uuid,
            dose_index=args.dose_index,
            group_head=args.group_head,
            machine_name=args.machine_name,
        )

    if args.duration_ms is None or args.mass_g is None:
        raise UsageError(
            "provide --duration-ms and --mass-g, or --replay-event FILE / --stdin"
        )
    if override_grinder is None:
        raise UsageError(
            f"--grinder-id or {ENV_GRINDER_ID} is required"
        )
    return build_brew_payload(
        duration_ms=args.duration_ms,
        mass_g=args.mass_g,
        grinder_id=override_grinder,
        event_uuid=args.event_uuid,
        grind_uuid=args.grind_uuid,
        dose_index=args.dose_index if args.dose_index is not None else 1,
        group_head=args.group_head if args.group_head is not None else "0",
        machine_name=(
            args.machine_name if args.machine_name is not None else "scale-bridge"
        ),
    )


# ------------------------------------------------------------------- transport


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def request_preview(url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The exact request shape, with the bearer token always redacted."""

    return {
        "method": "POST",
        "url": url,
        "headers": _headers(REDACTED),
        "body": dict(payload),
    }


def _http_status_error(status: int) -> OwnerCloudError:
    phrase = http.client.responses.get(status, "HTTP error")
    if status == 401:
        return AuthenticationError(
            "HTTP 401 Unauthorized: the owner cloud rejected the token; check "
            "account consent, device binding and token expiry. Response body "
            "suppressed."
        )
    if status == 403:
        return AuthenticationError(
            "HTTP 403 Forbidden: the account/token lacks permission for this "
            "grinder or the device is not bound to the account. Response body "
            "suppressed."
        )
    if status == 429:
        return RemoteServerError(
            "HTTP 429 Too Many Requests: the owner cloud rate-limited the "
            "request; wait before retrying. Response body suppressed."
        )
    if 500 <= status <= 599:
        return RemoteServerError(
            f"HTTP {status} {phrase}: owner cloud server error; retry later. "
            "Response body suppressed."
        )
    if 300 <= status <= 399:
        return UsageError(
            f"HTTP {status} {phrase}: redirect refused; check the configured "
            "base URL and endpoint path."
        )
    return UsageError(
        f"HTTP {status} {phrase}: request rejected; check the configured "
        "endpoint path and payload schema. Response body suppressed."
    )


def post_payload(
    url: str,
    token: str,
    payload: Mapping[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    ca_file: str | None = None,
) -> tuple[int, int]:
    """POST *payload* once and return ``(status, response_bytes)``.

    Response bodies are read only to a bounded size and are never returned to
    the caller, so a server echo cannot leak the token into local output.
    Redirects are refused and environment proxies are ignored.
    """

    data = json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    request = Request(url, data=data, headers=_headers(token), method="POST")

    context = ssl.create_default_context()
    if ca_file:
        try:
            context.load_verify_locations(cafile=ca_file)
        except (OSError, ssl.SSLError) as exc:
            raise UsageError(
                f"cannot load CA file {ca_file}: {_clean_reason(exc)}"
            ) from None

    opener = build_opener(
        ProxyHandler({}),
        _NoRedirectHandler(),
        HTTPSHandler(context=context),
    )

    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read(MAX_RESPONSE_BYTES)
            if not 200 <= status <= 299:
                raise _http_status_error(status)
            return status, len(body)
    except HTTPError as exc:
        status = int(exc.code)
        exc.close()
        raise _http_status_error(status) from None
    except (socket.timeout, TimeoutError):
        raise NetworkError(
            "request timed out before a response; no retry was attempted"
        ) from None
    except ssl.SSLCertVerificationError:
        raise NetworkError(
            "TLS certificate verification failed; use a valid certificate or "
            "provide --ca-file for a private test CA"
        ) from None
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise NetworkError(
                "TLS certificate verification failed; use a valid certificate "
                "or provide --ca-file for a private test CA"
            ) from None
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise NetworkError(
                "request timed out before a response; no retry was attempted"
            ) from None
        detail = _clean_reason(redact(reason, token))
        raise NetworkError(f"request failed: {detail}") from None
    except ValueError as exc:
        detail = _clean_reason(redact(exc, token))
        raise UsageError(f"request could not be built: {detail}") from None
    except (OSError, http.client.HTTPException) as exc:
        detail = _clean_reason(redact(exc, token))
        raise NetworkError(f"request failed: {detail}") from None


# ------------------------------------------------------------------------ CLI


def _resolve_token(args: argparse.Namespace) -> str:
    if args.token_file:
        if args.token_file == "-":
            raise UsageError(
                "--token-file - is not supported; use a file or "
                f"{ENV_TOKEN}"
            )
        raw = _read_text_file(args.token_file, what="token file")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if len(lines) != 1:
            raise UsageError("token file must contain exactly one non-empty line")
        token = lines[0]
    else:
        token = os.environ.get(ENV_TOKEN, "").strip()

    if not token:
        raise UsageError(
            "--send requires a bearer token via --token-file or "
            f"{ENV_TOKEN}"
        )
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise UsageError("bearer token is empty")
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in token):
        raise UsageError(
            "bearer token contains whitespace, control or non-ASCII characters"
        )
    return token


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))
    sys.stdout.flush()


def _run(args: argparse.Namespace) -> int:
    if args.send and not args.yes:
        raise UsageError(
            "--send posts a brew event to the cloud; review the dry run and "
            "add --yes to confirm"
        )
    timeout = args.timeout
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT_S:
        raise UsageError(
            f"--timeout must be greater than 0 and at most {MAX_TIMEOUT_S:g}"
        )

    payload = _payload_for(args)

    raw_base = (
        args.base_url
        if args.base_url is not None
        else os.environ.get(ENV_BASE_URL, "")
    )
    if args.endpoint_path is not None:
        endpoint_path = args.endpoint_path
    else:
        endpoint_path = (
            os.environ.get(ENV_ENDPOINT_PATH, "") or DEFAULT_ENDPOINT_PATH
        )
    url = build_endpoint_url(
        raw_base, endpoint_path, allow_http=args.allow_http
    )

    if not args.send:
        _emit({"dryRun": True, "request": request_preview(url, payload)})
        return 0

    token = _resolve_token(args)
    ca_file = args.ca_file or os.environ.get(ENV_CA_FILE) or None
    status, response_bytes = post_payload(
        url,
        token,
        payload,
        timeout=timeout,
        ca_file=ca_file,
    )
    _emit(
        {
            "sent": True,
            "status": status,
            "eventUuid": payload["eventUuid"],
            "grinderId": payload["grinderId"],
            "responseBytes": response_bytes,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--replay-event",
        metavar="FILE",
        help=(
            "scale_bridge.py JSON record/event, JSONL or concatenated JSON; "
            "the last value wins ('-' reads stdin)"
        ),
    )
    source.add_argument(
        "--stdin",
        action="store_true",
        help="read the event JSON from stdin",
    )
    parser.add_argument(
        "--duration-ms",
        type=float,
        help="explicit shot duration in milliseconds",
    )
    parser.add_argument(
        "--mass-g",
        type=float,
        help="explicit shot mass in grams",
    )
    parser.add_argument(
        "--grinder-id",
        help=f"owner-cloud grinder id (or {ENV_GRINDER_ID})",
    )
    parser.add_argument("--event-uuid", help="override the new event UUID")
    parser.add_argument("--grind-uuid", help="override the grind UUID")
    parser.add_argument("--dose-index", type=int, help="dose index (default 1)")
    parser.add_argument("--group-head", help="group head (default \"0\")")
    parser.add_argument(
        "--machine-name",
        help="machine label (default \"scale-bridge\")",
    )

    cloud = parser.add_argument_group("owner cloud")
    cloud.add_argument(
        "--base-url",
        help=(
            f"owner cloud base URL; required unless {ENV_BASE_URL} is set "
            "(no production default)"
        ),
    )
    cloud.add_argument(
        "--endpoint-path",
        help=(
            "brew-event endpoint path (default "
            f"{DEFAULT_ENDPOINT_PATH}; also {ENV_ENDPOINT_PATH})"
        ),
    )
    cloud.add_argument(
        "--allow-http",
        action="store_true",
        help="allow plain HTTP only for a loopback test fixture",
    )
    cloud.add_argument("--ca-file", help="PEM CA bundle for a private test CA")
    cloud.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"request timeout in seconds (default {DEFAULT_TIMEOUT_S:g})",
    )
    cloud.add_argument(
        "--token-file",
        metavar="FILE",
        help=f"file containing one bearer token (or {ENV_TOKEN}); never printed",
    )
    cloud.add_argument(
        "--send",
        action="store_true",
        help="perform the POST (default is a redacted dry run)",
    )
    cloud.add_argument(
        "--yes",
        action="store_true",
        help="confirm --send after reviewing the dry run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except OwnerCloudError as exc:
        print(f"owner-cloud-event: {redact(exc)}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        print(f"owner-cloud-event: {redact(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
