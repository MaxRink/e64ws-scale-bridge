<?php
declare(strict_types=1);
require_once(dirname(__DIR__) . '/lib.php');

/**
 * ESP32 BLE-gateway ingest.
 *
 * The ESP32 runs the BLE central + shot detector and POSTs an already-derived
 * state snapshot here. The WiFi daemon is untouched; a fresh ESP snapshot wins
 * for SCALE_BRIDGE_ESP_FRESH_MS and then the WiFi state resumes.
 *
 * Auth: X-Scale-Token: <ingest_token from /db/scale-bridge/config.json>.
 * This endpoint is intentionally separate from the E64-facing Xenia routes.
 */

if (scale_bridge_method() !== 'POST') {
    scale_bridge_send_json(405, ['error' => 'method not allowed']);
}
$token = scale_bridge_ingest_token();
if ($token === '') {
    scale_bridge_send_json(503, ['error' => 'ingest disabled: set ingest_token in config']);
}
$provided = $_SERVER['HTTP_X_SCALE_TOKEN'] ?? '';
if (!is_string($provided) || $provided === '' || !hash_equals($token, $provided)) {
    scale_bridge_send_json(403, ['error' => 'bad ingest token']);
}
$raw = file_get_contents('php://input');
if (!is_string($raw) || strlen($raw) > 16384) {
    scale_bridge_send_json(413, ['error' => 'body too large or unreadable']);
}
$body = json_decode($raw, true);
if (!is_array($body)) {
    scale_bridge_send_json(400, ['error' => 'invalid JSON body']);
}

$phase = $body['phase'] ?? 'idle';
if (!is_string($phase) || !in_array($phase, ['idle', 'serving', 'finishing'], true)) {
    scale_bridge_send_json(400, ['error' => 'phase must be idle|serving|finishing']);
}
$number = static function (mixed $value, string $name, float $default = 0.0): float {
    if ($value === null) {
        return $default;
    }
    if (!is_int($value) && !is_float($value)) {
        scale_bridge_send_json(400, ['error' => $name . ' must be a number']);
    }
    $result = (float) $value;
    if (!is_finite($result) || $result < 0) {
        scale_bridge_send_json(400, ['error' => $name . ' must be a finite non-negative number']);
    }
    return $result;
};
$live_grams = $number($body['live_grams'] ?? 0, 'live_grams');
$flow_g_s = $number($body['flow_g_s'] ?? 0, 'flow_g_s');
$active_ms = (int) $number($body['active_ms'] ?? 0, 'active_ms');
$shot_count = (int) $number($body['shot_count'] ?? 0, 'shot_count');
$finishing_until = (int) $number($body['finishing_until_ms'] ?? 0, 'finishing_until_ms');
if ($phase === 'finishing' && $finishing_until === 0) {
    $finishing_until = scale_bridge_now_ms() + 3000;
}
$last = $body['last_shot'] ?? null;
if ($last !== null) {
    if (!is_array($last)) {
        scale_bridge_send_json(400, ['error' => 'last_shot must be an object or null']);
    }
    $last = [
        'mass_g' => $number($last['mass_g'] ?? 0, 'last_shot.mass_g'),
        'duration_ms' => $number($last['duration_ms'] ?? 0, 'last_shot.duration_ms'),
        'valid' => (bool) ($last['valid'] ?? false),
        'reason' => is_string($last['reason'] ?? null) ? substr($last['reason'], 0, 128) : '',
        'at_ms' => (int) $number($last['at_ms'] ?? 0, 'last_shot.at_ms'),
    ];
}
$source = is_string($body['source'] ?? null) ? substr($body['source'], 0, 64) : 'esp32-ble';
$state = [
    'schema' => 1,
    'source' => $source,
    'connected' => true,
    'phase' => $phase,
    'live_grams' => round($live_grams, 3),
    'live_ms' => (int) $number($body['live_ms'] ?? 0, 'live_ms'),
    'flow_g_s' => round($flow_g_s, 3),
    'active_ms' => $active_ms,
    'last_shot' => $last,
    'finishing_until_ms' => $finishing_until,
    'shot_count' => $shot_count,
    'reconnects' => (int) $number($body['reconnects'] ?? 0, 'reconnects'),
    'error' => is_string($body['error'] ?? null) ? substr($body['error'], 0, 128) : '',
    'updated_ms' => scale_bridge_now_ms(),
];
if (!scale_bridge_write_json(scale_bridge_esp_state_path(), $state)) {
    scale_bridge_send_json(500, ['error' => 'cannot persist ESP state']);
}
scale_bridge_send_json(200, ['ok' => true, 'updated_ms' => $state['updated_ms']]);
