<?php
declare(strict_types=1);
require_once(dirname(__DIR__, 2) . '/lib.php');

if (scale_bridge_method() !== 'POST') {
    scale_bridge_send_json(405, ['error' => 'method not allowed']);
}
$body = scale_bridge_request_json();
$id = $body['ID'] ?? null;
if ($id !== null && !is_string($id) && !is_int($id)) {
    scale_bridge_send_json(400, ['error' => 'ID must be a string or integer']);
}
scale_bridge_write_json(
    scale_bridge_env_path('SCALE_BRIDGE_BREW_START', SCALE_BRIDGE_BREW_START_DEFAULT),
    ['at_ms' => scale_bridge_now_ms(), 'id' => $id]
);
scale_bridge_send_json(200, ['ok' => true, 'ID' => $id]);
