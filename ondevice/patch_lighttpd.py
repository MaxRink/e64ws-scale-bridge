#!/usr/bin/env python3
"""Patch the vendor lighttpd config for the on-device Xenia routes.

The E64 Xenia client is HTTP-only: ``RestClient::open(host, 80)`` builds
``http://<host>/api/v2/...`` and does not use TLS.  The vendor lighttpd config
redirects every HTTP request to HTTPS, which would make the local Xenia client
fail before PHP is reached.

This script makes one narrow, marked change:

* exempt only ``/api/v2/*`` from the HTTP->HTTPS redirect;
* rewrite the five exact Xenia paths to their ``index.php`` so the client does
  not have to follow a directory redirect;
* leave every other HTTP path redirected to HTTPS.

The exemption is a deliberate cleartext trade-off: everything under ``/api/v2/``
(including the ``brewratio`` write and the ESP32 ``X-Scale-Token`` header) is
readable and injectable on the LAN path.  It is accepted only for a trusted,
isolated LAN; the E64 client cannot use TLS, so narrowing the exemption would
break the M2M path.  See docs/REVIEW_FOLLOWUPS.md.

It is idempotent and always snapshots the pre-edit file under a unique name
next to ``--backup``.  The unpatched vendor config is the restore point and is
refreshed whenever an edit starts from an unpatched file, so a firmware update
that ships a new vendor config cannot leave ``--restore`` on a stale snapshot.
Only the newest ``SNAPSHOT_KEEP`` snapshots are kept, and "newest" is by write
time, not by name: the name is a whole-second stamp, so name order disagrees
with reality as soon as two edits land in the same second.

``--restore`` validates the snapshot before it copies a byte: the restore point
must be a complete lighttpd config, not the truncated file a power loss can
leave behind.  Every copy -- the snapshot, the restore point and the live
config -- is written to a sibling ``.tmp`` file, fsynced and renamed, so an
interrupted write leaves the previous file intact instead of a half file that
looks complete.  A refusal is loud and exits non-zero; the live config is not
touched.  The installer runs ``lighttpd -t`` before restarting and restores the
snapshot on failure.

Exit codes: 0 ok, 2 missing config, 3 no vendor redirect block to patch,
4 missing backup, 5 backup is not a complete config.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

BEGIN = "# BEGIN E64 scale bridge Xenia HTTP"
END = "# END E64 scale bridge Xenia HTTP"

REDIRECT = re.compile(
    r"# force redirection from http to https\s*"
    r'\$HTTP\["scheme"\] == "http" \{\s*'
    r'\$HTTP\["host"\] =~ "\.\*" \{\s*'
    r'url\.redirect = \("\.\*" => "https://%0\$0"\)\s*'
    r"\}\s*\}"
)

PATCH = f'''{BEGIN}
# The E64 Xenia client is HTTP-only (RestClient::open(host, 80)). Exempt the
# five local Xenia routes from the global HTTPS redirect and rewrite them to
# their index.php files so no directory redirect is needed.
#
# TRADE-OFF (deliberate, do not "fix" silently): this makes every /api/v2/
# request plain HTTP on the LAN. POST /api/v2/brewratio therefore writes the
# SYNC_BEVERAGE_WEIGHT/SYNC_FILTER pair in the clear, and /api/v2/ingest sends
# its X-Scale-Token header in the clear. Only run this on a trusted, isolated
# LAN and never expose port 80 to the internet. See docs/REVIEW_FOLLOWUPS.md.
$HTTP["scheme"] == "http" {{
    $HTTP["url"] !~ "^/api/v2/" {{
        $HTTP["host"] =~ ".*" {{
            url.redirect = (".*" => "https://%0$0")
        }}
    }}
}}
$HTTP["url"] =~ "^/api/v2/(mako|machine|brewratio|scripts/execute|ingest)$" {{
    url.rewrite-once = ( "^/api/v2/(mako|machine|brewratio|scripts/execute|ingest)$" => "/api/v2/$1/index.php" )
}}
{END}'''


def _fsync_directory(path: Path) -> None:
    """Make the rename in *path* durable; best effort where unsupported."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_copy(source: Path, destination: Path, *, preserve_times: bool = True) -> None:
    """Copy *source* to *destination* so a crash cannot leave half a file.

    The bytes go to a sibling ``.tmp`` file, are fsynced, and are renamed over
    *destination*.  A power loss therefore leaves the previous file or the
    complete new one -- never the truncated restore point that used to make
    ``--restore`` brick lighttpd.

    ``preserve_times=False`` keeps the permissions but not the timestamps, so
    *destination* is dated when it was written.  A snapshot has to be: its age
    is what retention sorts on, and inheriting the date of a config file that
    shipped inside a firmware image would make a brand-new snapshot look older
    than every other one.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".tmp")
    try:
        with open(source, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if preserve_times:
            shutil.copystat(source, tmp)
        else:
            shutil.copymode(source, tmp)
        os.replace(tmp, destination)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_directory(destination.parent)


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace *path* with *text* without ever exposing a partial file."""

    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def validate_config_text(text: str) -> str | None:
    """Return why *text* cannot be a complete lighttpd config, else ``None``.

    Deliberately structural rather than a lighttpd parse: the device has no
    lighttpd grammar in Python, and the question that has to be answered
    before overwriting the live config is only "is this a whole config file?".
    It catches the truncated restore point (a 39-byte file holding nothing but
    the vendor comment line) and a garbage or empty backup.
    """

    if not text.strip():
        return "the file is empty"
    if "\x00" in text:
        return "the file contains binary data"
    # A truncated file usually dies inside a block, leaving a dangling brace.
    if text.count("{") != text.count("}"):
        return (
            f"it has {text.count('{')} opening and {text.count('}')} closing braces, "
            "so it was cut off"
        )
    if "$HTTP[" not in text and re.search(r"(?m)^\s*server\.", text) is None:
        return "it holds no lighttpd directive ($HTTP[...] or server.*)"
    return None


# One snapshot per edit, never pruned, is a slow leak on a device whose /db is
# small: keep the newest few for rollback evidence.
SNAPSHOT_KEEP = 5


def _snapshot_age(path: Path) -> tuple[int, str]:
    """Sort key for the snapshots: when it was written, then its name.

    Retention used to sort by name, and the name is a whole-second UTC stamp
    with a ``.N`` collision suffix that gets *reused* once a name has been
    pruned.  Two edits in the same second therefore produced ``<base>``
    (newest) and ``<base>.2`` (older), and name order put ``<base>`` first --
    so the newest snapshot was deleted and an older one kept.  Age comes from
    ``st_mtime_ns``, which ``snapshot()`` dates at write time.  The name only
    breaks a tie on a filesystem with a coarser clock, which is why the
    counter in it never goes backwards.
    """

    try:
        return (path.stat().st_mtime_ns, path.name)
    except OSError:
        # Vanished or unreadable: treat it as the oldest so it is pruned first.
        return (-1, path.name)


def prune_snapshots(backup: Path, *, keep: int = SNAPSHOT_KEEP,
                    protect: Path | None = None) -> None:
    """Keep the newest *keep* snapshots next to *backup*; drop the rest."""

    if keep <= 0:
        return
    # *protect* is the snapshot just written and counts towards the budget.
    room = keep - 1 if protect is not None else keep
    snapshots = sorted(
        (item for item in backup.parent.glob(backup.name + ".*")
         if not item.name.endswith(".tmp") and item != protect),
        key=_snapshot_age,
    )
    for stale in (snapshots[:-room] if room else snapshots):
        try:
            stale.unlink()
        except OSError:
            pass


def _describe_snapshots(backup: Path) -> str:
    """Name the newest sibling snapshot that does look complete, for the operator."""

    usable = []
    for candidate in sorted(backup.parent.glob(backup.name + ".*"), key=_snapshot_age):
        if candidate.name.endswith(".tmp"):
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if validate_config_text(text) is None:
            usable.append(candidate)
    if not usable:
        return ""
    return (
        f"\n       the newest complete snapshot exists: {usable[-1]}"
        f"\n       re-run with --backup {usable[-1]}"
    )


def _next_snapshot_path(backup: Path) -> Path:
    """Return an unused snapshot path that sorts after every surviving one.

    The UTC stamp is for the operator; the counter is what makes the *name*
    agree with write order.  It is ``max(existing) + 1`` rather than the first
    free number, so a pruned name is never handed out again while an older
    sibling still carries a higher one -- the reuse that made name ordering
    disagree with reality.
    """

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    prefix = f"{backup.name}.{stamp}."
    highest = 0
    for item in backup.parent.glob(prefix + "*"):
        suffix = item.name[len(prefix):]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    candidate = backup.with_name(f"{prefix}{highest + 1}")
    while candidate.exists():  # a concurrent run took that name
        highest += 1
        candidate = backup.with_name(f"{prefix}{highest + 1}")
    return candidate


def snapshot(config: Path, backup: Path) -> Path:
    """Copy the pre-edit config to a unique sibling of *backup*."""

    backup.parent.mkdir(parents=True, exist_ok=True)
    candidate = _next_snapshot_path(backup)
    _atomic_copy(config, candidate, preserve_times=False)
    prune_snapshots(backup, protect=candidate)
    print(f"snapshotted pre-edit config to {candidate}")
    return candidate


def patch(config: Path, backup: Path) -> int:
    text = config.read_text()
    if BEGIN in text:
        if PATCH in text:
            print("lighttpd config already patched")
            return 0
        updated = re.sub(
            re.escape(BEGIN) + r".*?" + re.escape(END),
            PATCH,
            text,
            count=1,
            flags=re.DOTALL,
        )
        # Our own older block is not a vendor file: snapshot it for rollback
        # evidence, but leave the canonical vendor restore point alone.
        snapshot(config, backup)
        _atomic_write_text(config, updated)
        print("updated existing lighttpd patch block")
        return 0
    patched, count = REDIRECT.subn(PATCH, text, count=1)
    if count != 1:
        print("error: vendor redirect block not found; refusing to patch", file=sys.stderr)
        return 3
    # Always snapshot the pre-edit file, and make *this* snapshot the restore
    # point: after a firmware update the file is a new vendor revision, so the
    # backup from the previous revision is stale and must not be kept.
    snapshot_path = snapshot(config, backup)
    _atomic_copy(snapshot_path, backup)
    print(f"recorded vendor restore point {backup}")
    _atomic_write_text(config, patched)
    print("patched lighttpd HTTP redirect for /api/v2/")
    return 0


def restore(config: Path, backup: Path) -> int:
    if not backup.is_file():
        print(f"error: backup {backup} not found", file=sys.stderr)
        return 4
    try:
        text = backup.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: cannot read backup {backup}: {exc}", file=sys.stderr)
        return 5
    problem = validate_config_text(text)
    if problem is not None:
        print(
            f"error: refusing to restore {config} from {backup}: {problem}",
            file=sys.stderr,
        )
        print(
            "       the backup is not a complete lighttpd config; the live config "
            "was not touched" + _describe_snapshots(backup),
            file=sys.stderr,
        )
        return 5
    _atomic_copy(backup, config)
    print(f"restored {config} from {backup}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/lighttpd/lighttpd.conf")
    parser.add_argument("--backup", default="/db/scale-bridge/lighttpd.conf.vendor")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args(argv)
    config = Path(args.config)
    backup = Path(args.backup)
    if not config.is_file():
        print(f"error: {config} not found", file=sys.stderr)
        return 2
    if args.restore:
        return restore(config, backup)
    return patch(config, backup)


if __name__ == "__main__":
    raise SystemExit(main())
