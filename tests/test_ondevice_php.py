"""HTTP tests for the on-device PHP Xenia endpoints and WebUI page.

Run directly with a local PHP 8.4 (`php -S`) or set PHP_BASE to a server that
already has this repository's ondevice/web as its docroot.  The Docker helper
is `tools/run_php_tests.sh`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


def _request(
    base: str,
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float = 5.0,
    headers: dict[str, str] | None = None,
):
    data = json.dumps(body).encode() if body is not None else None
    request_headers = {"Content-Type": "application/json"} if data is not None else {}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers=request_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class OnDevicePhpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state_dir = Path(os.environ.get("PHP_STATE_DIR", cls.tmp.name))
        cls.state_dir.mkdir(parents=True, exist_ok=True)
        cls.base = os.environ.get("PHP_BASE", "")
        cls.proc = None
        if not cls.base:
            php = shutil.which("php")
            if not php:
                raise unittest.SkipTest("php not found; set PHP_BASE or use tools/run_php_tests.sh")
            port = 18081
            env = os.environ.copy()
            env.update({
                "SCALE_BRIDGE_STATE": str(cls.state_dir / "state.json"),
                "SCALE_BRIDGE_RATIO": str(cls.state_dir / "brewratio.json"),
                "SCALE_BRIDGE_BREW_START": str(cls.state_dir / "brew-start.json"),
                "SCALE_BRIDGE_ESP_STATE": str(cls.state_dir / "esp-state.json"),
                "SCALE_BRIDGE_INGEST_TOKEN": "test-token",
                "SCALE_BRIDGE_NO_AUTH": "1",
            })
            cls.proc = subprocess.Popen(
                [php, "-S", f"127.0.0.1:{port}", "-t", "ondevice/web"],
                cwd=Path(__file__).resolve().parent.parent,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            cls.base = f"http://127.0.0.1:{port}"
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    _request(cls.base, "GET", "/api/v2/machine")
                    break
                except OSError:
                    time.sleep(0.05)
        cls.state_path = cls.state_dir / "state.json"
        cls.ratio_path = cls.state_dir / "brewratio.json"
        cls.start_path = cls.state_dir / "brew-start.json"
        cls.esp_path = cls.state_dir / "esp-state.json"

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.proc is not None:
            cls.proc.terminate()
            cls.proc.wait(timeout=5)
        if not os.environ.get("PHP_STATE_DIR"):
            cls.tmp.cleanup()

    def setUp(self) -> None:
        for path in (self.state_path, self.ratio_path, self.start_path, self.esp_path):
            path.unlink(missing_ok=True)

    def _state(self, **fields) -> None:
        value = {
            "schema": 1,
            "source": "ws://hds.local:80/snapshot",
            "connected": True,
            "phase": "idle",
            "ma_status": 1,
            "extraction_status": 0,
            "live_grams": 0.0,
            "live_ms": 0,
            "last_shot": None,
            "finishing_until_ms": 0,
            "shot_count": 0,
            "reconnects": 1,
            "error": "",
            "updated_ms": 0,
        }
        value.update(fields)
        self.state_path.write_text(json.dumps(value))

    def test_machine_shape(self) -> None:
        status, raw = _request(self.base, "GET", "/api/v2/machine")
        self.assertEqual(status, 200)
        value = json.loads(raw)
        self.assertEqual(value["MA_TYPE"], "XENIA")
        self.assertEqual(value["ma_type"], "xenia")
        self.assertIn("MA_SN", value)

    def test_idle_mako_shape(self) -> None:
        self._state()
        status, raw = _request(self.base, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        value = json.loads(raw)
        self.assertEqual(value["MA_STATUS"], 1)
        self.assertEqual(value["MA_EXTRACTION_STATUS"], 0)
        self.assertEqual(value["REAL_EXTRACTION_TIME_MS"], 0)
        self.assertIsInstance(value["TIMESTAMP"], str)
        self.assertRegex(value["TIMESTAMP"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(value["PU_SENS_FLOW_METER_VOLUME"], 0)
        self.assertEqual(value["PU_SENS_FLOW_METER_FLOWRATE"], 0)
        self.assertEqual(value["script"], "SCALE")
        for key in (
            "PU_SENS_FLOW_METER_VOLUME",
            "PU_SENS_FLOW_METER_FLOWRATE",
            "BG_SET_TEMP",
            "BG_SENS_TEMP",
            "TANK_LEVEL",
            "script",
            "TIMESTAMP",
            "MA_SYNC_BEVERAGE_WEIGHT",
            "MA_SYNC_FILTER",
        ):
            self.assertIn(key, value)

    def test_serving_and_finishing(self) -> None:
        self._state(
            phase="serving", ma_status=3, extraction_status=1,
            live_grams=12.5, flow_g_s=1.2, active_ms=5000,
        )
        _, raw = _request(self.base, "GET", "/api/v2/mako")
        serving = json.loads(raw)
        self.assertEqual(serving["MA_STATUS"], 3)
        self.assertEqual(serving["MA_EXTRACTION_STATUS"], 1)
        self.assertEqual(serving["REAL_EXTRACTION_TIME_MS"], 5000)
        self.assertEqual(serving["PU_SENS_FLOW_METER_VOLUME"], 12.5)
        self.assertEqual(serving["PU_SENS_FLOW_METER_FLOWRATE"], 1.2)

        now = int(time.time() * 1000) % 2147483648
        self._state(
            phase="finishing",
            ma_status=4,
            extraction_status=2,
            finishing_until_ms=now + 5000,
            last_shot={"duration_ms": 24600, "mass_g": 18.0, "valid": True, "reason": "stable"},
        )
        _, raw = _request(self.base, "GET", "/api/v2/mako")
        value = json.loads(raw)
        self.assertEqual(value["MA_STATUS"], 4)
        self.assertEqual(value["MA_EXTRACTION_STATUS"], 2)
        self.assertEqual(value["REAL_EXTRACTION_TIME_MS"], 24600)
        self.assertEqual(value["PU_SENS_FLOW_METER_VOLUME"], 18.0)

    def test_finishing_window_expires(self) -> None:
        now = int(time.time() * 1000) % 2147483648
        self._state(
            phase="finishing",
            finishing_until_ms=now - 1,
            last_shot={"duration_ms": 24600, "mass_g": 18.0, "valid": True, "reason": "stable"},
        )
        _, raw = _request(self.base, "GET", "/api/v2/mako")
        self.assertEqual(json.loads(raw)["MA_STATUS"], 1)

    def test_brewratio_roundtrip_and_validation(self) -> None:
        status, raw = _request(
            self.base, "POST", "/api/v2/brewratio",
            {"SYNC_BEVERAGE_WEIGHT": "18", "SYNC_FILTER": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"SYNC_BEVERAGE_WEIGHT": 18, "SYNC_FILTER": 1})
        self.assertEqual(json.loads(self.ratio_path.read_text())["SYNC_BEVERAGE_WEIGHT"], 18)
        status, _ = _request(self.base, "POST", "/api/v2/brewratio", {"SYNC_FILTER": -1})
        self.assertEqual(status, 400)
        status, _ = _request(self.base, "GET", "/api/v2/brewratio")
        self.assertEqual(status, 405)

    def test_scripts_execute(self) -> None:
        status, raw = _request(
            self.base, "POST", "/api/v2/scripts/execute", {"ID": "shot-1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": True, "ID": "shot-1"})
        self.assertEqual(json.loads(self.start_path.read_text())["id"], "shot-1")
        status, _ = _request(self.base, "GET", "/api/v2/scripts/execute")
        self.assertEqual(status, 405)

    def test_esp_ingest_drives_xenia_and_hmi_fields(self) -> None:
        status, raw = _request(
            self.base, "POST", "/api/v2/ingest",
            {
                "source": "esp32-ble",
                "phase": "serving",
                "live_grams": 17.4,
                "flow_g_s": 1.3,
                "active_ms": 4200,
                "shot_count": 3,
            },
            headers={"X-Scale-Token": "test-token"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        status, raw = _request(self.base, "GET", "/api/v2/mako")
        value = json.loads(raw)
        self.assertEqual(value["MA_STATUS"], 3)
        self.assertEqual(value["MA_EXTRACTION_STATUS"], 1)
        self.assertEqual(value["REAL_EXTRACTION_TIME_MS"], 4200)
        self.assertEqual(value["PU_SENS_FLOW_METER_VOLUME"], 17.4)
        self.assertEqual(value["PU_SENS_FLOW_METER_FLOWRATE"], 1.3)
        self.assertEqual(value["script"], "SCALE")

    def test_esp_ingest_requires_token_and_valid_phase(self) -> None:
        status, _ = _request(
            self.base, "POST", "/api/v2/ingest",
            {"phase": "serving", "live_grams": 1.0},
        )
        self.assertEqual(status, 403)
        status, _ = _request(
            self.base, "POST", "/api/v2/ingest",
            {"phase": "serving", "live_grams": 1.0},
            headers={"X-Scale-Token": "wrong"},
        )
        self.assertEqual(status, 403)
        status, _ = _request(
            self.base, "POST", "/api/v2/ingest",
            {"phase": "nope", "live_grams": 1.0},
            headers={"X-Scale-Token": "test-token"},
        )
        self.assertEqual(status, 400)

    def test_stale_esp_state_falls_back_to_wifi_daemon(self) -> None:
        self._state()
        self.esp_path.write_text(json.dumps({
            "schema": 1, "source": "esp32-ble", "connected": True,
            "phase": "serving", "live_grams": 99.0, "flow_g_s": 9.0,
            "active_ms": 9, "last_shot": None, "finishing_until_ms": 0,
            "shot_count": 9, "reconnects": 0, "error": "",
            "updated_ms": 1,
        }))
        status, raw = _request(self.base, "GET", "/api/v2/mako")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["MA_STATUS"], 1)

    def test_webui_page_renders_state(self) -> None:
        self._state(phase="serving", live_grams=17.4, shot_count=2,
                    last_shot={"duration_ms": 20000, "mass_g": 18.0, "valid": True, "reason": "stable"})
        status, raw = _request(self.base, "GET", "/scale/")
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertIn("Scale bridge", text)
        self.assertIn("17.4", text)
        self.assertIn("20.0", text)


if __name__ == "__main__":
    unittest.main()
