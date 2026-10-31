#!/bin/sh
# Run the on-device PHP endpoint tests against PHP 8.4 in Docker.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PORT=${PHP_TEST_PORT:-18090}
TMP=$(mktemp -d "${TMPDIR:-/tmp}/e64-sb-php.XXXXXX")
NAME="e64-sb-php-$$"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

docker run -d --rm --name "$NAME" \
    -p "127.0.0.1:$PORT:80" \
    -v "$ROOT:/w" -w /w \
    -v "$TMP:/state" \
    -e SCALE_BRIDGE_STATE=/state/state.json \
    -e SCALE_BRIDGE_RATIO=/state/brewratio.json \
    -e SCALE_BRIDGE_BREW_START=/state/brew-start.json \
    -e SCALE_BRIDGE_ESP_STATE=/state/esp-state.json \
    -e SCALE_BRIDGE_INGEST_TOKEN=test-token \
    -e SCALE_BRIDGE_NO_AUTH=1 \
    php:8.4-cli php -S 0.0.0.0:80 -t ondevice/web >/dev/null

i=0
while [ "$i" -lt 50 ]; do
    if curl -fsS "http://127.0.0.1:$PORT/api/v2/machine" >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 0.1
done

cd "$ROOT"
PHP_BASE="http://127.0.0.1:$PORT" PHP_STATE_DIR="$TMP" \
    python3 -m unittest tests.test_ondevice_php -v
