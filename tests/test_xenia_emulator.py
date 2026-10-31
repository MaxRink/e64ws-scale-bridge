import http.client
import json
import unittest

from tools.xenia_emulator import XeniaServer


def request(port: int, method: str, path: str, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        return response.status, json.loads(payload) if payload else None
    finally:
        conn.close()


class XeniaEmulatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = XeniaServer(port=0, control_port=0).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.close()

    def setUp(self) -> None:
        self.server.state.idle()

    def test_machine_endpoint(self) -> None:
        status, value = request(self.server.port, "GET", "/api/v2/machine")
        self.assertEqual(status, 200)
        self.assertEqual(value["MA_TYPE"], "XENIA")
        self.assertEqual(value["MA_SN"], "VG-XENIA-0001")
        self.assertEqual(value["ma_type"], "xenia")

    def test_idle_mako_and_brewratio(self) -> None:
        status, value = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(value["MA_STATUS"], 1)
        status, echo = request(
            self.server.port,
            "POST",
            "/api/v2/brewratio",
            {"SYNC_BEVERAGE_WEIGHT": 18, "SYNC_FILTER": 1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(echo["MA_SYNC_BEVERAGE_WEIGHT"], 18)
        self.assertEqual(echo["MA_SYNC_FILTER"], 1)

    def test_execute_starts_brew(self) -> None:
        status, _ = request(
            self.server.port, "POST", "/api/v2/scripts/execute", {"ID": 9}
        )
        self.assertEqual(status, 200)
        status, value = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(value["MA_STATUS"], 3)

    def test_shot_finishes_then_idles(self) -> None:
        status, shot = request(
            self.server.control_port,
            "POST",
            "/control/shot",
            {"duration_ms": 25_400, "mass_g": 18.2},
        )
        self.assertEqual(status, 200)
        self.assertEqual(shot["ma_status"], 4)

        status, first = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(first["MA_STATUS"], 4)
        self.assertEqual(first["REAL_EXTRACTION_TIME_MS"], 25_400)

        status, second = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(second["MA_STATUS"], 1)

    def test_control_state_and_rejects_negative_shot(self) -> None:
        status, value = request(self.server.control_port, "GET", "/control/state")
        self.assertEqual(status, 200)
        self.assertIn("ma_status", value)
        status, _ = request(
            self.server.control_port,
            "POST",
            "/control/shot",
            {"duration_ms": -1, "mass_g": 10},
        )
        self.assertEqual(status, 400)

    def test_unknown_path_is_404(self) -> None:
        status, value = request(self.server.port, "GET", "/api/v2/nope")
        self.assertEqual(status, 404)
        self.assertEqual(value["error"], "unknown path")


if __name__ == "__main__":
    unittest.main()
