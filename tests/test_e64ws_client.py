"""Tests for the local E64 WSS client's login diagnostics."""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

from tools.e64ws_client import login_error_message
from tools.scale_bridge import _token_for


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


if __name__ == "__main__":
    unittest.main()
