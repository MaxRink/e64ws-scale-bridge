#!/bin/sh
#
# Install the on-device scale bridge on a rooted E64 WS.
#
# This touches two writable areas:
#   /storage/scale-bridge        code, config-independent
#   /db/scale-bridge             config + shot history (survives normal A/B)
# and two app/rootfs paths:
#   /usr/local/www/api/v2        Xenia routes (lost on signed update)
#   /usr/local/www/scale         WebUI page (lost on signed update)
#   /etc/init.d/S95scalebridge   init hook (lost on signed update)
#
# It does not remount, patch the HMI, change lighttpd, or touch the ESP32.

set -eu

DRY_RUN=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        -h|--help)
            echo "usage: $0 [--dry-run] [--force]"
            exit 0
            ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEST=/storage/scale-bridge
DB=/db/scale-bridge
RUN=/run/scale-bridge
WEB=/usr/local/www
INIT=/etc/init.d/S95scalebridge

say() { printf '%s\n' "$*"; }
run() {
    if [ "$DRY_RUN" = 1 ]; then
        say "  + $*"
    else
        "$@"
    fi
}

if [ "$(id -u)" != 0 ] && [ "$DRY_RUN" = 0 ]; then
    say "error: run as root (or use --dry-run to preview)" >&2
    exit 1
fi

if [ ! -r "$ROOT/ondevice/scalebridged.py" ]; then
    say "error: $ROOT does not look like the scale-bridge repo" >&2
    exit 1
fi

if [ "$DRY_RUN" = 0 ]; then
    if ! touch "$WEB/.scale-bridge-write-test" 2>/dev/null; then
        say "error: $WEB is read-only." >&2
        say "Run the vendor unlock for the app slot first (e.g. unlockapp), then retry." >&2
        exit 1
    fi
    rm -f "$WEB/.scale-bridge-write-test"
    if ! touch "$INIT.write-test" 2>/dev/null; then
        say "error: /etc/init.d is read-only." >&2
        say "Run the vendor rootfs unlock first (e.g. unlockrfs), then retry." >&2
        exit 1
    fi
    rm -f "$INIT.write-test"
fi

if [ -e "$WEB/api/v2" ] && [ ! -e "$WEB/api/v2/.scale-bridge" ] && [ "$FORCE" = 0 ]; then
    say "error: $WEB/api/v2 exists and was not created by this installer." >&2
    say "Refusing to overwrite it; inspect it or use --force." >&2
    exit 1
fi
if [ -e "$INIT" ] && ! cmp -s "$ROOT/ondevice/S95scalebridge" "$INIT" && [ "$FORCE" = 0 ]; then
    say "error: $INIT already exists and differs from this installer." >&2
    say "Refusing to overwrite it; inspect it or use --force." >&2
    exit 1
fi

say "Installing code to $DEST"
run install -d "$DEST/ondevice" "$DEST/tools"
run install -m 0755 "$ROOT/ondevice/scalebridged.py" "$DEST/ondevice/scalebridged.py"
for file in scale_bridge.py openscale_client.py e64ws_protocol.py __init__.py; do
    if [ -f "$ROOT/tools/$file" ]; then
        run install -m 0644 "$ROOT/tools/$file" "$DEST/tools/$file"
    fi
done

say "Preparing state directories"
run install -d -m 0775 "$DB" "$RUN"
if [ "$DRY_RUN" = 0 ]; then
    chown -R www-data:www-data "$DB" "$RUN" 2>/dev/null || true
fi

if [ ! -e "$DB/config.json" ]; then
    say "Writing default config $DB/config.json"
    if [ "$DRY_RUN" = 1 ]; then
        say "  + default config (host hds.local, port 80, /snapshot)"
    else
        cat > "$DB/config.json" <<'EOF'
{
  "host": "hds.local",
  "port": 80,
  "path": "/snapshot",
  "timeout": 10,
  "finishing_ms": 3000
}
EOF
        chown www-data:www-data "$DB/config.json" 2>/dev/null || true
    fi
else
    say "Keeping existing $DB/config.json"
fi

say "Installing Xenia endpoints under $WEB/api/v2"
run install -d "$WEB/api/v2"
if [ "$DRY_RUN" = 0 ]; then
    cp -R "$ROOT/ondevice/web/api/v2/." "$WEB/api/v2/"
    touch "$WEB/api/v2/.scale-bridge"
    chown -R root:root "$WEB/api/v2"
    chmod -R a+rX "$WEB/api/v2"
fi

say "Installing WebUI page under $WEB/scale"
run install -d "$WEB/scale"
if [ "$DRY_RUN" = 0 ]; then
    cp -R "$ROOT/ondevice/web/scale/." "$WEB/scale/"
    chown -R root:root "$WEB/scale"
    chmod -R a+rX "$WEB/scale"
fi

say "Installing init script $INIT"
run install -m 0755 "$ROOT/ondevice/S95scalebridge" "$INIT"

if [ "$DRY_RUN" = 0 ]; then
    say "Starting the daemon"
    "$INIT" start || {
        say "warning: start failed; check /log/scalebridged.log" >&2
    }
else
    say "  + $INIT start"
fi

say ""
say "Installed. Next steps:"
say "  1. Edit $DB/config.json (host/port/path of the scale)."
say "  2. Restart: $INIT restart"
say "  3. Open http://<grinder>/scale/ in the vendor WebUI."
say "  4. Point the E64 M2M host at this unit's LAN address."
say "  5. Verify GET /api/v2/mako returns REAL_EXTRACTION_TIME_MS after a shot."
say ""
say "Rollback: ondevice/uninstall.sh --yes"
