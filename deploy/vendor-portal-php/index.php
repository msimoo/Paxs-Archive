<?php
/**
 * PAXS Vendor Licensing Portal — Master Admin Dashboard
 */

declare(strict_types=1);

require_once __DIR__ . '/config.php';
require_once __DIR__ . '/auth.php';
require_once __DIR__ . '/db.php';
require_once __DIR__ . '/crypto.php';

requireAuth();

$db = getDb();
$msg = '';
$msgType = 'ok';

// Handle POST actions
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $action = $_POST['action'] ?? '';
    $csrf = $_POST['csrf_token'] ?? '';

    if (!verifyCsrfToken($csrf)) {
        $msg = 'Invalid security token.';
        $msgType = 'err';
    } elseif ($action === 'create_license') {
        $name = trim($_POST['customer_name'] ?? '');
        $email = trim($_POST['customer_email'] ?? '');
        $org = trim($_POST['customer_org'] ?? '');
        $edition = strtolower($_POST['edition'] ?? 'hospital');
        $fingerprint = strtoupper(trim($_POST['fingerprint'] ?? '*'));
        $days = (int)($_POST['validity_days'] ?? 365);
        $isPerpetual = isset($_POST['is_perpetual']) && $_POST['is_perpetual'] === '1';
        $maxUsers = (int)($_POST['max_users'] ?? 25);
        $maxStudies = (int)($_POST['max_studies'] ?? 25000);
        $features = $_POST['features'] ?? [];

        if (empty($name)) {
            $msg = 'Customer name is required.';
            $msgType = 'err';
        } else {
            $issuedAt = gmdate('Y-m-d\TH:i:s\Z');
            $expiresAt = $isPerpetual ? null : gmdate('Y-m-d\TH:i:s\Z', strtotime("+{$days} days"));
            $licenseId = LicenseCrypto::generateLicenseId($edition);

            $licData = [
                'license_id' => $licenseId,
                'version' => '1.0',
                'customer_name' => $name,
                'customer_email' => $email ?: null,
                'customer_organization' => $org ?: null,
                'customer_country' => null,
                'product' => 'PAXS Archive & Viewer',
                'edition' => $edition,
                'machine_fingerprint' => $fingerprint ?: '*',
                'issued_at' => $issuedAt,
                'expires_at' => $expiresAt,
                'max_users' => $maxUsers,
                'max_studies_per_year' => $maxStudies,
                'features' => array_values($features)
            ];

            try {
                $containerBytes = LicenseCrypto::encryptContainer($licData);
                $containerB64 = base64_encode($containerBytes);

                $ins = $db->prepare("
                    INSERT INTO licenses (
                        license_id, customer_name, customer_email, customer_organization,
                        edition, machine_fingerprint, issued_at, expires_at, is_perpetual,
                        max_users, max_studies_per_year, features_json, status, container_b64
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
                ");
                $ins->execute([
                    $licenseId, $name, $email, $org, $edition, $fingerprint ?: '*',
                    $issuedAt, $expiresAt, $isPerpetual ? 1 : 0, $maxUsers, $maxStudies,
                    json_encode($features), $containerB64
                ]);

                $msg = "License {$licenseId} created successfully!";
            } catch (Exception $e) {
                $msg = "Error creating license: " . $e->getMessage();
                $msgType = 'err';
            }
        }
    } elseif ($action === 'create_key') {
        $edition = strtolower($_POST['edition'] ?? 'hospital');
        $maxAct = (int)($_POST['max_activations'] ?? 1);
        $days = (int)($_POST['expires_days'] ?? 365);
        $allowedEmail = trim($_POST['allowed_email'] ?? '');
        $features = $_POST['features'] ?? [];

        $key = LicenseCrypto::generateProductKey($edition);

        try {
            $ins = $db->prepare("
                INSERT INTO activation_keys (product_key, edition, max_activations, activations_count, allowed_email, expires_days, features_json, is_active)
                VALUES (?, ?, ?, 0, ?, ?, ?, 1)
            ");
            $ins->execute([$key, $edition, $maxAct, $allowedEmail ?: null, $days, json_encode($features)]);
            $msg = "Generated activation key: {$key}";
        } catch (Exception $e) {
            $msg = "Error generating key: " . $e->getMessage();
            $msgType = 'err';
        }
    } elseif ($action === 'revoke_license') {
        $id = (int)$_POST['id'];
        $reason = trim($_POST['reason'] ?? 'Revoked by vendor admin');
        $upd = $db->prepare("UPDATE licenses SET status = 'REVOKED', revocation_reason = ? WHERE id = ?");
        $upd->execute([$reason, $id]);
        $msg = "License marked as REVOKED.";
    } elseif ($action === 'extend_license') {
        $id = (int)$_POST['id'];
        $daysToAdd = (int)($_POST['days_to_add'] ?? 365);
        
        $sel = $db->prepare("SELECT * FROM licenses WHERE id = ?");
        $sel->execute([$id]);
        $lic = $sel->fetch();

        if ($lic) {
            $currentExp = $lic['expires_at'] ? strtotime($lic['expires_at']) : time();
            if ($currentExp < time()) $currentExp = time();
            $newExp = gmdate('Y-m-d\TH:i:s\Z', $currentExp + ($daysToAdd * 86400));

            $features = json_decode($lic['features_json'], true) ?: [];
            $licData = [
                'license_id' => $lic['license_id'],
                'version' => '1.0',
                'customer_name' => $lic['customer_name'],
                'customer_email' => $lic['customer_email'],
                'customer_organization' => $lic['customer_organization'],
                'customer_country' => null,
                'product' => 'PAXS Archive & Viewer',
                'edition' => $lic['edition'],
                'machine_fingerprint' => $lic['machine_fingerprint'],
                'issued_at' => $lic['issued_at'],
                'expires_at' => $newExp,
                'max_users' => (int)$lic['max_users'],
                'max_studies_per_year' => (int)$lic['max_studies_per_year'],
                'features' => $features
            ];

            $newContainerB64 = base64_encode(LicenseCrypto::encryptContainer($licData));
            $upd = $db->prepare("UPDATE licenses SET expires_at = ?, is_perpetual = 0, status = 'ACTIVE', container_b64 = ? WHERE id = ?");
            $upd->execute([$newExp, $newContainerB64, $id]);
            $msg = "License extended by {$daysToAdd} days!";
        }
    } elseif ($action === 'delete_license') {
        $id = (int)$_POST['id'];
        $del = $db->prepare("DELETE FROM licenses WHERE id = ?");
        $del->execute([$id]);
        $msg = "License deleted from database.";
    } elseif ($action === 'toggle_key') {
        $id = (int)$_POST['id'];
        $state = (int)$_POST['state'];
        $upd = $db->prepare("UPDATE activation_keys SET is_active = ? WHERE id = ?");
        $upd->execute([$state, $id]);
        $msg = $state ? "Key activated" : "Key deactivated";
    }
}

// Download .lic file action
if (isset($_GET['download']) && is_numeric($_GET['download'])) {
    $id = (int)$_GET['download'];
    $stmt = $db->prepare("SELECT * FROM licenses WHERE id = ? LIMIT 1");
    $stmt->execute([$id]);
    $lic = $stmt->fetch();
    if ($lic && !empty($lic['container_b64'])) {
        $bytes = base64_decode($lic['container_b64']);
        header('Content-Type: application/octet-stream');
        header('Content-Disposition: attachment; filename="' . $lic['license_id'] . '.lic"');
        header('Content-Length: ' . strlen($bytes));
        echo $bytes;
        exit;
    }
}

// Fetch dashboard data
$stats = [
    'total' => (int)$db->query("SELECT COUNT(*) FROM licenses")->fetchColumn(),
    'active' => (int)$db->query("SELECT COUNT(*) FROM licenses WHERE status = 'ACTIVE'")->fetchColumn(),
    'revoked' => (int)$db->query("SELECT COUNT(*) FROM licenses WHERE status = 'REVOKED'")->fetchColumn(),
    'keys' => (int)$db->query("SELECT COUNT(*) FROM activation_keys WHERE is_active = 1")->fetchColumn(),
    'activations' => (int)$db->query("SELECT COUNT(*) FROM activations_log")->fetchColumn(),
    'heartbeats' => (int)$db->query("SELECT COUNT(*) FROM heartbeat_log")->fetchColumn(),
];

$licenses = $db->query("SELECT * FROM licenses ORDER BY id DESC LIMIT 100")->fetchAll();
$keys = $db->query("SELECT * FROM activation_keys ORDER BY id DESC LIMIT 100")->fetchAll();
$activations = $db->query("SELECT * FROM activations_log ORDER BY id DESC LIMIT 50")->fetchAll();
$heartbeats = $db->query("SELECT * FROM heartbeat_log ORDER BY id DESC LIMIT 50")->fetchAll();

global $EDITIONS, $ALL_FEATURES;
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PAXS Licensing Portal — Vendor Master Dashboard</title>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600;700&display=swap">
    <style>
        :root {
            --bg-dark: #020617;
            --panel-bg: #0f172a;
            --card-bg: #1e293b;
            --border-color: rgba(51, 65, 85, 0.6);
            --accent-sky: #38bdf8;
            --accent-emerald: #34d399;
            --accent-amber: #fbbf24;
            --accent-rose: #f43f5e;
            --font-sans: 'Inter', system-ui, -apple-system, sans-serif;
            --font-mono: 'JetBrains Mono', monospace;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg-dark);
            color: #f8fafc;
            font-family: var(--font-sans);
            padding: 24px;
            min-height: 100vh;
        }

        .header-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 16px 24px;
            background: var(--panel-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            margin-bottom: 24px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.4);
        }

        .brand-title {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 18px;
            font-weight: 800;
        }

        .brand-title span {
            background: linear-gradient(90deg, #38bdf8, #34d399);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
        }

        .btn {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 14px;
            font-family: var(--font-sans);
            font-size: 13px;
            font-weight: 600;
            border-radius: 8px;
            border: 1px solid var(--border-color);
            background: rgba(30, 41, 59, 0.8);
            color: #ffffff;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.2s ease;
        }

        .btn:hover {
            border-color: var(--accent-sky);
            background: rgba(56, 189, 248, 0.15);
            transform: translateY(-1px);
        }

        .btn-primary {
            background: linear-gradient(135deg, #0284c7, #0d9488);
            border: none;
            font-weight: 700;
            box-shadow: 0 4px 14px rgba(2, 132, 199, 0.4);
        }

        .btn-primary:hover {
            background: linear-gradient(135deg, #0369a1, #0f766e);
            box-shadow: 0 6px 18px rgba(2, 132, 199, 0.6);
        }

        .btn-danger {
            background: rgba(244, 63, 94, 0.15);
            border-color: rgba(244, 63, 94, 0.4);
            color: #f43f5e;
        }

        .btn-danger:hover {
            background: rgba(244, 63, 94, 0.3);
            border-color: #f43f5e;
        }

        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }

        .kpi-card {
            background: var(--panel-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px 20px;
            box-shadow: 0 4px 14px rgba(0,0,0,0.3);
        }

        .kpi-label {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #94a3b8;
            margin-bottom: 4px;
        }

        .kpi-val {
            font-size: 24px;
            font-weight: 800;
            color: #ffffff;
        }

        .tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 8px;
        }

        .tab-btn {
            padding: 10px 18px;
            font-size: 13.5px;
            font-weight: 600;
            color: #94a3b8;
            background: transparent;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .tab-btn.active {
            color: #ffffff;
            background: rgba(56, 189, 248, 0.15);
            border: 1px solid rgba(56, 189, 248, 0.4);
        }

        .tab-content { display: none; }
        .tab-content.active { display: block; }

        .panel-card {
            background: var(--panel-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.5);
            margin-bottom: 20px;
        }

        .panel-header {
            font-size: 16px;
            font-weight: 700;
            color: var(--accent-sky);
            margin-bottom: 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 12px;
        }

        .table th, .table td {
            padding: 12px 14px;
            text-align: left;
            border-bottom: 1px solid var(--border-color);
            font-size: 13px;
        }

        .table th {
            color: var(--accent-sky);
            font-weight: 700;
            background: rgba(30, 41, 59, 0.5);
        }

        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 6px;
            font-size: 11px;
            font-family: var(--font-mono);
            font-weight: 700;
        }
        .badge-green { background: rgba(52, 211, 153, 0.2); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.4); }
        .badge-blue { background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); }
        .badge-amber { background: rgba(251, 191, 36, 0.2); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.4); }
        .badge-rose { background: rgba(244, 63, 94, 0.2); color: #f43f5e; border: 1px solid rgba(244, 63, 94, 0.4); }

        .form-group { margin-bottom: 16px; }
        .form-group label {
            display: block;
            font-size: 12px;
            font-weight: 600;
            color: #94a3b8;
            margin-bottom: 6px;
        }
        .form-control {
            width: 100%;
            padding: 9px 12px;
            font-family: var(--font-sans);
            font-size: 13.5px;
            background: rgba(30, 41, 59, 0.7);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: #ffffff;
        }
        .form-control:focus { outline: none; border-color: var(--accent-sky); }

        .modal {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(2, 6, 23, 0.85);
            backdrop-filter: blur(8px);
            z-index: 10000;
            align-items: center;
            justify-content: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            background: var(--panel-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            width: 620px;
            max-width: 92vw;
            max-height: 90vh;
            overflow-y: auto;
            box-shadow: 0 25px 60px rgba(0,0,0,0.8);
        }

        .toast {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: #10b981;
            color: white;
            padding: 12px 24px;
            border-radius: 8px;
            font-weight: 700;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            z-index: 20000;
        }
    </style>
</head>
<body>

    <div class="header-bar">
        <div class="brand-title">
            <svg width="24" height="24" viewBox="0 0 32 32" fill="none">
                <rect x="4" y="6" width="24" height="20" rx="4" stroke="#22d3ee" stroke-width="2"/>
                <path d="M4 12h24M4 18h24M10 6v20M22 6v20" stroke="#2dd4bf" stroke-width="1.5"/>
            </svg>
            <span>PAXS Licensing Portal · Vendor Master</span>
        </div>
        <div style="display: flex; align-items: center; gap: 14px;">
            <span style="font-size: 13px; color: #94a3b8;">Admin: <b style="color: #ffffff;"><?= htmlspecialchars($_SESSION['paxs_admin_user'] ?? 'admin') ?></b></span>
            <a href="logout.php" class="btn btn-danger" style="padding: 6px 12px; font-size: 12px;">Sign Out</a>
        </div>
    </div>

    <?php if ($msg): ?>
        <div class="toast" style="background: <?= $msgType === 'err' ? '#e11d48' : '#10b981' ?>;">
            <?= htmlspecialchars($msg) ?>
        </div>
    <?php endif; ?>

    <!-- KPI Metrics -->
    <div class="kpi-grid">
        <div class="kpi-card">
            <div class="kpi-label">Total Licenses</div>
            <div class="kpi-val"><?= $stats['total'] ?></div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">Active Subscriptions</div>
            <div class="kpi-val" style="color: #34d399;"><?= $stats['active'] ?></div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">Revoked / Terminated</div>
            <div class="kpi-val" style="color: #f43f5e;"><?= $stats['revoked'] ?></div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">Active Activation Keys</div>
            <div class="kpi-val" style="color: #38bdf8;"><?= $stats['keys'] ?></div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">Remote Activations</div>
            <div class="kpi-val"><?= $stats['activations'] ?></div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">Server Heartbeats</div>
            <div class="kpi-val"><?= $stats['heartbeats'] ?></div>
        </div>
    </div>

    <!-- Navigation Tabs -->
    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('tab-licenses')">📋 Issued Licenses (<?= count($licenses) ?>)</button>
        <button class="tab-btn" onclick="switchTab('tab-keys')">🔑 Product Activation Keys (<?= count($keys) ?>)</button>
        <button class="tab-btn" onclick="switchTab('tab-telemetry')">📡 Heartbeats &amp; Telemetry</button>
        <button class="tab-btn" onclick="switchTab('tab-crypto')">🔐 Public Key &amp; API Integration</button>
    </div>

    <!-- Tab 1: Licenses -->
    <div id="tab-licenses" class="tab-content active">
        <div class="panel-card">
            <div class="panel-header">
                <span>All Licensed Instances</span>
                <button class="btn btn-primary" onclick="openModal('create-license-modal')">➕ Issue New License Certificate</button>
            </div>
            
            <table class="table">
                <thead>
                    <tr>
                        <th>License ID</th>
                        <th>Customer / Hospital</th>
                        <th>Edition</th>
                        <th>Hardware Fingerprint</th>
                        <th>Expires</th>
                        <th>Seats / Cap</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    <?php if (empty($licenses)): ?>
                        <tr><td colspan="8" style="color: #94a3b8; text-align: center;">No licenses issued yet. Click "+ Issue New License Certificate".</td></tr>
                    <?php else: ?>
                        <?php foreach ($licenses as $l): ?>
                            <tr>
                                <td style="font-family: var(--font-mono); font-weight: 700; color: #38bdf8; font-size: 12px;"><?= htmlspecialchars($l['license_id']) ?></td>
                                <td>
                                    <b><?= htmlspecialchars($l['customer_name']) ?></b>
                                    <?php if ($l['customer_organization']): ?>
                                        <br><small style="color: #94a3b8;"><?= htmlspecialchars($l['customer_organization']) ?></small>
                                    <?php endif; ?>
                                </td>
                                <td><span class="badge badge-blue"><?= strtoupper($l['edition']) ?></span></td>
                                <td style="font-family: var(--font-mono); font-size: 11px;">
                                    <?= $l['machine_fingerprint'] === '*' ? '<span class="badge">WILDCARD / CLOUD</span>' : htmlspecialchars($l['machine_fingerprint']) ?>
                                </td>
                                <td>
                                    <?= $l['is_perpetual'] ? '<b>PERPETUAL</b>' : ($l['expires_at'] ? date('Y-m-d', strtotime($l['expires_at'])) : '-') ?>
                                </td>
                                <td><?= $l['max_users'] ?> Seats<br><small style="color: #94a3b8;"><?= number_format($l['max_studies_per_year']) ?>/yr</small></td>
                                <td>
                                    <?php if ($l['status'] === 'ACTIVE'): ?>
                                        <span class="badge badge-green">ACTIVE</span>
                                    <?php elseif ($l['status'] === 'REVOKED'): ?>
                                        <span class="badge badge-rose">REVOKED</span>
                                    <?php else: ?>
                                        <span class="badge badge-amber"><?= htmlspecialchars($l['status']) ?></span>
                                    <?php endif; ?>
                                </td>
                                <td>
                                    <div style="display: flex; gap: 6px;">
                                        <a href="?download=<?= $l['id'] ?>" class="btn" style="padding: 4px 8px; font-size: 11px;" title="Download .lic binary">📥 .lic</a>
                                        <button class="btn" style="padding: 4px 8px; font-size: 11px;" onclick="copyBase64('<?= htmlspecialchars($l['container_b64']) ?>')" title="Copy Base64 string">📋</button>
                                        
                                        <form method="POST" style="display:inline;" onsubmit="return confirm('Extend this license by 1 year?');">
                                            <input type="hidden" name="csrf_token" value="<?= htmlspecialchars(getCsrfToken()) ?>">
                                            <input type="hidden" name="action" value="extend_license">
                                            <input type="hidden" name="id" value="<?= $l['id'] ?>">
                                            <input type="hidden" name="days_to_add" value="365">
                                            <button type="submit" class="btn" style="padding: 4px 8px; font-size: 11px; color: #34d399;" title="Extend +1 Year">🔄 +1Y</button>
                                        </form>

                                        <?php if ($l['status'] === 'ACTIVE'): ?>
                                            <button class="btn btn-danger" style="padding: 4px 8px; font-size: 11px;" onclick="openRevokeModal(<?= $l['id'] ?>, '<?= htmlspecialchars($l['license_id']) ?>')">🚫</button>
                                        <?php endif; ?>
                                    </div>
                                </td>
                            </tr>
                        <?php endforeach; ?>
                    <?php endif; ?>
                </tbody>
            </table>
        </div>
    </div>

    <!-- Tab 2: Activation Keys -->
    <div id="tab-keys" class="tab-content">
        <div class="panel-card">
            <div class="panel-header">
                <span>Product Activation Keys (Single &amp; Multi-Use)</span>
                <button class="btn btn-primary" onclick="openModal('create-key-modal')">🔑 Generate New Product Key</button>
            </div>
            
            <table class="table">
                <thead>
                    <tr>
                        <th>Product Key</th>
                        <th>Edition</th>
                        <th>Activations Used</th>
                        <th>Allowed Email</th>
                        <th>Validity Period</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    <?php if (empty($keys)): ?>
                        <tr><td colspan="7" style="color: #94a3b8; text-align: center;">No activation keys generated yet.</td></tr>
                    <?php else: ?>
                        <?php foreach ($keys as $k): ?>
                            <tr>
                                <td style="font-family: var(--font-mono); font-weight: 700; color: #38bdf8; font-size: 13px;">
                                    <?= htmlspecialchars($k['product_key']) ?>
                                    <button class="btn" style="padding: 2px 6px; font-size: 10px; margin-left: 6px;" onclick="copyText('<?= htmlspecialchars($k['product_key']) ?>')">Copy</button>
                                </td>
                                <td><span class="badge badge-blue"><?= strtoupper($k['edition']) ?></span></td>
                                <td>
                                    <b><?= $k['activations_count'] ?></b> / <?= $k['max_activations'] ?>
                                    <?php if ($k['activations_count'] >= $k['max_activations']): ?>
                                        <span class="badge badge-rose" style="margin-left: 4px;">EXHAUSTED</span>
                                    <?php endif; ?>
                                </td>
                                <td><?= $k['allowed_email'] ? htmlspecialchars($k['allowed_email']) : '<span style="color:#64748b;">Any</span>' ?></td>
                                <td><?= $k['expires_days'] ?> Days</td>
                                <td>
                                    <?= $k['is_active'] ? '<span class="badge badge-green">ACTIVE</span>' : '<span class="badge badge-rose">DISABLED</span>' ?>
                                </td>
                                <td>
                                    <form method="POST" style="display:inline;">
                                        <input type="hidden" name="csrf_token" value="<?= htmlspecialchars(getCsrfToken()) ?>">
                                        <input type="hidden" name="action" value="toggle_key">
                                        <input type="hidden" name="id" value="<?= $k['id'] ?>">
                                        <input type="hidden" name="state" value="<?= $k['is_active'] ? 0 : 1 ?>">
                                        <button type="submit" class="btn" style="padding: 4px 8px; font-size: 11px;">
                                            <?= $k['is_active'] ? 'Disable' : 'Enable' ?>
                                        </button>
                                    </form>
                                </td>
                            </tr>
                        <?php endforeach; ?>
                    <?php endif; ?>
                </tbody>
            </table>
        </div>
    </div>

    <!-- Tab 3: Telemetry & Heartbeats -->
    <div id="tab-telemetry" class="tab-content">
        <div class="panel-card">
            <div class="panel-header">Recent Heartbeats (Last 50 Check-ins)</div>
            <table class="table">
                <thead>
                    <tr>
                        <th>License ID</th>
                        <th>Hardware Fingerprint</th>
                        <th>Server IP</th>
                        <th>App Version</th>
                        <th>Last Seen (UTC)</th>
                    </tr>
                </thead>
                <tbody>
                    <?php if (empty($heartbeats)): ?>
                        <tr><td colspan="5" style="color: #94a3b8; text-align: center;">No heartbeat pings recorded yet.</td></tr>
                    <?php else: ?>
                        <?php foreach ($heartbeats as $hb): ?>
                            <tr>
                                <td style="font-family: var(--font-mono); font-weight: 700; color: #38bdf8;"><?= htmlspecialchars($hb['license_id']) ?></td>
                                <td style="font-family: var(--font-mono); font-size: 11px;"><?= htmlspecialchars($hb['machine_fingerprint']) ?></td>
                                <td><?= htmlspecialchars($hb['ip_address']) ?></td>
                                <td><span class="badge badge-blue"><?= htmlspecialchars($hb['app_version']) ?></span></td>
                                <td><?= htmlspecialchars($hb['last_seen_at']) ?></td>
                            </tr>
                        <?php endforeach; ?>
                    <?php endif; ?>
                </tbody>
            </table>
        </div>
    </div>

    <!-- Tab 4: Cryptography & Integration -->
    <div id="tab-crypto" class="tab-content">
        <div class="panel-card">
            <div class="panel-header">Vendor Public Key (Deploy to PAXS Servers)</div>
            <p style="color: #94a3b8; font-size: 13px; margin-bottom: 12px;">
                PAXS Archive servers use this RSA-2048 Public Key to verify digital signatures on all licenses generated by this portal.
            </p>
            <textarea class="form-control" style="min-height: 160px; font-family: var(--font-mono); font-size: 11.5px;" readonly><?= htmlspecialchars(PAXS_VENDOR_PUBLIC_KEY) ?></textarea>
            
            <h4 style="margin-top: 24px; color: var(--accent-sky); font-size: 14px;">Online Activation Endpoint URL</h4>
            <div style="background: #020617; border: 1px solid var(--border-color); padding: 12px; border-radius: 8px; font-family: var(--font-mono); font-size: 13px; color: #38bdf8; margin-top: 8px;">
                <?= (isset($_SERVER['HTTPS']) && $_SERVER['HTTPS'] === 'on' ? "https" : "http") . "://{$_SERVER['HTTP_HOST']}" . dirname($_SERVER['SCRIPT_NAME']) . "/api/activate.php" ?>
            </div>
        </div>
    </div>

    <!-- Modal: Issue New License -->
    <div id="create-license-modal" class="modal">
        <div class="modal-content">
            <div class="panel-header">
                <span>Issue License Certificate</span>
                <button type="button" class="btn" onclick="closeModal('create-license-modal')">✕</button>
            </div>
            <form method="POST">
                <input type="hidden" name="csrf_token" value="<?= htmlspecialchars(getCsrfToken()) ?>">
                <input type="hidden" name="action" value="create_license">

                <div class="form-group">
                    <label>Customer / Hospital Name *</label>
                    <input type="text" name="customer_name" class="form-control" placeholder="St. Jude Diagnostic Center" required>
                </div>

                <div class="form-group">
                    <label>Customer Contact Email</label>
                    <input type="email" name="customer_email" class="form-control" placeholder="radiology@hospital.org">
                </div>

                <div class="form-group">
                    <label>Edition</label>
                    <select name="edition" class="form-control" onchange="onEditionChange(this.value)">
                        <?php foreach ($EDITIONS as $k => $e): ?>
                            <option value="<?= $k ?>" <?= $k === 'hospital' ? 'selected' : '' ?>><?= $e['name'] ?></option>
                        <?php endforeach; ?>
                    </select>
                </div>

                <div class="form-group">
                    <label>Host Hardware Fingerprint (32 Hex Characters or * for Cloud)</label>
                    <input type="text" name="fingerprint" class="form-control" value="*" style="font-family: var(--font-mono);" required>
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                    <div class="form-group">
                        <label>Validity (Days)</label>
                        <input type="number" id="lic_days_input" name="validity_days" class="form-control" value="365" min="1">
                    </div>
                    <div class="form-group" style="display: flex; align-items: center; gap: 8px; margin-top: 24px;">
                        <input type="checkbox" id="lic_is_perpetual" name="is_perpetual" value="1" onchange="togglePerpetual(this.checked)">
                        <label for="lic_is_perpetual" style="margin: 0; cursor: pointer;">Perpetual License</label>
                    </div>
                </div>

                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                    <div class="form-group">
                        <label>Max User Seats</label>
                        <input type="number" id="lic_seats_input" name="max_users" class="form-control" value="35" min="1">
                    </div>
                    <div class="form-group">
                        <label>Annual Studies Cap</label>
                        <input type="number" id="lic_studies_input" name="max_studies" class="form-control" value="30000" min="100">
                    </div>
                </div>

                <div class="form-group">
                    <label>Licensed Features &amp; Clinical Modules</label>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 6px;">
                        <?php foreach ($ALL_FEATURES as $fk => $flabel): ?>
                            <label style="font-size: 12px; color: #cbd5e1; display: flex; align-items: center; gap: 6px; cursor: pointer;">
                                <input type="checkbox" name="features[]" value="<?= $fk ?>" checked>
                                <?= $flabel ?>
                            </label>
                        <?php endforeach; ?>
                    </div>
                </div>

                <div style="text-align: right; margin-top: 20px; display: flex; gap: 10px; justify-content: flex-end;">
                    <button type="button" class="btn" onclick="closeModal('create-license-modal')">Cancel</button>
                    <button type="submit" class="btn btn-primary">⚡ Generate &amp; Sign License</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Modal: Generate Product Key -->
    <div id="create-key-modal" class="modal">
        <div class="modal-content">
            <div class="panel-header">
                <span>Generate Product Activation Key</span>
                <button type="button" class="btn" onclick="closeModal('create-key-modal')">✕</button>
            </div>
            <form method="POST">
                <input type="hidden" name="csrf_token" value="<?= htmlspecialchars(getCsrfToken()) ?>">
                <input type="hidden" name="action" value="create_key">

                <div class="form-group">
                    <label>License Edition</label>
                    <select name="edition" class="form-control">
                        <?php foreach ($EDITIONS as $k => $e): ?>
                            <option value="<?= $k ?>" <?= $k === 'hospital' ? 'selected' : '' ?>><?= $e['name'] ?></option>
                        <?php endforeach; ?>
                    </select>
                </div>

                <div class="form-group">
                    <label>Max Allowed Activations</label>
                    <input type="number" name="max_activations" class="form-control" value="1" min="1">
                </div>

                <div class="form-group">
                    <label>Validity Period (Days from activation)</label>
                    <input type="number" name="expires_days" class="form-control" value="365" min="1">
                </div>

                <div class="form-group">
                    <label>Restrict to Customer Email (Optional)</label>
                    <input type="email" name="allowed_email" class="form-control" placeholder="admin@hospital.org">
                </div>

                <div style="text-align: right; margin-top: 20px; display: flex; gap: 10px; justify-content: flex-end;">
                    <button type="button" class="btn" onclick="closeModal('create-key-modal')">Cancel</button>
                    <button type="submit" class="btn btn-primary">🔑 Generate Activation Key</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Modal: Revoke License -->
    <div id="revoke-modal" class="modal">
        <div class="modal-content">
            <div class="panel-header">
                <span style="color: var(--accent-rose);">Revoke License Remote Check</span>
                <button type="button" class="btn" onclick="closeModal('revoke-modal')">✕</button>
            </div>
            <form method="POST">
                <input type="hidden" name="csrf_token" value="<?= htmlspecialchars(getCsrfToken()) ?>">
                <input type="hidden" name="action" value="revoke_license">
                <input type="hidden" name="id" id="revoke_lic_id">

                <p style="color: #cbd5e1; font-size: 13px; margin-bottom: 14px;">
                    Are you sure you want to revoke license <b id="revoke_lic_label" style="color: #38bdf8;"></b>?
                    During the next 24-hour heartbeat check, the server will be notified and disabled.
                </p>

                <div class="form-group">
                    <label>Reason for Revocation</label>
                    <input type="text" name="reason" class="form-control" value="Subscription payment overdue or terms breach" required>
                </div>

                <div style="text-align: right; margin-top: 20px; display: flex; gap: 10px; justify-content: flex-end;">
                    <button type="button" class="btn" onclick="closeModal('revoke-modal')">Cancel</button>
                    <button type="submit" class="btn btn-danger">🚫 Confirm Remote Revocation</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        function switchTab(tabId) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById(tabId).classList.add('active');
        }

        function openModal(id) { document.getElementById(id).classList.add('active'); }
        function closeModal(id) { document.getElementById(id).classList.remove('active'); }

        function openRevokeModal(id, label) {
            document.getElementById('revoke_lic_id').value = id;
            document.getElementById('revoke_lic_label').textContent = label;
            openModal('revoke-modal');
        }

        function copyText(txt) {
            navigator.clipboard.writeText(txt).then(() => alert('Copied to clipboard: ' + txt));
        }

        function copyBase64(b64) {
            navigator.clipboard.writeText(b64).then(() => alert('Base64 License payload copied to clipboard!'));
        }

        function togglePerpetual(isPerp) {
            const inp = document.getElementById('lic_days_input');
            if (isPerp) {
                inp.disabled = true;
                inp.value = '';
            } else {
                inp.disabled = false;
                inp.value = '365';
            }
        }

        const editionSpecs = <?= json_encode($EDITIONS) ?>;
        function onEditionChange(ed) {
            const spec = editionSpecs[ed];
            if (spec) {
                document.getElementById('lic_seats_input').value = spec.max_users;
                document.getElementById('lic_studies_input').value = spec.max_studies;
                document.getElementById('lic_days_input').value = spec.days;
            }
        }

        setTimeout(() => {
            const t = document.querySelector('.toast');
            if (t) t.style.display = 'none';
        }, 4000);
    </script>
</body>
</html>
