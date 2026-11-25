"""Tests for the narrow lighttpd HTTP exception used by on-device Xenia."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ondevice.patch_lighttpd import BEGIN, END, patch, restore

VENDOR = '''# force redirection from http to https
$HTTP["scheme"] == "http" {
    $HTTP["host"] =~ ".*" {
        url.redirect = (".*" => "https://%0$0")
    }
}
'''


class LighttpdPatchTests(unittest.TestCase):
    def test_patch_is_narrow_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "lighttpd.conf"
            backup = Path(tmp) / "lighttpd.conf.vendor"
            config.write_text(VENDOR)
            self.assertEqual(patch(config, backup), 0)
            text = config.read_text()
            self.assertIn(BEGIN, text)
            self.assertIn(END, text)
            self.assertIn('$HTTP["url"] !~ "^/api/v2/"', text)
            self.assertIn('url.rewrite-once', text)
            self.assertIn('https://%0$0', text)
            self.assertTrue(backup.is_file())
            self.assertEqual(patch(config, backup), 0)
            self.assertEqual(text, config.read_text())

    def test_upgrades_an_older_marked_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "lighttpd.conf"
            backup = Path(tmp) / "lighttpd.conf.vendor"
            config.write_text(VENDOR)
            self.assertEqual(patch(config, backup), 0)
            old = config.read_text().replace("|ingest", "")
            config.write_text(old)
            self.assertEqual(patch(config, backup), 0)
            self.assertIn("|ingest", config.read_text())

    def test_restore_returns_vendor_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "lighttpd.conf"
            backup = Path(tmp) / "lighttpd.conf.vendor"
            config.write_text(VENDOR)
            self.assertEqual(patch(config, backup), 0)
            self.assertEqual(restore(config, backup), 0)
            self.assertEqual(config.read_text(), VENDOR)

    def test_apply_upgrade_restore_round_trips(self) -> None:
        """After a firmware update, --restore must bring back *that* vendor file."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "lighttpd.conf"
            backup = Path(tmp) / "lighttpd.conf.vendor"
            config.write_text(VENDOR)
            self.assertEqual(patch(config, backup), 0)
            # A signed update ships a new vendor revision without our block.
            upgraded = "# vendor revision 2\n" + VENDOR
            config.write_text(upgraded)
            self.assertNotIn(BEGIN, config.read_text())
            self.assertEqual(patch(config, backup), 0)
            self.assertIn(BEGIN, config.read_text())
            # The stale pre-upgrade backup must not win.
            self.assertEqual(restore(config, backup), 0)
            self.assertEqual(config.read_text(), upgraded)

    def test_every_edit_snapshots_the_pre_edit_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "lighttpd.conf"
            backup = Path(tmp) / "lighttpd.conf.vendor"
            config.write_text(VENDOR)
            self.assertEqual(patch(config, backup), 0)
            snapshots = sorted(Path(tmp).glob("lighttpd.conf.vendor.*"))
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].read_text(), VENDOR)
            # Upgrading an older marked block is a second edit: snapshot again.
            older = config.read_text().replace("|ingest", "")
            config.write_text(older)
            self.assertEqual(patch(config, backup), 0)
            snapshots = sorted(Path(tmp).glob("lighttpd.conf.vendor.*"))
            self.assertEqual(len(snapshots), 2)
            self.assertEqual(len({item.name for item in snapshots}), 2)
            self.assertEqual(snapshots[0].read_text(), VENDOR)
            self.assertEqual(snapshots[1].read_text(), older)
            # The canonical restore point still holds the pristine vendor file.
            self.assertEqual(backup.read_text(), VENDOR)
            self.assertEqual(restore(config, backup), 0)
            self.assertEqual(config.read_text(), VENDOR)

    def test_unknown_config_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "lighttpd.conf"
            backup = Path(tmp) / "lighttpd.conf.vendor"
            config.write_text('server.document-root = "/tmp"\n')
            self.assertEqual(patch(config, backup), 3)
            self.assertFalse(backup.exists())


class E64PhpCompatibilityTests(unittest.TestCase):
    def test_php_avoids_missing_filter_extension(self) -> None:
        root = Path(__file__).resolve().parent.parent / "ondevice" / "web"
        for path in root.rglob("*.php"):
            self.assertNotRegex(path.read_text(), r"filter_var\s*\(\s*\$", str(path))


if __name__ == "__main__":
    unittest.main()
