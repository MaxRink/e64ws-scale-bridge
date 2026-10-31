import http.client
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path

from tools.xenia_emulator import XeniaServer, build_parser


MAKO_KEYS = {
    "MA_STATUS",
    "MA_EXTRACTION_STATUS",
    "REAL_EXTRACTION_TIME_MS",
    "PU_SENS_FLOW_METER_VOLUME",
    "PU_SENS_FLOW_METER_FLOWRATE",
    "BG_SET_TEMP",
    "BG_SENS_TEMP",
    "TANK_LEVEL",
    "script",
    "TIMESTAMP",
    "MA_SYNC_BEVERAGE_WEIGHT",
    "MA_SYNC_FILTER",
}

MACHINE_KEYS = {
    "MA_TYPE",
    "ma_type",
    "MA_SN",
    "FW_VERSION_MAJOR",
    "FW_VERSION_MINOR",
    "ESP_FW_MAJOR",
    "ESP_FW_MINOR",
    "TIMESTAMP",
}


def request(
    port: int,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        if raw_body is not None:
            data = raw_body
        elif body is not None:
            data = json.dumps(body).encode()
        else:
            data = None
        request_headers: dict[str, str] = {}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        conn.request(method, path, body=data, headers=request_headers)
        response = conn.getresponse()
        payload = response.read()
        return response.status, json.loads(payload) if payload else None
    finally:
        conn.close()


# A compact 3 s shot: five idle samples, a rise above 0.5 g, then a stable
# 18 g window.  The detector emits it as a shot (validity is false because the
# default Sync threshold is 15 s, which is fine for a fixture test).
SHOT_SAMPLES = [
    {"grams": 0.0, "ms": 0},
    {"grams": 0.0, "ms": 100},
    {"grams": 0.0, "ms": 200},
    {"grams": 0.0, "ms": 300},
    {"grams": 0.0, "ms": 400},
    {"grams": 2.0, "ms": 500},
    {"grams": 10.0, "ms": 1000},
    {"grams": 18.0, "ms": 1500},
    {"grams": 18.0, "ms": 2000},
    {"grams": 18.0, "ms": 2500},
    {"grams": 18.0, "ms": 3000},
    {"grams": 18.0, "ms": 3500},
]


class XeniaEmulatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = XeniaServer(port=0, control_port=0).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.close()

    def setUp(self) -> None:
        self.server.reset()

    def test_machine_endpoint_shapes(self) -> None:
        status, value = request(self.server.port, "GET", "/api/v2/machine")
        self.assertEqual(status, 200)
        self.assertEqual(set(value), MACHINE_KEYS)
        self.assertEqual(value["MA_TYPE"], "XENIA")
        self.assertEqual(value["MA_SN"], "VG-XENIA-0001")
        self.assertEqual(value["ma_type"], "xenia")

    def test_idle_mako_shape_and_brewratio_pair_echo(self) -> None:
        status, value = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(set(value), MAKO_KEYS)
        self.assertEqual(value["MA_STATUS"], 1)
        self.assertEqual(value["MA_EXTRACTION_STATUS"], 0)
        self.assertEqual(value["REAL_EXTRACTION_TIME_MS"], 0)

        status, echo = request(
            self.server.port,
            "POST",
            "/api/v2/brewratio",
            {"SYNC_BEVERAGE_WEIGHT": 18, "SYNC_FILTER": 1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(echo), {"MA_SYNC_BEVERAGE_WEIGHT", "MA_SYNC_FILTER"})
        self.assertEqual(echo["MA_SYNC_BEVERAGE_WEIGHT"], 18)
        self.assertEqual(echo["MA_SYNC_FILTER"], 1)

        status, value = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(value["MA_SYNC_BEVERAGE_WEIGHT"], 18)
        self.assertEqual(value["MA_SYNC_FILTER"], 1)

    def test_execute_starts_serving_and_echoes_id(self) -> None:
        status, value = request(
            self.server.port, "POST", "/api/v2/scripts/execute", {"ID": 9}
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(value), {"ok", "ID"})
        self.assertTrue(value["ok"])
        self.assertEqual(value["ID"], 9)

        status, mako = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(mako["MA_STATUS"], 3)
        self.assertEqual(mako["MA_EXTRACTION_STATUS"], 1)

        status, state = request(self.server.control_port, "GET", "/control/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "serving")

    def test_state_machine_idle_serving_finishing_idle(self) -> None:
        self.assertEqual(self.server.state.phase, "idle")
        status, state = request(self.server.control_port, "GET", "/control/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "idle")

        status, state = request(
            self.server.control_port, "POST", "/control/start", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "serving")
        self.assertEqual(state["ma_status"], 3)
        self.assertEqual(state["extraction_status"], 1)

        status, state = request(
            self.server.control_port,
            "POST",
            "/control/shot",
            {"duration_ms": 25_400, "mass_g": 18.2},
        )
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "finishing")
        self.assertEqual(state["ma_status"], 4)
        self.assertEqual(state["extraction_status"], 2)
        self.assertEqual(state["extraction_time_ms"], 25_400)
        self.assertEqual(state["mass_g"], 18.2)
        self.assertEqual(state["last_shot"]["reason"], "control")

        status, first = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(first["MA_STATUS"], 4)
        self.assertEqual(first["MA_EXTRACTION_STATUS"], 2)
        self.assertEqual(first["REAL_EXTRACTION_TIME_MS"], 25_400)

        status, state = request(self.server.control_port, "GET", "/control/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "idle")
        self.assertEqual(state["ma_status"], 1)
        self.assertEqual(state["extraction_status"], 0)

        status, second = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(second["MA_STATUS"], 1)
        self.assertEqual(second["MA_EXTRACTION_STATUS"], 0)
        # The last measurement stays readable while idle until the next brew.
        self.assertEqual(second["REAL_EXTRACTION_TIME_MS"], 25_400)

    def test_finishing_polls_is_configurable(self) -> None:
        server = XeniaServer(port=0, control_port=0, finishing_polls=2).start()
        try:
            status, _ = request(
                server.control_port,
                "POST",
                "/control/shot",
                {"duration_ms": 12_000, "mass_g": 18},
            )
            self.assertEqual(status, 200)
            first = request(server.port, "GET", "/api/v2/mako")[1]
            second = request(server.port, "GET", "/api/v2/mako")[1]
            third = request(server.port, "GET", "/api/v2/mako")[1]
            self.assertEqual(first["MA_STATUS"], 4)
            self.assertEqual(first["REAL_EXTRACTION_TIME_MS"], 12_000)
            self.assertEqual(second["MA_STATUS"], 4)
            self.assertEqual(second["REAL_EXTRACTION_TIME_MS"], 12_000)
            self.assertEqual(third["MA_STATUS"], 1)
        finally:
            server.close()

    def test_replay_file_drives_a_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shot.jsonl"
            path.write_text(
                "\n".join(json.dumps(sample) for sample in SHOT_SAMPLES) + "\n",
                encoding="utf-8",
            )
            shots = self.server.replay_file(path, speed=0)

        self.assertEqual(len(shots), 1)
        self.assertAlmostEqual(shots[0].mass_g, 18.0, places=3)
        self.assertEqual(shots[0].duration_ms, 3000.0)
        self.assertEqual(shots[0].reason, "stable weight")

        status, state = request(self.server.control_port, "GET", "/control/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "finishing")
        self.assertEqual(state["extraction_time_ms"], 3000)
        self.assertEqual(state["last_shot"]["reason"], "stable weight")

        status, first = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(first["MA_STATUS"], 4)
        self.assertEqual(first["MA_EXTRACTION_STATUS"], 2)
        self.assertEqual(first["REAL_EXTRACTION_TIME_MS"], 3000)

        status, second = request(self.server.port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(second["MA_STATUS"], 1)

    def test_example_replay_is_usable(self) -> None:
        path = Path(__file__).resolve().parents[1] / "examples" / "shot.jsonl"
        shots = self.server.replay_file(path, speed=0)
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0].duration_ms, 24_000.0)
        self.assertAlmostEqual(shots[0].mass_g, 18.0, places=3)
        self.assertTrue(shots[0].valid)

    def test_control_sample_path_reports_completed_shot(self) -> None:
        shot = None
        for sample in SHOT_SAMPLES:
            status, value = request(
                self.server.control_port, "POST", "/control/sample", sample
            )
            self.assertEqual(status, 200)
            self.assertIn("state", value)
            if value["shot"] is not None:
                shot = value["shot"]

        self.assertIsNotNone(shot)
        assert shot is not None
        self.assertEqual(shot["duration_ms"], 3000.0)
        self.assertEqual(shot["reason"], "stable weight")
        status, state = request(self.server.control_port, "GET", "/control/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "finishing")
        self.assertEqual(state["extraction_time_ms"], 3000)

        status, _ = request(
            self.server.control_port,
            "POST",
            "/control/sample",
            {"grams": 18.0, "ms": 100},
        )
        self.assertEqual(status, 400)

    def test_idle_control_resets_detector(self) -> None:
        for sample in SHOT_SAMPLES[:5]:
            status, _ = request(
                self.server.control_port, "POST", "/control/sample", sample
            )
            self.assertEqual(status, 200)
        status, state = request(self.server.control_port, "POST", "/control/idle", {})
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "idle")

        # A fresh detector needs its baseline again; a single rise alone must
        # not complete a shot.
        status, value = request(
            self.server.control_port,
            "POST",
            "/control/sample",
            {"grams": 10.0, "ms": 5000},
        )
        self.assertEqual(status, 200)
        self.assertIsNone(value["shot"])

    def test_malformed_bodies_are_400(self) -> None:
        cases = [
            ("/api/v2/brewratio", b"{"),
            ("/api/v2/brewratio", b"[]"),
            ("/api/v2/brewratio", b'{"SYNC_BEVERAGE_WEIGHT": "x"}'),
            ("/api/v2/scripts/execute", b"null"),
            ("/control/shot", b'{"duration_ms": -1, "mass_g": 18}'),
            ("/control/shot", b'{"duration_ms": 25000}'),
            ("/control/shot", b'{"duration_ms": "25000", "mass_g": 18}'),
            ("/control/shot", b'{"duration_ms": NaN, "mass_g": 18}'),
            ("/control/shot", b'{"duration_ms": 1' + b"0" * 400 + b', "mass_g": 18}'),
            ("/control/sample", b'{"grams": 1}'),
            ("/control/sample", b'{"grams": NaN, "ms": 1}'),
        ]
        for path, raw_body in cases:
            with self.subTest(path=path, body=raw_body):
                status, value = request(
                    self.server.control_port
                    if path.startswith("/control/")
                    else self.server.port,
                    "POST",
                    path,
                    raw_body=raw_body,
                )
                self.assertEqual(status, 400)
                self.assertIn("error", value)

    def test_unknown_path_is_404_for_get_and_post(self) -> None:
        cases = [
            (self.server.port, "GET", "/api/v2/nope", None),
            (self.server.port, "POST", "/api/v2/nope", b"{}"),
            (self.server.port, "POST", "/api/v2/nope", b"{"),
            (self.server.control_port, "GET", "/control/nope", None),
            (self.server.control_port, "POST", "/control/nope", b"{}"),
        ]
        for port, method, path, raw_body in cases:
            with self.subTest(method=method, path=path):
                status, value = request(port, method, path, raw_body=raw_body)
                self.assertEqual(status, 404)
                self.assertEqual(value["error"], "unknown path")

    def test_control_routes_are_not_exposed_on_e64_listener(self) -> None:
        cases = [
            ("GET", "/control/state", None),
            ("POST", "/control/shot", {"duration_ms": 25_400, "mass_g": 18.2}),
            ("POST", "/control/reset", {}),
        ]
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                status, value = request(self.server.port, method, path, body)
                self.assertEqual(status, 404)
                self.assertEqual(value["error"], "unknown path")

        # The E64 routes remain available for inspection on the control
        # listener; the important guarantee is control -> loopback only.
        status, _ = request(self.server.control_port, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)

    def test_defaults_are_loopback(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.control_host, "127.0.0.1")
        self.assertFalse(args.verbose)

        server = XeniaServer(port=0, control_port=0).start()
        try:
            self.assertEqual(server.http.server_address[0], "127.0.0.1")
            self.assertEqual(server.control.server_address[0], "127.0.0.1")
        finally:
            server.close()


class XeniaLoggingTests(unittest.TestCase):
    def test_verbose_logging_never_contains_headers_bodies_or_query(self) -> None:
        stream = io.StringIO()
        server = XeniaServer(
            port=0, control_port=0, verbose=True, log_stream=stream
        ).start()
        try:
            status, _ = request(
                server.port,
                "POST",
                "/api/v2/brewratio",
                {
                    "SYNC_BEVERAGE_WEIGHT": 18,
                    "SYNC_FILTER": 1,
                    "token": "body-secret",
                },
                headers={"Authorization": "Bearer header-secret"},
            )
            self.assertEqual(status, 200)
            status, _ = request(server.port, "GET", "/api/v2/mako?token=query-secret")
            self.assertEqual(status, 200)

            logs = stream.getvalue()
            self.assertIn("POST /api/v2/brewratio 200", logs)
            self.assertIn("GET /api/v2/mako 200", logs)
            for secret in (
                "body-secret",
                "header-secret",
                "query-secret",
                "Authorization",
            ):
                self.assertNotIn(secret, logs)
        finally:
            server.close()

    def test_logging_is_off_by_default(self) -> None:
        stream = io.StringIO()
        server = XeniaServer(port=0, control_port=0, log_stream=stream).start()
        try:
            status, _ = request(server.port, "GET", "/api/v2/machine")
            self.assertEqual(status, 200)
            self.assertEqual(stream.getvalue(), "")
        finally:
            server.close()

    def test_malformed_request_line_is_not_echoed(self) -> None:
        stream = io.StringIO()
        server = XeniaServer(
            port=0, control_port=0, verbose=True, log_stream=stream
        ).start()
        try:
            with socket.create_connection(
                ("127.0.0.1", server.port), timeout=5
            ) as connection:
                connection.sendall(
                    b"GET /api/v2/mako?token=line-secret BAD\r\n\r\n"
                )
                response = connection.recv(4096)
            self.assertIn(b"400", response)
            logs = stream.getvalue()
            self.assertIn("protocol error", logs)
            self.assertNotIn("line-secret", logs)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
