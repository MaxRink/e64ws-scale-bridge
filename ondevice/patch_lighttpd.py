#!/usr/bin/env python3
"""Patch the vendor lighttpd config for the on-device Xenia routes.

The E64 Xenia client is HTTP-only: ``RestClient::open(host, 80)`` builds
``http://<host>/api/v2/...`` and does not use TLS.  The vendor lighttpd config
redirects every HTTP request to HTTPS, which would make the local Xenia client
fail before PHP is reached.

This script makes one narrow, marked change:

* exempt only ``/api/v2/*`` from the HTTP->HTTPS redirect;
* rewrite the four exact Xenia paths to their ``index.php`` so the client does
  not have to follow a directory redirect;
* leave every other HTTP path redirected to HTTPS.

It is idempotent, keeps a vendor backup, and can restore it.  The installer
runs ``lighttpd -t`` before restarting and restores the backup on failure.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
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
# four local Xenia routes from the global HTTPS redirect and rewrite them to
# their index.php files so no directory redirect is needed.
$HTTP["scheme"] == "http" {{
    $HTTP["url"] !~ "^/api/v2/" {{
        $HTTP["host"] =~ ".*" {{
            url.redirect = (".*" => "https://%0$0")
        }}
    }}
}}
$HTTP["url"] =~ "^/api/v2/(mako|machine|brewratio|scripts/execute)$" {{
    url.rewrite-once = ( "^/api/v2/(mako|machine|brewratio|scripts/execute)$" => "/api/v2/$1/index.php" )
}}
{END}'''


def patch(config: Path, backup: Path) -> int:
    text = config.read_text()
    if BEGIN in text:
        print("lighttpd config already patched")
        return 0
    patched, count = REDIRECT.subn(PATCH, text, count=1)
    if count != 1:
        print("error: vendor redirect block not found; refusing to patch", file=sys.stderr)
        return 3
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config, backup)
        print(f"backed up vendor config to {backup}")
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
