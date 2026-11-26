#!/bin/sh
#
# Remove the on-device scale bridge.  Keeps /db/scale-bridge (config and shot
# history) unless --purge is passed.  Refuses to remove the web routes if the
# installer marker is missing.
#
# The /api/v2/ HTTP exemption is cleartext and lives in the vendor lighttpd
# config, so leaving it behind is a security outcome, not a cosmetic one.  The
# restore is therefore loud: if the vendor backup is missing or rejected, this
# script says so, leaves the code in place so it can be re-run, and exits
# non-zero instead of printing "Uninstalled." over a still-open exemption.

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
DB=/db/scale-bridge
STORE=/storage/scale-bridge
LIGHTTPD_CONF=/etc/lighttpd/lighttpd.conf
BACKUP=$DB/lighttpd.conf.vendor
PATCHER=$STORE/ondevice/patch_lighttpd.py
LIGHTTPD=${SCALE_BRIDGE_LIGHTTPD:-/usr/sbin/lighttpd}
MARKER='# BEGIN E64 scale bridge Xenia HTTP'
FAILED=0

say() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

if [ -x "$INIT" ]; then
    "$INIT" stop || true
    rm -f "$INIT"
fi

# Keep a copy of whatever is running before touching the config, so a restore
# that turns out to be invalid can be undone instead of bricking the web
# server.
SAFETY=""
if [ -f "$LIGHTTPD_CONF" ]; then
    SAFETY=/tmp/lighttpd.conf.preuninstall.$$
    cp "$LIGHTTPD_CONF" "$SAFETY" || SAFETY=""
fi

if [ -f "$LIGHTTPD_CONF" ] && grep -qF "$MARKER" "$LIGHTTPD_CONF"; then
    say "Removing the /api/v2/ HTTP exemption"
    if [ ! -f "$BACKUP" ]; then
        warn "error: $LIGHTTPD_CONF still exempts /api/v2/ from HTTPS, but the"
        warn "       vendor backup $BACKUP is missing, so it cannot be restored."
        FAILED=1
    elif [ ! -f "$PATCHER" ]; then
        warn "error: $LIGHTTPD_CONF still exempts /api/v2/ from HTTPS, but"
        warn "       $PATCHER is missing, so it cannot be restored."
        FAILED=1
    else
        python3 "$PATCHER" --restore \
            --config "$LIGHTTPD_CONF" \
            --backup "$BACKUP" || FAILED=1
        if grep -qF "$MARKER" "$LIGHTTPD_CONF" 2>/dev/null; then
            warn "error: the /api/v2/ HTTP exemption is STILL INSTALLED in"
            warn "       $LIGHTTPD_CONF after the restore."
            FAILED=1
        fi
    fi
fi

if [ "$FAILED" = 0 ] && [ -f "$LIGHTTPD_CONF" ] && [ -x "$LIGHTTPD" ]; then
    if "$LIGHTTPD" -t -f "$LIGHTTPD_CONF" >/dev/null 2>&1; then
        if [ -x /etc/init.d/S50lighttpd ]; then
            /etc/init.d/S50lighttpd restart || true
        fi
    else
        warn "error: $LIGHTTPD_CONF is not a valid lighttpd config after the restore."
        FAILED=1
    fi
fi

if [ "$FAILED" != 0 ]; then
    if [ -n "$SAFETY" ]; then
        # Undo a half-restore and keep the web server working, then tell the
        # operator exactly what is still installed.  The copy goes away once it
        # is back in place, so a failed uninstall does not leave a stray copy of
        # the lighttpd config in /tmp; if the copy back fails it is kept.
        if cp "$SAFETY" "$LIGHTTPD_CONF"; then
            rm -f "$SAFETY"
        fi
        if [ -x "$LIGHTTPD" ] && "$LIGHTTPD" -t -f "$LIGHTTPD_CONF" >/dev/null 2>&1; then
            if [ -x /etc/init.d/S50lighttpd ]; then
                /etc/init.d/S50lighttpd restart || true
            fi
        fi
    fi
    warn ""
    warn "UNINSTALL INCOMPLETE: $LIGHTTPD_CONF could not be restored."
    if grep -qF "$MARKER" "$LIGHTTPD_CONF" 2>/dev/null; then
        warn "  The /api/v2/ HTTP exemption is STILL INSTALLED: every /api/v2/"
        warn "  request, including brewratio writes and the ingest token, is"
        warn "  still plain HTTP on the LAN."
    fi
    warn "  $STORE was left in place so you can fix this and re-run:"
    warn "    ondevice/uninstall.sh --yes"
    warn "  Recover a complete vendor config from the firmware image or a"
    warn "  $BACKUP.* snapshot, put it at $BACKUP, then re-run."
    exit 1
fi

if [ -n "$SAFETY" ]; then
    rm -f "$SAFETY"
fi

if [ -e "$WEB/api/v2/.scale-bridge" ]; then
    rm -rf "$WEB/api/v2"
else
    warn "warning: $WEB/api/v2 has no installer marker; leaving it"
fi
if [ -d "$WEB/scale" ]; then
    rm -rf "$WEB/scale"
fi

rm -rf "$STORE"

if [ "$PURGE" = 1 ]; then
    rm -rf "$DB"
    say "Removed $DB (config and shot history)."
else
    say "Kept $DB. Use --purge to remove it."
fi

say "Uninstalled. Reboot or stop the scale daemon if it is still running."
