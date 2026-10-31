<?php
declare(strict_types=1);

// Vendor session integration.  The page lives under /usr/local/www/scale, so
// chdir first: sessionManager.php includes sessionHelper.php/checkLogin.php by
// relative name.  SCALE_BRIDGE_NO_AUTH is a test-only escape hatch.
if (getenv('SCALE_BRIDGE_NO_AUTH') !== '1') {
    $vendor = '/usr/local/www/sessionManager.php';
    if (!is_file($vendor)) {
        http_response_code(503);
        echo 'Vendor session manager not found; is this the E64 web root?';
        exit;
    }
    chdir('/usr/local/www');
    require_once('sessionManager.php');
}

require_once(__DIR__ . '/../api/v2/lib.php');

$state = scale_bridge_state();
$ratio = scale_bridge_ratio();
$last = is_array($state['last_shot'] ?? null) ? $state['last_shot'] : null;
$connected = (bool) ($state['connected'] ?? false);
$phase = (string) ($state['phase'] ?? 'idle');
$updated = (int) ($state['updated_ms'] ?? 0);
$age = $updated > 0 ? max(0, scale_bridge_now_ms() - $updated) : null;

function h(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>E64 scale bridge</title>
    <link rel="stylesheet" type="text/css" href="/css/style.css">
    <style>
        .scale-card { max-width: 760px; margin: 2rem auto; padding: 1rem 1.5rem; }
        .scale-card table { width: 100%; border-collapse: collapse; }
        .scale-card th, .scale-card td { text-align: left; padding: .4rem .2rem; border-bottom: 1px solid #ddd; }
        .scale-ok { color: #1b7f3a; font-weight: 600; }
        .scale-bad { color: #a3261b; font-weight: 600; }
        .scale-muted { color: #666; }
        .scale-value { font-size: 1.6rem; font-variant-numeric: tabular-nums; }
    </style>
</head>
<body>
<div class="scale-card">
    <h1>Scale bridge</h1>
    <p class="scale-muted">On-device OpenScale/HDS reader. This page is read-only;
        the grinder's M2M screen is the control surface.</p>

    <p>
        <?php if ($connected): ?>
            <span class="scale-ok">Scale connected</span>
        <?php else: ?>
            <span class="scale-bad">Scale not connected</span>
        <?php endif; ?>
        <?php if ($age !== null): ?>
            <span class="scale-muted">· state age <?= h((string) $age) ?> ms</span>
        <?php endif; ?>
    </p>

    <table>
        <tr><th>Source</th><td><?= h((string) ($state['source'] ?? '')) ?></td></tr>
        <tr><th>Phase</th><td><?= h($phase) ?></td></tr>
        <tr><th>Live weight</th><td class="scale-value"><?= h(number_format((float) ($state['live_grams'] ?? 0), 1)) ?> g</td></tr>
        <tr><th>Shot count</th><td><?= h((string) (int) ($state['shot_count'] ?? 0)) ?></td></tr>
        <tr><th>Reconnects</th><td><?= h((string) (int) ($state['reconnects'] ?? 0)) ?></td></tr>
        <?php if ($last !== null): ?>
            <tr><th>Last shot</th>
                <td>
                    <?= h(number_format((float) ($last['mass_g'] ?? 0), 1)) ?> g /
                    <?= h(number_format((float) ($last['duration_ms'] ?? 0) / 1000.0, 1)) ?> s
                    (<?= !empty($last['valid']) ? 'valid' : 'short' ?>)
                </td>
            </tr>
            <tr><th>Last reason</th><td><?= h((string) ($last['reason'] ?? '')) ?></td></tr>
        <?php else: ?>
            <tr><th>Last shot</th><td class="scale-muted">none yet</td></tr>
        <?php endif; ?>
        <tr><th>Brew ratio</th><td><?= h((string) (int) ($ratio['SYNC_BEVERAGE_WEIGHT'] ?? 0)) ?> g /
                filter <?= h((string) (int) ($ratio['SYNC_FILTER'] ?? 0)) ?></td></tr>
    </table>

    <?php if (!empty($state['error'])): ?>
        <p class="scale-bad">Last error: <?= h((string) $state['error']) ?></p>
    <?php endif; ?>

    <p class="scale-muted">
        Xenia endpoints: <code>/api/v2/mako</code>, <code>/api/v2/machine</code>,
        <code>/api/v2/brewratio</code>, <code>/api/v2/scripts/execute</code>.
        Point the grinder's M2M host at this unit's LAN address.
    </p>
    <p><a href="/scale/">Refresh</a> · <a href="/">Back to the grinder</a></p>
</div>
</body>
</html>
