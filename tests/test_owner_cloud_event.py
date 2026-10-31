import contextlib
import io
import json
import os
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from tools import owner_cloud_event as cloud


# Test-only self-signed certificate for 127.0.0.1/localhost.  It is not a
# production credential and is never used outside the loopback fixture.
TEST_CERT_PEM = """\
-----BEGIN CERTIFICATE-----
MIIDBjCCAe6gAwIBAgIUQw0hf1Ljrc9mFbAbH1j9yUtL0aMwDQYJKoZIhvcNAQEL
BQAwFDESMBAGA1UEAwwJMTI3LjAuMC4xMCAXDTIwMDEwMTAwMDAwMFoYDzIxMjYw
MTAxMDAwMDAwWjAUMRIwEAYDVQQDDAkxMjcuMC4wLjEwggEiMA0GCSqGSIb3DQEB
AQUAA4IBDwAwggEKAoIBAQDFZetwFQVvJ1D1sf6OLCngvsKdliKoCzJlcphP93R+
4gypbRf+cNyzQ/Rr22C4sD1YRYZcyu58DB8bVPRxYE57g8rYIQWbkH0GV5zjJyoO
7i6baX6Qr/AjEdOqEF6efYGUAg0jHEa+Yd99Dru7qIit4g/NHRi89Yg4iohsojmw
h247VSxVuTHfY+GkSd3IKve0v992fio+IM4wANyuM7sFErfbUQEE6oThciRWgUln
4XxmcJTJ6lYNJk0l22mMuVEG/73NP8N7D3oE8pWneHmHWbx/wvKvwhYS0ac4WciP
ace2Yld0xFuHiAacborb+bGaO5FBAnbcnwsMU8MPxMQtAgMBAAGjTjBMMB0GA1Ud
DgQWBBTB0Iq9J1FKhdxoSNYtyjWqxexYYjAPBgNVHRMBAf8EBTADAQH/MBoGA1Ud
EQQTMBGHBH8AAAGCCWxvY2FsaG9zdDANBgkqhkiG9w0BAQsFAAOCAQEAnnrLR8QZ
XPAh+EsDbiIExK+CLJiktFeh/oGXnsIimrjQHN3IkvQfCABp/tgGniBo5noIBfaI
HYQ3t0HWEVxScNf11h3p1nuvEzpKfINsJzA//1Jq5gg9HQxD8MiqhZpnOHxTGbsn
ojKDX26j5v2Sry+9f6w6l/Yc0CVHLuBBoFAnP36do0jLKqorWTDrz8svCy932bNu
LG9Rw0V5FvUqUMOq8SMWMLJ4cqwI7j0t9I0TMNDPGy3VZtBnTeNpYbFqpedKxgF7
EVtfHuBoTuawgXBeU5ivWrHaEWilrTwOZX6RFo0aoVUWzSMb3yH2y+LPrM/t7Axg
T9cmsl4H0/epfw==
-----END CERTIFICATE-----
"""

TEST_KEY_PEM = """\
-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQDFZetwFQVvJ1D1
sf6OLCngvsKdliKoCzJlcphP93R+4gypbRf+cNyzQ/Rr22C4sD1YRYZcyu58DB8b
VPRxYE57g8rYIQWbkH0GV5zjJyoO7i6baX6Qr/AjEdOqEF6efYGUAg0jHEa+Yd99
Dru7qIit4g/NHRi89Yg4iohsojmwh247VSxVuTHfY+GkSd3IKve0v992fio+IM4w
ANyuM7sFErfbUQEE6oThciRWgUln4XxmcJTJ6lYNJk0l22mMuVEG/73NP8N7D3oE
8pWneHmHWbx/wvKvwhYS0ac4WciPace2Yld0xFuHiAacborb+bGaO5FBAnbcnwsM
U8MPxMQtAgMBAAECggEADmaCB3oxHEvE9zeV9283WuLvtalXO+UdNxNmEyddQ0hg
XhfMUU6Qey9w7FFju/V3dDIWPDy9qyUDO+41qvO4pgAWUzANhazDwiuwEU4xHke3
9LR8fDepkBVj2Js7G9rSEicYKECO1KK1OB1UCrufxPeh6Xe+70IGPwG0NCCdjsgh
rAad63gdelpH2gNFtj+0XGnbXO+kKtggm64+LNxW//IMSP2DrVsD8zGxkcSY/lK6
k/oSsOU8FOKlmxYVZEQz5bayGIT2H1JeRCmckL6pDt5VSgJr8bbTVObRytE0D1FC
MjXJVEKHCy0MydLRgexggLwasV6/2q3MQTGzEZM1wQKBgQDoqlJRhFZAnhEXatIZ
oP3VD4zOdNi+Waoj7YdC2QSpISdKnfq0ElkNnZ/OPTGrslAGt8XM9ss+vUEFO3N8
1akN6NHKlY2l2ZYy4XhIQsqH2KXURfLMVtr1blCATk/6PZNqBs6AwQle9tKviG3L
PseAgmUa7FTfOYznDhzBhKIBUQKBgQDZMh0Kk2ESNsmz/MtdepPbJ/IFWDqKHpkS
CmvLP989yDvyTC4NZXB3Mz3eL68ZngXtDOztQSk2XKdo6fDQel1pg6tquBgjgW1p
s06SBO2sa4k5HUn3wW+BQL3V0AP54K3btNNr7ifvWv9XdbDJpA5Y3wwXegdt5vwj
FUubXUk+HQKBgBL2/yrB3L37pG0COgn4w3gAgnu5rokliebPz7y1L9oRZ8B/TBYt
gLDJbmNDPa8/I6LIjAj+/OYqSwiAJhPA3rzRg3g95IPmToXrNwC84QFduXaqFPZv
Bf4HdexfdclyB3gXAfIgygDBob7JIhn1DhH9jvw401o2uLOExPdJNmNhAoGAH4HQ
iq80frLp8a0zRx04A6u4xSO2YkDsDZzrZDGgzdKVN6HrcFANz4zkZa3fJcUxsOCW
Ihi4xMUOF/8Pyv5lPKA4JqQMBGKLH7E+LZp/a+qxnlJsX391EHf20eR6Hy6DqgKC
RP7oViWKMwLEoYul9ShuFZ3DJCNE4kX9kM0oidUCgYAar/hoGO8Y4vV1BwjxRJJ/
uh5dYpPifIzP0eXsGubVkFJ83ncvPPP2nLy33FUQLMDKdYEQWWYP0GLLqRNBL/RQ
6tKCb5ohU7Hiin6G0BPlwgtsW9C55bMl5IxXIRm+crCa0+9ahOA82ACsJRxH29xy
r+swXMo0KQFkqlRad6B4+w==
-----END PRIVATE KEY-----
"""


class _RecordingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.requests.append(  # type: ignore[attr-defined]
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                }
            )
            status = self.server.status  # type: ignore[attr-defined]
            payload = self.server.response_body  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)


class _LoopbackServer:
    """Tiny recording HTTP(S) server; tests never leave the loopback interface."""

    def __init__(self, tls_context: ssl.SSLContext | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.status = 200
        self.response_body = b'{"ok":true}'
        self.scheme = "https" if tls_context is not None else "http"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        self.httpd.daemon_threads = True
        if tls_context is not None:
            self.httpd.socket = tls_context.wrap_socket(
                self.httpd.socket, server_side=True
            )
        self.httpd.requests = self.requests  # type: ignore[attr-defined]
        self.httpd.lock = self.lock  # type: ignore[attr-defined]
        self.httpd.status = self.status  # type: ignore[attr-defined]
        self.httpd.response_body = self.response_body  # type: ignore[attr-defined]
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"{self.scheme}://{host}:{port}"

    def start(self) -> "_LoopbackServer":
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()
        return self

    def reset(self) -> None:
        with self.lock:
            self.requests.clear()
            self.status = 200
            self.response_body = b'{"ok":true}'
            self.httpd.status = 200
            self.httpd.response_body = b'{"ok":true}'

    def set_response(self, status: int, body: bytes) -> None:
        with self.lock:
            self.status = status
            self.response_body = body
            self.httpd.status = status
            self.httpd.response_body = body

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


class OwnerCloudEventTests(unittest.TestCase):
    TOKEN = "owner-test-token-4f8a2c"
    EVENT_UUID = "event-test-0001"
    GRINDER_ID = "owner-grinder-1"

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _LoopbackServer().start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.close()

    def setUp(self) -> None:
        self.server.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.token_file = self.dir / "token.txt"
        self.token_file.write_text(self.TOKEN + "\n", encoding="utf-8")
        env = {
            cloud.ENV_BASE_URL: "",
            cloud.ENV_ENDPOINT_PATH: "",
            cloud.ENV_TOKEN: "",
            cloud.ENV_GRINDER_ID: "",
            cloud.ENV_CA_FILE: "",
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ------------------------------------------------------------- helpers

    def write_json(self, name: str, value: Any) -> str:
        path = self.dir / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def write_text(self, name: str, value: str) -> str:
        path = self.dir / name
        path.write_text(value, encoding="utf-8")
        return str(path)

    def run_cli(
        self, argv: list[str], *, stdin: str | None = None
    ) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        stdin_context = (
            mock.patch("sys.stdin", io.StringIO(stdin))
            if stdin is not None
            else contextlib.nullcontext()
        )
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with stdin_context:
                code = cloud.main(argv)
        return code, out.getvalue(), err.getvalue()

    def event_record(self) -> dict[str, Any]:
        return {
            "shot": {
                "duration_ms": 24600.0,
                "mass_g": 18.0,
                "valid": True,
            },
            "event": {
                "grindUuid": "grind-uuid-1",
                "machineName": "scale-bridge",
                "groupHead": "0",
                "doseIndex": 2,
                "validity": True,
                "brewTimeMs": 24600,
                "brewWeightMg": 18000,
            },
        }

    def send_args(self, *extra: str) -> list[str]:
        return [
            "--base-url",
            self.server.base_url,
            "--allow-http",
            "--token-file",
            str(self.token_file),
        ] + list(extra)

    # ------------------------------------------------------------- payload

    def test_payload_keys_and_units_from_scale_bridge_record(self) -> None:
        payload = cloud.payload_from_record(
            self.event_record(),
            grinder_id=self.GRINDER_ID,
            event_uuid=self.EVENT_UUID,
        )
        self.assertEqual(
            set(payload),
            {
                "eventUuid",
                "grinderId",
                "grindUuid",
                "brewTimeMs",
                "brewWeightMg",
                "validity",
                "doseIndex",
                "groupHead",
                "machineName",
            },
        )
        self.assertEqual(payload["eventUuid"], self.EVENT_UUID)
        self.assertEqual(payload["grinderId"], self.GRINDER_ID)
        self.assertEqual(payload["grindUuid"], "grind-uuid-1")
        self.assertEqual(payload["brewTimeMs"], 24600)
        self.assertEqual(payload["brewWeightMg"], 18000)
        self.assertIs(payload["validity"], True)
        self.assertEqual(payload["doseIndex"], 2)
        self.assertEqual(payload["groupHead"], "0")
        self.assertEqual(payload["machineName"], "scale-bridge")
        self.assertIsInstance(payload["brewTimeMs"], int)
        self.assertIsInstance(payload["brewWeightMg"], int)

    def test_explicit_fields_convert_units_and_derive_validity(self) -> None:
        payload = cloud.build_brew_payload(
            duration_ms=26900.4,
            mass_g=18.4,
            grinder_id=self.GRINDER_ID,
            event_uuid=self.EVENT_UUID,
            grind_uuid="grind-explicit",
            dose_index=3,
            group_head=1,
            machine_name="kitchen-e64",
        )
        self.assertEqual(payload["brewTimeMs"], 26900)
        self.assertEqual(payload["brewWeightMg"], 18400)
        self.assertIs(payload["validity"], True)
        self.assertEqual(payload["doseIndex"], 3)
        self.assertEqual(payload["groupHead"], "1")
        self.assertEqual(payload["machineName"], "kitchen-e64")

        short = cloud.build_brew_payload(
            duration_ms=9000,
            mass_g=12.0,
            grinder_id=self.GRINDER_ID,
        )
        self.assertIs(short["validity"], False)

    def test_bare_event_and_last_jsonl_value_are_accepted(self) -> None:
        first = {
            "brewTimeMs": 20000,
            "brewWeightMg": 18000,
            "grindUuid": "grind-first",
        }
        last = {
            "brewTimeMs": 21000,
            "brewWeightMg": 19000,
            "grindUuid": "grind-last",
        }
        record = cloud.load_event(
            json.dumps(first) + "\n" + json.dumps(last) + "\n",
            source="fixture",
        )
        payload = cloud.payload_from_record(
            record,
            grinder_id=self.GRINDER_ID,
            event_uuid=self.EVENT_UUID,
        )
        self.assertEqual(payload["grindUuid"], "grind-last")
        self.assertEqual(payload["brewTimeMs"], 21000)

    # ------------------------------------------------------------- dry run

    def test_dry_run_prints_redacted_shape_and_sends_nothing(self) -> None:
        event_file = self.write_json("event.json", self.event_record())
        # A nonexistent token file proves dry-run does not read the token.
        code, out, err = self.run_cli(
            [
                "--replay-event",
                event_file,
                "--base-url",
                self.server.base_url,
                "--allow-http",
                "--grinder-id",
                self.GRINDER_ID,
                "--event-uuid",
                self.EVENT_UUID,
                "--token-file",
                str(self.dir / "does-not-exist"),
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.requests, [])
        document = json.loads(out)
        self.assertIs(document["dryRun"], True)
        request = document["request"]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(
            request["url"], self.server.base_url + "/brew-events"
        )
        self.assertEqual(
            request["headers"]["Authorization"], "Bearer <redacted>"
        )
        self.assertEqual(request["body"]["eventUuid"], self.EVENT_UUID)
        self.assertNotIn(self.TOKEN, out + err)
        self.assertNotIn("Bearer " + self.TOKEN, out + err)

    def test_stdin_event_is_supported(self) -> None:
        code, out, err = self.run_cli(
            [
                "--stdin",
                "--base-url",
                self.server.base_url,
                "--allow-http",
                "--grinder-id",
                self.GRINDER_ID,
                "--event-uuid",
                self.EVENT_UUID,
            ],
            stdin=json.dumps(self.event_record()),
        )
        self.assertEqual(code, 0, err)
        document = json.loads(out)
        self.assertEqual(
            document["request"]["body"]["brewWeightMg"], 18000
        )
        self.assertEqual(self.server.requests, [])

    # ---------------------------------------------------------------- send

    def test_send_posts_once_with_bearer_header(self) -> None:
        event_file = self.write_json("event.json", self.event_record())
        code, out, err = self.run_cli(
            self.send_args(
                "--replay-event",
                event_file,
                "--grinder-id",
                self.GRINDER_ID,
                "--event-uuid",
                self.EVENT_UUID,
                "--send",
                "--yes",
            )
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.server.requests), 1)
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/brew-events")
        self.assertEqual(
            request["headers"].get("Authorization"),
            "Bearer " + self.TOKEN,
        )
        self.assertEqual(
            request["headers"].get("Content-Type"), "application/json"
        )
        body = json.loads(request["body"].decode("utf-8"))
        self.assertEqual(body["eventUuid"], self.EVENT_UUID)
        self.assertEqual(body["grinderId"], self.GRINDER_ID)
        self.assertEqual(body["grindUuid"], "grind-uuid-1")
        self.assertEqual(body["brewTimeMs"], 24600)
        self.assertEqual(body["brewWeightMg"], 18000)
        self.assertNotIn(self.TOKEN, out + err)
        result = json.loads(out)
        self.assertIs(result["sent"], True)
        self.assertEqual(result["status"], 200)

    def test_https_loopback_with_ca_file_posts_once(self) -> None:
        cert_file = self.write_text("server-cert.pem", TEST_CERT_PEM)
        key_file = self.write_text("server-key.pem", TEST_KEY_PEM)
        ca_file = self.write_text("ca.pem", TEST_CERT_PEM)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        tls_server = _LoopbackServer(tls_context=context).start()
        self.addCleanup(tls_server.close)

        event_file = self.write_json("event.json", self.event_record())
        code, out, err = self.run_cli(
            [
                "--base-url",
                tls_server.base_url,
                "--ca-file",
                ca_file,
                "--token-file",
                str(self.token_file),
                "--replay-event",
                event_file,
                "--grinder-id",
                self.GRINDER_ID,
                "--event-uuid",
                self.EVENT_UUID,
                "--send",
                "--yes",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(tls_server.requests), 1)
        self.assertEqual(
            tls_server.requests[0]["headers"].get("Authorization"),
            "Bearer " + self.TOKEN,
        )
        self.assertNotIn(self.TOKEN, out + err)

    def test_env_token_and_base_url_are_used(self) -> None:
        event_file = self.write_json("event.json", self.event_record())
        with mock.patch.dict(
            os.environ,
            {
                cloud.ENV_TOKEN: self.TOKEN,
                cloud.ENV_BASE_URL: self.server.base_url,
                cloud.ENV_GRINDER_ID: self.GRINDER_ID,
            },
        ):
            code, out, err = self.run_cli(
                [
                    "--allow-http",
                    "--endpoint-path",
                    "/owner/brew",
                    "--replay-event",
                    event_file,
                    "--event-uuid",
                    self.EVENT_UUID,
                    "--send",
                    "--yes",
                ]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["path"], "/owner/brew")
        self.assertEqual(
            self.server.requests[0]["headers"].get("Authorization"),
            "Bearer " + self.TOKEN,
        )
        self.assertNotIn(self.TOKEN, out + err)

    # ------------------------------------------------------------ failures

    def test_error_statuses_do_not_leak_token_or_body(self) -> None:
        event_file = self.write_json("event.json", self.event_record())
        for status, expected_code in (
            (401, 3),
            (403, 3),
            (500, 4),
            (302, 2),
            (404, 2),
        ):
            with self.subTest(status=status):
                self.server.reset()
                self.server.set_response(
                    status,
                    json.dumps(
                        {
                            "error": "echo " + self.TOKEN,
                            "secret": "body-secret-marker",
                        }
                    ).encode("utf-8"),
                )
                code, out, err = self.run_cli(
                    self.send_args(
                        "--replay-event",
                        event_file,
                        "--grinder-id",
                        self.GRINDER_ID,
                        "--event-uuid",
                        self.EVENT_UUID,
                        "--send",
                        "--yes",
                    )
                )
                self.assertEqual(code, expected_code, err)
                self.assertEqual(len(self.server.requests), 1)
                combined = out + err
                self.assertIn(str(status), err)
                self.assertNotIn(self.TOKEN, combined)
                self.assertNotIn("body-secret-marker", combined)
                self.assertNotIn("echo", combined)

    def test_send_requires_yes_and_sends_nothing(self) -> None:
        event_file = self.write_json("event.json", self.event_record())
        code, out, err = self.run_cli(
            self.send_args(
                "--replay-event",
                event_file,
                "--grinder-id",
                self.GRINDER_ID,
                "--send",
            )
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.server.requests, [])
        self.assertIn("--yes", err)

    def test_send_requires_a_token(self) -> None:
        event_file = self.write_json("event.json", self.event_record())
        code, out, err = self.run_cli(
            [
                "--replay-event",
                event_file,
                "--base-url",
                self.server.base_url,
                "--allow-http",
                "--grinder-id",
                self.GRINDER_ID,
                "--send",
                "--yes",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.server.requests, [])
        self.assertIn("token", err.lower())

    def test_malformed_inputs_are_rejected_without_network(self) -> None:
        good_event = self.event_record()
        missing_mass = {
            "event": {
                "grindUuid": "grind-1",
                "brewTimeMs": 24600,
            }
        }
        cases = [
            ("bad-json", ["--replay-event", self.write_text("bad.json", "{no")]),
            (
                "missing-mass",
                ["--replay-event", self.write_json("missing.json", missing_mass)],
            ),
            (
                "nan-json",
                [
                    "--replay-event",
                    self.write_text(
                        "nan.json",
                        '{"brewTimeMs": 20000, "brewWeightMg": NaN}',
                    ),
                ],
            ),
            ("zero-duration", ["--duration-ms", "0", "--mass-g", "18"]),
            ("nan-mass", ["--duration-ms", "20000", "--mass-g", "nan"]),
            (
                "missing-fields",
                ["--duration-ms", "20000"],
            ),
        ]
        for name, input_args in cases:
            with self.subTest(case=name):
                self.server.reset()
                code, out, err = self.run_cli(
                    [
                        *input_args,
                        "--base-url",
                        self.server.base_url,
                        "--allow-http",
                        "--grinder-id",
                        self.GRINDER_ID,
                    ]
                )
                self.assertEqual(code, 2, f"{name}: {err}")
                self.assertEqual(self.server.requests, [])
                self.assertTrue(err.strip())

        # The good event still parses, so the failures above are input-specific.
        payload = cloud.payload_from_record(
            good_event,
            grinder_id=self.GRINDER_ID,
            event_uuid=self.EVENT_UUID,
        )
        self.assertEqual(payload["brewTimeMs"], 24600)

    def test_bad_event_or_token_inputs_do_not_echo_secrets(self) -> None:
        event_file = self.write_json("event.json", self.event_record())
        multi_line = self.write_text(
            "multi-token.txt", "first-token\nsecond-token\n"
        )
        code, out, err = self.run_cli(
            [
                "--base-url",
                self.server.base_url,
                "--allow-http",
                "--token-file",
                multi_line,
                "--replay-event",
                event_file,
                "--grinder-id",
                self.GRINDER_ID,
                "--send",
                "--yes",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.server.requests, [])
        self.assertNotIn("first-token", out + err)
        self.assertNotIn("second-token", out + err)

    # --------------------------------------------------------------- safety

    def test_no_production_host_default(self) -> None:
        self.assertEqual(cloud.DEFAULT_BASE_URL, "")
        self.assertNotIn("://", cloud.DEFAULT_ENDPOINT_PATH)
        parser = cloud.build_parser()
        args = parser.parse_args([])
        self.assertIsNone(args.base_url)
        self.assertFalse(args.send)
        self.assertFalse(args.yes)

    def test_missing_base_url_is_a_usage_error(self) -> None:
        code, out, err = self.run_cli(
            [
                "--duration-ms",
                "20000",
                "--mass-g",
                "18",
                "--grinder-id",
                self.GRINDER_ID,
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("no base URL", err)
        self.assertEqual(self.server.requests, [])

    def test_url_validation_requires_https_except_loopback_fixture(self) -> None:
        self.assertEqual(
            cloud.validate_base_url("https://cloud.example.invalid"),
            "https://cloud.example.invalid",
        )
        self.assertEqual(
            cloud.validate_base_url("https://cloud.example.invalid/"),
            "https://cloud.example.invalid",
        )
        self.assertEqual(
            cloud.validate_base_url(
                "http://127.0.0.1:8443", allow_http=True
            ),
            "http://127.0.0.1:8443",
        )
        self.assertEqual(
            cloud.validate_base_url("http://localhost", allow_http=True),
            "http://localhost",
        )
        self.assertEqual(
            cloud.validate_base_url("http://[::1]:8080", allow_http=True),
            "http://[::1]:8080",
        )

        rejected = [
            ("ftp://127.0.0.1", False),
            ("file:///tmp/event.json", False),
            ("https://user:pass@cloud.example.invalid", False),
            ("https://cloud.example.invalid/path?token=x", False),
            ("https://cloud.example.invalid/#frag", False),
            ("http://127.0.0.1:8080", False),
            ("http://192.0.2.10", True),
            ("http://127.0.0.1:bad", True),
            ("", False),
        ]
        for url, allow_http in rejected:
            with self.subTest(url=url, allow_http=allow_http):
                with self.assertRaises(cloud.UsageError):
                    cloud.validate_base_url(url, allow_http=allow_http)

    def test_http_non_loopback_is_rejected_at_the_cli(self) -> None:
        code, out, err = self.run_cli(
            [
                "--duration-ms",
                "20000",
                "--mass-g",
                "18",
                "--grinder-id",
                self.GRINDER_ID,
                "--base-url",
                "http://192.0.2.10",
                "--allow-http",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("loopback", err)
        self.assertEqual(self.server.requests, [])

    def test_redaction_replaces_bearer_values(self) -> None:
        text = "Authorization: Bearer " + self.TOKEN
        self.assertNotIn(self.TOKEN, cloud.redact(text, self.TOKEN))
        self.assertNotIn(self.TOKEN, cloud.redact(text))
        self.assertIn("<redacted>", cloud.redact(text))

    def test_endpoint_path_must_be_a_path(self) -> None:
        for path in ("brew-events", "https://evil.invalid/x", "/x?token=1", "/x#f", "/x\\y"):
            with self.subTest(path=path):
                with self.assertRaises(cloud.UsageError):
                    cloud.build_endpoint_url(
                        "https://cloud.example.invalid", path
                    )


if __name__ == "__main__":
    unittest.main()
