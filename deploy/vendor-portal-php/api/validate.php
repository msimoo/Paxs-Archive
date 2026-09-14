<?php
/**
 * REST API: Online License Heartbeat & Validation Endpoint
 * 
 * Periodically invoked by PAXS Archive servers (every 24 hours).
 */

declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization, User-Agent');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(200);
    exit;
}

require_once __DIR__ . '/../config.php';
require_once __DIR__ . '/../db.php';
require_once __DIR__ . '/../crypto.php';

$rawBody = file_get_contents('php://input');
$input = json_decode($rawBody, true);

if (!is_array($input) || empty($input['license_id'])) {
    http_response_code(400);
    echo json_encode([
        'success' => false,
        'message' => 'Missing license_id in request payload.'
    ]);
    exit;
}

$licenseId = trim($input['license_id']);
$fingerprint = strtoupper(trim($input['fingerprint'] ?? ''));
$appVersion = trim($input['app_version'] ?? '0.1.0');
$ip = $_SERVER['REMOTE_ADDR'] ?? '127.0.0.1';

$db = getDb();

// 1. Look up license in database
$stmt = $db->prepare("SELECT * FROM licenses WHERE license_id = ? LIMIT 1");
$stmt->execute([$licenseId]);
$licRow = $stmt->fetch();

if (!$licRow) {
    // License not found in vendor database (issued offline or forged)
    echo json_encode([
        'success' => true,
        'status' => 'UNTRACKED',
        'message' => 'License is untracked on cloud portal.'
    ]);
    exit;
}

// 2. Record / Update Heartbeat Log
try {
    $hb = $db->prepare("
        INSERT INTO heartbeat_log (license_id, machine_fingerprint, ip_address, app_version, last_seen_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
    ");
    $hb->execute([$licenseId, $fingerprint, $ip, $appVersion]);
} catch (Exception $e) {
    // Non-fatal
}

// 3. Check for Revocation
if ($licRow['status'] === 'REVOKED') {
    echo json_encode([
        'success' => false,
        'revoked' => true,
        'reason' => $licRow['revocation_reason'] ?: 'License has been revoked by vendor portal administrator.'
    ]);
    exit;
}

// 4. Return Validation Status and updated container if renewed
$response = [
    'success' => true,
    'status' => $licRow['status'],
    'edition' => strtoupper($licRow['edition']),
    'expires_at' => $licRow['expires_at'],
    'is_perpetual' => (bool)$licRow['is_perpetual'],
    'message' => 'License is active and verified.'
];

// If license was updated in portal (e.g. renewal/seat upgrade), deliver fresh container
if (!empty($licRow['container_b64'])) {
    $response['renewed_license_b64'] = $licRow['container_b64'];
}

echo json_encode($response);
