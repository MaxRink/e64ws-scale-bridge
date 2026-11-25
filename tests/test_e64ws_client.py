"""Tests for the local E64 WSS client's login diagnostics."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from tools.e64ws_client import (
    _vendor_hash,
    login_error_message,
    main as client_main,
    no_password_payload,
    redact,
    run_request,
)
from tools.scale_bridge import _token_for


REQUEST = {"type": "RequestDriverState", "msgId": 1, "data": {}}
RESPONSE = {"type": "RequestDriverStateResult", "refId": 1, "data": {"ddValueDum": 1}}


class _FakeSocket:
    def __init__(self, frames: tuple[str, ...] = (), error: BaseException | None = None) -> None:
        self.frames = list(frames)
        self.error = error
        self.sent: list[str] = []
        self.closed = 0

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self) -> str:
        if self.error is not None:
            raise self.error
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed += 1


class _FakeWebSocketModule(types.ModuleType):
    """Just enough of ``websocket-client`` to drive ``run_request``."""

    def __init__(self, sockets: list[object]) -> None:
        super().__init__("websocket")

        class WebSocketException(Exception):
            pass

        class WebSocketTimeoutException(WebSocketException):
            pass

        class WebSocketConnectionClosedException(WebSocketException):
            pass

        self.WebSocketException = WebSocketException
        self.WebSocketTimeoutException = WebSocketTimeoutException
        self.WebSocketConnectionClosedException = WebSocketConnectionClosedException
        self.sockets = list(sockets)
        self.urls: list[str] = []

    def create_connection(self, url: str, timeout: float | None = None,
                          sslopt: dict | None = None) -> object:
        self.urls.append(url)
        item = self.sockets.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class RedactionTests(unittest.TestCase):
    """The WSS token goes in the query string and must never be printed."""

    TOKEN = "s3cr3t-token-value"
    URL = f"wss://192.0.2.1:9997/?token={TOKEN}"

    def _request(self, fake: _FakeWebSocketModule) -> dict:
        with mock.patch.dict(sys.modules, {"websocket": fake}):
            return run_request(
                "192.0.2.1", self.TOKEN, dict(REQUEST),
                insecure=True, ca_file=None, timeout=0.2,
            )

    def test_redact_removes_query_and_bearer_values(self) -> None:
        text = f"{self.URL} Authorization: Bearer abc123"
        cleaned = redact(text, self.TOKEN)
        self.assertNotIn(self.TOKEN, cleaned)
        self.assertNotIn("abc123", cleaned)
        self.assertIn("<redacted>", cleaned)
        # The query-string form is removed even without the explicit secret.
        self.assertNotIn(self.TOKEN, redact(text))

    def test_connect_failure_never_echoes_the_token(self) -> None:
        fake = _FakeWebSocketModule([OSError(f"handshake failed for {self.URL}")])
        with self.assertRaises(RuntimeError) as caught:
            self._request(fake)
        self.assertNotIn(self.TOKEN, str(caught.exception))
        self.assertIn("<redacted>", str(caught.exception))
        # No chained exception may carry the unredacted text either.
        self.assertIsNone(caught.exception.__cause__)

    def test_receive_failure_never_echoes_the_token(self) -> None:
        fake = _FakeWebSocketModule([])
        fake.sockets = [
            _FakeSocket(error=fake.WebSocketException(f"socket error on {self.URL}"))
        ]
        with self.assertRaises(RuntimeError) as caught:
            self._request(fake)
        self.assertNotIn(self.TOKEN, str(caught.exception))

    def test_timeout_message_never_echoes_the_token(self) -> None:
        fake = _FakeWebSocketModule([])
        fake.sockets = [_FakeSocket(error=fake.WebSocketTimeoutException("timed out"))]
        with self.assertRaises(RuntimeError) as caught:
            self._request(fake)
        self.assertNotIn(self.TOKEN, str(caught.exception))

    def test_cli_error_output_never_contains_the_token(self) -> None:
        fake = _FakeWebSocketModule([OSError(f"handshake failed for {self.URL}")])
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"websocket": fake}), \
                contextlib.redirect_stderr(output):
            code = client_main([
                "--host", "192.0.2.1", "--token", self.TOKEN,
                "--request", "RequestDriverState", "--insecure", "--timeout", "0.2",
            ])
        self.assertEqual(code, 2)
        self.assertNotIn(self.TOKEN, output.getvalue())
        self.assertIn("<redacted>", output.getvalue())

    def test_the_query_string_still_carries_the_token(self) -> None:
        fake = _FakeWebSocketModule([])
        good = _FakeSocket(frames=(json.dumps(RESPONSE),))
        fake.sockets = [good]
        self._request(fake)
        self.assertEqual(fake.urls, [self.URL])


class ReceiveTimeoutTests(unittest.TestCase):
    """A silent socket must end in a message and exit 2, not a traceback."""

    TOKEN = "s3cr3t-token-value"

    def _request(self, fake: _FakeWebSocketModule) -> dict:
        with mock.patch.dict(sys.modules, {"websocket": fake}):
            return run_request(
                "192.0.2.1", self.TOKEN, dict(REQUEST),
                insecure=True, ca_file=None, timeout=0.2,
            )

    def test_recv_timeout_becomes_a_bounded_error(self) -> None:
        fake = _FakeWebSocketModule([])
        sock = _FakeSocket(error=fake.WebSocketTimeoutException("timed out"))
        fake.sockets = [sock]
        with self.assertRaises(RuntimeError) as caught:
            self._request(fake)
        self.assertIn("no WSS response", str(caught.exception))
        self.assertEqual(sock.closed, 1)

    def test_cli_exits_2_with_a_message_instead_of_a_traceback(self) -> None:
        fake = _FakeWebSocketModule([])
        sock = _FakeSocket(error=fake.WebSocketTimeoutException("timed out"))
        fake.sockets = [sock]
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"websocket": fake}), \
                contextlib.redirect_stderr(output):
            code = client_main([
                "--host", "192.0.2.1", "--token", self.TOKEN,
                "--request", "RequestDriverState", "--insecure", "--timeout", "0.2",
            ])
        self.assertEqual(code, 2)
        text = output.getvalue()
        self.assertIn("e64ws-client:", text)
        self.assertNotIn("Traceback", text)

    def test_the_next_attempt_reconnects_and_succeeds(self) -> None:
        fake = _FakeWebSocketModule([])
        stale = _FakeSocket(error=fake.WebSocketTimeoutException("timed out"))
        good = _FakeSocket(frames=(json.dumps(RESPONSE),))
        fake.sockets = [stale, good]
        with self.assertRaises(RuntimeError):
            self._request(fake)
        response = self._request(fake)
        self.assertEqual(response["type"], "RequestDriverStateResult")
        self.assertEqual(stale.closed, 1)
        self.assertEqual(good.closed, 1)
        self.assertEqual(len(fake.urls), 2)


class LoginErrorMessageTests(unittest.TestCase):
    def test_generic_error_is_unchanged(self) -> None:
        message = login_error_message(500, "boom")
        self.assertEqual(message, "login failed with HTTP 500: boom")

    def test_empty_owner_password_gets_the_hsg_hint(self) -> None:
        detail = '{\n    "console": "Wrong password!",\n    "success": false\n}'
        message = login_error_message(400, detail)
        self.assertIn("Wrong password!", message)
        self.assertIn("Role.Owner.LoginData", message)
        self.assertIn("--token", message)

    def test_other_400_does_not_get_the_hint(self) -> None:
        message = login_error_message(400, "Missing parameter!")
        self.assertNotIn("Role.Owner.LoginData", message)

    def test_token_file_is_read_and_mode_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("abcd\n")
            os.chmod(path, 0o600)
            args = SimpleNamespace(token=None, token_file=path, login_role=None)
            self.assertEqual(_token_for(args), "abcd")

    def test_missing_token_file_without_login_is_rejected(self) -> None:
        args = SimpleNamespace(token=None, token_file=None, login_role=None)
        with self.assertRaises(ValueError):
            _token_for(args)

    def test_no_password_payload_is_select_only_and_verifiable(self) -> None:
        role, password, encoded = no_password_payload()
        self.assertTrue(role.startswith('x" UNION SELECT "1$1000$'))
        self.assertTrue(role.endswith('" -- '))
        self.assertNotIn("'", role)
        _, rounds, salt, expected = encoded.split("$", 3)
        self.assertEqual(_vendor_hash(password, salt, int(rounds)), expected)


if __name__ == "__main__":
    unittest.main()
