<?php
declare(strict_types=1);
require_once(dirname(__DIR__) . '/lib.php');

if (scale_bridge_method() !== 'GET') {
    scale_bridge_send_json(405, ['error' => 'method not allowed']);
}
scale_bridge_send_json(200, scale_bridge_machine());
