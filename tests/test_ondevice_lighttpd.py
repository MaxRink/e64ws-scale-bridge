"""Tests for the narrow lighttpd HTTP exception used by on-device Xenia."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ondevice import patch_lighttpd

# Reached through the module at call time, so these tests also run against an
# older copy of the script (the review's before/after runs) instead of dying
# at import on a symbol that did not exist yet.
BEGIN, END = patch_lighttpd.BEGIN, patch_lighttpd.END


def patch(config: Path, backup: Path) -> int:
    return patch_lighttpd.patch(config, backup)


def restore(config: Path, backup: Path) -> int:
    return patch_lighttpd.restore(config, backup)


def main(argv: list[str]) -> int:
    return patch_lighttpd.main(argv)


def validate_config_text(text: str) -> str | None:
    return patch_lighttpd.validate_config_text(text)

VENDOR = '''# force redirection from http to https
$HTTP["scheme"] == "http" {
    $HTTP["host"] =~ ".*" {
        url.redirect = (".*" => "https://%0$0")
    }
}
'''

# What a power loss during the snapshot left behind: the vendor header and
# nothing else.  Restoring it used to exit 0 and brick the web server.
TRUNCATED = "# force redirection from http to https\n"


def _dies_after_writing_part_of_the_file():
    """Emulate a copy that is cut off mid-file (power loss, ENOSPC).

    Covers both copy shapes this script has used: ``copy2(src, dst_path)``
    writes straight to the destination path, ``copyfileobj(src, dst_file)``
    writes into an open file object.
    """

    def copy(source, destination, *args, **kwargs):
        if hasattr(destination, "write"):
            try:
                destination.write(TRUNCATED)
            except TypeError:  # a binary handle
                destination.write(TRUNCATED.encode())
        else:
            Path(destination).write_text(TRUNCATED, encoding="utf-8")
        raise OSError(28, "No space left on device")

    return mock.patch.multiple("shutil", copy2=copy, copyfileobj=copy)


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


class RestoreValidationTests(unittest.TestCase):
    """``--restore`` must never copy a file that is not a whole config."""

    def _patched(self, tmp: str) -> tuple[Path, Path, str]:
        config = Path(tmp) / "lighttpd.conf"
        backup = Path(tmp) / "lighttpd.conf.vendor"
        config.write_text(VENDOR)
        self.assertEqual(patch(config, backup), 0)
        return config, backup, config.read_text()

    def test_restore_refuses_a_truncated_backup(self) -> None:
        self.assertEqual(len(TRUNCATED.encode()), 39)
        with tempfile.TemporaryDirectory() as tmp:
            config, backup, patched = self._patched(tmp)
            backup.write_text(TRUNCATED)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = restore(config, backup)
            self.assertNotEqual(code, 0)
            self.assertEqual(code, 5)
            # The live config was not touched by the refused restore.
            self.assertEqual(config.read_text(), patched)
            self.assertIn("refusing to restore", stderr.getvalue())
            self.assertIn("the live config was not touched", stderr.getvalue())
            # ... and the operator is told which snapshot is still complete.
            self.assertIn("re-run with --backup", stderr.getvalue())

    def test_restore_refuses_an_empty_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, backup, patched = self._patched(tmp)
            backup.write_text("")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = restore(config, backup)
            self.assertEqual(code, 5)
            self.assertEqual(config.read_text(), patched)
            self.assertIn("the file is empty", stderr.getvalue())

    def test_restore_refuses_a_backup_that_is_not_a_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, backup, patched = self._patched(tmp)
            backup.write_bytes(b"\x00\x01binary junk\x00")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = restore(config, backup)
            self.assertEqual(code, 5)
            self.assertEqual(config.read_text(), patched)
            self.assertIn("binary", stderr.getvalue())

    def test_restore_refuses_a_backup_cut_off_inside_a_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, backup, patched = self._patched(tmp)
            backup.write_text(VENDOR[: VENDOR.index('url.redirect')])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = restore(config, backup)
            self.assertEqual(code, 5)
            self.assertEqual(config.read_text(), patched)
            self.assertIn("cut off", stderr.getvalue())

    def test_the_restore_cli_exits_non_zero_on_a_bad_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, backup, patched = self._patched(tmp)
            backup.write_text(TRUNCATED)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main([
                    "--config", str(config), "--backup", str(backup), "--restore",
                ])
            self.assertEqual(code, 5)
            self.assertEqual(config.read_text(), patched)

    def test_old_snapshots_are_pruned(self) -> None:
        # One snapshot per edit with nothing pruning them is a slow leak.  The
        # cap is written as a value so this fails on the leak, not on a missing
        # constant.
        keep = 5
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "lighttpd.conf"
            backup = Path(tmp) / "lighttpd.conf.vendor"
            for index in range(keep + 4):
                config.write_text(VENDOR + f"# revision {index}\n")
                self.assertEqual(patch(config, backup), 0)
            snapshots = sorted(
                item.name for item in Path(tmp).glob("lighttpd.conf.vendor.*")
                if item.name != "lighttpd.conf.vendor"
            )
            self.assertEqual(len(snapshots), keep)
            # Each snapshot holds the pre-edit file, so the kept window is the
            # newest `keep` revisions (edits-keep .. edits-1).
            edits = keep + 4
            self.assertIn("# revision %d" % (edits - keep),
                          (Path(tmp) / snapshots[0]).read_text())
            self.assertIn("# revision %d" % (edits - 1),
                          (Path(tmp) / snapshots[-1]).read_text())

    def test_validate_accepts_a_complete_config(self) -> None:
        self.assertIsNone(validate_config_text(VENDOR))
        self.assertIsNone(validate_config_text('server.port = 80\n'))

    def test_a_snapshot_that_dies_midway_leaves_no_restore_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "lighttpd.conf"
            backup = Path(tmp) / "lighttpd.conf.vendor"
            config.write_text(VENDOR)
            with _dies_after_writing_part_of_the_file():
                with self.assertRaises(OSError):
                    patch(config, backup)
            # A later --restore must not find a half-written file at any name.
            self.assertEqual(sorted(item.name for item in Path(tmp).iterdir()),
                             ["lighttpd.conf"])
            self.assertEqual(config.read_text(), VENDOR)

    def test_a_restore_that_dies_midway_keeps_the_live_config_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config, backup, patched = self._patched(tmp)
            with _dies_after_writing_part_of_the_file():
                with self.assertRaises(OSError):
                    restore(config, backup)
            # The live config is either the old bytes or the restored ones,
            # never a truncated mixture, and no temp file is left behind.
            self.assertEqual(config.read_text(), patched)
            self.assertEqual(
                [item.name for item in Path(tmp).iterdir() if item.name.endswith(".tmp")],
                [],
            )


class E64PhpCompatibilityTests(unittest.TestCase):
    def test_php_avoids_missing_filter_extension(self) -> None:
        root = Path(__file__).resolve().parent.parent / "ondevice" / "web"
        for path in root.rglob("*.php"):
            self.assertNotRegex(path.read_text(), r"filter_var\s*\(\s*\$", str(path))


if __name__ == "__main__":
    unittest.main()
