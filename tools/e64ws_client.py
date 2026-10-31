#!/usr/bin/env python3
"""Small owner-controlled local WSS client for E64 WS.

The optional dependency is websocket-client. The client deliberately verifies
TLS by default. Because the supplied image uses a self-signed certificate,
--insecure or --ca-file is required when the local certificate is not trusted.
The token is accepted through an argument or obtained from the local login
route; it is never printed by this program.
"""

from __future__ import annotations

import argparse
import getpass
import json
import ssl
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

try:  # script mode (tools/ on sys.path) and package mode (repo root)
    from e64ws_protocol import (
        ACTION_SUFFIXES,
        E64WSProtocol,
        HAZARDOUS_ACTIONS,
        HAZARDOUS_DIRECT_COMMANDS,
    )
except ModuleNotFoundError:  # pragma: no cover - package import path
    from tools.e64ws_protocol import (
        ACTION_SUFFIXES,
        E64WSProtocol,
        HAZARDOUS_ACTIONS,
        HAZARDOUS_DIRECT_COMMANDS,
    )


def _https_context(*, insecure: bool, ca_file: str | None) -> ssl.SSLContext:
    if insecure and ca_file:
        raise ValueError("choose --insecure or --ca-file, not both")
    if insecure:
        context = ssl._create_unverified_context()
        context.check_hostname = False
        return context
    return ssl.create_default_context(cafile=ca_file)


def login(host: str, role: str, password: str, *, insecure: bool, ca_file: str | None) -> str:
    body = json.dumps({"userRole": role, "password": password}).encode()
    request = urllib.request.Request(
        f"https://{host}/rest_api/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    jar = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=_https_context(insecure=insecure, ca_file=ca_file)),
    )
    try:
        with opener.open(request, timeout=10) as response:
            authorization = next(
                (
                    value
                    for key, value in response.headers.items()
                    if key.lower() == "authorization"
                ),
                "",
            )
            if not authorization.startswith("Bearer "):
                raise RuntimeError(f"login returned no bearer token (HTTP {response.status})")
            return authorization.removeprefix("Bearer ").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"login failed with HTTP {exc.code}: {detail}") from exc


def run_request(
    host: str,
    token: str,
    message: dict[str, Any],
    *,
    insecure: bool,
    ca_file: str | None,
    timeout: float,
    allow_hazardous: bool = False,
    wss_port: int = 9997,
) -> dict[str, Any]:
    command_type = str(message.get("type", ""))
    if command_type == "GrinderAction":
        data = message.get("data", {})
        suffix = data.get("actionType") if isinstance(data, dict) else None
        if suffix not in ACTION_SUFFIXES:
            raise RuntimeError("unknown recovered GrinderAction actionType")
        if suffix in HAZARDOUS_ACTIONS and not allow_hazardous:
            raise RuntimeError(
                "physical/maintenance action is guarded; pass allow_hazardous=True only for an isolated owner-controlled test"
            )
    elif command_type.startswith("GrinderAction"):
        raise RuntimeError("legacy GrinderAction<suffix> envelope is not the recovered wire format")
    elif command_type in HAZARDOUS_DIRECT_COMMANDS and not allow_hazardous:
        raise RuntimeError(
            "direct maintenance command is guarded; pass --allow-hazardous only on an isolated owner-controlled test machine"
        )
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("install optional dependency with: python3 -m pip install -r tools/requirements.txt") from exc

    protocol = E64WSProtocol(next_msg_id=int(message.get("msgId", 1)) + 1)
    url = f"wss://{host}:{wss_port}/?token={token}"
    sslopt: dict[str, Any] = {}
    if insecure:
        sslopt.update(cert_reqs=ssl.CERT_NONE, check_hostname=False)
    elif ca_file:
        sslopt.update(cert_reqs=ssl.CERT_REQUIRED, ca_certs=ca_file)
    else:
        sslopt.update(cert_reqs=ssl.CERT_REQUIRED, check_hostname=True)
    socket = websocket.create_connection(url, timeout=timeout, sslopt=sslopt)
    try:
        socket.send(json.dumps(message, separators=(",", ":")))
        while True:
            response = json.loads(socket.recv())
            if E64WSProtocol.matches_response(message, response):
                return response
            if response == {"pong": True}:
                continue
            # Keep waiting for the matching refId, but surface unrelated
            # messages in a deterministic way if the server emits them.
            if "type" in response and response.get("type") == "UnknownType":
                return response
    finally:
        socket.close()


def parse_json(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="local grinder hostname or IP, without a scheme")
    parser.add_argument("--token", help="existing local WSS bearer token; never printed")
    parser.add_argument("--login-role", help="login locally and obtain a new token")
    parser.add_argument("--password", help="avoid this on shell history; prefer the prompt")
    parser.add_argument("--insecure", action="store_true", help="accept the image's self-signed certificate")
    parser.add_argument("--ca-file", help="CA/certificate file to trust instead of disabling verification")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--request", help="exact recovered non-action command type")
    parser.add_argument("--action", help="recovered action suffix, for example StartGrindGbw")
    parser.add_argument("--data", help="JSON object for data")
    parser.add_argument("--action-type", help="optional exact firmware policy value")
    parser.add_argument("--allow-hazardous", action="store_true", help="allow physical/maintenance action commands")
    args = parser.parse_args(argv)

    if bool(args.request) == bool(args.action):
        parser.error("choose exactly one of --request or --action")
    if args.request == "GbsCreateReport":
        parser.error("use --action GbsCreateReport; it is a GrinderAction variant")
    if args.request == "GrinderAction" or (args.request and args.request.startswith("GrinderAction")):
        parser.error("use --action for GrinderAction commands so the hazardous-operation guard is applied")
    data = parse_json(args.data)
    if data is not None and not isinstance(data, dict):
        parser.error("--data must be a JSON object")
    if args.action in HAZARDOUS_ACTIONS and not args.allow_hazardous:
        parser.error("action is guarded; add --allow-hazardous only on an isolated owner-controlled test machine")
    if args.request in HAZARDOUS_DIRECT_COMMANDS and not args.allow_hazardous:
        parser.error("direct maintenance command is guarded; add --allow-hazardous only on an isolated owner-controlled test machine")

    token = args.token
    if not token:
        if not args.login_role:
            parser.error("provide --token or --login-role")
        password = args.password if args.password is not None else getpass.getpass("local grinder password: ")
        token = login(args.host, args.login_role, password, insecure=args.insecure, ca_file=args.ca_file)

    protocol = E64WSProtocol()
    if args.request:
        message = protocol.request(args.request, data)
    else:
        message = protocol.action(args.action, data, action_type=args.action_type)
    response = run_request(
        args.host,
        token,
        message,
        insecure=args.insecure,
        ca_file=args.ca_file,
        timeout=args.timeout,
        allow_hazardous=args.allow_hazardous,
    )
    json.dump(response, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
