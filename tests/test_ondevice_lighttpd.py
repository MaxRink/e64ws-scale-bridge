"""Tests for the narrow lighttpd HTTP exception used by on-device Xenia."""

from __future__ import annotations

import contextlib
import io
import os
import re
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


def snapshot(config: Path, backup: Path) -> Path:
    return patch_lighttpd.snapshot(config, backup)


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


# A single frozen second for every snapshot, so retention is exercised through
# the same-second path on every run instead of whenever the machine is fast.
FROZEN_STAMP = "20260101T000000Z"

# A filesystem clock that ticks once per run: every snapshot of the run is
# stamped with this exact st_mtime_ns, so write time cannot separate them and
# retention has to fall back on the counter in the name.  A whole second, so a
# filesystem that stores timestamps coarsely still keeps it as written.
COARSE_TICK_NS = 1_700_000_000_000_000_000


def _frozen_clock(stamp: str = FROZEN_STAMP):
    """Pin the snapshot stamp to one second, so retention is deterministic.

    Reached through the module's ``time`` object, so this keeps working if the
    script reformats the stamp.
    """

    return mock.patch.multiple(
        "ondevice.patch_lighttpd.time",
        strftime=lambda *args, **kwargs: stamp,
        gmtime=lambda *args, **kwargs: 0,
    )


def _aged_snapshot(backup: Path, name: str, age_ns: int, body: str) -> Path:
    """Write a snapshot named *name* with a chosen write time."""

    path = backup.with_name(backup.name + "." + name)
    path.write_text(VENDOR + body, encoding="utf-8")
    os.utime(path, ns=(age_ns, age_ns))
    return path


def _revision(path: Path) -> int:
    """The ``# revision N`` marker a harness snapshot holds."""

    match = re.search(r"# revision (\d+)", path.read_text(encoding="utf-8"))
    if match is None:
        raise AssertionError(f"{path} holds no revision marker")
    return int(match.group(1))


def _written_revisions(paths: list[Path]) -> list[int]:
    return [_revision(item) for item in paths]


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
            # Deterministic, not load-dependent: the whole run happens inside
            # one frozen second, so every snapshot shares a stamp and retention
            # has to fall back on the counter in the name.  Two edits that land
            # in the same second used to be the *unlucky* case (the newest name
            # was reused and sorted first, so the oldest revision was kept).
            with _frozen_clock():
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
            self.assertEqual(
                [int(re.search(r"# revision (\d+)",
                               (Path(tmp) / name).read_text()).group(1))
                 for name in snapshots],
                list(range(edits - keep, edits)),
            )
            # The bug this pins: with one frozen second the revisions kept were
            # 2,3,4,5,7 -- the newest-but-one deleted while revision 2 survived.
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


class SnapshotRetentionTests(unittest.TestCase):
    """Retention keeps the newest snapshots even when their names sort first.

    The name is a whole-second UTC stamp plus a ``.N`` collision suffix, and
    the suffix used to be the first free number -- so a pruned name was handed
    out again and ``<base>`` (newest) sorted before ``<base>.2`` (older).
    Sorting by name therefore deleted the newest revision.  These tests pin
    write-time order with explicit mtimes, so they do not depend on how fast
    the machine is or on the filesystem's timestamp granularity.

    The exception is the coarse-clock harness: it pins every snapshot of a run
    to a single tick on purpose, because then write time cannot separate them
    and the counter in the name is the only thing left.  That is where the
    suffix has to be compared as a number.
    """

    def test_a_coarse_clock_keeps_the_newest_by_write_time(self) -> None:
        # Every snapshot of the run carries the same st_mtime_ns, so retention
        # falls back on the counter -- which used to be compared as text, where
        # ".10" sorts before ".9".  Measured before this test existed: eleven
        # edits kept revisions 5,6,7,8,10 and twelve kept 5,6,7,8,11, i.e. the
        # newest revision was deleted while older ones survived.
        #
        # The magnitudes bracket the defect: correct up to nine by luck
        # (single-digit suffixes), wrong from ten on, and still right at 128
        # once the counter is compared as an int.
        keep = 5
        for edits in (8, 9, 10, 11, 12, 20, 128):
            with self.subTest(edits=edits), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "lighttpd.conf"
                backup = Path(tmp) / "lighttpd.conf.vendor"
                for index in range(edits):
                    config.write_text(VENDOR + f"# revision {index}\n")
                    written = snapshot(config, backup)
                    # The whole run lands inside one filesystem clock tick.
                    os.utime(written, ns=(COARSE_TICK_NS, COARSE_TICK_NS))
                survivors = sorted(Path(tmp).glob("lighttpd.conf.vendor.*"))
                # The tick really is coarse, and really is shared by all of them.
                self.assertEqual({item.stat().st_mtime_ns for item in survivors},
                                 {COARSE_TICK_NS},
                                 "the harness must leave write time undecidable")
                self.assertEqual(len(survivors), keep)
                # Newest by write time = the last `keep` revisions this loop
                # wrote, which the harness knows from the loop and not from a
                # name.
                self.assertEqual(sorted(_written_revisions(survivors)),
                                 list(range(edits - keep, edits)),
                                 f"{edits} edits inside one clock tick")

    def test_a_coarse_mtime_tick_orders_by_the_stamp_before_the_counter(self) -> None:
        # vfat rounds mtime to two seconds while the name stamp is whole
        # seconds, so one mtime tick can hold two stamps -- and the earlier
        # second can carry the higher counter.  Ordering by the counter alone
        # then deletes the newest snapshot and keeps the three oldest.
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "lighttpd.conf.vendor"
            earlier, later = FROZEN_STAMP, "20260101T000001Z"
            _aged_snapshot(backup, earlier + ".1", COARSE_TICK_NS, "# revision 1\n")
            _aged_snapshot(backup, earlier + ".2", COARSE_TICK_NS, "# revision 2\n")
            _aged_snapshot(backup, earlier + ".3", COARSE_TICK_NS, "# revision 3\n")
            _aged_snapshot(backup, later + ".1", COARSE_TICK_NS, "# revision 4\n")
            patch_lighttpd.prune_snapshots(backup, keep=2)
            survivors = sorted(_written_revisions(Path(tmp).glob("lighttpd.conf.vendor.*")))
            self.assertEqual(survivors, [3, 4],
                             "the newest snapshot must not be the one deleted")

    def test_retention_keeps_the_newest_even_when_its_name_sorts_first(self) -> None:
        # The measured defect, with a reused <base>: revisions 2,3,4,5,7 were
        # kept and 0,1,6 deleted -- the newest-but-one dropped while an older
        # revision survived.
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "lighttpd.conf.vendor"
            newest = _aged_snapshot(backup, FROZEN_STAMP, 3_000, "# revision 8\n")
            older = _aged_snapshot(backup, FROZEN_STAMP + ".2", 2_000, "# revision 2\n")
            # Name order really is the wrong way round here.
            self.assertLess(newest.name, older.name)
            patch_lighttpd.prune_snapshots(backup, keep=1)
            self.assertTrue(newest.exists(), "retention deleted the newest snapshot")
            self.assertFalse(older.exists(), "retention kept an older snapshot")

    def test_a_snapshot_is_dated_by_when_it_was_taken(self) -> None:
        # The device's vendor config ships inside a firmware image, so it can
        # carry an old mtime; inheriting it would make a brand-new snapshot
        # look older than every other one.
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "lighttpd.conf"
            backup = Path(tmp) / "lighttpd.conf.vendor"
            config.write_text(VENDOR)
            long_ago = 1_000_000_000_000_000_000  # 2001-09-09
            os.utime(config, ns=(long_ago, long_ago))
            written = snapshot(config, backup)
            self.assertGreater(written.stat().st_mtime_ns, long_ago + 10**12,
                               "the snapshot inherited the source timestamp")
            self.assertEqual(written.read_text(), VENDOR)

    def test_describe_snapshots_names_the_newest_complete_one(self) -> None:
        # The hint after a refused --restore must point at the newest complete
        # snapshot; name order used to make it name an older one.
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "lighttpd.conf.vendor"
            newest = _aged_snapshot(backup, FROZEN_STAMP, 4_000, "# revision 8\n")
            _aged_snapshot(backup, FROZEN_STAMP + ".2", 3_000, "# revision 2\n")
            # ... and an incomplete one that is newer than both: it must not be
            # offered as a restore point.
            broken = backup.with_name(backup.name + "." + FROZEN_STAMP + ".9")
            broken.write_text(TRUNCATED, encoding="utf-8")
            os.utime(broken, ns=(9_000, 9_000))
            described = patch_lighttpd._describe_snapshots(backup)
            # Compare whole paths: one name is a prefix of the other, so a
            # substring check would pass on the wrong snapshot.
            named = [line.split(": ", 1)[1].strip() for line in described.splitlines()
                     if "exists: " in line]
            hint = [line.split("--backup ", 1)[1].strip() for line in described.splitlines()
                    if "--backup " in line]
            self.assertEqual(named, [str(newest)])
            self.assertEqual(hint, [str(newest)])
            self.assertNotIn(str(broken), named + hint)


class E64PhpCompatibilityTests(unittest.TestCase):
    def test_php_avoids_missing_filter_extension(self) -> None:
        root = Path(__file__).resolve().parent.parent / "ondevice" / "web"
        for path in root.rglob("*.php"):
            self.assertNotRegex(path.read_text(), r"filter_var\s*\(\s*\$", str(path))


if __name__ == "__main__":
    unittest.main()
