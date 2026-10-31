<?php
declare(strict_types=1);

/**
 * Shared state and Xenia response helpers for the on-device scale bridge.
 *
 * The daemon (ondevice/scalebridged.py) owns /run/scale-bridge/state.json.
 * These endpoints are deliberately read-mostly: the E64's M2M client polls
 * them without authentication, so they expose only shot/state data, never
 * control or grinder actions.
 */

const SCALE_BRIDGE_STATE_DEFAULT = '/run/scale-bridge/state.json';
const SCALE_BRIDGE_RATIO_DEFAULT = '/run/scale-bridge/brewratio.json';
const SCALE_BRIDGE_BREW_START_DEFAULT = '/run/scale-bridge/brew-start.json';
const SCALE_BRIDGE_CONFIG_DEFAULT = '/db/scale-bridge/config.json';
const SCALE_BRIDGE_ESP_STATE_DEFAULT = '/run/scale-bridge/esp-state.json';
const SCALE_BRIDGE_ESP_FRESH_MS = 5000;

function scale_bridge_env_path(string $name, string $default): string
{
    $value = getenv($name);
    return is_string($value) && $value !== '' ? $value : $default;
}

function scale_bridge_config_path(): string
{
    return scale_bridge_env_path('SCALE_BRIDGE_CONFIG', SCALE_BRIDGE_CONFIG_DEFAULT);
}

function scale_bridge_config(): array
{
    return scale_bridge_read_json(scale_bridge_config_path());
}

function scale_bridge_ingest_token(): string
{
    $token = scale_bridge_env_path('SCALE_BRIDGE_INGEST_TOKEN', '');
    if ($token !== '') {
        return $token;
    }
    $config = scale_bridge_config();
    return is_string($config['ingest_token'] ?? null) ? $config['ingest_token'] : '';
}

function scale_bridge_state_path(): string
{
    return scale_bridge_env_path('SCALE_BRIDGE_STATE', SCALE_BRIDGE_STATE_DEFAULT);
}

function scale_bridge_ratio_path(): string
{
    return scale_bridge_env_path('SCALE_BRIDGE_RATIO', SCALE_BRIDGE_RATIO_DEFAULT);
}

function scale_bridge_now_ms(): int
{
    // 32-bit PHP (the E64) cannot hold a millisecond epoch. Use a positive
    // 32-bit window; the daemon writes the same window. Wraps every ~24.8 d.
    return (int) fmod(microtime(true) * 1000.0, 2147483648.0);
}

function scale_bridge_timestamp(): string
{
    return date('Y-m-d H:i:s');
}

function scale_bridge_read_json(string $path, array $default = []): array
{
    if (!is_file($path) || !is_readable($path)) {
        return $default;
    }
    $raw = @file_get_contents($path);
    if (!is_string($raw) || $raw === '') {
        return $default;
    }
    $value = json_decode($raw, true);
    return is_array($value) ? $value : $default;
}

function scale_bridge_write_json(string $path, array $value): bool
{
    $dir = dirname($path);
    if (!is_dir($dir) && !@mkdir($dir, 0775, true) && !is_dir($dir)) {
        return false;
    }
    $tmp = $path . '.tmp.' . getmypid();
    $encoded = json_encode($value, JSON_UNESCAPED_SLASHES);
    if ($encoded === false) {
        return false;
    }
    if (@file_put_contents($tmp, $encoded, LOCK_EX) === false) {
        return false;
    }
    return @rename($tmp, $path);
}

function scale_bridge_esp_state_path(): string
{
    return scale_bridge_env_path('SCALE_BRIDGE_ESP_STATE', SCALE_BRIDGE_ESP_STATE_DEFAULT);
}

function scale_bridge_esp_state(): ?array
{
    $path = scale_bridge_esp_state_path();
    if (!is_file($path) || !is_readable($path)) {
        return null;
    }
    $value = scale_bridge_read_json($path);
    if ($value === []) {
        return null;
    }
    $updated = (int) ($value['updated_ms'] ?? 0);
    $now = scale_bridge_now_ms();
    $age = $now - $updated;
    if ($age < 0 || $age > SCALE_BRIDGE_ESP_FRESH_MS) {
        return null;
    }
    return $value;
}

function scale_bridge_state(): array
{
    $default = [
        'schema' => 1,
        'source' => '',
        'connected' => false,
        'phase' => 'idle',
        'ma_status' => 1,
        'extraction_status' => 0,
        'live_grams' => 0.0,
        'live_ms' => 0,
        'last_shot' => null,
        'finishing_until_ms' => 0,
        'shot_count' => 0,
        'reconnects' => 0,
        'error' => '',
        'updated_ms' => 0,
    ];
    // A fresh ESP32 BLE-gateway snapshot wins over the WiFi daemon. This lets
    // one HMI/PHP surface serve either source without duplicating Xenia.
    $esp = scale_bridge_esp_state();
    if ($esp !== null) {
        return array_merge($default, $esp);
    }
    return array_merge($default, scale_bridge_read_json(scale_bridge_state_path()));
}

function scale_bridge_ratio(): array
{
    $default = ['SYNC_BEVERAGE_WEIGHT' => 0, 'SYNC_FILTER' => 0];
    return array_merge($default, scale_bridge_read_json(scale_bridge_ratio_path()));
}

function scale_bridge_mako(array $state, array $ratio): array
{
    $phase = is_string($state['phase'] ?? null) ? $state['phase'] : 'idle';
    $last = is_array($state['last_shot'] ?? null) ? $state['last_shot'] : null;
    $now = scale_bridge_now_ms();
    $status = 1;
    $extraction = 0;
    $duration = 0;
    $volume = 0.0;
    $flow = 0.0;

    if ($phase === 'serving') {
        $status = 3;
        $extraction = 1;
        $volume = (float) ($state['live_grams'] ?? 0.0);
        $flow = (float) ($state['flow_g_s'] ?? 0.0);
        $duration = max(0, (int) ($state['active_ms'] ?? 0));
    } elseif ($phase === 'finishing' && $last !== null) {
        $until = (int) ($state['finishing_until_ms'] ?? 0);
        if ($until > 0 && $now < $until) {
            $status = 4;
            $extraction = 2;
            $volume = (float) ($last['mass_g'] ?? 0.0);
            $duration = max(0, (int) round((float) ($last['duration_ms'] ?? 0)));
        }
    }

    return [
        'MA_STATUS' => $status,
        'MA_EXTRACTION_STATUS' => $extraction,
        'REAL_EXTRACTION_TIME_MS' => $duration,
        'PU_SENS_FLOW_METER_VOLUME' => round($volume, 3),
        'PU_SENS_FLOW_METER_FLOWRATE' => round($flow, 3),
        'BG_SET_TEMP' => 93,
        'BG_SENS_TEMP' => 92,
        'TANK_LEVEL' => 1,
        // The vendor Xenia diagnosis screen labels this field "Script"; use it
        // to identify the local scale source without inventing a coffee recipe.
        'script' => 'SCALE',
        'TIMESTAMP' => scale_bridge_timestamp(),
        'MA_SYNC_BEVERAGE_WEIGHT' => (int) ($ratio['SYNC_BEVERAGE_WEIGHT'] ?? 0),
        'MA_SYNC_FILTER' => (int) ($ratio['SYNC_FILTER'] ?? 0),
    ];
}

function scale_bridge_machine(): array
{
    return [
        'MA_TYPE' => 'XENIA',
        'ma_type' => 'xenia',
        'MA_SN' => 'E64-ONDEVICE-0001',
        'FW_VERSION_MAJOR' => 3,
        'FW_VERSION_MINOR' => 87,
        'ESP_FW_MAJOR' => 3,
        'ESP_FW_MINOR' => 87,
        'TIMESTAMP' => scale_bridge_timestamp(),
    ];
}

function scale_bridge_send_json(int $status, array $value): void
{
    http_response_code($status);
    header('Content-Type: application/json');
    header('Cache-Control: no-store');
    echo json_encode($value, JSON_UNESCAPED_SLASHES);
    exit;
}

function scale_bridge_int(mixed $value): ?int
{
    // The E64 PHP build has no filter extension; validate without filter_var().
    if (is_int($value)) {
        return $value;
    }
    if (is_string($value) && preg_match('/^-?\d+$/', $value) === 1) {
        return (int) $value;
    }
    return null;
}

function scale_bridge_method(): string
{
    return strtoupper((string) ($_SERVER['REQUEST_METHOD'] ?? 'GET'));
}

function scale_bridge_request_json(): array
{
    $raw = file_get_contents('php://input');
    if (!is_string($raw) || trim($raw) === '') {
        return [];
    }
    $value = json_decode($raw, true);
    if (!is_array($value)) {
        scale_bridge_send_json(400, ['error' => 'invalid JSON body']);
    }
    return $value;
}
