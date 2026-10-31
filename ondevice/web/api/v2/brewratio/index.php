<?php
declare(strict_types=1);
require_once(dirname(__DIR__) . '/lib.php');

if (scale_bridge_method() !== 'POST') {
    scale_bridge_send_json(405, ['error' => 'method not allowed']);
}
$body = scale_bridge_request_json();
$weight = scale_bridge_int($body['SYNC_BEVERAGE_WEIGHT'] ?? 0);
$filter = scale_bridge_int($body['SYNC_FILTER'] ?? 0);
if ($weight === null || $filter === null || $weight < 0 || $weight > 100000 || $filter < 0 || $filter > 100) {
    scale_bridge_send_json(400, ['error' => 'SYNC_BEVERAGE_WEIGHT/SYNC_FILTER out of range']);
}
$value = ['SYNC_BEVERAGE_WEIGHT' => (int) $weight, 'SYNC_FILTER' => (int) $filter];
if (!scale_bridge_write_json(scale_bridge_ratio_path(), $value)) {
    scale_bridge_send_json(500, ['error' => 'cannot persist brew ratio']);
}
scale_bridge_send_json(200, $value);
