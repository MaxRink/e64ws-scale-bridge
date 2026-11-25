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
``--restore`` copies that exact snapshot back.  The installer runs
``lighttpd -t`` before restarting and restores the snapshot on failure.
"""

from __future__ import annotations

import argparse
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


def snapshot(config: Path, backup: Path) -> Path:
    """Copy the pre-edit config to a unique sibling of *backup*."""

    backup.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = f"{backup.name}.{stamp}"
    candidate = backup.with_name(base)
    index = 1
    while candidate.exists():
        candidate = backup.with_name(f"{base}.{index}")
        index += 1
    shutil.copy2(config, candidate)
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
        config.write_text(updated)
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
    shutil.copy2(snapshot_path, backup)
    print(f"recorded vendor restore point {backup}")
    config.write_text(patched)
    print("patched lighttpd HTTP redirect for /api/v2/")
    return 0


def restore(config: Path, backup: Path) -> int:
    if not backup.exists():
        print(f"error: backup {backup} not found", file=sys.stderr)
        return 4
    shutil.copy2(backup, config)
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
