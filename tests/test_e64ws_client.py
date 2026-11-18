"""Tests for the local E64 WSS client's login diagnostics."""
from __future__ import annotations

import unittest

from tools.e64ws_client import login_error_message


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


if __name__ == "__main__":
    unittest.main()
