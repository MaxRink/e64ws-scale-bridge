#!/bin/sh
#
# Remove the on-device scale bridge.  Keeps /db/scale-bridge (config and shot
# history) unless --purge is passed.  Refuses to remove the web routes if the
# installer marker is missing.

set -eu

YES=0
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --yes) YES=1 ;;
        --purge) PURGE=1 ;;
        -h|--help)
            echo "usage: $0 --yes [--purge]"
            exit 0
            ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

if [ "$YES" != 1 ]; then
    echo "Refusing to remove files without --yes." >&2
    exit 1
fi
if [ "$(id -u)" != 0 ]; then
    echo "error: run as root" >&2
    exit 1
fi

WEB=/usr/local/www
INIT=/etc/init.d/S95scalebridge

if [ -x "$INIT" ]; then
    "$INIT" stop || true
    rm -f "$INIT"
fi

if [ -e "$WEB/api/v2/.scale-bridge" ]; then
    rm -rf "$WEB/api/v2"
else
    echo "warning: $WEB/api/v2 has no installer marker; leaving it" >&2
fi
if [ -d "$WEB/scale" ]; then
    rm -rf "$WEB/scale"
fi

rm -rf /storage/scale-bridge

if [ "$PURGE" = 1 ]; then
    rm -rf /db/scale-bridge
    echo "Removed /db/scale-bridge (config and shot history)."
else
    echo "Kept /db/scale-bridge. Use --purge to remove it."
fi

echo "Uninstalled. Reboot or stop the scale daemon if it is still running."
